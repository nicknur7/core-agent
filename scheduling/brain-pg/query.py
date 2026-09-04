#!/usr/bin/env python3
"""Hybrid retrieval (4 legs): pgvector + Postgres FTS + entity_edges graph BFS + edge-relation vectors, fused via Reciprocal Rank Fusion.

Used by:
  - .claude/commands/recall-similar.md (replaces grep)
  - ~/.claude/skills/claude-brain/SKILL.md (replaces grep)
  - scheduling/brain-pg/eval.py (Step 7 benchmark)

Usage:
  python3 query.py "what did we decide about untrusted-reader"
  python3 query.py --k 5 --json "compile-truth scope"
  python3 query.py --no-graph "..."        # disable BFS leg (for benchmarking)
  python3 query.py --no-vector "..."       # disable the entity/evidence vector leg
  python3 query.py --no-edge-vector "..."  # disable the edge-vector leg
  python3 query.py --no-fts "..."          # disable FTS leg

RRF fuses whichever ranked lists actually ran (vector, fts, graph, edge_vector, assertions):
score(d) = Σ 1/(k_rrf + rank_in_leg(d)). Defaults: k_rrf=60 (industry standard), top-50 per leg.

Graceful degradation:
  - Postgres unreachable: prints error to stderr, exits 2, so callers can fall back to grep.
  - VOYAGE_API_KEY unset (or the voyageai package not importable): the vector + edge-vector
    legs are dropped AUTOMATICALLY, no flag required — FTS + graph BFS + assertions still run
    and the process still exits 0. A one-line WARNING goes to stderr saying the vector legs
    are off and why (2026-08-31 fix — this used to sys.exit() on the documented, flagless
    invocation, which is the common case for anyone cloning this repo before they have a paid
    Voyage key; README.md's "Core runs without it if you skip that" was false until this fix).
    --no-vector / --no-edge-vector still exist for explicit or benchmark use (see eval.py's
    per-leg ablation) even when a key IS present.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# Load ~/.claude/secrets.env into os.environ before any env-var lookup.
# Handles the bash-subprocess case where zshenv was never sourced (Stop-hook
# context, launchd GUI context). No-op if file missing or all keys already
# set. See _env.py docstring for full rationale.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import (load_secrets, get_org_id, connect_corebrain,  # noqa: E402
                  describe_db_failure)
load_secrets()

# Voyage is only imported when a vector leg is requested (use_vector or use_edge_vector) and
# not disabled by --no-vector/--no-edge-vector. A missing VOYAGE_API_KEY (or an unimportable
# voyageai package) degrades those legs at call time inside hybrid_query — see voyage_client()
# and the VoyageUnavailable handling there — rather than failing at import or exiting the process.
DEFAULT_PER_LEG = 50
DEFAULT_K_RRF = 60
DEFAULT_TOP_K = 10


# --- Temporal filter (Tier 2 #6) -------------------------------------------
# When as_of is None: standard "currently active" filter (valid_until IS NULL).
# When as_of is an ISO date/timestamp string: point-in-time filter — row was
# active at as_of (valid_from <= as_of AND (valid_until IS NULL OR valid_until > as_of)).
def _as_of_filter(as_of: Optional[str], col_prefix: str = "") -> Tuple[str, list]:
    p = (col_prefix + ".") if col_prefix else ""
    if as_of is None:
        return f" AND {p}valid_until IS NULL", []
    return (
        f" AND {p}valid_from <= %s AND ({p}valid_until IS NULL OR {p}valid_until > %s)",
        [as_of, as_of],
    )


# --- Scope handling ---------------------------------------------------------
# scope controls which org_id rows are visible to the query.
#   "all"  → no org filter (default; relies on RLS read_all policy)
#   "self" → restrict to current_setting('app.current_org_id')
#   list[int] → restrict to that explicit set of org_ids
#
# Returns (sql_fragment, params_list) where the fragment is "" or "AND <cond>".
def _scope_clause(scope) -> Tuple[str, list]:
    if scope == "all" or scope is None:
        return "", []
    if scope == "self":
        # OWN ORG UNION THE SHARED TIER (org 0). A strict `org_id = current` makes every hub that
        # is genuinely cross-cutting — the ones deliberately owned by no single Core — invisible to
        # default recall, which is the opposite of what a shared tier is for.
        return (" AND org_id IN (0, current_setting('app.current_org_id')::bigint)", [])
    if isinstance(scope, (list, tuple)):
        if not scope:
            return "", []
        # Safe int coercion — never trust caller-supplied ints into SQL string
        ints = [int(x) for x in scope]
        placeholders = ",".join(["%s"] * len(ints))
        return f" AND org_id IN ({placeholders})", ints
    raise ValueError(f"Invalid scope: {scope!r}. Use 'all', 'self', or list[int].")


_ORG_NAMES: Dict[str, int] = {}


def _org_name_map() -> Dict[str, int]:
    """{org name -> org_id} from the tenants table, with a complete fallback.

    Added 2026-07-29 for the CLI's `--scope`. Reads `tenants`, which is the source of truth
    (verified live: 1 life, 2 business, 3 school, 4 finance, 5 ops). The fallback lists all
    five deliberately — mcp-server.py's equivalent fallback had only three and therefore
    raised on `scope=["finance"]` whenever the tenants read failed, punishing the explicit
    scoping this module now requires.
    """
    global _ORG_NAMES
    if _ORG_NAMES:
        return _ORG_NAMES
    try:
        conn = connect_corebrain()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name, org_id FROM tenants")
            _ORG_NAMES = {n: int(o) for n, o in cur.fetchall()}
        finally:
            conn.close()
    except Exception:
        pass
    if not _ORG_NAMES:
        _ORG_NAMES = {"life": 1, "business": 2, "school": 3, "finance": 4, "ops": 5}
    return _ORG_NAMES


def _org_id_for_name(name: str) -> Optional[int]:
    return _org_name_map().get((name or "").strip().lower())


def _org_name_for_id(org_id) -> Optional[str]:
    if org_id is None:
        return None
    for n, i in _org_name_map().items():
        if i == int(org_id):
            return n
    return None


def connect():
    return connect_corebrain()


class VoyageUnavailable(RuntimeError):
    """Raised by voyage_client() when the vector legs cannot run: VOYAGE_API_KEY is unset (the
    common case for a fresh install — Voyage requires a paid key, Postgres/FTS/graph do not) or
    the voyageai package itself isn't importable. hybrid_query() catches this ONE exception and
    drops use_vector/use_edge_vector, printing a stderr note (DEFECT 1, measured 2026-08-31 by
    forcing VOYAGE_API_KEY="" against the live corebrain: `python3 query.py "<query>"`, the
    documented usage, hard-exited before FTS + graph BFS + assertions — which need no key —
    ever ran). This is deliberately NOT raised by embed.py's own voyage_client() (a separate,
    unrelated function): ingest cannot degrade — there is no vector to write without a key —
    so it is correct for that path to keep hard-exiting."""


def voyage_client():
    try:
        import voyageai
    except ImportError as e:
        raise VoyageUnavailable(f"voyageai package not importable ({e}).") from e
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        raise VoyageUnavailable(
            "VOYAGE_API_KEY missing. Set it in ~/.claude/secrets.env "
            "(canonical location) — file is loaded automatically by _env.py.")
    return voyageai.Client(api_key=key)


def embed_query(client, text: str) -> List[float]:
    r = client.embed([text], model="voyage-3-large", input_type="query")
    return r.embeddings[0]


def vec_literal(emb: List[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in emb) + "]"


# --- Three retrieval legs ---------------------------------------------------

def leg_vector(cur, query_emb: List[float], top: int, scope="all", as_of: Optional[str] = None) -> List[Tuple[str, str, str, float]]:
    """Returns [(kind, source, excerpt, sim), ...] from entities + evidence via cosine.

    scope: 'all' (default, cross-Core) | 'self' | list[int] of org_ids.
    as_of: None for current state, ISO date/timestamp for point-in-time.
    """
    lit = vec_literal(query_emb)
    results = []
    sc_sql, sc_params = _scope_clause(scope)
    tm_sql, tm_params = _as_of_filter(as_of)

    cur.execute(f"""
        SELECT 'entity' as kind, COALESCE(name, '') as src, COALESCE(compiled_truth_md, ''), 1 - (embedding <=> %s::vector) as sim
        FROM entities WHERE embedding IS NOT NULL{tm_sql}{sc_sql}{_visibility_filter()}
        ORDER BY embedding <=> %s::vector ASC
        LIMIT %s
    """, (lit, *tm_params, *sc_params, lit, top))
    results.extend(cur.fetchall())

    cur.execute(f"""
        SELECT 'evidence' as kind, source_file, excerpt, 1 - (embedding <=> %s::vector) as sim
        FROM evidence WHERE embedding IS NOT NULL{tm_sql}{sc_sql}{_visibility_filter()}
        ORDER BY embedding <=> %s::vector ASC
        LIMIT %s
    """, (lit, *tm_params, *sc_params, lit, top))
    results.extend(cur.fetchall())

    results.sort(key=lambda r: -r[3])
    return results[:top]


def leg_fts(cur, query_text: str, top: int, scope="all", as_of: Optional[str] = None) -> List[Tuple[str, str, str, float]]:
    """Postgres tsvector full-text rank.

    scope: 'all' (default, cross-Core) | 'self' | list[int] of org_ids.
    as_of: None for current state, ISO date/timestamp for point-in-time.
    """
    results = []
    sc_sql, sc_params = _scope_clause(scope)
    tm_sql, tm_params = _as_of_filter(as_of)
    cur.execute(f"""
        SELECT 'entity', name, COALESCE(compiled_truth_md, ''),
               ts_rank(to_tsvector('english', COALESCE(compiled_truth_md, '')),
                       plainto_tsquery('english', %s)) as r
        FROM entities
        WHERE to_tsvector('english', COALESCE(compiled_truth_md, '')) @@ plainto_tsquery('english', %s)
          {tm_sql}{sc_sql}{_visibility_filter()}
        ORDER BY r DESC
        LIMIT %s
    """, (query_text, query_text, *tm_params, *sc_params, top))
    results.extend(cur.fetchall())

    cur.execute(f"""
        SELECT 'evidence', source_file, excerpt,
               ts_rank(to_tsvector('english', excerpt), plainto_tsquery('english', %s)) as r
        FROM evidence
        WHERE to_tsvector('english', excerpt) @@ plainto_tsquery('english', %s)
          {tm_sql}{sc_sql}{_visibility_filter()}
        ORDER BY r DESC
        LIMIT %s
    """, (query_text, query_text, *tm_params, *sc_params, top))
    results.extend(cur.fetchall())

    results.sort(key=lambda r: -r[3])
    return results[:top]


def _scope_clause_aliased(scope, alias: str) -> Tuple[str, list]:
    """Same as _scope_clause but qualifies the org_id column with a table alias.
    Used inside the BFS recursive CTE final SELECT where entities is aliased 'e'.
    """
    if scope == "all" or scope is None:
        return "", []
    if scope == "self":
        # See _scope_clause: own org ∪ shared (org 0). Applies to graph BFS anchors and traversal
        # too, so an edge into a shared hub is walkable rather than a dead end.
        return (f" AND {alias}.org_id IN (0, current_setting('app.current_org_id')::bigint)", [])
    if isinstance(scope, (list, tuple)):
        if not scope:
            return "", []
        ints = [int(x) for x in scope]
        placeholders = ",".join(["%s"] * len(ints))
        return f" AND {alias}.org_id IN ({placeholders})", ints
    raise ValueError(f"Invalid scope: {scope!r}")


# --- Content-privacy visibility (Phase 3) -----------------------------------
# A SEPARATE axis from _scope_clause (which filters org_id). A row is visible if
# it belongs to the caller's OWN org OR its scope is 'shared'. Private rows are
# visible only to their own org — this hides CONTENT cross-Core.
#
# Table-aware (B2): only reference the `scope` column on tables that HAVE it
# (entities, evidence) — NEVER on entity_edges (which has no scope column; a
# predicate on it would raise UndefinedColumn and crash the leg). Two forms:
#   _visibility_filter → WHERE fragment; EXCLUDES other-org private rows entirely.
#       Used on content-search legs (vector/fts): you must not be able to find
#       another org's private content by searching its content.
#   _truth_redact → SELECT expression; KEEPS the row but blanks its content column
#       for other-org private rows. Used on STRUCTURAL legs (edge-vector /
#       bfs-neighbors): the breadcrumb — a cross-Core edge still resolves the
#       name/existence ("this Core knows something here"), but not the content.
# scope is NOT NULL DEFAULT 'shared', so no COALESCE is needed (M2).
def _attach_workflow_steps(hits: List[dict]) -> None:
    """Turn any `Workflow` entity hit into an ordered procedure, in place.

    Phase B3 of tasks/research/learning-substrate-unification-2026-08-03.md. Phase B gave the brain
    a way to STORE an ordered sequence; without this, nothing could ever read one back out as a
    sequence, so the representation existed and no consumer could use it.

    Two details that matter more than they look:

    1. The steps are rendered into `excerpt` as well as attached as structured `steps`. Most callers
       — including the MCP `recall_similar` consumers and every prompt-injection path — read
       `excerpt` and nothing else. Attaching a `steps` key alone would have been the same class of
       defect this file is full of comments about: a value provided on one surface while every real
       consumer reads the other.
    2. `kind` is flipped to "workflow" so a caller can tell a procedure from a topic without
       inspecting the payload.

    Opens its own short connection rather than reusing the caller's, because it runs AFTER the fused
    result set is final — the ordering has to be applied to what actually survived ranking, not to
    the candidate pool. Only fires when an entity hit is present, so the common path pays nothing.
    """
    names = [h.get("source") for h in hits
             if h.get("kind") == "entity" and h.get("source")]
    if not names:
        return
    steps_by_name: dict = {}
    try:
        conn = connect_corebrain()
    except Exception:
        return                      # recall must not fail because the step lookup could not connect
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT e.name, w.step_index, w.action, w.tool_hint "
            "FROM entities e JOIN workflow_steps w ON w.workflow_entity_id = e.id "
            "WHERE e.kind = 'Workflow' AND e.name = ANY(%s) "
            "  AND (e.org_id = current_setting('app.current_org_id')::bigint OR e.scope = 'shared') "
            "ORDER BY e.name, w.step_index",
            (names,),
        )
        for name, idx, action, hint in cur.fetchall():
            steps_by_name.setdefault(name, []).append(
                {"step": int(idx), "action": action, "tool_hint": hint})
    except Exception:
        return                      # e.g. pre-migration Core with no workflow_steps table
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for h in hits:
        s = steps_by_name.get(h.get("source"))
        if not s:
            continue
        h["kind"] = "workflow"
        h["steps"] = s
        rendered = "\n".join(
            f"{d['step']}. {d['action']}" + (f"   [{d['tool_hint']}]" if d.get("tool_hint") else "")
            for d in s)
        base = (h.get("excerpt") or "").strip()
        h["excerpt"] = (base + "\n\n" + rendered).strip() if base else rendered


def _visibility_filter(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (f" AND ({p}org_id = current_setting('app.current_org_id')::bigint"
            f" OR {p}scope = 'shared')")


def _truth_redact(truth_col: str, alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (f"CASE WHEN {p}scope = 'private'"
            f" AND {p}org_id <> current_setting('app.current_org_id')::bigint"
            f" THEN '' ELSE COALESCE({truth_col}, '') END")


# Tier 1 B3: edge-type weights applied in graph BFS scoring.  # privacy-ok: generic engineering vocabulary
# Higher = stronger signal toward the connected entity.
EDGE_TYPE_WEIGHTS = {
    "supersedes":    1.5,   # most-recent assertion supersedes prior — strong
    "motivated_by":  1.2,   # causal link — strong
    "cross_impacts": 1.1,   # mutual influence — moderate
    "learned_from":  1.0,   # baseline
    "references":    0.8,   # weak mention
    "originates_in": 0.3,   # provenance backbone — de-isolates without dominating BFS
    "same_as":       0.4,   # cross-Core corroboration (M1) — bridges Cores without crowding BFS slots
}
DEFAULT_EDGE_WEIGHT = 1.0


def leg_edge_vector(cur, query_emb: List[float], top: int, scope="all", as_of: Optional[str] = None) -> List[Tuple[str, str, str, float]]:
    """Tier 2 #7: Semantic search over edge embeddings.

    Returns the connected entities of the most semantically-relevant edges.
    Each matched edge contributes BOTH endpoints as separate result rows so
    they fuse naturally with other legs in RRF.

    Score: edge cosine similarity × edge-type weight × confidence.
    """
    lit = vec_literal(query_emb)
    sc_sql, sc_params = _scope_clause(scope)
    edge_tm_sql, edge_tm_params = ("", []) if as_of is None else (" AND created_at <= %s", [as_of])

    cur.execute(f"""
        SELECT ee.from_entity_id, ee.to_entity_id, ee.edge_type, COALESCE(ee.confidence, 1.0) AS conf,
               1 - (ee.embedding <=> %s::vector) AS sim
        FROM entity_edges ee
        WHERE ee.embedding IS NOT NULL{sc_sql}{edge_tm_sql}
        ORDER BY ee.embedding <=> %s::vector ASC
        LIMIT %s
    """, (lit, *sc_params, *edge_tm_params, lit, top))
    edge_rows = cur.fetchall()

    if not edge_rows:
        return []

    # Bulk fetch endpoint names + truths
    eids = set()
    for fid, tid, *_ in edge_rows:
        eids.add(fid); eids.add(tid)
    eids = list(eids)
    tm_sql, tm_params = _as_of_filter(as_of)
    cur.execute(
        f"SELECT id, name, {_truth_redact('compiled_truth_md')} FROM entities WHERE id = ANY(%s){tm_sql}",
        (eids, *tm_params),
    )
    by_id = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    results: List[Tuple[str, str, str, float]] = []
    seen_keys: set = set()  # ('entity', name) — dedup repeats from multiple edges
    for fid, tid, etype, conf, sim in edge_rows:
        type_weight = EDGE_TYPE_WEIGHTS.get(etype, DEFAULT_EDGE_WEIGHT)
        score = float(sim) * type_weight * float(conf)
        for eid in (fid, tid):
            if eid not in by_id:
                continue
            name, truth = by_id[eid]
            key = ("entity", name)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(("entity", name, truth, score))
    return results[:top]


def leg_graph_bfs(cur, query_text: str, top: int, hops: int = 2, scope="all", as_of: Optional[str] = None) -> List[Tuple[str, str, str, float]]:
    """Trigram-match an anchor entity, BFS its 2-hop neighborhood, score by
    edge_type weight × confidence × 1/(1+distance).

    scope: 'all' (default, cross-Core) | 'self' | list[int] of org_ids.
    as_of: None for current state, ISO date/timestamp for point-in-time.
    Time filter applies to entities (valid_from/valid_until) and to edges
    (entity_edges.created_at — edges added after as_of are excluded).

    Tier 1 B3: extended the recursive CTE to track edge_type + confidence
    per hop. Outer SELECT picks the SHORTEST-distance path per neighbor;
    score multiplies anchor similarity by edge-type weight × confidence.
    """
    sc_sql, sc_params = _scope_clause(scope)
    sc_sql_e, _ = _scope_clause_aliased(scope, "e")
    tm_sql, tm_params = _as_of_filter(as_of)
    # Edge-level temporal filter — entity_edges has created_at but no valid_until
    edge_tm_sql, edge_tm_params = ("", []) if as_of is None else (" AND created_at <= %s", [as_of])

    # Anchor: best trigram similarity on entity name
    cur.execute(f"""
        SELECT id, name, COALESCE(compiled_truth_md, ''), similarity(name, %s) as s
        FROM entities WHERE TRUE{tm_sql}{sc_sql}{_visibility_filter()}
        ORDER BY s DESC LIMIT 5
    """, (query_text, *tm_params, *sc_params))
    anchors = cur.fetchall()
    anchors = [a for a in anchors if a[3] > 0.15]
    if not anchors:
        return []

    results = []
    seen = set()
    for anchor in anchors:
        anchor_id, anchor_name, anchor_truth, anchor_score = anchor
        if anchor_id in seen:
            continue
        seen.add(anchor_id)
        results.append(("entity", anchor_name, anchor_truth, 1.0 * anchor_score))

        # Recursive CTE carries edge_type + confidence at each hop.
        # Postgres recursive CTE needs ONE recursive term; we use LATERAL
        # to combine forward+reverse traversal into a single recursive arm.
        # Temporal filter applied to edges (created_at) and entities (valid_*).  # privacy-ok: generic engineering vocabulary
        tm_sql_e, _ = _as_of_filter(as_of, col_prefix="e")
        cur.execute(f"""
            WITH RECURSIVE neighbors AS (
              SELECT to_entity_id AS eid, 1 AS dist, edge_type, COALESCE(confidence, 1.0) AS conf
                FROM entity_edges
                WHERE from_entity_id = %s{sc_sql}{edge_tm_sql}
              UNION
              SELECT from_entity_id AS eid, 1 AS dist, edge_type, COALESCE(confidence, 1.0) AS conf
                FROM entity_edges
                WHERE to_entity_id = %s{sc_sql}{edge_tm_sql}
              UNION
              SELECT next.eid, n.dist + 1, next.edge_type, next.conf
              FROM neighbors n
              JOIN LATERAL (
                SELECT to_entity_id AS eid, edge_type, COALESCE(confidence, 1.0) AS conf
                  FROM entity_edges
                  WHERE from_entity_id = n.eid{sc_sql}{edge_tm_sql}
                UNION
                SELECT from_entity_id AS eid, edge_type, COALESCE(confidence, 1.0) AS conf
                  FROM entity_edges
                  WHERE to_entity_id = n.eid{sc_sql}{edge_tm_sql}
              ) next ON true
              WHERE n.dist < %s
            )
            SELECT e.name, {_truth_redact('e.compiled_truth_md', 'e')},
                   MIN(n.dist) AS dist,
                   (array_agg(n.edge_type ORDER BY n.dist ASC, n.conf DESC))[1] AS edge_type,
                   (array_agg(n.conf      ORDER BY n.dist ASC, n.conf DESC))[1] AS confidence
            FROM neighbors n JOIN entities e ON e.id = n.eid
            WHERE e.id != %s{tm_sql_e}{sc_sql_e}
            GROUP BY e.id, e.name, e.compiled_truth_md
            ORDER BY dist
            LIMIT %s
        """, (
            anchor_id, *sc_params, *edge_tm_params,
            anchor_id, *sc_params, *edge_tm_params,
            *sc_params, *edge_tm_params,
            *sc_params, *edge_tm_params,
            hops,
            anchor_id, *tm_params, *sc_params,
            top,
        ))
        for name, truth, dist, edge_type, conf in cur.fetchall():
            type_weight = EDGE_TYPE_WEIGHTS.get(edge_type, DEFAULT_EDGE_WEIGHT)
            score = anchor_score * type_weight * float(conf) * (1.0 / (1.0 + dist))
            results.append(("entity", name, truth, score))

    # Dedup by (kind, src)
    by_key = {}
    for r in results:
        key = (r[0], r[1])
        if key not in by_key or r[3] > by_key[key][3]:
            by_key[key] = r
    out = list(by_key.values())
    out.sort(key=lambda r: -r[3])
    return out[:top]


# --- Reciprocal Rank Fusion -------------------------------------------------

def rrf_fuse(ranked_lists: List[Tuple[str, List[Tuple[str, str, str, float]]]],
             k_rrf: int = DEFAULT_K_RRF,
             top_k: int = DEFAULT_TOP_K) -> List[dict]:
    """Take N (leg_name, ranked_list) pairs of (kind, src, excerpt, leg_score) and fuse via RRF.

    Leg-count fix (2026-08-31, DEFECT 5 review): this used to take a bare list of ranked lists
    and label each one's contributions by its POSITION against a fixed
    leg_names=[vector,fts,graph,edge_vector,assertions]. That mislabeled every result's `legs`
    field whenever ANY leg was skipped — --no-fts/--no-graph already did this, and the vector-
    key-missing degrade above would have made it worse (position 0 becomes "fts", not "vector").
    The RRF SCORE itself was never at risk: score(d) = Σ 1/(k_rrf + rank) sums only over legs
    that actually ran, with no leg-count divisor anywhere, so dropping legs neither divides by
    zero nor unfairly skews the surviving legs' scores — a candidate found by fewer legs simply
    earns a lower cumulative score, which is RRF's intended corroboration behavior. Only the
    NAME attached to each rank was wrong. Passing (name, list) pairs removes the positional
    assumption instead of special-casing every new way a leg can be absent.
    """
    scores: Dict[Tuple[str, str], float] = defaultdict(float)
    contributions: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    payload: Dict[Tuple[str, str], Tuple[str, str, str]] = {}

    for leg_name, lst in ranked_lists:
        for rank, row in enumerate(lst, start=1):
            kind, src, excerpt, _ = row
            key = (kind, src)
            scores[key] += 1.0 / (k_rrf + rank)
            contributions[key].append(f"{leg_name}#{rank}")
            payload[key] = (kind, src, excerpt)

    fused = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
    out = []
    for (kind, src), score in fused:
        _, _, excerpt = payload[(kind, src)]
        out.append({
            "kind": kind,
            "source": src,
            "excerpt": (excerpt[:300] + ("…" if len(excerpt) > 300 else "")) if excerpt else "",
            "rrf_score": round(score, 6),
            "legs": contributions[(kind, src)],
        })
    return out


# --- Main -------------------------------------------------------------------

# --- MMR diversification (Tier 1 B2) ---------------------------------------
# Pure-Python MMR over fused candidates using DB-cached embeddings.

def _cosine(a, b) -> float:
    import math
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _fetch_embeddings_for_candidates(cur, candidates) -> List[Optional[List[float]]]:
    """Look up cached embeddings from entities + evidence for each candidate.
    Returns a list aligned with candidates; None for any that can't be resolved.
    """
    entity_names = [c["source"] for c in candidates if c.get("kind") == "entity"]
    evidence_paths = [c["source"] for c in candidates if c.get("kind") == "evidence"]
    emb_map: dict = {}

    if entity_names:
        # Single batched lookup
        cur.execute(
            "SELECT name, embedding FROM entities WHERE name = ANY(%s) "
            "AND valid_until IS NULL AND embedding IS NOT NULL",
            (entity_names,),
        )
        for name, emb in cur.fetchall():
            # pgvector returns string '[0.1,0.2,...]' — parse to list[float]
            if isinstance(emb, str):
                emb = [float(x) for x in emb.strip("[]").split(",")] if emb else None
            emb_map[("entity", name)] = emb

    if evidence_paths:
        cur.execute(
            "SELECT DISTINCT ON (source_file) source_file, embedding "
            "FROM evidence WHERE source_file = ANY(%s) AND valid_until IS NULL "
            "AND embedding IS NOT NULL "
            "ORDER BY source_file, valid_from DESC",
            (evidence_paths,),
        )
        for sf, emb in cur.fetchall():
            if isinstance(emb, str):
                emb = [float(x) for x in emb.strip("[]").split(",")] if emb else None
            emb_map[("evidence", sf)] = emb

    return [emb_map.get((c.get("kind"), c.get("source"))) for c in candidates]


def _mmr_diversify(candidates, embeddings, top_k: int, lambda_param: float = 0.5) -> List[dict]:
    """MMR: argmax over remaining of lambda*relevance - (1-lambda)*max_sim_to_selected.

    relevance = rrf_score (already a unified relevance metric across legs)
    similarity = cosine over cached embeddings; None embedding ⇒ treated as
                 maximally diverse (no penalty)
    """
    from typing import Optional as _Opt  # quiet linter
    if lambda_param >= 1.0 or not candidates:
        return candidates[:top_k]

    n = len(candidates)
    selected_idx: List[int] = []
    remaining = set(range(n))
    while remaining and len(selected_idx) < top_k:
        if not selected_idx:
            best = max(remaining, key=lambda i: candidates[i].get("rrf_score", 0))
        else:
            def score_i(i):
                rel = candidates[i].get("rrf_score", 0)
                ei = embeddings[i]
                if ei is None:
                    div_pen = 0.0
                else:
                    sims = []
                    for j in selected_idx:
                        ej = embeddings[j]
                        if ej is not None:
                            sims.append(_cosine(ei, ej))
                    div_pen = max(sims) if sims else 0.0
                return lambda_param * rel - (1 - lambda_param) * div_pen
            best = max(remaining, key=score_i)
        selected_idx.append(best)
        remaining.remove(best)

    return [candidates[i] for i in selected_idx]


# --- Cross-encoder reranking (Tier 1 B1) ------------------------------------
# Lazy singleton — model loads on first use (~1.5s, ~22MB ms-marco-MiniLM-L-12-v2).
# Subsequent reranks: ~5-50ms for ~30 candidates.
_RERANKER = None
RERANK_MODEL = "ms-marco-MiniLM-L-12-v2"


def _get_reranker():
    global _RERANKER
    if _RERANKER is None:
        try:
            from flashrank import Ranker
            # Persistent cache_dir — flashrank defaults to /tmp, which macOS wipes
            # (reboot / periodic cleanup), silently deleting the model and breaking
            # recall's rerank on the next /tmp clear. Pin to ~/.cache so it survives.
            # 2026-06-03 fix: recall kept "breaking again" for exactly this reason.
            cache_dir = os.environ.get("FLASHRANK_CACHE", os.path.expanduser("~/.cache/flashrank"))
            os.makedirs(cache_dir, exist_ok=True)
            _RERANKER = Ranker(model_name=RERANK_MODEL, max_length=512, cache_dir=cache_dir)
        except ImportError:
            print("WARN: flashrank not installed — rerank disabled. pip install flashrank", file=sys.stderr)
            _RERANKER = False  # sentinel: tried + failed, don't retry
        except Exception as e:
            # Model load/download failure (missing onnx, corrupt cache, network) must
            # DEGRADE to unranked recall, not crash the whole query. Was only catching
            # ImportError, so a wiped /tmp model propagated an ONNXRuntimeError and took
            # recall down entirely.
            print(f"WARN: reranker load failed ({type(e).__name__}: {e}) — recall continues unranked.", file=sys.stderr)
            _RERANKER = False
    return _RERANKER or None


def _rerank(query_text: str, candidates: List[dict], top_k: int) -> List[dict]:
    """Cross-encoder rerank of fused candidates.

    Takes [{kind, source, excerpt, rrf_score, legs}, ...]; returns same shape
    with rerank_score added, ordered by cross-encoder score desc. Falls back
    to candidates as-is if flashrank unavailable.
    """
    ranker = _get_reranker()
    if ranker is None or not candidates:
        return candidates[:top_k]
    from flashrank import RerankRequest

    passages = []
    for i, c in enumerate(candidates):
        # Cross-encoder needs text — use source name + excerpt as the passage
        text = (c.get("source", "") + ". " + (c.get("excerpt") or ""))[:512]
        passages.append({"id": str(i), "text": text})

    result = ranker.rerank(RerankRequest(query=query_text, passages=passages))
    out = []
    for r in result[:top_k]:
        orig = candidates[int(r["id"])]
        out.append({**orig, "rerank_score": round(float(r["score"]), 6)})
    return out


# --- Recency signal (D1 fix 2026-05-31) -------------------------------------
# The recall scorer (RRF over relevance legs) had NO temporal term, so "what did
# we do with X recently" returned the most-relevant-EVER node, not the most
# recent. Fix: when the query signals recency intent, blend a date-based recency
# score into the final ranking. Gated on intent so non-recency lookups (e.g.
# "what is RRF") keep pure relevance ordering. See brain-reliability-audit-2026-05-31.md.
import datetime as _dt

_RECENCY_INTENT_RX = re.compile(
    r"\b(recent(ly)?|lately|nowadays|these\s+days|currently|"
    r"the\s+other\s+(day|night|week)|this\s+(week|morning|month)|"
    r"latest|most\s+recent|just\s+(did|finished|shipped|worked)|"
    r"last\s+(time|session|few\s+days)|past\s+(few\s+)?(days|week))\b",
    re.I,
)
RECENCY_WEIGHT = 0.55          # blend weight when intent fires (0=off, 1=pure recency)
RECENCY_HALFLIFE_DAYS = 21     # recency score halves every 3 weeks


def _recency_intent(text: str) -> bool:
    return bool(_RECENCY_INTENT_RX.search(text or ""))


_DATE_IN_PATH_RX = re.compile(r"(\d{4}-\d{2}-\d{2})")
# leg_assertions() embeds the decision's effective_from as a "[decision YYYY-MM-DD]"
# prefix in its excerpt. Decisions have no source_file to date from, so the recency
# blend read date=None for every decision candidate → recency_score 0 → a "what shipped
# recently" query could never surface a recent DECISION (2026-07-19 Fix #2).
_DECISION_DATE_RX = re.compile(r"\[decision (\d{4}-\d{2}-\d{2})\]")


def _date_from_path(s: str):
    """Parse the LAST YYYY-MM-DD in a source_file path (the content date).
    e.g. 'chunk-body-2026-05-12_agent-...json' -> date(2026,5,12)."""
    if not s:
        return None
    hits = _DATE_IN_PATH_RX.findall(s)
    if not hits:
        return None
    try:
        return _dt.date.fromisoformat(hits[-1])
    except ValueError:
        return None


def _attach_candidate_dates(cur, candidates: List[dict], scope="all", as_of: Optional[str] = None) -> None:
    """Annotate each candidate dict with a CONTENT 'date' (datetime.date or None).

    scope/as_of MIRROR the retrieval's own filters (2026-07-19): without them the
    dating query saw ALL orgs and ALL time, so a scope='self' candidate could inherit
    a newer path date from another org, and an as_of point-in-time query could inherit
    a date from a row that did not exist at as_of (future date → recency 1.0). Now the
    entities/evidence date lookups apply the same _scope_clause + _as_of_filter the legs
    use. (kind-collision — a Topic inheriting a same-name Decision's date — needs entity
    kind/id carried through fusion; deferred, documented as a follow-up.)
    Entities: date parsed from source_file (content origin), falling back to
    created_at (FIRST-ingestion time) when the path has no date.
    A name maps to multiple rows (cross-org fragmentation + path- vs hub-sourced).
    Resolve per-name: a real content date parsed from ANY row's path WINS, and among
    path dates the NEWEST (latest content mention). Only when NO row has a dated path
    do we fall back to created_at — and then the OLDEST created_at (first ingestion),
    because recall is cross-org (scope='all') and the SAME undated topic hub exists
    once per Core; a peer Core re-ingesting it TODAY must not make the hub look fresh
    for everyone. Earliest ingestion pins an undated hub to when it first entered the
    graph, immune to peer re-ingestion; a dated path always beats that fallback.
    Evidence: max(session_date). Content date >> ingestion time for recency, since
    entities get re-compiled (updated_at moves) without the content changing —
    which is exactly why the fallback uses created_at, NOT GREATEST(created_at,
    updated_at): a topic-hub .md (no date in its path) that gets recompiled would
    otherwise score as today-fresh off its last-recompile time (2026-07-19 fix)."""
    ent_names = sorted({c["source"] for c in candidates if c.get("kind") == "entity" and c.get("source")})
    ev_files = sorted({c["source"] for c in candidates if c.get("kind") == "evidence" and c.get("source")})
    ent_dates, ev_dates = {}, {}
    scope_sql, scope_params = _scope_clause(scope)
    asof_sql, asof_params = _as_of_filter(as_of)
    if ent_names:
        cur.execute(
            "SELECT name, source_file, created_at FROM entities WHERE name = ANY(%s)" + scope_sql + asof_sql,
            [ent_names] + scope_params + asof_params,
        )
        path_dates, fallback_dates = {}, {}
        for name, sf, ts in cur.fetchall():
            pd = _date_from_path(sf)
            if pd is not None:
                # content date embedded in the path → NEWEST wins (latest content mention).
                if name not in path_dates or pd > path_dates[name]:
                    path_dates[name] = pd
            else:
                # undated hub → created_at fallback → OLDEST wins (first content origin), so a
                # peer Core re-ingesting the same cross-org hub today can't inflate recency.
                cd = ts.date() if hasattr(ts, "date") else ts
                if cd is not None and (name not in fallback_dates or cd < fallback_dates[name]):
                    fallback_dates[name] = cd
        for name in set(path_dates) | set(fallback_dates):
            # a real content date (from any row's path) beats an undated hub's ingestion fallback.
            ent_dates[name] = path_dates.get(name, fallback_dates.get(name))
    if ev_files:
        cur.execute(
            "SELECT source_file, max(session_date) FROM evidence WHERE source_file = ANY(%s)"
            + scope_sql + asof_sql + " GROUP BY source_file",
            [ev_files] + scope_params + asof_params,
        )
        for sf, d in cur.fetchall():
            ev_dates[sf] = d
    for c in candidates:
        kind = c.get("kind")
        if kind == "decision":
            # Date the decision from its effective_from, embedded in the excerpt.
            m = _DECISION_DATE_RX.search(c.get("excerpt", "") or "")
            try:
                c["date"] = _dt.date.fromisoformat(m.group(1)) if m else None
            except ValueError:
                c["date"] = None
        else:
            c["date"] = (ent_dates if kind == "entity" else ev_dates).get(c.get("source"))


def _recency_score(d, today) -> float:
    if d is None:
        return 0.0
    age = max(0, (today - d).days)
    return 0.5 ** (age / RECENCY_HALFLIFE_DAYS)


def _blend_recency(candidates: List[dict], k: int) -> List[dict]:
    """Re-rank candidates by (1-w)*relevance_norm + w*recency. Relevance is the
    rerank_score if present else rrf_score, min-max normalized across the pool."""
    if not candidates:
        return candidates
    today = _dt.date.today()
    rels = [c.get("rerank_score", c.get("rrf_score", 0.0)) for c in candidates]
    lo, hi = min(rels), max(rels)
    span = (hi - lo) or 1.0
    w = RECENCY_WEIGHT
    for c, rel in zip(candidates, rels):
        rel_norm = (rel - lo) / span
        c["recency_score"] = round(_recency_score(c.get("date"), today), 4)
        c["final_score"] = round((1 - w) * rel_norm + w * c["recency_score"], 6)
    return sorted(candidates, key=lambda c: -c["final_score"])[:k]


# --- Variant dedup (D3 fix 2026-05-31) --------------------------------------
# Entity extraction creates case/punctuation variants of the same name
# ("brain vault" vs "Brain Vault"), which then BOTH surface in recall and eat
# top-k slots with duplicate content. Collapse case/whitespace/punct-identical
# results, keeping the highest-ranked. Conservative — only folds names that are
# identical after normalization, so genuinely distinct entities are untouched.
# (Root cause — canonicalize at extraction — is a separate merge.py change.)
def _norm_name(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[\s\-_]+", " ", s)
    s = re.sub(r"[^\w ]", "", s)
    return s


def _dedup_variants(results: List[dict]) -> List[dict]:
    seen, out = set(), []
    for r in results:
        key = (r.get("kind"), _norm_name(r.get("source", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def leg_assertions(cur, query_text: str, top: int, scope="all",
                   as_of: Optional[str] = None) -> List[Tuple[str, str, str, float]]:
    """Supersession-aware 'what is the CURRENT decision' leg (unified redesign, step ②).

    Returns active, accepted decision-assertions matching the query. SUPERSEDED assertions are
    EXCLUDED (that is the directional demotion the old symmetric graph traversal could never do).
    Ranked by text relevance BLENDED with decision recency (effective_from), so a later decision on
    the same topic outranks an earlier one — the fix for recall handing back a reversed decision
    (e.g. the 2026-07-10 guard unfreeze now outranks the 2026-05-19 'keep per_core_keep')."""
    scope_sql, scope_params = _scope_clause(scope)
    # OR the query terms (assertion text is short — an AND tsquery almost never matches all terms).
    import re as _re
    words = [w for w in _re.findall(r"[a-zA-Z0-9_]+", query_text.lower()) if len(w) > 2]
    if not words:
        return []
    or_tsq = " | ".join(dict.fromkeys(words))  # dedupe, keep order
    q = f"""
        SELECT 'decision' AS kind,
               COALESCE(subject_key, '') AS src,
               ('[decision ' || COALESCE(effective_from::date::text, '?') || '] '
                 || subject_key || ' — ' || (object_json #>> '{{}}')) AS excerpt,
               ( ts_rank(to_tsvector('english', subject_key || ' ' || (object_json #>> '{{}}')),
                         to_tsquery('english', %s))
                 -- Recency boost. COALESCE to 'epoch', NOT to now().
                 --
                 -- This read `COALESCE(effective_from, now())` until 2026-07-29, which scored an
                 -- UNDATED assertion as though it were decided this instant — the MAXIMUM boost
                 -- of 0.1500, strictly above any real date (a 2026-07-10 decision scores 0.0906).
                 -- Combined with assertions_ingest.py never writing the column, 44% of the active
                 -- set was undated and every one of those rows outranked every dated one. That is
                 -- how a policy Nick reversed on 2026-07-10 came back as the current answer and
                 -- core-business built a fabricated security finding on top of it.
                 --
                 -- A missing date is an absence of evidence, so it must earn NO recency credit
                 -- rather than maximum credit. 'epoch' puts age at ~56 years, boost ~0.0000, which
                 -- ranks an undated row below everything dated while still letting text relevance
                 -- surface it. The ingest fix stops new NULLs; this makes the ranking safe if one
                 -- ever appears again (a fresh Core mid-backfill, a hand-inserted row).
                 + 0.15 / (1 + EXTRACT(EPOCH FROM (now() - COALESCE(effective_from, 'epoch'::timestamptz))) / 2592000.0)
               ) AS r
        FROM assertions
        WHERE lifecycle_status = 'active' AND review_status = 'accepted'
          AND to_tsvector('english', subject_key || ' ' || (object_json #>> '{{}}'))
              @@ to_tsquery('english', %s)
          {scope_sql}
        ORDER BY r DESC, effective_from DESC NULLS LAST
        LIMIT %s
    """
    try:
        cur.execute(q, [or_tsq, or_tsq] + scope_params + [top])
        return [(k, s, e, float(r)) for k, s, e, r in cur.fetchall()]
    except Exception:
        return []  # fail-open: never let the new leg break existing recall


def hybrid_query(text: str, k: int = DEFAULT_TOP_K, per_leg: int = DEFAULT_PER_LEG,
                 use_vector: bool = True, use_fts: bool = True, use_graph: bool = True,
                 use_edge_vector: bool = True, use_assertions: bool = True,
                 scope="self", rerank: bool = True, diversity: float = 0.0,
                 as_of: Optional[str] = None) -> List[dict]:
    """Hybrid retrieval.

    scope: 'self' (DEFAULT — this Core's own org_id only), 'all' (cross-Core, every org_id
    via the RLS read_all policy), or list[int] of explicit org_ids.

    THE DEFAULT CHANGED 2026-07-29, from 'all' to 'self'. This is not a new design decision —
    it implements the design that was already recorded in three places and never wired:
      * this repo's mcp-server.py module docstring: "all reads default to that Core's scope.
        Cross-Core reads are explicit via the `scope` parameter" — which contradicted the
        recall_similar tool docstring 120 lines below it, in the same file;
      * tasks/spec-multi-core-architecture-2026-05-19.md, Phase 7 (instance-only);
      * core-business's CLAUDE.md, which asserts an org filter the server never applied.

    Why it matters rather than being tidy: an unscoped recall swept five partitions —
    including workplace material on org 2 and brokerage data on org 4 — into every query on
    every Core, then shipped the winners into an API context. The Privacy Principle's first
    enforcement rule is "scoped queries only… no fishing"; a five-partition default is a
    fishing default. RLS read_all is AUTHORIZATION, and this principle operates above
    authorization (Apple Calendar reads are fully authorized and still require scoping).

    The honest cost, and the reason this was a real judgement call: the corpus is heavily
    skewed to org 1, which built the shared subsystems. A self-scoped peer asking about a
    Core-system topic can get a thin result, and in a system whose hooks push recall-before-
    responding, a silently-thin recall converts into "no prior decision exists" — an absence
    fabrication, which is the mirror image of the bug being fixed here. That is mitigated by
    the breadth hint in the return value (see below), NOT by leaving the default broad.

    The ASSERTIONS leg is always self-scoped regardless of this parameter — see its call site.

    rerank: when True (default), runs a cross-encoder (ms-marco-MiniLM-L-12-v2)
    over the fused top-(2k) candidates to refine ordering. Disable for latency-
    sensitive callers or when flashrank is unavailable.

    rerank: when True (default), runs a cross-encoder (ms-marco-MiniLM-L-12-v2)
    over the fused top-(2k) candidates to refine ordering. Disable for latency-
    sensitive callers or when flashrank is unavailable.

    diversity: 0.0 (default, OFF) to 1.0. When > 0, apply MMR over the fused
    candidate pool with lambda = 1 - diversity.

    as_of: None (default) → current state ("valid_until IS NULL" filter).
    ISO date/timestamp string → point-in-time. Returns rows that were active at
    as_of: valid_from <= as_of AND (valid_until IS NULL OR valid_until > as_of).
    Edges filtered by created_at <= as_of (entity_edges has no valid_until).
    """
    conn = connect()
    cur = conn.cursor()
    ranked: List[Tuple[str, List[Tuple[str, str, str, float]]]] = []

    # DEFECT 1 fix (2026-08-31, measured against the live corebrain with VOYAGE_API_KEY forcibly
    # unset): this call used to be unconditional — `client = voyage_client(); emb =
    # embed_query(...)` with no try/except — so a missing key sys.exit()'d the whole process
    # before FTS + graph BFS + assertions (none of which need a key) ever ran. Degrade instead:
    # catch VoyageUnavailable, drop both vector legs, and say so LOUDLY on stderr. Silent
    # degradation would be worse than the crash it replaces — this file's own leg_assertions
    # logic exists because an under-retrieved answer reads as "the fact is absent" rather than
    # "the fact was never looked for"; a silently-dropped leg is exactly that failure mode.
    emb: Optional[List[float]] = None
    if use_vector or use_edge_vector:
        try:
            client = voyage_client()
            emb = embed_query(client, text)
        except VoyageUnavailable as e:
            print(f"WARN: vector legs disabled ({e}) — recall continues on "
                  f"FTS + graph BFS + assertions only.", file=sys.stderr)
            use_vector = False
            use_edge_vector = False
    if use_vector:
        ranked.append(("vector", leg_vector(cur, emb, per_leg, scope=scope, as_of=as_of)))
    if use_fts:
        ranked.append(("fts", leg_fts(cur, text, per_leg, scope=scope, as_of=as_of)))
    if use_graph:
        ranked.append(("graph", leg_graph_bfs(cur, text, per_leg, scope=scope, as_of=as_of)))
    if use_edge_vector:
        ranked.append(("edge_vector", leg_edge_vector(cur, emb, per_leg, scope=scope, as_of=as_of)))
    # Current decisions (supersession-aware) — only meaningful for current-state recall (as_of None).
    # ASSERTIONS ARE ALWAYS SELF-SCOPED, regardless of the scope the content legs run under.
    #
    # 2026-07-29. This leg feeds the PRIORITY OVERLAY below, which stamps its hits
    # `current_decision: True` — an authority claim meaning "this is YOUR current, accepted,
    # active decision." That claim is only coherent for the calling Core's own partition. A peer's
    # decision record is context; it is never your current truth.
    #
    # This is the component that manufactured a fabricated security finding on 2026-07-28:
    # core-business ran an unscoped recall, a stale trust-root assertion from ANOTHER org's
    # partition came back flagged current_decision, business took it for its own live policy and
    # reported a security hole that did not exist. A full session went to chasing it. The recency
    # half of that bug (undated rows taking the maximum boost) is fixed separately; this is the
    # org half.
    #
    # Passing scope="self" here rather than filtering the overlay afterwards is deliberate: it
    # makes the invariant hold by construction, so a future caller cannot reintroduce the failure
    # by adding a new overlay path that forgets to filter.
    assertion_hits = leg_assertions(cur, text, per_leg, scope="self") if (use_assertions and as_of is None) else []
    if use_assertions:
        ranked.append(("assertions", assertion_hits))

    # Fuse — get a larger pool than k for any post-processing. Recency-intent
    # queries get a still-larger pool so recent-but-lower-relevance items are
    # present for the recency blend (D1 fix 2026-05-31).
    want_recency = _recency_intent(text)
    base_pool = max(2 * k, 20) if (rerank or diversity > 0) else k
    # Larger pool on recency intent so a recent-but-lower-relevance item is
    # present for the recency blend.
    fuse_k = max(base_pool, 4 * k, 40) if want_recency else base_pool
    fused = rrf_fuse(ranked, top_k=fuse_k)

    # MMR diversification (optional)
    if diversity > 0 and fused:
        embeddings = _fetch_embeddings_for_candidates(cur, fused)
        lambda_param = max(0.0, min(1.0, 1.0 - diversity))
        # Diversify down to a pool ~1.5x final k, then let rerank pick final k
        diverse_pool = max(int(k * 1.5), 10) if rerank else k
        fused = _mmr_diversify(fused, embeddings, top_k=diverse_pool, lambda_param=lambda_param)

    # Attach dates while the cursor is open (only needed for the recency blend).
    if want_recency and fused:
        _attach_candidate_dates(cur, fused, scope=scope, as_of=as_of)
    conn.close()

    if not fused:
        return []

    # Rank the full pool (recency blend / rerank), then collapse case-variant
    # duplicate results (D3) before cutting to k, so dedup never under-fills.
    if want_recency:
        pool = _rerank(text, fused, top_k=len(fused)) if rerank else fused
        ranked_out = _blend_recency(pool, len(pool))
    elif rerank:
        ranked_out = _rerank(text, fused, top_k=len(fused))
    else:
        ranked_out = fused
    result = _dedup_variants(ranked_out)[:k]

    # PRIORITY OVERLAY (unified redesign step ②): a strongly-matching current, accepted, active
    # decision-assertion surfaces FIRST — it is the authoritative current truth and must not be
    # buried under stale auto-extracted entity nodes (the recall-serves-stale bug this fixes).
    # Superseded assertions are already excluded upstream in leg_assertions. Capped + threshold so
    # it never floods; fail-safe (only prepends what leg_assertions actually returned).
    if assertion_hits:
        seen = {(r.get("kind"), r.get("source")) for r in result}
        overlay = []
        for hit in assertion_hits[:3]:
            kind, src, excerpt, score = hit
            if score < 0.05 or (kind, src) in seen:
                continue
            overlay.append({"kind": kind, "source": src, "excerpt": excerpt,
                            "rrf_score": float(score), "legs": ["assertions"], "current_decision": True})
            seen.add((kind, src))
        if overlay:
            result = (overlay + result)[:k]

    # WORKFLOW OVERLAY (Phase B3, 2026-08-05) — a Workflow hit returns its STEPS, IN ORDER.
    #
    # Before this, `entities.kind='Workflow'` rows came back as ordinary entity hits: a name and
    # whatever `compiled_truth_md` held, score-ranked like any other chunk. The whole point of the
    # Phase B migration was that a sequence is not a chunk, and recall was still flattening it into
    # one. `grep -c Workflow query.py` returned 0 — the retrieval layer had no idea the type existed.
    _attach_workflow_steps(result)
    return result


def grep_baseline(text: str, k: int = DEFAULT_TOP_K) -> List[dict]:
    """Baseline: TRUE grep over brain markdown files + simple recency rank.
    Used by the benchmark to compare hybrid RRF against the original
    grep-on-markdown recall path. No FTS, no embeddings — just shell
    `grep -rl` against $CORE_BRAIN.
    """
    import subprocess
    import os
    brain_root = os.environ.get("CORE_BRAIN")
    if not brain_root:
        return []
    brain_root = os.path.expanduser(brain_root)
    # Use grep -liE for case-insensitive, files-only, multi-pattern via |
    # Drop pure stop-words from query to avoid useless hits
    terms = [t for t in re.split(r"\W+", text) if len(t) > 2]
    if not terms:
        return []
    pattern = "|".join(re.escape(t) for t in terms)
    try:
        res = subprocess.run(
            ["grep", "-rliE", pattern, brain_root, "--include=*.md"],
            capture_output=True, text=True, timeout=30
        )
        files = res.stdout.strip().split("\n") if res.stdout.strip() else []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        files = []
    # Recency rank: newer files first by mtime
    files_with_mtime = []
    for f in files[:200]:  # cap for perf
        try:
            mt = os.path.getmtime(f)
            files_with_mtime.append((f, mt))
        except OSError:
            continue
    files_with_mtime.sort(key=lambda x: -x[1])
    results = []
    for f, _ in files_with_mtime[:k * 5]:
        try:
            with open(f, "r", errors="replace") as fp:
                first_line = fp.readline().strip()
        except OSError:
            first_line = ""
        results.append({"kind": "grep", "source": f, "excerpt": first_line,
                        "rrf_score": 0.0, "legs": ["grep"]})
    return results[:k]




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="+")
    ap.add_argument("--k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--per-leg", type=int, default=DEFAULT_PER_LEG)
    ap.add_argument("--no-vector", action="store_true",
                    help="disable the entity/evidence vector leg (needs VOYAGE_API_KEY)")
    # DEFECT 2 fix (2026-08-31): --no-vector alone never disabled the edge-vector leg, which
    # ALSO needs VOYAGE_API_KEY (hybrid_query constructs the Voyage client whenever
    # use_vector OR use_edge_vector is true) — so the documented "--no-vector ... # disable
    # vector leg" escape hatch could not, in fact, avoid the key. Added as its own flag rather
    # than folded into --no-vector because eval.py's per-leg ablation table already needs the
    # two controlled independently (use_vector and use_edge_vector are toggled separately
    # there). A missing key now degrades both legs automatically regardless of these flags
    # (see voyage_client()/VoyageUnavailable in hybrid_query) — these remain for explicit or
    # benchmark control when a key IS present.
    ap.add_argument("--no-edge-vector", action="store_true",
                    help="disable the edge-vector leg (needs VOYAGE_API_KEY)")
    ap.add_argument("--no-fts", action="store_true")
    ap.add_argument("--no-graph", action="store_true")
    ap.add_argument("--baseline", action="store_true", help="FTS+graph only (grep+BFS proxy)")
    ap.add_argument("--json", action="store_true")
    # --scope added 2026-07-29, in the SAME change that flipped the default to 'self'.
    # Without it the CLI would have been permanently self-scoped with no widening path, which
    # would make /recall-similar and the claude-brain skill WRONG rather than merely narrower —
    # both call this CLI, not the MCP tool. Accepts 'self', 'all', or a comma-separated list of
    # org NAMES or ids ("business,school" / "2,3").
    ap.add_argument("--scope", default="self",
                    help="self (default) | all | comma-separated org names or ids")
    args = ap.parse_args()

    def _parse_scope(s: str):
        s = (s or "self").strip()
        if s in ("self", "all"):
            return s
        parts = [p.strip() for p in s.split(",") if p.strip()]
        out = []
        for p in parts:
            if p.isdigit():
                out.append(int(p))
            else:
                oid = _org_id_for_name(p)
                if oid is None:
                    ap.error(f"unknown org name in --scope: {p!r}")
                out.append(oid)
        return out

    qtext = " ".join(args.query)
    try:
        if args.baseline:
            results = grep_baseline(qtext, k=args.k)
        else:
            results = hybrid_query(qtext, k=args.k, per_leg=args.per_leg,
                                   use_vector=not args.no_vector,
                                   use_fts=not args.no_fts,
                                   use_graph=not args.no_graph,
                                   use_edge_vector=not args.no_edge_vector,
                                   scope=_parse_scope(args.scope))
    except psycopg2.OperationalError as e:
        # NOT "unreachable" unconditionally. QueryCanceled (the statement_timeout _env.py sets) is
        # an OperationalError subclass, so a SLOW query landed here and was reported as a DOWN
        # DATABASE — with a hint pointing at a service that was running. core-finance hit this as
        # an operator: query.py is what the recall-first gate requires, so the misattribution
        # turned a slow query into a blocked write. describe_db_failure names the real cause and
        # the override.
        print(f"ERROR: {describe_db_failure(e)}", file=sys.stderr)
        print("Fall back to grep if this persists.", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for i, r in enumerate(results, 1):
            legs = " ".join(r["legs"])
            print(f"[{i}] {r['kind']}  {r['source']}  rrf={r['rrf_score']}  legs={legs}")
            if r["excerpt"]:
                first = r["excerpt"].splitlines()[0] if r["excerpt"] else ""
                print(f"    {first}")


if __name__ == "__main__":
    main()
