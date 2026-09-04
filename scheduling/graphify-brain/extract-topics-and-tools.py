#!/usr/bin/env python3
"""Extract topic and tool nodes from brain vault `topics/` and `tools/` dirs.

Each file has:
  ---
  type: topic|tool
  name: <name>
  sessions: <count>
  ---
  # <name>
  **<count> sessions**
  - <path> — <description>
  - <path> — <description>
  ...

We emit:
  - one node per topic/tool with name, session count, and a synthesized
    description from the most recent bullets
  - edges to the sessions/subagents listed in the body bullets

Output:
  chunk-topics-full.json    (replaces chunk-topics.json on next merge if both present;
                              we keep both, dedup is by node id which differs slightly)
  chunk-tools-full.json     (replaces chunk-tools.json similarly)
"""
import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
import os
from humanize import humanize_slug
_BRAIN_ENV = os.environ.get("CORE_BRAIN")
if not _BRAIN_ENV:
    print(f"{Path(__file__).name}: $CORE_BRAIN not set — required.", file=sys.stderr)
    sys.exit(1)
BRAIN = Path(_BRAIN_ENV)
TOPICS_DIR = BRAIN / "topics"
TOOLS_DIR = BRAIN / "tools"
# Per spec-graphify-out-relocation-2026-05-16.md — pipeline outputs in brain repo.
OUT_DIR = BRAIN / "_build" / "output" / "checkpoints"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def normalize_token(s):
    if not s:
        return None
    norm = re.sub(r"[\s\-./]+", "_", str(s).strip().lower())
    norm = re.sub(r"[^a-z0-9_]", "", norm)
    return norm or None


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}, text
    fm_text = m.group(1)
    body = text[m.end():].lstrip("\n")
    fm = {}
    for line in fm_text.split("\n"):
        m2 = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$", line)
        if m2:
            k, v = m2.group(1), m2.group(2).strip().strip('"').strip("'")
            if re.match(r"^-?\d+$", v):
                v = int(v)
            fm[k] = v
    return fm, body


def path_to_node_id(path_str):
    """Convert a path string like 'projects/core/subagents/2026-04-24_agent-XXXXX.md'
    to a node ID like 'subagent_XXXXXXXXXX' (first 10 chars of agent hash)
    or 'session_XXXXXXXX' (first 8 chars of session id segment).
    """
    # Strip any leading prefixes
    p = path_str.strip()
    # remove leading /Users/.../core(-nick)?[ -]brain/ if present (matches legacy/renamed brain vault path prefixes)
    p = re.sub(r"^.*?core(?:-nick)?[ -]brain/", "", p)
    # remove leading slash
    p = p.lstrip("/")
    # extract just the basename
    fname = Path(p).name

    # subagent: YYYY-MM-DD_agent-aXXXXXXXXX.md
    m = re.match(r"^\d{4}-\d{2}-\d{2}_agent-(a[a-f0-9]+)\.md$", fname)
    if m:
        return f"subagent_{m.group(1)[:10]}"

    # session: YYYY-MM-DD_<project>_<short_sid>.md
    m = re.match(r"^\d{4}-\d{2}-\d{2}_[^_]+_([a-f0-9]+)\.md$", fname)
    if m:
        return f"session_{m.group(1)[:8]}"

    return None


def extract_one(filepath, prefix):
    text = filepath.read_text(errors="replace")
    fm, body = parse_frontmatter(text)
    name = fm.get("name") or filepath.stem
    sessions_count = fm.get("sessions") or 0

    norm_name = normalize_token(name)
    node_id = f"{prefix}_{norm_name}"

    # Pull bullet lines: "- <path> — <description>" (em-dash or hyphen)
    bullets = []
    for line in body.split("\n"):
        m = re.match(r"^\s*-\s+(.+?)\s+[—–-]\s+(.+)$", line)
        if m:
            bullets.append((m.group(1).strip(), m.group(2).strip()))

    # Description: top 3 bullet summaries joined
    description_lines = [desc for _, desc in bullets[:3]]
    description = " | ".join(description_lines) if description_lines else f"{name} ({sessions_count} sessions)"

    node = {
        "id": node_id,
        # `name` here is the raw slug (frontmatter `name:` / filename stem —
        # written by consolidate.py's write_hub, itself an LLM-extracted topic
        # or tool string like "phase-11-r14-knowledge-graph-extraction").
        # Humanize the DISPLAY label only; node_id (the dedupe/edge key)
        # stays the raw slug untouched. See humanize.py for the transform.
        "label": humanize_slug(name),
        "type": prefix,  # "topic" or "tool"
        "properties": {
            "name": name,
            "sessions": sessions_count,
            "bullet_count": len(bullets),
            "description": description[:400],
        },
    }

    edges = []
    seen_targets = set()
    for path_str, desc in bullets:
        tgt = path_to_node_id(path_str)
        if not tgt or tgt in seen_targets:
            continue
        seen_targets.add(tgt)
        relation = "covered_in" if prefix == "topic" else "used_in"
        edges.append({
            "source": node_id,
            "target": tgt,
            "relation": relation,
            "properties": {"summary": desc[:200]},
        })

    return node, edges


def extract_dir(directory, prefix, out_filename):
    files = sorted(directory.glob("*.md"))
    print(f"Found {len(files)} files in {directory}")
    nodes = []
    edges = []
    for fp in files:
        try:
            node, e = extract_one(fp, prefix)
        except Exception as exc:
            print(f"  ! skip {fp.name}: {exc}")
            continue
        nodes.append(node)
        edges.extend(e)
    chunk = {
        "metadata": {
            "chunk_id": out_filename.replace(".json", ""),
            "source_dir": str(directory.relative_to(BRAIN)),
            "total_files": len(files),
            "nodes_extracted": len(nodes),
            "edges_extracted": len(edges),
            "approach": f"Full {prefix} extraction: one node per file with body-bullet edges to sessions/subagents",
            "extracted_by": "Core (Opus 4.7, Python script)",
            "extraction_date": "2026-05-03",
        },
        "nodes": nodes,
        "edges": edges,
    }
    out_path = OUT_DIR / out_filename
    out_path.write_text(json.dumps(chunk, indent=2))
    print(f"Wrote {out_path}")
    print(f"  Nodes: {len(nodes)}")
    print(f"  Edges: {len(edges)}")
    return len(nodes), len(edges)


def main():
    extract_dir(TOPICS_DIR, "topic", "chunk-topics-full.json")
    print()
    extract_dir(TOOLS_DIR, "tool", "chunk-tools-full.json")


if __name__ == "__main__":
    sys.exit(main() or 0)
