#!/usr/bin/env python3
"""reports-from-checkpoints.py — A4 single-read-dual-output (2026-06-08).

The brain had TWO LLM passes over the same session files: (1) chunk-body extraction (-> graph nodes/edges,
the live recall layer) and (2) a separate report extraction (-> consolidate.py hub labels). A4 collapses
that: the chunk-body the extraction ALREADY produced contains the entities/topics/tools and the
Decision/Lesson reasoning nodes — so the hub reports are DERIVED from it with NO second LLM read.

This is also the durable fix for the Layer-3 "freeze": consolidate.py reads reports/*.json, which were
frozen at May 17 (regenerate-reports.py never existed). Wiring this into the brain update keeps hubs
current for free, off the same single read.

For each chunk-body checkpoint not already covered by an existing report, emit one result:
  {file, session_id, topics[], tools[], entities[], summary}
  topics  = labels of 'topic' nodes      tools = 'tool' nodes
  entities= project/person/org/entity     summary = the top Decision/Lesson node label (LLM-extracted
            node labels, NOT raw-text keyword heuristics — so quality is decent, not the B1 gap).

Output: $CORE_BRAIN/_build/reports/derived-from-checkpoints.json  (consolidate.py reads all reports/*.json).

Usage:
  CORE_BRAIN=... python3 reports-from-checkpoints.py            # write the derived reports
  CORE_BRAIN=... python3 reports-from-checkpoints.py --dry-run  # report counts only
"""
import json
import os
import sys
from pathlib import Path

BRAIN = Path(os.environ.get("CORE_BRAIN", str(Path.home() / "AI Projects" / "core-brain")))
CHECKPOINTS = BRAIN / "_build" / "output" / "checkpoints"
REPORTS = BRAIN / "_build" / "reports"
OUT = REPORTS / "derived-from-checkpoints.json"

ENTITY_KINDS = {"project", "person", "organization", "entity", "company", "Entity"}


def covered_files():
    """Files already present in any existing report (skip them — don't double-cover)."""
    seen = set()
    if not REPORTS.is_dir():
        return seen
    for rp in REPORTS.glob("*.json"):
        if rp.name == OUT.name:
            continue
        try:
            data = json.loads(rp.read_text())
        except Exception:
            continue
        for r in data.get("results", []):
            f = r.get("file")
            if f:
                seen.add(f)
    return seen


def derive(cp):
    try:
        d = json.loads(cp.read_text())
    except Exception:
        return None
    meta = d.get("metadata", {}) or {}
    src = meta.get("source_file")
    if not src:
        return None
    nodes = d.get("nodes", [])
    def kind(n):
        return n.get("type") or n.get("kind") or ""
    def label(n):
        return (n.get("label") or n.get("name") or n.get("id") or "").strip()
    topics = sorted({label(n) for n in nodes if kind(n) == "topic" and label(n)})
    tools = sorted({label(n) for n in nodes if kind(n) == "tool" and label(n)})
    entities = sorted({label(n) for n in nodes if kind(n) in ENTITY_KINDS and label(n)})
    # summary: first Decision, else Lesson, else session node, else first node.
    summary = ""
    for want in ("Decision", "Lesson", "Incident", "session", "subagent_session"):
        hit = next((label(n) for n in nodes if kind(n) == want and label(n)), "")
        if hit:
            summary = hit[:300]
            break
    if not summary and nodes:
        summary = label(nodes[0])[:300]
    return {
        "file": src,
        "session_id": meta.get("chunk_id", ""),
        "topics": topics,
        "tools": tools,
        "entities": entities,
        "summary": summary,
    }


def main(dry_run=False):
    if not CHECKPOINTS.is_dir():
        print(f"no checkpoints dir: {CHECKPOINTS}")
        return 0
    seen = covered_files()
    results, skipped = [], 0
    for cp in sorted(CHECKPOINTS.glob("chunk-body-*.json")):
        r = derive(cp)
        if not r:
            continue
        if r["file"] in seen:
            skipped += 1
            continue
        results.append(r)
    print(f"checkpoints derived: {len(results)} new  ({skipped} already covered by existing reports)")
    if dry_run:
        print("(--dry-run — no file written)")
        return 0
    REPORTS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"results": results}, indent=1))
    print(f"WROTE {OUT.relative_to(BRAIN)} — {len(results)} results")
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
