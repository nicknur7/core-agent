-- Brain-connectivity fix (2026-07-04) — origin-backbone support.
-- Part of tasks/research/brain-connectivity-2026-07-03.md (Phase 2 + Phase 5).
--
-- Adds:
--   1. edge_type 'originates_in' — the origin backbone (entity -> its source
--      file/session/doc hub). Down-weighted in query.py so it de-isolates
--      without dominating graph-BFS relevance.
--   2. entity kind 'Source' — the origin-hub node kind (a source file made a
--      graph node so entities can point at where they came from). Distinct so
--      hubs are identifiable, weightable, and excludable from vector recall.
--
-- Additive (widens allowed values) — all existing rows stay valid. Reversible
-- (see DOWN below; delete the new-typed rows first, then restore the CHECKs).
BEGIN;

ALTER TABLE entity_edges DROP CONSTRAINT entity_edges_edge_type_check;
ALTER TABLE entity_edges ADD CONSTRAINT entity_edges_edge_type_check
  CHECK (edge_type = ANY (ARRAY[
    'motivated_by','learned_from','supersedes','cross_impacts','references',
    'originates_in'
  ]));

ALTER TABLE entities DROP CONSTRAINT entities_kind_check;
ALTER TABLE entities ADD CONSTRAINT entities_kind_check
  CHECK (kind = ANY (ARRAY[
    'Topic','Tool','Entity','Project','Decision','Lesson','Rule','Incident',
    'Source'
  ]));

COMMIT;

-- DOWN (manual, if reverting):
--   DELETE FROM entity_edges WHERE edge_type = 'originates_in';
--   DELETE FROM entities     WHERE kind = 'Source';
--   ALTER TABLE entity_edges DROP CONSTRAINT entity_edges_edge_type_check;
--   ALTER TABLE entity_edges ADD CONSTRAINT entity_edges_edge_type_check
--     CHECK (edge_type = ANY (ARRAY['motivated_by','learned_from','supersedes','cross_impacts','references']));
--   ALTER TABLE entities DROP CONSTRAINT entities_kind_check;
--   ALTER TABLE entities ADD CONSTRAINT entities_kind_check
--     CHECK (kind = ANY (ARRAY['Topic','Tool','Entity','Project','Decision','Lesson','Rule','Incident']));
