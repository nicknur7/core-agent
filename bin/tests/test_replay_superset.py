#!/usr/bin/env python3
"""A change to a gate may not drop a confirmed true positive. CI fails if one does.

THE RULE (master plan Phase 3.3): tuning admissibility is REPLAY SUPERSET, not narrow-only.

Narrow-only says "a tune must reduce what a rule matches." core-business showed that is
unenforceable four separate ways, and my own gmail-guard tune this week proved the sharpest one:
a textual narrowing that looks strictly narrower can REMOVE REAL COVERAGE, because "narrower
regex" and "catches less of what matters" are different claims and only the second one counts.
Superset replaces it with something checkable — keep every confirmed true positive, and you may
change whatever else you like.

WHAT THIS ENFORCES, AND WHERE THE RULE ALREADY LIVED
----------------------------------------------------
The invariant was already real in two places and enforced in neither by CI:

  · friction_tune.py holds it for GENERATED artifacts: a narrowing is verified against the
    artifact's own positives, using the production matcher, before it is written.
  · bin/grade-intent.py holds it for HAND-WRITTEN gates: each replays its intent record's
    positive and negative examples and returns `rotted` when a positive stops firing.

16 gates carry 4 positives and 4 negatives each. Nothing ran grade-intent in CI, so a change
that rotted a gate would have been caught only if somebody happened to run it by hand — which is
how a gmail regression reached the baseline and was found by a peer Core instead.

`rotted` is the only failing verdict here, deliberately:
  rotted       a positive stopped firing — coverage lost. FAIL.
  imprecise    fires on its own negatives — worth knowing, not worth blocking a commit.
  unproven     no examples recorded yet — a gap in evidence, not a regression.
  ungradeable  no detect() to replay (shims, side-effect writers). Expected for most hooks.

Run: python3 bin/tests/test_replay_superset.py
"""
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
GI = ROOT / "bin" / "grade-intent.py"


def main() -> int:
    if not GI.exists():
        print(f"  SKIP — no grade-intent at {GI}")
        return 0
    spec = importlib.util.spec_from_file_location("gi", GI)
    gi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gi)

    reg = json.loads((ROOT / "bin" / "hook-registry.json").read_text())
    have = {}
    for h in reg.get("hooks", []):
        if not h.get("intent") or h.get("retired"):
            continue
        merged = dict(h["intent"])
        merged.update(gi._evidence(h["name"]))
        have[h["name"]] = merged

    saved = sys.stdin
    try:
        sys.stdin = io.StringIO("")
        gg = gi._gg()
        results = {n: gi.grade(n, i, gg) for n, i in sorted(have.items())}
    finally:
        sys.stdin = saved

    tally = {}
    for r in results.values():
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1

    print("=== replay superset — every confirmed positive must still fire ===\n")
    print(f"  {len(results)} entries with an intent record: "
          + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())) + "\n")

    rotted = {n: r for n, r in results.items() if r["verdict"] == "rotted"}
    graded = [n for n, r in results.items() if r["verdict"] in ("holds", "imprecise", "rotted")]
    covered = sum(r.get("positives", 0) for r in results.values())

    ok = True
    if rotted:
        ok = False
        print(f"  FAIL  {len(rotted)} gate(s) no longer catch their own confirmed positives:")
        for n, r in rotted.items():
            print(f"          {n}: {r['why']}")
            if r.get("example_failure"):
                print(f"            e.g. {r['example_failure'][:100]}")
    else:
        print(f"  PASS  no gate has lost coverage "
              f"({len(graded)} replayed against {covered} confirmed positives)")

    imprecise = [n for n, r in results.items() if r["verdict"] == "imprecise"]
    if imprecise:
        print(f"\n  NOTE  {len(imprecise)} gate(s) fire on their own negatives "
              f"(reported, not blocking): {', '.join(imprecise)}")

    # A replay harness with nothing to replay is the failure mode this session kept finding — a
    # check that cannot fail is indistinguishable from one that passes. But "the corpus VANISHED"
    # and "this Core never built one" are different facts and only the first is a regression.
    #
    # Found by testing the pull on core-business rather than reasoning about it: the evidence
    # store is .claude/state/hook-intent-evidence.json, which is per-Core and never synced, so a
    # freshly-pulled Core has none and this failed instantly. Shipping that would have put every
    # peer's CI permanently red for the crime of being new — and a CI that is red by default is a
    # CI nobody reads, which is worse than the gap it was guarding.
    evidence = ROOT / ".claude" / "state" / "hook-intent-evidence.json"
    if not graded:
        if not evidence.exists():
            # `note:` NOT `SKIP` — this file prints its own verdict at the end, and a leading
            # SKIP here makes run-all discard it. The absence is a legitimate per-CHECK state
            # (a new Core has no corpus); the FILE still ran and still has a verdict.
            print("\n  note: no intent-evidence corpus on this Core yet (per-Core file, never "
                  "synced). Nothing to regress against — this is a NEW Core, not a broken one. "
                  "The corpus builds as gates record their examples.")
        else:
            ok = False
            print("\n  FAIL  an evidence corpus EXISTS but no gate replayed against it — the "
                  "harness would report clean no matter what changed")

    print(f"\n=== {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
