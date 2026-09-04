#!/usr/bin/env bash
# init-brain-roles.sh — Create the brain_app + brain_admin Postgres roles + grants.
#
# Idempotent: safe to re-run. CREATE ROLE is guarded by IF NOT EXISTS; GRANTs are
# additive. Run once per machine AFTER the corebrain DB + schema.sql exist and
# BEFORE first recall.
#
# Why this exists: brain_app's role + grants were previously a manual "run once"
# comment block at the bottom of migrations/2026-05-19-rls-enforcement.sql, which
# fresh setups (and forks) skip — leaving query.py/embed.py unable to connect as
# brain_app, so recall silently degrades to the grep fallback. Surfaced by an
# external fork's migration, 2026-05-26. Pairs with the brain_app check in core-doctor.sh.
#
# brain_admin added 2026-05-28 (Phase 4, spec-brain-unfreeze): the ingestion path
# (embed.py) connects as brain_admin (NOSUPERUSER BYPASSRLS) to write rows tagged
# with each source file's org_id. It lived ONLY in migrations/2026-05-20-brain-admin-
# role.sql with a bare un-guarded CREATE ROLE — fresh forks that skip that migration
# (like the fork above, 2026-05-26) get an embed.py that can't connect. Now auto-created here,
# idempotently, alongside brain_app so DB prereqs travel with the code.
#
# Usage: bash bin/init-brain-roles.sh
set -euo pipefail

DB="${COREBRAIN_DB:-corebrain}"

if ! command -v psql >/dev/null 2>&1; then
  echo "ERROR: psql not found — install Postgres first (see INSTALL / install-deps)." >&2
  exit 1
fi
if ! psql -d "$DB" -c 'SELECT 1' >/dev/null 2>&1; then
  echo "ERROR: cannot reach database '$DB'. Create it + apply scheduling/brain-pg/schema.sql first." >&2
  exit 1
fi

echo "[init-brain-roles] ensuring brain_app role + grants on '$DB'..."
psql -d "$DB" -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'brain_app') THEN
    CREATE ROLE brain_app WITH LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END\$\$;
GRANT CONNECT ON DATABASE $DB TO brain_app;
GRANT USAGE ON SCHEMA public TO brain_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON entities, evidence, entity_edges, ingest_log TO brain_app;
GRANT SELECT ON tenants TO brain_app;
-- ALL EXISTING TABLES, not just the five named above (2026-08-31).
--
-- ALTER DEFAULT PRIVILEGES below only affects tables created AFTER it runs. Every table any
-- migration created — assertions, the whole SI spine (artifacts, friction_cases,
-- learned_contracts, pattern_observations, si_*), workflow_steps, workflow_triggers, sources —
-- therefore had NO grant at all, and whether a fresh Core works depended on whether
-- run-migrations.sh happened to run before or after this script. Found when a pg_restore
-- rebuilt the DB and 28 tables came back ungranted: brain-health reported "permission denied
-- for table assertions" and the SI loop would have failed silently on its next nightly.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO brain_app;
-- RE-NARROW THE TABLES THIS GRANT SHOULD NEVER HAVE WIDENED, RIGHT WHERE IT WIDENS THEM.
--
-- FOUND 2026-09-03, bin/tests/test_org_isolation.py auditing a fresh clone: tenants failed with
-- brain_app holding full write access, RLS off. Traced to an ORDERING BUG, not a one-table gap.
-- bin/setup-brain.sh runs run-migrations.sh (step 4) BEFORE this script (step 5) — so every
-- migration that carefully REVOKEs brain_app's writes on a specific table (tenants: 2026-09-01;
-- pattern_observations_dupe_backup_20260805 + two pre-origin backups: 2026-08-05d;
-- entities_bak_orphans/entity_edges_bak_orphans/merge_journal_20260828: 2026-08-31c) has its work
-- silently undone by the blanket GRANT two lines above, which runs strictly after every one of
-- them on a fresh install. Worse: 2026-09-01's own guard against erroring when brain_app doesn't
-- exist yet (IF NOT EXISTS ... RAISE NOTICE ... RETURN) makes it a no-op on that exact fresh-
-- install path — and run-migrations.sh still records it as applied, so it never runs again.
--
-- Reordering the two scripts was considered and rejected: migrations/2026-05-20-brain-admin-
-- role.sql does a bare (unguarded) CREATE ROLE brain_admin, which only succeeds today because
-- migrations run before this script creates that role idempotently — reversing the order would
-- break that migration on every fresh install for a different reason than the one being fixed.
--
-- So this script — which OWNS brain_app's grants and is immune to migration-ordering, since it is
-- always the last word on what brain_app can touch — is the right place to re-apply the narrowing,
-- not the migrations. NAMED, not pattern-matched (2026-08-05d already explains why: a future live
-- table whose name happens to match a backup convention must not silently lose its writes). A
-- table matching the convention but NOT on this list is reported, never touched.
DO \$\$
DECLARE
    -- SELECT kept (writer is always admin): tenants (2026-09-01), and the dedupe backup, which
    -- 2026-08-05d keeps readable "on purpose, so the dedupe backup stays readable for inspection".
    ro   text[] := ARRAY['tenants', 'pattern_observations_dupe_backup_20260805'];
    -- admin-only, no brain_app access at all: 2026-07-28c (entities/entity_edges orphans) and
    -- 2026-08-31c (merge_journal, which "revokes brain_app's access outright — it never needed any").
    none text[] := ARRAY['entities_bak_orphans', 'entity_edges_bak_orphans',
                          'merge_journal_20260828'];
    t        text;
    unlisted text[];
BEGIN
    FOREACH t IN ARRAY ro LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.%I FROM brain_app', t);
        END IF;
    END LOOP;
    FOREACH t IN ARRAY none LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON public.%I FROM brain_app', t);
        END IF;
    END LOOP;

    SELECT array_agg(c.relname ORDER BY c.relname) INTO unlisted
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'r'
       AND NOT (c.relname = ANY (ro || none))
       AND (c.relname LIKE '%\_dupe\_backup\_%' ESCAPE '\'
         OR c.relname LIKE '%\_bak\_%' ESCAPE '\'
         OR c.relname LIKE '%\_backup\_20%' ESCAPE '\'
         OR c.relname LIKE 'merge\_journal\_%' ESCAPE '\');
    IF unlisted IS NOT NULL THEN
        RAISE WARNING 'table(s) match a backup/registry naming convention but are not on this '
                      'script''s re-narrow list, so brain_app keeps whatever the blanket grant '
                      'gave it: %. Decide per table, per 2026-08-05d''s reasoning.', unlisted;
    END IF;
END \$\$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO brain_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO brain_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO brain_app;
SQL

echo "[init-brain-roles] ensuring brain_admin role + grants on '$DB'..."
psql -d "$DB" -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'brain_admin') THEN
    CREATE ROLE brain_admin WITH LOGIN NOSUPERUSER NOINHERIT BYPASSRLS PASSWORD NULL;
  END IF;
END\$\$;
GRANT CONNECT ON DATABASE $DB TO brain_admin;
GRANT USAGE ON SCHEMA public TO brain_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON entities, evidence, entity_edges, ingest_log, tenants TO brain_admin;
-- Same reason as brain_app above: cover every table that already exists, not only future ones.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO brain_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO brain_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO brain_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO brain_admin;
SQL

echo "[init-brain-roles] ✓ brain_app + brain_admin ready. Verify with: psql -d $DB -c '\\du brain_a*'"
