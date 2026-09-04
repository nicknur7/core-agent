-- 2026-07-28 — give artifact evidence a durable home: a column on si_artifacts.
--
-- WHY NOT WHERE IT WAS (the spec)
-- ------------------------------
-- Example texts were written into spec['tests'] AFTER _validate_spec had already run, so the
-- persisted spec carried keys the validator rejects and the artifact became un-reinstallable
-- through the normal path. (core-business finding 8.)
--
-- WHY NOT WHERE I PUT IT NEXT (a local JSON file)
-- ----------------------------------------------
-- Moving it out of the spec was right; moving it out of the DATABASE was not. Those are two
-- different things and I conflated them. Only the first was required. (core-business finding 9.)
--
-- The file destination caused three problems, all landing on the same symptom — no evidence,
-- so no narrowing, so quarantine, which is exactly the state five commits were spent escaping:
--
--   9a  .claude/state/**/evidence.json is gitignored and invisible to pg_dump, so the evidence
--       was backed up by NOTHING. In the spec it was org-partitioned, RLS-isolated and captured
--       by every backup.
--   9b  the write is fail-open, so install() could report success having written no evidence,
--       silently.
--   9c  the read turned any parse failure into d = {}, then atomically wrote one artifact over
--       the top — so a single corrupt read destroyed every OTHER artifact's evidence, cleanly
--       and completely, leaving a healthy-looking file behind.
--
-- A jsonb column on si_artifacts is outside the spec but inside the database: it inherits the
-- table's existing RLS and org partitioning, it is captured by pg_dump like everything else, and
-- there is no second file to corrupt. si_artifacts already carries non-spec columns for exactly
-- this reason (prior_spec, revision, provenance), so this follows the pattern rather than adding
-- a mechanism.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE si_artifacts ADD COLUMN IF NOT EXISTS evidence jsonb;

COMMENT ON COLUMN si_artifacts.evidence IS
  'Example texts and tuning bookkeeping for this artifact. Deliberately OUTSIDE spec: spec has a '
  'closed key set enforced by friction_installer._validate_spec, and bookkeeping in a validated '
  'object is what made tuned artifacts un-reinstallable (finding 8). Deliberately INSIDE Postgres: '
  'a local JSON file was gitignored, unbacked-up, and lost every artifact on one corrupt read '
  '(finding 9). Shape: {positive_texts:[{text,channel}], negative_texts:[...], narrow_count:int, '
  'narrow_history:[str]}. Channel is load-bearing — see friction_watchdog._ctx_for (finding 7a).';
