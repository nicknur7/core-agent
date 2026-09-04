-- 2026-05-19 RLS deployment on multi-Core brain tables (C0).
-- Iterated 2026-05-20: "read all, write own" model.
--
-- STATUS: APPLIED. Live in corebrain DB. Reverse SQL at bottom.
--
-- Setup (one-time):
--   1. CREATE ROLE brain_app WITH LOGIN NOSUPERUSER NOBYPASSRLS;
--   2. Grant minimum perms:
--        GRANT CONNECT ON DATABASE corebrain TO brain_app;
--        GRANT USAGE ON SCHEMA public TO brain_app;
--        GRANT SELECT, INSERT, UPDATE, DELETE ON entities, evidence, entity_edges, ingest_log TO brain_app;
--        GRANT SELECT ON tenants TO brain_app;
--        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO brain_app;
--        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO brain_app;
--        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO brain_app;
--   3. Apply this migration (below).
--   4. Update _env.py:connect_corebrain() to use user='brain_app' (done in this same commit batch).
--
-- Policy model: "read all, write own"
--   • SELECT: any org_id visible (cross-Core recall works — school can read life's history).
--   • INSERT: only with your own org_id (set via session var app.current_org_id).
--   • UPDATE: only rows matching your org_id (can't modify others' rows OR change org_id to escape).
--   • DELETE: only rows matching your org_id.
--
-- Verified isolation tests:
--   • As org=3: SELECT count(*) FROM entities → 9913 (all life data visible).
--   • As org=3: INSERT with org_id=3 → succeeds.
--   • As org=3: INSERT with org_id=1 → BLOCKED ("new row violates row-level security policy").
--   • As org=3: UPDATE org_id=1 row → affects 0 rows.
--   • As org=3: DELETE org_id=1 row → affects 0 rows.
--
-- current_setting(..., true): second arg is missing_ok=true. Returns NULL
-- if session var unset. NULL::bigint = anything → NULL → row filtered out
-- of write paths (safe default — can't insert without setting the var).

BEGIN;

-- entities
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities FORCE ROW LEVEL SECURITY;
CREATE POLICY read_all ON entities FOR SELECT USING (true);
CREATE POLICY insert_own ON entities FOR INSERT WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
CREATE POLICY update_own ON entities FOR UPDATE
  USING (org_id = current_setting('app.current_org_id', true)::bigint)
  WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
CREATE POLICY delete_own ON entities FOR DELETE USING (org_id = current_setting('app.current_org_id', true)::bigint);

-- evidence
ALTER TABLE evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY read_all ON evidence FOR SELECT USING (true);
CREATE POLICY insert_own ON evidence FOR INSERT WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
CREATE POLICY update_own ON evidence FOR UPDATE
  USING (org_id = current_setting('app.current_org_id', true)::bigint)
  WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
CREATE POLICY delete_own ON evidence FOR DELETE USING (org_id = current_setting('app.current_org_id', true)::bigint);

-- entity_edges
ALTER TABLE entity_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_edges FORCE ROW LEVEL SECURITY;
CREATE POLICY read_all ON entity_edges FOR SELECT USING (true);
CREATE POLICY insert_own ON entity_edges FOR INSERT WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
CREATE POLICY update_own ON entity_edges FOR UPDATE
  USING (org_id = current_setting('app.current_org_id', true)::bigint)
  WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
CREATE POLICY delete_own ON entity_edges FOR DELETE USING (org_id = current_setting('app.current_org_id', true)::bigint);

-- ingest_log
ALTER TABLE ingest_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_log FORCE ROW LEVEL SECURITY;
CREATE POLICY read_all ON ingest_log FOR SELECT USING (true);
CREATE POLICY insert_own ON ingest_log FOR INSERT WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
CREATE POLICY update_own ON ingest_log FOR UPDATE
  USING (org_id = current_setting('app.current_org_id', true)::bigint)
  WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
CREATE POLICY delete_own ON ingest_log FOR DELETE USING (org_id = current_setting('app.current_org_id', true)::bigint);

COMMIT;

-- ROLLBACK SQL (run if anything breaks):
--   DROP POLICY read_all ON entities;
--   DROP POLICY insert_own ON entities;
--   DROP POLICY update_own ON entities;
--   DROP POLICY delete_own ON entities;
--   ALTER TABLE entities DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE entities NO FORCE ROW LEVEL SECURITY;
--   (repeat for evidence, entity_edges, ingest_log)
--
-- Companion role + grants (run once before this migration the first time):
--   DO $$ BEGIN
--     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'brain_app') THEN
--       CREATE ROLE brain_app WITH LOGIN NOSUPERUSER NOBYPASSRLS;
--     END IF;
--   END$$;
--   GRANT CONNECT ON DATABASE corebrain TO brain_app;
--   GRANT USAGE ON SCHEMA public TO brain_app;
--   GRANT SELECT, INSERT, UPDATE, DELETE ON entities, evidence, entity_edges, ingest_log TO brain_app;
--   GRANT SELECT ON tenants TO brain_app;
--   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO brain_app;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO brain_app;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO brain_app;
