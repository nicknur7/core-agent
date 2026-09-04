#!/usr/bin/env python3
"""steering_ingest — load hook-events.log lines into `steering_events` (Phase 0 sensor).

Implements tasks/research/steering-surface-governance-2026-07-27.md Phase 0: "At close,
core-si-close.py ingests the session's hook-events lines into a new Postgres table
steering_events (org-scoped, RLS like si_artifacts)."

WHY THE LOG IS NOT ENOUGH ON ITS OWN. The log answers "did this hook fire". The tuning layer
needs to answer "at what rate, at what token cost, with what outcome, versus its own 14-day
baseline" — and to do that per Core, with retention, joined to judgments. That is a table.

IDEMPOTENT BY CONSTRUCTION. Close can run more than once, the log is append-only and never
truncated mid-session, and a re-run therefore re-reads lines already stored. dedupe_key is a hash
of the whole line; the unique index drops repeats. This matters more than it looks: fire VOLUME is
an input to the proof window that auto-promotes tunes with NO human in the loop, so double-counting
would let a tune clear its window on evidence that never happened.

FAIL-OPEN. Called from the close path. Any exception returns a zero-count result and never raises
into close — a telemetry failure must not cost Nick a session close. Same contract as hooklog.

Usage:
    python3 steering_ingest.py [--dry-run] [--log PATH] [--since ISO]
    from steering_ingest import ingest_log; ingest_log(dry_run=True)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import connect_corebrain, get_org_id  # noqa: E402

# Mirrors .claude/hooks/lib/hooklog.py's line format. detail/tokens_injected are OPTIONAL so
# lines written before 2026-07-30 still parse — the log is continuous history and reformatting
# it would destroy the baseline the tuning layer compares against.
LINE_RX = re.compile(
    r"^(?P<ts>\S+) \| hook=(?P<hook>[\w.:-]+) \| event=(?P<event>\S+) "
    r"\| verdict=(?P<verdict>\w+) \| session=(?P<session>[\w-]*) "
    r"(?:\| detail=(?P<detail>[^|]*) )?(?:\| tokens_injected=(?P<tokens>\d+) )?"
    r"\| excerpt=(?P<excerpt>.*)$"
)


def _instance() -> Path:
    return Path(os.environ.get("CORE_INSTANCE")
                or os.environ.get("CLAUDE_PROJECT_DIR")
                or Path(__file__).resolve().parents[2])


def default_log_path() -> Path:
    return _instance() / ".claude" / "state" / "hook-events.log"


def parse_line(line: str) -> dict | None:
    m = LINE_RX.match(line.rstrip("\n"))
    if not m:
        return None
    d = m.groupdict()
    try:
        ts = datetime.fromisoformat(d["ts"].replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return {
        "hook": d["hook"],
        "event": d["event"],
        "verdict": d["verdict"],
        "detail": (d.get("detail") or "").strip(),
        "tokens_injected": int(d.get("tokens") or 0),
        "session_id": d.get("session") or "",
        "excerpt": (d.get("excerpt") or "").strip(),
        "ts": ts,
        # Hash the RAW line: two genuinely distinct fires can share every parsed field (same hook,
        # same verdict, same millisecond bucket) and must both be recorded, while a re-read of the
        # same physical line must not be. The line itself is the identity.
        "dedupe_key": hashlib.sha256(line.rstrip("\n").encode("utf-8", "replace")).hexdigest(),
    }


def ingest_log(log_path: Path | None = None, dry_run: bool = False,
               since: datetime | None = None) -> dict:
    """Load the log into steering_events. Returns a stats dict; never raises."""
    stats = {"read": 0, "parsed": 0, "unparsed": 0, "inserted": 0, "duplicates": 0, "error": None}
    try:
        path = Path(log_path) if log_path else default_log_path()
        if not path.exists():
            stats["error"] = f"no log at {path}"
            return stats

        rows = []
        # ACROSS ROTATION (2026-08-13). hook-events.log rotates at 1 MB; this read the live
        # generation only, so after a rotation it ingested the tail and reported it as the log.
        # core_paths.rotated_set was written on 2026-08-12 naming this file as one of the four
        # readers it existed for, and this is the last of the four to be routed through it —
        # grade-gate, steering-ledger and refresh-hook-dispositions carried their own copies
        # instead, and hook_block_miner and skill_graduate were reading live-only tonight.
        #
        # It matters more here than in a counting organ: this INSERTs into steering_events, so a
        # truncated read does not merely under-report, it durably persists a partial history that
        # later queries treat as complete. Duplicates are already handled downstream (the stats dict
        # counts them), so re-ingesting rotated generations is safe.
        #
        # Degrades to live-only, never to empty — the same discipline as the other two: an ingester
        # that reads nothing on resolver failure writes a confident empty history.
        try:
            import sys as _sys
            from pathlib import Path as _P
            _sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "bin"))
            from core_paths import read_rotated as _rr
            _text = _rr(path)
        except Exception:
            _text = path.read_text(errors="replace")
        for line in _text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue          # blank + the log's own '# ...' header banner
            stats["read"] += 1
            rec = parse_line(line)
            if rec is None:
                stats["unparsed"] += 1
                continue
            if since is not None and rec["ts"] < since:
                continue
            stats["parsed"] += 1
            rows.append(rec)

        if dry_run or not rows:
            return stats

        org = get_org_id()
        conn = connect_corebrain()
        try:
            with conn.cursor() as cur:
                for r in rows:
                    # ON CONFLICT DO NOTHING is the idempotency: re-running close is a no-op
                    # rather than an inflation of the fire counts the proof window reads.
                    cur.execute(
                        """INSERT INTO steering_events
                             (org_id, hook, event, verdict, detail, tokens_injected,
                              session_id, excerpt, ts, dedupe_key)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (org_id, dedupe_key) DO NOTHING""",
                        (org, r["hook"], r["event"], r["verdict"], r["detail"],
                         r["tokens_injected"], r["session_id"], r["excerpt"],
                         r["ts"], r["dedupe_key"]))
                    if cur.rowcount:
                        stats["inserted"] += 1
                    else:
                        stats["duplicates"] += 1
            conn.commit()
        finally:
            conn.close()
    except Exception as e:                                    # fail-open by contract
        stats["error"] = f"{type(e).__name__}: {e}"
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default=None)
    ap.add_argument("--since", default=None, help="ISO timestamp; ignore older lines")
    a = ap.parse_args()
    since = None
    if a.since:
        try:
            since = datetime.fromisoformat(a.since.replace("Z", "+00:00"))
        except Exception:
            print(f"bad --since: {a.since}", file=sys.stderr)
            return 2
    st = ingest_log(Path(a.log) if a.log else None, dry_run=a.dry_run, since=since)
    print(f"steering_ingest: {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
