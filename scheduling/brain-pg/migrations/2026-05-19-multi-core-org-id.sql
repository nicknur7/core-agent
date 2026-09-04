-- Multi-Core architecture v2 — Phase 2 migration
-- Adds org_id to 4 tables, Amendment A unique constraint, tenants registry.
-- Spec: tasks/spec-multi-core-architecture-2026-05-19.md (Phase 2, lines 331-400)
-- Notes:
--   * Rename core-nick → core-life is ALREADY DONE on disk before this migration,
--     so tenants.vault_path for life uses /core-life (not /core-nick from v1 spec text).
--   * Backfill DEFAULT 1 then DROP DEFAULT — forces explicit org_id on future inserts.

BEGIN;

-- ─── Add org_id columns (DEFAULT 1 = backfills existing Nick data as life-Core) ───
ALTER TABLE entities     ADD COLUMN org_id BIGINT NOT NULL DEFAULT 1;
ALTER TABLE evidence     ADD COLUMN org_id BIGINT NOT NULL DEFAULT 1;
ALTER TABLE entity_edges ADD COLUMN org_id BIGINT NOT NULL DEFAULT 1;
ALTER TABLE ingest_log   ADD COLUMN org_id BIGINT NOT NULL DEFAULT 1;

-- ─── Drop DEFAULT after backfill so future inserts must specify org_id explicitly ───
ALTER TABLE entities     ALTER COLUMN org_id DROP DEFAULT;
ALTER TABLE evidence     ALTER COLUMN org_id DROP DEFAULT;
ALTER TABLE entity_edges ALTER COLUMN org_id DROP DEFAULT;
ALTER TABLE ingest_log   ALTER COLUMN org_id DROP DEFAULT;

-- ─── Per-org indexes for scoped reads ───
CREATE INDEX idx_entities_org    ON entities      (org_id);
CREATE INDEX idx_evidence_org    ON evidence      (org_id);
CREATE INDEX idx_edges_org       ON entity_edges  (org_id);
CREATE INDEX idx_ingest_org      ON ingest_log    (org_id);

-- ─── AMENDMENT A: allow same (kind, name) across orgs ───
-- e.g. Jordan-the-person can exist independently as life-org Jordan and business-org Jordan.
ALTER TABLE entities DROP CONSTRAINT entities_kind_name_key;
ALTER TABLE entities ADD CONSTRAINT entities_kind_name_org_unique UNIQUE (org_id, kind, name);

-- ─── Tenants registry ───
CREATE TABLE IF NOT EXISTS tenants (
  org_id      BIGINT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  vault_path  TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tenant rows are per-deployment, NOT in the engine template. Each Core operator
-- seeds their own tenants from their local CORE_INSTANCE paths after running
-- this migration. Example (run from your shell with $CORE_INSTANCE etc. set):
--
--   psql -d corebrain <<SQL
--   INSERT INTO tenants (org_id, name, vault_path) VALUES
--     (1, 'life',     '$HOME/AI Projects/core-life'),
--     (2, 'business', '$HOME/AI Projects/core-business'),
--     (3, 'school',   '$HOME/AI Projects/core-school')
--   ON CONFLICT (org_id) DO NOTHING;
--   SQL
--
-- Engine ships clean of personal paths — strip-check CI fails on hardcoded user home dirs.

COMMIT;
