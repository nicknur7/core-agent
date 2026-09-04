-- Claude SI Loop — Phase 2 schema
-- Parent spec: tasks/specs/claude-si-phase-2-spec-2026-05-21.md
-- Handoff:     tasks/handoff-phase-2-autonomous-build-2026-05-21.md
--
-- Adds three telemetry tables + a central artifact registry + two Goodhart-
-- separated Postgres roles. All add-only relative to Phase 1's schema.sql.
-- Idempotent (CREATE IF NOT EXISTS / DROP POLICY IF EXISTS).
--
-- Roll-back:
--   DROP TABLE artifact_utility, artifact_outcome, artifact_event, artifacts CASCADE;
--   DROP ROLE fire_writer, outcome_writer;
--
-- Goodhart-separation property: no single role can write both an event and an
-- outcome. fire_writer can INSERT into artifact_event only; outcome_writer can
-- INSERT/UPDATE artifact_outcome only. brain_app keeps SELECT everywhere for
-- read-only diagnostic joins.

-- ============================================================================
-- artifacts — central registry of all measurable Core artifacts
-- ============================================================================
-- One row per artifact (hook, rule, slash command, skill, sentinel agent, etc.).
-- Pre-existing artifacts (e.g. the 4 manual hooks) get bootstrapped below so
-- Phase 2b can backfill artifact_event against stable IDs.
--
-- `kind` is broader than pattern_promotions.target_kind because it includes
-- pre-existing artifacts that were never promoted from a pattern (e.g. the
-- hooks Nick hand-built before the claude-si loop existed).

CREATE TABLE IF NOT EXISTS artifacts (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL,            -- 'hook' | 'rule' | 'slash_command' | 'skill' | 'subagent_brief' | 'verification_trigger' | 'permission_entry'
    label           TEXT NOT NULL,            -- stable identifier (e.g. 'state-claim-gate', 'say-do-gap')
    path            TEXT,                     -- repo-relative file path when applicable
    promotion_id    BIGINT,                   -- pattern_promotions.id if promoted from a pattern; NULL for pre-existing
    org_id          BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at      TIMESTAMPTZ,
    notes           TEXT,
    CONSTRAINT artifacts_kind_chk CHECK (
        kind IN ('hook','rule','slash_command','skill','subagent_brief',
                 'verification_trigger','permission_entry')
    ),
    CONSTRAINT artifacts_label_org_uniq UNIQUE (label, org_id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_org_kind ON artifacts (org_id, kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_label ON artifacts (label);

COMMENT ON TABLE artifacts IS
'Central registry of all measurable Core artifacts. (label, org_id) is the stable identity. Bootstrapped with pre-existing manual hooks in Phase 2a; new artifacts added by apply-promotion.py downstream.';

-- ============================================================================
-- artifact_event — append-only fire/override/noop/error log
-- ============================================================================
-- One row per artifact event. kind enumerated; ruleID is the specific regex
-- or sub-pattern that fired (so a single hook with multiple patterns can be
-- audited at sub-pattern granularity).
--
-- Dedup key: (artifact_id, timestamp, ruleID, excerpt_hash). Allows idempotent
-- backfill from /tmp/core-hook-events.log.
--
-- IMPORTANT: only fire_writer (or brain_admin) may INSERT. brain_app gets
-- SELECT for read-only diagnostic joins.

CREATE TABLE IF NOT EXISTS artifact_event (
    id              BIGSERIAL PRIMARY KEY,
    artifact_id     BIGINT NOT NULL REFERENCES artifacts(id),
    version         TEXT,                     -- artifact version at fire time (git sha or hand-stamp)
    session_id      TEXT,                     -- Claude session UUID when known
    timestamp       TIMESTAMPTZ NOT NULL,
    kind            TEXT NOT NULL,            -- 'fire' | 'override' | 'noop' | 'error'
    rule_id         TEXT,                     -- specific sub-pattern within the artifact
    reason          TEXT,                     -- excerpt, error message, etc.
    excerpt_hash    TEXT,                     -- sha256(reason) prefix for dedup
    org_id          BIGINT NOT NULL,
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT artifact_event_kind_chk CHECK (
        kind IN ('fire','override','noop','error')
    )
);

CREATE INDEX IF NOT EXISTS idx_artifact_event_artifact_ts
    ON artifact_event (artifact_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_artifact_event_org_kind_ts
    ON artifact_event (org_id, kind, timestamp DESC);

-- Dedup: same artifact + same timestamp + same rule + same excerpt_hash is the
-- same event. Allows idempotent re-runs of the backfill script.
CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_event_dedup
    ON artifact_event (artifact_id, timestamp, COALESCE(rule_id, ''), COALESCE(excerpt_hash, ''));

COMMENT ON TABLE artifact_event IS
'Append-only event log for every artifact fire/override/noop/error. Only fire_writer + brain_admin may INSERT (Goodhart-separation). Phase 2 self-improvement telemetry.';

-- ============================================================================
-- artifact_outcome — weekly outcome summary
-- ============================================================================
-- Weekly snapshot of whether the artifact's underlying pattern recurred after
-- promotion. recurrence_decision ∈ {keep, tighten, loosen, retire}.
--
-- IMPORTANT: only outcome_writer (or brain_admin) may INSERT/UPDATE. NOT
-- writable from fire_writer — that's the Goodhart guard.

CREATE TABLE IF NOT EXISTS artifact_outcome (
    id                      BIGSERIAL PRIMARY KEY,
    artifact_id             BIGINT NOT NULL REFERENCES artifacts(id),
    week_starting           DATE NOT NULL,
    target_pattern_hash     TEXT,             -- hash of the pattern this artifact targets
    observed_count          INTEGER NOT NULL DEFAULT 0,
    post_promotion_count    INTEGER NOT NULL DEFAULT 0,
    recurrence_decision     TEXT,             -- 'keep' | 'tighten' | 'loosen' | 'retire' | NULL (pending)
    rationale               TEXT,
    org_id                  BIGINT NOT NULL,
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT artifact_outcome_decision_chk CHECK (
        recurrence_decision IS NULL OR
        recurrence_decision IN ('keep','tighten','loosen','retire')
    ),
    CONSTRAINT artifact_outcome_artifact_week_uniq UNIQUE (artifact_id, week_starting)
);

CREATE INDEX IF NOT EXISTS idx_artifact_outcome_artifact_week
    ON artifact_outcome (artifact_id, week_starting DESC);
CREATE INDEX IF NOT EXISTS idx_artifact_outcome_org_decision
    ON artifact_outcome (org_id, recurrence_decision);

COMMENT ON TABLE artifact_outcome IS
'Weekly outcome summary per artifact. Written ONLY by outcome_writer or brain_admin — separate from artifact_event to prevent a single code path optimizing both signal and verdict (Goodhart guard).';

-- ============================================================================
-- artifact_utility — ACT-R-style utility scoring (refreshed table, not view)
-- ============================================================================
-- Populated by a refresher job (Phase 3) that scans artifact_event + outcome
-- and writes one row per (artifact, week). Stored as a table rather than a
-- live view so we can pin a value at compute time and explain back later.

CREATE TABLE IF NOT EXISTS artifact_utility (
    id                  BIGSERIAL PRIMARY KEY,
    artifact_id         BIGINT NOT NULL REFERENCES artifacts(id),
    week_starting       DATE NOT NULL,
    utility_score       DOUBLE PRECISION,
    fire_count          INTEGER NOT NULL DEFAULT 0,
    override_count      INTEGER NOT NULL DEFAULT 0,
    noop_count          INTEGER NOT NULL DEFAULT 0,
    error_count         INTEGER NOT NULL DEFAULT 0,
    last_useful_at      TIMESTAMPTZ,
    org_id              BIGINT NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT artifact_utility_artifact_week_uniq UNIQUE (artifact_id, week_starting)
);

CREATE INDEX IF NOT EXISTS idx_artifact_utility_artifact_week
    ON artifact_utility (artifact_id, week_starting DESC);
CREATE INDEX IF NOT EXISTS idx_artifact_utility_score
    ON artifact_utility (org_id, utility_score DESC NULLS LAST);

COMMENT ON TABLE artifact_utility IS
'ACT-R-style utility scoring per (artifact, week). Refreshed by Phase 3 job from artifact_event + artifact_outcome. Empty in Phase 2; the refresher arrives later.';

-- ============================================================================
-- Bootstrap pre-existing manual hooks
-- ============================================================================
-- These rows give Phase 2b's backfill stable artifact_ids to reference. Kept
-- idempotent via the (label, org_id) UNIQUE constraint.

INSERT INTO artifacts (kind, label, path, org_id, notes)
VALUES
    ('hook', 'state-claim-gate',  '.claude/hooks/state-claim-gate.py',
        1, 'Blocks state-claim language without same-turn read tool call.'),
    ('hook', 'say-do-gap',        '.claude/hooks/say-do-gap.py',
        1, 'Blocks "I''ll save / saved" without same-turn Write/Edit.'),
    ('hook', 'time-claim-gate',   '.claude/hooks/time-claim-gate.py',
        1, 'Blocks duration/clock claims without same-turn date or session-start read.'),
    ('hook', 'pretooluse-guard',  '.claude/hooks/pretooluse-guard.sh',
        1, 'Sentinel router for outward-facing actions (email, SMS, push, curl).')
ON CONFLICT (label, org_id) DO NOTHING;

-- rot-check.py does NOT write to /tmp/core-hook-events.log (no FIRES collected
-- there). It still registers as an artifact for future Phase 3 telemetry.
INSERT INTO artifacts (kind, label, path, org_id, notes)
VALUES ('hook', 'rot-check', '.claude/hooks/rot-check.py', 1,
        'UserPromptSubmit ASI computation. No /tmp log writes; Phase 3 will wire telemetry.')
ON CONFLICT (label, org_id) DO NOTHING;

-- ============================================================================
-- Roles — Goodhart-separated write paths
-- ============================================================================
-- fire_writer:    INSERT on artifact_event only.   NO grant on artifact_outcome.
-- outcome_writer: INSERT/UPDATE on artifact_outcome. NO grant on artifact_event.
--
-- brain_app keeps SELECT on all three so read-only diagnostic queries (joining
-- events and outcomes) remain ergonomic.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fire_writer') THEN
        CREATE ROLE fire_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'outcome_writer') THEN
        CREATE ROLE outcome_writer NOLOGIN;
    END IF;
END $$;

-- Revoke any wide-default grants from PUBLIC just to be safe.
REVOKE ALL ON artifact_event   FROM PUBLIC;
REVOKE ALL ON artifact_outcome FROM PUBLIC;
REVOKE ALL ON artifact_utility FROM PUBLIC;

-- fire_writer can only INSERT into artifact_event. SELECT is allowed so
-- INSERT ... RETURNING clauses work; this is read-only on existing rows.
GRANT SELECT, INSERT ON artifact_event TO fire_writer;
GRANT USAGE, SELECT ON SEQUENCE artifact_event_id_seq TO fire_writer;
GRANT SELECT ON artifacts TO fire_writer;
-- NOTE: no GRANT on artifact_outcome to fire_writer. That's the Goodhart guard.

-- outcome_writer can INSERT/UPDATE artifact_outcome only.
GRANT SELECT, INSERT, UPDATE ON artifact_outcome TO outcome_writer;
GRANT USAGE, SELECT ON SEQUENCE artifact_outcome_id_seq TO outcome_writer;
GRANT SELECT ON artifacts TO outcome_writer;
GRANT SELECT ON artifact_event TO outcome_writer;   -- read-only join access
-- NOTE: no INSERT on artifact_event to outcome_writer.

-- brain_app gets full SELECT + INSERT on artifacts (registry maintenance) and
-- SELECT on everything else (read-only diagnostic joins). Outcome refresher /
-- backfill scripts use fire_writer / outcome_writer specifically.
GRANT SELECT, INSERT, UPDATE ON artifacts TO brain_app;
GRANT USAGE, SELECT ON SEQUENCE artifacts_id_seq TO brain_app;
GRANT SELECT ON artifact_event   TO brain_app;
GRANT SELECT ON artifact_outcome TO brain_app;
GRANT SELECT, INSERT, UPDATE ON artifact_utility TO brain_app;
GRANT USAGE, SELECT ON SEQUENCE artifact_utility_id_seq TO brain_app;

-- ============================================================================
-- RLS — same shape as Phase 1 (read all, write own)
-- ============================================================================
-- Defense-in-depth on top of role grants. The PRIMARY guard is the GRANT
-- separation above; RLS prevents cross-org writes within a permitted role.

ALTER TABLE artifacts          ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts          FORCE ROW LEVEL SECURITY;
ALTER TABLE artifact_event     ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_event     FORCE ROW LEVEL SECURITY;
ALTER TABLE artifact_outcome   ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_outcome   FORCE ROW LEVEL SECURITY;
ALTER TABLE artifact_utility   ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_utility   FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS artifacts_select ON artifacts;
CREATE POLICY artifacts_select ON artifacts FOR SELECT USING (true);
DROP POLICY IF EXISTS artifacts_insert ON artifacts;
CREATE POLICY artifacts_insert ON artifacts FOR INSERT
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
DROP POLICY IF EXISTS artifacts_update ON artifacts;
CREATE POLICY artifacts_update ON artifacts FOR UPDATE
    USING (org_id = current_setting('app.current_org_id', true)::bigint)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS artifact_event_select ON artifact_event;
CREATE POLICY artifact_event_select ON artifact_event FOR SELECT USING (true);
DROP POLICY IF EXISTS artifact_event_insert ON artifact_event;
CREATE POLICY artifact_event_insert ON artifact_event FOR INSERT
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS artifact_outcome_select ON artifact_outcome;
CREATE POLICY artifact_outcome_select ON artifact_outcome FOR SELECT USING (true);
DROP POLICY IF EXISTS artifact_outcome_insert ON artifact_outcome;
CREATE POLICY artifact_outcome_insert ON artifact_outcome FOR INSERT
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
DROP POLICY IF EXISTS artifact_outcome_update ON artifact_outcome;
CREATE POLICY artifact_outcome_update ON artifact_outcome FOR UPDATE
    USING (org_id = current_setting('app.current_org_id', true)::bigint)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS artifact_utility_select ON artifact_utility;
CREATE POLICY artifact_utility_select ON artifact_utility FOR SELECT USING (true);
DROP POLICY IF EXISTS artifact_utility_insert ON artifact_utility;
CREATE POLICY artifact_utility_insert ON artifact_utility FOR INSERT
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
DROP POLICY IF EXISTS artifact_utility_update ON artifact_utility;
CREATE POLICY artifact_utility_update ON artifact_utility FOR UPDATE
    USING (org_id = current_setting('app.current_org_id', true)::bigint)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
