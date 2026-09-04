#!/usr/bin/env python3
"""The ledger must never report a verdict it does not have the data for.

WHY. Three times this session a metric was found reporting a VERDICT where it actually had
missing or mismatched data, and each time the wrong answer looked exactly like a right one:

  · contract-fitness said DECAYING on all six contracts — it was comparing two detector
    generations across the exact date they changed over.
  · steering_liveness said "runs but never matches" for four hooks whose entire job is a side
    effect the fire counter structurally cannot see.
  · the first cut of THIS tool said "EARNING at 0.0 tok/session" for every component, because
    tokens_injected has only been recorded since hooklog.emit landed on 2026-07-30.

That last one is the reason this test exists. A ledger is the thing the retire/keep decisions get
made from, so it is the one place where "I don't know" must never render as a pass. COST-BLIND is
that answer, and these assertions keep it reachable.

Run: python3 bin/tests/test_steering_ledger.py
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
spec = importlib.util.spec_from_file_location("sl", ROOT / "bin" / "steering-ledger.py")
sl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sl)


def verdict_for(fires, tokens, effect, invokes=0, name="x", older=0, newer=0, live=True):
    """Drive the classifier directly with a synthetic component.

    `live` STUBS registered_now(), and it has to. When the UNREGISTERED branch landed it was placed
    ahead of every other verdict — correctly, since a component absent from settings.json has no
    measurable cost — and a synthetic name is absent from the REAL settings.json by construction.
    So every case here started returning UNREGISTERED and ten assertions stopped testing anything.

    THE TELL WAS IN THE PASSES, NOT THE FAILURES. Two assertions were written as `!= "LOW-YIELD"`,
    and a constant verdict satisfies a negative assertion forever. They reported PASS across the
    whole outage. Both are pinned to exact verdicts below — a negative assertion cannot distinguish
    a right answer from a stuck one, which is this session's rule applied to a test rather than to
    a gate: the answer must be shown to depend on the input.
    """
    saved = (sl.load_rows, sl.registry, sl.registered_now)
    try:
        rows = []
        base = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        td = __import__("datetime").timedelta
        per = (tokens // max(1, fires)) if fires else 0
        for i in range(older):
            rows.append({"hook": name, "verdict": "inject", "session": "s", "tok": per,
                         "_ts": base - td(days=10)})
        for i in range(newer):
            rows.append({"hook": name, "verdict": "inject", "session": "s", "tok": per,
                         "_ts": base - td(days=1)})
        for i in range(max(0, fires - older - newer)):
            rows.append({"hook": name, "verdict": "inject", "session": "s", "tok": per,
                         "_ts": base - td(days=5)})
        for i in range(invokes):
            rows.append({"hook": name, "verdict": "invoke", "session": "s", "tok": 0,
                         "_ts": base - td(days=5)})
        sl.load_rows = lambda days: rows
        sl.registry = lambda: {name: {"event": "UserPromptSubmit",
                                      "intent": {"effect": effect, "guards": "g" * 50}}}
        # live=None means "leave registered_now alone" — the caller is stubbing it itself.
        if live is not None:
            sl.registered_now = (lambda: {name}) if live else (lambda: {"some-other-hook"})
        out = sl.build(14)["components"][0]
        return out["verdict"]
    finally:
        sl.load_rows, sl.registry, sl.registered_now = saved


CASES = [
    # (label, fires, tokens, effect, invokes, expected)
    ("fires with NO cost data must not read as a pass", 40, 0, "inject", 0, "COST-BLIND"),
    ("a blocking gate with no cost data likewise",       12, 0, "block",  0, "COST-BLIND"),
    ("side-effect hook, zero fires, is UNMEASURED",       0, 0, "side-effect", 5, "UNMEASURED"),
    ("log-only hook, zero fires, is UNMEASURED",          0, 0, "log-only", 3, "UNMEASURED"),
    ("registered with no telemetry at all is NO-DATA",    0, 0, "inject", 0, "NO-DATA"),
    ("ran but never had to act is RARE-VALUABLE",         0, 0, "inject", 29, "RARE-VALUABLE"),
]


def main() -> int:
    p = f = 0
    print("=== steering ledger: a verdict requires the data behind it ===\n")
    for label, fires, tok, eff, inv, want in CASES:
        got = verdict_for(fires, tok, eff, invokes=inv)
        if got == want:
            print(f"  PASS  {label}  → {got}")
            p += 1
        else:
            print(f"  FAIL  {label}  → got {got}, want {want}")
            f += 1

    print("\n--- cost-bearing components are judged on cost, not on fire count ---")
    # 40 fires x 200 tok = 8000 tok over 1 session, behaviour flat → EXPENSIVE.
    got = verdict_for(40, 8000, "inject", older=10, newer=10)
    if got == "EXPENSIVE":
        print(f"  PASS  high cost + flat behaviour → EXPENSIVE")
        p += 1
    else:
        print(f"  FAIL  high cost + flat behaviour → got {got}, want EXPENSIVE")
        f += 1
    # Same cost, behaviour clearly declining → EARNING.
    got = verdict_for(40, 8000, "inject", older=30, newer=5)
    if got == "EARNING":
        print(f"  PASS  high cost + declining behaviour → EARNING")
        p += 1
    else:
        print(f"  FAIL  high cost + declining behaviour → got {got}, want EARNING")
        f += 1

    print("\n--- LOW-YIELD: distinguished from RARE-VALUABLE by the DENOMINATOR ---")
    # These two carry the SAME small fire count and opposite meanings. Only invocations tell them
    # apart, which is why this verdict could not exist until every gate recorded them.
    got = verdict_for(1, 100, "block", invokes=25)      # learned-validator's real shape
    if got == "LOW-YIELD":
        print("  PASS  1 fire in 26 runs → LOW-YIELD")
        p += 1
    else:
        print(f"  FAIL  1-in-26 → got {got}, want LOW-YIELD")
        f += 1
    # PINNED TO THE EXACT VERDICT, not to "anything but LOW-YIELD". Written as negatives, these two
    # reported PASS for the entire time every case in this file was stuck on UNREGISTERED — a
    # negative assertion is satisfied by any wrong answer, including a constant one.
    got = verdict_for(4, 100, "block", invokes=0)       # cross-core-completion-gate's real shape
    if got == "EARNING":
        print("  PASS  4 fires with no run history → EARNING, not LOW-YIELD")
        p += 1
    else:
        print(f"  FAIL  4 fires with no run history → got {got}, want EARNING")
        f += 1
    got = verdict_for(15, 400, "block", invokes=25)     # acts 37% of the time
    if got == "EARNING":
        print("  PASS  a gate acting 37% of runs → EARNING, not LOW-YIELD")
        p += 1
    else:
        print(f"  FAIL  37% yield → got {got}, want EARNING (threshold drifted?)")
        f += 1

    print("\n--- UNREGISTERED: the branch that broke this file, now doubly dosed ---")
    # It outranks every cost verdict, so if it is ever wrong it is wrong about EVERYTHING. Both
    # directions, on ONE differing input: the same component, registered and not.
    got_live = verdict_for(40, 8000, "inject", older=10, newer=10, live=True)
    got_dead = verdict_for(40, 8000, "inject", older=10, newer=10, live=False)
    if got_dead == "UNREGISTERED" and got_live == "EXPENSIVE":
        print("  PASS  identical telemetry → EXPENSIVE registered, UNREGISTERED absent from settings")
        p += 1
    else:
        print(f"  FAIL  registration must decide this: live={got_live} (want EXPENSIVE), "
              f"absent={got_dead} (want UNREGISTERED)")
        f += 1
    # And the guard on it: an UNREADABLE settings.json returns empty, which must NOT mass-relabel
    # the estate as unregistered. Absence of knowledge is not knowledge of absence.
    saved_rn = sl.registered_now
    try:
        sl.registered_now = lambda: set()
        got = verdict_for(40, 8000, "inject", older=10, newer=10, live=None)
    finally:
        sl.registered_now = saved_rn
    if got == "EXPENSIVE":
        print("  PASS  empty settings read → falls back to the cost verdict, relabels nothing")
        p += 1
    else:
        print(f"  FAIL  unreadable settings.json relabelled a live component: got {got}")
        f += 1

    print("\n--- every verdict the classifier can emit is reachable in the display order ---")
    # A verdict missing from `order` sorts to the bottom silently. LOW-YIELD and UNREGISTERED are
    # both retirement-relevant and both were absent — the reader would see them last, under
    # EARNING, which reads as "least urgent" exactly when it is not.
    emitted = {"EXPENSIVE", "PRE-EMPTED", "COST-BLIND", "NO-DATA", "UNMEASURED",
               "RARE-VALUABLE", "EARNING", "LOW-YIELD", "UNREGISTERED"}
    missing = sorted(emitted - set(sl.DISPLAY_ORDER))
    if not missing:
        print("  PASS  all %d verdicts carry an explicit sort rank" % len(emitted))
        p += 1
    else:
        print(f"  FAIL  {missing} emitted but absent from the sort order — sorts last silently")
        f += 1

    print("\n--- pre-emption: the mechanism works, and the list is empty on purpose ---")
    # The mechanism, exercised with a synthetic entry rather than a live one.
    sl.PRE_EMPTED["synthetic-gate"] = "its failure is impossible by construction"
    try:
        got = verdict_for(69, 0, "block", name="synthetic-gate")
        if got == "PRE-EMPTED":
            print("  PASS  a listed component reads PRE-EMPTED ahead of any cost verdict")
            p += 1
        else:
            print(f"  FAIL  synthetic-gate → got {got}, want PRE-EMPTED")
            f += 1
    finally:
        sl.PRE_EMPTED.pop("synthetic-gate", None)

    # time-claim-gate was listed here and the entry was WRONG, which is worth pinning so it does
    # not come back. The clock pre-empts the time-of-day half and not the duration/session-start
    # half — the gate is still the only thing gating "we started at 09:14". A partial pre-emption
    # is a payload-reduction candidate, never a retirement candidate, and because hook-registry
    # is baseline-shared a tombstone from life's telemetry would have retired it on all four
    # peers. (Fable, blast-radius review.)
    if "time-claim-gate" not in sl.PRE_EMPTED:
        print("  PASS  time-claim-gate is NOT pre-empted — only half its job is")
        p += 1
    else:
        print("  FAIL  time-claim-gate is back in PRE_EMPTED; re-read why that retires a live gate")
        f += 1

    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
