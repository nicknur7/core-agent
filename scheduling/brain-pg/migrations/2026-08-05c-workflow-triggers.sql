-- Store the REAL prompts that started an accepted workflow window.
--
-- WHY THIS IS NEEDED FOR PHASE D3. `friction_installer.install()` gates every artifact against a
-- corpus: the positive test must be a prompt the trigger genuinely fires on, drawn from real
-- traffic, and `ask_miner.route_ask_case` refuses outright when no real member matches ("the ask has
-- no honest positive — refuse it"). That bar exists because a trigger grounded in nothing is how an
-- over-broad rule gets installed and then fires on everything.
--
-- A Workflow captured by consolidate_sessions has real triggering prompts — the Nick asks that
-- opened each accepted window — but Phase C never stored them. Without them, D3 had three options:
-- fabricate a positive from the synthesized trigger text (defeats the gate), match the workflow's
-- terms against the CORRECTION corpus (wrong corpus — a success sequence graded against failure
-- prompts), or skip the gate for workflow artifacts (a second, weaker install path). All three are
-- worse than storing the thing we already had and threw away.
--
-- So: one table, the prompts kept verbatim, and D3 reuses the SAME gate every other artifact passes.

BEGIN;

CREATE TABLE IF NOT EXISTS workflow_triggers (
    id                  bigserial PRIMARY KEY,
    workflow_entity_id  bigint  NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    prompt_text         text    NOT NULL,
    org_id              bigint  NOT NULL,
    source_revision_id  text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT workflow_triggers_prompt_nonempty CHECK (length(btrim(prompt_text)) > 0)
);

-- Uniqueness is per (workflow, prompt, SOURCE REVISION), not per (workflow, prompt).
--
-- Re-consolidating the SAME session must be a no-op — that is the C4 idempotency the pass already
-- has, and the revision_id is content-hashed so an unchanged session yields the same key. But the
-- same prompt appearing in a DIFFERENT session is a genuine second occurrence and must record, because
-- this table is what recurrence is counted from: `workflow_steps` is unique per (entity, step_index),
-- so a recurring workflow cannot add step rows and therefore cannot register a new session there.
-- Keying on prompt alone would have silently dropped any recurrence where Nick opened with the same
-- words twice — which is exactly the case for "continue"-style openings, the most common kind.
--
-- A UNIQUE table CONSTRAINT cannot carry an expression in Postgres — it must be a unique INDEX.
DROP INDEX IF EXISTS uq_workflow_triggers_content;
CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_triggers_occurrence
    ON workflow_triggers (workflow_entity_id, md5(prompt_text), COALESCE(source_revision_id, ''));

CREATE INDEX IF NOT EXISTS idx_workflow_triggers_wf ON workflow_triggers (workflow_entity_id);
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_org ON workflow_triggers (org_id);

-- Same partitioning posture as workflow_steps and the rest of the brain.
ALTER TABLE workflow_triggers ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE tablename = 'workflow_triggers'
                     AND policyname = 'workflow_triggers_org_isolation') THEN
        CREATE POLICY workflow_triggers_org_isolation ON workflow_triggers
            USING (org_id = (current_setting('app.current_org_id', true))::bigint)
            WITH CHECK (org_id = (current_setting('app.current_org_id', true))::bigint);
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brain_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_triggers TO brain_app;
        GRANT USAGE, SELECT ON SEQUENCE workflow_triggers_id_seq TO brain_app;
    END IF;
END $$;

COMMIT;
