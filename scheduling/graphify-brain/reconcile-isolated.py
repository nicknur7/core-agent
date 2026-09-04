#!/usr/bin/env python3
"""Reconcile isolated nodes by string-similarity to connected nodes.

Per `tasks/full-system-audit-2026-05-12/graph-integrity.md` finding #4:
~60% of 516 isolated nodes have sibling concepts already in the connected
component but no edge linking them (e.g., `rule_no_spend_money` is isolated
while `rule_core_hard_rule_never_spend_money` lives in a connected community,
and 7 spend-related nodes form an island elsewhere).

This pass emits a chunk file with INFERRED `references` edges from each
isolated node to its best string-similarity match among connected nodes.
The chunk gets absorbed by the next merge.py run, so isolated nodes stop
being isolated and recall improves immediately.

Threshold: SIMILARITY_THRESHOLD (default 0.70). Below this, no edge is
emitted — better to leave a node truly orphaned than to invent a wrong link.

Output: scheduling/graphify-brain/checkpoints/chunk-reconcile-isolated-YYYY-MM-DD.json
(Note: NOT chunk-body- prefix — this is a synthetic edge-only chunk, not
a body-extraction output.)

Run AFTER merge.py + name-communities.py have produced the current graph.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

REPO = Path(__file__).resolve().parent
# Per spec-graphify-out-relocation-2026-05-16.md — pipeline outputs in brain repo.
_BRAIN = os.environ.get("CORE_BRAIN")
if not _BRAIN:
    print(f"{Path(__file__).name}: $CORE_BRAIN not set — required.", file=sys.stderr)
    sys.exit(1)
_PIPELINE_OUT = Path(_BRAIN) / "_build" / "output"
OUT = _PIPELINE_OUT / "graphify-out"
GRAPH_JSON = OUT / "graph.json"
CHECKPOINTS = _PIPELINE_OUT / "checkpoints"
OUT.mkdir(parents=True, exist_ok=True)
CHECKPOINTS.mkdir(parents=True, exist_ok=True)

SIMILARITY_THRESHOLD = 0.70
TOP_K_PER_ISOLATED = 1  # how many matches to emit per isolated node
JACCARD_PREFILTER = 0.30  # token-set Jaccard floor before paying for SequenceMatcher
MIN_TOKEN_OVERLAP = 1  # at least this many shared tokens after stop-word strip


# Stop-words filtered out of token sets — too generic to be signal
STOP_WORDS = {
    "the", "a", "an", "of", "to", "in", "on", "at", "and", "or", "for",
    "with", "by", "from", "is", "are", "was", "were", "be", "core",
    "session", "agent", "subagent",
}


def normalize_for_compare(s: str) -> str:
    """Normalize label/id for similarity comparison.

    Strip type prefix (entity_/topic_/rule_/etc.) and collapse separators.
    `rule_no_spend_money` -> `no spend money`
    `rule_core_hard_rule_never_spend_money` -> `core hard rule never spend money`
    """
    s = s.lower()
    # Strip common type prefixes
    for prefix in ("rule_", "decision_", "lesson_", "incident_", "topic_",
                   "entity_", "tool_", "code_", "session_", "subagent_",
                   "hypothesis_", "tradeoff_", "project_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.replace("_", " ").replace("-", " ").strip()


def tokens_of(s: str) -> set[str]:
    """Token set with stop-words removed."""
    return {t for t in s.split() if t and t not in STOP_WORDS and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    return len(inter) / len(a | b)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def main():
    g = json.loads(GRAPH_JSON.read_text())
    nodes = g.get("nodes", [])
    edges = g.get("edges", [])

    # Connected vs isolated
    has_edge: set[str] = set()
    for e in edges:
        has_edge.add(e.get("source", ""))
        has_edge.add(e.get("target", ""))

    isolated_nodes = [n for n in nodes if n.get("id", "") not in has_edge]
    connected_nodes = [n for n in nodes if n.get("id", "") in has_edge]

    print(f"Isolated: {len(isolated_nodes)}, connected: {len(connected_nodes)}")

    # Pre-normalize + tokenize all connected labels for fast comparison.
    # Index by token to enable cheap candidate lookup (only consider connected
    # nodes that share at least one non-stopword token with the isolated node).
    conn_norm: list[tuple[str, str, str, set[str]]] = []  # (id, label, norm, tokens)
    token_index: dict[str, list[int]] = {}  # token -> list of conn_norm indices
    for n in connected_nodes:
        nid = n.get("id", "")
        label = n.get("label", "") or nid
        norm = normalize_for_compare(label) or normalize_for_compare(nid)
        if not norm:
            continue
        toks = tokens_of(norm)
        if not toks:
            continue
        idx = len(conn_norm)
        conn_norm.append((nid, label, norm, toks))
        for t in toks:
            token_index.setdefault(t, []).append(idx)

    # For each isolated node, find best match via token-prefilter -> Jaccard -> SequenceMatcher
    new_edges: list[dict] = []
    matched_count = 0
    examined_avg = 0
    examined_total = 0

    for iso in isolated_nodes:
        iso_id = iso.get("id", "")
        iso_label = iso.get("label", "") or iso_id
        iso_norm = normalize_for_compare(iso_label) or normalize_for_compare(iso_id)
        if not iso_norm:
            continue
        iso_toks = tokens_of(iso_norm)
        if len(iso_toks) < MIN_TOKEN_OVERLAP:
            continue

        # Candidate set: any connected node sharing at least one token
        candidate_idxs: set[int] = set()
        for t in iso_toks:
            for idx in token_index.get(t, ()):
                candidate_idxs.add(idx)

        examined_total += len(candidate_idxs)

        # Filter by Jaccard, then score by SequenceMatcher on the survivors
        scored: list[tuple[float, str, str]] = []
        for idx in candidate_idxs:
            cid, clabel, cnorm, ctoks = conn_norm[idx]
            j = jaccard(iso_toks, ctoks)
            if j < JACCARD_PREFILTER:
                continue
            score = similarity(iso_norm, cnorm)
            if score >= SIMILARITY_THRESHOLD:
                scored.append((score, cid, clabel))

        scored.sort(reverse=True)
        for (score, cid, clabel) in scored[:TOP_K_PER_ISOLATED]:
            new_edges.append({
                "source": iso_id,
                "target": cid,
                "relation": "references",
                "properties": {
                    "confidence": "INFERRED",
                    "_inferred_by": "reconcile-isolated.py",
                    "_similarity_score": round(score, 3),
                    "source_excerpt": f"Auto-reconciled: '{iso_label}' ↔ '{clabel}' (string similarity {score:.2f})",
                },
            })
            matched_count += 1

    if isolated_nodes:
        examined_avg = examined_total / len(isolated_nodes)

    # Emit chunk
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    chunk_path = CHECKPOINTS / f"chunk-reconcile-isolated-{today}.json"

    chunk = {
        "metadata": {
            "chunk_id": chunk_path.name,
            "source_file": "reconcile-isolated.py",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "extractor_model": "reconcile-isolated.py (string similarity, threshold=%.2f)" % SIMILARITY_THRESHOLD,
            "node_count": 0,
            "edge_count": len(new_edges),
        },
        "nodes": [],
        "edges": new_edges,
    }

    chunk_path.write_text(json.dumps(chunk, indent=2))

    print(f"Wrote {chunk_path.name}")
    print(f"  Reconciled {matched_count} of {len(isolated_nodes)} isolated nodes "
          f"({100*matched_count/max(len(isolated_nodes),1):.1f}%)")
    print(f"  {len(new_edges)} INFERRED 'references' edges emitted")
    print(f"  Avg candidates examined per isolated (token prefilter): {examined_avg:.1f}")
    print(f"  Re-run merge.py to absorb.")


if __name__ == "__main__":
    main()
