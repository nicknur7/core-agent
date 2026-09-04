#!/usr/bin/env python3
"""brain_status.py — the typed status authority (unified redesign, step ③).

Plan: PART 7.2 (typed status, not one boolean) + PART 2 finding #1 (the old freshness-gate compared
the store against ITSELF, never against disk — so it flashed green while whole sessions rotted).

This compares the ledger (what's been captured) against REALITY (session JSONL on disk) and against
the job queue, and emits a TYPED status per dimension — never one 'fresh' boolean, and DB-down is
NEVER reported as fresh. Wired into SessionStart to make staleness self-announcing.

Dimensions:
  capture_lag   — session JSONL on disk not yet captured into the vault (unregistered OR pending job).
  failed        — stage jobs that hit 'dead' (max retries) — visible repair debt.
  deferred      — the lock-busy deferred marker (from run-brain-update.sh).
  availability   — DB reachable? (unreachable → UNAVAILABLE, never 'fresh').

Exit: 0 always (informational). Prints a one-line human summary; --json for the structured form.
Fork-safe (CORE_INSTANCE / CORE_ORG_ID / transcript dir derived, no life-specific paths).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "bin"))
import core_seat as _core_seat  # noqa: E402 — canonical seat/slug resolver

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _transcript_dir() -> Path:
    inst = os.environ.get("CORE_INSTANCE", "")
    return _core_seat.transcripts_dir(Path(inst))


def status() -> dict:
    out = {"availability": "READY", "capture_lag": 0, "unregistered": 0, "failed": 0,
           "deferred": 0, "overall": "READY", "detail": ""}
    # deferred marker (DB-independent)
    inst = os.environ.get("CORE_INSTANCE", "")
    if inst:
        dm = Path(inst) / ".claude" / "state" / ".brain-update-deferred"
        if dm.exists():
            try:
                out["deferred"] = sum(1 for _ in dm.open())
            except Exception:
                out["deferred"] = 1
    # disk truth
    tdir = _transcript_dir()
    disk_sessions = len(list(tdir.glob("*.jsonl"))) if tdir.is_dir() else 0
    # ledger truth
    try:
        from _env import connect_corebrain, get_org_id, describe_db_failure
        org = get_org_id()
        conn = connect_corebrain()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sources WHERE org_id=%s AND source_kind='session_jsonl'", (org,))
            registered = cur.fetchone()[0]
            cur.execute("""SELECT count(*) FROM source_revisions r
                           JOIN sources s ON s.id=r.source_id
                           WHERE s.org_id=%s AND s.source_kind='session_jsonl'
                             AND NOT EXISTS (SELECT 1 FROM stage_jobs j
                                 WHERE j.source_revision_id=r.id AND j.stage='captured' AND j.status='done')""", (org,))
            uncaptured_rev = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM stage_jobs WHERE org_id=%s AND status='dead'", (org,))
            out["failed"] = cur.fetchone()[0]
        conn.close()
        out["unregistered"] = max(0, disk_sessions - registered)
        out["capture_lag"] = uncaptured_rev + out["unregistered"]
    except Exception as e:
        out["availability"] = "UNAVAILABLE"
        out["overall"] = "UNAVAILABLE"
        # Was an unconditional "corebrain unreachable" on a bare `except Exception`, so a slow
        # query and a schema error both reported a down database. See _env.describe_db_failure.
        out["detail"] = f"{describe_db_failure(e)} — recall may be stale; using local files"
        return out
    # overall: worst dimension wins (never collapse to a single fresh boolean silently)
    if out["failed"] > 0:
        out["overall"] = "FAILED"
        out["detail"] = f"{out['failed']} extraction job(s) dead (max retries) — repair debt"
    elif out["capture_lag"] > 0:
        out["overall"] = "LAGGING"
        out["detail"] = (f"{out['capture_lag']} session(s) not yet captured into the brain "
                         f"({out['unregistered']} unregistered on disk) — next close resolves it")
    elif out["deferred"] > 0:
        out["overall"] = "LAGGING"
        out["detail"] = f"{out['deferred']} brain-update(s) deferred on a busy lock — capture may lag"
    else:
        out["detail"] = f"all {disk_sessions} sessions captured; brain reflects disk"
    return out


def main() -> int:
    s = status()
    if "--json" in sys.argv:
        print(json.dumps(s))
    else:
        icon = {"READY": "✅", "LAGGING": "🟡", "FAILED": "🔴", "UNAVAILABLE": "⚠"}.get(s["overall"], "")
        print(f"{icon} BRAIN STATUS [{s['overall']}]: {s['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
