#!/usr/bin/env python3
"""state-feeder.py — gather THIS Core's live operational state into honest, timestamped
facts. The DB-free half of the Core State Spine (build plan 2026-07-14).

WHY: the dashboard today reads copies of Mac files and renders confidently-WRONG zeros
(a missing file shows as "0 hooks"). The Spine's core rule: MISSING != ZERO. This feeder
produces facts that each carry a `status` (fresh|delayed|partial|offline|unknown) and
timestamps, so a value can never silently become a fake zero — an uncollectable fact is
emitted as status=unknown with value=null, NOT written as 0.

TODAY (no PlanetScale yet): writes the fact set to memory/state-facts.json locally and
prints it. WHEN PlanetScale is wired: add one upsert step (see build plan Phase 2) —
this gather logic is unchanged.

Per-Core-safe: reads only THIS Core's own root (via $CORE_INSTANCE / git toplevel).
Fail-open per fact: a fact that can't be collected becomes status=unknown, never a crash.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
FEEDER_VERSION = "1.0.0-local"

# Per-subsystem expected freshness (seconds). A fact older than this → "delayed".
CADENCE = {
    "health": 26 * 3600,      # brain-health runs ~per brain-update
    "brain": 26 * 3600,
    "hooks": 24 * 3600,
    "contracts": 7 * 24 * 3600,
    "si": 24 * 3600,
    "session": 90 * 24 * 3600,  # a session date is "current" for a long window
    "sync": 30 * 24 * 3600,
}


def _root() -> Path:
    r = os.environ.get("CORE_INSTANCE")
    if r:
        return Path(r)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except Exception:
        pass
    return Path.cwd()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _status_for(observed: datetime | None, subsystem: str, now: datetime, value) -> str:
    """MISSING != ZERO. No observation → unknown. Otherwise fresh unless past cadence."""
    if observed is None or value is None:
        return "unknown"
    # AN EMPTY STRING IS NOT AN OBSERVATION (2026-08-12, found by core-finance).
    #
    # This function rejected only `value is None`, so a reader that returned "" — an empty or  # privacy-ok: generic engineering vocabulary
    # truncated marker file, e.g. a zero-byte `.last-baseline-sync` — was emitted as
    # value='' status='fresh'. That is an uncollected observation reported with full confidence:
    # precisely the fake zero this file's own MISSING != ZERO header abolishes, one type away.
    #
    # NARROW BY DESIGN — `not value` would be wrong. A count of 0 is a REAL observation and must
    # stay `fresh`; collapsing it to unknown would reintroduce the same defect from the other side.
    # Only a string with no content is treated as absence.
    if isinstance(value, str) and not value.strip():
        return "unknown"
    age = (now - observed).total_seconds()
    return "fresh" if age <= CADENCE.get(subsystem, 24 * 3600) else "delayed"


class Facts:
    """Accumulate (subsystem, key) -> {value, status, observed_at}. Every add() is
    wrapped so a collection failure yields status=unknown, never a missing/zeroed fact."""

    def __init__(self, core_id: str, now: datetime):
        self.core_id = core_id
        self.now = now
        self.items: list[dict] = []

    def add(self, subsystem: str, key: str, collector):
        """collector() -> (value, observed_at:datetime|None). Any exception → unknown."""
        try:
            value, observed = collector()
        except Exception as e:
            value, observed = None, None
            note = f"{e.__class__.__name__}"
        else:
            note = None
        status = _status_for(observed, subsystem, self.now, value)
        self.items.append({
            "core_id": self.core_id,
            "subsystem": subsystem,
            "key": key,
            "value": value,
            "status": status,
            "observed_at": _iso(observed) if observed else None,
            "collected_at": _iso(self.now),
            **({"note": note} if note else {}),
        })


def _mtime(p: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return None


def _psql_scalar(sql: str):
    """One scalar from local corebrain, or None if unreachable (→ status unknown, not 0)."""
    try:
        out = subprocess.run(["psql", "-d", os.environ.get("COREBRAIN_DB", "corebrain"), "-tA", "-c", sql],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return int(out.stdout.strip())
    except Exception:
        pass
    return None


def gather(root: Path) -> dict:
    now = _now()
    identity = {}
    try:
        identity = json.loads((root / ".claude" / "identity.json").read_text())
    except Exception:
        pass
    core_id = identity.get("domain_label") or re.sub(r"^core-", "", root.name)
    org_id = identity.get("org_id")
    f = Facts(core_id, now)

    # health: parse .brain-health-status "N PASS · M WARN · K FAIL"
    def health():
        p = root / ".claude" / "state" / ".brain-health-status"
        txt = p.read_text()
        m = re.search(r"(\d+)\s*PASS.*?(\d+)\s*WARN.*?(\d+)\s*FAIL", txt)
        if not m:
            return None, None
        return {"pass": int(m.group(1)), "warn": int(m.group(2)), "fail": int(m.group(3))}, _mtime(p)
    f.add("health", "brain_health", health)

    # brain: live per-org counts (None if DB unreachable → unknown, never 0)
    def brain_entities():
        if org_id is None:
            return None, None
        n = _psql_scalar(f"SELECT count(*) FROM entities WHERE org_id={int(org_id)}")
        return n, (now if n is not None else None)
    f.add("brain", "entity_count", brain_entities)

    def brain_evidence():
        if org_id is None:
            return None, None
        n = _psql_scalar(f"SELECT count(*) FROM evidence WHERE org_id={int(org_id)}")
        return n, (now if n is not None else None)
    f.add("brain", "evidence_count", brain_evidence)

    # hooks: count of distinct hook scripts on disk (NOT "registered" — see below)
    def hooks():
        d = root / ".claude" / "hooks"
        if not d.is_dir():
            return None, None            # missing dir → unknown, not 0
        # COUNTS NAMES, NOT FILES (2026-08-12, found by core-finance).
        #
        # This counted every .sh and .py in the directory and the comment called the result
        # "registered hook scripts". Neither half was true. Registration here is commonly a thin
        # .sh wrapper around a .py implementation of the same name, so one hook was counted twice:
        # measured 57 files against 47 distinct names on both life and finance — 10 wrapper/impl
        # pairs (brain-recall-trigger, learned-classifier, learned-recallguard, learned-stopguard,
        # learned-validator, say-do-gap, state-claim-gate, stop-signal-gate, time-claim-gate,
        # verification-trigger). Identical on both seats because .claude/hooks is baseline-shared.
        #
        # DELIBERATELY NOT counted from settings.json. Finance tried that first and got "26 present
        # but not registered", which was their extraction being naive rather than a real gap —
        # session-lifecycle.sh and say-do-gap.py look unregistered because a wrapper or router
        # invokes them. Disk names is the number this function can actually defend, so the name it
        # is given now matches what it measures. It still slightly overcounts: an imported library
        # that lives here and is never invoked as a hook (casebook_matchers.py) counts as one.
        names = {p.stem for p in d.iterdir() if p.suffix in (".sh", ".py")}
        return len(names), now           # live-counted → observed = now (fresh)
    f.add("hooks", "count", hooks)

    # contracts: live count from corebrain
    def contracts():
        if org_id is None:
            return None, None
        n = _psql_scalar(f"SELECT count(*) FROM learned_contracts WHERE org_id={int(org_id)}")
        return n, (now if n is not None else None)
    f.add("contracts", "count", contracts)

    # si: pending core-si items (from the cache the engine writes)
    def si_count():
        p = root / ".claude" / "state" / ".core-si-count"
        return int(p.read_text().strip()), _mtime(p)
    f.add("si", "queue_count", si_count)

    # session: most recent session-log date
    def last_session():
        d = root / "sessions"
        dates = sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []
        if not dates:
            return None, None
        return dates[-1], _mtime(d / f"{dates[-1]}.md")
    f.add("session", "last_date", last_session)

    # sync: last baseline sync marker
    def last_sync():
        p = root / ".claude" / "state" / ".last-baseline-sync"
        return p.read_text().strip()[:40], _mtime(p)
    f.add("sync", "last_baseline_sync", last_sync)

    return {
        "schema_version": SCHEMA_VERSION,
        "feeder_version": FEEDER_VERSION,
        "core_id": core_id,
        "org_id": org_id,
        "heartbeat_at": _iso(now),
        "facts": f.items,
    }


def main() -> int:
    root = _root()
    payload = gather(root)
    # Local sink (no DB yet). When PlanetScale is wired: upsert payload["facts"] here.
    out_path = root / "memory" / "state-facts.json"
    try:
        out_path.write_text(json.dumps(payload, indent=2))
    except Exception as e:
        print(f"[state-feeder] WARN: could not write {out_path}: {e}", file=sys.stderr)
    if "--quiet" not in sys.argv:
        unknown = sum(1 for x in payload["facts"] if x["status"] == "unknown")
        print(f"[state-feeder] {payload['core_id']}: {len(payload['facts'])} facts "
              f"({unknown} unknown/uncollectable) → {out_path.name}")
        if "--print" in sys.argv:
            print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
