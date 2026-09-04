#!/usr/bin/env python3
"""discover.py — register real sources into the ledger (memory-brain-SI unified redesign, step 1).

Plan: tasks/research/memory-brain-si-unified-redesign-2026-07-18.md PART 7.6 step ①.

Scans this Core's actual source material and registers each into the source-revision ledger, so the
ledger reflects reality (the thing the old freshness-gate never checked — store-vs-DISK). v1 covers
the primary capture source: this Core's Claude Code session-transcript JSONL files. Each becomes a
`session_jsonl` source (source_key = session UUID = filename stem); a content change appends a new
revision and enqueues a 'captured' stage job for the export processor to claim.

Idempotent: re-running registers nothing new for unchanged files (register_revision dedups by hash).
Fork-safe: transcript dir is derived from CORE_INSTANCE (mangled like Claude Code does — '/' and ' '
both → '-'); no life-specific paths.

Usage:
  CORE_ORG_ID=1 CORE_INSTANCE=... python3 discover.py            # register + enqueue
  ... python3 discover.py --dry-run                              # report only, no writes
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
from _env import connect_corebrain, get_org_id, connect_or_skip  # noqa: E402
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "bin"))
import core_seat as _core_seat  # noqa: E402 — canonical seat/slug resolver

CAPTURE_PROCESSOR_VERSION = "capture/v1"  # bump → all session revisions re-enqueue for re-export


def transcript_dir() -> Path:
    """~/.claude/projects/<mangled CORE_INSTANCE>. Claude Code replaces both '/' and ' ' with '-'."""
    inst = os.environ.get("CORE_INSTANCE")
    if not inst:
        raise SystemExit("CORE_INSTANCE required")
    mangled = _core_seat.transcripts_dir(Path(inst)).name
    return Path.home() / ".claude" / "projects" / mangled


def _is_queue_operation(data: bytes) -> bool:
    """True for a subagent TASK QUEUE file, which is not a session and is never exported.

    The vault exporter ($CORE_BRAIN/_build/export.py) skips these by design: a
    queue-operation file is a subagent task queue, not a conversation, and it has no cwd to
    attribute to a project. But discovery enqueued every *.jsonl in the transcript dir as a
    session revision, so the capture worker then demanded a vault .md for a file the
    exporter would never write, failed with 'no-or-empty-vault-md', and retried forever.

    Measured 2026-07-28: 3 jobs stuck at attempts=3/retry_wait, permanently. Harmless in
    isolation, except brain_status counts them as capture debt — so a fully clean close
    reports LAGGING and step 8's expected READY is unreachable.

    Same defect shape as the pipeline-exhaust leak fixed the same day: two components
    disagreeing about what counts as a session, with nothing forcing them to agree. Here the
    predicate is one line and cheap to co-locate, so discovery applies the exporter's own
    rule rather than a second approximation of it.
    """
    head = data[:4096].lstrip()
    if not head.startswith(b"{"):
        return False
    try:
        first = json.loads(head.split(b"\n", 1)[0])
    except Exception:
        return False   # unparseable -> NOT skipped; fail toward capture, never toward silent drop
    return first.get("type") == "queue-operation"


def discover_sessions(conn, dry_run: bool = False) -> dict:
    tdir = transcript_dir()
    stats = {"dir": str(tdir), "files": 0, "new_revisions": 0, "jobs_enqueued": 0,
             "unchanged": 0, "queue_ops_skipped": 0}
    if not tdir.is_dir():
        stats["error"] = "transcript dir not found"
        return stats
    for jsonl in sorted(tdir.glob("*.jsonl")):
        stats["files"] += 1
        data = jsonl.read_bytes()
        if _is_queue_operation(data):
            # Never registered, so no revision and no job — the ledger stays a record of
            # things that are actually capturable.
            stats["queue_ops_skipped"] += 1
            continue
        chash = ledger.content_hash_of(data)
        session_key = jsonl.stem  # Claude Code session UUID
        if dry_run:
            continue
        source_id = ledger.register_source(conn, session_key, "session_jsonl", str(jsonl))
        rev_id, seq, is_new = ledger.register_revision(
            conn, source_id, chash, str(jsonl), operation="upsert", byte_size=len(data))
        if is_new:
            stats["new_revisions"] += 1
            fp = ledger.compute_fingerprint(chash, "captured", CAPTURE_PROCESSOR_VERSION)
            _, jnew = ledger.enqueue_job(conn, rev_id, "captured", CAPTURE_PROCESSOR_VERSION, fp)
            if jnew:
                stats["jobs_enqueued"] += 1
        else:
            stats["unchanged"] += 1
        conn.commit()
    return stats


def main() -> int:
    dry = "--dry-run" in sys.argv
    # Degrade with a named status instead of killing the close chain (see
    # _env.connect_or_skip). A Core with no database must still be able to close.
    conn = connect_or_skip("CAPTURE-discover")
    if conn is None:
        return 0
    try:
        s = discover_sessions(conn, dry_run=dry)
        tag = "[dry-run] " if dry else ""
        print(f"{tag}discover(org {get_org_id()}): {s['files']} session file(s) in {s['dir']}")
        if s.get("error"):
            print(f"  {s['error']}")
        elif not dry:
            print(f"  {s['new_revisions']} new revision(s), {s['jobs_enqueued']} 'captured' job(s) enqueued, "
                  f"{s['unchanged']} unchanged")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
