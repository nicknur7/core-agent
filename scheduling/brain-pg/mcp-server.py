#!/usr/bin/env python3
"""Brain MCP server — exposes corebrain hybrid retrieval to Claude Code sessions.

Wraps `query.py`'s hybrid RRF + the underlying Postgres schema. Each Core
declares this server in its `.mcp.json` with a per-Core `CORE_ORG_ID` env,
so all reads default to that Core's scope. Cross-Core reads are explicit
via the `scope` parameter on `recall_similar`.

Spec: tasks/spec-multi-core-architecture-2026-05-19.md Phase 7.

Run standalone (stdio):
    CORE_ORG_ID=1 CORE_BRAIN=... uv run --python 3.12 --with mcp \\
        --with psycopg2-binary --with pgvector --with voyageai \\
        python mcp-server.py
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

# Make sibling modules importable (query.py, _env.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _env import load_secrets, get_org_id, connect_corebrain  # noqa: E402
load_secrets()

# Surface the env-var checks BEFORE importing query (which fails loud on its own).
_ = get_org_id()  # raises if CORE_ORG_ID unset

from query import hybrid_query  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    # 2026-08-27: this reported EVERY failure as "SDK not installed" — which is the one thing
    # it is NOT when mcp 2.x is present. 2.x dropped mcp.server.fastmcp and renamed FastMCP to
    # MCPServer, so the import dies with the package sitting right there on disk. The message
    # pointed at packaging and away from a breaking API change; four of five seats sat dead for
    # 15 days behind it (school found it on the Mac mini, 2026-08-27), and the seat that fixed
    # itself on 08-12 still misread its own remaining server tonight. Tell the two cases apart.
    import importlib.util
    if importlib.util.find_spec("mcp") is None:
        sys.stderr.write(
            "ERROR: mcp Python SDK not installed. Install via:\n"
            "  uv run --python 3.12 --with 'mcp<2' python mcp-server.py\n"
        )
    else:
        sys.stderr.write(
            f"ERROR: mcp IS installed but has no mcp.server.fastmcp ({exc}).\n"
            "That means mcp 2.x, which renamed FastMCP -> MCPServer. This server targets the\n"
            "1.x API. Pin it in .mcp.json:  \"--with\", \"mcp<2\"   (verified good: 1.29.1)\n"
        )
    sys.exit(1)


mcp = FastMCP("core-brain")


# ─── Tools ───────────────────────────────────────────────────────────────────

# org_name → org_id resolution (cached at module load from tenants table)
_ORG_NAME_TO_ID: dict[str, int] = {}


def _load_org_name_map() -> None:
    global _ORG_NAME_TO_ID
    if _ORG_NAME_TO_ID:
        return
    conn = connect_corebrain()
    cur = conn.cursor()
    try:
        cur.execute("SELECT org_id, name FROM tenants")
        for org_id, name in cur.fetchall():
            _ORG_NAME_TO_ID[name] = org_id
    except Exception:
        # tenants table missing or empty — fall back to hardcoded map.
        # 2026-07-29: was {"life": 1, "business": 2, "school": 3} — missing finance=4 and
        # ops=5, both of which have existed for weeks. So whenever the tenants read failed,
        # `scope=["finance"]` raised ValueError. That punished the exact behaviour the
        # 2026-07-29 default flip now REQUIRES of every caller: naming a scope explicitly.
        # Verified against the live tenants table: 1 life, 2 business, 3 school, 4 finance,
        # 5 ops.
        _ORG_NAME_TO_ID = {"life": 1, "business": 2, "school": 3, "finance": 4, "ops": 5}
    finally:
        conn.close()


def _resolve_scope(scope) -> "str | list[int]":
    """Convert MCP-layer scope (None | list[str] of org names | 'all' | 'self') to
    query.py's scope (str | list[int])."""
    # OMITTED SCOPE MEANS SELF (2026-08-31). This returned "all" for scope=None, and
    # recall_similar/recall_at default scope to None — so query.py's deliberate `scope="self"`
    # default (set 2026-07-29) was overridden on every ordinary MCP recall, and every Core has been
    # reading all five partitions this whole time. An explicit scope="all" still means all; the
    # caller then chose it.
    if scope == "all":
        return "all"
    if scope is None:
        return "self"
    if scope == "self":
        return "self"
    if isinstance(scope, list):
        _load_org_name_map()
        ids = []
        for s in scope:
            if isinstance(s, int):
                ids.append(s)
            elif s in _ORG_NAME_TO_ID:
                ids.append(_ORG_NAME_TO_ID[s])
            else:
                # Try parsing as int string
                try:
                    ids.append(int(s))
                except (TypeError, ValueError):
                    raise ValueError(f"Unknown org name: {s!r}. Known: {list(_ORG_NAME_TO_ID.keys())}")
        return ids
    raise ValueError(f"Invalid scope: {scope!r}")


@mcp.tool()
def recall_at(query: str, as_of: str, k: int = 10, scope: list[str] | None = None,
              rerank: bool = True) -> list[dict]:
    """Point-in-time retrieval. Returns top-k hits as they were at as_of.

    Filters entities + evidence by valid_from <= as_of AND
    (valid_until IS NULL OR valid_until > as_of). Edges filtered by
    created_at <= as_of (entity_edges has no valid_until).

    Use cases:
      - "what did we believe about X in April" (as_of='2026-04-01')
      - "the system state right before the RLS shift" (as_of='2026-05-19T20:10:00')
      - debugging which fact was current at session-T

    Args:
        query: free-text search query.
        as_of: ISO timestamp/date (e.g. '2026-04-15' or '2026-04-15T12:00:00-07:00').
        k, scope, rerank: same as recall_similar.

    Returns:
        List of dicts: [{kind, source, excerpt, rrf_score, legs, rerank_score?}, ...]
    """
    resolved = _resolve_scope(scope)
    return hybrid_query(query, k=k, scope=resolved, rerank=rerank, as_of=as_of)


@mcp.tool()
def recall_similar(query: str, k: int = 10, scope: list[str] | None = None,
                   rerank: bool = True) -> list[dict]:
    """Retrieve top-k hits from the brain via 4-leg hybrid RRF (vector + tsvector + graph BFS + edge-relation vectors).

    Args:
        query: free-text search query.
        k: number of results to return (default 10).
        scope: optional list of org names ['life','business','school'] to scope across.
               None or 'all' (default): read across all orgs via RLS read_all policy.
               'self': restrict to this Core's own org_id.
               List of names: restrict to that explicit set.
        rerank: cross-encoder rerank (FlashRank ms-marco-MiniLM-L-12-v2) over fused
               top-(2k) candidates. Default True; disable for latency-sensitive callers.

    Returns:
        List of dicts: [{kind, source, excerpt, rrf_score, legs, rerank_score?}, ...]
    """
    resolved = _resolve_scope(scope)
    results = hybrid_query(query, k=k, scope=resolved, rerank=rerank)
    return results


@mcp.tool()
def get_entity(name: str) -> dict:
    """Look up an entity hub by exact name (any kind). Returns compiled_truth_md + metadata.

    Args:
        name: entity name (case-sensitive match against entities.name).

    Returns:
        {id, name, kind, compiled_truth_md, confidence, last_compiled_at, source_file}
        or {} if not found.
    """
    conn = connect_corebrain()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, kind, compiled_truth_md, confidence, last_compiled_at, source_file "
        "FROM entities "
        "WHERE name = %s AND (valid_until IS NULL) "
        # B5 (Phase 3): visibility — own org OR shared; hide other-org private content.
        "  AND (org_id = current_setting('app.current_org_id')::bigint OR scope = 'shared') "
        "LIMIT 1",
        (name,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {}
    return {
        "id": row[0], "name": row[1], "kind": row[2],
        "compiled_truth_md": row[3], "confidence": row[4],
        "last_compiled_at": row[5].isoformat() if row[5] else None,
        "source_file": row[6],
    }


@mcp.tool()
def get_topic(slug: str) -> dict:
    """Look up a topic hub by slug (matches against name with case-insensitive prefix).

    Args:
        slug: topic slug or name (e.g. 'engine-split', 'multi-core').

    Returns:
        {id, name, kind, compiled_truth_md, confidence, last_compiled_at, source_file}
        or {} if not found.
    """
    conn = connect_corebrain()
    cur = conn.cursor()
    # Try exact match first, then trigram-best-match
    cur.execute(
        "SELECT id, name, kind, compiled_truth_md, confidence, last_compiled_at, source_file "
        "FROM entities "
        "WHERE kind = 'Topic' "
        "  AND (name = %s OR name ILIKE %s) "
        "  AND (valid_until IS NULL) "
        # B5 (Phase 3): visibility — own org OR shared.
        "  AND (org_id = current_setting('app.current_org_id')::bigint OR scope = 'shared') "
        "ORDER BY name = %s DESC "
        "LIMIT 1",
        (slug, slug.replace('-', ' ') + '%', slug),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {}
    return {
        "id": row[0], "name": row[1], "kind": row[2],
        "compiled_truth_md": row[3], "confidence": row[4],
        "last_compiled_at": row[5].isoformat() if row[5] else None,
        "source_file": row[6],
    }


@mcp.tool()
def get_evidence(evidence_id: int) -> dict:
    """Fetch a single evidence row by id (chunk-level fact).

    Args:
        evidence_id: the evidence.id Postgres primary key.

    Returns:
        {id, entity_id, source_file, excerpt, session_date, chunk_id, valid_from}
        or {} if not found.
    """
    conn = connect_corebrain()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, entity_id, source_file, excerpt, session_date, chunk_id, valid_from "
        "FROM evidence "
        "WHERE id = %s AND (valid_until IS NULL) "
        # B5 (Phase 3): visibility — own org OR shared; evidence has its own scope col (B4).
        "  AND (org_id = current_setting('app.current_org_id')::bigint OR scope = 'shared') "
        "LIMIT 1",
        (evidence_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {}
    return {
        "id": row[0], "entity_id": row[1], "source_file": row[2],
        "excerpt": row[3],
        "session_date": row[4].isoformat() if row[4] else None,
        "chunk_id": row[5],
        "valid_from": row[6].isoformat() if row[6] else None,
    }


@mcp.tool()
def list_recent_sessions(project: str | None = None, since_days: int = 7) -> list[dict]:
    """List session source files ingested in the last N days.

    Args:
        project: optional project slug filter (e.g. 'core', 'life', 'business').
                 Matches if source_file contains '/projects/<project>/sessions/'.
        since_days: window in days (default 7).

    Returns:
        List of dicts sorted by session_date desc: [{source_file, session_date, evidence_count}, ...]
    """
    conn = connect_corebrain()
    cur = conn.cursor()
    if project:
        # Use ANY-form pattern match
        like_pat = f"%/projects/{project}/sessions/%"
        cur.execute(
            "SELECT source_file, MAX(session_date) AS sd, COUNT(*) AS n "
            "FROM evidence "
            "WHERE valid_until IS NULL "
            ""
            "  AND source_file LIKE %s "
            "  AND (session_date IS NULL OR session_date >= CURRENT_DATE - %s::int) "
            "GROUP BY source_file "
            "ORDER BY sd DESC NULLS LAST "
            "LIMIT 100",
            (like_pat, since_days),
        )
    else:
        cur.execute(
            "SELECT source_file, MAX(session_date) AS sd, COUNT(*) AS n "
            "FROM evidence "
            "WHERE valid_until IS NULL "
            ""
            "  AND (session_date IS NULL OR session_date >= CURRENT_DATE - %s::int) "
            "GROUP BY source_file "
            "ORDER BY sd DESC NULLS LAST "
            "LIMIT 100",
            (since_days,),
        )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "source_file": r[0],
            "session_date": r[1].isoformat() if r[1] else None,
            "evidence_count": r[2],
        }
        for r in rows
    ]


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
