-- Federated brain Phase 2 (2026-07-07): cross-Core corroboration layer (layer 3).
-- Adds:
--   1. edge_type 'same_as' — links the SAME concept across orgs (life's "Core"
--      == business's "Core"). The shared/team layer. Down-weighted in query.py
--      (EDGE_TYPE_WEIGHTS) so it bridges Cores without dominating BFS relevance.
--   2. entity_edges.is_cross_org BOOLEAN — marks an edge whose endpoints span two
--      orgs. Exempt from per-org visibility filtering (M10) so a cross-Core
--      breadcrumb survives the Phase-3 scope clause instead of vanishing.
-- Additive (widens allowed edge_types; new column defaults false) + reversible.
BEGIN;

ALTER TABLE entity_edges DROP CONSTRAINT entity_edges_edge_type_check;
ALTER TABLE entity_edges ADD CONSTRAINT entity_edges_edge_type_check
  CHECK (edge_type = ANY (ARRAY[
    'motivated_by','learned_from','supersedes','cross_impacts','references',
    'originates_in','same_as'
  ]));

ALTER TABLE entity_edges ADD COLUMN IF NOT EXISTS is_cross_org BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_edges_cross_org ON entity_edges (is_cross_org) WHERE is_cross_org;

COMMIT;

-- DOWN (manual, if reverting):
--   DELETE FROM entity_edges WHERE edge_type = 'same_as';
--   ALTER TABLE entity_edges DROP COLUMN is_cross_org;
--   ALTER TABLE entity_edges DROP CONSTRAINT entity_edges_edge_type_check;
--   ALTER TABLE entity_edges ADD CONSTRAINT entity_edges_edge_type_check
--     CHECK (edge_type = ANY (ARRAY['motivated_by','learned_from','supersedes','cross_impacts','references','originates_in']));
