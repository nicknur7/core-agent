#!/usr/bin/env python3
"""Retire a steering component when the evidence says so — autonomously, reversibly, narrowly.

The operator, 2026-07-30: make self-improvement and tuning fully autonomous, with no human gates.
This is the retirement half of that. The generation half (friction_loop) and the
quarantine half (friction_watchdog) already run unattended; nothing ever retired a HAND-WRITTEN
hook, so the estate could only grow.

WHY THIS IS SAFE TO RUN UNATTENDED — four structural reasons, not four good intentions:

1. REVERSIBLE BY CONSTRUCTION. Retirement writes `retired: true` + `retired_reason` onto the
   registry entry. reconcile-hooks keeps the tombstone (deleting an entry does NOT unregister a
   hook — found 2026-07-27 when the registry went 39->37 and the hook kept firing), removes the
   registration on every Core's next pull, and the reason travels with it. Undo is flipping one
   boolean.

2. A PROOF WINDOW, not a single reading. A verdict must hold across MIN_OBSERVATIONS separate
   closes before it counts. One bad measurement retires nothing — which matters because this
   session found three metrics that were confidently reporting the wrong thing.

3. TRUST ROOTS ARE EXCLUDED IN CODE. Not by policy, not by a comment: by a deny-list checked
   before anything else. pretooluse-guard, the sentinel receipt chain, the sync and write guards,
   and the close/save path can never be retired here regardless of what the ledger says. That is
   Nick's own ratified exclusion (master plan §8) and the one human gate that is load-bearing.

4. ONLY TWO VERDICTS QUALIFY, and both mean "the firing is provably not worth it":
     PRE-EMPTED  the failure it catches is now impossible by construction (the clock), so every
                 fire is pure cost no matter how CORRECT the fire is.
     EXPENSIVE   high cost per session AND the guarded behaviour is not declining.
   Not RARE-VALUABLE (rarity x severity — cross-core-completion-gate fires 3x in 74 sessions and
   caught a real overclaim). Not UNMEASURED (its effect is a side effect the fire counter cannot
   see — retiring those would have killed recall-satisfied, which unlinks the recall marker).
   Not COST-BLIND (that is missing data, and missing data must never read as a verdict).

  python3 bin/steering-retire.py              # what would retire, and why
  python3 bin/steering-retire.py --apply
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[1])
REG = ROOT / "bin" / "hook-registry.json"
HIST = ROOT / ".claude" / "state" / "retire-observations.json"

MIN_OBSERVATIONS = 3
RETIRABLE = {"PRE-EMPTED", "EXPENSIVE"}

# Never retired here, whatever the evidence. Security and data-integrity paths whose failure mode
# is silent and unrecoverable, so the cost of a wrong retirement is not symmetric with the cost of
# keeping a quiet hook.
TRUST_ROOTS = {
    "pretooluse-guard",            # the Sentinel chokepoint for every outward action
    "sentinel-receipt",            # proof Sentinel actually ran
    "reconcile-subagent-receipt",  # proof close-reconciler actually ran
    "shared-write-guard",          # stops a puller Core clobbering baseline-shared files
    "sync-from-baseline",          # keeps pull-only Cores current
    "stop-hook",                   # executes a queued /close-core
    "defensive-save",              # the only thing that saves work on an unclean exit
    "model-pin-gate",              # spend control on fan-outs
    # Added 2026-07-30 after a hostile pass over this list before the baseline push. These three
    # were already unreachable — a log-only or side-effect hook cannot earn PRE-EMPTED or
    # EXPENSIVE, since both are cost verdicts and the cost counter cannot see a side effect. But
    # that is protection by ACCIDENT: it depends on the ledger's effect classification, which is
    # data, and if that classification ever changed these would become retirable with no other
    # guard in the way. A safety list should hold because it says so.
    "friction-watchdog",           # quarantines misbehaving artifacts — the reversibility half of
                                   # autonomous self-improvement. Retiring it removes the safety
                                   # net for the entire generation loop.
    "capability-usage-log",        # skill_graduate.demote() reads this telemetry to retire unused
                                   # skills; a silent recorder makes skill retirement fail closed
                                   # forever, which is how this whole module's docstring starts.
    "session-start-check",         # the only staleness surface at session start.
    # Added 2026-07-30 from Fable's blast-radius review. Both are LOOP-CRITICAL in a way the
    # ledger structurally cannot see, because a component's ledger cost is the cost of what it
    # CARRIES, not of itself:
    "friction-dispatch",           # the ONE interpreter for every generated artifact — its own
                                   # intent record says the spine is inert without it. Its cost
                                   # is the cost of the artifacts it dispatches, so bad generator
                                   # output makes the DISPATCHER read EXPENSIVE. Retiring it
                                   # severs the actuator of the entire self-improvement loop, and
                                   # the resulting freeze is exactly the quiet-looking failure
                                   # this whole push exists to eliminate.
    "session-presence",            # supplies the clock. The clock is the PREMISE of every
                                   # PRE-EMPTED verdict; retire the supply and the pre-emption it
                                   # justified is silently false. It was protected only by
                                   # accident (it prints directly rather than through
                                   # hooklog.emit, so it has no cost rows) — and protection by
                                   # accident is what the comment above says is unacceptable.
}


def _ledger(days: int):
    spec = importlib.util.spec_from_file_location("sl", ROOT / "bin" / "steering-ledger.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.build(days)


def _history() -> dict:
    try:
        return json.loads(HIST.read_text())
    except Exception:
        return {}


def observe(days: int = 14) -> dict:
    """Record this run's verdicts and return {hook: consecutive_retirable_observations}."""
    led = _ledger(days)
    hist = _history()
    today = datetime.date.today().isoformat()
    for c in led["components"]:
        row = hist.setdefault(c["hook"], {"streak": 0, "verdict": "", "dates": []})
        if c["verdict"] in RETIRABLE and c["verdict"] == (row["verdict"] or c["verdict"]):
            if today not in row["dates"]:
                row["streak"] += 1
                row["dates"] = (row["dates"] + [today])[-10:]
        elif c["verdict"] in RETIRABLE:
            row["streak"], row["dates"] = 1, [today]
        else:
            row["streak"], row["dates"] = 0, []
        row["verdict"] = c["verdict"]
    # PRUNE history for hooks the ledger no longer reports. After a successful --apply the hook
    # is flagged retired and drops out of registry(), but its history row kept streak/verdict
    # forever — so it reappeared in `ready` every run and the READY print did by_name[n] on a
    # name the ledger no longer contains. KeyError, crash, even in dry-run: the retirement tool
    # died the first time it succeeded. Reproduced before fixing. (Fable, blast-radius review.)
    live = {c["hook"] for c in led["components"]}
    for gone in [n for n in hist if n not in live]:
        hist.pop(gone, None)
    return hist, led


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    hist, led = observe(a.days)
    by_name = {c["hook"]: c for c in led["components"]}

    ready, held = [], []
    for name, row in sorted(hist.items()):
        if row["verdict"] not in RETIRABLE:
            continue
        if name in TRUST_ROOTS:
            held.append((name, row, "trust root — excluded in code, never auto-retired"))
            continue
        if row["streak"] < MIN_OBSERVATIONS:
            held.append((name, row,
                         f"proof window {row['streak']}/{MIN_OBSERVATIONS} observations"))
            continue
        ready.append((name, row))

    print(f"auto-retire — {len(led['components'])} components, "
          f"window {a.days}d, proof {MIN_OBSERVATIONS} observations\n")
    if held:
        print("  HELD:")
        for n, r, why in held:
            print(f"    {n:28} {r['verdict']:12} {why}")
    if ready:
        print("\n  READY TO RETIRE:")
        for n, r in ready:
            print(f"    {n:28} {r['verdict']:12} {by_name[n]['why'][:52]}")
    if not ready and not held:
        print("  nothing flagged.")

    try:
        HIST.parent.mkdir(parents=True, exist_ok=True)
        HIST.write_text(json.dumps(hist, indent=2))
    except Exception as e:
        print(f"  (could not persist observations: {e})")

    if not a.apply or not ready:
        if ready:
            print("\n  DRY RUN — pass --apply to write tombstones.")
        return 0

    reg = json.loads(REG.read_text())
    n = 0
    for name, row in ready:
        for h in reg["hooks"]:
            if h.get("name") == name and not h.get("retired"):
                h["retired"] = True
                h["retired_reason"] = (
                    f"auto-retired {datetime.date.today().isoformat()} — ledger verdict "
                    f"{row['verdict']} sustained across {row['streak']} observations: "
                    f"{by_name[name]['why']}. Reversible: set retired=false. "
                    f"See bin/steering-retire.py for the admissibility rules.")
                n += 1
    REG.write_text(json.dumps(reg, indent=2) + "\n")
    print(f"\n  wrote {n} tombstone(s). Run bin/reconcile-hooks.py --apply to unregister.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
