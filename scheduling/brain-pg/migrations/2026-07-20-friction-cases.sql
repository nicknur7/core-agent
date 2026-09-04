-- 2026-07-20-friction-cases.sql
-- The capture-layer store for the friction-case SI engine (P1). Each row is a rich
-- friction moment mined from a correction + its transcript. Org-isolated with forced
-- RLS: SELECT is org-scoped (a Core sees ONLY its own captured moments — they may hold
-- sensitive correction text, and there's no cross-org read use); writes scoped to the
-- writing Core. (Differs from pattern_observations, which is intentionally cross-org for
-- peer recall.) Idempotent on
-- (org_id, source_file, source_uuid, detector_version) so re-mining never duplicates.
-- Artifacts (P2/P3) live in a separate table; this is capture only.

CREATE TABLE IF NOT EXISTS friction_cases (
  case_id           text        NOT NULL,
  org_id            bigint      NOT NULL DEFAULT current_setting('app.current_org_id', true)::bigint,
  status            text        NOT NULL DEFAULT 'mined',
  source_file       text        NOT NULL,
  source_uuid       text        NOT NULL,
  detector_version  text        NOT NULL,
  cluster_key       text,
  candidate_event   text,
  eligible          boolean     NOT NULL DEFAULT false,
  case_json         jsonb       NOT NULL,
  content_sha256    text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, case_id),
  UNIQUE (org_id, source_file, source_uuid, detector_version)
);

CREATE INDEX IF NOT EXISTS friction_cases_org_eligible_idx ON friction_cases (org_id, eligible);
CREATE INDEX IF NOT EXISTS friction_cases_cluster_idx ON friction_cases (org_id, cluster_key);

ALTER TABLE friction_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE friction_cases FORCE ROW LEVEL SECURITY;

-- SELECT is ORG-ISOLATED (unlike pattern_observations, which is intentionally cross-org for peer
-- recall). friction_cases holds captured correction MOMENTS (may contain sensitive text) with no
-- cross-org read use case — a Core sees only its own (Codex review #9, 2026-07-22).
DROP POLICY IF EXISTS friction_cases_select ON friction_cases;
CREATE POLICY friction_cases_select ON friction_cases
  FOR SELECT USING (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS friction_cases_insert ON friction_cases;
CREATE POLICY friction_cases_insert ON friction_cases
  FOR INSERT WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS friction_cases_update ON friction_cases;
CREATE POLICY friction_cases_update ON friction_cases
  FOR UPDATE USING (org_id = current_setting('app.current_org_id', true)::bigint)
             WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS friction_cases_delete ON friction_cases;
CREATE POLICY friction_cases_delete ON friction_cases
  FOR DELETE USING (org_id = current_setting('app.current_org_id', true)::bigint);
