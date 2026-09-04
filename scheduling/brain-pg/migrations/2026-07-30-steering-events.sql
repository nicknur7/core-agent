-- 2026-07-30 — steering-surface governance, Phase 0 (the sensor) + Phase 1 (the judge).
--
-- Implements tasks/research/steering-surface-governance-2026-07-27.md, ratified by Nick on
-- 2026-07-27 with FULL AUTONOMY. Phase 0 says: "At close, core-si-close.py ingests the session's
-- hook-events lines into a new Postgres table `steering_events` (org-scoped, RLS like
-- si_artifacts)." Phase 1 adds the close-time judge writing `steering_judgments` append-only.
--
-- WHY A TABLE AND NOT JUST THE LOG. hook-events.log answers "did this fire". It cannot answer
-- "did firing HELP", "what did it cost", or "how has that changed over 14 days" — those need
-- joins, per-org partitioning and retention. Without this the tuning layer has no evidence source
-- and the gate_tune template has nothing to prove against.
--
-- WHY TWO TABLES. steering_events is the SENSOR (what happened, append-only, cheap, high volume).
-- steering_judgments is the OUTCOME (what it was worth, written by the close-time judge, lower
-- volume, revisable confidence). Keeping them apart is what makes the anti-reward-hacking rule
-- expressible: the judge may write judgments and must never be able to rewrite the telemetry it
-- is judging. Enforced below by GRANT, not by convention.
--
-- Additive and reversible: two new tables, no existing object altered. Reverse block at the foot.

BEGIN;

-- ── SENSOR ───────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS steering_events (
    id               BIGSERIAL PRIMARY KEY,
    org_id           BIGINT      NOT NULL DEFAULT current_setting('app.current_org_id', true)::bigint,
    hook             TEXT        NOT NULL,
    event            TEXT        NOT NULL,          -- PreToolUse | Stop | UserPromptSubmit | ...
    verdict          TEXT        NOT NULL,          -- pass | block | inject | invoke
    detail           TEXT        NOT NULL DEFAULT '',   -- why: 'suppress:current-session' etc.
    tokens_injected  INTEGER     NOT NULL DEFAULT 0,    -- cost side; chars/4 of emitted context
    session_id       TEXT        NOT NULL DEFAULT '',
    excerpt          TEXT        NOT NULL DEFAULT '',
    ts               TIMESTAMPTZ NOT NULL,              -- when the hook fired (from the log line)
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Idempotency: close-time ingest re-reads a log that may overlap a previous run. Without
    -- this a re-run double-counts fire volume, and fire volume is an input to the proof window
    -- that auto-promotes tunes with no human in the loop. Double-counting would let a tune clear
    -- its window without the evidence actually existing.
    dedupe_key       TEXT        NOT NULL,
    CONSTRAINT steering_events_dedupe_uq UNIQUE (org_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS steering_events_hook_idx    ON steering_events (org_id, hook, ts DESC);
CREATE INDEX IF NOT EXISTS steering_events_session_idx ON steering_events (org_id, session_id);
CREATE INDEX IF NOT EXISTS steering_events_ts_idx      ON steering_events (org_id, ts DESC);

-- ── OUTCOME ──────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS steering_judgments (
    id               BIGSERIAL PRIMARY KEY,
    org_id           BIGINT      NOT NULL DEFAULT current_setting('app.current_org_id', true)::bigint,
    steering_event_id BIGINT     NOT NULL REFERENCES steering_events(id) ON DELETE CASCADE,
    -- helped | neutral | false_positive | overridden  (Phase 1 vocabulary)
    outcome          TEXT        NOT NULL,
    confidence       REAL,
    -- 'deterministic' when a pre-signal decided it without the LLM, else the judge model id.
    -- Recorded so the deterministic share is measurable: if the LLM is deciding everything, the
    -- pre-signals are not earning their place and the cost claim in Phase 1 is wrong.
    decided_by       TEXT        NOT NULL DEFAULT 'deterministic',
    evidence         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    judged_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Append-only in spirit; one judgment per event per judge revision.
    judge_revision   INTEGER     NOT NULL DEFAULT 1,
    CONSTRAINT steering_judgments_uq UNIQUE (steering_event_id, judge_revision)
);

CREATE INDEX IF NOT EXISTS steering_judgments_outcome_idx ON steering_judgments (org_id, outcome, judged_at DESC);

-- ── RLS — same shape as si_artifacts / assertions ─────────────────────────────────────────────
-- read_all is deliberate and matches the existing tables: cross-Core READ is permitted (the fleet
-- shares one brain), while writes are confined to the calling Core's own partition.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['steering_events','steering_judgments'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY read_all   ON %I FOR SELECT USING (true)', t);
    EXECUTE format('CREATE POLICY insert_own ON %I FOR INSERT WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
    EXECUTE format('CREATE POLICY update_own ON %I FOR UPDATE USING (org_id = current_setting(''app.current_org_id'', true)::bigint) WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
    EXECUTE format('CREATE POLICY delete_own ON %I FOR DELETE USING (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON steering_events, steering_judgments TO brain_app;
GRANT USAGE, SELECT ON steering_events_id_seq, steering_judgments_id_seq TO brain_app;

-- ── ANTI-REWARD-HACKING, ENFORCED RATHER THAN DOCUMENTED ─────────────────────────────────────
-- Phase 1: "the judge cannot modify telemetry or patterns; outcome rows are append-only."
-- A rule that exists only in prose is the defect class this system spent 2026-07-28/29 removing
-- (an invariant written above the code that violates it). So the judge runs as its own role with
-- INSERT-only on judgments and SELECT-only on events: it structurally cannot rewrite the evidence
-- it is scoring, nor revise a verdict it already gave.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steering_judge') THEN
    CREATE ROLE steering_judge NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO steering_judge;
GRANT SELECT                     ON steering_events    TO steering_judge;
GRANT SELECT, INSERT             ON steering_judgments TO steering_judge;
GRANT USAGE, SELECT ON steering_judgments_id_seq       TO steering_judge;
-- Deliberately NOT granted: UPDATE/DELETE on either table, and any write to
-- pattern_observations / friction_cases (ground truth must stay independent of the judge).

COMMIT;

-- ── REVERSE ───────────────────────────────────────────────────────────────────────────────────
-- BEGIN;
-- DROP TABLE IF EXISTS steering_judgments;
-- DROP TABLE IF EXISTS steering_events;
-- DROP ROLE IF EXISTS steering_judge;
-- COMMIT;

-- ---------------------------------------------------------------------------
-- 2026-07-30 (addendum, from Fable's blast-radius review before the baseline push).
--
-- The steering_judge role is created NOLOGIN, so nothing can CONNECT as it. The judge therefore
-- drops privilege with SET ROLE over its normal brain_app connection — which requires brain_app
-- to be a MEMBER of steering_judge. That grant was issued by hand while fixing Codex's Critical
-- finding and existed nowhere in this file, so a fork or a fresh replay would come up with the
-- judge silently holding UPDATE/DELETE on the very telemetry it scores: the anti-reward-hacking
-- property working on this machine and nowhere else.
--
-- Idempotent; safe to re-run.
GRANT steering_judge TO brain_app;
