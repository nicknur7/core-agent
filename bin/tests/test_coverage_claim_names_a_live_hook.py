#!/usr/bin/env python3
"""A COVERED_CONCEPTS entry may not defer to a hook that is registered nowhere.

WHY THIS EXISTS (2026-08-18).

`artifact_typer.COVERED_CONCEPTS` is how the loop refuses to build something a hook already
enforces. Each value is a claim about the running system — `hook:state-claim-gate` asserts that a
gate by that name is executing. The claim was hardcoded, and hardcoded claims about the running
system rot in exactly one direction: silently, toward suppression.

On 2026-08-06 nine Stop gates were retired fleet-wide under Nick's policy that nothing may drive
the agent after the reply is sent. FOUR of this map's five `hook:` targets went with them —
state-claim-gate, recall-gate, approval-gate, say-do-gap — and the map went on reporting all four
as coverage on every seat for twelve days.

business measured the bill on its own corpus and it is not small:

    19x  recall from the brain before answering about past work  -> already_covered (recall-gate)
     8x  verify state against the live source before claiming    -> already_covered (state-claim-gate)

Its single most-recurrent correction produced nothing, deferred to enforcement that no seat runs.
Both asks are literally two of the three anti-patterns CLAUDE.md says nothing enforces.

It left no symptom because the suppression branch wrote no log row — the one outcome in the whole
dispatch with no artifact to point at. Same shape as `test_block_install_needs_a_live_dispatcher`
one layer down: a check that compares against a STORED COPY of the truth cannot notice the truth
moved. There the stored copy was a hash-pinned template's event; here it is a dict value.

WHAT THIS ASSERTS.
  1. Every `hook:` target in the map is registered in the live settings.json — the data check, and
     the one that fires the next time a hook is retired.
  2. `route_type` does not terminate on an unregistered hook claim for an ask that is still
     recurring — the mechanism check, which fails if the resolution is ever reverted to faith.

No hardcoded list of live hooks appears below. That would be a second place recording which hooks
run, and it would go stale exactly as the map did.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

import artifact_typer as T  # noqa: E402

checks = 0
live = T.live_hook_names()

# The resolver must actually resolve something, or assertion 1 passes vacuously by failing open.
assert live, "live_hook_names() returned empty — settings.json unreadable, so this test proves nothing"
checks += 1

# --- 1) every hook: claim in the map names a hook the runtime executes -------------------------
dead = sorted({v.split(":", 1)[1] for v in T.COVERED_CONCEPTS.values()
               if v.startswith("hook:") and v.split(":", 1)[1] not in live})
assert not dead, (
    "COVERED_CONCEPTS defers to hook(s) registered in no settings.json: " + ", ".join(dead) +
    " — every ask matching those concepts is suppressed by enforcement that does not run. "
    "Repoint each at its LIVE successor, or demote it to rule: if it has none.")
checks += 1

# --- 2) an unregistered hook claim does not terminate the route for a recurring ask -------------
FAKE = "hook:a-gate-that-was-retired-and-is-not-registered-anywhere"
CONCEPT = "zz test concept for the coverage resolver"
saved = dict(T.COVERED_CONCEPTS)
try:
    T.COVERED_CONCEPTS[CONCEPT] = FAKE
    r = T.route_type(CONCEPT, still_recurring=True)
    assert r["type"] != "already_covered", (
        "a dead hook still suppressed a recurring ask -> " + repr(r) +
        " — the coverage claim is being taken on faith again")
    checks += 1

    # ... and a LIVE hook still does settle it, or assertion 2 would pass by breaking coverage.
    live_one = sorted(live)[0]
    T.COVERED_CONCEPTS[CONCEPT] = "hook:" + live_one
    r2 = T.route_type(CONCEPT, still_recurring=True)
    assert r2["type"] == "already_covered", (
        "a registered hook no longer settles a concept -> " + repr(r2) +
        " — dedupe against real enforcement is broken, which is the opposite failure")
    checks += 1
finally:
    T.COVERED_CONCEPTS.clear()
    T.COVERED_CONCEPTS.update(saved)

print(f"ok — {checks} checks: {len(live)} live hooks, "
      f"{sum(1 for v in T.COVERED_CONCEPTS.values() if v.startswith('hook:'))} hook: claims all resolve")
