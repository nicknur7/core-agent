-- 2026-07-18 — Source-revision ledger (the keystone of the memory-brain-SI unified redesign).
-- Plan: tasks/research/memory-brain-si-unified-redesign-2026-07-18.md PART 7.2 (Codex-designed DDL).
--
-- WHAT: one control plane that knows every source (session JSONL, subagent, decision entry,
-- correction, memory file) across all Cores, and which processing stage each revision reached,
-- with leases, retries, tombstones, and version-triggered reprocessing. Replaces the mtime/basename
-- completion logic in ingest_log + extract-pending.sh + build-tracker.py as the completion authority.
--
-- SAFETY: PURELY ADDITIVE. Creates 4 new tables; touches NO existing table. Reversible = drop them
-- (reverse SQL at bottom). Legacy entities/evidence/entity_edges/ingest_log remain the live authority
-- until later phases wire recall onto the ledger. RLS matches the fleet "read all, write own" model
-- (2026-05-19-rls-enforcement.sql). Per-Core data; never baseline-synced.
--
-- 4 tables:
--   sources          — stable identity (source_key) separate from current path (rename-safe).
--   source_revisions — immutable occurrence log per source (upsert/tombstone/rename ops).
--   stage_jobs       — durable claimable queue: one row per (revision, stage, processor_version, input_fingerprint).
--   stage_attempts   — append-only execution receipts (history is never overwritten).

BEGIN;

-- ── sources: stable identity, separate from locator ──────────────────────────
CREATE TABLE IF NOT EXISTS sources (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT NOT NULL,
    source_key   TEXT NOT NULL,            -- native stable id: session/subagent/decision/observation UUID, or generated UUID for files
    source_kind  TEXT NOT NULL,            -- session_jsonl | subagent | decision_entry | memory_file | correction
    current_uri  TEXT,                     -- current locator (path); NULL once tombstoned
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, source_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS sources_current_uri_unique
    ON sources (org_id, current_uri) WHERE current_uri IS NOT NULL;

-- ── source_revisions: immutable occurrence log ───────────────────────────────
CREATE TABLE IF NOT EXISTS source_revisions (
    id                       BIGSERIAL PRIMARY KEY,
    org_id                   BIGINT NOT NULL,
    source_id                BIGINT NOT NULL REFERENCES sources(id),
    revision_seq             BIGINT NOT NULL,          -- monotonic per source; occurrence identity (NOT content_hash)
    operation                TEXT NOT NULL CHECK (operation IN ('upsert','tombstone','rename')),
    source_uri               TEXT NOT NULL,
    previous_uri             TEXT,                      -- set on rename
    content_hash             TEXT,                      -- NULL iff tombstone
    byte_size                BIGINT,
    observed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    supersedes_revision_id   BIGINT REFERENCES source_revisions(id),
    UNIQUE (source_id, revision_seq),
    CHECK ( (operation = 'tombstone' AND content_hash IS NULL)
         OR (operation <> 'tombstone' AND content_hash IS NOT NULL) )
);
CREATE INDEX IF NOT EXISTS source_revisions_latest_idx     ON source_revisions (source_id, revision_seq DESC);
CREATE INDEX IF NOT EXISTS source_revisions_hash_idx       ON source_revisions (content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS source_revisions_supersedes_idx ON source_revisions (supersedes_revision_id) WHERE supersedes_revision_id IS NOT NULL;

-- ── stage_jobs: durable claimable queue (current state) ──────────────────────
CREATE TABLE IF NOT EXISTS stage_jobs (
    id                   BIGSERIAL PRIMARY KEY,
    org_id               BIGINT NOT NULL,
    source_revision_id   BIGINT NOT NULL REFERENCES source_revisions(id),
    stage                TEXT NOT NULL,                 -- captured | mechanically_indexed | semantically_interpreted | recall_ready
    processor_version    TEXT NOT NULL,                 -- code+prompt+schema+model capability version
    input_fingerprint    TEXT NOT NULL,                 -- hash(content_hash + stage + processor_version + upstream_output_hashes) → version bump = new job
    status               TEXT NOT NULL CHECK (status IN ('pending','leased','retry_wait','done','dead')),
    lease_owner          TEXT,
    lease_token          UUID,
    lease_expires_at     TIMESTAMPTZ,
    attempts             INTEGER NOT NULL DEFAULT 0,
    next_attempt_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error_code      TEXT,
    last_error           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_revision_id, stage, processor_version, input_fingerprint)
);
CREATE INDEX IF NOT EXISTS stage_jobs_claim_idx           ON stage_jobs (next_attempt_at, id) WHERE status IN ('pending','retry_wait');
CREATE INDEX IF NOT EXISTS stage_jobs_lease_recovery_idx  ON stage_jobs (lease_expires_at) WHERE status = 'leased';
CREATE INDEX IF NOT EXISTS stage_jobs_revision_status_idx ON stage_jobs (source_revision_id, stage, status);

-- ── stage_attempts: append-only execution receipts (history) ─────────────────
CREATE TABLE IF NOT EXISTS stage_attempts (
    id               BIGSERIAL PRIMARY KEY,
    org_id           BIGINT NOT NULL,
    stage_job_id     BIGINT NOT NULL REFERENCES stage_jobs(id),
    attempt_no       INTEGER NOT NULL,
    worker_id        TEXT,
    lease_token      UUID,
    started_at       TIMESTAMPTZ NOT NULL,
    completed_at     TIMESTAMPTZ,
    outcome          TEXT CHECK (outcome IN ('done','failed','lease_expired','cancelled')),
    error_code       TEXT,
    error_detail     TEXT,
    output_ref       TEXT,                              -- revision-scoped output (NEVER basename)
    output_hash      TEXT,
    item_total       INTEGER,
    item_done        INTEGER,
    UNIQUE (stage_job_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS stage_attempts_job_idx ON stage_attempts (stage_job_id);

-- ── RLS: "read all, write own" (fleet model, 2026-05-19-rls-enforcement.sql) ──
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['sources','source_revisions','stage_jobs','stage_attempts'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY read_all   ON %I FOR SELECT USING (true)', t);
    EXECUTE format('CREATE POLICY insert_own ON %I FOR INSERT WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
    EXECUTE format('CREATE POLICY update_own ON %I FOR UPDATE USING (org_id = current_setting(''app.current_org_id'', true)::bigint) WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
    EXECUTE format('CREATE POLICY delete_own ON %I FOR DELETE USING (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
  END LOOP;
END $$;

-- ── grants (default privileges from the RLS migration cover future tables, but be explicit) ──
GRANT SELECT, INSERT, UPDATE, DELETE ON sources, source_revisions, stage_jobs, stage_attempts TO brain_app;
GRANT USAGE, SELECT ON
    sources_id_seq, source_revisions_id_seq, stage_jobs_id_seq, stage_attempts_id_seq TO brain_app;

COMMIT;

-- ── REVERSE (rollback) ───────────────────────────────────────────────────────
-- BEGIN;
-- DROP TABLE IF EXISTS stage_attempts;
-- DROP TABLE IF EXISTS stage_jobs;
-- DROP TABLE IF EXISTS source_revisions;
-- DROP TABLE IF EXISTS sources;
-- DELETE FROM schema_migrations WHERE filename = '2026-07-18-source-revision-ledger.sql';
-- COMMIT;
