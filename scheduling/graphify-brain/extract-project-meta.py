#!/usr/bin/env python3
"""Extract _project.md meta files into project nodes."""
import json
import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_BRAIN_ENV = os.environ.get("CORE_BRAIN")
if not _BRAIN_ENV:
    print(f"{Path(__file__).name}: $CORE_BRAIN not set — required.", file=sys.stderr)
    sys.exit(1)
BRAIN = Path(_BRAIN_ENV)
# Per spec-graphify-out-relocation-2026-05-16.md — pipeline outputs in brain repo.
_CHECKPOINTS = BRAIN / "_build" / "output" / "checkpoints"
_CHECKPOINTS.mkdir(parents=True, exist_ok=True)
OUT_PATH = _CHECKPOINTS / "chunk-project-meta.json"


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).split("\n"):
        m2 = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$", line)
        if m2:
            k, v = m2.group(1), m2.group(2).strip().strip('"')
            if re.match(r"^-?\d+$", v):
                v = int(v)
            out[k] = v
    return out


import re as _re
def main():
    files = sorted(BRAIN.glob("projects/*/_project.md"))
    nodes = []
    edges = []
    for fp in files:
        fm = parse_frontmatter(fp.read_text(errors="replace"))
        proj_dir = fp.parent
        proj_id = fm.get("project") or proj_dir.name
        node_id = proj_id.replace("-", "_").replace(" ", "_")
        # If a project id collides with the username (path-based artifact),
        # normalize to a generic "project_home" so the graph isn't keyed on
        # the user's name.
        if node_id == os.environ.get("USER", "").replace("-", "_"):
            node_id = "project_home"
        nodes.append({
            "id": node_id,
            "label": f"Project: {proj_id}",
            "type": "project",
            "properties": {
                "name": proj_id,
                "sessions": fm.get("sessions"),
                "total_messages": fm.get("total_messages"),
                "total_tool_calls": fm.get("total_tool_calls"),
                "last_active": fm.get("last_active"),
                "source": str(fp.relative_to(BRAIN)),
            },
        })
        # Edges: project -> each session/subagent in that project's subdirs
        for child in proj_dir.glob("sessions/*.md"):
            m = _re.match(r"^\d{4}-\d{2}-\d{2}_[^_]+_([a-f0-9]+)\.md$", child.name)
            if m:
                edges.append({"source": node_id, "target": f"session_{m.group(1)[:8]}",
                              "relation": "contains", "properties": {}})
        for child in proj_dir.glob("subagents/*.md"):
            m = _re.match(r"^\d{4}-\d{2}-\d{2}_agent-(a[a-f0-9]+)\.md$", child.name)
            if m:
                edges.append({"source": node_id, "target": f"subagent_{m.group(1)[:10]}",
                              "relation": "contains", "properties": {}})

    chunk = {
        "metadata": {
            "chunk_id": "chunk-project-meta",
            "source_dir": "projects/*/_project.md",
            "total_files": len(files),
            "nodes_extracted": len(nodes),
            "extracted_by": "Core (Opus 4.7, Python script)",
            "extraction_date": "2026-05-03",
        },
        "nodes": nodes,
        "edges": edges,
    }
    OUT_PATH.write_text(json.dumps(chunk, indent=2))
    print(f"Wrote {OUT_PATH}: {len(nodes)} project meta nodes")


if __name__ == "__main__":
    sys.exit(main() or 0)
