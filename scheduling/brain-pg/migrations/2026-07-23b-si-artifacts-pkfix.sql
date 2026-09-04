-- 2026-07-23b-si-artifacts-pkfix.sql — idempotent upgrade for any Core that applied the FIRST
-- si-artifacts migration before the composite-PK / positive-org fix (Codex WS1 review). A migration
-- runner records by filename and never re-runs an edited file, and CREATE TABLE IF NOT EXISTS cannot
-- alter an existing table's PK — so the constraint change MUST live in its own migration.
--
-- Safe to run whether the table has the old single-column PK or the new composite one.

DO $$
BEGIN
    -- swap single-column PK -> composite (org_id, artifact_id)
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid = 'si_artifacts'::regclass AND contype = 'p'
                 AND array_length(conkey, 1) = 1) THEN
        EXECUTE 'ALTER TABLE si_artifacts DROP CONSTRAINT ' ||
                (SELECT conname FROM pg_constraint WHERE conrelid = 'si_artifacts'::regclass AND contype = 'p');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'si_artifacts'::regclass AND contype = 'p') THEN
        ALTER TABLE si_artifacts ADD PRIMARY KEY (org_id, artifact_id);
    END IF;
    -- positive-org guard (fail-closed: never an org-0 row)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'si_artifacts'::regclass AND conname = 'si_artifacts_org_positive') THEN
        ALTER TABLE si_artifacts ADD CONSTRAINT si_artifacts_org_positive CHECK (org_id > 0);
    END IF;
END $$;
