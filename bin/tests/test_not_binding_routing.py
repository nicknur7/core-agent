#!/usr/bin/env python3
"""A NOT-BINDING artifact must reach flag_rederive — and must never reach narrowing.

WHY THIS EXISTS
---------------
`measure-contract-fitness.py` emits two families of verdict:

    contracts     ... NOT-BINDING              (legacy learned_contract slugs)
    si_artifacts  ... NOT-BINDING-FIRED
                      NOT-BINDING-NO-FIRE      (artifacts the loop itself generated)

Only the first was ever collected into `contract-fitness.json`. `friction_loop.tune_pass()` loaded
that one list and matched it BY SUBSTRING against `artifact_id` / `case_id` — but a slug like
`plan-not-execute` never appears inside an `art_9f3c…` hash, so the `flag_rederive` branch could not
fire for anything the loop built. Found 2026-08-05 while auditing the tuner.

It was latent, not live: at the time every artifact read GRADUATED / GRADUATED-UNPROVEN /
INSUFFICIENT, so nothing was being mishandled — the failure would have appeared the first time a
genuinely ineffective artifact was measured, and it would have appeared as silence.

The routing itself matters more than the plumbing. These are opposite diagnoses:

    NOT-BINDING   the rule fires and the correction recurs anyway -> it is INEFFECTIVE.
                  Narrowing makes it fire less, which is strictly worse. Re-derive it.
    OVER-FIRING   the rule fires far more often than the correction occurs -> it is NOISY.
                  Narrowing is exactly right.

A tuner that conflated them would "fix" an ineffective rule by making it quieter. So this test pins
both halves: NOT-BINDING must route to flag_rederive, and must never route to narrow.

Run: python3 bin/tests/test_not_binding_routing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    try:
        import friction_loop as fl
    except Exception as exc:
        print(f"  FAIL  import friction_loop — {exc}")
        return 1

    ART_ID = "art_deadbeefdeadbeef0001"
    art = {"artifact_id": ART_ID, "case_id": "ask_some-recurring-thing",
           "effect": {"mode": "inject", "message": "a reminder"}}

    # classify_artifact_health short-circuits on the not-binding branch BEFORE it touches the
    # database, so a None cursor is safe here and keeps the test hermetic. If a future edit moves a
    # DB call above that branch this test fails loudly with a TypeError, which is the correct signal.
    def classify(nb=frozenset(), nba=frozenset()):
        return fl.classify_artifact_health(None, 1, art, set(nb), set(nba))

    # 1 — the artifact-id path, the one that was dead
    h = classify(nba={ART_ID})
    check("NOT-BINDING artifact id routes to flag_rederive",
          h.get("action") == "flag_rederive", f"got {h!r}")
    check("...and is classed not_binding, not over_firing",
          h.get("class") == "not_binding", f"got class={h.get('class')!r}")
    check("...and is NEVER routed to narrow",
          h.get("action") != "narrow", f"got action={h.get('action')!r}")

    # 2 — the legacy slug path must still work (substring match against case_id)
    h = classify(nb={"some-recurring-thing"})
    check("legacy contract slug still routes to flag_rederive",
          h.get("action") == "flag_rederive", f"got {h!r}")

    # 3 — a DIFFERENT artifact's id must not match this one
    h = classify(nba={"art_deadbeefdeadbeef9999"})
    # PINNED TO THE EXACT CLASS, not to "anything but not_binding". All three of these were negative
    # assertions, and a negative assertion is satisfied by ANY wrong answer — a bug that misroutes
    # every artifact to `fossil_trigger` or `over_firing` passes all three while the routing is
    # completely broken. Verified: the correct verdict for every no-match case is class="silent",
    # action="none". Third instance of this shape found today; the other two were in
    # test_steering_ledger.py, where two `!= "LOW-YIELD"` assertions reported PASS through an outage
    # that had frozen every case to one constant.
    check("a different artifact id does not match -> silent/none",
          (h.get("class"), h.get("action")) == ("silent", "none"), f"got {h!r}")

    # 4 — artifact ids are matched EXACTLY, never by substring. A truncated id must not match, or an
    #     unrelated artifact whose hash shares a prefix would be flagged as someone else's failure.
    h = classify(nba={ART_ID[:12]})
    check("a truncated artifact id does NOT substring-match -> silent/none",
          (h.get("class"), h.get("action")) == ("silent", "none"), f"got {h!r}")

    # 5 — empty inputs must be inert, and must not crash on a pre-2026-08-05 fitness file that has
    #     no not_binding_artifacts key at all (the consumer defaults it to an empty set).
    h = classify()
    check("no verdicts at all -> silent/none",
          (h.get("class"), h.get("action")) == ("silent", "none"), f"got {h!r}")
    try:
        fl.classify_artifact_health(None, 1, art, set())   # old 4-arg call shape
        ok = True
    except TypeError as exc:
        ok = False
        detail = str(exc)
    check("the pre-existing 4-argument call shape still works",
          ok, locals().get("detail", ""))

    print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
