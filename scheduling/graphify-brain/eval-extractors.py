#!/usr/bin/env python3
"""
eval-extractors.py — extraction quality/cost bake-off across Claude + Codex models.

Answers Nick's 2026-07-19 question: which model gives the highest extraction quality at the
lowest cost, for the headless brain extractor (plan: brain-lifecycle-unification-2026-07-19).

Runs each candidate on the SAME fixed sample, scores on the contract's own validation bar,
and computes cost/file. Reference = the existing Agent()-Sonnet checkpoint for each file (the
current production-quality bar) when present.

Backends:
  claude:<id>   via anthropic SDK   (needs a valid ANTHROPIC_API_KEY)
  codex:<model> via `codex exec`     (uses ChatGPT auth; works headless now)

Usage:
  eval-extractors.py --candidate codex:luna --candidate codex:terra
  eval-extractors.py --candidate claude:claude-haiku-4-5 --candidate claude:claude-sonnet-5
  eval-extractors.py --all-codex            # luna, terra, sol
  eval-extractors.py --sample-list <file>   # override the default sample
Output: one scored row per candidate + a JSON summary line.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CORE_BRAIN = Path(os.environ.get("CORE_BRAIN", str(Path.home() / "AI Projects/core-brain")))
CORE_INSTANCE = Path(os.environ.get("CORE_INSTANCE", str(Path.cwd())))
PROMPT_FILE = CORE_INSTANCE / "scheduling/graphify-brain/body-extraction-prompt.md"
CHECKPOINTS = CORE_BRAIN / "_build/output/checkpoints"
EVAL_OUT = CORE_INSTANCE / "scheduling/graphify-brain/eval-work"

# $/MTok (in, out) — from claude-api skill (cached 2026-06-24) + codex-routing.md
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0), "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-4-8": (5.0, 25.0),
    "codex:gpt-5.6-luna": (1.0, 6.0), "codex:gpt-5.6-terra": (2.5, 15.0), "codex:gpt-5.6-sol": (10.0, 50.0),
}
REASONING_TYPES = {"Decision", "Lesson", "Rule", "Incident", "Hypothesis", "Tradeoff",
                   "decision", "lesson", "rule", "incident", "hypothesis", "tradeoff"}
REASONING_RELS = {"motivated_by", "learned_from", "supersedes", "addresses", "cross_impacts",
                  "validated_by", "contradicts"}

# Fixed sample — set once so every candidate is scored on identical inputs.
DEFAULT_SAMPLE = [
    "projects/life/subagents/2026-07-18_agent-a8972eec8adb8e69b.md",  # adversarial architect review = signal
    "projects/life/subagents/2026-07-18_agent-a7d5f8842c5f46caa.md",  # sentinel verdict = noise/gate check
]


def load_prompt_parts():
    text = PROMPT_FILE.read_text()
    s, u = text.find("## SYSTEM PROMPT"), text.find("## USER PROMPT TEMPLATE")
    system = text[s:u].split("\n", 1)[1].strip()
    user_tmpl = text[u:].split("```\n", 1)[1].split("\n```", 1)[0].strip()
    return system, user_tmpl


def build_prompt(user_tmpl, rel, content):
    ft = "session" if "/sessions/" in rel else ("subagent" if "/subagents/" in rel else "other")
    date = (re.search(r"(\d{4}-\d{2}-\d{2})", Path(rel).name) or [None, "unknown"])[1] \
        if re.search(r"(\d{4}-\d{2}-\d{2})", Path(rel).name) else "unknown"
    return (user_tmpl.replace("{filename}", Path(rel).name).replace("{source_path}", rel)
            .replace("{session|subagent|topic|entity|tool|other}", ft)
            .replace("{date}", date).replace("{file_content}", content))


def _strip_fences(t):
    t = t.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def run_claude(model, system, prompt):
    from anthropic import Anthropic
    c = Anthropic()
    t0 = time.time()
    r = c.messages.create(model=model, max_tokens=16000, system=system,
                          messages=[{"role": "user", "content": prompt}])
    txt = "".join(b.text for b in r.content if b.type == "text")
    return txt, r.usage.input_tokens, r.usage.output_tokens, time.time() - t0


def run_codex(model, system, prompt):
    full = system + "\n\n" + prompt
    t0 = time.time()
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        outfile = f.name
    try:
        p = subprocess.run(
            ["codex", "exec", "-s", "read-only", "--skip-git-repo-check", "-m", model,
             "--output-last-message", outfile, full],
            capture_output=True, text=True, timeout=300)
        txt = Path(outfile).read_text() if Path(outfile).exists() else p.stdout
        # rough token estimate for cost (codex doesn't return usage here): chars/4
        tin = len(full) // 4
        tout = len(txt) // 4
        return txt, tin, tout, time.time() - t0
    finally:
        try: os.unlink(outfile)
        except OSError: pass


def score(txt, rel):
    """Return quality metrics for one extraction."""
    m = {"json_valid": False, "schema_ok": False, "nodes": 0, "edges": 0,
         "reasoning_nodes": 0, "reasoning_edges": 0, "rz_edge_excerpt_cov": 0.0,
         "node_excerpt_cov": 0.0}
    try:
        d = json.loads(_strip_fences(txt))
    except Exception:
        return m
    m["json_valid"] = True
    nodes, edges = d.get("nodes", []), d.get("edges", [])
    m["schema_ok"] = isinstance(nodes, list) and isinstance(edges, list) and "metadata" in d
    m["nodes"], m["edges"] = len(nodes), len(edges)
    rz_nodes = [n for n in nodes if n.get("type") in REASONING_TYPES]
    m["reasoning_nodes"] = len(rz_nodes)
    if rz_nodes:
        m["node_excerpt_cov"] = round(sum(1 for n in rz_nodes
            if (n.get("properties") or {}).get("source_excerpt")) / len(rz_nodes), 2)
    rz_edges = [e for e in edges if e.get("relation") in REASONING_RELS]
    m["reasoning_edges"] = len(rz_edges)
    if rz_edges:
        m["rz_edge_excerpt_cov"] = round(sum(1 for e in rz_edges
            if (e.get("properties") or {}).get("source_excerpt")) / len(rz_edges), 2)
    return m


def judge_faithfulness(source_content, extraction_txt, judge_backend, system):
    """A judge model scores whether the extraction faithfully reflects the source.
    Returns (score_0_10, one_line_note). judge_backend: 'claude:<id>' or 'codex:<model>'."""
    src = source_content[:60_000]
    ext = _strip_fences(extraction_txt)[:20_000]
    jprompt = (
        "You are grading a knowledge-graph extraction against its SOURCE transcript. "
        "Score 0-10 on FAITHFULNESS+RECALL: are the extracted Decision/Lesson/Rule/Incident nodes "
        "actually supported by the source (not hallucinated), and are the source's real decisions/lessons "
        "captured (not missed)? Penalize invented content HARD. Reply with exactly: SCORE=<0-10> | <one-line reason>.\n\n"
        f"SOURCE:\n---\n{src}\n---\n\nEXTRACTION:\n---\n{ext}\n---\n\nSCORE="
    )
    try:
        if judge_backend.startswith("codex:"):
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
                of = f.name
            subprocess.run(["codex", "exec", "-s", "read-only", "--skip-git-repo-check",
                            "-m", judge_backend.split(":", 1)[1], "--output-last-message", of, jprompt],
                           capture_output=True, text=True, timeout=200)
            out = Path(of).read_text() if Path(of).exists() else ""
            os.unlink(of)
        else:
            from anthropic import Anthropic
            r = Anthropic().messages.create(model=judge_backend.split(":", 1)[-1], max_tokens=200,
                                            messages=[{"role": "user", "content": jprompt}])
            out = "".join(b.text for b in r.content if b.type == "text")
        m = re.search(r"SCORE\s*=?\s*(\d+(?:\.\d+)?)", out)
        note = out.strip().split("|", 1)[-1].strip()[:120] if "|" in out else out.strip()[:120]
        return (float(m.group(1)) if m else None, note)
    except Exception as e:
        return (None, f"judge error: {str(e)[:60]}")


def reference_counts(rel):
    cp = CHECKPOINTS / f"chunk-body-{Path(rel).stem}.json"
    if not cp.exists():
        return None
    try:
        d = json.loads(cp.read_text())
        return {"nodes": len(d.get("nodes", [])), "edges": len(d.get("edges", []))}
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", action="append", default=[])
    ap.add_argument("--all-codex", action="store_true")
    ap.add_argument("--sample-list")
    ap.add_argument("--judge", help="faithfulness judge backend, e.g. claude:claude-opus-4-8 or codex:gpt-5.6-sol")
    a = ap.parse_args()
    cands = list(a.candidate)
    if a.all_codex:
        cands += ["codex:gpt-5.6-luna", "codex:gpt-5.6-terra", "codex:gpt-5.6-sol"]
    if not cands:
        sys.exit("no --candidate given")
    sample = ([l.strip() for l in Path(a.sample_list).read_text().split("\n") if l.strip()]
              if a.sample_list else DEFAULT_SAMPLE)

    system, user_tmpl = load_prompt_parts()
    EVAL_OUT.mkdir(parents=True, exist_ok=True)
    summary = []

    for cand in cands:
        is_codex = cand.startswith("codex:")
        price = PRICES.get(cand if is_codex else cand.split(":", 1)[-1], (0, 0))
        print(f"\n=== {cand} ===")
        agg = {"json_valid": 0, "files": 0, "nodes": 0, "reasoning_nodes": 0,
               "node_excerpt_cov": [], "cost": 0.0, "sec": 0.0}
        for rel in sample:
            src = CORE_BRAIN / rel
            if not src.exists():
                print(f"  MISSING {rel}"); continue
            content = src.read_text(errors="ignore")[:200_000]
            prompt = build_prompt(user_tmpl, rel, content)
            try:
                if is_codex:
                    txt, tin, tout, sec = run_codex(cand.split(":", 1)[1], system, prompt)
                else:
                    txt, tin, tout, sec = run_claude(cand.split(":", 1)[-1], system, prompt)
            except Exception as e:
                print(f"  ERROR {rel}: {str(e)[:80]}"); continue
            # save the raw extraction for inspection
            (EVAL_OUT / f"{cand.replace(':', '_')}__{Path(rel).stem}.json").write_text(txt)
            sc = score(txt, rel)
            cost = (tin * price[0] + tout * price[1]) / 1_000_000
            ref = reference_counts(rel)
            refstr = f" ref={ref['nodes']}n/{ref['edges']}e" if ref else " ref=none"
            jstr = ""
            if a.judge and sc["json_valid"]:
                jscore, jnote = judge_faithfulness(content, txt, a.judge, system)
                agg.setdefault("judge", []).append(jscore) if jscore is not None else None
                jstr = f" JUDGE={jscore} ({jnote})"
            print(f"  {Path(rel).name[:40]:40} json={sc['json_valid']} "
                  f"nodes={sc['nodes']}({sc['reasoning_nodes']}rz) edges={sc['edges']} "
                  f"excerpt_cov={sc['node_excerpt_cov']}{refstr} ${cost:.4f} {sec:.0f}s{jstr}")
            agg["files"] += 1
            agg["json_valid"] += int(sc["json_valid"])
            agg["nodes"] += sc["nodes"]; agg["reasoning_nodes"] += sc["reasoning_nodes"]
            if sc["reasoning_nodes"]:
                agg["node_excerpt_cov"].append(sc["node_excerpt_cov"])
            agg["cost"] += cost; agg["sec"] += sec
        cov = round(sum(agg["node_excerpt_cov"]) / len(agg["node_excerpt_cov"]), 2) if agg["node_excerpt_cov"] else 0
        row = {"candidate": cand, "files": agg["files"],
               "json_valid_rate": round(agg["json_valid"] / max(agg["files"], 1), 2),
               "total_nodes": agg["nodes"], "reasoning_nodes": agg["reasoning_nodes"],
               "avg_excerpt_cov": cov, "total_cost": round(agg["cost"], 4),
               "avg_sec": round(agg["sec"] / max(agg["files"], 1), 1),
               "avg_judge": round(sum(j for j in agg.get("judge", []) if j is not None) /
                                  max(len([j for j in agg.get("judge", []) if j is not None]), 1), 1)
                            if agg.get("judge") else None}
        summary.append(row)
        print(f"  >> {json.dumps(row)}")

    (EVAL_OUT / "bakeoff-summary.json").write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY (written to eval-work/bakeoff-summary.json) ===")
    for r in summary:
        print(json.dumps(r))


if __name__ == "__main__":
    main()
