-- 2026-07-23-si-artifacts.sql — the UNIFIED canonical artifact store (Workstream 1: one spine).
--
-- Postgres is authoritative; .claude/state/friction-artifacts/active.json becomes a DISPOSABLE
-- projection rebuilt from this table (Codex 2026-07-23: never treat active.json as both truth and
-- cache — that split-brain is the #1 risk). Both friction-generated and legacy learned_contracts
-- artifacts live here, project to one runtime file, and inject through one dispatcher.
--
-- Org-isolated + forced RLS, matching friction_cases / learned_contracts.

CREATE TABLE IF NOT EXISTS si_artifacts (
    artifact_id       TEXT NOT NULL,                    -- 'art_...' (friction) | 'legacy_<id>' | 'seed_...'
    org_id            INT  NOT NULL CHECK (org_id > 0), -- fail-closed: never an org-0 row (Codex WS1 review)
    PRIMARY KEY (org_id, artifact_id),                  -- composite: identical legacy ids across orgs must not collide
    provenance        TEXT NOT NULL DEFAULT 'friction', -- 'friction' | 'legacy' | 'seed'
    event             TEXT NOT NULL DEFAULT 'UserPromptSubmit',
    spec              JSONB NOT NULL,                   -- the full artifact spec the dispatcher reads
    active            BOOLEAN NOT NULL DEFAULT true,
    quarantined       BOOLEAN NOT NULL DEFAULT false,
    quarantine_reason TEXT,
    prior_spec        JSONB,                            -- reversibility: prior spec before last upsert
    revision          INT NOT NULL DEFAULT 1,           -- bumps on every canonical change (projection checksum)
    installed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS si_artifacts_org_live
    ON si_artifacts (org_id) WHERE active AND NOT quarantined;

-- forced RLS, org-partitioned (same pattern as friction_cases)
ALTER TABLE si_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE si_artifacts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS si_artifacts_org_isolation ON si_artifacts;
CREATE POLICY si_artifacts_org_isolation ON si_artifacts
    USING (org_id = current_setting('app.current_org_id', true)::int)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::int);

-- a single monotonically-advancing revision marker per org, for the projection checksum invariant
CREATE TABLE IF NOT EXISTS si_projection_state (
    org_id           INT PRIMARY KEY,
    canonical_rev    BIGINT NOT NULL DEFAULT 0,   -- bumped on every canonical write
    projected_rev    BIGINT NOT NULL DEFAULT 0,   -- last rev successfully projected to active.json
    projected_at     TIMESTAMPTZ
);
ALTER TABLE si_projection_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE si_projection_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS si_projection_state_org_isolation ON si_projection_state;
CREATE POLICY si_projection_state_org_isolation ON si_projection_state
    USING (org_id = current_setting('app.current_org_id', true)::int)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::int);
