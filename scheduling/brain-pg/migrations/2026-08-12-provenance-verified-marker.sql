-- T007 — record WHICH rows had their provenance re-checked, not only which were rejected.
--
-- WHY. Commit 4f207c7 (2026-08-11) replaced a blocklist with turn_provenance.is_human_turn(); its
-- message is "41.6% of what the system mined as Nick's words was not Nick." Every row mined BEFORE
-- that commit was classified by the broken filter.
--
-- T007 offered two exits: backfill the pre-fix corpus, or restrict the backtest window. Measured on
-- life before choosing, because the choice turns on a number nobody had:
--
--     pre-fix rows                537
--       transcript on disk        189   (35.2%)  re-checkable
--       transcript deleted        348   (64.8%)  UNVERIFIABLE, PERMANENTLY
--     distinct transcripts         93, of which 21 survive
--
-- A full backfill is not hard, it is IMPOSSIBLE. Two thirds of the evidence is gone.
--
-- AND THE SURVIVORS ARE NOT A RANDOM SAMPLE, which is the part that decides the design. Median
-- session date of the verifiable rows is 2026-07-24; of the unverifiable rows, 2026-06-03. Seven
-- weeks apart. So the 0-3% non-human rate measured on what survived CANNOT be projected onto the
-- 334 that did not — and the older population is precisely the one the broken filter had longest to
-- damage. Reporting "we re-checked and it is 3%" would be a confident number about the wrong
-- population.
--
-- Hence a MARKER rather than a verdict. NULL means UNVERIFIED, which is NOT the same as bad. Any
-- instrument that needs trustworthy provenance filters on
--
--     provenance_verified_at IS NOT NULL  OR  created_at >= '2026-08-11'
--
-- rather than assuming the corpus is uniform. That is the T004 backtest's defensible window, and it
-- is now a queryable set instead of a scan that has to be re-run and hope the same files still exist.
--
-- The first version of the tool excluded the bad rows and recorded nothing about the good ones,
-- which silently converts "checked and clean" into "indistinguishable from never checked" — the
-- distinction the whole verification suite exists to keep.
--
-- Additive and reversible: DROP COLUMN restores the prior state exactly. Each Core runs its own
-- bin/reverify-provenance.py, because the fix is a WRITE and writes are RLS-scoped.

ALTER TABLE pattern_observations
  ADD COLUMN IF NOT EXISTS provenance_verified_at timestamptz;

COMMENT ON COLUMN pattern_observations.provenance_verified_at IS
  'When this row''s source turn was re-checked against turn_provenance.turn_kind() and found HUMAN. '
  'NULL means UNVERIFIED, not bad: on life 334 rows are unverifiable because their transcripts were '
  'deleted, and their median session date is ~7 weeks OLDER than the verifiable ones, so the verified '
  'sample cannot be extrapolated onto them. A backtest needing trustworthy provenance must filter on '
  'this being NOT NULL (or created_at >= 2026-08-11). Added 2026-08-12 for T007.';
