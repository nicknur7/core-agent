#!/usr/bin/env python3
"""corroborate.py — Federated brain Phase 2: cross-Core corroboration layer (layer 3).

Builds the shared/team graph: for each entity, find the SAME concept in OTHER orgs
and wire a `same_as` edge with provenance. This is what makes a query about "Core"
surface BOTH life's and business's Core — the cross-Core moat.

Clean version (plan §4 — sophistication deferred to real-tenant scaling): match on
exact (lowercased) name across orgs, gated by embedding cosine >= THRESHOLD to reject
homonyms (same name, different concept). No LLM, no distillation.

Design guards:
- Idempotent — ON CONFLICT DO NOTHING (re-runnable every close).
- Direction canonicalized low_id -> high_id (M4) so two Cores never write
  opposite-direction duplicate tuples.
- Edges tagged is_cross_org=true (M10) so the Phase-3 scope filter keeps the
  cross-Core breadcrumb instead of dropping it under one org's view.
- Runs against EXISTING embeddings only (no Voyage calls) — cheap.
- Name-join + cosine filter (NOT pure embedding-NN), so the HNSW filtered-ANN
  concern (M9) does not apply here.

Usage:  corroborate.py [--dry-run]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import connect_corebrain_admin

COSINE_THRESHOLD = float(os.environ.get("CORROBORATE_THRESHOLD", "0.85"))


# Concept kinds worth corroborating across Cores — the shared *vocabulary* (who/what
# multiple Cores know). Deliberately EXCLUDES the reasoning kinds
# (Decision/Lesson/Rule/Incident): identical reasoning text across orgs is the same
# subagent log extracted into multiple partitions (a contamination artifact), not two
# Cores independently holding a shared concept. Source hubs are excluded too.
CONCEPT_KINDS = ("Entity", "Topic", "Project", "Tool")


def find_pairs(cur):
    """Cross-org same-name, same-kind CONCEPT pairs above the cosine threshold,
    canonicalized a.id<b.id (M4)."""
    cur.execute("""
        SELECT a.id, b.id, a.org_id, b.org_id, a.name,
               1 - (a.embedding <=> b.embedding) AS cosine
        FROM entities a
        JOIN entities b
          ON lower(a.name) = lower(b.name)
         AND a.kind = b.kind
         AND a.id < b.id
         AND a.org_id <> b.org_id
         -- NEVER bridge two copies of ONE file (2026-08-31). Before the hub repartition, every Core
         -- ingested the same flat vault hub, so this join found `sentinel.md` in org 1 and
         -- `sentinel.md` in org 5, scored them ~1.0 cosine (identical text) and minted a same_as.
         -- Measured: 36,506 of 38,020 same_as edges linked rows with an IDENTICAL source_file —
         -- 96% of the "cross-Core bridge" was the graph discovering that a file is itself, and it
         -- inflated every connectivity metric that reads this edge type.
         AND (a.source_file IS DISTINCT FROM b.source_file)
        WHERE a.kind = ANY(%s)
          AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND a.valid_until IS NULL AND b.valid_until IS NULL
          AND (1 - (a.embedding <=> b.embedding)) >= %s
        ORDER BY cosine DESC
    """, (list(CONCEPT_KINDS), COSINE_THRESHOLD))
    return cur.fetchall()


def main():
    dry = "--dry-run" in sys.argv
    conn = connect_corebrain_admin()  # BYPASSRLS: cross-org reads/writes, rows tagged explicitly
    try:
        cur = conn.cursor()
        pairs = find_pairs(cur)
        print(f"[corroborate] {len(pairs)} cross-org same_as candidates "
              f"(exact name + cosine >= {COSINE_THRESHOLD})")
        if dry:
            for a, b, ao, bo, name, cos in pairs[:15]:
                print(f"  {name!r}: org{ao} #{a} <-> org{bo} #{b}  cosine={cos:.3f}")
            return
        import psycopg2.extras
        rows = [(a, b, "same_as", round(float(cos), 4), "INFERRED", ao, True)
                for (a, b, ao, bo, name, cos) in pairs]
        if rows:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO entity_edges
                    (from_entity_id, to_entity_id, edge_type, confidence, confidence_label, org_id, is_cross_org)
                VALUES %s
                ON CONFLICT (from_entity_id, to_entity_id, edge_type) DO NOTHING
            """, rows)
            conn.commit()
        print(f"[corroborate] wired {len(rows)} same_as edges (idempotent, is_cross_org=true)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
