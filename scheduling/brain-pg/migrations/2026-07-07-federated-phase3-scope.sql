-- Federated brain Phase 3 (2026-07-07): scope (shared/private) — the multi-tenant framework.
--
-- Adds a DEDICATED `scope` column (M3 — NOT ownership_tag reuse: its CHECK excludes
-- 'private' and its semantics are work/personal-style/shared, a different axis) to
-- entities + evidence. NOT NULL DEFAULT 'shared' (M2 — avoids the NULL-drops-from-recall
-- bug cleanly; no COALESCE needed) => behavior IDENTICAL to today for Nick's co-located
-- Cores (everything shared = mutual awareness preserved).
--
-- Marking a row 'private' hides its CONTENT cross-Core (enforced by the Phase-3
-- _visibility_clause in query.py + MCP). The breadcrumb — the row's existence and its
-- same_as edges — still resolves (knows-that-it-knows, no content leak).
BEGIN;

ALTER TABLE entities ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'shared'
  CHECK (scope = ANY (ARRAY['shared','private']));
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'shared'
  CHECK (scope = ANY (ARRAY['shared','private']));

CREATE INDEX IF NOT EXISTS idx_entities_scope ON entities (scope) WHERE scope = 'private';
CREATE INDEX IF NOT EXISTS idx_evidence_scope ON evidence (scope) WHERE scope = 'private';

COMMIT;

-- DOWN (manual):
--   ALTER TABLE entities DROP COLUMN scope;
--   ALTER TABLE evidence DROP COLUMN scope;
