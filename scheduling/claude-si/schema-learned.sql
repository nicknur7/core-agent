-- Learned-workflow interpretation layer — corpus schema extension.
-- Spec: tasks/specs/spec-learned-workflow-layer-2026-06-05.md
-- ADDITIVE ONLY (ADD COLUMN IF NOT EXISTS) — reversible via DROP COLUMN.
-- Applied 2026-06-05.
--
-- DEPENDS ON schema.sql (which CREATEs pattern_observations — this file ALTERs it).
-- bin/install-learned-layer.sh applies schema.sql FIRST, so the layer stands up on a
-- fresh fork. Do NOT run this standalone on an empty DB. (2026-06-18 — a fork report.)

CREATE EXTENSION IF NOT EXISTS vector;

-- pattern_observations gains the two legs the 2026-05-28 design assumed but the
-- table never had: the user PROMPT that triggered the bad response (turn N-2),
-- and an embedding of that prompt for prompt-time similarity retrieval.
ALTER TABLE pattern_observations ADD COLUMN IF NOT EXISTS prompt_text TEXT;
ALTER TABLE pattern_observations ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- excluded_at/excluded_reason: the exclusion filter every consumer of this corpus already
-- queries on (`WHERE excluded_reason IS NULL`) in bin/reverify-provenance.py,
-- bin/null-calibration.py, bin/corpus-readiness.py, ask_miner.py and
-- measure-contract-fitness.py — reverify-provenance.py and ask_miner.py also WRITE it
-- (`UPDATE pattern_observations SET excluded_reason=%s, excluded_at=now() ...`). Same defect
-- class and same era as ingest_log.content_hash (found in the same 2026-08-31 sweep): these
-- columns existed on the live corebrain via a hand-run ALTER that was never captured here, so
-- every read-only query above degraded silently (a WHERE clause on a column Postgres treats as
-- always-absent-therefore-always-true is invisible), while the two UPDATE statements would
-- have hard-failed with "column does not exist" on the very first exclusion write a fresh
-- Core ever attempted. 228 of 2110 rows use it live; 0 rows would ever be excludable fresh.
ALTER TABLE pattern_observations ADD COLUMN IF NOT EXISTS excluded_at TIMESTAMPTZ;
ALTER TABLE pattern_observations ADD COLUMN IF NOT EXISTS excluded_reason TEXT;

-- HNSW cosine index for the UserPromptSubmit classifier's retrieval
-- (mirrors scheduling/brain-pg embed/query infra: voyage-3-large, 1024-dim).
CREATE INDEX IF NOT EXISTS idx_patobs_embedding
  ON pattern_observations USING hnsw (embedding vector_cosine_ops);

-- The table was created append-only (SELECT + INSERT policies only), so RLS
-- default-denies UPDATE. The learned layer legitimately updates rows in place
-- (backfill prompt_text/evidence_excerpt, write embeddings), so add an
-- org-scoped UPDATE policy mirroring the existing INSERT policy. Still no DELETE
-- policy — the corpus stays delete-protected.
DROP POLICY IF EXISTS pattern_observations_update ON pattern_observations;
CREATE POLICY pattern_observations_update ON pattern_observations FOR UPDATE
  USING (org_id = (current_setting('app.current_org_id', true))::bigint)
  WITH CHECK (org_id = (current_setting('app.current_org_id', true))::bigint);

-- Phase 2: synthesized typed contracts. One row per recurring "situation".
-- required_shape/forbidden_moves are injected as guidance (judgment-shaped);
-- checkable holds deterministic clauses [{id, rule, ...}] that the Stop validator
-- can BLOCK on. Read at runtime by the classifier hook (brain_app).
CREATE TABLE IF NOT EXISTS learned_contracts (
  id              SERIAL PRIMARY KEY,
  situation       TEXT NOT NULL,
  trigger_labels  TEXT[] NOT NULL,
  required_shape  TEXT[] NOT NULL DEFAULT '{}',
  forbidden_moves TEXT[] NOT NULL DEFAULT '{}',
  checkable       JSONB  NOT NULL DEFAULT '[]',
  example_prompts TEXT[] NOT NULL DEFAULT '{}',
  triggers        TEXT[] NOT NULL DEFAULT '{}',  -- 2026-07-18: data-driven classifier triggers (regex strings) so induced contracts fire without code edits
  org_id          BIGINT NOT NULL DEFAULT 1,
  active          BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON learned_contracts TO brain_app;
GRANT USAGE, SELECT ON SEQUENCE learned_contracts_id_seq TO brain_app;
