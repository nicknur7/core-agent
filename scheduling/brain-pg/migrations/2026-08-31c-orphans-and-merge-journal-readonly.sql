-- 2026-08-31 — close the SAME hole a fourth time, on three tables that never got it closed once.
--
-- FOUND BY bin/tests/test_org_isolation.py, RUN FOR REAL (2026-08-31)
-- ---------------------------------------------------------------------
-- `entities_bak_orphans` and `entity_edges_bak_orphans` were created earlier tonight (T018,
-- decisions-log ~4911: "preserve-then-drop, not drop-or-keep" — 277 entities + 495 edges that
-- existed ONLY in the pre-origin/pre-phase1 backups, copied out before those backups were
-- dropped). test_org_isolation.py's own comment for these two tables reads:
--
--     "THE SUCCESSORS INHERIT THE DEFECT, AND I CREATED IT. ... Revoked immediately; asserted
--      here so it cannot come back."
--
-- That comment is wrong about the live database. Verified 2026-08-31 by direct query
-- (`information_schema.role_table_grants`, then `has_table_privilege` to rule out a PUBLIC or
-- role-inheritance path per the 2026-08-05 Codex finding): brain_app holds INSERT/UPDATE/DELETE
-- on both tables today, RLS is off (relrowsecurity = f), and SELECT was never revoked either. The
-- revoke the comment describes was never actually run against this seat's corebrain — the same
-- "looks applied and is not" shape 2026-08-05d already documented for a different table (wrong
-- role, or never run at all; the grant catalog can't distinguish the two and it doesn't matter —
-- the fix is the same either way).
--
-- `merge_journal_20260828` (bin/canonical-merge-apply.py:53) has the identical shape and was
-- never covered by ANY migration: full-row JOURNAL for tonight's canonical-merge runs, org_id
-- column present, RLS off, brain_app holds INSERT/UPDATE/DELETE/SELECT. The merge tool itself
-- connects as brain_admin (`_env.connect_corebrain_admin()`, line 81 — "BYPASSRLS: a loser can
-- be an endpoint on another org's edge row"), NOT brain_app, so brain_app's grants here are pure
-- default-grant residue from CREATE TABLE, not anything the application needs. Unlike the two
-- backup tables above, this one is still being actively appended to by design (it is the undo
-- log for the merge that is currently landing) — so this migration revokes brain_app's access
-- outright (it never needed any) rather than touching brain_admin's, which must keep writing it.
--
-- THE PATTERN, again: this is the third time a `CREATE TABLE AS` / ad-hoc DDL against corebrain
-- has landed a multi-org table with default grants and no RLS (2026-07-28, 2026-08-05b, now
-- T018 + the merge journal). 2026-08-05d already named the actual fix for the class ("AT
-- CREATION: the migration that creates a backup revokes writes in the same transaction") — that
-- discipline did not hold here because these three tables were created by hand, outside any
-- tracked migration, so there was no creation-time transaction to put the revoke in. Recorded so
-- the next one is caught the same way this one was: bin/tests/test_org_isolation.py, which is the
-- backstop regardless of whether the creating operation was a migration file or a psql session.
--
-- pattern_observations_dupe_backup_20260805 is NOT re-touched here — 2026-08-05d already targets
-- it by name and correctly leaves its SELECT grant in place ("on purpose, so the dedupe backup
-- stays readable for inspection"). Its writes are STILL granted live for the same reason this
-- file exists: re-run 2026-08-05d itself, AS THE TABLE OWNER per its own header
-- (`psql -d corebrain -f 2026-08-05d-...sql`, not `-U brain_admin`) — do not duplicate its REVOKE
-- here, or the two files will silently disagree the day one of them is edited.
--
-- Non-destructive, reversible, idempotent: no rows touched, REVOKE on an already-revoked
-- privilege is a no-op, and the DO block skips any table that does not exist (a Core spawned
-- after tonight, or one without T018 applied, never had these). Tables are NOT dropped — that is
-- Nick's call, same reasoning as every prior migration in this file's lineage.
--
-- RUN AS THE TABLE OWNER, NOT brain_admin (2026-08-05d's warning applies identically here):
--   psql -d corebrain -f scheduling/brain-pg/migrations/2026-08-31c-orphans-and-merge-journal-readonly.sql
--
-- Verify afterward with EFFECTIVE privileges, not direct grants:
--   SELECT c.relname,
--          has_table_privilege('brain_app', c.oid, 'INSERT') AS ins,
--          has_table_privilege('brain_app', c.oid, 'UPDATE') AS upd,
--          has_table_privilege('brain_app', c.oid, 'DELETE') AS del,
--          has_table_privilege('brain_app', c.oid, 'SELECT') AS sel
--     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--    WHERE n.nspname = 'public' AND c.relkind = 'r'
--      AND c.relname IN ('entities_bak_orphans', 'entity_edges_bak_orphans',
--                         'merge_journal_20260828');
-- Expect ins/upd/del/sel = f for all three. Then: python3 bin/tests/test_org_isolation.py

DO $$
DECLARE
    t         text;
    admin_ok  boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brain_admin');
BEGIN
    -- entities_bak_orphans / entity_edges_bak_orphans: same treatment as the pre-origin/
    -- pre-phase1 tables they succeeded (2026-07-28c) — admin-readable, not app-readable.
    FOREACH t IN ARRAY ARRAY['entities_bak_orphans', 'entity_edges_bak_orphans']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_class c
                     JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = t AND c.relkind = 'r') THEN
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE, SELECT ON public.%I FROM brain_app', t);
            IF admin_ok THEN
                EXECUTE format('GRANT SELECT ON public.%I TO brain_admin', t);
            END IF;
            RAISE NOTICE 'backup table %: all brain_app grants revoked, SELECT granted to brain_admin', t;
        ELSE
            RAISE NOTICE 'backup table %: absent, nothing to do', t;
        END IF;
    END LOOP;

    -- merge_journal_20260828: brain_app never legitimately touches this (the merge tool connects
    -- as brain_admin). Revoke everything brain_app was handed by default at CREATE TABLE time.
    IF EXISTS (SELECT 1 FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = 'merge_journal_20260828' AND c.relkind = 'r') THEN
        EXECUTE 'REVOKE INSERT, UPDATE, DELETE, TRUNCATE, SELECT ON public.merge_journal_20260828 FROM brain_app';
        RAISE NOTICE 'merge_journal_20260828: all brain_app grants revoked (writer is brain_admin, not brain_app)';
    ELSE
        RAISE NOTICE 'merge_journal_20260828: absent, nothing to do';
    END IF;
END $$;
