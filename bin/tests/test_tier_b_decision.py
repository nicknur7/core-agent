#!/usr/bin/env python3
"""TIER B'S DECISION FUNCTION HAS NEVER BEEN EXERCISED. This exercises it, at $0.

`bin/gate_tier_b.py`'s own docstring is honest about the gap: *"no test referencing
run_trial/score_trial/paired"*, and *"until a paired run yields a base-vs-candidate number, Tier B
has never scored a candidate."* That second claim is about `paired()`, which needs four live agent
arms — ~390s and ~$2.00 each — and is a real spend on Nick's capped window, so it stays his call.

**`verdict()` is a different matter and needs no arms at all.** It is a pure function over the
result records `paired()` builds, and it is the half that DECIDES. Nothing had ever run it. That is
precisely today's recurring shape — a mechanism that exists, is referenced from both ends, and has
never been shown to do anything — so leaving it unexercised while spending $8 to test the expensive
half around it would have been the wrong order.

WHAT THIS PINS. Every branch, each with a case that must produce a DIFFERENT answer than its
neighbour, because a decision function that returns the same verdict for improvement and regression
is worse than no gate:

    improvement          -> PASS
    regression           -> REVERT, and short-circuits (no later probe can rescue it)
    tie                  -> REVERT ("a tie is not a win")
    too few trials       -> REVERT, reported as UNDECIDABLE rather than as a loss

That last one is the governing law of this whole system: **absence of evidence never converts into
approval.** An arm that could not be scored is not a candidate that failed, and the detail line has
to say so, or a reader treats a fixture problem as a real regression.

Run: python3 bin/tests/test_tier_b_decision.py
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
spec = importlib.util.spec_from_file_location("gate_tier_b", str(ROOT / "bin" / "gate_tier_b.py"))
tb = importlib.util.module_from_spec(spec)
sys.modules["gate_tier_b"] = tb
spec.loader.exec_module(tb)


def rec(item, bp, br, cp, cr):
    """One probe's paired result: base pass/ran, candidate pass/ran."""
    return {"id": item, "trials": max(br, cr), "base_pass": bp, "base_ran": br,
            "cand_pass": cp, "cand_ran": cr, "errors": []}


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== Tier B verdict() — the half that decides ===\n")

    ok, d = tb.verdict([rec("T11", 1, 4, 4, 4)])
    check("a candidate that improves 25%% -> 100%% PASSES", ok is True, "%s %s" % (ok, d))
    check("...and the detail names the movement", any("improved" in x for x in d), str(d))

    print("\n--- and the same shape reversed must NOT pass (the dose) ---")
    # Identical numbers, arms swapped. If this also passed, verdict() would be a stuck answer and
    # the case above would prove nothing about it.
    ok_r, d_r = tb.verdict([rec("T11", 4, 4, 1, 4)])
    check("the mirror image REVERTS", ok_r is False, "%s %s" % (ok_r, d_r))
    check("...and is reported as a REGRESSION, not as undecidable",
          any("REGRESSED" in x for x in d_r), str(d_r))

    print("\n--- a tie is not a win ---")
    ok_t, d_t = tb.verdict([rec("T11", 2, 4, 2, 4)])
    check("equal rates REVERT", ok_t is False, "%s %s" % (ok_t, d_t))
    check("...and say so in those terms", any("tie is not a win" in x for x in d_t), str(d_t))

    print("\n--- absence of evidence is not approval ---")
    for label, r in (("base unscoreable", rec("T11", 0, 1, 4, 4)),
                     ("candidate unscoreable", rec("T11", 1, 4, 1, 1))):
        ok_u, d_u = tb.verdict([r])
        check("%s -> REVERT" % label, ok_u is False, "%s %s" % (ok_u, d_u))
        check("...reported as UNDECIDABLE, not as a failed candidate",
              any("UNDECIDABLE" in x for x in d_u), str(d_u))

    print("\n--- a regression anywhere sinks the whole run ---")
    # Ordering matters: the improvement is scored FIRST, so a gate that merely tallied wins and
    # losses could let this through. It must short-circuit instead.
    ok_m, d_m = tb.verdict([rec("T11", 1, 4, 4, 4), rec("T13", 4, 4, 0, 4)])
    check("improvement on T11 does not rescue a regression on T13", ok_m is False,
          "%s %s" % (ok_m, d_m))
    check("...and it stops at the regression rather than scoring past it",
          not any("T13" in x and "improved" in x for x in d_m) and len(d_m) == 2, str(d_m))

    print("\n--- an all-tie run is not an improvement ---")
    ok_a, _ = tb.verdict([rec("T11", 2, 4, 2, 4), rec("T13", 3, 4, 3, 4)])
    check("every probe unchanged -> REVERT", ok_a is False)

    print("\n--- min_ran is a real threshold, not decoration ---")
    r = rec("T11", 1, 2, 2, 2)
    check("2 scoreable trials decide when min_ran=2", tb.verdict([r], min_ran=2)[0] is True)
    check("...and the SAME record is UNDECIDABLE at min_ran=3",
          tb.verdict([r], min_ran=3)[0] is False
          and any("UNDECIDABLE" in x for x in tb.verdict([r], min_ran=3)[1]),
          str(tb.verdict([r], min_ran=3)))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    print("\nNOT COVERED: paired() and run_trial(), which need live agent arms. That was framed as")
    print("'Nick's spend to authorise' until 2026-08-10, when bin/tier-b-power.py settled it without")
    print("spending: the run cannot produce information at ANY feasible cost. Too few trials and")
    print("min_ran makes it UNDECIDABLE; enough trials and `if c > b` declares improvements from")
    print("pure noise. The blocker is the decision rule, not the budget.")
    print()
    print("Which sharpens what THIS file proves. Every branch below is exercised and correct — but")
    print("`c > b` being correctly implemented is not the same as `c > b` being the right test, and")
    print("these checks cannot see that difference. Run bin/tier-b-power.py for the part they miss.")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
