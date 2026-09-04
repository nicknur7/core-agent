#!/usr/bin/env python3
"""A trigger that MATCHED must never be re-derived. The state has to change the decision.

WHY THIS EXISTS (2026-08-13, master-plan Phase 4).

Phase 4 asks for "matched but did not fire" as a first-class, **CONSUMED** state. `ecec751` added
the `matched` field to `dispatch_nofire`, which made it READABLE — and nothing read it. A field
written and never consulted is the void-write shape that same session spent hours cataloguing, and
this instance was in the Phase 4 work itself. Measured before fixing: 7 `dispatch_nofire` rows on
disk, 0 carrying `matched`, and the only greps for the key were this suite and a different field of
the same name on reply-observer rows.

WHAT CONSUMES IT NOW, and it is a decision rather than a report. `classify_artifact_health` returns
`flag_rederive` justified by "its trigger NEVER MATCHED while the correction recurred". That
sentence is an INFERENCE from `fire_count == 0`, and it collapses two states the dispatcher already
distinguishes:

    considered=19, matched=[]         no trigger fits the traffic   -> re-derive is right
    considered=19, matched=[art_x]    art_x matched and was DROPPED -> re-derive is WRONG

In the second case the trigger works and something downstream ate the result. Re-deriving throws
away a working trigger for a fresh guess — `friction_dispatch.py:624` names it exactly: "re-deriving
a rule whose trigger is fine is how a working artifact gets replaced by a worse one."

WHAT THIS ASSERTS. Not that the field exists — that was the half already done and it changed
nothing. It asserts the VERDICT MOVES: same artifact, same fitness input, one `matched` row, and
the action must stop being `flag_rederive`. A state that cannot change an outcome is telemetry, and
Phase 4 did not ask for telemetry.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_matched_is_consumed")
    try:
        import friction_loop as fl
        import friction_installer as inst
    except Exception as exc:
        check("friction_loop imports", False, str(exc))
        print(f"\n{len(passes)} passed, {len(failures)} failed")
        return 1

    check("the consumer exists", hasattr(fl, "_matched_without_firing"),
          "no _matched_without_firing — dispatch_nofire.matched is written and read by nothing, "
          "which is the state Phase 4 asked to close")
    if not hasattr(fl, "_matched_without_firing"):
        print(f"\n{len(passes)} passed, {len(failures)} failed")
        return 1

    AID = "art_probe_matched_consumed"
    now = int(time.time())
    original = inst.ACTION_LOG
    try:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "actions.jsonl"
            inst.ACTION_LOG = log

            # ---- the reader ------------------------------------------------------------------
            log.write_text(json.dumps({"action": "dispatch_nofire", "matched": [AID], "ts": now}))
            check("a matched row is counted", fl._matched_without_firing(AID) == 1)

            log.write_text(json.dumps({"action": "dispatch_nofire", "matched": [], "ts": now}))
            check("an empty matched list is not counted",
                  fl._matched_without_firing(AID) == 0)

            # ABSENT IS NOT EMPTY. Rows written before ecec751 carry no `matched` key at all. An
            # old row cannot testify that the trigger failed to match — it did not record the
            # answer — and counting it as a non-match would manufacture support for exactly the
            # re-derivation this guard prevents.
            log.write_text(json.dumps({"action": "dispatch_nofire", "considered": 19, "ts": now}))
            check("a pre-ecec751 row with NO matched key is not read as a non-match",
                  fl._matched_without_firing(AID) == 0,
                  "an absent field was treated as evidence of absence")

            # A NON-LIST `matched` MUST NOT BE SUBSTRING-MATCHED. This assertion exists because a
            # dose that SHOULD have failed did not: replacing the isinstance guard with
            # `aid not in (m or [])` passed every check above, since both forms handle a missing
            # key identically. The distinction only appears when `matched` is a STRING — then `in`
            # silently becomes a substring test, and a row mentioning the id anywhere would vouch
            # for a trigger that never matched. Substring-where-exact-is-required is the recurring
            # family in this repo; the guard is real and was untested until the dose came back green.
            log.write_text(json.dumps(
                {"action": "dispatch_nofire", "matched": f"prefix-{AID}-suffix", "ts": now}))
            check("a STRING matched field is refused, not substring-searched",
                  fl._matched_without_firing(AID) == 0,
                  "a malformed non-list `matched` was substring-matched, so any row merely "
                  "mentioning the artifact id would vouch for a trigger that never matched")

            # Windowed, like _fire_count. A match from months ago says nothing about today's
            # traffic, and comparing a lifetime match against a 30-day fire count is the
            # two-different-frames error this file already carries scars from.
            old = now - 400 * 86400
            log.write_text(json.dumps({"action": "dispatch_nofire", "matched": [AID], "ts": old}))
            check("a match outside the window is not counted",
                  fl._matched_without_firing(AID) == 0,
                  "an ancient match still vouches for a trigger's current fitness")

            # ---- THE DECISION, which is the point --------------------------------------------
            art = {"artifact_id": AID, "case_id": "case_probe",
                   "effect": {"mode": "inject", "message": "probe"}}

            log.write_text(json.dumps({"action": "dispatch_nofire", "matched": [], "ts": now}))
            no_match = fl.classify_artifact_health(
                None, 1, art, set(), not_binding_artifacts={AID})

            log.write_text(json.dumps({"action": "dispatch_nofire", "matched": [AID], "ts": now}))
            matched = fl.classify_artifact_health(
                None, 1, art, set(), not_binding_artifacts={AID})

            check("with NO match recorded the verdict is still flag_rederive",
                  no_match.get("action") == "flag_rederive",
                  f"got {no_match.get('action')!r} — the honest action for a trigger that never "
                  "matched is re-derivation, and this change must not have removed it")

            check("ONE matched row changes the verdict away from flag_rederive",
                  matched.get("action") != "flag_rederive",
                  f"got {matched.get('action')!r}. The trigger demonstrably matched, so this is a "
                  "suppression problem; re-deriving discards a WORKING trigger for a fresh guess. "
                  "If the verdict cannot move, the state is telemetry and Phase 4 asked for a "
                  "consumed state.")

            check("...and it says WHY, so the reader knows to hunt the suppression",
                  "match" in (matched.get("why") or "").lower(),
                  f"rationale does not mention matching: {matched.get('why')!r}")

            # And the new action must be HANDLED, not silently dropped. Every action in tune_pass
            # is an explicit `if action == ...` branch; an unhandled one falls through to
            # `untouched`, which for a new state is indistinguishable from never computing it.
            src = (REPO / "scheduling" / "claude-si" / "friction_loop.py").read_text()
            act = matched.get("action")
            check(f"the new action ({act}) has an explicit handler in tune_pass",
                  f'action == "{act}"' in src,
                  f"no `if action == \"{act}\"` branch — the verdict would be computed and then "
                  "fall through every branch, counted as untouched")
    finally:
        inst.ACTION_LOG = original

    check("the live action log path was restored", inst.ACTION_LOG == original,
          "this test left friction_installer pointed at a temp path")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
