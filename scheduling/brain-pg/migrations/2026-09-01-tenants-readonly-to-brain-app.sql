-- tenants: revoke brain_app's write grants. The registry of Cores is admin-only.
--
-- WHY (2026-09-01). bin/tests/test_org_isolation.py asserts every org-partitioned table is either
-- write-scoped by RLS or write-revoked. `tenants` was neither: brain_app held INSERT/UPDATE/DELETE,
-- the table has no RLS, and it is not org-scoped — it IS the org list. So any Core, running as the
-- unprivileged application role, could add, rename, repoint or delete a tenant row. That is the one
-- table where a bad write is not a data error but an identity error: repoint tenants.vault_path or
-- delete an org row and every downstream org lookup, health check and partition report silently
-- describes a fleet that does not exist.
--
-- Verified before revoking rather than assumed — every writer already runs as the database OWNER,
-- not as brain_app:
--   bin/setup-brain.sh:87          psql -d "$DB"            INSERT ... ON CONFLICT DO NOTHING
--   bin/init-multi-core.sh:110     psql -d "$COREBRAIN_DB"  INSERT on Core spawn
--   scheduling/brain-pg/teardown-org.py:38  DELETE — an explicitly manual, destructive admin tool
--                                  (bin/wiring-allowlist.json: "must never be automatic")
-- so nothing loses a capability it was using. SELECT is kept: every org lookup and the health
-- checks read this table on every run.
--
-- Caught only because test_org_isolation is a whole-class assertion rather than a list of known
-- tables — it surfaced three backup tables, then merge_journal, then this, one per fix round. A
-- test that enumerates what it expects to be wrong would have found none of them.
--
-- Idempotent: REVOKE on an absent grant is a no-op. Run as the table owner:
--   psql -d corebrain -f scheduling/brain-pg/migrations/2026-09-01-tenants-readonly-to-brain-app.sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brain_app') THEN
        RAISE NOTICE 'brain_app role absent — nothing to revoke (fresh install before init-brain-roles)';
        RETURN;
    END IF;
    IF to_regclass('public.tenants') IS NULL THEN
        RAISE NOTICE 'tenants absent — nothing to do';
        RETURN;
    END IF;

    REVOKE INSERT, UPDATE, DELETE ON public.tenants FROM brain_app;
    GRANT SELECT ON public.tenants TO brain_app;
    RAISE NOTICE 'tenants: brain_app write grants revoked, SELECT kept (writers run as owner)';
END $$;
