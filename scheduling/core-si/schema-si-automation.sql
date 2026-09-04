-- core-si system-fix automation — ADAS-style threshold-gated admission.
-- Spec: tasks/si-loop-completeness-map-2026-06-07.md §6 (delta from external research).
-- ADDITIVE ONLY (CREATE ... IF NOT EXISTS). Reversible via DROP TABLE.
--
-- Idea: a DETERMINISTIC core-si fix earns autonomy by being approved K times
-- running. Until then every fix stays human-gated (surfaced in the core-si table).
-- After K consecutive approvals of the same (signal_key, fix_action), it is admitted
-- to a trusted-fix set; inside that set core-si MAY auto-apply WITHOUT surfacing —
-- but ONLY for life-local, reversible actions (enforced by the apply path, not here).
--
-- Mirrors the org-partitioned RLS pattern of pattern_observations / learned_contracts:
-- every row carries org_id; RLS scopes reads/writes to app.current_org_id.

-- 1. Approval ledger — one row per human approval of a (signal_key, fix_action).
CREATE TABLE IF NOT EXISTS core_si_fix_approvals (
  id           SERIAL PRIMARY KEY,
  signal_key   TEXT        NOT NULL,           -- e.g. 'sys-marker', 'sys-docpath'
  fix_action   TEXT        NOT NULL,           -- canonical action string approved
  kind         TEXT        NOT NULL DEFAULT 'approve'  -- 'approve' | 'reject' (a reject resets the streak)
                 CHECK (kind IN ('approve','reject')),
  approved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  org_id       BIGINT      NOT NULL DEFAULT (current_setting('app.current_org_id', true))::bigint
);

-- 2. Trusted-fix set — a (signal_key, fix_action) that reached K consecutive approvals.
CREATE TABLE IF NOT EXISTS core_si_trusted_fixes (
  id                     SERIAL PRIMARY KEY,
  signal_key             TEXT        NOT NULL,
  fix_action             TEXT        NOT NULL,
  admitted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  approvals_at_admission INT         NOT NULL,
  org_id                 BIGINT      NOT NULL DEFAULT (current_setting('app.current_org_id', true))::bigint,
  active                 BOOLEAN     NOT NULL DEFAULT true,
  UNIQUE (signal_key, fix_action, org_id)
);

-- RLS (mirror the existing per-org pattern) ----------------------------------
ALTER TABLE core_si_fix_approvals  ENABLE ROW LEVEL SECURITY;
ALTER TABLE core_si_trusted_fixes  ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS core_si_fix_approvals_rls ON core_si_fix_approvals;
CREATE POLICY core_si_fix_approvals_rls ON core_si_fix_approvals
  USING (org_id = (current_setting('app.current_org_id', true))::bigint)
  WITH CHECK (org_id = (current_setting('app.current_org_id', true))::bigint);

DROP POLICY IF EXISTS core_si_trusted_fixes_rls ON core_si_trusted_fixes;
CREATE POLICY core_si_trusted_fixes_rls ON core_si_trusted_fixes
  USING (org_id = (current_setting('app.current_org_id', true))::bigint)
  WITH CHECK (org_id = (current_setting('app.current_org_id', true))::bigint);

GRANT SELECT, INSERT, UPDATE, DELETE ON core_si_fix_approvals TO brain_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON core_si_trusted_fixes TO brain_app;
GRANT USAGE, SELECT ON SEQUENCE core_si_fix_approvals_id_seq TO brain_app;
GRANT USAGE, SELECT ON SEQUENCE core_si_trusted_fixes_id_seq TO brain_app;
