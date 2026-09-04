#!/usr/bin/env python3
"""staleness_invariant.py — Workstream 3: ONE continuous staleness readout.

Staleness is already handled by scattered, working machinery (truth-drift refresh, freshness-gate,
recency-aware recall, >30d memory warnings) + WS1's new SI-spine invariants. This does NOT rewrite any
of that — it AGGREGATES every signal into a single GREEN/YELLOW/RED state so "is anything stale right
now?" is one glance, checked every session boundary, instead of five scattered checks nobody holds together.

  python3 staleness_invariant.py            # print the unified readout + write state file
  python3 staleness_invariant.py --json     # machine-readable

Read-only except for the state file it writes. Fail-open per signal (a broken probe → 'unknown', never
a crash). RED only for a genuinely-actionable invariant breach.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("CORE_INSTANCE") or HERE.parents[1])
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

STATE = Path(os.environ.get("CLAUDE_PROJECT_DIR") or REPO) / ".claude" / "state"
DRIFT_DIR = REPO / "scheduling" / "brain-pg" / "compile-truth-work"
MEM_DIRS = [REPO / "memory", REPO / "memory" / "relationships", REPO / "memory" / "projects"]
STALE_DAYS = 30


def _sig(name, state, detail):
    return {"name": name, "state": state, "detail": detail}


def _truth_drift() -> dict:
    try:
        reports = sorted(glob.glob(str(DRIFT_DIR / "drift-report-*.json")), reverse=True)
        if not reports:
            return _sig("truth_drift", "unknown", "no drift report yet")
        d = json.loads(Path(reports[0]).read_text())
        n = len(d.get("drift_records", []))
        # drift is EXPECTED and auto-refreshed each session; RED only if a large backlog persists
        state = "green" if n == 0 else ("yellow" if n <= 8 else "red")
        return _sig("truth_drift", state, f"{n} drifted entit(ies) in latest report ({Path(reports[0]).name})")
    except Exception as e:
        return _sig("truth_drift", "unknown", f"probe error: {str(e)[:80]}")


def _spine_invariants() -> dict:
    try:
        import si_project
        from _env import get_org_id  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
        org = get_org_id()
        inv = si_project.verify_invariants(org)
        ok = inv.get("count_parity") and inv.get("rev_in_sync")
        return _sig("si_spine", "green" if ok else "red",
                    f"parity={inv.get('count_parity')} rev_in_sync={inv.get('rev_in_sync')} "
                    f"(canonical={inv.get('canonical_live')} projected={inv.get('projected')})")
    except Exception as e:
        return _sig("si_spine", "unknown", f"probe error: {str(e)[:80]}")


def _memory_freshness() -> dict:
    """Count personal/project memory files whose `Last updated:` stamp is >30d (drift risk)."""
    try:
        now = time.time()
        stale = []
        seen = set()
        for d in MEM_DIRS:
            if not d.exists():
                continue
            for p in d.glob("*.md"):
                if p in seen:
                    continue
                seen.add(p)
                txt = p.read_text(errors="ignore")[:2000]
                if re.search(r"Status:\s*(archived|SUPERSEDED|LOCKED)", txt, re.I):
                    continue
                m = re.search(r"Last updated:\s*(\d{4})-(\d{2})-(\d{2})", txt)
                if not m:
                    continue
                import datetime
                dt = datetime.date(int(m[1]), int(m[2]), int(m[3]))
                age = (now - time.mktime(dt.timetuple())) / 86400
                if age > STALE_DAYS:
                    stale.append((p.name, int(age)))
        stale.sort(key=lambda x: -x[1])
        n = len(stale)
        state = "green" if n == 0 else ("yellow" if n <= 5 else "red")
        top = ", ".join(f"{n_}({a}d)" for n_, a in stale[:4])
        return _sig("memory_freshness", state, f"{n} file(s) >{STALE_DAYS}d" + (f": {top}" if top else ""))
    except Exception as e:
        return _sig("memory_freshness", "unknown", f"probe error: {str(e)[:80]}")


def readout() -> dict:
    signals = [_truth_drift(), _spine_invariants(), _memory_freshness()]
    order = {"red": 3, "yellow": 2, "green": 1, "unknown": 0}
    worst = max(signals, key=lambda s: order.get(s["state"], 0))["state"]
    overall = "RED" if worst == "red" else ("YELLOW" if worst == "yellow" else
              ("GREEN" if worst == "green" else "UNKNOWN"))
    out = {"state": overall, "signals": signals, "generated_at": int(time.time())}
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "staleness-state.json").write_text(json.dumps(out, indent=2))
    except Exception:
        pass
    return out


def main() -> int:
    out = readout()
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
        return 0
    icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "UNKNOWN": "⚪"}
    print(f"{icon.get(out['state'], '⚪')} STALENESS: {out['state']}")
    for s in out["signals"]:
        print(f"  {icon.get(s['state'].upper(), '⚪')} {s['name']}: {s['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
