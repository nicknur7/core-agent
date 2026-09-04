-- 2026-05-20: brain_admin role for cross-org writes
--
-- Why: embed.py needs to write rows tagged with the SOURCE FILE's org_id
-- (projects/business/... → org_id=2, projects/school/... → org_id=3) regardless
-- of which Core invoked the script. RLS write-own policies on entities/evidence/
-- entity_edges/ingest_log restrict INSERT/UPDATE/DELETE to current_setting org_id,
-- which would block cross-org writes.
--
-- brain_admin is NOSUPERUSER but BYPASSRLS — only the ingestion path uses it.
-- All recall paths continue to connect as brain_app (NOSUPERUSER NOBYPASSRLS).
--
-- Production pattern: OpenSanctions resolver runs as admin; MemClaw's
-- crystallization runs as admin. The "system process" tier needs cross-org
-- write authority that user-facing recall does NOT.
--
-- Reverse SQL at bottom.

CREATE ROLE brain_admin NOSUPERUSER NOINHERIT LOGIN BYPASSRLS
  PASSWORD NULL;  -- peer authentication via local socket; no password

GRANT CONNECT ON DATABASE corebrain TO brain_admin;
GRANT USAGE ON SCHEMA public TO brain_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  entities, evidence, entity_edges, ingest_log, tenants
  TO brain_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO brain_admin;

-- Sanity probe: list role attributes (visible only when run interactively)
-- SELECT rolname, rolbypassrls, rolsuper FROM pg_roles WHERE rolname='brain_admin';


-- ─── Reverse SQL ─────────────────────────────────────────────────────────
-- REVOKE SELECT, INSERT, UPDATE, DELETE ON entities, evidence, entity_edges,
--   ingest_log, tenants FROM brain_admin;
-- REVOKE USAGE ON SCHEMA public FROM brain_admin;
-- REVOKE CONNECT ON DATABASE corebrain FROM brain_admin;
-- DROP ROLE brain_admin;
