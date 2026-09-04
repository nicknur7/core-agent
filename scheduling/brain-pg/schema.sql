-- Brain primitives schema — canonical reference for corebrain DB.
-- Deployed live 2026-05-13; bi-temporal columns added 2026-05-17 (D1 fold from spec-corpus-keeper-2026-05-16.md).
-- Multi-Core org_id partitioning + Amendment A unique constraint + tenants table added 2026-05-19
--   (spec-multi-core-architecture-2026-05-19.md Phase 2 — migration file
--    migrations/2026-05-19-multi-core-org-id.sql).
-- Idempotent: safe to re-run on a fresh DB or to verify drift.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- entities: compiled-truth layer (one row per entity/topic/project/tool hub).
CREATE TABLE IF NOT EXISTS entities (
  id                BIGSERIAL PRIMARY KEY,
  name              TEXT NOT NULL,
  -- 'Source' = origin-hub kind (origin backbone). Folded from migrations/2026-07-04-brain-connectivity.sql
  -- so a fresh schema.sql is the complete current end-state (B3, federated-brain-plan §9).
  -- 'Workflow' added 2026-08-10, catching schema.sql up to migration 2026-08-05-workflow-steps.sql,
  -- which widened this CHECK on the live DB and was never reflected here. 12 rows already carry it.
  -- A fresh Core provisioned from this file would have REJECTED them — the precise failure
  -- verify-schema-checks.py exists to catch, and it had been reporting the drift unread.
  kind              TEXT NOT NULL CHECK (kind = ANY (ARRAY['Topic','Tool','Entity','Project','Decision','Lesson','Rule','Incident','Source','Workflow'])),
  compiled_truth_md TEXT,
  last_compiled_at  TIMESTAMPTZ,
  confidence        REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  ownership_tag     TEXT CHECK (ownership_tag IS NULL OR ownership_tag = ANY (ARRAY['work','personal-style','shared'])),
  -- Content-privacy scope (Phase 3, federated brain). Dedicated column (M3 — NOT
  -- ownership_tag reuse). NOT NULL DEFAULT 'shared' (M2) => today's mutual awareness.
  -- 'private' hides CONTENT cross-Core (query.py _visibility_filter/_truth_redact).
  -- Folded from migrations/2026-07-07-federated-phase3-scope.sql.
  scope             TEXT NOT NULL DEFAULT 'shared' CHECK (scope = ANY (ARRAY['shared','private'])),
  source_file       TEXT,
  embedding         vector(1024),
  -- Bi-temporal fields (Zep Graphiti pattern, D1 fold from spec-corpus-keeper-2026-05-16.md:128)
  valid_from        TIMESTAMPTZ DEFAULT now(),
  valid_until       TIMESTAMPTZ,
  superseded_by     BIGINT REFERENCES entities(id) ON DELETE SET NULL,
  -- Multi-Core org_id partitioning (spec-multi-core-architecture-2026-05-19.md Phase 2).
  -- 1=life, 2=business, 3=school. New inserts must specify explicitly (no DEFAULT).
  org_id            BIGINT NOT NULL,
  created_at        TIMESTAMPTZ DEFAULT now(),
  updated_at        TIMESTAMPTZ DEFAULT now(),
  -- Amendment A: allow same (kind, name) across orgs (Jordan-life ≠ Jordan-business).
  CONSTRAINT entities_kind_name_org_unique UNIQUE (org_id, kind, name)
);

CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities (kind);
CREATE INDEX IF NOT EXISTS idx_entities_name_trgm ON entities USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entities_ownership ON entities (ownership_tag) WHERE ownership_tag IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entities_scope ON entities (scope) WHERE scope = 'private';
CREATE INDEX IF NOT EXISTS idx_entities_embedding ON entities USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_entities_truth_fts ON entities USING gin (to_tsvector('english', COALESCE(compiled_truth_md, '')));
CREATE INDEX IF NOT EXISTS idx_entities_validity ON entities (valid_until) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_entities_org ON entities (org_id);

DROP TRIGGER IF EXISTS entities_touch_updated_at ON entities;
CREATE TRIGGER entities_touch_updated_at BEFORE UPDATE ON entities
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- evidence: append-only fact layer (one row per chunk/excerpt from source markdown).
CREATE TABLE IF NOT EXISTS evidence (
  id            BIGSERIAL PRIMARY KEY,
  entity_id     BIGINT REFERENCES entities(id) ON DELETE CASCADE,
  source_file   TEXT NOT NULL,
  source_offset INTEGER,
  excerpt       TEXT NOT NULL,
  session_date  DATE,
  chunk_id      TEXT,
  embedding     vector(1024),
  -- Bi-temporal fields
  valid_from    TIMESTAMPTZ DEFAULT now(),
  valid_until   TIMESTAMPTZ,
  superseded_by BIGINT REFERENCES evidence(id) ON DELETE SET NULL,
  -- Multi-Core partitioning (1=life, 2=business, 3=school).
  org_id        BIGINT NOT NULL,
  -- Content-privacy scope (Phase 3, B4): evidence inherits its parent entity's
  -- scope so a 'private' concept is hidden at the raw-fact layer too, not just the
  -- hub. NOT NULL DEFAULT 'shared'. Folded from 2026-07-07-federated-phase3-scope.sql.
  scope         TEXT NOT NULL DEFAULT 'shared' CHECK (scope = ANY (ARRAY['shared','private'])),
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_evidence_entity ON evidence (entity_id);
CREATE INDEX IF NOT EXISTS idx_evidence_source_file ON evidence (source_file);
CREATE INDEX IF NOT EXISTS idx_evidence_session_date ON evidence (session_date DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_embedding ON evidence USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_evidence_excerpt_fts ON evidence USING gin (to_tsvector('english', excerpt));
CREATE INDEX IF NOT EXISTS idx_evidence_validity ON evidence (valid_until) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_evidence_org ON evidence (org_id);
CREATE INDEX IF NOT EXISTS idx_evidence_scope ON evidence (scope) WHERE scope = 'private';

-- entity_edges: typed relations.
CREATE TABLE IF NOT EXISTS entity_edges (
  id               BIGSERIAL PRIMARY KEY,
  from_entity_id   BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  to_entity_id     BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  -- 'originates_in' = origin backbone edge (entity -> its Source hub). Folded from
  -- migrations/2026-07-04-brain-connectivity.sql so a fresh schema.sql is complete (B3, §9).
  -- 'next_step' added 2026-08-10, same migration, same reason. NOTE the asymmetry: 0 rows use it
  -- today, so unlike 'Workflow' this one breaks nothing yet. It is added anyway because the checker
  -- compares CONSTRAINT DEFINITIONS, not data — a fresh Core must be able to accept what the live
  -- one accepts, or the divergence surfaces later as an insert failure on a Core nobody has touched.
  edge_type        TEXT NOT NULL CHECK (edge_type = ANY (ARRAY['motivated_by','learned_from','supersedes','cross_impacts','references','originates_in','same_as','next_step'])),
  confidence       REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  confidence_label TEXT CHECK (confidence_label IS NULL OR confidence_label = ANY (ARRAY['EXTRACTED','INFERRED','AMBIGUOUS','NONE'])),
  source_file      TEXT,
  -- Edge relation embedding (4th retrieval leg, Practical GraphRAG arxiv 2507.03226).
  -- Folded in from migrations/2026-05-20-edge-embeddings.sql so a fresh schema.sql
  -- is the complete current end-state. Nullable — query.py ignores NULL-embedding edges.
  embedding        vector(1024),
  -- Multi-Core partitioning.
  org_id           BIGINT NOT NULL,
  -- Cross-Core corroboration (Phase 2): true for a same_as edge whose endpoints
  -- span two orgs. Exempt from per-org visibility filtering (M10) so the
  -- breadcrumb survives the Phase-3 scope clause. Folded from
  -- migrations/2026-07-07-federated-phase2-same-as.sql.
  is_cross_org     BOOLEAN NOT NULL DEFAULT false,
  created_at       TIMESTAMPTZ DEFAULT now(),
  UNIQUE (from_entity_id, to_entity_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON entity_edges (from_entity_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON entity_edges (to_entity_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON entity_edges (edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_org ON entity_edges (org_id);
CREATE INDEX IF NOT EXISTS idx_edges_cross_org ON entity_edges (is_cross_org) WHERE is_cross_org;
CREATE INDEX IF NOT EXISTS idx_entity_edges_embedding_hnsw ON entity_edges USING hnsw (embedding vector_cosine_ops);

-- ingest_log: tracks which source files have been ingested + their mtime for incremental embed.
CREATE TABLE IF NOT EXISTS ingest_log (
  id               BIGSERIAL PRIMARY KEY,
  source_file      TEXT NOT NULL UNIQUE,
  last_mtime       TIMESTAMPTZ NOT NULL,
  last_size        BIGINT NOT NULL,
  chunks_extracted INTEGER DEFAULT 0,
  nodes_emitted    INTEGER DEFAULT 0,
  edges_emitted    INTEGER DEFAULT 0,
  -- Multi-Core partitioning.
  org_id           BIGINT NOT NULL,
  embedded_at      TIMESTAMPTZ DEFAULT now(),
  -- Content hash of the source file (sha256), for the two-tier incremental-embed gate in
  -- embed.py's needs_reembed()/update_ingest_log() (2026-07-26 fix — the old mtime-only gate
  -- re-embedded ~2,400 byte-identical files through Voyage after a mass mtime bump). embed.py
  -- has written this column on every ingest since that date. It was added to the LIVE corebrain
  -- by hand at the time and never captured here or in a migration, so it was invisible on every
  -- existing Core (which already had it) and FATAL on a fresh install (embed.py's INSERT names
  -- the column; schema.sql never created it, so the very first ingest exits 1 before anything is
  -- ever written). Measured 2026-08-31 on a scratch DB built from this file + migrations/ only.
  -- Folded in here (this file already claims to be "the complete current end-state", same
  -- convention as the bi-temporal/RLS/edge-embedding columns above) AND captured as
  -- migrations/2026-08-31-ingest-log-content-hash.sql, because schema.sql alone would still
  -- leave any Core that was NOT hand-ALTERed stuck on the old shape, and the migration alone
  -- would leave this file lying about what a fresh CREATE TABLE actually produces.
  content_hash     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_log_source ON ingest_log (source_file);
CREATE INDEX IF NOT EXISTS idx_ingest_log_org ON ingest_log (org_id);

-- tenants: registry of all Cores sharing this DB. Inserted by the multi-core migration.
CREATE TABLE IF NOT EXISTS tenants (
  org_id     BIGINT PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  vault_path TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Row-Level Security: "read all, write own" ──────────────────────────────
-- Folded in from migrations/2026-05-19-rls-enforcement.sql so a fresh schema.sql
-- is the complete current end-state (the migration stays as the upgrade path for
-- pre-existing DBs). Idempotent: ENABLE/FORCE are no-ops if already set; policies
-- are DROP IF EXISTS + CREATE so re-running schema.sql never errors.
--   SELECT: any org_id (cross-Core recall). INSERT/UPDATE/DELETE: own org only,
--   gated on session var app.current_org_id (set by _env.connect_corebrain()).
-- The ingestion path (embed.py) connects as brain_admin (BYPASSRLS) to write
-- rows tagged with each source file's org_id; recall connects as brain_app.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['entities','evidence','entity_edges','ingest_log'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS read_all ON %I', t);
    EXECUTE format('DROP POLICY IF EXISTS insert_own ON %I', t);
    EXECUTE format('DROP POLICY IF EXISTS update_own ON %I', t);
    EXECUTE format('DROP POLICY IF EXISTS delete_own ON %I', t);
    EXECUTE format('CREATE POLICY read_all ON %I FOR SELECT USING (true)', t);
    EXECUTE format('CREATE POLICY insert_own ON %I FOR INSERT WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
    EXECUTE format('CREATE POLICY update_own ON %I FOR UPDATE USING (org_id = current_setting(''app.current_org_id'', true)::bigint) WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
    EXECUTE format('CREATE POLICY delete_own ON %I FOR DELETE USING (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
  END LOOP;
END $$;
