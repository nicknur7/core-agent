-- 2026-07-18b — Assertion model (step ② of the unified redesign: recall stops serving stale).
-- Plan: tasks/research/memory-brain-si-unified-redesign-2026-07-18.md PART 7.2.2 (Codex DDL).
--
-- WHAT: assertion-level, bitemporal, directional supersession — the fix for "recall serves a
-- reversed decision as current" (the 2026-07-10 guard case). An assertion is one atomic claim with
-- applicability (scope), review_status (candidate/accepted/rejected) and lifecycle_status
-- (active/superseded/withdrawn). Reversals are directional relations between ASSERTIONS, not whole
-- entity hubs — and candidate-by-default (never silently hide a disputed fact).
--
-- SAFETY: PURELY ADDITIVE (2 new tables + 1 trigger fn). Touches NO existing table. Legacy
-- entities/evidence stay the live recall authority until step ② wires query.py onto assertions.
-- Reversible = drop the 2 tables + fn (reverse SQL at bottom). RLS 'read all, write own' (fleet model).
--
-- DB-ENFORCED TENANCY (Codex, mandatory): an accepted supersedes/refines/conflicts_with relation
-- requires BOTH endpoints to share org_id — a decision in one Core can NEVER auto-terminate a
-- similarly-named fact in another Core. Cross-org links may only be 'corroborates' (suggest, never mutate).

BEGIN;

CREATE TABLE IF NOT EXISTS assertions (
    id                   BIGSERIAL PRIMARY KEY,
    org_id               BIGINT NOT NULL,
    subject_key          TEXT NOT NULL,
    predicate            TEXT NOT NULL,
    object_json          JSONB NOT NULL,
    qualifiers           JSONB NOT NULL DEFAULT '{}',
    applicability        JSONB NOT NULL DEFAULT '{}',   -- e.g. {"target_org_ids":[1]} or {"core_roles":["peer"],"exclude_org_ids":[1]}
    canonical_hash       TEXT NOT NULL,
    effective_from       TIMESTAMPTZ,
    effective_until      TIMESTAMPTZ,
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_revision_id   BIGINT NOT NULL REFERENCES source_revisions(id),
    review_status        TEXT NOT NULL CHECK (review_status IN ('candidate','accepted','rejected')),
    lifecycle_status     TEXT NOT NULL CHECK (lifecycle_status IN ('active','superseded','withdrawn')),
    confidence           REAL CHECK (confidence BETWEEN 0 AND 1)
);
CREATE INDEX IF NOT EXISTS assertions_lookup_idx          ON assertions (org_id, subject_key, predicate);
CREATE INDEX IF NOT EXISTS assertions_active_idx          ON assertions (org_id, review_status, lifecycle_status);
CREATE INDEX IF NOT EXISTS assertions_source_revision_idx ON assertions (source_revision_id);
CREATE INDEX IF NOT EXISTS assertions_canonical_idx       ON assertions (org_id, canonical_hash);

CREATE TABLE IF NOT EXISTS assertion_relations (
    id                   BIGSERIAL PRIMARY KEY,
    org_id               BIGINT NOT NULL,
    from_assertion_id    BIGINT NOT NULL REFERENCES assertions(id),
    to_assertion_id      BIGINT NOT NULL REFERENCES assertions(id),
    relation             TEXT NOT NULL CHECK (relation IN ('supersedes','refines','conflicts_with','corroborates')),
    reviewer_status      TEXT NOT NULL CHECK (reviewer_status IN ('candidate','accepted','rejected')),
    source_revision_id   BIGINT REFERENCES source_revisions(id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (from_assertion_id, to_assertion_id, relation)
);
CREATE INDEX IF NOT EXISTS assertion_relations_from_idx ON assertion_relations (from_assertion_id);
CREATE INDEX IF NOT EXISTS assertion_relations_to_idx   ON assertion_relations (to_assertion_id);

-- Same-org guard for lifecycle-mutating relations (constraint trigger — ordinary CHECK can't read
-- referenced rows). An ACCEPTED supersedes/refines/conflicts_with must have both endpoints in the
-- same org, and that org must equal the relation's org_id. Cross-org 'corroborates' is always allowed.
CREATE OR REPLACE FUNCTION assertion_relation_same_org() RETURNS trigger AS $$
DECLARE from_org BIGINT; to_org BIGINT;
BEGIN
  IF NEW.relation = 'corroborates' OR NEW.reviewer_status <> 'accepted' THEN
    RETURN NEW;  -- suggestions + corroboration may cross orgs; only accepted mutating links are gated
  END IF;
  SELECT org_id INTO from_org FROM assertions WHERE id = NEW.from_assertion_id;
  SELECT org_id INTO to_org   FROM assertions WHERE id = NEW.to_assertion_id;
  IF from_org IS NULL OR to_org IS NULL OR from_org <> to_org OR from_org <> NEW.org_id THEN
    RAISE EXCEPTION 'accepted % relation must be same-org (from=%, to=%, rel_org=%)',
      NEW.relation, from_org, to_org, NEW.org_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS assertion_relation_same_org_trg ON assertion_relations;
CREATE CONSTRAINT TRIGGER assertion_relation_same_org_trg
  AFTER INSERT OR UPDATE ON assertion_relations
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assertion_relation_same_org();

-- RLS: read all, write own (fleet model).
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['assertions','assertion_relations'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY read_all   ON %I FOR SELECT USING (true)', t);
    EXECUTE format('CREATE POLICY insert_own ON %I FOR INSERT WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
    EXECUTE format('CREATE POLICY update_own ON %I FOR UPDATE USING (org_id = current_setting(''app.current_org_id'', true)::bigint) WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
    EXECUTE format('CREATE POLICY delete_own ON %I FOR DELETE USING (org_id = current_setting(''app.current_org_id'', true)::bigint)', t);
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON assertions, assertion_relations TO brain_app;
GRANT USAGE, SELECT ON assertions_id_seq, assertion_relations_id_seq TO brain_app;

COMMIT;

-- ── REVERSE ──────────────────────────────────────────────────────────────────
-- BEGIN;
-- DROP TABLE IF EXISTS assertion_relations;
-- DROP TABLE IF EXISTS assertions;
-- DROP FUNCTION IF EXISTS assertion_relation_same_org();
-- DELETE FROM schema_migrations WHERE filename = '2026-07-18b-assertions.sql';
-- COMMIT;
