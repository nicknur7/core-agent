-- 2026-07-27b — canonical_ask_steps: let the extraction step record the ARITY of an ask.
--
-- Why: nothing currently distinguishes "one rule applied at a moment" from "an ordered sequence with
-- a preflight and a fallback". The shape label (canonical_ask_type) says constraint vs procedure, but
-- it is a judgement with no evidence attached, so a one-line reminder and a five-step procedure look
-- identical to the router.
--
-- The discriminator is arity, and it is checkable. Compare two real asks from this corpus:
--   "verify state against the live source before claiming"  -> one constraint, no steps  -> contract
--   "keep the architecture diagram in sync with the system" -> check manifest, regenerate,
--      compare to rundown, verify the Atlas, never hand-edit AUTO blocks -> 5 steps -> hooked_skill
--
-- Recording the steps makes the type label falsifiable rather than asserted: an ask claimed to be a
-- procedure that decomposes into fewer than 2 steps is mislabelled, and the router can say so.
--
-- Storage note: jsonb array of short strings. It carries the LLM's decomposition, not authored
-- content — the payload a hooked_skill actually injects is still written separately through
-- friction_installer.write_procedure(), where it is redacted, size-capped and hash-pinned. Nothing
-- here becomes durable instruction.

ALTER TABLE pattern_observations
  ADD COLUMN IF NOT EXISTS canonical_ask_steps jsonb;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'pattern_observations_ask_steps_ck'
  ) THEN
    -- array-only, bounded. A malformed or oversized decomposition is rejected at the DB rather than
    -- being carried into the router, matching how canonical_ask_type is CHECK-constrained.
    ALTER TABLE pattern_observations
      ADD CONSTRAINT pattern_observations_ask_steps_ck
      CHECK (
        canonical_ask_steps IS NULL
        OR (jsonb_typeof(canonical_ask_steps) = 'array'
            AND jsonb_array_length(canonical_ask_steps) <= 12)
      );
  END IF;
END $$;
