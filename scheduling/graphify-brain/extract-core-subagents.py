#!/usr/bin/env python3
"""Extract one node + edges per Core subagent file from frontmatter.

Reads every file under `$CORE_BRAIN/projects/core/subagents/``$CORE_BRAIN/projects/<project-slug>/subagents/` (resolved via
env var; fallback is `~/AI Projects/<your-brain>/projects/<project-slug>/subagents/`),
parses YAML frontmatter, emits a chunk JSON with subagent nodes and edges
to topics/entities/tools/parent-sessions.
"""
import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
import os
_BRAIN_ENV = os.environ.get("CORE_BRAIN")
if not _BRAIN_ENV:
    print(f"{Path(__file__).name}: $CORE_BRAIN not set — required.", file=sys.stderr)
    sys.exit(1)
_BRAIN = Path(_BRAIN_ENV)
# Per-Core scoping (federated-brain-plan-2026-07-07 Phase 1): mirror of the
# SESSIONS_DIRS fix in extract-core-sessions.py. Each Core extracts its OWN
# subagents dir, slug resolved from CORE_ORG_ID via the tenants table.
def _my_org_slug():
    sys.path.insert(0, str(_REPO / "scheduling" / "brain-pg"))
    from _env import get_org_id  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
    my_org = get_org_id()
    # ops (5) was missing here while the SAME map three files over already carried it —
    # extract-pending.sh:89 says why it was added: "was absent -> DB-down fallback misrouted
    # ops dirs to org 1". The fix landed in one file and two siblings in this directory kept
    # the stale copy. One registry, four copies, corrected one at a time as each bit.
    # core-business caught these minutes before a baseline push would have shipped them.
    tenant = {1: "life", 2: "business", 3: "school", 4: "finance", 5: "ops"}
    try:
        import psycopg2
        # COREBRAIN_DB resolver (2026-08-31 fix) — was hardcoded "corebrain" instead of
        # reusing _env.connect_corebrain()'s resolution rule; see _env.py connect_corebrain_admin().
        c = psycopg2.connect(dbname=os.environ.get("COREBRAIN_DB", "corebrain"),
                             user=os.environ.get("CORE_BRAIN_DB_USER", "brain_app"))
        cur = c.cursor(); cur.execute("SELECT org_id, name FROM tenants")
        rows = {oid: name for (oid, name) in cur.fetchall()}
        c.close()
        if rows:
            tenant = rows
    except Exception:
        pass
    return tenant.get(my_org, "life")

_SLUG = _my_org_slug()
# The calling Core's own subagents dir. Life also owns legacy projects/core/.
SUBAGENTS_DIRS = [_BRAIN / f"projects/{_SLUG}/subagents"]
if _SLUG == "life":
    SUBAGENTS_DIRS.insert(0, _BRAIN / "projects/core/subagents")
SUBAGENTS_DIR = SUBAGENTS_DIRS[0]  # back-compat for any external reference
# Per spec-graphify-out-relocation-2026-05-16.md — pipeline outputs in brain repo.
_CHECKPOINTS = _BRAIN / "_build" / "output" / "checkpoints"
_CHECKPOINTS.mkdir(parents=True, exist_ok=True)
# Org-suffixed (Q6 clobber guard); merge.py globs all *.json so per-slug all merge.
OUT_PATH = _CHECKPOINTS / f"chunk-core-subagents-{_SLUG}.json"

# Entity name -> normalized ID. Loaded from $CORE_INSTANCE/memory/brain/entity-normalize.json
# at runtime. Engine ships clean; instance owns the mapping (per
# cascade-fix-2026-05-16 follow-up — was previously inline in engine code).
# If the JSON is missing or unreadable, ENTITY_NORMALIZE is empty and the
# extraction falls back to raw lowercased mention strings (no canonical merging).
def _load_entity_normalize():
    import os, json
    instance = os.environ.get("CORE_INSTANCE")
    if not instance:
        return {}
    cfg = Path(instance) / "memory" / "brain" / "entity-normalize.json"
    if not cfg.exists():
        return {}
    try:
        data = json.loads(cfg.read_text())
        return data.get("entities", {})
    except Exception:
        return {}

ENTITY_NORMALIZE = _load_entity_normalize()



def normalize_token(s):
    """Normalize an arbitrary token (entity, topic, tool name) to an ID."""
    if not s:
        return None
    key = str(s).strip().lower()
    if key in ENTITY_NORMALIZE:
        return ENTITY_NORMALIZE[key]
    # generic normalization: lower, spaces/dashes/dots/slashes -> _
    norm = re.sub(r"[\s\-./]+", "_", key)
    norm = re.sub(r"[^a-z0-9_]", "", norm)
    return norm or None


def parse_frontmatter(text):
    """Parse YAML frontmatter from a markdown file. Returns dict or None."""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    yaml_text = m.group(1)
    return parse_simple_yaml(yaml_text)


def parse_simple_yaml(text):
    """Parse the subset of YAML used in brain frontmatter.
    Handles: scalar, inline list [a, b], block list (- item)."""
    out = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()
        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            if not inner:
                out[key] = []
            else:
                items = [p.strip().strip('"').strip("'") for p in inner.split(",")]
                out[key] = [x for x in items if x]
            i += 1
        elif rest:
            # scalar (may be quoted)
            v = rest.strip('"').strip("'")
            # int conversion if possible
            if re.match(r"^-?\d+$", v):
                v = int(v)
            out[key] = v
            i += 1
        else:
            # block list follows OR empty
            block = []
            j = i + 1
            while j < len(lines) and re.match(r"^\s+-\s+", lines[j]):
                item = re.sub(r"^\s+-\s+", "", lines[j]).strip().strip('"').strip("'")
                if item:
                    block.append(item)
                j += 1
            if block:
                out[key] = block
                i = j
            else:
                out[key] = None
                i += 1
    return out


def extract_one(filepath):
    """Extract one node + its edges from a single subagent file."""
    fname = filepath.name
    # Agent hash from filename: YYYY-MM-DD_agent-aXXXXXXX.md
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_agent-(a[a-f0-9]+)\.md$", fname)
    if not m:
        return None, [], f"unparseable filename: {fname}"
    file_date, agent_hash = m.group(1), m.group(2)
    short_hash = agent_hash[:10]
    node_id = f"subagent_{short_hash}"

    text = filepath.read_text(errors="replace")
    fm = parse_frontmatter(text)
    if not fm:
        return None, [], f"no frontmatter: {fname}"

    # Label: prefer summary, fall back to first_message truncated
    summary = fm.get("summary") or ""
    first_msg = fm.get("first_message") or ""
    label_src = summary if summary else first_msg
    if isinstance(label_src, list):
        label_src = " ".join(str(x) for x in label_src)
    label = f"{file_date} — {str(label_src)[:60]}"

    properties = {
        "file": fname,
        "date": file_date,
        "session_id": fm.get("session_id"),
        "msgs": fm.get("messages"),
        "tool_calls": fm.get("tool_calls"),
        "summary": (str(summary) or str(first_msg))[:200],
    }
    properties = {k: v for k, v in properties.items() if v is not None}

    node = {
        "id": node_id,
        "label": label,
        "type": "subagent_session",
        "properties": properties,
    }

    edges = []

    # parent session
    sid = fm.get("session_id")
    if sid:
        edges.append({
            "source": node_id,
            "target": f"session_{str(sid)[:8]}",
            "relation": "spawned_by",
            "properties": {},
        })

    # topics
    for t in fm.get("topics") or []:
        nt = normalize_token(t)
        if nt:
            edges.append({
                "source": node_id,
                "target": f"topic_{nt}",
                "relation": "discusses",
                "properties": {},
            })

    # entities
    for e in fm.get("entities") or []:
        ne = normalize_token(e)
        if ne:
            edges.append({
                "source": node_id,
                "target": ne,
                "relation": "involves",
                "properties": {},
            })

    # tools
    for t in fm.get("tools") or []:
        nt = normalize_token(t)
        if nt:
            target = nt if nt.startswith("tool_") else f"tool_{nt}"
            edges.append({
                "source": node_id,
                "target": target,
                "relation": "uses",
                "properties": {},
            })

    return node, edges, None


def main():
    files = sorted(f for d in SUBAGENTS_DIRS if d.exists() for f in d.glob("*.md"))
    print(f"Found {len(files)} subagent files across {[str(d) for d in SUBAGENTS_DIRS]}")

    nodes = []
    all_edges = []
    skipped = []
    for fp in files:
        node, edges, err = extract_one(fp)
        if err:
            skipped.append((fp.name, err))
            continue
        nodes.append(node)
        all_edges.extend(edges)

    chunk = {
        "metadata": {
            "chunk_id": "chunk-core-subagents",
            "source_dir": "projects/core/subagents/",
            "total_files": len(files),
            "nodes_extracted": len(nodes),
            "edges_extracted": len(all_edges),
            "files_skipped": len(skipped),
            "approach": "Frontmatter-only extraction via Python script (one node per subagent file, edges to topics/entities/tools/parent_session)",
            "extracted_by": "Core (Opus 4.7, Python script)",
            "extraction_date": "2026-05-03",
        },
        "nodes": nodes,
        "edges": all_edges,
    }
    OUT_PATH.write_text(json.dumps(chunk, indent=2))
    print(f"\nWrote {OUT_PATH}")
    print(f"  Nodes: {len(nodes)}")
    print(f"  Edges: {len(all_edges)}")
    print(f"  Skipped: {len(skipped)}")
    if skipped:
        for fn, err in skipped[:10]:
            print(f"    {fn}: {err}")


if __name__ == "__main__":
    sys.exit(main() or 0)
