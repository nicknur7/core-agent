-- Phase B of tasks/research/learning-substrate-unification-2026-08-03.md — make sequences
-- expressible in the brain.
--
-- WHY THIS EXISTS. Finding F5 of that artifact: an ordered A->B->C->D is not representable in the
-- brain at all. No ordinal column exists in any of the eight brain tables; entities.kind's CHECK
-- forbids a Workflow/Procedure type; assertions.object_json is never an array (756 string, 11
-- object, 0 array for org 1); and query.py returns independently-scored chunks ranked by a scalar,
-- with no code path that returns an ordered path. So "how Nick does X" could only ever be stored
-- as prose about X, which is why the learning loop could learn from corrections (single moments)
-- and never from workflows (sequences).
--
-- ORDERING LIVES IN EXACTLY ONE PLACE: workflow_steps.step_index. The `next_step` edge type added
-- below is for graph traversal only and is NEVER the source of truth for order. Two ordering
-- mechanisms would be precisely the patch-not-consolidate failure Nick has corrected 9 times.
--
-- WHY A TABLE AND NOT object_json ARRAYS: steps need per-step identity so one step can be
-- corrected without invalidating the whole workflow. Assertions are single-fact by design with
-- supersession semantics per fact; a step list is not a fact.
--
-- IDEMPOTENT. The brain schema is shared — every peer Core inherits this on its next pull — so
-- re-running must be a no-op. Every statement below is guarded.

BEGIN;

-- 1 ---------------------------------------------------------------- entities.kind += 'Workflow'
-- Widening a CHECK is additive and harmless if unused, but it must not fail when already widened.
-- The allowed-value list is DERIVED from the constraint already on the table, never hard-coded.
--
-- The first version of this recreated the CHECK from a literal list copied off core-life. Codex
-- flagged it on pre-push review: any peer carrying an additional valid kind would have its
-- existing rows fail the new constraint, and since a CHECK is validated on creation, the whole
-- migration would roll back on that Core — a shared migration that breaks precisely on the Cores
-- that diverged. Deriving the union from `unnest` of the live constraint means this widens
-- whatever is actually there, on whatever Core it runs.
DO $$
DECLARE
    con_name text;
    con_def  text;
    vals     text[];
BEGIN
    SELECT conname, pg_get_constraintdef(oid) INTO con_name, con_def
    FROM pg_constraint
    WHERE conrelid = 'entities'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%kind%=%ANY%';

    IF con_name IS NULL THEN
        RAISE NOTICE 'entities.kind CHECK not found — nothing to widen (fresh or already open schema)';
    ELSIF con_def LIKE '%Workflow%' THEN
        RAISE NOTICE 'entities.kind already permits Workflow — no-op';
    ELSE
        -- Pull the quoted literals out of the existing definition, in order, and append.
        SELECT array_agg(m[1] ORDER BY ord)
          INTO vals
          FROM regexp_matches(con_def, '''([^'']+)''', 'g') WITH ORDINALITY AS t(m, ord);

        IF vals IS NULL OR array_length(vals, 1) IS NULL THEN
            RAISE EXCEPTION 'could not parse existing entities.kind CHECK (%) — refusing to '
                            'replace it with a guess', con_def;
        END IF;

        -- ::text is REQUIRED (2026-08-31). `text[] || 'Workflow'` is ambiguous: Postgres resolves
        -- the untyped literal as a text[] for array||array concatenation and fails with
        -- `malformed array literal: "Workflow"`. The cast selects array||element instead.
        --
        -- This branch only runs where entities.kind has a CHECK that does NOT already list
        -- Workflow — i.e. exactly and only on a FRESH Core built from schema.sql. Every existing
        -- Core took the `already permits Workflow` no-op path, so the migration looked healthy on
        -- all five seats while making the documented install impossible for anyone else. Found by
        -- actually cloning the baseline and running bin/setup-brain.sh, which stopped here and
        -- applied nothing after it.
        vals := vals || 'Workflow'::text;
        EXECUTE format('ALTER TABLE entities DROP CONSTRAINT %I', con_name);
        EXECUTE format('ALTER TABLE entities ADD CONSTRAINT entities_kind_check '
                       'CHECK (kind = ANY (%L))', vals);
        RAISE NOTICE 'entities.kind widened to include Workflow (now: %)', vals;
    END IF;
END $$;

-- 2 ------------------------------------------------- entity_edges.edge_type += 'next_step'
-- Associative traversal only. See the header: order is workflow_steps.step_index, not this.
DO $$
DECLARE
    con_name text;
    con_def  text;
BEGIN
    SELECT conname, pg_get_constraintdef(oid) INTO con_name, con_def
    FROM pg_constraint
    WHERE conrelid = 'entity_edges'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%edge_type = ANY%';

    IF con_name IS NULL THEN
        RAISE NOTICE 'entity_edges.edge_type CHECK not found — nothing to widen';
    ELSIF con_def LIKE '%next_step%' THEN
        RAISE NOTICE 'entity_edges.edge_type already permits next_step — no-op';
    ELSE
        EXECUTE format('ALTER TABLE entity_edges DROP CONSTRAINT %I', con_name);
        ALTER TABLE entity_edges ADD CONSTRAINT entity_edges_edge_type_check
            CHECK (edge_type = ANY (ARRAY['motivated_by','learned_from','supersedes',
                                          'cross_impacts','references','originates_in',
                                          'same_as','next_step']));
        RAISE NOTICE 'entity_edges.edge_type widened to include next_step';
    END IF;
END $$;

-- 3 ------------------------------------------------------------------------ workflow_steps
CREATE TABLE IF NOT EXISTS workflow_steps (
    id                  bigserial PRIMARY KEY,
    workflow_entity_id  bigint  NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    step_index          int     NOT NULL,
    action              text    NOT NULL,
    -- Which tool the step is expected to use, when the consolidator could tell. Advisory: a step
    -- is a description of what Nick wanted done, not a script, and must stay readable when the
    -- tool changes underneath it.
    tool_hint           text,
    org_id              bigint  NOT NULL,
    -- Provenance, so a bad consolidation batch is identifiable and removable in one statement.
    -- Caveat §7 of the plan: success is inferred from absence-of-correction, which is a proxy; a
    -- silently-wrong sequence Nick did not catch would be learned as good. Being able to delete a
    -- batch by provenance is the mitigation.
    source_revision_id  text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT workflow_steps_order_unique UNIQUE (workflow_entity_id, step_index),
    CONSTRAINT workflow_steps_index_positive CHECK (step_index >= 1),
    CONSTRAINT workflow_steps_action_nonempty CHECK (length(btrim(action)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_wf_order
    ON workflow_steps (workflow_entity_id, step_index);
CREATE INDEX IF NOT EXISTS idx_workflow_steps_org
    ON workflow_steps (org_id);
CREATE INDEX IF NOT EXISTS idx_workflow_steps_provenance
    ON workflow_steps (source_revision_id);

-- 4 ------------------------------------------------------------------------------- RLS
-- Same partitioning posture as the rest of the brain: a Core reads its own org only. brain_admin
-- (BYPASSRLS) is what cross-org system processes use, exactly as they do for entities/assertions.
ALTER TABLE workflow_steps ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE tablename = 'workflow_steps' AND policyname = 'workflow_steps_org_isolation') THEN
        CREATE POLICY workflow_steps_org_isolation ON workflow_steps
            USING (org_id = (current_setting('app.current_org_id', true))::bigint)
            WITH CHECK (org_id = (current_setting('app.current_org_id', true))::bigint);
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brain_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_steps TO brain_app;
        GRANT USAGE, SELECT ON SEQUENCE workflow_steps_id_seq TO brain_app;
    END IF;
END $$;

COMMIT;
