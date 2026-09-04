-- Claude SI Loop — Phase 1 schema
-- Spec: ~/.claude/plans/wild-juggling-hummingbird.md (Phase 1)
-- Digest: tasks/specs/claude-si-plan-digest-2026-05-21.md
-- Research-calibrated 2026-05-21: regex-label-as-identity dedup, threshold=5,
-- entities-table reuse for promoted artifacts via bi-temporal cols.
--
-- This file is RE-RUNNABLE. All CREATEs use IF NOT EXISTS. Phase 1 adds two
-- new tables; promoted patterns flow into the existing entities table
-- (kind='Rule' or 'Lesson') for retirement via valid_until.
--
-- Phase 2 (hook tier) and Phase 3 (artifact_event/outcome/utility with role-
-- separated write paths) will add further migrations.

-- ============================================================================
-- pattern_observations — cheap-append capture
-- ============================================================================
-- One row per detected correction/hallucination event. The pattern_label is
-- the dedup key (regex-label-as-identity, lifted from claude-reflect). Same
-- label across multiple sessions = same theme.
--
-- Org-partitioned: source_file determines org_id via path_to_org_id()
-- mirroring evidence/entities pattern.

CREATE TABLE IF NOT EXISTS pattern_observations (
    id              BIGSERIAL PRIMARY KEY,
    pattern_label   TEXT NOT NULL,            -- e.g. "solved-wrong-problem", "premature-execution"
    theme_hint      TEXT,                     -- short human-readable description, may differ across events
    evidence_excerpt TEXT NOT NULL,           -- ~300 chars of the prior assistant turn being corrected
    correction_text TEXT NOT NULL,            -- Nick's typed correction (first 300 chars)
    session_date    DATE NOT NULL,
    source_file     TEXT NOT NULL,            -- JSONL path the event came from
    source_uuid     TEXT,                     -- message uuid for back-reference
    detector_version TEXT NOT NULL DEFAULT 'v1',
    org_id          BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pattern_obs_label ON pattern_observations (pattern_label);
CREATE INDEX IF NOT EXISTS idx_pattern_obs_org_date ON pattern_observations (org_id, session_date DESC);
CREATE INDEX IF NOT EXISTS idx_pattern_obs_label_org_date ON pattern_observations (pattern_label, org_id, session_date DESC);

-- Idempotency: nightly detector re-scans last 14d, so (source_file, source_uuid, pattern_label)
-- must be unique to prevent double-insert on overlapping windows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pattern_obs_dedup
    ON pattern_observations (source_file, COALESCE(source_uuid, ''), pattern_label);

COMMENT ON TABLE pattern_observations IS
'Append-only log of detected correction/hallucination events. dedup by pattern_label (regex-label-as-identity, cheaper than content hash). Promoted to entities table on graduation. Phase 1 of claude-si loop.';

-- ============================================================================
-- pattern_promotions — record of patterns that graduated to artifacts
-- ============================================================================
-- One row per approved promotion. Links the pattern_label to the artifact
-- written (rule edit / new slash command). Prevents re-proposing the same
-- pattern. References entities.id when the artifact is itself an entity row.

CREATE TABLE IF NOT EXISTS pattern_promotions (
    id                  BIGSERIAL PRIMARY KEY,
    pattern_label       TEXT NOT NULL,
    promoted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    target_kind         TEXT NOT NULL,        -- 'rule_edit' | 'new_slash_command' | 'skill' (P2) | 'hook' (P2)
    target_path         TEXT NOT NULL,        -- the file path written/edited
    target_diff         TEXT NOT NULL,        -- the diff or full content applied
    approver            TEXT NOT NULL DEFAULT 'nick',
    ledger_entry_id     TEXT,                 -- anchor in memory/claude-si-ledger.md (YYYY-MM-DD-<slug>)
    entity_id           BIGINT,               -- if promoted to entities table, that row's id
    org_id              BIGINT NOT NULL,
    CONSTRAINT pattern_promotions_target_kind_chk CHECK (
        target_kind IN (
            'rule_edit','new_slash_command','skill','hook',
            -- Phase 2d Tier-A widening (2026-05-21):
            'subagent_brief_edit','verification_trigger_tune','permission_allowlist_add'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_pattern_prom_label ON pattern_promotions (pattern_label);
CREATE INDEX IF NOT EXISTS idx_pattern_prom_label_org ON pattern_promotions (pattern_label, org_id);

COMMENT ON TABLE pattern_promotions IS
'One row per approved pattern promotion. pattern_label joins to pattern_observations. Prevents re-proposing same pattern. entity_id links to entities row when the artifact became a first-class entity.';

-- ============================================================================
-- detector_runs — DEAD. Written 2026-05-21..2026-06-05, never once read.
-- ============================================================================
-- WHAT IT WAS FOR: tracking the most-recent window the detector had scanned,
-- so nightly runs would resume instead of re-scanning.
--
-- THAT NEVER HAPPENED. Audited 2026-08-13 by cross-referencing every table
-- against the codebase for a reader. detector_runs is the only table on this
-- seat with rows and no reader anywhere:
--
--   the only writer  scheduling/archive/claude-si-behavior-loop/detect-patterns.py
--                    (INSERT at :713, UPDATE finished_at at :726)
--   readers          NONE, in any file, ever
--   rows             14, org 1 only, 2026-05-21 -> 2026-06-05
--
-- The resume it exists for was never implemented even while the writer was
-- live: detect-patterns.py:608 computes its window as
-- `now - timedelta(days=args.since)` — a CLI flag with a default — and does
-- not consult window_end. So the bookkeeping was write-only from the first
-- commit, not something that decayed.
--
-- The writer was archived 2026-06-05 ("Unfreeze + ship the learned-workflow
-- interpretation layer"), so the table has had neither producer nor consumer
-- since. NOT DROPPED: a DROP is a migration against the corebrain all five
-- Cores share, which is a blast-radius action, and 14 rows cost nothing to
-- keep. Surfaced for that decision instead.
--
-- THE LIVE DATABASE STILL CARRIES THE OLD COMMENT, and will until migrations
-- run. Verified at the time of writing: obj_description('detector_runs') on
-- the live corebrain still reads "Lets the script resume from last window_end
-- and surfaces per-run summary stats." Applying the corrected text means a
-- write to the shared DB, so it is deliberately batched with the drop decision
-- rather than done here for a comment — but that leaves this FILE and the
-- DATABASE disagreeing in the meantime, and a reader who queries the DB gets
-- the false version. Stating it beats creating a second silent drift while
-- fixing the first.

CREATE TABLE IF NOT EXISTS detector_runs (
    id                  BIGSERIAL PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    window_start        DATE NOT NULL,
    window_end          DATE NOT NULL,
    events_observed     INTEGER NOT NULL DEFAULT 0,
    distinct_labels     INTEGER NOT NULL DEFAULT 0,
    detector_version    TEXT NOT NULL DEFAULT 'v1',
    org_id              BIGINT NOT NULL,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_detector_runs_org_started ON detector_runs (org_id, started_at DESC);

COMMENT ON TABLE detector_runs IS
'DEAD as of 2026-08-13 audit: 14 rows, no reader in any file, ever. Its only writer (detect-patterns.py) was archived 2026-06-05. This comment previously claimed it "lets the script resume from last window_end" - that resume was never implemented; the writer always took its window from the --since CLI flag. Retained pending a decision on dropping it.';

-- ============================================================================
-- View: ready_for_review
-- ============================================================================
-- Patterns above threshold (default 5) that have NOT been promoted yet.
-- Used by /claude-si and the session-start Check (n).
--
-- Threshold is a parameter; default 5 calibrated from 14-day empirical scan
-- (2026-05-21 calibration report).

CREATE OR REPLACE VIEW ready_for_review AS
SELECT
    o.pattern_label,
    o.org_id,
    COUNT(*) AS observed_count,
    MIN(o.session_date) AS first_seen,
    MAX(o.session_date) AS last_seen,
    -- Rank weight per plan §3.2: count weighted by sqrt(age in days)
    COUNT(*) * SQRT(EXTRACT(DAY FROM NOW() - MAX(o.session_date)::timestamp) + 1) AS rank_weight
FROM pattern_observations o
WHERE NOT EXISTS (
    SELECT 1 FROM pattern_promotions p
    WHERE p.pattern_label = o.pattern_label
      AND p.org_id = o.org_id
)
GROUP BY o.pattern_label, o.org_id
HAVING COUNT(*) >= 5;  -- threshold; override at query time

COMMENT ON VIEW ready_for_review IS
'Patterns at/above threshold (default 5) with no row in pattern_promotions. /claude-si queries this. Rank weight = count * sqrt(days since last seen + 1).';

-- ============================================================================
-- Permissions — Phase 1 only
-- ============================================================================
-- brain_app reads + writes pattern_observations (detector runs as caller)
-- brain_app reads pattern_promotions / detector_runs
-- brain_admin can do anything (BYPASSRLS)
--
-- Phase 3 will add separate roles for artifact_event vs artifact_outcome
-- writes to enforce Goodhart-separation. Phase 1 keeps it simple.

GRANT SELECT, INSERT ON pattern_observations TO brain_app;
GRANT USAGE, SELECT ON SEQUENCE pattern_observations_id_seq TO brain_app;

GRANT SELECT, INSERT ON pattern_promotions TO brain_app;
GRANT USAGE, SELECT ON SEQUENCE pattern_promotions_id_seq TO brain_app;

GRANT SELECT, INSERT, UPDATE ON detector_runs TO brain_app;
GRANT USAGE, SELECT ON SEQUENCE detector_runs_id_seq TO brain_app;

GRANT SELECT ON ready_for_review TO brain_app;

-- ============================================================================
-- RLS — same pattern as entities/evidence
-- ============================================================================
-- Read all (cross-org), write own (caller's org_id).

ALTER TABLE pattern_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE pattern_observations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pattern_observations_select ON pattern_observations;
CREATE POLICY pattern_observations_select ON pattern_observations FOR SELECT USING (true);

DROP POLICY IF EXISTS pattern_observations_insert ON pattern_observations;
CREATE POLICY pattern_observations_insert ON pattern_observations FOR INSERT
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

ALTER TABLE pattern_promotions ENABLE ROW LEVEL SECURITY;
ALTER TABLE pattern_promotions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pattern_promotions_select ON pattern_promotions;
CREATE POLICY pattern_promotions_select ON pattern_promotions FOR SELECT USING (true);

DROP POLICY IF EXISTS pattern_promotions_insert ON pattern_promotions;
CREATE POLICY pattern_promotions_insert ON pattern_promotions FOR INSERT
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

ALTER TABLE detector_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE detector_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS detector_runs_select ON detector_runs;
CREATE POLICY detector_runs_select ON detector_runs FOR SELECT USING (true);

DROP POLICY IF EXISTS detector_runs_insert ON detector_runs;
CREATE POLICY detector_runs_insert ON detector_runs FOR INSERT
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS detector_runs_update ON detector_runs;
CREATE POLICY detector_runs_update ON detector_runs FOR UPDATE
    USING (org_id = current_setting('app.current_org_id', true)::bigint)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);
