-- 2026-07-28 — backup tables become admin-readable, not app-readable.
--
-- Completes 2026-07-28-backup-tables-readonly.sql, which revoked WRITES. core-business finding 10
-- is the read half.
--
-- THE ARGUMENT
-- ------------
-- corebrain deliberately leaves cross-org READS open — that is the peer-Core feature, and for the
-- LIVE tables it is correct. For pre-migration backups it is a weaker justification wearing the
-- same permission. The peer feature is about sharing current context; a snapshot of what another
-- Core deleted is a different thing.
--
-- Measured by core-business: 280 rows exist in entities_bak_pre_origin that are absent from live
-- entities, all org 1, consistent with a machinery purge. They checked kinds only, not content
-- (Decision 100, Rule 97, Incident 74, Lesson 9) and reported it explicitly as NOT a live exposure.
-- It isn't. The mechanism is the finding:
--
--   · RLS disabled on both tables (relrowsecurity = f)
--   · SELECT granted to brain_app, the role every Core connects as
--   · no purge path touches these tables — a DELETE against entities does not propagate
--
-- So "delete" currently has no defined relationship to these tables. Today's 280 rows are system
-- meta and harmless. The risk is the next purge that is a REDACTION rather than a tidy-up: it would
-- clear the live table, leave the backup readable by every Core, and nothing would say so.
--
-- THE FIX RESOLVES AN AMBIGUITY RATHER THAN PATCHING A LEAK
-- --------------------------------------------------------
-- These tables are currently neither a backup nor not-a-backup: retained indefinitely, unscoped for
-- read, and outside every purge. This picks one — they ARE a backup — and makes the permissions say
-- so. A backup is for restoring FROM, not for querying. Reading one becomes an admin act.
--
-- Verified before applying: nothing in bin/, scheduling/ or .claude/ references either table by
-- name, so no code path loses a read it depends on.
--
-- Reversible, non-destructive, no rows touched. Dropping the tables would also resolve the
-- ambiguity and is deliberately NOT done here — that is Nick's call, and a pre-migration backup is
-- the thing you regret deleting.

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['entities_bak_pre_origin', 'entity_edges_bak_pre_phase1']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relname = t AND c.relkind = 'r') THEN
            EXECUTE format('REVOKE SELECT ON public.%I FROM brain_app', t);
            -- brain_admin is the restore role; make the intent explicit rather than implicit.
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brain_admin') THEN
                EXECUTE format('GRANT SELECT ON public.%I TO brain_admin', t);
            END IF;
            RAISE NOTICE 'backup table %: SELECT revoked from brain_app, granted to brain_admin', t;
        ELSE
            RAISE NOTICE 'backup table %: absent, nothing to do', t;
        END IF;
    END LOOP;
END $$;
