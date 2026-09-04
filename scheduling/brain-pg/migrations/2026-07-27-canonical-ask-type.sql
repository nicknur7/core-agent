-- 2026-07-27 — canonical_ask_type: let the ask-extraction LLM emit the artifact SHAPE, not just the text.
--
-- Why: artifact_typer routed "is this ask a capability?" off SKILL_SIGNALS, a 7-needle hardcoded list.
-- Measured 2026-07-27: it matched 0 of 27 real recurring asks at support>=2, while the corpus was full of
-- clearly procedure-shaped demand ("keep the architecture diagram in sync with the actual system", 12x).
-- Needle-matching over LLM-CANONICALIZED text is a category error — canonicalization already destroyed the
-- surface features a needle list would key on.
--
-- Trust boundary: the vocabulary is CLOSED to ('constraint','procedure','none') and enforced by this CHECK.
-- Both live values produce inject-mode artifacts with identical blast radius (a bounded reminder string), so
-- a misroute costs a slightly-wrong-shaped reminder and nothing more — the same trust level already accepted
-- for canonical_ask itself. 'enforcement' is deliberately NOT in this vocabulary and must never be added:
-- block-mode stays reachable only through artifact_typer.ORACLE_CATALOG, which requires code plus a locked
-- equivalence test, never a data-only change.

-- canonical_ask itself was added to the live DB by hand and never captured in any
-- schema file or migration — verified 2026-07-27 by installing from a clean clone,
-- where the index at the bottom of this file failed with
--   ERROR: column "canonical_ask" does not exist
-- because nothing in the repo had ever created it. Declared here so this migration
-- is self-contained and a fresh install matches production. No-op on any DB that
-- already has the column (i.e. every existing Core).
ALTER TABLE pattern_observations
  ADD COLUMN IF NOT EXISTS canonical_ask text;

ALTER TABLE pattern_observations
  ADD COLUMN IF NOT EXISTS canonical_ask_type text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'pattern_observations_canonical_ask_type_ck'
  ) THEN
    ALTER TABLE pattern_observations
      ADD CONSTRAINT pattern_observations_canonical_ask_type_ck
      CHECK (canonical_ask_type IS NULL OR canonical_ask_type IN ('constraint', 'procedure', 'none'));
  END IF;
END $$;

-- Cluster-level lookup: recurring_asks() groups by canonical_ask and takes the majority type.
CREATE INDEX IF NOT EXISTS pattern_observations_ask_type_idx
  ON pattern_observations (org_id, canonical_ask, canonical_ask_type)
  WHERE canonical_ask IS NOT NULL AND canonical_ask <> '';
