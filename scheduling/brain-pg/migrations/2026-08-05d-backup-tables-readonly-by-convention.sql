-- 2026-08-05 — revoke writes on the backup tables, and DETECT any new one rather than guessing.
--
-- WHY THIS EXISTS: I reintroduced the exact hole 2026-07-28 closed, eight days later.
-- --------------------------------------------------------------------------------------
-- 2026-08-05b (`pattern_observations` dedupe) backed 275 rows up into
-- `pattern_observations_dupe_backup_20260805` before deleting them, using plain `CREATE TABLE AS`.
-- That inherits the schema's default grants: INSERT/UPDATE/DELETE to brain_app, the role EVERY Core
-- connects as, and no RLS. So a backup spanning multiple orgs was writable and deletable by any
-- Core — precisely the exposure 2026-07-28-backup-tables-readonly.sql closed for two earlier backup
-- tables, on precisely the reasoning it gave: "a backup you can rewrite is not a backup."
--
-- Caught by bin/tests/test_org_isolation.py, which exists because of the July incident:
--
--   FAIL  every org-partitioned table is write-scoped or write-revoked
--         pattern_observations_dupe_backup_20260805
--         (grants ['DELETE','INSERT','UPDATE'], rls=False, org_scoped=False)
--
-- WHY THIS FILE NO LONGER REVOKES BY NAME PATTERN
-- -----------------------------------------------
-- The first version of this migration revoked writes on every table matching `%_dupe_backup_%`,
-- `%_bak_pre_%` or `%_backup_20%`, and its comment claimed that "closes the class". Codex reviewed
-- it before it shipped and was right on two counts, so that approach is gone:
--
--   1. IT CANNOT CLOSE THE CLASS, and claiming so was the more dangerous half. A migration only
--      scans tables that exist WHEN IT RUNS. A backup table created next month still inherits the
--      default write grants; nothing here can reach it. The comment asserted an ongoing guarantee
--      that the mechanism cannot provide — the same defect as a rule claiming fleet-wide state that
--      is only true locally.
--   2. NAME MATCHING IS NOT AUTHORIZATION. A future LIVE table legitimately named something like
--      `..._backup_2027_state` would silently lose brain_app's writes, and the breakage would
--      surface far from this file with nothing pointing back to it. Trading a loud, tested failure
--      for a silent one is the wrong direction.
--
-- So enforcement is layered, and each layer does only what it can actually do:
--
--   · HERE: revoke writes on the three backup tables that exist, BY NAME. Explicit, auditable,
--     no false positives possible.
--   · AT CREATION: the migration that creates a backup revokes writes in the same transaction
--     (2026-08-05b now does this). That is the only place a future backup can be caught reliably,
--     because it is the only place that knows a backup is being made.
--   · AS BACKSTOP: bin/tests/test_org_isolation.py fails if ANY org_id-carrying table is neither
--     write-scoped by RLS nor write-revoked. That is what actually caught this one, and it catches
--     the next one whether or not its name follows a convention.
--
-- The convention patterns survive below only as a DETECTOR: they SELECT for convention-matching
-- tables that are not on the explicit list and RAISE A WARNING naming them. Discovery without
-- silent breakage — it tells you a new backup table appeared and needs a decision, instead of
-- deciding for you.
--
-- RUN THIS AS THE TABLE OWNER, NOT AS brain_admin.
-- ------------------------------------------------
--   psql -d corebrain -f <this file>          # correct — connects as the local owner role
--   psql -d corebrain -U brain_admin -f ...   # WRONG: reports success, changes nothing
--
-- Only the owner (or a superuser) can revoke a grant it did not make. These tables are owned by the
-- local login role, because that is who ran the migration that created them — not brain_admin, which
-- is explicitly NOSUPERUSER (2026-05-20-brain-admin-role.sql) and whose BYPASSRLS confers neither
-- ownership nor grant-option authority. Observed when this was first run wrongly: Postgres emitted
-- `WARNING: no privileges could be revoked for column …` once per column, the DO block still printed
-- its own success NOTICE, psql still exited 0, and role_table_grants afterwards still showed
-- brain_app holding INSERT/UPDATE/DELETE — the test kept failing. It looks applied and is not.
-- (Codex read the statement as table-level and expected no per-column warnings; the per-column
-- warnings above are what Postgres actually emitted, so the observation stands as written.)
--
-- Also: querying `information_schema.table_privileges` AS brain_admin shows only brain_admin's OWN
-- grants, which made it briefly look as though brain_app held nothing at all. Use
-- `role_table_grants`, or better `has_table_privilege`, which reports EFFECTIVE privilege including
-- anything inherited via PUBLIC or role membership.
--
-- Non-destructive, reversible, idempotent: no rows are touched, SELECT is preserved, REVOKE on an
-- already-revoked privilege is a no-op, and a Core with none of these tables does nothing. Tables
-- are NOT dropped — that needs Nick's explicit confirmation, and a backup is exactly the thing you
-- regret deleting.

DO $$
DECLARE
    known text[] := ARRAY[
        'pattern_observations_dupe_backup_20260805',   -- 2026-08-05b dedupe, 275 rows, multi-org
        'entities_bak_pre_origin',                     -- 2026-07-28, 46,584 rows
        'entity_edges_bak_pre_phase1'                  -- 2026-07-28, 30,769 rows
    ];
    t         text;
    n         int := 0;
    unlisted  text[];
BEGIN
    FOREACH t IN ARRAY known
    LOOP
        IF EXISTS (SELECT 1 FROM pg_class c
                     JOIN pg_namespace ns ON ns.oid = c.relnamespace
                    WHERE ns.nspname = 'public' AND c.relname = t AND c.relkind = 'r') THEN
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.%I FROM brain_app', t);
            RAISE NOTICE 'backup table %: write grants revoked from brain_app (SELECT kept)', t;
            n := n + 1;
        ELSE
            RAISE NOTICE 'backup table %: absent, nothing to do', t;
        END IF;
    END LOOP;
    RAISE NOTICE 'processed % backup table(s)', n;

    -- DETECT ONLY. Never revoke from this list — see the note above on why name matching must not
    -- be authorization. A hit here means a new backup table exists and someone must decide whether
    -- it is a backup (add it above, and fix the migration that created it to revoke at creation) or
    -- a live table that merely looks like one (leave it alone).
    SELECT array_agg(c.relname ORDER BY c.relname) INTO unlisted
      FROM pg_class c
      JOIN pg_namespace ns ON ns.oid = c.relnamespace
     WHERE ns.nspname = 'public'
       AND c.relkind = 'r'
       AND NOT (c.relname = ANY (known))
       AND (c.relname LIKE '%\_dupe\_backup\_%' ESCAPE '\'
         OR c.relname LIKE '%\_bak\_pre\_%'     ESCAPE '\'
         OR c.relname LIKE '%\_backup\_20%'     ESCAPE '\');

    IF unlisted IS NOT NULL THEN
        RAISE WARNING 'table(s) match a backup naming convention but are NOT on this migration''s '
                      'explicit list, and were deliberately left untouched: %. '
                      'Decide per table: a real backup -> add it here AND make its creating '
                      'migration revoke at creation; a live table -> leave it. '
                      'bin/tests/test_org_isolation.py is the backstop either way.', unlisted;
    END IF;
END $$;

-- Verify with EFFECTIVE privileges, not just direct grants — this must show f for every write:
--
--   SELECT c.relname,
--          has_table_privilege('brain_app', c.oid, 'INSERT') AS ins,
--          has_table_privilege('brain_app', c.oid, 'UPDATE') AS upd,
--          has_table_privilege('brain_app', c.oid, 'DELETE') AS del,
--          has_table_privilege('brain_app', c.oid, 'SELECT') AS sel
--     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--    WHERE n.nspname = 'public' AND c.relkind = 'r'
--      AND c.relname IN ('pattern_observations_dupe_backup_20260805',
--                        'entities_bak_pre_origin', 'entity_edges_bak_pre_phase1');
--
-- Measured 2026-08-05 after applying as owner: all three tables ins/upd/del = f. The two July
-- tables are also sel = f (admin-only, per 2026-07-28c); the new one keeps sel = t on purpose, so
-- the dedupe backup stays readable for inspection.
