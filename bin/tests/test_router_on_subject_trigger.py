#!/usr/bin/env python3
"""A trigger must be ON THE SUBJECT of the ask, or the router must mint nothing.

WHY THIS EXISTS
---------------
friction_router states its own two requirements, both mandatory:

    on-subject   drawn from the ask, or you get \\bnothing\\b
    occurring    present in what Nick actually TYPES, or the contract never fires

and then had a fallback that kept `occurring` and abandoned `on-subject`:

    words = _grounded or [w for w in _distinctive(correction, n=8) if ...]

justified as *"better an on-corpus trigger than none"*. Measured across the 22 live artifacts on
2026-08-06, every one of the three whose trigger shares NO topical word with its own ask came through
that branch:

    "warn before usage or cost approaches a spend limit"        fires on: fucked, close
    "answer design questions independently before approval"     fires on: come, well
    "monitor external model usage during autonomous execution"  fires on: whatever, working

An off-subject trigger is not a weaker on-subject one. It cannot fire on its subject at all — the
spend warning arrives when Nick swears, which coincides with the spend moment by accident — so it is
injected-token cost with no reachable benefit, and "none" was the better option the whole time.

The file already carried the argument against its own fallback, a few blocks below it: *"A generator
that cannot distill must REFUSE rather than quote... Fail-toward-silence is correct for a steering
surface: a missing nudge costs nothing, a bad one costs every turn it fires on."*

WHAT THIS GUARDS. `trigger_is_fossil` in friction_loop detects the defect AFTER installation. This
asserts the router cannot create it in the first place — the mint-time half. Both are needed: the
detector cleans up the three already live, this stops the next three.

It also pins the invariant that made the fallback harmless-until-moved: `canonical_ask` is now
required BEFORE the trigger is derived, not by a gate 120 lines further down. That ordering was the
only thing stopping `_canon_for_key or correction` from being a second off-subject source.

GAP C (2026-08-31) MOVED THE GROUNDING SOURCE, NOT THE INVARIANT. `route()` used to ground
candidate words in `case["support"]["members"]` — a field this test's own fixtures populate
directly, with no I/O. GAP C replaced that read with a live call, `ask_miner.
sibling_moments_for_ask(org, canonical_ask)`, because `support["members"]` is bucketed by
pattern_label (the correction's TYPE), not by ask — the wrong corpus for most cases (see
friction_router.py's own TRIGGER DERIVATION comment, and tests/test_trigger_grounding.py, which
locks the fix against the live corpus). That is a real, separately-verified improvement for real
cases with real DB history.

It also means this suite's hand-built fixtures — synthetic asks that were never typed into a real
session — now query a DB that has nothing to return for them. Checked directly against corebrain,
2026-08-31: the "good" on-subject fixture's ask ("use codex alongside core for substantial system
work") returns ZERO rows. That is not the production guarantee failing; it is this test's own
plumbing no longer matching where `route()` gets its grounding evidence from. The guarantee this
suite exists to lock — an on-subject, recurring case still mints — is real and still holds (verified
above against live data); what changed is which call has to be fed the fixture's `member_texts`.
So `main()` below stubs exactly that one call, `ask_miner.sibling_moments_for_ask`, to return each
case's own `member_texts` — restoring this suite's original DB-free determinism without touching
friction_router.py and without reintroducing `support["members"]` as a production grounding source.

Run: python3 bin/tests/test_router_on_subject_trigger.py
"""
from __future__ import annotations

import re
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


# GAP C STUB (2026-08-31) — see module docstring. `route()` now grounds its trigger candidates by
# calling `ask_miner.sibling_moments_for_ask(org, canonical_ask)` instead of reading
# `case["support"]["members"]`. Every `_case()` below still authors a `member_texts` list — the
# fixture's INTENDED sibling evidence — so this dict mirrors it under the same key the live call
# would use, and `_stub_sibling_moments` (patched onto `ask_miner` in `main()`) hands it back. One
# fixture, one lookup, no live DB dependency for a suite whose whole point is to be self-contained.
_ASK_SIBLINGS: dict[str, list[str]] = {}


def _stub_sibling_moments(org, canonical_ask, limit=30):
    return list(_ASK_SIBLINGS.get(canonical_ask, []))


def _case(canonical_ask, correction, member_texts):
    """A friction case shaped the way friction_miner writes them."""
    _ASK_SIBLINGS[canonical_ask] = member_texts
    return {
        "case_id": "fc_testcase0000000000000000",
        "org_id": 1,
        "canonical_ask": canonical_ask,
        "correction": correction,
        "user_wanted": canonical_ask,
        "status": "recurring",
        # distinct_sessions >= 2 AND a non-empty positive_case_ids are BOTH required by the recurrence
        # gate. My first fixture omitted positive_case_ids, so every case was refused there and the
        # on-subject control could not distinguish "the fix broke minting" from "the fixture was
        # incomplete" — which is the whole job of a control.
        "support": {"count": len(member_texts), "distinct_sessions": 2,
                    "positive_case_ids": [f"fc_sibling{i}" for i, _ in enumerate(member_texts)],
                    "members": [{"prompt_text": t} for t in member_texts]},
        "quality": {"eligible_for_routing": True},
    }


# route() takes a corpus-neighbour list and REFUSES without at least one real negative — a Codex
# re-review requirement, so an artifact cannot ship without a prompt it provably does not fire on.
# Traced by instrumenting every `return None` in route(): the on-subject control was dying here, at
# the very last gate, purely because the fixture passed none. That is the second time this fixture was
# incomplete in a way that looked like the fix under test had broken something — hence the tracer
# rather than a third guess.
NEIGHBORS = [
    "what is on the calendar tomorrow",
    "summarise the last session for me",
    "check whether the deploy finished",
    "how much did that cost",
]


def _trigger_words(spec):
    """The topical words a routed spec's condition fires on."""
    if not spec:
        return None
    cond = spec.get("condition") or {}
    out = []
    for c in cond.get("all", []):
        if str(c.get("op", "")).startswith("prompt"):
            v = re.sub(r"\\.", " ", str(c.get("value") or ""))
            out += [w.lower() for w in re.findall(r"[A-Za-z]{3,}", v)]
    return sorted(set(out))


def main() -> int:
    try:
        import friction_router as fr
    except Exception as exc:
        print(f"  FAIL  import friction_router — {exc}")
        return 1

    # GAP C STUB (2026-08-31) — see module docstring's "GAP C MOVED THE GROUNDING SOURCE" section.
    # `route()` resolves `ask_miner.sibling_moments_for_ask` off the shared module object at call
    # time (it does `import ask_miner as am` internally), so patching the attribute here reaches
    # every `fr.route(...)` call below, fossils and the good case alike, without touching
    # friction_router.py itself. Restored in `finally` so this file never leaves a live module
    # patched for whatever imports it next.
    import ask_miner as am
    _orig_sibling_fn = am.sibling_moments_for_ask
    am.sibling_moments_for_ask = _stub_sibling_moments
    try:
        return _run(fr)
    finally:
        am.sibling_moments_for_ask = _orig_sibling_fn


def _run(fr) -> int:
    src = (REPO / "scheduling" / "claude-si" / "friction_router.py").read_text()

    # ── the fallback is gone from LIVE CODE (the text survives in the comment explaining it) ──
    live = [ln for ln in src.splitlines()
            if "_grounded or [" in ln and not ln.strip().startswith("#")]
    check("the correction-vocabulary fallback is gone from live code",
          not live, str(live))
    # SITE RENAMED, NOT REMOVED (GAP C, 2026-08-31). `words = _grounded` was replaced by
    # `key = am._rank_ask_terms(_canon_for_key, _sib_prompts)` — the ranking moved into ask_miner
    # so friction_router's fc_-lane and ask_miner's own ask lane share one validated algorithm
    # instead of two. The invariant this line checks — the trigger's terms come from the ask
    # (`_canon_for_key`) and its real siblings (`_sib_prompts`), never from `correction` or an
    # `or`-fallback — is unchanged; only the call site moved, so the anchor moves with it.
    check("...and the trigger terms are ranked from the ask-grounded set alone "
          "(post-GAP-C: `key = am._rank_ask_terms(_canon_for_key, _sib_prompts)`, "
          "the renamed site `words = _grounded` used to anchor)",
          re.search(r"^\s*key = am\._rank_ask_terms\(_canon_for_key,\s*_sib_prompts\)\s*$",
                    src, re.M) is not None)

    # ── canonical_ask required BEFORE the trigger is derived, not 120 lines later ──
    # ANCHOR MOVED WITH THE DERIVE SITE (GAP C, 2026-08-31): the candidate set used to be built by
    # `_cands = _distinctive(...)`, now it is `_sib_prompts = am.sibling_moments_for_ask(...)`. The
    # ordering invariant this guards — `_canon_for_key` must be required before ANY derivation call
    # runs, so a missing distilled form can never fall back to deriving off raw `correction` — is
    # unchanged; confirmed directly in friction_router.py that the `if not _canon_for_key: return
    # None` gate still precedes the (renamed) derive call.
    i_req = src.find("if not _canon_for_key:")
    i_derive = src.find("_sib_prompts = am.sibling_moments_for_ask(")
    check("canonical_ask is required BEFORE the trigger is derived "
          "(the ordering that kept `or correction` from being a second off-subject source; "
          "anchor updated post-GAP-C to the renamed derive site)",
          0 <= i_req < i_derive, f"require@{i_req} derive@{i_derive}")
    check("...and the `or correction` fallback in the candidate set is gone",
          "_distinctive(_canon_for_key or correction" not in src)

    # ── THE REAL TEST: the three live fossils must now be refused ──
    # Reconstructed from the actual asks and the vocabulary of the corrections they were mined from.
    fossils = [
        ("warn before usage or cost approaches a spend limit",
         "are we close to getting fucked on usage here",
         ["are we close to getting fucked on usage",
          "we close to fucked on the window again"]),
        ("answer design questions independently before seeking approval",
         "well come on just decide it yourself",
         ["well come on you decide", "well come on and just pick one"]),
        ("monitor external model usage during autonomous execution",
         "whatever keep working on it",
         ["whatever keep working", "whatever just keep working on that"]),
    ]
    for ask, corr, members in fossils:
        spec = fr.route(_case(ask, corr, members), NEIGHBORS)
        words = _trigger_words(spec)
        if spec is None:
            check(f"REFUSED (correct): {ask[:46]}", True)
            continue
        # If it still mints, every trigger word must appear in the ask — that is the invariant.
        ask_words = set(re.findall(r"[a-z]+", ask.lower()))
        off = [w for w in (words or [])
               if w not in ("userpromptsubmit",)
               and not any((w + s) in ask_words for s in ("", "s", "es", "ed", "d", "ing"))]
        check(f"minted but ON-SUBJECT: {ask[:38]}", not off,
              f"off-subject trigger words {off} for ask {ask!r}")

    # ── and it must NOT have become so strict that a good case is refused ──
    good = _case("use codex alongside core for substantial system work",
                 "you should use codex for this",
                 ["can you use codex on this system work",
                  "use codex for the system work here"])
    spec = fr.route(good, NEIGHBORS)
    check("an ON-SUBJECT case is NOT refused — the fix removed a bad fallback, it did not zero "
          "out minting (which is the outcome the fallback was originally added to avoid)",
          spec is not None,
          "refused; trace which gate before assuming the fixture is at fault")
    if spec is not None:
        w = _trigger_words(spec)
        ask_words = set(re.findall(r"[a-z]+", good["canonical_ask"].lower()))
        check("an ON-SUBJECT case still mints, and its trigger words come from the ask",
              all(any((x + s) in ask_words for s in ("", "s", "es", "ed", "d", "ing"))
                  for x in w if x != "userpromptsubmit"), str(w))

    # ── cross-check against the post-install detector: they must agree ──
    try:
        import friction_loop as fl
        for ask, corr, members in fossils:
            spec = fr.route(_case(ask, corr, members), NEIGHBORS)
            if spec is None:
                continue
            art = {"artifact_id": "t", "effect": {"message": ask},
                   "condition": spec.get("condition")}
            check(f"anything the router still mints is not a fossil: {ask[:34]}",
                  fl.trigger_is_fossil(art) is None,
                  "router minted what the detector calls a fossil — the two halves disagree")
    except Exception as exc:
        print(f"  SKIP  cross-check against trigger_is_fossil — {str(exc)[:60]}")

    print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
