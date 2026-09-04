#!/usr/bin/env python3
"""Step 7 benchmark: hybrid RRF vs grep+BFS baseline on ~30 representative queries.

Each query in the eval set has:
  - text: the recall query as Nick would type it
  - relevant: list of source_file substrings that MUST appear in returned results to count as relevant
  - notes: free-text description for human review

For each query, run both retrievers, compute P@5 and R@5, compare. Acceptance: hybrid
must beat baseline by >=20 percentage points on R@5 (spec line 115).

Output: tasks/research/brain-primitives-benchmark-YYYY-MM-DD.md

Usage:
  python3 eval.py                            # run full benchmark, write report
  python3 eval.py --query "<text>"           # ad-hoc single-query inspect
  python3 eval.py --eval-set ./eval.json     # use custom eval set
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict

# THIS IS A SLOW BATCH JOB AND THE DEFAULT TIMEOUT KILLS IT. Set before query.py is imported,
# because _env.connect_corebrain() reads COREBRAIN_STATEMENT_TIMEOUT_MS at connect time.
#
# _env.py:240 defaults to 30_000 ms — correct for the interactive/hook paths it was written for,
# where a wedged query must not hold a close open. This runs 30 queries x 4 legs over the whole
# corpus; the FTS leg alone exceeds 30s, so a plain `python3 eval.py` dies with
# `psycopg2.errors.QueryCanceled: canceling statement due to statement timeout` — every time.
#
# That is why the recall-eval benchmark sat >5 weeks stale while core-si's proposed fix read
# "run eval.py + schedule it nightly". The instruction was correct and the command it named
# could not succeed, so the item could never clear. Found 2026-08-17 by running it rather than
# re-reading the item.
#
# _env.py:238 already anticipated exactly this: "Overridable so a genuinely slow legitimate query
# is not capped by a number chosen here." This IS that query, so it declares its own need instead
# of expecting every caller to know. An explicit env var still wins — setdefault, not assignment.
os.environ.setdefault("COREBRAIN_STATEMENT_TIMEOUT_MS", "300000")

# Reuse query.py functions
sys.path.insert(0, str(Path(__file__).resolve().parent))
from query import hybrid_query, grep_baseline

# Instance-specific paths: eval set + report dir live in $CORE_INSTANCE
# (the engine ships clean of personal data).
_INSTANCE = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parents[2]))
EVAL_SET_DEFAULT = _INSTANCE / "scheduling" / "brain-pg" / "eval-set.json"
REPORT_DIR = _INSTANCE / "tasks" / "research"


def precision_at_k(results: List[dict], relevant_substrs: List[str], k: int = 5) -> float:
    """v1 schema: substring matching."""
    if not results:
        return 0.0
    top_k = results[:k]
    hits = sum(1 for r in top_k if any(s.lower() in r["source"].lower() or s.lower() in r["excerpt"].lower()
                                       for s in relevant_substrs))
    return hits / k


def recall_at_k(results: List[dict], relevant_substrs: List[str], k: int = 5) -> float:
    """v1 schema: substring matching."""
    if not relevant_substrs:
        return 0.0
    top_k = results[:k]
    hits = set()
    for s in relevant_substrs:
        for r in top_k:
            if s.lower() in r["source"].lower() or s.lower() in r["excerpt"].lower():
                hits.add(s)
                break
    return len(hits) / len(relevant_substrs)


# --- v2 scoring (entity_id + source_file ground truth) ------------------------

_NAME_TO_ID_CACHE: Dict[str, int] = {}


def _build_name_to_id_cache():
    """One-time pull of (name, kind) → entity.id map. Used by v2 scoring to resolve
    a result's source field (entity name for kind=entity rows) back to entity_id."""
    global _NAME_TO_ID_CACHE
    if _NAME_TO_ID_CACHE:
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _env import connect_corebrain  # noqa: E402
    conn = connect_corebrain()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM entities WHERE valid_until IS NULL "
                "AND org_id = current_setting('app.current_org_id')::bigint")
    for row in cur.fetchall():
        # Multiple kinds can share a name; first-wins. Resolution good enough for scoring.
        _NAME_TO_ID_CACHE.setdefault(row[1], row[0])
    conn.close()


def _is_hit_v2(result: dict, relevant_entity_ids: List[int], relevant_source_files: List[str]) -> bool:
    """v2 hit: result counts if its entity_id ∈ relevant OR its source ∈ relevant_source_files."""
    src = result.get("source", "")
    # entity-kind results: source is the entity name; look up the id
    if result.get("kind") == "entity":
        eid = _NAME_TO_ID_CACHE.get(src)
        if eid is not None and eid in relevant_entity_ids:
            return True
    # evidence-kind / grep-kind: source is the file path
    if src in relevant_source_files:
        return True
    # tolerate path-prefix match (relevant could be a stem)
    for rf in relevant_source_files:
        if rf and (src.endswith(rf) or rf in src):
            return True
    return False


def precision_at_k_v2(results, rel_ids, rel_sf, k=5):
    if not results:
        return 0.0
    top_k = results[:k]
    hits = sum(1 for r in top_k if _is_hit_v2(r, rel_ids, rel_sf))
    return hits / k


def recall_at_k_v2(results, rel_ids, rel_sf, k=5):
    """v2 recall: fraction of (entity_ids ∪ source_files) that have ≥1 retrieval hit in top-k."""
    rel_total = len(rel_ids) + len(rel_sf)
    if rel_total == 0:
        return 0.0
    top_k = results[:k]
    hit_eids = set()
    hit_sfs = set()
    for r in top_k:
        if r.get("kind") == "entity":
            eid = _NAME_TO_ID_CACHE.get(r.get("source", ""))
            if eid in rel_ids:
                hit_eids.add(eid)
        src = r.get("source", "")
        if src in rel_sf:
            hit_sfs.add(src)
        else:
            for rf in rel_sf:
                if rf and (src.endswith(rf) or rf in src):
                    hit_sfs.add(rf)
                    break
    return (len(hit_eids) + len(hit_sfs)) / rel_total


def _is_v2(eval_set: List[dict]) -> bool:
    return bool(eval_set) and "relevant_entity_ids" in eval_set[0]


def run_eval(eval_set: List[dict]) -> Dict:
    v2 = _is_v2(eval_set)
    if v2:
        _build_name_to_id_cache()
    per_query = []
    p5_h, r5_h, p5_b, r5_b = [], [], [], []
    for q in eval_set:
        hybrid = hybrid_query(q["text"], k=10)
        baseline = grep_baseline(q["text"], k=10)
        if v2:
            rel_ids = q.get("relevant_entity_ids", [])
            rel_sf = q.get("relevant_source_files", [])
            p5h = precision_at_k_v2(hybrid, rel_ids, rel_sf, 5)
            r5h = recall_at_k_v2(hybrid, rel_ids, rel_sf, 5)
            p5b = precision_at_k_v2(baseline, rel_ids, rel_sf, 5)
            r5b = recall_at_k_v2(baseline, rel_ids, rel_sf, 5)
            n_relevant = len(rel_ids) + len(rel_sf)
        else:
            rel = q["relevant"]
            p5h = precision_at_k(hybrid, rel, 5)
            r5h = recall_at_k(hybrid, rel, 5)
            p5b = precision_at_k(baseline, rel, 5)
            r5b = recall_at_k(baseline, rel, 5)
            n_relevant = len(rel)
        p5_h.append(p5h); r5_h.append(r5h); p5_b.append(p5b); r5_b.append(r5b)
        per_query.append({
            "query": q["text"],
            "shape": q.get("shape", ""),
            "notes": q.get("notes", ""),
            "n_relevant": n_relevant,
            "hybrid": {"p5": round(p5h, 3), "r5": round(r5h, 3), "top5": [r["source"] for r in hybrid[:5]]},
            "baseline": {"p5": round(p5b, 3), "r5": round(r5b, 3), "top5": [r["source"] for r in baseline[:5]]},
            "delta_r5_pp": round((r5h - r5b) * 100, 1),
        })
    summary = {
        "n_queries": len(eval_set),
        "hybrid_p5_mean": round(sum(p5_h) / len(p5_h), 3) if p5_h else 0,
        "hybrid_r5_mean": round(sum(r5_h) / len(r5_h), 3) if r5_h else 0,
        "baseline_p5_mean": round(sum(p5_b) / len(p5_b), 3) if p5_b else 0,
        "baseline_r5_mean": round(sum(r5_b) / len(r5_b), 3) if r5_b else 0,
    }
    summary["r5_lift_pp"] = round((summary["hybrid_r5_mean"] - summary["baseline_r5_mean"]) * 100, 1)
    summary["acceptance_threshold_pp"] = 20.0
    summary["passes"] = summary["r5_lift_pp"] >= 20.0
    return {"summary": summary, "per_query": per_query}


def write_report(result: Dict, eval_set_path: Path):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    # Distinguish v2 reports so they don't overwrite v1
    suffix = "-v2" if "v2" in eval_set_path.name else ""
    out = REPORT_DIR / f"brain-primitives-benchmark{suffix}-{today}.md"
    s = result["summary"]
    lines = [
        f"# Brain primitives benchmark — {today}",
        "",
        f"Eval set: `{eval_set_path}` ({s['n_queries']} queries)",
        "",
        "## Headline",
        "",
        f"| Metric | Hybrid RRF | grep+BFS baseline | Δ |",
        f"|---|---|---|---|",
        f"| P@5 mean | {s['hybrid_p5_mean']} | {s['baseline_p5_mean']} | {round((s['hybrid_p5_mean']-s['baseline_p5_mean'])*100,1)} pp |",
        f"| R@5 mean | {s['hybrid_r5_mean']} | {s['baseline_r5_mean']} | **{s['r5_lift_pp']} pp** |",
        "",
        f"**Acceptance threshold: ≥{s['acceptance_threshold_pp']} pp R@5 lift.**",
        f"**Result: {'PASS — wire hybrid into /recall-similar and claude-brain.' if s['passes'] else 'FAIL — do NOT auto-wire substrate; investigate which leg is underperforming.'}**",
        "",
        "## Per-query breakdown",
        "",
        "| # | Query | Δ R@5 (pp) | Hybrid R@5 | Baseline R@5 | Hybrid P@5 | Baseline P@5 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, q in enumerate(result["per_query"], 1):
        lines.append(f"| {i} | {q['query'][:60]} | {q['delta_r5_pp']} | {q['hybrid']['r5']} | {q['baseline']['r5']} | {q['hybrid']['p5']} | {q['baseline']['p5']} |")
    lines.append("")
    lines.append("## Per-query top-5 sources (hybrid vs baseline)")
    lines.append("")
    for i, q in enumerate(result["per_query"], 1):
        lines.append(f"### {i}. {q['query']}")
        lines.append(f"_Relevant substrings expected: {q['n_relevant']}_ — {q['notes']}")
        lines.append("")
        lines.append("**Hybrid top-5:**")
        for s in q["hybrid"]["top5"]:
            lines.append(f"- `{s}`")
        lines.append("")
        lines.append("**Baseline top-5:**")
        for s in q["baseline"]["top5"]:
            lines.append(f"- `{s}`")
        lines.append("")
    out.write_text("\n".join(lines))
    print(f"Report: {out}")
    return out


# --- Per-leg ablation (Phase 0 of the brain-connectivity fix) ---------------
# Answers "does each of the 4 legs earn its keep on R@5?" using query.py's
# per-leg toggles. Run before AND after FIX1/FIX2 to separate "graph leg is
# useless" from "graph leg was broken."
ABLATION_CONFIGS = [
    ("vector",             dict(use_vector=True,  use_fts=False, use_graph=False, use_edge_vector=False)),
    ("fts",                dict(use_vector=False, use_fts=True,  use_graph=False, use_edge_vector=False)),
    ("graph",              dict(use_vector=False, use_fts=False, use_graph=True,  use_edge_vector=False)),
    ("edge_vector",        dict(use_vector=False, use_fts=False, use_graph=False, use_edge_vector=True)),
    ("vector+fts",         dict(use_vector=True,  use_fts=True,  use_graph=False, use_edge_vector=False)),
    ("vector+fts+graph",   dict(use_vector=True,  use_fts=True,  use_graph=True,  use_edge_vector=False)),
    ("vector+fts+edgevec", dict(use_vector=True,  use_fts=True,  use_graph=False, use_edge_vector=True)),
    ("all4",               dict(use_vector=True,  use_fts=True,  use_graph=True,  use_edge_vector=True)),
]


def _score(results, q, v2):
    if v2:
        rel_ids = q.get("relevant_entity_ids", [])
        rel_sf = q.get("relevant_source_files", [])
        return precision_at_k_v2(results, rel_ids, rel_sf, 5), recall_at_k_v2(results, rel_ids, rel_sf, 5)
    rel = q["relevant"]
    return precision_at_k(results, rel, 5), recall_at_k(results, rel, 5)


def run_ablation(eval_set: List[dict]) -> List[Dict]:
    v2 = _is_v2(eval_set)
    if v2:
        _build_name_to_id_cache()
    rows = []
    for label, cfg in ABLATION_CONFIGS:
        p5s, r5s = [], []
        for q in eval_set:
            res = hybrid_query(q["text"], k=10, **cfg)
            p5, r5 = _score(res, q, v2)
            p5s.append(p5); r5s.append(r5)
        rows.append({"config": label,
                     "p5_mean": round(sum(p5s) / len(p5s), 3) if p5s else 0.0,
                     "r5_mean": round(sum(r5s) / len(r5s), 3) if r5s else 0.0})
    return rows


def print_ablation(rows: List[Dict]) -> None:
    base = next((r for r in rows if r["config"] == "all4"), None)
    print(f"{'config':<22} {'R@5':>6} {'P@5':>6} {'ΔR@5 vs all4':>14}")
    print("-" * 52)
    for r in rows:
        d = "" if not base else f"{round((r['r5_mean'] - base['r5_mean']) * 100, 1):+g} pp"
        print(f"{r['config']:<22} {r['r5_mean']:>6} {r['p5_mean']:>6} {d:>14}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-set", type=Path, default=EVAL_SET_DEFAULT)
    ap.add_argument("--query")
    ap.add_argument("--ablation", action="store_true", help="per-leg ablation R@5 over the eval set")
    args = ap.parse_args()

    if args.ablation:
        if not args.eval_set.exists():
            sys.exit(f"Eval set missing: {args.eval_set}")
        eval_set = json.loads(args.eval_set.read_text())
        rows = run_ablation(eval_set)
        print_ablation(rows)
        out = _INSTANCE / ".claude" / "state" / ".brain-leg-ablation.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"generated": datetime.now().isoformat(), "configs": rows}, indent=2))
        print(f"→ wrote {out}")
        return

    if args.query:
        h = hybrid_query(args.query, k=5)
        b = grep_baseline(args.query, k=5)
        print("HYBRID:"); [print(f"  {i+1}. {r['source']}") for i, r in enumerate(h)]
        print("BASELINE:"); [print(f"  {i+1}. {r['source']}") for i, r in enumerate(b)]
        return

    if not args.eval_set.exists():
        sys.exit(f"Eval set missing: {args.eval_set}. Create eval-set.json with ~30 queries first.")
    eval_set = json.loads(args.eval_set.read_text())
    result = run_eval(eval_set)
    print(json.dumps(result["summary"], indent=2))
    write_report(result, args.eval_set)


if __name__ == "__main__":
    main()
