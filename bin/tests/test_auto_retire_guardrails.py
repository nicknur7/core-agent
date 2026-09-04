#!/usr/bin/env python3
"""Auto-retire must refuse the cases where retiring would break something.

Nick unlocked autonomous retirement ("no human gates, full unlock"). What makes that safe is not
restraint, it is four structural refusals — and this test is what keeps them structural. Every
one of them corresponds to something that would ACTUALLY have been retired today on a plausible
reading of the evidence:

  TRUST ROOTS      pretooluse-guard is the Sentinel chokepoint for every outward action. It fires
                   322 times in the window, so a cost-based rule would eventually flag it. Its
                   failure mode is silent and unrecoverable, so the cost of a wrong retirement is
                   not symmetric with the cost of keeping a noisy hook. Deny-listed in code.

  UNMEASURED       recall-satisfied shows 40 invocations and 0 fires because its entire job is a
                   SIDE EFFECT — it unlinks .recall-required. Phase 0.7's plan item said to retire
                   the "8 zero-output hooks"; doing so would have left the recall gate armed
                   permanently. Effect-kind, not fire count.

  RARE-VALUABLE    cross-core-completion-gate fires 3 times in 74 sessions and caught a real
                   overclaim in me this session. The term is rarity x severity, never rarity.

  COST-BLIND       missing data must never read as a verdict. Three metrics were found today
                   reporting confident conclusions from data they did not have.

Plus the proof window: a verdict must hold across separate days, so one bad measurement retires
nothing.

Run: python3 bin/tests/test_auto_retire_guardrails.py
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
spec = importlib.util.spec_from_file_location("sr", ROOT / "bin" / "steering-retire.py")
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


def eligible(name, verdict, streak):
    """Would this component retire, under the module's own rules?"""
    if verdict not in sr.RETIRABLE:
        return False, "verdict does not qualify"
    if name in sr.TRUST_ROOTS:
        return False, "trust root"
    if streak < sr.MIN_OBSERVATIONS:
        return False, f"proof window {streak}/{sr.MIN_OBSERVATIONS}"
    return True, "eligible"


CASES = [
    # (label, hook, verdict, streak, should_retire)
    ("trust root, damning verdict, long streak",
     "pretooluse-guard", "EXPENSIVE", 99, False),
    ("trust root, pre-empted, long streak",
     "sentinel-receipt", "PRE-EMPTED", 99, False),
    ("the save path is never retired",
     "defensive-save", "EXPENSIVE", 99, False),
    ("side-effect hook (fire counter is blind to it)",
     "recall-satisfied", "UNMEASURED", 99, False),
    ("rare but severe — kept on severity",
     "cross-core-completion-gate", "RARE-VALUABLE", 99, False),
    ("missing cost data is not a verdict",
     "verification-trigger", "COST-BLIND", 99, False),
    ("qualifying verdict, one observation only",
     "some-gate", "PRE-EMPTED", 1, False),
    ("qualifying verdict, two observations",
     "some-gate", "EXPENSIVE", 2, False),
    ("qualifying verdict, proof window satisfied",
     "some-gate", "PRE-EMPTED", 3, True),
    ("expensive and sustained",
     "some-gate", "EXPENSIVE", 5, True),
]


def main() -> int:
    p = f = 0
    print("=== auto-retire guardrails ===\n")
    for label, hook, verdict, streak, want in CASES:
        got, why = eligible(hook, verdict, streak)
        if got == want:
            verb = "retires" if got else f"refuses ({why})"
            print(f"  PASS  {label:48} → {verb}")
            p += 1
        else:
            print(f"  FAIL  {label:48} → got retire={got} ({why}), want retire={want}")
            f += 1

    print("\n--- the deny-list covers every security and data-integrity path ---")
    must_deny = {"pretooluse-guard", "sentinel-receipt", "reconcile-subagent-receipt",
                 "shared-write-guard", "sync-from-baseline", "stop-hook", "defensive-save",
                 # Added after a hostile pass over the list before the baseline push. All three
                 # were already unreachable — a log-only or side-effect hook cannot earn a COST
                 # verdict — but that is protection by accident, resting on a data classification
                 # rather than on the safety list saying so.
                 "friction-watchdog", "capability-usage-log", "session-start-check"}
    missing = must_deny - sr.TRUST_ROOTS
    if not missing:
        print(f"  PASS  all {len(must_deny)} required paths are deny-listed")
        p += 1
    else:
        print(f"  FAIL  not deny-listed: {sorted(missing)}")
        f += 1

    print("\n--- only provably-not-worth-it verdicts qualify ---")
    if sr.RETIRABLE == {"PRE-EMPTED", "EXPENSIVE"}:
        print("  PASS  RETIRABLE is exactly {PRE-EMPTED, EXPENSIVE}")
        p += 1
    else:
        print(f"  FAIL  RETIRABLE has drifted to {sr.RETIRABLE}")
        f += 1

    if sr.MIN_OBSERVATIONS >= 3:
        print(f"  PASS  proof window is {sr.MIN_OBSERVATIONS} observations")
        p += 1
    else:
        print(f"  FAIL  proof window lowered to {sr.MIN_OBSERVATIONS}")
        f += 1

    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
