#!/usr/bin/env python3
"""The generator must mint from the DISTILLED ask, and its trigger must be able to fire.

WHY. Every quality filter in friction_router — length, profanity, pasted-output, first-person —
was compensating for one upstream decision: the generator built its injected message from the RAW
correction text. It did not have to. ask_miner already writes canonical_ask onto
pattern_observations (287 of 443 live rows carry one) and those read as instructions:

    "act autonomously and execute without pausing to ask permission"
    "always run the full close-reconciler at session close"

Two channels over the same corpus, one distilling and one quoting, and every artifact purged on
2026-07-30 came from the quoting one. Unifying onto the distilled channel is the fix; the text
filters stay as belt-and-braces and should essentially never fire again.

Switching channels then exposed three defects in sequence, each of which produced a WORKING-LOOKING
failure, and each of which is a case below:

  1. The trigger still came from the correction, so a correct message ("fix session start/close
     reliability across all cores") was wired to \\bnothing\\b — a long word from an angry
     sentence, ranked highly because _distinctive uses LENGTH as its distinctiveness proxy.
  2. Key terms were grounded per-term across sibling prompts, so both could be well-grounded while
     NO single prompt contained both. The condition is conjunctive, so those contracts could never
     fire on anything: "positive p1 did NOT fire" on 6 of 8 routed artifacts — not a wrong
     contract, an impossible one.
  3. Grounding used substring containment (`w in text`) while the emitted condition uses
     r"\\bw\\b", so "systems" grounded a trigger on \\bsystem\\b that could never match it. A check
     deciding whether a trigger is grounded has to use the trigger's own matching semantics.

GAP C STUB (2026-08-31). `route()`'s trigger-derivation step (defect #1/#2 above) now grounds
against a live call, `ask_miner.sibling_moments_for_ask(org, canonical_ask)`, instead of reading
`case["support"]["members"]` — see friction_router.py's own TRIGGER DERIVATION comment and
bin/tests/test_router_on_subject_trigger.py's module docstring for the full rationale (grounding by
pattern_label mixed in every unrelated ask of the same correction TYPE; this case's own fixtures
built `members` to be exactly the ask's siblings, so the bug that motivated the move never applied
to them). Checked directly against corebrain: case 2's ask ("use codex alongside core for
substantial system and code work") returns ZERO live rows — it was never typed into a real session
— so without this stub `route()` now refuses every case below that carries a `members` list, not
because the fix under test is wrong but because this file's fixtures predate the live-DB grounding
call. `main()` patches `ask_miner.sibling_moments_for_ask` to read `members` back out of `case()`'s
own dict instead, keeping this suite's original DB-free determinism intact.

Run: python3 bin/tests/test_distilled_mint.py
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))

spec = importlib.util.spec_from_file_location(
    "friction_router", ROOT / "scheduling" / "claude-si" / "friction_router.py")
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)


# GAP C STUB (2026-08-31) — see module docstring. Keyed on canonical_ask, same shape as
# test_router_on_subject_trigger.py's identical stub, since both suites hit the identical
# DB-dependency defect from the same GAP C change.
_ASK_SIBLINGS: dict[str, list[str]] = {}


def _stub_sibling_moments(org, canonical_ask, limit=30):
    return list(_ASK_SIBLINGS.get(canonical_ask, []))


def case(canonical, correction, members, sessions=3):
    if canonical:
        _ASK_SIBLINGS[canonical] = members
    return {
        "case_id": "fc_test", "org_id": 1,
        "quality": {"eligible_for_routing": True},
        "moment": {"correction": correction},
        "user_wanted": correction,
        "canonical_ask": canonical,
        "support": {"cluster_key": "instruction-standing", "distinct_sessions": sessions,
                    "positive_case_ids": ["fc_a", "fc_b"],
                    "members": [{"case_id": f"fc_{i}", "prompt_text": m}
                                for i, m in enumerate(members)]},
    }


NEIGHBORS = ["a totally different subject about etsy listings and photographs",
             "please look at the calendar for next week", "what is the weather"]


def main() -> int:
    # GAP C STUB (2026-08-31) — see module docstring. Patched onto the `ask_miner` module object
    # directly (fr's internal `import ask_miner as am` resolves via sys.modules regardless of how
    # friction_router itself was loaded above), restored in `finally`.
    import ask_miner as am
    _orig_sibling_fn = am.sibling_moments_for_ask
    am.sibling_moments_for_ask = _stub_sibling_moments
    try:
        return _run()
    finally:
        am.sibling_moments_for_ask = _orig_sibling_fn


def _run() -> int:
    p = f = 0
    print("=== the generator mints from the distilled ask ===\n")

    # 1 — no canonical form: refuse rather than quote.
    got = fr.route(case(None, "GO FIX THE FUCKING SESSION THING", ["session thing", "session thing"]),
                   neighbors=NEIGHBORS)
    if got is None:
        print("  PASS  no canonical_ask → refuses to mint (fail toward silence)")
        p += 1
    else:
        print(f"  FAIL  minted from a raw quote: {got['effect']['message'][:70]}")
        f += 1

    # 2 — with a canonical form: the MESSAGE is the distilled instruction, not the quote.
    c = case("use codex alongside core for substantial system and code work",
             "why the fuck arent you using codex on this system code work again",
             ["use codex for this system code work", "codex should do the system code work"])
    got = fr.route(c, neighbors=NEIGHBORS)
    if got and "codex" in got["effect"]["message"].lower() and "fuck" not in got["effect"]["message"].lower():
        print("  PASS  message is the distilled ask, not the correction")
        p += 1
    else:
        print(f"  FAIL  message: {got['effect']['message'][:80] if got else None}")
        f += 1

    # 3 — the trigger comes from the ask's subject, never from incidental venting.
    if got:
        trig = [t["value"] for t in got["condition"]["all"] if t.get("op") == "prompt_regex"]
        if all("nothing" not in t and "fuck" not in t for t in trig):
            print(f"  PASS  trigger is on-subject: {trig}")
            p += 1
        else:
            print(f"  FAIL  trigger drawn from the frustration: {trig}")
            f += 1

        # 4 — the trigger must be FIRABLE: its terms must co-occur in a real prompt, using the
        #     same word-boundary semantics the condition itself applies.
        import re
        mems = [m["prompt_text"] for m in c["support"]["members"]]
        firable = any(all(re.search(t, mp, re.I) for t in trig) for mp in mems)
        if firable:
            print("  PASS  trigger terms co-occur in a real prompt — the contract can fire")
            p += 1
        else:
            print(f"  FAIL  no member prompt matches all of {trig} — unfirable contract")
            f += 1

        # 5 — the positive fixture is a real prompt the trigger actually matches.
        pos = (got.get("_examples") or {}).get("positive") or []
        if pos:
            ptext = pos[0]["hook_input"]["prompt"]
            if all(re.search(t, ptext, re.I) for t in trig):
                print("  PASS  positive fixture is matched by the emitted condition")
                p += 1
            else:
                print(f"  FAIL  positive fixture does not match the trigger: {ptext[:60]!r}")
                f += 1

    # 6 — word-boundary semantics: 'systems' must not ground a trigger on \bsystem\b.
    if fr._occurs("system", "the systems are down") is False and fr._occurs("system", "the system is down"):
        print("  PASS  grounding uses word boundaries, matching the emitted condition")
        p += 1
    else:
        print("  FAIL  _occurs does not match the condition's semantics")
        f += 1

    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
