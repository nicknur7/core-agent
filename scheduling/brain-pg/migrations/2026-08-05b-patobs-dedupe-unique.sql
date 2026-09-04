-- Make duplicate correction observations structurally impossible.
--
-- FOUND BY: Codex adversarial review of the un-gating commit (093a285), 2026-08-05, pre-push.
-- Its claim was that learned-corpus-miner.detect() dedupes in PROCESS MEMORY — it SELECTs the
-- existing (source_uuid, pattern_label) pairs once at the top and then INSERTs — with no DB
-- uniqueness behind it, and that the session close does not share run-brain-update's lock, so two
-- concurrent runs can both pass the same check and both insert.
--
-- IT IS NOT THEORETICAL. Measured before writing this: 135 duplicate
-- (source_uuid, pattern_label, org_id) groups, 275 rows, ALL on org 1. That matters more than a
-- tidiness complaint, because those rows are the NUMERATOR of bin/correction-rate.py — the metric
-- the entire fleet spent tonight auditing. A double-mined week reads as a worse week. The 2026-08-05
-- fix already de-duplicates by (session_date, correction_text) at read time, which is why the
-- numbers reported tonight are not affected; this closes the hole at the source instead.
--
-- Un-gating the miner (093a285) raised the exposure: the close and the heavy nightly run can now
-- both mine on a Core that previously ran neither. Fixing the concurrency window without fixing
-- the missing constraint would have been the same half-application this session has now produced
-- twice (repo() vs get_org_id, then trend vs streak).
--
-- SAFETY. Verified before deleting anything: within every duplicate group, correction_text and
-- canonical_ask are IDENTICAL (0 groups disagree) and no duplicate row carries an embedding. The
-- extra rows are exact redundant copies, so keeping the lowest id loses no information. The
-- deleted rows are copied to pattern_observations_dupe_backup_20260805 first regardless — a
-- delete that cannot be inspected afterwards is not a repair.

BEGIN;

-- 1 ------------------------------------------------------------------ preserve before deleting
CREATE TABLE IF NOT EXISTS pattern_observations_dupe_backup_20260805 AS
SELECT * FROM pattern_observations WHERE false;

-- A backup must not be writable by the role every Core connects as. `CREATE TABLE AS` inherits the
-- schema's default grants, which hand brain_app INSERT/UPDATE/DELETE and attach no RLS — so without
-- this, a multi-org backup is rewritable and deletable by ANY Core. That is exactly the exposure
-- 2026-07-28-backup-tables-readonly.sql closed for two earlier backup tables, on the reasoning that
-- "a backup you can rewrite is not a backup"; this migration reintroduced it eight days later and
-- bin/tests/test_org_isolation.py failed the same day. Revoked at creation so the next backup starts
-- correct rather than being hand-patched after a test failure. 2026-08-05d does the same by naming
-- convention for tables that already exist.
--
-- Must run as the table OWNER (plain `psql -d corebrain`), not `-U brain_admin`: a non-owner REVOKE
-- warns per column, changes nothing, and still exits 0.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON pattern_observations_dupe_backup_20260805 FROM brain_app;

INSERT INTO pattern_observations_dupe_backup_20260805
SELECT p.*
FROM pattern_observations p
WHERE p.source_uuid IS NOT NULL
  AND p.id NOT IN (
        SELECT min(id) FROM pattern_observations
        WHERE source_uuid IS NOT NULL
        GROUP BY source_uuid, pattern_label, org_id
  )
  AND NOT EXISTS (
        SELECT 1 FROM pattern_observations_dupe_backup_20260805 b WHERE b.id = p.id
  );

-- 2 ------------------------------------------------------------------------------- dedupe
DELETE FROM pattern_observations p
WHERE p.source_uuid IS NOT NULL
  AND p.id NOT IN (
        SELECT min(id) FROM pattern_observations
        WHERE source_uuid IS NOT NULL
        GROUP BY source_uuid, pattern_label, org_id
  );

-- 3 --------------------------------------------------------------------- make it impossible
-- Partial index: rows with a NULL source_uuid are brain-vault-sourced observations that have no
-- JSONL identity to dedupe on, and there are legitimately many of them. Constraining only the
-- rows that HAVE an identity is the whole point.
CREATE UNIQUE INDEX IF NOT EXISTS uq_patobs_source_label_org
    ON pattern_observations (source_uuid, pattern_label, org_id)
    WHERE source_uuid IS NOT NULL;

COMMIT;

-- NOTE FOR THE MINER: with this index live, detect()'s INSERT should carry
-- `ON CONFLICT (source_uuid, pattern_label, org_id) WHERE source_uuid IS NOT NULL DO NOTHING`
-- so a concurrent run degrades to a no-op instead of raising and failing the close. That change
-- ships in the same commit as this migration; if you are reading this because an INSERT raised a
-- unique violation, the two have drifted apart.
