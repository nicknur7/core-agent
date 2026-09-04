-- 2026-07-28 — make pre-migration backup tables READ-ONLY to the application role.
--
-- THE HOLE
-- --------
-- corebrain's isolation model is deliberately asymmetric: cross-org READS are open (that is the
-- peer-Core feature — life reads business through MCP), and WRITES are org-scoped by RLS. On the
-- live tables that holds. Measured, as brain_app with app.current_org_id = 1, inside a
-- transaction that was rolled back:
--
--   UPDATE entities            SET org_id=org_id WHERE org_id=4  ->     0 rows   (RLS enforces)
--   UPDATE entities_bak_pre_origin      ... WHERE org_id=4       -> 4,278 rows   (no RLS)
--   DELETE entity_edges_bak_pre_phase1  ... WHERE org_id=4       -> 4,417 rows   (no RLS)
--
-- Two backup tables carry org_id, hold 46,584 entities and 30,769 edges spanning all four Cores,
-- have NO row-level security, and grant INSERT/UPDATE/DELETE to brain_app — the role every Core
-- connects as. So any Core could silently corrupt or destroy another Core's pre-migration backup.
-- Not a read exposure (reads are open by design); a WRITE exposure, which is the half the model
-- actually protects.
--
-- Found while auditing why 27 tables carry org_id but only 23 have RLS.
--
-- THE FIX
-- -------
-- These are backups. Nothing should ever write them — not this Core, not any Core. So rather than
-- adding org-scoped write policies (which would still permit a Core to rewrite its OWN backup, and
-- a backup you can rewrite is not a backup), the write grants are revoked outright.
--
-- Non-destructive and reversible: no rows are touched, SELECT is preserved, and the grants can be
-- restored with a GRANT if a restore ever legitimately needs to write. Deleting the tables would
-- also close the hole and is NOT done here — that needs Nick's explicit confirmation, and a
-- pre-migration backup is exactly the thing you regret deleting.
--
-- Idempotent: REVOKE on an already-revoked privilege is a no-op, and the DO block skips tables
-- that do not exist (a Core spawned after these migrations never had them).

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['entities_bak_pre_origin', 'entity_edges_bak_pre_phase1']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relname = t AND c.relkind = 'r') THEN
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.%I FROM brain_app', t);
            RAISE NOTICE 'backup table %: write grants revoked from brain_app (SELECT kept)', t;
        ELSE
            RAISE NOTICE 'backup table %: absent, nothing to do', t;
        END IF;
    END LOOP;
END $$;

-- Belt and braces: any FUTURE table matching the pre-migration backup naming convention should
-- start read-only too. This cannot be expressed as a policy, so it is a check the freshness/health
-- pass can assert rather than something enforced here — see bin/tests/test_org_isolation.py.
