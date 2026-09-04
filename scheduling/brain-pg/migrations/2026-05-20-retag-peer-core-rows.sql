-- 2026-05-20: re-tag existing peer-Core rows from org_id=1 to their proper org_id
--
-- Before today, embed.py tagged every row with the CALLING Core's org_id
-- (get_org_id() reads CORE_ORG_ID env var). Life Core's stop-hook is the only
-- one that ran the heavy embed pass, so all rows got org_id=1 even when the
-- source file was under projects/business/ or projects/school/.
--
-- Today's embed.py update derives org_id from the source file path. For new
-- writes this Just Works. For existing rows, we do this one-time correction.
--
-- Scope: evidence + ingest_log only. entities and entity_edges represent
-- conceptual hubs and graph relations that are owned by whichever Core ran
-- the heavy job (life today). We leave those org_id=1 — they're still
-- visible to all Cores via RLS read_all.
--
-- Idempotent: filters on org_id != target.
-- Requires BYPASSRLS or superuser (RLS write-own blocks cross-org UPDATE).

BEGIN;

-- evidence: business
UPDATE evidence
   SET org_id = 2
 WHERE source_file LIKE '%/projects/business/%'
   AND org_id != 2;

-- evidence: school
UPDATE evidence
   SET org_id = 3
 WHERE source_file LIKE '%/projects/school/%'
   AND org_id != 3;

-- ingest_log: business
UPDATE ingest_log
   SET org_id = 2
 WHERE source_file LIKE '%/projects/business/%'
   AND org_id != 2;

-- ingest_log: school
UPDATE ingest_log
   SET org_id = 3
 WHERE source_file LIKE '%/projects/school/%'
   AND org_id != 3;

COMMIT;

-- Verification (run interactively):
--   SELECT org_id, COUNT(*) FROM evidence
--     WHERE source_file LIKE '%/projects/business/%' OR source_file LIKE '%/projects/school/%'
--     GROUP BY org_id;
--   SELECT org_id, COUNT(*) FROM ingest_log
--     WHERE source_file LIKE '%/projects/business/%' OR source_file LIKE '%/projects/school/%'
--     GROUP BY org_id;
--
-- ─── Reverse SQL ─────────────────────────────────────────────────────────
-- (Reverses to all-org_id=1 — recovers prior incorrect state. Not recommended
--  unless rolling back the brain_admin migration entirely.)
-- UPDATE evidence SET org_id = 1
--   WHERE source_file LIKE '%/projects/business/%' OR source_file LIKE '%/projects/school/%';
-- UPDATE ingest_log SET org_id = 1
--   WHERE source_file LIKE '%/projects/business/%' OR source_file LIKE '%/projects/school/%';
