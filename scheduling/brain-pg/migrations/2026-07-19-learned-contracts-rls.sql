-- 2026-07-19-learned-contracts-rls.sql
-- learned_contracts was the ONE org-partitioned table missing write-isolation RLS.
-- pattern_observations (and the other tenant tables) enable+force RLS with an
-- org-scoped INSERT/UPDATE WITH CHECK; learned_contracts had RLS DISABLED, so
-- brain_app (the app role, no rolbypassrls) could INSERT/UPDATE/DELETE ANY Core's
-- contracts. Two concrete latent bugs this closes:
--   (1) the seed's `DELETE FROM learned_contracts WHERE situation=%s` has no org
--       filter -> life re-seeding would WIPE every peer Core's contract for that
--       situation (cross-org clobber). A FOR DELETE policy scopes it to self.
--   (2) org_id DEFAULT was the literal `1`, so a brain_app INSERT that omits org_id
--       lands in LIFE's org regardless of which Core wrote it. Default now derives
--       from the session GUC, so an omitted org_id tags the WRITING Core.
--
-- Safe because: brain_admin has rolbypassrls=TRUE (cross-org system writes still
-- work), the owner/superuser bypasses (DDL + maintenance), and reads stay cross-org
-- (SELECT USING(true) — peer-Core recall by design, same as pattern_observations).
-- Mirrors pattern_observations' policy shape + adds the DELETE policy the seed needs.

ALTER TABLE learned_contracts
  ALTER COLUMN org_id SET DEFAULT current_setting('app.current_org_id', true)::bigint;

ALTER TABLE learned_contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE learned_contracts FORCE ROW LEVEL SECURITY;

-- idempotent: DROP IF EXISTS before each CREATE so a re-apply against the already-
-- migrated shared corebrain is safe (CREATE POLICY has no IF NOT EXISTS).
DROP POLICY IF EXISTS learned_contracts_select ON learned_contracts;
CREATE POLICY learned_contracts_select ON learned_contracts
  FOR SELECT USING (true);

DROP POLICY IF EXISTS learned_contracts_insert ON learned_contracts;
CREATE POLICY learned_contracts_insert ON learned_contracts
  FOR INSERT WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS learned_contracts_update ON learned_contracts;
CREATE POLICY learned_contracts_update ON learned_contracts
  FOR UPDATE USING (org_id = current_setting('app.current_org_id', true)::bigint)
             WITH CHECK (org_id = current_setting('app.current_org_id', true)::bigint);

DROP POLICY IF EXISTS learned_contracts_delete ON learned_contracts;
CREATE POLICY learned_contracts_delete ON learned_contracts
  FOR DELETE USING (org_id = current_setting('app.current_org_id', true)::bigint);
