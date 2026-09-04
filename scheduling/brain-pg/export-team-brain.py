#!/usr/bin/env python3
"""export-team-brain.py — build team-brain.json for the Core OS "Team Brain" tab.

Core-ux has no Postgres client (reads JSON files), so this Python job queries the
live corebrain and writes public/brain-graph/team-brain.json — the same pattern as
build-brain-graph.mjs. Produces:
  - per-Core cards: memory count, shared-concept count, top concepts (by degree)
  - the shared/team layer: cross-Core concepts (via same_as), how many Cores hold
    each, and whether any copy is currently private (the scope toggle state)

Federated brain Phase 5 — the "Team of Brains" management surface.
Usage: export-team-brain.py [out_path]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import connect_corebrain_admin

CORES = [(1, "life", "primary · daily driver"),
         (2, "business", "workplace agent"),
         (3, "school", "OSU coursework"),
         (4, "finance", "CFO · trading")]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("CORE_UX", os.path.expanduser("~/AI Projects/core-ux")),
        "public", "brain-graph", "team-brain.json")
    conn = connect_corebrain_admin()
    cur = conn.cursor()

    cores = []
    for org, name, role in CORES:
        cur.execute("SELECT count(*) FROM entities WHERE org_id=%s AND kind<>'Source' AND valid_until IS NULL", (org,))
        memories = cur.fetchone()[0]
        # how many of this Core's concepts are shared (have a same_as edge)
        cur.execute("""
            SELECT count(DISTINCT e.id) FROM entities e
            JOIN entity_edges ee ON (ee.from_entity_id = e.id OR ee.to_entity_id = e.id)
            WHERE e.org_id=%s AND ee.edge_type='same_as' AND e.valid_until IS NULL
        """, (org,))
        shared = cur.fetchone()[0]
        # top concepts by degree (real named entities, not file paths / Source hubs)
        cur.execute("""
            SELECT e.name, count(ee.*) deg FROM entities e
            JOIN entity_edges ee ON (ee.from_entity_id = e.id OR ee.to_entity_id = e.id)
            WHERE e.org_id=%s AND e.kind IN ('Entity','Project') AND e.valid_until IS NULL
              AND e.compiled_truth_md IS NOT NULL AND e.name !~ '[/.]' AND length(e.name) BETWEEN 2 AND 32
            GROUP BY e.id, e.name ORDER BY deg DESC LIMIT 14
        """, (org,))
        seen, top = set(), []
        for (nm, _deg) in cur.fetchall():
            k = nm.lower().rstrip("s")  # fold Core/core and light plural dupes
            if k in seen:
                continue
            seen.add(k)
            top.append(nm)
            if len(top) >= 5:
                break
        cores.append({"org": org, "name": name, "role": role, "memories": memories, "shared": shared, "top": top})

    # shared/team layer: cross-Core concepts via same_as → orgs holding each + scope
    # Clean shared concepts: same NAME held by >=3 Cores, real synthesized hubs
    # (compiled_truth_md), SHORT names (real entities like "Core"/"Anthropic", not
    # sentence-fragment reasoning nodes), no slug/path names.
    cur.execute("""
        SELECT min(name) nm, array_agg(DISTINCT org_id ORDER BY org_id) orgs, bool_or(scope='private') priv
        FROM entities
        WHERE kind IN ('Entity','Project','Topic','Tool') AND compiled_truth_md IS NOT NULL
          AND valid_until IS NULL AND length(name) BETWEEN 3 AND 26 AND name !~ '[/_]'
          AND name !~ '^[A-Z]{2,3}$'  -- drop bare acronyms (JP, MLX) that read as noise
        GROUP BY lower(name) HAVING count(DISTINCT org_id) >= 3
        ORDER BY count(DISTINCT org_id) DESC, length(min(name)) ASC LIMIT 60
    """)
    shared_concepts = [{"name": r[0], "orgs": r[1], "private": r[2]} for r in cur.fetchall()]

    # HONEST total: distinct clean concepts known by 2+ Cores. This is the headline
    # number on the Team card — it reconciles with the per-Core "shared" counts
    # (both count real cross-Core concepts). The `shared` list above is the curated
    # top-60 held by 3+ Cores (the most universal), shown as examples in the drill.
    # (Fixed 2026-07-11 — the card used to show len(shared)=60, a LIMIT-capped ≥3
    #  number, while labeling it "concepts >1 Core knows"; Nick caught the mismatch.)
    cur.execute("""
        SELECT count(*) FROM (
          SELECT lower(name) FROM entities
          WHERE kind IN ('Entity','Project','Topic','Tool') AND compiled_truth_md IS NOT NULL
            AND valid_until IS NULL AND length(name) BETWEEN 3 AND 40 AND name !~ '[/_]'
          GROUP BY lower(name) HAVING count(DISTINCT org_id) >= 2) t
    """)
    shared_total = cur.fetchone()[0]
    conn.close()

    payload = {
        "cores": cores,
        "shared": shared_concepts,
        "sharedCount": len(shared_concepts),
        "sharedTotal": shared_total,
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(payload, f)
    print(f"export-team-brain: {len(cores)} cores · {len(shared_concepts)} shared concepts → {out}")


if __name__ == "__main__":
    main()
