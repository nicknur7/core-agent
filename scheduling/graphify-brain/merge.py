#!/usr/bin/env python3
"""Merge brain-graphify checkpoint chunks into a unified graph.

Normalizes ID inconsistencies across chunks:
- core_system  -> core
- gmail_skill  -> gmail
- adds missing entity nodes for untrusted_reader and brain_vault
- hyphen/space  -> underscore  (brain-lint -> brain_lint)
- course slugs  -> canonical underscore form  (mgmt453 -> mgmt_453, ba283 -> ba_283).
  THIS DEPLOYMENT'S TAXONOMY, NOT A GENERAL RULE — the course prefixes below (mgmt, ba, dsgn) are
  this operator's own school Core; a fork with a different school (or none) should replace or
  drop Step 2 below.
- entity_X      -> X or topic_X when a plain/topic sibling already exists
- project_X     -> X or topic_X when a plain/topic sibling already exists

Outputs:
  graphify-out/graph.json   unified nodes+edges
  graphify-out/graph.html   D3 force-layout viz
  graphify-out/audit.json   dangling-edge / dup-node report
"""
import json
import os
import re
import sys
from collections import defaultdict, Counter
from pathlib import Path

from humanize import humanize_slug

ROOT = Path(__file__).parent
# Pipeline outputs live in the brain repo, not engine — engine ships code only.
# Per spec-graphify-out-relocation-2026-05-16.md (executed 2026-05-17).
_BRAIN = os.environ.get("CORE_BRAIN")
if not _BRAIN:
    print(f"{Path(__file__).name}: $CORE_BRAIN not set — required for pipeline output paths.", file=sys.stderr)
    sys.exit(1)
_PIPELINE_OUT = Path(_BRAIN) / "_build" / "output"
CHECKPOINTS = _PIPELINE_OUT / "checkpoints"
OUT = _PIPELINE_OUT / "graphify-out"
CHECKPOINTS.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

# ID normalization map (applied to BOTH node ids and edge source/target refs).
# Static renames: applied first, before any regex rules.  # privacy-ok: generic engineering vocabulary
# Generic engine-side renames are inline below; instance-specific aliases
# (entity_marley → marley, etc.) are loaded from
# $CORE_INSTANCE/memory/brain/entity-normalize.json `merge_aliases` key.
# Per cascade-fix-2026-05-16: keeps engine free of instance-specific entity data.
def _load_merge_aliases():
    instance = os.environ.get("CORE_INSTANCE")
    if not instance:
        return {}
    cfg = Path(instance) / "memory" / "brain" / "entity-normalize.json"
    if not cfg.exists():
        return {}
    try:
        data = json.loads(cfg.read_text())
        return data.get("merge_aliases", {})
    except Exception:
        return {}


def _load_topic_denylist() -> set[str]:
    """Topic slugs that should be filtered from graph.json even when sessions
    reference them. Per-instance config at `.claude/identity.json`
    `topic_denylist` (list of slugs). Empty if config missing.

    The graphify pipeline reconstructs topic nodes from session-content
    references, so deleting `topics/<slug>.md` + the Postgres entities row
    is not enough — the node returns on the next pipeline pass. The denylist
    is the structural fix: a slug listed here is filtered AT GRAPH ASSEMBLY
    time, before write to graph.json. Both `topic_<slug>` ids and any
    raw <slug> id form are caught.

    Seeded 2026-05-18 with the 4 ghost hubs from Spec 4 (post-deletion they
    were still resurfacing as graph nodes).
    """
    instance = os.environ.get("CORE_INSTANCE")
    if not instance:
        return set()
    cfg = Path(instance) / ".claude" / "identity.json"
    try:
        data = json.loads(cfg.read_text())
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
        return set()
    raw_cfg = data.get("topic_denylist", {})
    # Accept either {"slugs": [...]} (preferred, allows sibling _comment) or
    # a plain list (legacy form).
    if isinstance(raw_cfg, dict):
        raw = raw_cfg.get("slugs", [])
    elif isinstance(raw_cfg, list):
        raw = raw_cfg
    else:
        return set()
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for slug in raw:
        if not isinstance(slug, str) or not slug.strip():
            continue
        s = slug.strip().lower()
        # Accept both forms: the plain slug AND the topic_<underscored> id form.
        out.add(s)
        out.add(f"topic_{s.replace('-', '_')}")
    return out


TOPIC_DENYLIST = _load_topic_denylist()

ID_RENAMES = {
    "core_system": "core",
    "gmail_skill": "gmail",
    # chunk-core used "entity_*" prefix for nodes that already exist in chunk-entities.
    # Instance-specific aliases merged from entity-normalize.json:
    **_load_merge_aliases(),
}

# Entity nodes referenced but never defined in any chunk - add them here
MISSING_NODES = [
    {
        "id": "untrusted_reader",
        "label": "Untrusted Reader",
        "type": "system",
        "properties": {
            "description": "Phase 2 Docker container that isolates untrusted incoming content (emails, web pages) from the main model context. Built Apr 24 2026 to defend against prompt injection. Runtime managed by Colima auto-lifecycle.",
            "location": "docker/untrusted-reader/",
            "phase": "Phase 2 security"
        }
    },
    {
        "id": "brain_vault",
        "label": "Brain Vault",
        "type": "system",
        "properties": {
            "description": "Obsidian vault holding session/subagent/topic/entity/tool markdown files extracted from Core's session JSONLs. Source corpus for the brain knowledge graph. Vault location resolved via $CORE_BRAIN env var.",
            "location": os.environ.get("CORE_BRAIN", "<CORE_BRAIN not set>")
        }
    },
]

# Files in checkpoints/ that should be skipped (pilot, drafts, superseded)
SKIP_FILES = {
    "pilot-chunk-apr18-small.json",
    "chunk-topics.json",   # superseded by chunk-topics-full.json (35 -> 461 nodes)
    "chunk-tools.json",    # superseded by chunk-tools-full.json (52 -> 64 nodes)
}


def normalize_id(node_id):
    """Phase 1 normalization: static renames + regex rules safe to apply without
    knowing the full node set (no context needed).

    Phase 2 (context-aware) renames — entity_X/project_X deduplication — are
    applied in a second pass inside main() after all nodes are collected.
    """
    # Step 0: static renames (highest priority, applied first)
    if node_id in ID_RENAMES:
        return ID_RENAMES[node_id]

    result = node_id

    # Step 1: hyphen/space -> underscore
    # Catches: brain-lint, recall-similar, read-before-ask, canvas-automation variants
    result = re.sub(r'[-\s]+', '_', result)

    # Step 2: course-slug underscore normalization
    # mgmt<NNN> (no underscore) -> mgmt_<NNN>
    # Applies inside longer IDs too: entity_mgmt453 -> entity_mgmt_453
    # Safe: mgmt_453 pattern already has underscore so (\d{3}) won't fire again
    result = re.sub(r'mgmt(\d{3})', r'mgmt_\1', result)

    # ba_dsgn<NNN> -> ba_dsgn_<NNN>  (must run BEFORE the plain ba rule)
    result = re.sub(r'ba_dsgn(\d{3})', r'ba_dsgn_\1', result)

    # ba<NNN> -> ba_<NNN>  (negative lookbehind: don't fire on already-expanded ba_dsgn_)
    result = re.sub(r'(?<!dsgn_)ba(\d{3})', r'ba_\1', result)

    return result


def build_context_renames(nodes_by_id):
    """Phase 2 normalization: build a rename map for entity_X / project_X nodes
    that have a duplicate plain (X) or topic-prefixed (topic_X) sibling in the
    graph.  Prefer plain > topic > entity/project in that priority order.

    Called after all nodes are collected so the full ID set is known.
    Returns a dict {old_id: canonical_id} for IDs that should be merged.
    """
    all_ids = set(nodes_by_id.keys())
    renames = {}

    for nid in sorted(all_ids):
        if nid in renames:
            continue  # already remapped

        if nid.startswith('entity_'):
            base = nid[len('entity_'):]
            if base in all_ids:
                renames[nid] = base          # entity_X -> X  (plain wins)
            elif ('topic_' + base) in all_ids:
                renames[nid] = 'topic_' + base  # entity_X -> topic_X

        elif nid.startswith('project_'):
            base = nid[len('project_'):]
            if base in all_ids:
                renames[nid] = base
            elif ('topic_' + base) in all_ids:
                renames[nid] = 'topic_' + base

    return renames


def apply_context_renames(nodes_by_id, node_source_chunks, renames):
    """Merge nodes that were mapped by build_context_renames into their canonical IDs.
    Properties are merged (canonical wins on conflict); source_chunks are unioned.
    Returns updated nodes_by_id and node_source_chunks.
    """
    if not renames:
        return nodes_by_id, node_source_chunks

    removed = 0
    for old_id, canon_id in renames.items():
        if old_id not in nodes_by_id:
            continue  # already removed (shouldn't happen, but guard)
        old_node = nodes_by_id.pop(old_id)
        removed += 1
        if canon_id in nodes_by_id:
            # Merge: canonical node absorbs old node's properties
            existing = nodes_by_id[canon_id]
            for k, v in old_node.get('properties', {}).items():
                if k not in existing['properties']:
                    existing['properties'][k] = v
                elif k == '_source_chunks':
                    existing['properties'][k] = list(set(
                        existing['properties'][k] + v
                    ))
            if not existing.get('label') and old_node.get('label'):
                existing['label'] = old_node['label']
        else:
            # Canonical doesn't exist yet — rename in place
            old_node['id'] = canon_id
            nodes_by_id[canon_id] = old_node
            removed -= 1  # net 0: rename not removal

        # Update source-chunk ledger
        old_chunks = node_source_chunks.pop(old_id, [])
        node_source_chunks[canon_id].extend(old_chunks)

    return nodes_by_id, node_source_chunks


def main():
    chunk_files = sorted(
        f for f in os.listdir(CHECKPOINTS)
        if f.endswith(".json") and f not in SKIP_FILES
    )
    # Control-char / corruption resilience (2026-06-09): a single malformed
    # Haiku checkpoint must NOT crash the whole merge. It did — a raw control
    # char (literal \n/\t inside a JSON string) raised JSONDecodeError and blocked
    # both life + school. Validate up front with strict=False (tolerates control
    # chars); quarantine anything that STILL won't parse so the merge proceeds.
    _QUAR = CHECKPOINTS / "_quarantine"
    _good = []
    for _fname in chunk_files:
        try:
            with open(CHECKPOINTS / _fname) as _vf:
                json.load(_vf, strict=False)
            _good.append(_fname)
        except Exception as _e:
            try:
                _QUAR.mkdir(exist_ok=True)
                (CHECKPOINTS / _fname).rename(_QUAR / _fname)
            except OSError:
                pass
            print(f"  ! quarantined unparseable checkpoint {_fname}: {_e}")
    chunk_files = _good
    print(f"Merging {len(chunk_files)} chunks: {chunk_files}")

    nodes_by_id = {}            # id -> node (last write wins on dup, with merge of properties)
    node_source_chunks = defaultdict(list)  # id -> [chunk_filename]
    chunk_to_src = {}           # chunk_filename -> metadata.source_file (relative vault path)
    edges = []                  # list of (source, target, relation, properties, chunk)
    malformed_edges = []        # (chunk, repr) for edges missing source/target — see pass 2

    # Pass 1: collect all nodes from all chunks (filtering denylisted topics)
    denylist_filtered = 0
    for fname in chunk_files:
        path = CHECKPOINTS / fname
        with open(path) as f:
            chunk = json.load(f, strict=False)
        # Record this chunk's originating vault path so the org_id of each node
        # can be derived downstream (embed.py) via path_to_org_id. Multi-Core
        # ownership (Phase 1B): a node extracted from projects/business/... must
        # be tagged org2, not the org of whichever Core ran the heavy build.
        chunk_to_src[fname] = (chunk.get("metadata", {}) or {}).get("source_file")
        for n in chunk.get("nodes", []):
            nid = normalize_id(n["id"])
            # Drop denylisted topic nodes entirely. Edges referencing them
            # become dangling and are dropped in Pass 2 (existing reconcile-
            # isolated step handles this).
            if nid.lower() in TOPIC_DENYLIST:
                denylist_filtered += 1
                continue
            n_copy = dict(n)
            n_copy["id"] = nid
            n_copy.setdefault("properties", {})
            n_copy["properties"]["_source_chunks"] = list(set(
                n_copy["properties"].get("_source_chunks", []) + [fname]
            ))
            if nid in nodes_by_id:
                # merge properties
                existing = nodes_by_id[nid]
                existing["properties"].update(n_copy["properties"])
                # keep first non-empty label/type
                if not existing.get("label") and n_copy.get("label"):
                    existing["label"] = n_copy["label"]
            else:
                nodes_by_id[nid] = n_copy
            node_source_chunks[nid].append(fname)

    # Add the synthetic missing-entity nodes
    for n in MISSING_NODES:
        nid = n["id"]
        if nid in nodes_by_id:
            print(f"  ! synthetic node {nid} already present in chunks - skipping seed")
            continue
        n_copy = dict(n)
        n_copy.setdefault("properties", {})
        n_copy["properties"]["_source_chunks"] = ["__merge_seed__"]
        nodes_by_id[nid] = n_copy

    if denylist_filtered > 0:
        print(f"  Topic denylist filtered: {denylist_filtered} node refs "
              f"(slugs: {sorted(s for s in TOPIC_DENYLIST if not s.startswith('topic_'))})")

    # Pass 1.5: context-aware dedup (entity_X / project_X -> canonical sibling)
    ctx_renames = build_context_renames(nodes_by_id)
    nodes_by_id, node_source_chunks = apply_context_renames(
        nodes_by_id, node_source_chunks, ctx_renames
    )
    print(f"  Context-aware renames applied: {len(ctx_renames)} "
          f"(entity_/project_ dedup, {len(nodes_by_id)} nodes remain)")

    def full_normalize(nid):
        """Apply phase-1 normalize_id, then context renames."""
        nid = normalize_id(nid)
        return ctx_renames.get(nid, nid)

    # Pass 2: collect edges, normalize refs, drop self-loops
    for fname in chunk_files:
        path = CHECKPOINTS / fname
        with open(path) as f:
            chunk = json.load(f, strict=False)
        for e in chunk.get("edges", []):
            # Malformed edge → SKIP, don't crash. Every chunk here is LLM-authored,
            # and the extraction pipeline is built around that being imperfect
            # (readback-parse, repair-retry, Sonnet fallback). Merge had no such
            # tolerance: on 2026-07-30 a single Haiku edge that emitted "id" where
            # the schema says "source" raised KeyError and took down a merge over
            # 2,826 chunks — months of accumulated extraction, stopped by one edge
            # in one file from that morning. A parse-clean checkpoint is not a
            # schema-valid one, so the VERIFY-BEFORE-MERGE gate cannot catch this.
            # Counted and reported below, never silently swallowed: one is drift,
            # a hundred is a broken extraction prompt, and the count is what
            # distinguishes them.
            if not isinstance(e, dict) or "source" not in e or "target" not in e:
                malformed_edges.append((fname, repr(e)[:100]))
                continue
            src = full_normalize(e["source"])
            tgt = full_normalize(e["target"])
            if src == tgt:
                continue
            # Backfill missing confidence — chunks from earlier extractions
            # (pre v2.1 schema) sometimes shipped edges without properties.confidence,
            # leaving 14.3% of merged edges untagged. Default to EXTRACTED since
            # the chunk-extraction pipeline IS the only edge source — explicit
            # default is safer than leaving consumers to handle missing field.
            # Audit graph-integrity.md #7 (2026-05-13).
            props = dict(e.get("properties", {}) or {})
            if "confidence" not in props:
                props["confidence"] = "EXTRACTED"
                props["_confidence_backfilled"] = True
            edges.append({
                "source": src,
                "target": tgt,
                "relation": e.get("relation", "related"),
                "properties": props,
                "_chunk": fname,
            })

    if malformed_edges:
        print(f"  ! {len(malformed_edges)} malformed edge(s) skipped "
              f"(missing source/target) across "
              f"{len({c for c, _ in malformed_edges})} chunk(s):")
        for c, r in malformed_edges[:10]:
            print(f"      {c}: {r}")
        if len(malformed_edges) > 10:
            print(f"      ... and {len(malformed_edges) - 10} more")

    # Dedupe edges by (source, target, relation)
    seen = set()
    deduped = []
    dup_count = 0
    for e in edges:
        key = (e["source"], e["target"], e["relation"])
        if key in seen:
            dup_count += 1
            continue
        seen.add(key)
        deduped.append(e)

    # Auto-create stub nodes for dangling edge targets (edges -> nodes that
    # don't exist as defined nodes anywhere). Type inferred from id prefix.
    all_ids = set(nodes_by_id.keys())

    def stub_type_for(nid):
        if nid.startswith("topic_"):
            return "topic"
        if nid.startswith("session_"):
            return "session"
        if nid.startswith("subagent_"):
            return "subagent_session"
        if nid.startswith("tool_"):
            return "tool"
        if nid.startswith("entity_"):
            return "entity"
        return "inferred"

    dangling_targets = set()
    for e in deduped:
        for ep in (e["source"], e["target"]):
            if ep not in all_ids:
                dangling_targets.add(ep)

    # Honor the topic denylist a SECOND time at stub creation. Otherwise
    # filtered-from-Pass-1 topics get re-created here as stubs because
    # session edges still reference them.
    if TOPIC_DENYLIST:
        before = len(dangling_targets)
        dangling_targets -= TOPIC_DENYLIST
        also_skipped = before - len(dangling_targets)
        if also_skipped > 0:
            print(f"  Topic denylist filtered: {also_skipped} additional dangling-target stubs skipped")

    # Drop edges pointing at denylisted targets too, so the final graph is
    # truly denylist-clean (no edges to nonexistent nodes).
    if TOPIC_DENYLIST:
        before = len(deduped)
        deduped = [e for e in deduped if e["source"] not in TOPIC_DENYLIST and e["target"] not in TOPIC_DENYLIST]
        dropped = before - len(deduped)
        if dropped > 0:
            print(f"  Topic denylist filtered: {dropped} edges to denylisted nodes dropped")

    for nid in sorted(dangling_targets):
        # Stub nodes have no chunk-JSON-provided label (nothing ever defined
        # them), so we're synthesizing a display string straight from the raw
        # id here. Strip the type prefix, convert back to a hyphenated slug
        # shape, and run it through the same humanize_slug() used for
        # regular topic/tool labels so stub nodes don't stand out as raw
        # slugs in the graph viz. `nid` itself (the dedupe/edge key) is
        # untouched.
        _display_src = re.sub(r'^(topic|session)_', '', nid).replace('_', '-')
        nodes_by_id[nid] = {
            "id": nid,
            "label": humanize_slug(_display_src),
            "type": stub_type_for(nid),
            "properties": {
                "_source_chunks": ["__merge_stub__"],
                "_stub_reason": "referenced in edges but never defined as a node",
            },
        }
    print(f"  Auto-created {len(dangling_targets)} stub nodes for dangling targets")

    all_ids = set(nodes_by_id.keys())
    dangling = []  # all dangling now resolved by stub creation

    # Find nodes with no edges
    edge_endpoints = set()
    for e in deduped:
        edge_endpoints.add(e["source"])
        edge_endpoints.add(e["target"])
    isolated = sorted(all_ids - edge_endpoints)

    # Stats by type
    type_counts = Counter(n.get("type", "untyped") for n in nodes_by_id.values())
    relation_counts = Counter(e["relation"] for e in deduped)

    # Run Louvain community detection locally (graphify CLI's cluster-only errors
    # at >5000 nodes, and we want communities updated on every merge anyway).
    try:
        import networkx as nx
        import community as community_louvain  # python-louvain
        G = nx.Graph()
        for nid in nodes_by_id:
            G.add_node(nid)
        for e in deduped:
            if e["source"] in nodes_by_id and e["target"] in nodes_by_id:
                G.add_edge(e["source"], e["target"])
        partition = community_louvain.best_partition(G, random_state=42)
        for nid, n in nodes_by_id.items():
            n["community"] = partition.get(nid, -1)
        com_count = len(set(partition.values()))
        print(f"  Communities: {com_count} (Louvain, local)")
    except Exception as e:
        print(f"  (Louvain failed: {e}; nodes will have community=-1)")
        for n in nodes_by_id.values():
            n.setdefault("community", -1)

    # Stamp each node with its primary originating vault path (first source
    # chunk that resolves to a real path). embed.py turns this into a per-row
    # org_id via path_to_org_id — the multi-Core ownership fix (Phase 1B).
    # _source_chunks is maintained through the rename passes above, so it is the
    # reliable carrier here. Synthetic stubs (__merge_stub__/__merge_seed__) have
    # no real source and fall through to embed.py's caller-org default.
    for n in nodes_by_id.values():
        props = n.setdefault("properties", {})
        primary_src = None
        for fname in props.get("_source_chunks", []):
            src = chunk_to_src.get(fname)
            if src:
                primary_src = src
                break
        props["_primary_source_file"] = primary_src

    # Write unified graph.json
    graph = {
        "metadata": {
            "merged_from": chunk_files,
            "node_count": len(nodes_by_id),
            "edge_count": len(deduped),
            "dangling_count": len(dangling),
            "duplicate_edges_dropped": dup_count,
            "isolated_node_count": len(isolated),
            "id_renames_applied": {**ID_RENAMES, **ctx_renames},
            "id_renames_static_count": len(ID_RENAMES),
            "id_renames_context_count": len(ctx_renames),
            "synthetic_nodes_added": [n["id"] for n in MISSING_NODES],
        },
        "nodes": list(nodes_by_id.values()),
        "edges": deduped,
        "links": deduped,
    }
    with open(OUT / "graph.json", "w") as f:
        json.dump(graph, f, indent=2)
    print(f"\nWrote {OUT / 'graph.json'}")
    print(f"  Nodes: {len(nodes_by_id)}")
    print(f"  Edges: {len(deduped)} ({dup_count} duplicates dropped)")
    print(f"  Dangling: {len(dangling)}")
    print(f"  Isolated nodes: {len(isolated)}")

    # Write audit
    audit = {
        "dangling_edges": [
            {"source": e["source"], "target": e["target"],
             "relation": e["relation"], "from_chunk": e["_chunk"],
             "missing": [
                 ep for ep in [e["source"], e["target"]] if ep not in all_ids
             ]}
            for e in dangling
        ],
        "isolated_nodes": isolated,
        "duplicate_node_ids": {
            nid: chunks for nid, chunks in node_source_chunks.items()
            if len(chunks) > 1
        },
        "type_counts": dict(type_counts),
        "relation_counts_top20": dict(relation_counts.most_common(20)),
    }
    with open(OUT / "audit.json", "w") as f:
        json.dump(audit, f, indent=2)
    print(f"\nWrote {OUT / 'audit.json'}")
    print(f"\nNode types: {dict(type_counts)}")
    if dangling:
        print(f"\n  WARNING: {len(dangling)} dangling edges remain. See audit.json")
        for e in dangling[:5]:
            print(f"    {e['source']} -[{e['relation']}]-> {e['target']}  ({e['_chunk']})")
    if isolated:
        print(f"\n  Note: {len(isolated)} isolated nodes (no edges). First 10:")
        for nid in isolated[:10]:
            print(f"    {nid}")

    # Write HTML viz (D3 force layout, Obsidian-style)
    html_template = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Brain Graph</title>
<style>
  :root { --bg:#0d1117; --fg:#c9d1d9; --muted:#7d8590; --border:#30363d; --panel:rgba(13,17,23,.94); --accent:#58a6ff; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--fg); overflow:hidden; }
  #canvas { width:100vw; height:100vh; cursor:grab; }
  #canvas:active { cursor:grabbing; }
  .panel { position:fixed; background:var(--panel); border:1px solid var(--border); border-radius:8px; backdrop-filter:blur(8px); }
  #info { top:14px; left:14px; padding:14px 16px; max-width:420px; max-height:80vh; overflow-y:auto; font-size:13px; line-height:1.5; opacity:0; transform:translateY(-4px); transition:opacity .14s, transform .14s; pointer-events:none; }
  #info.show { opacity:1; transform:translateY(0); pointer-events:auto; }
  #info.pinned { border-color:var(--accent); }
  #info h3 { margin:0 0 4px 0; font-size:15px; color:#f0f6fc; word-break:break-word; }
  #info .id { color:var(--muted); font-size:11px; margin-bottom:8px; word-break:break-all; }
  #info .meta { display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap; }
  #info .badge { background:#21262d; padding:2px 8px; border-radius:10px; font-size:11px; color:#a6abb3; }
  #info .badge.com { background:#1f2a3a; color:#7fb3ff; }
  #info .props { color:#a6abb3; font-size:12px; }
  #info .props > div { margin:4px 0; padding:6px 8px; background:rgba(110,118,129,.05); border-left:2px solid var(--border); border-radius:0 4px 4px 0; }
  #info .props strong { color:#d2d6db; font-weight:600; }
  #info .props .val { color:#8b949e; word-break:break-word; white-space:pre-wrap; }
  #info .neighbors { margin-top:10px; padding-top:10px; border-top:1px solid var(--border); }
  #info .neighbors .title { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }
  #info .neighbors a { display:block; color:#8b949e; font-size:11px; padding:2px 0; cursor:pointer; text-decoration:none; }
  #info .neighbors a:hover { color:var(--accent); }
  #info .pin-hint { margin-top:8px; padding-top:8px; border-top:1px solid var(--border); font-size:10px; color:var(--muted); }
  #legend { top:14px; right:14px; padding:10px 14px; font-size:12px; max-height:80vh; overflow-y:auto; min-width:180px; }
  #legend .header { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; margin-bottom:6px; }
  #legend .mode-toggle { display:flex; gap:6px; margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); }
  #legend .mode-toggle button { flex:1; background:#161b22; border:1px solid var(--border); color:var(--fg); padding:4px 8px; border-radius:4px; cursor:pointer; font-size:11px; }
  #legend .mode-toggle button.active { background:#1f2a3a; border-color:var(--accent); color:var(--accent); }
  #legend .item { display:flex; align-items:center; gap:8px; margin:3px 0; cursor:pointer; user-select:none; padding:2px 4px; border-radius:3px; }
  #legend .item.muted { opacity:0.35; }
  #legend .item:hover { background:rgba(255,255,255,.04); }
  #legend .swatch { width:12px; height:12px; border-radius:50%; flex-shrink:0; }
  #legend .actions { font-size:10px; color:var(--accent); cursor:pointer; margin-top:6px; padding-top:6px; border-top:1px solid var(--border); display:flex; gap:12px; }
  #legend .actions span:hover { text-decoration:underline; }
  #legend .count { color:var(--muted); margin-left:auto; font-size:10px; }
  #stats { bottom:14px; left:14px; padding:8px 12px; font-size:11px; color:var(--muted); }
  #search { bottom:14px; right:14px; padding:6px 10px; }
  #search input { background:var(--bg); border:1px solid var(--border); color:var(--fg); padding:5px 10px; border-radius:4px; width:240px; outline:none; font-size:12px; }
  #search input:focus { border-color:var(--accent); }
  text.label { fill:var(--fg); font-size:11px; font-weight:500; pointer-events:none; paint-order:stroke; stroke:var(--bg); stroke-width:3px; stroke-linejoin:round; }
  .help { bottom:42px; right:14px; padding:6px 10px; font-size:10px; color:var(--muted); }
</style>
</head>
<body>
<div class="panel" id="info"></div>
<div class="panel" id="legend"></div>
<div class="panel" id="stats"></div>
<div class="panel" id="search"><input type="text" id="search-input" placeholder="Search (label or id)…"></div>
<div class="panel help">esc clears · click node to pin · drag to rearrange · scroll to zoom</div>
<svg id="canvas"></svg>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const DATA = __GRAPH_DATA__;

// Type fallback palette (used when "color by: type" mode is on)
const TYPE_COLORS = {
  person:"#f78166", company:"#58a6ff", project:"#3fb950", system:"#d29922",
  topic:"#bc8cff", tool:"#f85149", subagent_session:"#9da7b3", subagent_cluster:"#9da7b3",
  session:"#56d364", session_arc:"#56d364", ui_component:"#a371f7",
  external_service:"#79c0ff", external_project:"#79c0ff", event:"#ffa657",
  service:"#79c0ff", contact:"#f78166", entity:"#ffa657",
  Decision:"#3fb950", Rule:"#a371f7", Incident:"#f85149", Lesson:"#ffa657",
  Hypothesis:"#79c0ff", Tradeoff:"#d29922", code_location:"#7d8590",
};
const DEFAULT_COLOR = "#6e7681";

// HSL hash for community coloring (deterministic, distinct hues across 437+ communities).
function communityColor(c) {
  if (c == null || c < 0) return DEFAULT_COLOR;
  const h = (c * 137.508) % 360; // golden-angle distribution
  const s = 55 + (c * 7) % 25;
  const l = 58 + (c * 13) % 12;
  return `hsl(${h.toFixed(0)} ${s.toFixed(0)}% ${l.toFixed(0)}%)`;
}

// Build neighbor adjacency.
const adj = new Map();
DATA.edges.forEach(e => {
  const s = typeof e.source === "object" ? e.source.id : e.source;
  const t = typeof e.target === "object" ? e.target.id : e.target;
  if (!adj.has(s)) adj.set(s, new Set());
  if (!adj.has(t)) adj.set(t, new Set());
  adj.get(s).add(t);
  adj.get(t).add(s);
});

const idMap = new Map(DATA.nodes.map(n => [n.id, n]));
const width = window.innerWidth, height = window.innerHeight;
const svg = d3.select("#canvas").attr("viewBox", [0, 0, width, height]);
const g = svg.append("g");

// Wide zoom range — was 0.1-8, now 0.02-16.
const zoom = d3.zoom().scaleExtent([0.02, 16]).on("zoom", (e) => g.attr("transform", e.transform));
svg.call(zoom);

const sim = d3.forceSimulation(DATA.nodes)
  .force("link", d3.forceLink(DATA.edges).id(d => d.id).distance(50).strength(0.18))
  .force("charge", d3.forceManyBody().strength(-90).distanceMax(280).theta(1.2))
  .force("center", d3.forceCenter(width/2, height/2))
  .force("collide", d3.forceCollide().radius(d => nodeRadius(d) + 2))
  .alphaDecay(0.1)
  .velocityDecay(0.65)
  .stop(); // we control ticks manually below

function nodeRadius(d) {
  const sessions = (d.properties && (d.properties.sessions || d.properties.msgs)) || 1;
  const deg = (adj.get(d.id) || new Set()).size;
  return Math.min(20, 3 + Math.sqrt(sessions) + Math.sqrt(deg) * 0.6);
}

let colorMode = "community"; // or "type"
function nodeFill(d) {
  return colorMode === "community" ? communityColor(d.community) : (TYPE_COLORS[d.type] || DEFAULT_COLOR);
}

const link = g.append("g")
  .attr("stroke", "#30363d").attr("stroke-opacity", 0.45)
  .selectAll("line").data(DATA.edges).join("line").attr("stroke-width", 1);

const node = g.append("g").attr("class", "nodes")
  .selectAll("circle").data(DATA.nodes).join("circle")
  .attr("r", nodeRadius)
  .attr("fill", nodeFill)
  .attr("stroke", "#0d1117").attr("stroke-width", 1.2)
  .style("cursor", "pointer")
  .call(d3.drag()
    .on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));

// Labels: rendered ONLY for currently focused node + its neighbors. Massive perf win
// over rendering 5000+ <text> elements.
const labelLayer = g.append("g").attr("class", "labels");
let activeLabels = []; // [{id, x, y, text}]

function renderLabels() {
  const sel = labelLayer.selectAll("text").data(activeLabels, d => d.id);
  sel.exit().remove();
  sel.enter().append("text").attr("class", "label").attr("dx", 10).attr("dy", 4)
    .merge(sel)
    .text(d => d.text)
    .attr("x", d => d.x).attr("y", d => d.y);
}

// Pre-tick: run simulation synchronously for N iterations BEFORE first render.
// Avoids the "insanely unstable" thrashing at 5000+ nodes.
const PRE_TICKS = 320;
for (let i = 0; i < PRE_TICKS; i++) sim.tick();

function renderPositions() {
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  node.attr("cx", d => d.x).attr("cy", d => d.y);
  activeLabels.forEach(L => { const n = idMap.get(L.id); if (n) { L.x = n.x; L.y = n.y; } });
  if (activeLabels.length) renderLabels();
}
renderPositions();

// Live ticks ONLY while user is dragging. Otherwise sim is frozen.
sim.on("tick", renderPositions);

// Focus / pin state.
let focused = null;
let pinned = null;

function setFocus(d, opts = {}) {
  const targetId = d ? d.id : null;
  const neighbors = d ? (adj.get(d.id) || new Set()) : new Set();
  const visible = new Set();
  if (d) { visible.add(d.id); neighbors.forEach(n => visible.add(n)); }

  if (d) {
    node.attr("opacity", n => visible.has(n.id) ? 1 : 0.12)
        .attr("stroke", n => n.id === d.id ? "#f0f6fc" : "#0d1117")
        .attr("stroke-width", n => n.id === d.id ? 2.5 : 1.2);
    link.attr("opacity", e => (e.source.id === d.id || e.target.id === d.id) ? 0.85 : 0.06)
        .attr("stroke", e => (e.source.id === d.id || e.target.id === d.id) ? "#8b949e" : "#30363d");
    activeLabels = DATA.nodes.filter(n => visible.has(n.id))
      .map(n => ({ id:n.id, x:n.x||0, y:n.y||0, text:(n.label||n.id).slice(0, 60) }));
    renderLabels();
  } else {
    node.attr("opacity", 1).attr("stroke", "#0d1117").attr("stroke-width", 1.2);
    link.attr("opacity", 0.45).attr("stroke", "#30363d");
    activeLabels = []; renderLabels();
  }

  if (d) showInfo(d, opts.pinned);
  else hideInfo();
}

function hideInfo() {
  document.getElementById("info").classList.remove("show", "pinned");
}

function showInfo(d, isPinned) {
  const info = document.getElementById("info");
  const props = d.properties || {};
  const com = (d.community != null) ? d.community : "—";
  const type = d.type || "untyped";
  const neighbors = [...(adj.get(d.id) || new Set())].slice(0, 12).map(nid => idMap.get(nid)).filter(Boolean);

  let propsHtml = "";
  for (const k of Object.keys(props)) {
    if (k.startsWith("_")) continue;
    let v = props[k];
    if (v == null || v === "") continue;
    if (typeof v === "object") v = JSON.stringify(v, null, 2);
    propsHtml += `<div><strong>${escapeHtml(k)}</strong><div class="val">${escapeHtml(String(v))}</div></div>`;
  }

  let neighHtml = "";
  if (neighbors.length) {
    neighHtml = `<div class="neighbors"><div class="title">${neighbors.length} neighbor${neighbors.length===1?"":"s"} ${ (adj.get(d.id)||new Set()).size>12 ? `(showing 12)` : ""}</div>` +
      neighbors.map(n => `<a data-id="${escapeAttr(n.id)}">${escapeHtml(n.label || n.id)} <span style="color:#586069;font-size:10px">· ${escapeHtml(n.type||"")}</span></a>`).join("") + `</div>`;
  }

  info.innerHTML = `
    <h3>${escapeHtml(d.label || d.id)}</h3>
    <div class="id">${escapeHtml(d.id)}</div>
    <div class="meta">
      <span class="badge">${escapeHtml(type)}</span>
      <span class="badge com">community ${com}</span>
    </div>
    <div class="props">${propsHtml || '<div class="val" style="color:#586069">no properties</div>'}</div>
    ${neighHtml}
    <div class="pin-hint">${isPinned ? "📌 pinned · click background or another node to unpin" : "click node to pin · esc to clear"}</div>
  `;
  info.classList.add("show");
  if (isPinned) info.classList.add("pinned"); else info.classList.remove("pinned");

  // wire neighbor click
  info.querySelectorAll(".neighbors a").forEach(a => {
    a.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const nid = a.dataset.id;
      const target = idMap.get(nid);
      if (target) {
        pinned = target;
        setFocus(target, { pinned: true });
      }
    });
  });
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeAttr(s) { return escapeHtml(s); }

// Hover (only when not pinned).
node.on("mouseenter", (e, d) => { if (!pinned) { focused = d; setFocus(d); } })
    .on("mouseleave", () => { if (!pinned && focused) { focused = null; setFocus(null); } })
    .on("click", (e, d) => {
      e.stopPropagation();
      if (pinned && pinned.id === d.id) { pinned = null; setFocus(null); }
      else { pinned = d; setFocus(d, { pinned: true }); }
    });

// Click background to unpin.
svg.on("click", () => { if (pinned) { pinned = null; setFocus(null); } });

// Esc to clear.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { pinned = null; focused = null; setFocus(null); document.getElementById("search-input").value=""; runSearch(""); }
});

// Legend — color mode toggle + community/type filter.
const types = [...new Set(DATA.nodes.map(n => n.type))].sort();
const communities = [...new Set(DATA.nodes.map(n => n.community))]
  .filter(c => c != null).sort((a,b) => a-b);
// rank communities by size, show top 30
const comSizes = {};
DATA.nodes.forEach(n => { comSizes[n.community] = (comSizes[n.community]||0)+1; });
const topCommunities = communities.slice().sort((a,b) => comSizes[b]-comSizes[a]).slice(0, 30);

const hidden = new Set(); // hidden type or community ids depending on mode
const legend = document.getElementById("legend");

function renderLegend() {
  const isCom = colorMode === "community";
  const items = isCom
    ? topCommunities.map(c => ({ id:`c${c}`, name:`Community ${c}`, color:communityColor(c), count:comSizes[c] }))
    : types.map(t => ({ id:`t${t}`, name:t, color:TYPE_COLORS[t]||DEFAULT_COLOR, count:DATA.nodes.filter(n=>n.type===t).length }));
  const totalShown = isCom ? topCommunities.reduce((s,c)=>s+comSizes[c],0) : DATA.nodes.length;
  const pctShown = isCom ? Math.round(totalShown/DATA.nodes.length*100) : 100;

  legend.innerHTML = `
    <div class="header">View</div>
    <div class="mode-toggle">
      <button class="active" disabled>2D</button>
      <button onclick="window.location.href='graph-3d.html'">3D</button>
    </div>
    <div class="header">Color by</div>
    <div class="mode-toggle">
      <button data-mode="community" class="${isCom?'active':''}">Community</button>
      <button data-mode="type" class="${!isCom?'active':''}">Type</button>
    </div>
    <div class="header">Filter${isCom ? ` · top 30 of ${communities.length} (${pctShown}%)` : ""}</div>
    ${items.map(it => `<div class="item" data-id="${it.id}"><span class="swatch" style="background:${it.color}"></span><span>${escapeHtml(it.name)}</span><span class="count">${it.count}</span></div>`).join("")}
    <div class="actions"><span id="show-all">show all</span><span id="hide-all">hide all</span></div>
  `;

  legend.querySelectorAll(".mode-toggle button").forEach(b => {
    b.addEventListener("click", () => { colorMode = b.dataset.mode; node.attr("fill", nodeFill); hidden.clear(); applyFilter(); renderLegend(); });
  });
  legend.querySelectorAll(".item").forEach(el => {
    el.addEventListener("click", () => {
      const id = el.dataset.id;
      if (hidden.has(id)) hidden.delete(id); else hidden.add(id);
      applyFilter();
      el.classList.toggle("muted", hidden.has(id));
    });
  });
  legend.querySelector("#show-all").addEventListener("click", () => { hidden.clear(); applyFilter(); legend.querySelectorAll(".item").forEach(el => el.classList.remove("muted")); });
  legend.querySelector("#hide-all").addEventListener("click", () => {
    legend.querySelectorAll(".item").forEach(el => { hidden.add(el.dataset.id); el.classList.add("muted"); });
    applyFilter();
  });
}

function applyFilter() {
  const isCom = colorMode === "community";
  const isHidden = (n) => hidden.has(isCom ? `c${n.community}` : `t${n.type}`);
  node.style("display", n => isHidden(n) ? "none" : "");
  link.style("display", e => (isHidden(e.source) || isHidden(e.target)) ? "none" : "");
}

renderLegend();

// Stats
document.getElementById("stats").textContent =
  `${DATA.nodes.length.toLocaleString()} nodes · ${DATA.edges.length.toLocaleString()} edges · ${communities.length} communities`;

// Search — focus matches, dim rest.
const searchInput = document.getElementById("search-input");
function runSearch(q) {
  q = q.toLowerCase().trim();
  if (!q) {
    if (!focused && !pinned) {
      node.attr("opacity", 1);
      link.attr("opacity", 0.45);
    }
    return;
  }
  const matched = new Set();
  DATA.nodes.forEach(n => {
    if ((n.label || "").toLowerCase().includes(q) || n.id.toLowerCase().includes(q)) matched.add(n.id);
  });
  node.attr("opacity", n => matched.has(n.id) ? 1 : 0.1);
  link.attr("opacity", e => matched.has(e.source.id) || matched.has(e.target.id) ? 0.5 : 0.04);
}
searchInput.addEventListener("input", (e) => runSearch(e.target.value));
</script>
</body>
</html>'''
    # Phase D prune (2026-07-23): the standalone graph.html D3 viz (~39 MB) is read by NOTHING — the UX
    # Brain tab renders from graph-slim.json (built off graph.json) and recall reads Postgres. Now that
    # headless extraction runs merge on every close (C3), rewriting 39 MB each time is pure waste. Skip it
    # in the close/headless path (MERGE_SKIP_HTML=1); a nightly/rebuild WITHOUT the flag still emits it for
    # browser viewing. graph.json (the real artifact, written above) is always current.
    if os.environ.get("MERGE_SKIP_HTML") == "1":
        print("\n[merge] MERGE_SKIP_HTML=1 → skipped graph.html render (graph.json current; UX reads graph-slim.json)")
    else:
        html = html_template.replace("__GRAPH_DATA__", json.dumps(graph))
        with open(OUT / "graph.html", "w") as f:
            f.write(html)
        print(f"\nWrote {OUT / 'graph.html'} ({len(html):,} bytes)")
        print(f"  Open: file://{OUT / 'graph.html'}")

    return 0 if not dangling else 1


if __name__ == "__main__":
    sys.exit(main())
