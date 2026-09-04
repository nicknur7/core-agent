-- 2026-08-31b — friction_cases funnel columns (observability, not a new mechanism).
--
-- THE DEFECT. friction_cases carries a `status` column since its very first migration
-- (2026-07-20), defaulted 'mined', and friction_miner.build_case sets it to exactly one of
-- 'mined' / 'ineligible' at MINE time (moment.complete ? 'mined' : 'ineligible') — a judgment
-- about the transcript capture, not the routing outcome. friction_loop.run() then routes, gates
-- and installs every 'mined' case entirely in-process (friction_router.route ->
-- friction_test_gate.gate -> friction_installer.install) and never UPDATEs the row again. So the
-- column that looks like a lifecycle tracker is a write-once label from the first stage of a
-- five-stage pipeline. Measured 2026-08-31: 769 rows fleet-wide, 704 'mined' + 65 'ineligible',
-- zero anywhere else — because nowhere else was ever written.
--
-- THE FIX. `status` gets three new terminal values a case can reach AFTER mining —
-- 'duplicate_ask' (same-canonical-ask dedup inside run()'s loop), 'denied' (friction_router.route
-- refused to mint; granular reason in the new `denied_reason` column — see
-- friction_router.py's `case["_drop_reason"]` sites), 'awaiting_ask' (route() refused ONLY for
-- lack of a canonical_ask yet — router's own comment: "not lost — it returns to the pool and
-- becomes mintable as soon as ask_miner canonicalises it", which is a different claim than
-- 'denied' and is kept distinct rather than folded in) — plus 'cap_denied' (per-run
-- MAX_CONTRACTS/MAX_BLOCKERS reached before this case's turn), 'gate_failed'
-- (friction_test_gate.gate said no; reason in `denied_reason`), 'installed' and 'install_failed'
-- (friction_installer.install's own verdict; reason in `denied_reason`). 'mined' and 'ineligible'
-- keep their existing meaning unchanged — this is additive to the vocabulary, not a rename.
--
-- `denied_reason` carries the granular WHY for every non-mined/non-ineligible/non-installed
-- status — a short code from friction_router (no_trigger_terms, not_recurring, profanity, ...) or
-- the gate's/installer's own reason string, truncated. Free text, not closed-vocabulary: the
-- funnel readout groups on it, but a new refusal reason must not require a migration to name.
--
-- `routed_artifact_id` records the artifact_id a case's spec computed to (friction_router
-- derives it deterministically from case_id — see `_artifact_id`) once the case reaches gating or
-- beyond, so a row here can be correlated to si_artifacts / the action log without re-deriving the
-- hash. NULL for any case that never produced a spec.
--
-- IDEMPOTENT / REVERSIBLE per the task's own constraint on this table, on top of the runner's
-- tracker-based idempotency: `ADD COLUMN IF NOT EXISTS` no-ops on re-run, the CHECK is guarded so
-- re-adding it does not error, and every new column is nullable with no backfill — dropping them
-- again loses nothing that wasn't computed by this same code path. The 769 pre-existing rows are
-- deliberately left as 'mined'/'ineligible' with NULL denied_reason: this migration does not know
-- how far they got and does not invent history (see friction_loop.py's backfill comment, which
-- derives a real 'installed' for the subset joinable to si_artifacts by case_id — a code-level
-- backfill, not a SQL default).

ALTER TABLE friction_cases ADD COLUMN IF NOT EXISTS denied_reason text;
ALTER TABLE friction_cases ADD COLUMN IF NOT EXISTS routed_artifact_id text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'friction_cases_status_ck'
  ) THEN
    ALTER TABLE friction_cases
      ADD CONSTRAINT friction_cases_status_ck
      CHECK (status IN ('mined', 'ineligible', 'duplicate_ask', 'awaiting_ask', 'denied',
                         'cap_denied', 'gate_failed', 'installed', 'install_failed'));
  END IF;
END $$;

-- The funnel readout groups by (org_id, status) and, within a status, by denied_reason — this
-- is that query's index. cluster/eligible idx from 2026-07-20 stay for their existing readers.
CREATE INDEX IF NOT EXISTS friction_cases_org_status_idx ON friction_cases (org_id, status);
