#!/usr/bin/env python3
"""Name communities in graph.json by fingerprint-anchored derivation.

Per audit `tasks/full-system-audit-2026-05-12/graph-integrity.md` finding #6:
the prior approach keyed curated names by Louvain ID. Louvain IDs reshuffle
across reclusters, so a name like "Same-Tab Storage Event Pattern" attached
to id 24 ended up on a totally different cluster after the next merge.

New approach: every community's name is derived deterministically from its
top-N highest-degree member node labels — a "fingerprint" that follows the
topic across reclusters because high-degree members are stable for a stable
cluster.

Output:
- `community_name` field on every node in graph.json
- `community_names` top-level dict in graph.json (Louvain-id → name)
- `community-names.json` standalone copy
- `community-names-by-fingerprint.json` reverse index (fingerprint → name)
  for cross-recluster name-stability tracking

Curation: edit `CURATED_NAMES_BY_FINGERPRINT` below. Each key is the sorted
join of top-3 highest-degree member node IDs. Override the auto-derived name.

Run AFTER `merge.py` writes graph.json.
"""
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# Per spec-graphify-out-relocation-2026-05-16.md — pipeline outputs in brain repo.
_BRAIN = os.environ.get("CORE_BRAIN")
if not _BRAIN:
    print(f"{Path(__file__).name}: $CORE_BRAIN not set — required.", file=sys.stderr)
    sys.exit(1)
OUT = Path(_BRAIN) / "_build" / "output" / "graphify-out"
OUT.mkdir(parents=True, exist_ok=True)
GRAPH_JSON = OUT / "graph.json"
GRAPH_HTML = OUT / "graph.html"
NAMES_JSON = OUT / "community-names.json"
NAMES_BY_FP_JSON = OUT / "community-names-by-fingerprint.json"

# Number of top-degree members to use as the fingerprint. 3 is the sweet spot:
# stable enough to survive small re-clusters, small enough to be unique.
FINGERPRINT_N = 3

# Curated names — keyed by fingerprint (sorted top-3 member node IDs joined by "|").
# Edit this to override auto-derived names. Survives reclusters because
# fingerprints follow node membership, not Louvain ID assignments.
#
# To add a curated name: run name-communities.py, find the auto-derived name
# you want to override, look up its fingerprint in community-names-by-fingerprint.json,
# add an entry here.
CURATED_NAMES_BY_FINGERPRINT: dict[str, str] = {
    # Example format:
    # "node_a|node_b|node_c": "Human-readable name",
}


def degree_count(edges: list[dict]) -> dict[str, int]:
    """Count degree (in + out) for each node id."""
    deg: dict[str, int] = defaultdict(int)
    for e in edges:
        deg[e.get("source", "")] += 1
        deg[e.get("target", "")] += 1
    return deg


def label_for(node: dict) -> str:
    """Get a display label for a node — prefer label, fall back to id."""
    return (node.get("label") or node.get("id") or "?").strip()


def make_fingerprint(member_node_ids: list[str], top_n: int = FINGERPRINT_N) -> str:
    """Deterministic fingerprint from top-N member ids (sorted, joined)."""
    return "|".join(sorted(member_node_ids[:top_n]))


def derive_name(top_labels: list[str]) -> str:
    """Auto-name a community from its top-degree member labels.

    Prefer a tight, recognizable form: "Topic: <label1>, <label2>, <label3>"
    """
    cleaned = [l.replace("\n", " ").strip()[:60] for l in top_labels[:FINGERPRINT_N] if l]
    if not cleaned:
        return "(empty community)"
    if len(cleaned) == 1:
        return f"Cluster: {cleaned[0]}"
    return f"Cluster: {', '.join(cleaned)}"


def main():
    g = json.loads(GRAPH_JSON.read_text())
    nodes = g.get("nodes", [])
    edges = g.get("edges", [])

    # Compute per-node degree
    deg = degree_count(edges)

    # Group nodes by community
    by_com: dict[int, list[dict]] = defaultdict(list)
    for n in nodes:
        cid = n.get("community")
        if cid is not None:
            by_com[cid].append(n)

    # For each community, derive name + fingerprint
    name_by_cid: dict[int, str] = {}
    fp_by_cid: dict[int, str] = {}
    name_by_fp: dict[str, str] = {}
    auto_count = 0
    curated_count = 0
    singleton_count = 0

    for cid, members in by_com.items():
        # Sort members by degree desc
        members_sorted = sorted(members, key=lambda n: -deg.get(n.get("id", ""), 0))
        top_ids = [m.get("id", "") for m in members_sorted[:FINGERPRINT_N]]
        top_labels = [label_for(m) for m in members_sorted[:FINGERPRINT_N]]

        fp = make_fingerprint(top_ids)
        fp_by_cid[cid] = fp

        # Curated takes precedence
        if fp in CURATED_NAMES_BY_FINGERPRINT:
            name = CURATED_NAMES_BY_FINGERPRINT[fp]
            curated_count += 1
        else:
            name = derive_name(top_labels)
            auto_count += 1

        # Mark singletons explicitly
        if len(members) == 1:
            name = f"Singleton: {top_labels[0]}" if top_labels else "Singleton: (empty)"
            singleton_count += 1

        name_by_cid[cid] = name
        name_by_fp[fp] = name

    # Inject community_name on every node
    for n in nodes:
        cid = n.get("community")
        if cid is not None and cid in name_by_cid:
            n["community_name"] = name_by_cid[cid]

    # Top-level community_names dict in graph.json (sorted by id)
    g["community_names"] = {str(cid): name_by_cid[cid] for cid in sorted(name_by_cid.keys())}

    GRAPH_JSON.write_text(json.dumps(g, indent=2))
    print(f"Wrote community_name to {len(nodes)} nodes in {GRAPH_JSON.name}")
    print(f"Communities named: {len(name_by_cid)}")
    print(f"  curated by fingerprint: {curated_count}")
    print(f"  auto-derived from top-{FINGERPRINT_N} members: {auto_count}")
    print(f"  singletons: {singleton_count}")

    # Standalone names files
    NAMES_JSON.write_text(json.dumps(g["community_names"], indent=2))
    NAMES_BY_FP_JSON.write_text(json.dumps(name_by_fp, indent=2))
    print(f"Wrote {NAMES_JSON.name}, {NAMES_BY_FP_JSON.name}")

    # Patch graph.html — replace "Community <N>" strings with derived names.
    if GRAPH_HTML.exists():
        html = GRAPH_HTML.read_text()
        before = len(html)
        # Replace longest IDs first to avoid "Community 1" matching inside "Community 12"
        for cid in sorted(name_by_cid.keys(), reverse=True):
            name = name_by_cid[cid]
            html = re.sub(rf'\bCommunity {cid}\b(?!\d)', name, html)
        GRAPH_HTML.write_text(html)
        print(f"Patched {GRAPH_HTML.name}: {before:,} -> {len(html):,} bytes")
    else:
        print(f"  (skipped HTML patch — {GRAPH_HTML.name} not found)")


if __name__ == "__main__":
    main()
