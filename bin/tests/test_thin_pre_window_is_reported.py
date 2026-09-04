#!/usr/bin/env python3
"""A thin pre-window is REPORTED even when the verdict is already refusing for another reason.

WHY THIS EXISTS (2026-08-18, found by core-business).

`_apply_power_floor` downgrades any comparative verdict whose pre-window is under `MIN_PRE_N`. It
short-circuited on `_NON_COMPARATIVE`, which contains `"INSUFFICIENT"` — and
`"INSUFFICIENT-CONFOUNDED"` starts with `"INSUFFICIENT"`. So whenever the confound test fired first,
this floor never ran and the confound label won every tie.

business measured its live fitness file: **5 of 11 contracts have a pre-window under MIN_PRE_N and
all five reported as CONFOUNDED.** The clean case is `plan-not-execute` at **pre 0, post 0**, labelled
*"the windows differ so much in ACTIVITY that a per-calendar-day rate cannot be compared"* — a
statement about a comparison with no operands on either side.

NO BEHAVIOUR CHANGES. Both labels are INSUFFICIENT-family and nothing outside the module acts on
either. What changes is the reason a human reads, and the two reasons have opposite remedies:

    CONFOUNDED    windows not comparable by activity  -> a better denominator would help
    UNDERPOWERED  nothing in the pre-window at all    -> a denominator does nothing; needs data

Reporting the first when the second is true invites exactly one wrong conclusion: that fixing the
denominator unblocks these contracts. On business it would unblock six of eleven, not all — which is
scoping evidence for a metric change, and it was invisible while the label was wrong.

MY FIRST FIX WAS REFUSED BY THE SUITE, AND IT WAS RIGHT TO REFUSE IT. I re-labelled these to
UNDERPOWERED; `test_underpowered_verdict_not_actionable.py:92-93` asserts by name that they must not
be, with a stated reason — *"already refuses — must not be double-downgraded"*. That is a deliberate
prior decision with a test guarding it, and a drive-by relabel would have overwritten someone's
reasoning on the strength of a peer's message.

Both positions are about different things and neither has to lose. The prior decision is about the
VERDICT: an already-refusing verdict must not be re-refused, because nothing downstream distinguishes
them and a churning label is how `fire_count:0 -> GRADUATED` once read as health. business's finding
is about the REASON, and the reason matters because the remedies diverge:

    CONFOUNDED    windows not comparable by activity  -> a better denominator would help
    UNDERPOWERED  nothing in the pre-window at all    -> a denominator does nothing; needs data

So the label stays and the count is appended. The human reads both facts.

WHAT THIS ASSERTS.
  1. An INSUFFICIENT-family verdict with a thin pre-window KEEPS its label and gains the count in its
     rationale — the half business measured.
  2. It is NOT re-labelled — the half the prior decision protects, and the one my first attempt broke.
  3. STRUCTURAL non-comparatives (UNENFORCEABLE / GATED-WATCH / QUARANTINED) gain nothing and change
     nothing: they are non-comparative for reasons unrelated to sample size.
  4. A sufficient pre-window leaves both label and rationale untouched.
  5. Comparative verdicts still hit the floor in BOTH directions — it must cost claimed successes too.
  6. The note is not appended twice if the rationale already carries it.
"""
import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "scheduling" / "claude-si" / "measure-contract-fitness.py"
spec = importlib.util.spec_from_file_location("mcf", SRC)
m = importlib.util.module_from_spec(spec)
sys.modules["mcf"] = m
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

f = m._apply_power_floor
THIN = m.MIN_PRE_N - 1
checks = 0

assert m.MIN_PRE_N >= 1 and "INSUFFICIENT" in m._NON_COMPARATIVE, "module contract changed — retarget"
checks += 1

# 1+2) the count is reported, the label is NOT changed
for v in ("INSUFFICIENT-CONFOUNDED", "INSUFFICIENT"):
    for pre in (0, THIN):
        out, why = f(v, "orig-reason.", pre)
        assert out == v, "%s at pre=%d was re-labelled to %s — the prior decision at " \
                         "test_underpowered_verdict_not_actionable.py:92-93 forbids that" % (v, pre, out)
        assert "MIN_PRE_N" in why and str(pre) in why, \
            "%s at pre=%d does not report the thin pre-window: %r" % (v, pre, why)
        assert "orig-reason." in why, "original rationale dropped for %s" % v
checks += 2

# 3) structural non-comparatives: untouched, label and reason both
for v in ("UNENFORCEABLE", "GATED-WATCH", "QUARANTINED"):
    out, why = f(v, "orig-reason.", 0)
    assert out == v and why == "orig-reason.", \
        "%s was modified (-> %s / %r); it is non-comparative for reasons unrelated to sample size" % (v, out, why)
checks += 1

# 4) a sufficient pre-window changes nothing
out, why = f("INSUFFICIENT-CONFOUNDED", "orig-reason.", m.MIN_PRE_N)
assert out == "INSUFFICIENT-CONFOUNDED" and why == "orig-reason.", "well-powered row was annotated: %r" % why
checks += 1

# 5) comparative verdicts still hit the floor, both directions
for v in ("NOT-BINDING-FIRED", "GRADUATED"):
    out, _ = f(v, "orig", THIN)
    assert out == "INSUFFICIENT-UNDERPOWERED", "%s at pre=%d survived the floor" % (v, THIN)
    keep, _ = f(v, "orig", m.MIN_PRE_N)
    assert keep == v, "%s at pre=MIN_PRE_N was downgraded" % v
checks += 1

# 6) idempotent — a re-measured row must not accrete the note twice
once, why1 = f("INSUFFICIENT-CONFOUNDED", "orig-reason.", 0)
twice, why2 = f(once, why1, 0)
assert why2 == why1, "the under-powered note was appended twice on re-measurement"
checks += 1

print("ok — %d checks: MIN_PRE_N=%d, label preserved, thin pre-window reported"
      % (checks, m.MIN_PRE_N))
