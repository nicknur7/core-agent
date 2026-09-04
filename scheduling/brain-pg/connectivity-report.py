#!/usr/bin/env python3
"""Brain graph connectivity report — per-org isolation%, degree, edge-type mix.

Measures the ACTUAL recall graph (Postgres entities + entity_edges), not the
graphify render (graph.json). Written 2026-07-04 as Phase 0 of the brain-
connectivity fix (tasks/research/brain-connectivity-2026-07-03.md) — establishes
the baseline before FIX1/FIX2 and is the core of the Phase-4 nightly health-gate.

Usage:
  python3 connectivity-report.py            # print summary + write state JSON
  python3 connectivity-report.py --quiet     # write state JSON only
  python3 connectivity-report.py --json       # print the JSON blob to stdout

Reads all orgs (brain_admin, BYPASSRLS). No writes to the graph — read-only.
Emits $CORE_INSTANCE/.claude/state/.brain-connectivity.json.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import connect_corebrain_admin  # noqa: E402

INSTANCE = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parents[2]))
STATE_PATH = INSTANCE / ".claude" / "state" / ".brain-connectivity.json"

ORG_NAMES = {1: "life", 2: "business", 3: "school", 4: "finance", 5: "ops"}

# Per-org degree + isolation over LIVE nodes (valid_until IS NULL). Edges are
# undirected for connectivity purposes: a node is "connected" if it appears as
# either endpoint of any edge.
SQL_PER_ORG = """
WITH ec AS (
    SELECT from_entity_id AS id FROM entity_edges
    UNION ALL
    SELECT to_entity_id AS id FROM entity_edges
),
deg AS (SELECT id, count(*) AS c FROM ec GROUP BY id)
SELECT e.org_id,
       count(*)                                                        AS nodes,
       count(*) FILTER (WHERE COALESCE(deg.c, 0) = 0)                  AS isolated,
       count(*) FILTER (WHERE COALESCE(deg.c, 0) = 1)                  AS degree1,
       ROUND(AVG(COALESCE(deg.c, 0))::numeric, 3)                      AS avg_degree
FROM entities e
LEFT JOIN deg ON deg.id = e.id
WHERE e.valid_until IS NULL
GROUP BY e.org_id
ORDER BY e.org_id;
"""

SQL_EDGES_PER_ORG = "SELECT org_id, count(*) FROM entity_edges GROUP BY org_id;"
SQL_EDGE_TYPES = "SELECT edge_type, count(*) FROM entity_edges GROUP BY edge_type ORDER BY count(*) DESC;"


def collect() -> dict:
    conn = connect_corebrain_admin()
    cur = conn.cursor()

    cur.execute(SQL_EDGES_PER_ORG)
    edges_by_org = {int(r[0]): int(r[1]) for r in cur.fetchall()}

    cur.execute(SQL_PER_ORG)
    orgs = {}
    tot_nodes = tot_iso = tot_deg1 = 0
    for org_id, nodes, isolated, degree1, avg_degree in cur.fetchall():
        org_id = int(org_id)
        nodes, isolated, degree1 = int(nodes), int(isolated), int(degree1)
        orgs[str(org_id)] = {
            "name": ORG_NAMES.get(org_id, f"org{org_id}"),
            "nodes": nodes,
            "edges": edges_by_org.get(org_id, 0),
            "isolated": isolated,
            "isolated_pct": round(100.0 * isolated / nodes, 1) if nodes else 0.0,
            "degree1": degree1,
            "degree1_pct": round(100.0 * degree1 / nodes, 1) if nodes else 0.0,
            "avg_degree": float(avg_degree) if avg_degree is not None else 0.0,
        }
        tot_nodes += nodes
        tot_iso += isolated
        tot_deg1 += degree1

    cur.execute(SQL_EDGE_TYPES)
    edge_types = {r[0]: int(r[1]) for r in cur.fetchall()}

    conn.close()

    total_edges = sum(edges_by_org.values())
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "total": {
            "nodes": tot_nodes,
            "edges": total_edges,
            "isolated": tot_iso,
            "isolated_pct": round(100.0 * tot_iso / tot_nodes, 1) if tot_nodes else 0.0,
            "degree1_pct": round(100.0 * tot_deg1 / tot_nodes, 1) if tot_nodes else 0.0,
            "avg_degree": round(2.0 * total_edges / tot_nodes, 3) if tot_nodes else 0.0,
        },
        "orgs": orgs,
        "edge_types": edge_types,
    }


def print_summary(rep: dict) -> None:
    t = rep["total"]
    print(f"Brain connectivity — {rep['generated']}")
    print(f"  TOTAL: {t['nodes']} nodes · {t['edges']} edges · "
          f"avg-deg {t['avg_degree']} · isolated {t['isolated']} ({t['isolated_pct']}%) · "
          f"deg1 {t['degree1_pct']}%")
    print("  per-org:")
    for _, o in sorted(rep["orgs"].items(), key=lambda kv: int(kv[0])):
        print(f"    {o['name']:<9} {o['nodes']:>6} nodes  {o['edges']:>6} edges  "
              f"avg-deg {o['avg_degree']:<5}  isolated {o['isolated_pct']:>5}%  deg1 {o['degree1_pct']:>5}%")
    allowed = {"motivated_by", "learned_from", "supersedes", "cross_impacts", "references"}
    et = rep["edge_types"]
    ref = et.get("references", 0)
    tot = sum(et.values()) or 1
    print(f"  edge-types: {len(et)} distinct; references={ref} ({round(100*ref/tot)}% — collapse indicator)")


# Drift gate: flag if total isolation ROSE materially since last run, or blew a
# hard ceiling. Written to a sentinel SessionStart surfaces (like .brain-update-failed).
DEGRADED_FLAG = INSTANCE / ".claude" / "state" / ".brain-connectivity-degraded"
DRIFT_PP = 3.0        # isolation rising >3pp run-over-run = drift
HARD_CEILING = 50.0   # absolute backstop


def run_gate(rep: dict) -> None:
    prior = None
    if STATE_PATH.exists():
        try:
            prior = json.loads(STATE_PATH.read_text()).get("total", {}).get("isolated_pct")
        except Exception:
            prior = None
    now = rep["total"]["isolated_pct"]
    reasons = []
    if prior is not None and (now - prior) > DRIFT_PP:
        reasons.append(f"isolation rose {prior}% → {now}% (+{round(now - prior, 1)}pp) since last brain update")
    if now > HARD_CEILING:
        reasons.append(f"isolation {now}% over ceiling {HARD_CEILING}%")
    if reasons:
        DEGRADED_FLAG.write_text("BRAIN CONNECTIVITY DEGRADED — " + "; ".join(reasons) +
                                 f"\nPer-org: {json.dumps({o['name']: o['isolated_pct'] for o in rep['orgs'].values()})}\n")
        print(f"  ⚠️ GATE: degraded — {'; '.join(reasons)} (wrote {DEGRADED_FLAG.name})")
    elif DEGRADED_FLAG.exists():
        DEGRADED_FLAG.unlink()
        print("  GATE: recovered — cleared prior degraded flag")
    else:
        print(f"  GATE: ok (isolation {now}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="write state JSON, no summary")
    ap.add_argument("--json", action="store_true", help="print JSON blob to stdout")
    ap.add_argument("--gate", action="store_true", help="drift-gate: flag .brain-connectivity-degraded on regression")
    args = ap.parse_args()

    rep = collect()

    if args.gate:
        run_gate(rep)  # compares against the prior STATE_PATH before we overwrite it

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(rep, indent=2))

    if args.json:
        print(json.dumps(rep, indent=2))
    elif not args.quiet:
        print_summary(rep)
        print(f"  → wrote {STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
