#!/usr/bin/env python3
"""Embed brain content into corebrain Postgres.

Reads (paths anchored at $CORE_BRAIN env var; script exits with an error if unset):
  - $CORE_BRAIN/entities/*.md                       → entities rows
  - $CORE_BRAIN/topics/*.md                         → entities rows
  - $CORE_BRAIN/projects/*/sessions/*.md            → evidence
  - $CORE_BRAIN/projects/*/subagents/*.md           → evidence
  - $CORE_BRAIN/_build/output/graphify-out/graph.json   → edges

Voyage AI embeddings (voyage-3-large, 1024d). Batched; incremental via ingest_log.mtime.
Source files are CANONICAL — Postgres is index, not truth.

Usage:
  python3 embed.py                  # full pass over all sources
  python3 embed.py --incremental    # only re-embed files whose mtime > ingest_log.last_mtime
  python3 embed.py --dry-run        # parse + count, no Voyage calls, no DB writes
  python3 embed.py --hubs-only      # only hub files (entities table), skip evidence

Env: VOYAGE_API_KEY required. Canonical location: ~/.claude/secrets.env (loaded
automatically via _env.py at module init). Falls back to interactive shell env
if the file is missing.
"""
from __future__ import annotations
import argparse
import atexit
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import psycopg2
import psycopg2.extras
import voyageai

# Load ~/.claude/secrets.env into os.environ before any env-var lookup.
# Handles the bash-subprocess case where zshenv was never sourced (Stop-hook
# context, launchd GUI context). No-op if file missing or keys already set.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import load_secrets, get_org_id, connect_corebrain, connect_corebrain_admin, path_to_org_id  # noqa: E402
load_secrets()

_BRAIN_ENV = os.environ.get("CORE_BRAIN")
if not _BRAIN_ENV:
    sys.exit("ERROR: $CORE_BRAIN env var required. Set it before invoking embed.py "
             "(e.g., export CORE_BRAIN=\"$HOME/AI Projects/<your-brain-dir>\").")
BRAIN_ROOT = Path(_BRAIN_ENV)
GRAPH_JSON = BRAIN_ROOT / "_build" / "output" / "graphify-out" / "graph.json"

VOYAGE_MODEL = "voyage-3-large"   # 1024-dim, matches schema
EMBED_DIM = 1024
HUB_BATCH_SIZE = 32                # hubs are small (~1K tokens avg)
EVIDENCE_BATCH_SIZE = 4            # truncate_for_embed caps each input at ~24K tokens; 4 * 24K = 96K — under voyage 120K/batch cap
MAX_CHARS_PER_INPUT = 80000        # ~27K tokens at 3 chars/token (dense-text floor) — headroom under voyage-3-large's 32K per-input limit (Codex round: 96000 sat AT ~32K on dense content)

# --- Brain write lock (unified blocking protocol, 2026-07-24) ----------------
# embed.py mutates the shared corebrain DB (entities/evidence/ingest_log). When
# invoked through the Stop-hook (run-brain-update.sh) the shared brain lock is
# already held, and the hook exports BRAIN_LOCK_HELD=1 so we skip re-locking. A
# DIRECT `embed.py` invocation self-acquires the SAME lock dir, speaking the
# SAME protocol as run-brain-update.sh (one protocol, two implementations —
# keep them in lock-step):
#   • BLOCKING QUEUE, no timeout: wait for a live holder however long it takes
#     (Nick 2026-07-24: closes queue until everyone is done; never fail-on-slow).
#   • Holder identity: holder.pid + holder.cmd (`ps -o command=`) written inside
#     the lock dir. A waiter reclaims ONLY a provably-gone holder: pid dead, or
#     pid alive under a different command line (recycled pid).
#   • Reclaim is ATOMIC-RENAME (os.rename to a waiter-private name): of N racing
#     waiters exactly one wins; nobody deletes a successor's fresh live lock.
#   • LEGACY COMPAT (until fleet push): pid-less lock dirs (old peers) fall back
#     to the legacy 600s-mtime staleness rule; our holder touches the dir every
#     60s so old peers' mtime reclaim can't kill a live new-style holder.
_BRAIN_LOCK_DIR: "Path | None" = None
_BRAIN_LOCK_STALE_AFTER = 600     # pid-less (legacy-style) locks only
_BRAIN_LOCK_TOUCHER = None


def _brain_lock_path() -> Path:
    # `echo "$CORE_BRAIN" | md5` in the shell hashes the path PLUS a trailing
    # newline — match that byte-for-byte so both compute the same lock dir.
    digest = hashlib.md5((_BRAIN_ENV + "\n").encode()).hexdigest()
    return Path(f"/tmp/core-brain-{digest}.lock")


def _proc_cmd(pid: int) -> "str | None":
    """Command line of a live pid via ps, or None if the pid is dead."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out or None
    except Exception:
        return None


def _atomic_reclaim(lock: Path) -> None:
    """Rename-then-delete: rename is atomic, so of N racing waiters exactly one
    wins; losers get OSError and loop back to mkdir."""
    import shutil
    corpse = Path(f"{lock}.reap.{os.getpid()}")
    try:
        os.rename(lock, corpse)
    except OSError:
        return
    shutil.rmtree(corpse, ignore_errors=True)


def _tree_pids(root: int) -> "list[int]":
    """Snapshot root + ALL descendant pids in one walk (the holder shell's CHILDREN are the real
    DB writers). Codex round 2: snapshot ONCE and signal that exact set — don't re-walk between
    TERM and KILL, or a TERM-survivor reparented to launchd is missed."""
    import subprocess
    pids = [root]
    try:
        kids = subprocess.run(["pgrep", "-P", str(root)],
                              capture_output=True, text=True, timeout=5).stdout.split()
    except Exception:
        kids = []
    for k in kids:
        try:
            pids.extend(_tree_pids(int(k)))
        except ValueError:
            pass
    return pids


def acquire_brain_lock():
    """Blocking-queue acquire. No-op if a parent (run-brain-update.sh) already
    holds the lock and signals BRAIN_LOCK_HELD=1."""
    global _BRAIN_LOCK_DIR, _BRAIN_LOCK_TOUCHER
    if os.environ.get("BRAIN_LOCK_HELD"):
        return
    lock = _brain_lock_path()
    logged_wait = False
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            pass
        holder_pid = None
        try:
            holder_pid = int((lock / "holder.pid").read_text().strip())
        except (OSError, ValueError):
            pass
        if holder_pid is not None:
            try:
                recorded = (lock / "holder.cmd").read_text().strip()
            except OSError:
                recorded = ""
            live = _proc_cmd(holder_pid)
            if live is not None and (not recorded or live == recorded):
                # BACKSTOP (mirrors run-brain-update.sh): a holder wedged past the backstop is
                # killed + reclaimed. Last-resort only — never fires on normal slowness; reads
                # holder.started (immune to the toucher) so it measures true hold-time.
                started = None
                try:
                    started = int((lock / "holder.started").read_text().strip())
                except (OSError, ValueError):
                    started = None
                backstop = int(os.environ.get("BRAIN_LOCK_BACKSTOP", "3600"))
                if started is not None and time.time() - started > backstop:
                    import signal
                    # Re-verify identity immediately before killing (close the check→kill PID-reuse
                    # window): only kill if this pid is STILL the recorded holder with a matching cmd.
                    try:
                        still_holder = (lock / "holder.pid").read_text().strip() == str(holder_pid)
                    except OSError:
                        still_holder = False
                    recheck = _proc_cmd(holder_pid)
                    if still_holder and recheck is not None and (not recorded or recheck == recorded):
                        print(f"embed.py: BACKSTOP — holder pid={holder_pid} wedged >{backstop}s — killing process TREE + reclaiming",
                              file=sys.stderr)
                        # Snapshot the tree once; TERM the set, then KILL the SAME set (a reparented
                        # TERM-survivor stays in the snapshot — Codex round 2 Critical).
                        tree = _tree_pids(holder_pid)
                        for _sig in (signal.SIGTERM, signal.SIGKILL):
                            for p in tree:
                                try:
                                    os.kill(p, _sig)
                                except OSError:
                                    pass
                            time.sleep(2)
                        _atomic_reclaim(lock)
                    continue
                if not logged_wait:
                    print(f"embed.py: brain lock held by live pid={holder_pid} — queuing (blocking, no timeout)",
                          file=sys.stderr)
                    logged_wait = True
                time.sleep(3)
                continue
            _atomic_reclaim(lock)      # pid dead, or recycled to another command
            continue
        # pid-less: legacy-style holder (old peer) or an acquirer dead in the
        # mkdir→write window → legacy 600s mtime rule for exactly this class.
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            continue                    # lock vanished between checks → retry mkdir
        if age > _BRAIN_LOCK_STALE_AFTER:
            _atomic_reclaim(lock)
            continue
        if not logged_wait:
            print(f"embed.py: pid-less (legacy) brain lock in flight ({int(age)}s) — queuing",
                  file=sys.stderr)
            logged_wait = True
        time.sleep(3)
    # We OWN the lock: record identity, sweep orphaned corpses, start the mtime
    # toucher (compat shim vs old peers' 600s reclaim; retire with the fleet push).
    try:
        # started FIRST (Codex round): pid present ⇒ started present, so a wedge after pid can't
        # leave the backstop permanently unable to fire.
        (lock / "holder.started").write_text(str(int(time.time())))
        (lock / "holder.pid").write_text(str(os.getpid()))
        own_cmd = _proc_cmd(os.getpid())
        (lock / "holder.cmd").write_text(own_cmd or "")
    except OSError:
        pass
    import glob as _glob
    import shutil as _shutil
    for corpse in _glob.glob(f"{lock}.reap.*"):
        _shutil.rmtree(corpse, ignore_errors=True)
    import threading
    _stop = threading.Event()

    def _touch_loop():
        while not _stop.wait(60):
            try:
                os.utime(lock)
            except OSError:
                return
    _t = threading.Thread(target=_touch_loop, daemon=True)
    _t.start()
    _BRAIN_LOCK_TOUCHER = _stop
    _BRAIN_LOCK_DIR = lock
    atexit.register(_release_brain_lock)


def _release_brain_lock():
    import shutil
    if _BRAIN_LOCK_TOUCHER is not None:
        _BRAIN_LOCK_TOUCHER.set()
    if _BRAIN_LOCK_DIR is not None:
        # Ownership check: only remove the lock if it is still OURS — a lock
        # recreated after a reclaim belongs to a successor.
        try:
            still_ours = (_BRAIN_LOCK_DIR / "holder.pid").read_text().strip() == str(os.getpid())
        except OSError:
            still_ours = False
        if still_ours:
            shutil.rmtree(_BRAIN_LOCK_DIR, ignore_errors=True)


def connect():
    """Connect for embed/ingestion: brain_admin (BYPASSRLS) so we can write
    rows tagged with the SOURCE FILE's org_id, not the calling Core's.

    Recall-side scripts continue to use brain_app via connect_corebrain().
    """
    return connect_corebrain_admin()


def voyage_client():
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        sys.exit("ERROR: VOYAGE_API_KEY missing. Set it in ~/.claude/secrets.env "
                 "(canonical location) — file is loaded automatically by _env.py.")
    return voyageai.Client(api_key=key)


# --- Source-file discovery + parsing ---------------------------------------

from hub_ownership import owner_for  # THE shared ownership rule — see that module's header


def _hub_owner(parsed: dict) -> "int | None":
    """Owning org for a parsed hub, or None when it cannot be derived from the file."""
    try:
        return owner_for(parsed["name"], parsed["body"])
    except Exception:
        return None


HUB_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)

def parse_hub_md(path: Path) -> dict:
    """Return {name, kind, body} from a hub markdown file."""
    text = path.read_text(errors="replace")
    m = HUB_FRONTMATTER_RE.match(text)
    fm, body = {}, text
    if m:
        fm_raw, body = m.group(1), m.group(2)
        for line in fm_raw.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip()
    name = fm.get("name") or path.stem.replace("-", " ")
    kind_raw = fm.get("type", "entity").lower()
    # Map graphify types to schema kinds
    kind_map = {
        "entity": "Entity", "person": "Entity",
        "topic": "Topic",
        "project": "Project",
        "tool": "Tool",
        "decision": "Decision", "lesson": "Lesson", "rule": "Rule", "incident": "Incident",
    }
    kind = kind_map.get(kind_raw, "Topic" if path.parent.name == "topics" else "Entity")
    return {"name": name, "kind": kind, "body": body.strip(), "source_file": str(path)}


def discover_hubs() -> List[Path]:
    # tools/ was NEVER ingested. This globbed entities/ + topics/ only, so 1,447 tool hub files
    # existed in the vault and in no Core's brain — measured 2026-08-31: zero entities rows cite a
    # tools/ path. An entire tier of the vault was invisible to recall for the life of the system.
    return (sorted((BRAIN_ROOT / "entities").glob("*.md"))
            + sorted((BRAIN_ROOT / "topics").glob("*.md"))
            + sorted((BRAIN_ROOT / "tools").glob("*.md")))


def discover_evidence_files() -> List[Path]:
    paths = []
    for proj_dir in (BRAIN_ROOT / "projects").iterdir():
        if not proj_dir.is_dir():
            continue
        for sub in ("sessions", "subagents"):
            d = proj_dir / sub
            if d.exists():
                paths.extend(sorted(d.glob("*.md")))
    return paths


SESSION_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

def extract_session_date(path: Path) -> Optional[str]:
    m = SESSION_DATE_RE.search(path.name)
    return m.group(1) if m else None


# --- Ingest-log + incremental gating ---------------------------------------

def _content_hash(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def needs_reembed(cur, path: Path) -> bool:
    """Two-tier gate: fast mtime+size match, then content hash (2026-07-26).

    The old mtime-only gate re-embedded ~2,400 byte-identical files through
    Voyage after a mass mtime-bump (vault rewrites at export/close touch files
    whose content didn't change) — real API spend for identical vectors. Now a
    mtime/size mismatch only re-embeds if the CONTENT hash actually differs;
    identical bytes refresh the ledger row for free and skip.

    Scope: any-org (no org_id filter) — brain_admin sees all rows. This
    handles the case where a file was previously ingested under the wrong
    org_id; we still consider it "embedded" by content match. If you
    need to force re-tag, DELETE the stale ingest_log row first.
    """
    st = path.stat()
    cur.execute("SELECT last_mtime, last_size, content_hash FROM ingest_log WHERE source_file = %s",
                (str(path),))
    row = cur.fetchone()
    if row is None:
        return True
    last_mtime, last_size, last_hash = row
    current_mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    if current_mtime <= last_mtime and st.st_size == last_size:
        return False  # untouched — fast path, no file read
    if last_hash and _content_hash(path) == last_hash:
        # touched but byte-identical — refresh ledger so next run fast-paths; no Voyage spend
        cur.execute("UPDATE ingest_log SET last_mtime = %s, last_size = %s WHERE source_file = %s",
                    (current_mtime, st.st_size, str(path)))
        return False
    return True  # new content (or legacy row with no hash yet — embeds once, then converges)


def update_ingest_log(cur, path: Path, chunks=1, nodes=0, edges=0, org_id: int | None = None):
    """Upsert ingest_log row. org_id derived from path if not passed."""
    st = path.stat()
    if org_id is None:
        org_id = path_to_org_id(path)
    cur.execute("""
        INSERT INTO ingest_log (source_file, last_mtime, last_size, content_hash, chunks_extracted, nodes_emitted, edges_emitted, org_id, embedded_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (source_file) DO UPDATE SET
          last_mtime = EXCLUDED.last_mtime,
          last_size = EXCLUDED.last_size,
          content_hash = EXCLUDED.content_hash,
          chunks_extracted = EXCLUDED.chunks_extracted,
          nodes_emitted = EXCLUDED.nodes_emitted,
          edges_emitted = EXCLUDED.edges_emitted,
          org_id = EXCLUDED.org_id,
          embedded_at = now()
    """, (str(path), datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), st.st_size,
          _content_hash(path), chunks, nodes, edges, org_id))


# --- Voyage batching --------------------------------------------------------

def truncate_for_embed(text: str) -> str:
    return text[:MAX_CHARS_PER_INPUT] if text else " "


def embed_batch(client: voyageai.Client, texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    safe = [truncate_for_embed(t) for t in texts]
    # PROACTIVE SPLIT (2026-07-24): keep every request safely under voyage-3-large's 120K-token/
    # batch cap. The fixed batch size assumed ~4 chars/token, but dense text runs ~3 — so a 4-input
    # batch could hit ~122K tokens, over the cap. That triggered a deterministic error → 5 retries
    # with exponential backoff (~31s wasted) → halve, PER oversized batch, making a full evidence
    # embed take ~2h. Estimate tokens conservatively (chars/3) and split BEFORE the API call so
    # requests succeed first try. Content-adaptive — no magic batch number to drift out of date.
    if len(safe) > 1 and sum(len(t) for t in safe) / 3.0 > 100_000:
        mid = len(texts) // 2
        return embed_batch(client, texts[:mid]) + embed_batch(client, texts[mid:])
    last_err = None
    for attempt in range(5):
        try:
            r = client.embed(safe, model=VOYAGE_MODEL, input_type="document")
            return r.embeddings
        except Exception as e:
            last_err = e
            err_str = str(e)[:200]
            low = err_str.lower()
            # A token/size-limit error is DETERMINISTIC — retrying the same oversized batch can
            # never succeed. Halve immediately instead of burning 5 backoff cycles. Retries are
            # reserved for genuinely transient errors (rate-limit, network, 5xx).
            if len(texts) > 1 and (("token" in low and "batch" in low) or "max allowed" in low):
                print(f"  voyage size-limit on batch of {len(texts)} — splitting immediately (no retry waste)")
                mid = len(texts) // 2
                return embed_batch(client, texts[:mid]) + embed_batch(client, texts[mid:])
            wait = 2 ** attempt
            print(f"  voyage error (attempt {attempt+1}/5): {type(e).__name__}: {err_str}; retry in {wait}s")
            time.sleep(wait)
    # Fall back: try halving the batch
    if len(texts) > 1:
        print(f"  batch of {len(texts)} failed; trying halved sub-batches")
        mid = len(texts) // 2
        return embed_batch(client, texts[:mid]) + embed_batch(client, texts[mid:])
    # SINGLE token-dense item that keeps failing (Codex round 2 Medium): a char cap can't bound
    # emoji/CJK/adversarial text under voyage's 32K-token per-input limit, and a 1-item batch can't
    # be split. Progressively truncate until it fits — guarantees termination + a real embedding
    # (a truncated embedding is far better than dropping the node). Floor at 2K chars.
    t = safe[0]
    while len(t) > 2000:
        t = t[: len(t) // 2]
        try:
            return client.embed([t], model=VOYAGE_MODEL, input_type="document").embeddings
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Voyage embed failed after 5 retries + progressive truncation on single item: {last_err}")


def vec_literal(emb: List[float]) -> str:
    """pgvector accepts a text literal like '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in emb) + "]"


# --- Upserts ----------------------------------------------------------------

def upsert_entities(cur, rows: List[Tuple], overwrite_truth_for_graph_kinds: bool = False):
    """rows: [(name, kind, source_file, body, embedding_literal, org_id), ...]

    org_id is now PER-ROW (Phase 1B multi-Core ownership): hubs default to the
    calling Core's org (global content), but graph nodes carry the org of their
    originating source file so business/school entities land under org 2/3 rather
    than being collapsed to whichever Core ran the heavy build. brain_admin
    (BYPASSRLS) connection required for the cross-org writes.

    overwrite_truth_for_graph_kinds (C4, 2026-07-23, Codex #2 fix): when True (the graph-nodes pass
    only), a CHANGED row of a graph-ONLY kind (Decision/Lesson/Rule/Incident — these are never
    LLM-synthesized hubs) UPDATES its compiled_truth_md, so revised knowledge (e.g. a Rule whose excerpt
    flipped from "always X" to "never X") reaches recall instead of keeping stale text. Hub-eligible kinds
    (Entity/Topic/Project/Tool) stay COALESCE-protected either way, so a raw graph excerpt can never clobber
    the LLM-synthesized hub truth (`--hubs-only` runs first and owns that text). Default False keeps the
    prior COALESCE-everything behavior for every other caller (pass_hubs, etc.).
    """
    if not rows:
        return
    if overwrite_truth_for_graph_kinds:
        # kinds list is a fixed hardcoded literal (no user input) → safe to inline.
        truth_clause = ("compiled_truth_md = CASE WHEN entities.kind IN "
                        "('Decision','Lesson','Rule','Incident') THEN EXCLUDED.compiled_truth_md "
                        "ELSE COALESCE(entities.compiled_truth_md, EXCLUDED.compiled_truth_md) END")
    else:
        truth_clause = "compiled_truth_md = COALESCE(entities.compiled_truth_md, EXCLUDED.compiled_truth_md)"
    psycopg2.extras.execute_values(cur, f"""
        INSERT INTO entities (name, kind, source_file, compiled_truth_md, embedding, valid_from, org_id)
        VALUES %s
        ON CONFLICT (org_id, kind, name) DO UPDATE SET
          source_file = EXCLUDED.source_file,
          {truth_clause},
          embedding = EXCLUDED.embedding,
          updated_at = now()
    """, [(name, kind, sf, body, emb, datetime.now(timezone.utc), org_id)
          for (name, kind, sf, body, emb, org_id) in rows],
       template="(%s, %s, %s, %s, %s::vector, %s, %s)")


def upsert_evidence(cur, rows: List[Tuple]):
    """rows: [(entity_id_or_None, source_file, excerpt, session_date, chunk_id, embedding_literal), ...]

    org_id is derived per row from source_file via path_to_org_id() — peer-Core
    session files get tagged with their own org_id (2=business, 3=school), not
    the calling Core's. Connection must be brain_admin (BYPASSRLS) for the
    cross-org writes to pass RLS.
    """
    if not rows:
        return
    psycopg2.extras.execute_values(cur, """
        INSERT INTO evidence (entity_id, source_file, excerpt, session_date, chunk_id, embedding, valid_from, org_id)
        VALUES %s
    """, [(eid, sf, excerpt, sd, cid, emb, datetime.now(timezone.utc), path_to_org_id(sf))
          for (eid, sf, excerpt, sd, cid, emb) in rows],
       template="(%s, %s, %s, %s, %s, %s::vector, %s, %s)")


def delete_stale_evidence(cur, source_file: str):
    """When a source file is re-ingested, drop its prior evidence rows.

    Uses the source_file's natural org_id (path-derived) — the prior row was
    tagged with the same org_id (because path → org_id is deterministic), so
    a single-key WHERE matches.
    """
    cur.execute(
        "DELETE FROM evidence WHERE source_file = %s AND org_id = %s",
        (source_file, path_to_org_id(source_file)),
    )


# --- Main passes ------------------------------------------------------------

def pass_hubs(conn, client, incremental: bool, dry_run: bool):
    cur = conn.cursor()
    hubs = discover_hubs()
    print(f"[hubs] {len(hubs)} files discovered")
    targets = []
    for h in hubs:
        if incremental and not needs_reembed(cur, h):
            continue
        targets.append(h)
    print(f"[hubs] {len(targets)} need (re)embed")
    if dry_run:
        for h in targets[:5]:
            parsed = parse_hub_md(h)
            print(f"  DRY  {h.name}  kind={parsed['kind']}  body_chars={len(parsed['body'])}")
        return

    parsed_rows = [parse_hub_md(h) for h in targets]

    # PER-HUB OWNERSHIP, DERIVED (2026-08-31). This was `hub_org = get_org_id()` with the comment
    # "hubs are global content owned by the running Core" — so the org column recorded which Core
    # happened to run the pass, and every Core ingested all ~7,661 flat hubs into its own partition.
    # 61% of the OPS Core's entities arrived this way, which is why the highest-degree hubs in a
    # business Core's partition were the operator's PERSONAL entities, not the business's.
    #
    # The rule lives in hub_ownership.py and is shared with bin/repartition-hubs.py deliberately: if
    # the ingester and the repair tool disagreed by even one case, each pass would recreate what the
    # other removed and the oscillation would read as corruption rather than as two copies of one
    # rule drifting apart.
    #
    # Falls back to the running Core only when ownership is genuinely underivable, which preserves
    # the old behaviour for exactly the files the old behaviour was ever right about.
    running_org = get_org_id()
    for i in range(0, len(parsed_rows), HUB_BATCH_SIZE):
        batch = parsed_rows[i:i+HUB_BATCH_SIZE]
        texts = [f"{r['name']}\n\n{r['body']}" for r in batch]
        embs = embed_batch(client, texts)
        rows = [(r["name"], r["kind"], r["source_file"], r["body"], vec_literal(e),
                 _hub_owner(r) or running_org)
                for r, e in zip(batch, embs)]
        upsert_entities(cur, rows)
        for r in batch:
            update_ingest_log(cur, Path(r["source_file"]), chunks=1, nodes=1)
        conn.commit()
        print(f"  [hubs] batch {i//HUB_BATCH_SIZE+1}: {len(batch)} embedded + upserted")


def pass_evidence(conn, client, incremental: bool, dry_run: bool):
    cur = conn.cursor()
    files = discover_evidence_files()
    print(f"[evidence] {len(files)} session/subagent files discovered")
    targets = []
    for f in files:
        if incremental and not needs_reembed(cur, f):
            continue
        targets.append(f)
    print(f"[evidence] {len(targets)} need (re)embed")
    if dry_run:
        for f in targets[:5]:
            text = f.read_text(errors="replace")
            print(f"  DRY  {f.name}  size={len(text)}  date={extract_session_date(f)}")
        return

    for i in range(0, len(targets), EVIDENCE_BATCH_SIZE):
        batch = targets[i:i+EVIDENCE_BATCH_SIZE]
        # Postgres text columns reject NUL (0x00); strip before embed + upsert.
        excerpts = [t.read_text(errors="replace").replace("\x00", "") for t in batch]
        embs = embed_batch(client, excerpts)
        rows = []
        for f, text, e in zip(batch, excerpts, embs):
            sd = extract_session_date(f)
            chunk_id = f"file:{f.name}"
            rows.append((None, str(f), text[:MAX_CHARS_PER_INPUT], sd, chunk_id, vec_literal(e)))
            delete_stale_evidence(cur, str(f))
        upsert_evidence(cur, rows)
        for f in batch:
            update_ingest_log(cur, f, chunks=1)
        conn.commit()
        print(f"  [evidence] batch {i//EVIDENCE_BATCH_SIZE+1}/{(len(targets)+EVIDENCE_BATCH_SIZE-1)//EVIDENCE_BATCH_SIZE}: {len(batch)} embedded + upserted")


def pass_edges(conn, dry_run: bool):
    """Load graph.json edges where BOTH endpoints exist as entities. Skip on dry-run.

    Graph.json edges reference nodes by node.id (e.g., 'rule_foo_bar'), not by label.
    Entities table stores nodes by label (e.g., 'foo bar is a rule'). Bridge via a
    two-level lookup: node.id → node.label → entity.id.
    """
    cur = conn.cursor()
    if not GRAPH_JSON.exists():
        print(f"[edges] {GRAPH_JSON} missing — skipping edges pass")
        return
    with open(GRAPH_JSON) as fp:
        g = json.load(fp)
    print(f"[edges] graph has {len(g.get('edges', []))} edges; loading into entity_edges...")
    if dry_run:
        return

    # name.lower() -> {org_id: {kind: entity_id}}. brain_admin bypasses RLS so
    # this sees ALL orgs. Live nodes only.
    #
    # FIX 2026-07-04 (brain-connectivity Phase 1): the prior resolver kept a
    # single global name->id map (lowercased name, last-write-wins). For a name
    # that exists in several orgs/kinds ("sentinel" = 11 rows), EVERY edge
    # collapsed onto one arbitrary survivor and org 1 (life) systematically won,
    # starving the smaller Cores (business hit 90% isolated). We now index by
    # (name, org, kind) and materialize each edge in EVERY org that owns the
    # source node, wiring to the same-org target (or the shared org-1 copy for
    # Topic/Tool). Edges only, against EXISTING nodes — never creates cross-org
    # nodes (that caused the 12,165-node pollution the locked ontology fixed).
    cur.execute("SELECT id, kind, name, org_id FROM entities WHERE valid_until IS NULL")
    name_index: dict = {}
    for _id, _kind, _name, _org in cur.fetchall():
        name_index.setdefault(_name.lower(), {}).setdefault(_org, {})[_kind] = _id
    print(f"[edges] indexed {len(name_index)} distinct names across orgs")

    nodes = g.get("nodes", [])
    nodeid_to_label = {n["id"]: n.get("label", n["id"]) for n in nodes}
    nodeid_to_kind = {n["id"]: GRAPH_NODE_KIND_MAP.get(n.get("type", "")) for n in nodes}

    def candidates(ref: str) -> dict:
        """Resolve a graph node ref to {org_id: entity_id} — one entity per org,
        preferring the graph node's kind, falling back to any kind of that name."""
        if not ref:
            return {}
        label = nodeid_to_label.get(ref, ref)
        want_kind = nodeid_to_kind.get(ref)
        variants = []
        for v in (label.lower(), ref.lower(), ref.replace("_", " ").lower(), ref.replace("_", "-").lower()):
            if v and v not in variants:
                variants.append(v)
        for v in variants:  # canonical label first; first matching variant wins
            orgmap = name_index.get(v)
            if not orgmap:
                continue
            out = {}
            for org, kinds in orgmap.items():
                out[org] = kinds[want_kind] if (want_kind and want_kind in kinds) else next(iter(kinds.values()))
            return out
        return {}

    edge_type_map = {
        "motivated_by": "motivated_by",
        "learned_from": "learned_from",
        "supersedes": "supersedes",
        "cross_impacts": "cross_impacts",
        "references": "references",
        # Graphify-emitted relation names
        "addresses": "motivated_by", "implements": "references",
        "depends_on": "cross_impacts", "supports": "references",
        "contradicts": "cross_impacts", "follows": "supersedes",
        "RELATED_TO": "references", "MENTIONS": "references",
        "PART_OF": "references", "DEPENDS_ON": "cross_impacts",
        "FOLLOWS": "supersedes", "CITED_BY": "references",
    }

    seen = set()
    edges_in = []
    skipped = 0
    for e in g.get("edges", []):
        src_ref = e.get("source") or e.get("from") or ""
        tgt_ref = e.get("target") or e.get("to") or ""
        # graphify uses "relation" field; older variants use type/label
        etype_raw = e.get("relation") or e.get("type") or e.get("label") or "references"
        etype = edge_type_map.get(etype_raw, "references")
        src_c = candidates(src_ref)
        tgt_c = candidates(tgt_ref)
        if not src_c or not tgt_c:
            skipped += 1
            continue
        conf = e.get("properties", {}).get("confidence_score") or e.get("confidence") or None
        clabel = e.get("properties", {}).get("confidence") or None
        if clabel not in ("EXTRACTED", "INFERRED", "AMBIGUOUS", "NONE", None):
            clabel = None
        # Materialize in every org that OWNS the source node. Target = same-org
        # copy if it exists, else the shared org-1 copy (Topic/Tool are reachable
        # from all orgs via read_all), else best-effort. Edge lives in source's org.
        for s_org, s_id in src_c.items():
            if s_org in tgt_c:
                t_id = tgt_c[s_org]
            elif 1 in tgt_c:
                t_id = tgt_c[1]
            else:
                t_id = next(iter(tgt_c.values()))
            if s_id == t_id:
                continue  # no self-loops
            key = (s_id, t_id, etype)
            if key in seen:
                continue
            seen.add(key)
            edges_in.append((s_id, t_id, etype, conf, clabel, s_org))

    if edges_in:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO entity_edges (from_entity_id, to_entity_id, edge_type, confidence, confidence_label, org_id)
            VALUES %s
            ON CONFLICT (from_entity_id, to_entity_id, edge_type) DO NOTHING
        """, edges_in)
        conn.commit()
    print(f"[edges] resolved {len(edges_in)} org-aware edge-rows, skipped {skipped} (endpoint unknown)")


# --- Graph-nodes pass: densify entities from graph.json ---------------------
# Per spec-graph-leg-densification-2026-05-17.md.

# graphify node.type → entities.kind (schema CHECK constraint: Topic|Tool|Entity|Project|Decision|Lesson|Rule|Incident)
GRAPH_NODE_KIND_MAP = {
    "topic": "Topic", "tool": "Tool", "entity": "Entity", "project": "Project",
    "person": "Entity", "Decision": "Decision", "Lesson": "Lesson",
    "Rule": "Rule", "Incident": "Incident",
    # Schema-absent types mapped to nearest legal kind:
    "Hypothesis": "Entity", "Tradeoff": "Decision",
    "code_location": "Entity", "ui_component": "Entity",
    "event": "Entity", "task": "Entity", "file": "Entity",
    "framework": "Tool", "language": "Tool", "model": "Tool",
    "inferred": "Entity",
}
# Types we deliberately SKIP (sessions/subagents are evidence, not entities)
GRAPH_NODE_SKIP_TYPES = {"session", "subagent_session", "session_arc"}
# C4 (2026-07-23, Codex #2): kinds that are ONLY ever graph nodes (never LLM-synthesized hubs). For these,
# a changed body is safe to re-upsert (truth+embedding) because no hub owns the row. Hub-eligible kinds
# (Entity/Topic/Project/Tool) are deliberately excluded — hubs own their truth, graph excerpts must not clobber it.
GRAPH_ONLY_TRUTH_KINDS = {"Decision", "Lesson", "Rule", "Incident"}


def pass_graph_nodes(conn, client, dry_run: bool):
    """Embed graphify nodes (beyond hubs) as entities so entity_edges resolves.
    Skips session-like types. Uses node.properties.source_excerpt as raw
    compiled_truth_md (NOT LLM-synthesized — that's the hub-only pattern)."""
    cur = conn.cursor()
    if not GRAPH_JSON.exists():
        print(f"[graph-nodes] {GRAPH_JSON} missing — skipping")
        return
    with open(GRAPH_JSON) as fp:
        g = json.load(fp)
    nodes = g.get("nodes", [])
    print(f"[graph-nodes] {len(nodes)} total nodes in graph.json")

    # Build (org_id, kind, name) set of already-known entities to skip dupes.
    # Org-AWARE (Phase 1B): the entities unique constraint is (org_id, kind, name),
    # so the SAME (kind, name) can legitimately exist once per org (a school Rule
    # and a life Rule of the same name are distinct rows). An org-blind skip-set
    # would suppress the org2/org3 row whenever the name already exists under org1.
    # C4 (2026-07-23, Codex #2): fetch existing compiled_truth_md too (not just the key), so a CHANGED
    # graph-only node is detected + re-upserted instead of silently skipped. Hub-eligible kinds still
    # skip-entirely in the loop below (hubs own their truth+embedding).
    cur.execute("SELECT org_id, kind, name, compiled_truth_md FROM entities")
    known = {(r[0], r[1], r[2]): (r[3] or "") for r in cur.fetchall()}
    print(f"[graph-nodes] {len(known)} (org,kind,name) entities already present (skip unchanged; update changed graph-only kinds)")

    # Filter + prepare insert rows
    candidates = []
    skipped_session_like = 0
    skipped_already = 0
    skipped_unmapped = 0
    for n in nodes:
        ntype = n.get("type", "")
        if ntype in GRAPH_NODE_SKIP_TYPES:
            skipped_session_like += 1
            continue
        kind = GRAPH_NODE_KIND_MAP.get(ntype)
        if kind is None:
            skipped_unmapped += 1
            continue
        name = n.get("label", n.get("id", "")).strip()
        if not name:
            continue
        props = n.get("properties", {}) or {}
        source_excerpt = (props.get("source_excerpt") or "").strip()
        source_chunks = props.get("_source_chunks") or []
        source_file = source_chunks[0] if source_chunks else None
        # Locked ontology (2026-06-08, masterplan §6): SHARED concept-kinds live
        # ONCE in the shared layer (org 1, life-owned, read by all via RLS read_all);
        # work-record kinds stay partitioned to the owning Core. This stops the
        # cross-org concept fan-out that duplicated shared concepts once per Core
        # (the 80-93% pollution removed 2026-06-08). Without this, every Core that
        # references a concept gets its own copy via _primary_source_file → org.
        # Nick's call (2026-06-08): partition Entity per-Core — Core-specific people
        # stay with their Core (read_all still surfaces them cross-Core). Only the
        # unambiguous global-knowledge kinds are shared.
        SHARED_GRAPH_KINDS = {"Topic", "Tool"}
        if kind in SHARED_GRAPH_KINDS:
            node_org = 1
        else:
            primary_src = props.get("_primary_source_file")
            if primary_src:
                node_org = path_to_org_id(str(BRAIN_ROOT / primary_src))
            else:
                node_org = get_org_id()
        # Compose body FIRST (needed to detect a content change vs the stored truth).
        rationale = (props.get("rationale") or "").strip()
        body_parts = [name]
        if source_excerpt: body_parts.append(source_excerpt)
        if rationale and rationale != source_excerpt: body_parts.append(f"Rationale: {rationale}")
        body = "\n\n".join(body_parts)
        body_final = body if len(body) > 1 else name
        # C4 (Codex #2) content-aware skip. If the row already exists for THIS org:
        #   - hub-eligible kinds (Entity/Topic/Project/Tool) → ALWAYS skip: the hub owns its truth +
        #     embedding; a raw graph excerpt must never overwrite the synthesized hub row.
        #   - graph-only kinds (Decision/Lesson/Rule/Incident) → skip only if the body is UNCHANGED; a
        #     CHANGED body falls through to upsert (truth+embedding update) so revised knowledge recalls.
        prev = known.get((node_org, kind, name))
        if prev is not None and (kind not in GRAPH_ONLY_TRUTH_KINDS or prev == body_final):
            skipped_already += 1
            continue
        candidates.append({
            "name": name, "kind": kind, "source_file": source_file,
            "body": body_final, "org_id": node_org,
        })

    # Dedupe candidates by (org_id, kind, name) — graph.json can have multiple
    # nodes with identical labels under the same type (e.g., common Decision
    # labels). Keep first occurrence; ON CONFLICT (org_id, kind, name) can't
    # handle in-batch dupes. Org-keyed so the same name under two orgs both land.
    seen = set()
    deduped = []
    for c in candidates:
        key = (c["org_id"], c["kind"], c["name"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    skipped_intra_batch_dup = len(candidates) - len(deduped)
    candidates = deduped

    print(f"[graph-nodes] candidates: {len(candidates)} new (after intra-batch dedup)")
    print(f"  skipped: {skipped_session_like} session-like, {skipped_already} already-present, {skipped_unmapped} unmapped type, {skipped_intra_batch_dup} intra-batch dup")

    if dry_run or not candidates:
        return

    # Embed in HUB_BATCH_SIZE chunks (these are small like hubs)
    for i in range(0, len(candidates), HUB_BATCH_SIZE):
        batch = candidates[i:i+HUB_BATCH_SIZE]
        texts = [c["body"] for c in batch]
        embs = embed_batch(client, texts)
        rows = [(c["name"], c["kind"], c["source_file"], c["body"], vec_literal(e), c["org_id"])
                for c, e in zip(batch, embs)]
        upsert_entities(cur, rows, overwrite_truth_for_graph_kinds=True)  # C4: revised Decision/Rule/Lesson/Incident update
        conn.commit()
        print(f"  [graph-nodes] batch {i//HUB_BATCH_SIZE+1}/{(len(candidates)+HUB_BATCH_SIZE-1)//HUB_BATCH_SIZE}: {len(batch)} embedded + upserted")


# --- Origin backbone pass ---------------------------------------------------
# brain-connectivity fix Phase 5 (2026-07-04). Every entity gets an edge to a
# hub node for its _primary_source_file — the session/subagent/doc it came from.
# 100% origin coverage → kills full isolation; same-origin nodes become 2 hops
# apart (co-occurrence for free) without an all-pairs edge explosion. Hubs are
# kind='Source', per-org (org from the source path), NO embedding (graph-only —
# excluded from vector recall). Edge type 'originates_in', down-weighted in
# query.py so origin edges de-isolate without dominating graph-BFS relevance.

def pass_origin_edges(conn, dry_run: bool):
    cur = conn.cursor()
    if not GRAPH_JSON.exists():
        print(f"[origin] {GRAPH_JSON} missing — skipping")
        return
    with open(GRAPH_JSON) as fp:
        g = json.load(fp)
    nodes = g.get("nodes", [])

    def source_of(n):
        p = n.get("properties", {}) or {}
        src = p.get("_primary_source_file")
        if not src:
            sc = p.get("_source_chunks") or []
            src = sc[0] if sc else None
        return src

    node_src = {}   # graph node_id -> source path
    src_orgs = {}   # source path -> org_id
    for n in nodes:
        if n.get("type") in GRAPH_NODE_SKIP_TYPES:
            continue
        src = source_of(n)
        if not src:
            continue
        node_src[n["id"]] = src
        if src not in src_orgs:
            src_orgs[src] = path_to_org_id(str(BRAIN_ROOT / src))
    print(f"[origin] {len(node_src)} nodes with a source; {len(src_orgs)} distinct source hubs")
    if dry_run:
        return

    # 1. Upsert Source hub entities (name = source path, NULL embedding).
    hub_rows = [(src, "Source", src, Path(src).stem.replace("_", " ").replace("-", " "), org)
                for src, org in src_orgs.items()]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO entities (name, kind, source_file, compiled_truth_md, org_id)
        VALUES %s
        ON CONFLICT (org_id, kind, name) DO NOTHING
    """, hub_rows)
    conn.commit()

    # 2. Hub ids + entity index (both keyed by org).
    cur.execute("SELECT id, name, org_id FROM entities WHERE kind = 'Source'")
    hub_index = {(r[2], r[1]): r[0] for r in cur.fetchall()}
    cur.execute("SELECT id, kind, name, org_id FROM entities WHERE valid_until IS NULL AND kind <> 'Source'")
    name_index: dict = {}
    for _id, _kind, _name, _org in cur.fetchall():
        name_index.setdefault(_name.lower(), {}).setdefault(_org, {})[_kind] = _id

    nodeid_to_label = {n["id"]: n.get("label", n["id"]) for n in nodes}
    nodeid_to_kind = {n["id"]: GRAPH_NODE_KIND_MAP.get(n.get("type", "")) for n in nodes}

    # 3. Wire each entity node -> its Source hub in the source's org.
    seen = set()
    edges_in = []
    skipped = 0
    for node_id, src in node_src.items():
        org = src_orgs[src]
        hub_id = hub_index.get((org, src))
        if not hub_id:
            skipped += 1
            continue
        orgmap = name_index.get(nodeid_to_label.get(node_id, node_id).lower())
        if not orgmap or org not in orgmap:
            skipped += 1
            continue
        kinds = orgmap[org]
        kind = nodeid_to_kind.get(node_id)
        ent_id = kinds.get(kind) if kind else None
        if ent_id is None:
            ent_id = next(iter(kinds.values()))
        if ent_id == hub_id:
            continue
        key = (ent_id, hub_id)
        if key in seen:
            continue
        seen.add(key)
        edges_in.append((ent_id, hub_id, "originates_in", None, "INFERRED", org))

    if edges_in:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO entity_edges (from_entity_id, to_entity_id, edge_type, confidence, confidence_label, org_id)
            VALUES %s
            ON CONFLICT (from_entity_id, to_entity_id, edge_type) DO NOTHING
        """, edges_in)
        conn.commit()
    print(f"[origin] {len(hub_rows)} source hubs, {len(edges_in)} origin edges, skipped {skipped}")


def pass_origin_edges_db(conn, dry_run: bool):
    """DB-native origin backbone (federated-brain-plan-2026-07-07 Phase 1).

    pass_origin_edges() above only wires graph.json NODES to their Source hub. But
    most peer entities live ONLY in Postgres (ingested from evidence, never promoted
    to a graph.json node), so they never received an origin edge and stayed isolated
    — business measured 57% isolated, and 100% of those isolated entities HAD a
    source_file. This pass connects EVERY entity that has a source_file to a Source
    hub in its own org, straight from the entities table — the actual de-isolation
    lever for all Cores (business 57% -> 0% when validated). Additive + idempotent.
    """
    cur = conn.cursor()
    if dry_run:
        cur.execute("""
            SELECT count(DISTINCT (e.source_file, e.org_id))
            FROM entities e
            WHERE e.source_file IS NOT NULL AND e.kind <> 'Source' AND e.valid_until IS NULL
        """)
        print(f"[origin-db] would ensure {cur.fetchone()[0]} (source,org) hubs + wire origin edges")
        return
    # 1. One Source hub per (source_file, org) among source'd entities. NULL
    #    embedding (excluded from vector recall, same as the graph.json hubs).
    cur.execute(r"""
        INSERT INTO entities (name, kind, source_file, compiled_truth_md, org_id)
        SELECT DISTINCT e.source_file, 'Source', e.source_file,
               regexp_replace(regexp_replace(split_part(e.source_file, '/', -1), '\.md$', ''), '[_-]', ' ', 'g'),
               e.org_id
        FROM entities e
        WHERE e.source_file IS NOT NULL AND e.kind <> 'Source' AND e.valid_until IS NULL
        ON CONFLICT (org_id, kind, name) DO NOTHING
    """)
    hubs = cur.rowcount
    # 2. originates_in edge: each source'd entity -> its Source hub (same org).
    #    Down-weighted in query.py (EDGE_TYPE_WEIGHTS originates_in=0.3) so it
    #    de-isolates without dominating graph-BFS relevance.
    cur.execute("""
        INSERT INTO entity_edges (from_entity_id, to_entity_id, edge_type, confidence, confidence_label, org_id)
        SELECT e.id, h.id, 'originates_in', NULL, 'INFERRED', e.org_id
        FROM entities e
        JOIN entities h ON h.kind = 'Source' AND h.name = e.source_file AND h.org_id = e.org_id
        WHERE e.source_file IS NOT NULL AND e.kind <> 'Source' AND e.valid_until IS NULL AND e.id <> h.id
        ON CONFLICT (from_entity_id, to_entity_id, edge_type) DO NOTHING
    """)
    edges = cur.rowcount
    conn.commit()
    print(f"[origin-db] {hubs} new Source hubs, {edges} origin edges wired")


# --- Entry ------------------------------------------------------------------

def pass_db_orphan_embeddings(conn, client, dry_run: bool):
    """Embed entities that live ONLY in Postgres and have no embedding.

    WHY THIS EXISTS — A CLOSED LOOP WITH NO EXIT, found by core-business on its own close (bus #700,
    2026-08-08) and reproduced on life before fixing:

        · close-core step 2c is MANDATORY, and it runs consolidate_sessions.py --apply
        · consolidation writes Workflow entities STRAIGHT TO POSTGRES — they never enter graph.json
        · pass_graph_nodes reads GRAPH_JSON and returns early without it, so no embed pass can see them
        · bin/verify-brain-synced.py refuses the brain-sync marker on any
          `org_id=X AND embedding IS NULL AND kind <> 'Source'`

    So EVERY Core that follows the close protocol correctly fails brain-sync verification from that
    point on, and the more consolidation succeeds the worse it gets. business had 10 NULL-embedded
    Workflows, life had 2. business refused to mint a false marker to make its own close look clean,
    which is the only reason this was found rather than papered over.

    Fixed here rather than by making consolidation emit into graph.json, and the choice matters: a
    graph.json round-trip fixes consolidation only, and the next writer that goes straight to Postgres
    reopens the hole. This closes the CLASS — any DB-resident entity missing an embedding gets one.

    The predicate is copied from the verifier deliberately, including `kind <> 'Source'` and the org
    scope. A pass that embeds a different set than verification checks would leave the marker withheld
    while reporting success, which is the same shape of defect as the bug it repairs.
    """
    cur = conn.cursor()
    org = get_org_id()
    cur.execute("""SELECT id, kind, name, COALESCE(compiled_truth_md, '')
                     FROM entities
                    WHERE org_id = %s AND embedding IS NULL AND kind <> 'Source'
                    ORDER BY id""", (org,))
    rows = cur.fetchall()
    if not rows:
        print(f"[db-orphans] org {org}: 0 entities missing an embedding")
        return
    print(f"[db-orphans] org {org}: {len(rows)} entity(ies) missing an embedding "
          f"({', '.join(sorted({r[1] for r in rows}))})")
    if dry_run:
        for _id, kind, name, body in rows[:5]:
            print(f"  DRY  {kind}/{name[:48]}  body_chars={len(body)}")
        return

    embedded = 0
    for i in range(0, len(rows), HUB_BATCH_SIZE):
        batch = rows[i:i + HUB_BATCH_SIZE]
        # Name AND body. A consolidation-written Workflow may legitimately carry no compiled_truth_md,
        # and embedding the empty string would either fail or produce a useless vector — so the name is
        # the floor. An entity with neither is skipped rather than embedded as nothing, and it stays
        # visible to the verifier, which is the honest outcome: better a withheld marker than a vector
        # that means nothing sitting where evidence should be.
        usable = [(r, (f"{r[2]}\n\n{r[3]}".strip() if r[3].strip() else (r[2] or "").strip()))
                  for r in batch]
        skipped = [r for r, t in usable if not t]
        usable = [(r, t) for r, t in usable if t]
        if skipped:
            print(f"  [db-orphans] SKIPPED {len(skipped)} with neither name nor body — "
                  f"these will keep the marker withheld, deliberately: "
                  f"{', '.join(str(r[0]) for r in skipped[:5])}")
        if not usable:
            continue
        embs = embed_batch(client, [t for _, t in usable])
        for (r, _t), e in zip(usable, embs):
            cur.execute("UPDATE entities SET embedding = %s::vector WHERE id = %s",
                        (vec_literal(e), r[0]))
        conn.commit()
        embedded += len(usable)
        print(f"  [db-orphans] batch {i // HUB_BATCH_SIZE + 1}: {len(usable)} embedded")
    print(f"[db-orphans] {embedded} embedded; re-run bin/verify-brain-synced.py --mint")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--incremental", action="store_true", help="Only re-embed changed files")
    ap.add_argument("--dry-run", action="store_true", help="Parse + count; no Voyage, no DB writes")
    ap.add_argument("--hubs-only", action="store_true", help="Skip evidence pass")
    ap.add_argument("--evidence-only", action="store_true", help="Skip hubs pass")
    ap.add_argument("--edges-only", action="store_true", help="Only load graph.json edges")
    ap.add_argument("--graph-nodes", action="store_true",
                    help="Embed graphify nodes (beyond hubs) into entities. Run AFTER --hubs-only.")
    ap.add_argument("--origin-edges", action="store_true",
                    help="Origin backbone: wire every entity to its _primary_source_file hub.")
    args = ap.parse_args()

    # Dry-run writes nothing, so it needs no lock. Real runs self-serialize.
    if not args.dry_run:
        acquire_brain_lock()

    client = None if args.dry_run else voyage_client()
    conn = connect()
    try:
        if args.graph_nodes:
            pass_graph_nodes(conn, client, args.dry_run)
            # AFTER graph-nodes, so anything that pass just inserted is covered too, and on this path
            # rather than only the default one because --graph-nodes is what the close actually runs.
            pass_db_orphan_embeddings(conn, client, args.dry_run)
            pass_edges(conn, args.dry_run)
            pass_origin_edges(conn, args.dry_run)   # origin backbone — runs against THIS Core's fresh graph.json
            pass_origin_edges_db(conn, args.dry_run)  # DB-native origin: covers Postgres-only entities (Phase 1)
        elif args.edges_only:
            pass_edges(conn, args.dry_run)
        elif args.origin_edges:
            pass_origin_edges(conn, args.dry_run)
            pass_origin_edges_db(conn, args.dry_run)  # DB-native origin (Phase 1)
        else:
            if not args.evidence_only:
                pass_hubs(conn, client, args.incremental, args.dry_run)
            if not args.hubs_only:
                pass_evidence(conn, client, args.incremental, args.dry_run)
            pass_edges(conn, args.dry_run)
            pass_origin_edges(conn, args.dry_run)   # origin backbone on every incremental close too
            pass_origin_edges_db(conn, args.dry_run)  # DB-native origin: covers Postgres-only entities (Phase 1)
            # ON THIS PATH TOO, and this is the one that actually matters: close-core.md step 3 runs
            # `embed.py --incremental`, which lands HERE, not in the --graph-nodes branch. Wiring the
            # orphan pass only into --graph-nodes would have left the real close path unfixed while the
            # tests and my own manual runs passed — the bug would have looked repaired from every angle
            # except the one it occurs on.
            pass_db_orphan_embeddings(conn, client, args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
