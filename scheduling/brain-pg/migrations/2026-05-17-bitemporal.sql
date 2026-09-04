-- Migration: bi-temporal fields on entities + evidence (D1 fold from spec-corpus-keeper-2026-05-16.md:128).
-- Date: 2026-05-17
-- Applied to corebrain DB this session as part of brain-primitives Step 1.5.
--
-- Adds Zep Graphiti pattern:
--   valid_from    — when this row became true in the world (defaults to now()).
--   valid_until   — when it stopped being true (NULL = currently valid).
--   superseded_by — FK to the row that replaced this one (NULL = not superseded).

ALTER TABLE entities
  ADD COLUMN IF NOT EXISTS valid_from    TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS valid_until   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS superseded_by BIGINT REFERENCES entities(id) ON DELETE SET NULL;

ALTER TABLE evidence
  ADD COLUMN IF NOT EXISTS valid_from    TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS valid_until   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS superseded_by BIGINT REFERENCES evidence(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_entities_validity ON entities (valid_until) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_evidence_validity ON evidence (valid_until) WHERE valid_until IS NULL;
