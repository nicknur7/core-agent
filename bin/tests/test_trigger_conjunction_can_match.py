#!/usr/bin/env python3
"""A conjunctive trigger must match a REAL prompt as a whole, not term by term.

WHY THIS EXISTS (2026-08-20, measured by core-ops).

`_wf_trigger_terms` validates each term individually — "does this word appear in at least one stored
prompt?" — and then combines them with `all`. So three terms can each match a DIFFERENT prompt while
the conjunction matches NONE. The artifact installs, its positive test passes term-by-term, and the
condition can never fire.

Not hypothetical. ops installed `art_wf72bf83d8ec6f7b5e` from this path and reported it after four
days: **zero fires, and the failure it was mined from recurred anyway.** Its condition:

    \\bclaude\\b  AND  \\bchrome\\b  AND  \\bright\\b

Three words that never co-occur in one prompt on that seat.

THIS IS THE OPPOSITE FAILURE FROM THE ONE THE SPECIFICITY BAR CATCHES, and the fleet has one of each
from the same weak extraction:

    over-broad    stopword alternations firing on 60%+ of everything    (business, school)
    over-narrow   an AND of three words that never co-occur             (ops)

core-finance named the gap hours before ops measured it: *"a specificity floor with no sensitivity
ceiling admits inert triggers silently."* A floor refuses rules that fire on everything. Nothing
refused a rule that fires on nothing — and a rule that never fires is indistinguishable from a rule
that works, in every counter the loop publishes.

WHAT THIS ASSERTS.
  1. ops's exact shape — each term groundable, the conjunction not — is narrowed until it matches.
  2. A conjunction that DOES match a real prompt is left intact.
  3. An ask with no groundable term still returns nothing (the pre-existing refusal is preserved).
  4. Whatever comes back, some real prompt satisfies ALL of it. That is the invariant; the rest is
     how it is reached.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

import friction_loop as F  # noqa: E402

checks = 0

# 1) ops's case: three groundable terms, no prompt holds all three
split = ["use claude in chrome for this", "the chrome tab is on the left", "that is the right approach"]
terms = F._wf_trigger_terms("retarget claude chrome right pane", split)
assert terms, "narrowed all the way to nothing on a case where a pair does co-occur"
assert len(terms) < 3, "kept a conjunction no single prompt satisfies: %s" % terms
checks += 1

# 2) intact when a real prompt satisfies the whole thing
whole = ["retarget claude chrome to the right pane now", "unrelated"]
kept = F._wf_trigger_terms("retarget claude chrome right pane", whole)
assert len(kept) >= 3, "narrowed a conjunction that a real prompt already satisfies: %s" % kept
checks += 1

# 3) the pre-existing refusal survives — a workflow whose prompts state no intent is still refused
assert F._wf_trigger_terms("continue the thing", ["sent them", "ok go"]) == [], \
    "a workflow with no groundable term must return nothing, not a rule grounded in itself"
checks += 1

# 4) THE INVARIANT, checked directly rather than inferred from the two cases above
for name, prompts in (("retarget claude chrome right pane", split),
                      ("retarget claude chrome right pane", whole),
                      ("verify a claim adversarially before it reaches nick",
                       ["verify the claim first", "adversarially check it", "verify a claim adversarially"])):
    got = F._wf_trigger_terms(name, prompts)
    if not got:
        continue
    rx = [re.compile(t) for t in got]
    assert any(all(r.search(p.lower()) for r in rx) for p in prompts), \
        ("returned a conjunction no stored prompt satisfies: %s\n"
         "   -> the artifact would install, pass its term-by-term test, and never fire" % got)
checks += 1

print("ok — %d checks: conjunctions narrowed until a real prompt satisfies them, refusal preserved"
      % checks)
