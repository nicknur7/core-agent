#!/usr/bin/env python3
"""Compile-truth orchestrator: partition hubs into batches → Sonnet subagents produce
compiled_truth_md → ingest results into entities table.

Phases (run separately):
  --partition          Read all hub files, split into BATCHES batches, write batch-N.json
                       files describing entity-name + source-file list per batch.
  --ingest             Read all batch-N-out.json files and bulk-upsert into entities.compiled_truth_md
                       and entities.last_compiled_at.

The Sonnet subagent work itself is spawned by the orchestrator (main thread / Opus) via
Agent() tool calls in parallel — this script does NOT call Anthropic directly. Each
subagent gets: batch-N.json (input), batch-N-out.json (output path), brief.

Usage:
  python3 compile-truth.py --partition --batches 14 --limit 10  # dry-run partition
  python3 compile-truth.py --partition --batches 14             # full partition
  python3 compile-truth.py --ingest                              # after subagents return
  python3 compile-truth.py --status                              # show progress

Output dir: scheduling/brain-pg/compile-truth-work/
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import connect_corebrain  # noqa: E402

_BRAIN_ENV = os.environ.get("CORE_BRAIN")
if not _BRAIN_ENV:
    sys.exit("ERROR: $CORE_BRAIN env var required.")
BRAIN_ROOT = Path(_BRAIN_ENV)
# Instance-specific path: subagent I/O lives in $CORE_INSTANCE (engine ships clean).
_INSTANCE = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parents[2]))
WORK_DIR = _INSTANCE / "scheduling" / "brain-pg" / "compile-truth-work"

HUB_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def connect():
    return connect_corebrain()


def parse_hub(path: Path) -> dict:
    text = path.read_text(errors="replace")
    m = HUB_FRONTMATTER_RE.match(text)
    fm, body = {}, text
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip()
        body = m.group(2)
    name = fm.get("name") or path.stem.replace("-", " ")
    kind_raw = fm.get("type", "entity").lower()
    kind_map = {"entity": "Entity", "person": "Entity", "topic": "Topic",
                "project": "Project", "tool": "Tool", "decision": "Decision",
                "lesson": "Lesson", "rule": "Rule", "incident": "Incident"}
    kind = kind_map.get(kind_raw, "Topic" if path.parent.name == "topics" else "Entity")
    # Pull session refs from the body — they're lines like:
    #   - projects/<slug>/sessions/<date>_<x>.md — <summary>
    src_files = re.findall(r"projects/[\w\-./]+\.md", body)
    return {"name": name, "kind": kind, "hub_path": str(path), "body": body.strip(),
            "source_files": [str(BRAIN_ROOT / s) for s in src_files]}


def discover_hubs() -> List[Path]:
    return sorted((BRAIN_ROOT / "entities").glob("*.md")) + sorted((BRAIN_ROOT / "topics").glob("*.md"))


def partition_hubs(batches: int, limit: Optional[int] = None) -> List[Path]:
    """Write batch-N.json files. Return paths."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    hubs = discover_hubs()
    if limit:
        hubs = hubs[:limit]
    parsed = [parse_hub(h) for h in hubs]
    # Round-robin partition (balances entity hubs vs topic hubs across workers)
    workers: List[List[dict]] = [[] for _ in range(batches)]
    for i, p in enumerate(parsed):
        workers[i % batches].append(p)

    batch_paths = []
    for n, batch in enumerate(workers, start=1):
        path = WORK_DIR / f"batch-{n:02d}.json"
        path.write_text(json.dumps({
            "batch_id": n,
            "n_entities": len(batch),
            "entities": batch,
        }, indent=2))
        batch_paths.append(path)
    print(f"Partitioned {len(parsed)} hubs into {batches} batches at {WORK_DIR}/")
    for p in batch_paths:
        with open(p) as fp:
            b = json.load(fp)
        print(f"  {p.name}: {b['n_entities']} entities")
    return batch_paths


def ingest_results():
    """Find all batch-*-out.json files, upsert into entities."""
    out_files = sorted(WORK_DIR.glob("batch-*-out.json"))
    if not out_files:
        print(f"No batch-*-out.json files in {WORK_DIR}/. Did the subagents run?")
        sys.exit(1)

    rows = []  # (kind, name, compiled_truth_md, confidence)
    for of in out_files:
        with open(of) as fp:
            data = json.load(fp)
        for entry in data.get("results", []):
            kind = entry.get("kind", "Topic")
            name = entry["name"]
            truth = entry.get("compiled_truth_md", "").strip()
            conf = entry.get("confidence")
            if truth:
                rows.append((kind, name, truth, conf))

    if not rows:
        print("No results to ingest.")
        return
    conn = connect()
    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        UPDATE entities SET
          compiled_truth_md = data.truth,
          confidence = data.conf,
          last_compiled_at = now()
        FROM (VALUES %s) AS data(kind, name, truth, conf)
        WHERE entities.kind = data.kind AND entities.name = data.name
          AND entities.org_id = current_setting('app.current_org_id')::bigint
    """, rows)
    conn.commit()
    print(f"Ingested compiled-truth for {len(rows)} entities into corebrain.")
    # Status check
    cur.execute("SELECT count(*) FROM entities WHERE compiled_truth_md IS NOT NULL AND length(compiled_truth_md) > 50 "
                "AND org_id = current_setting('app.current_org_id')::bigint")
    print(f"Total entities with compiled_truth_md (>50 chars): {cur.fetchone()[0]}")
    conn.close()


def status():
    if not WORK_DIR.exists():
        print(f"{WORK_DIR}/ does not exist — run --partition first.")
        return
    batches = sorted(WORK_DIR.glob("batch-*.json"))
    in_files = [p for p in batches if "-out" not in p.name]
    out_files = sorted(WORK_DIR.glob("batch-*-out.json"))
    print(f"Input batches: {len(in_files)}")
    print(f"Output files:  {len(out_files)}")
    for inp in in_files:
        out = inp.with_name(inp.stem + "-out.json")
        marker = "✓" if out.exists() else "·"
        try:
            with open(inp) as fp:
                n = json.load(fp).get("n_entities", "?")
        except Exception:
            n = "?"
        print(f"  {marker} {inp.name}  ({n} entities)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", action="store_true")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--batches", type=int, default=14)
    ap.add_argument("--limit", type=int, help="Cap hub count (dry-run)")
    args = ap.parse_args()

    if args.partition:
        partition_hubs(args.batches, args.limit)
    elif args.ingest:
        ingest_results()
    elif args.status:
        status()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
