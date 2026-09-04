#!/usr/bin/env python3
"""A BLOCKING ARTIFACT MUST PROVE ITS TRIGGER IS NARROW. The path for blocks had that check OFF.

`friction_test_gate.gate()` measures what fraction of REAL past prompts a trigger fires on and
refuses above 3%. `friction_installer.install()` supplies a corpus and is held to it. The separate
path that installs BLOCKING artifacts called the same gate with `corpus_prompts=None` — and None
makes the specificity test fall through BOTH branches, silently, so no measurement happened at all.

The file argues against itself. Fifty lines above the call:

    "A block should be held to a higher bar than an inject, not a lower one."

It was held to no bar. Injects were checked at 3%; blocks were unchecked.

DOSED WITH A REAL TRIGGER. core-business found four artifacts in its live state keyed on
\\b(want|company|they|core|really|work)\\b. Against this seat's actual corpus that fires on 83 of
150 prompts — 55.3%. An artifact that can stop work, firing on more than half of everything Nick
types.

BOTH DIRECTIONS, because "refuse everything" is the trivial way to pass a narrowness test and would
disable enforcement wherever it was switched on:

    over-broad trigger   -> REFUSED
    narrow trigger       -> still reaches the rest of the gate

AND AN EMPTY CORPUS MUST REFUSE, NOT PASS. If the corpus cannot be fetched, specificity is
unprovable, and for an artifact that can stop work unprovable must fail closed — otherwise a
database hiccup is indistinguishable from a narrow trigger.

Run: python3 bin/tests/test_block_proves_specificity.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SI = ROOT / "scheduling" / "claude-si"
sys.path.insert(0, str(SI))

OVERBROAD = r"\b(want|company|they|core|really|work)\b"
NARROW = r"\bpush the migration to staging\b"


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== a block must prove its trigger is narrow ===\n")

    # Resolve from IDENTITY, then export. setdefault(..., "1") pinned this to org 1 on any
    # seat where the variable was unset (core-ops, 2026-08-26).
    import sys as _s
    from pathlib import Path as _PP
    _s.path.insert(0, str(_PP(__file__).resolve().parents[2] / "scheduling" / "brain-pg"))
    from _env import get_org_id as _goi
    os.environ["CORE_ORG_ID"] = str(_goi())
    try:
        import friction_dispatch as fd
        import friction_test_gate as tg
    except Exception as e:
        print("  SKIP — claude-si modules unavailable: %s" % e)
        return 0

    print("--- the shipped block path supplies a corpus (the defect was passing None) ---")
    src = (SI / "friction_installer.py").read_text()
    check("the block path no longer calls the gate with corpus_prompts=None",
          "tg.gate(spec, examples, corpus_prompts=None)" not in src,
          "None makes the specificity test fall through both branches without measuring anything")
    check("...and it fetches a real corpus for the block gate",
          "_fetch_corpus_prompts(org)" in src.split("block_gate_fail")[0][-1200:],
          "the block path must measure against real prompts, like install() does")

    print("\n--- the measurement itself, on a synthetic corpus with a known answer ---")
    # Built here rather than pulled from the DB so the numbers are decidable: 60 prompts, exactly
    # 40 containing "work". A gate that measured nothing would show 0%.
    corpus = ["please work on the deploy" for _ in range(40)] + \
             ["ship the thing" for _ in range(20)]

    def rate(pattern):
        cond = {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                        {"op": "prompt_regex", "value": pattern}]}
        fires = sum(1 for x in corpus
                    if fd.evaluate(cond, fd._normalize({"prompt": x, "event": "UserPromptSubmit"},
                                                       "UserPromptSubmit")))
        return fires / len(corpus)

    over, narrow = rate(OVERBROAD), rate(NARROW)
    check("the over-broad trigger really is over-broad here (%.0f%%)" % (over * 100),
          over > tg.OVERBROAD_RATE, "measured %.2f, threshold %.2f" % (over, tg.OVERBROAD_RATE))
    check("...and the narrow one really is narrow (%.0f%%)" % (narrow * 100),
          narrow <= tg.OVERBROAD_RATE, "measured %.2f" % narrow)

    print("\n--- the gate's verdict on each, through the SHIPPED gate function ---")

    def spec_for(pattern):
        return {"artifact_id": "art_probe", "event": "UserPromptSubmit",
                "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                                      {"op": "prompt_regex", "value": pattern}]},
                "effect": {"mode": "block", "message": "no"},
                "tests": {"positive_ids": ["p1"], "negative_ids": ["n1", "n2"]}}

    examples = {"positive": [], "negative": []}   # incomplete on purpose; see below
    ok_over, why_over = tg.gate(spec_for(OVERBROAD), examples, corpus_prompts=corpus)
    check("an over-broad block is REFUSED", not ok_over, why_over)

    # The example-shape checks run BEFORE the corpus check, so an empty examples dict is refused for
    # that reason and would mask the thing under test. Assert the REASON, not just the verdict.
    ok2, why2 = tg.gate(spec_for(OVERBROAD),
                        {"positive": [{"id": "p1", "expected": "fire"}],
                         "negative": [{"id": "n1", "expected": "no_fire", "provenance": "a"},
                                      {"id": "n2", "expected": "no_fire", "provenance": "b"}]},
                        corpus_prompts=corpus)
    check("...and refused FOR OVER-BREADTH specifically, not an unrelated shape error",
          not ok2 and "over-broad" in why2,
          "reason was %r — a verdict that matches expectation for the wrong reason is not evidence"
          % why2)

    print("\n--- an unprovable corpus must fail closed ---")
    # COMPLETE examples here. Passing the incomplete dict refused on example SHAPE before the
    # corpus check was ever reached, so the assertion below read "cannot prove specificity" against
    # a message about positives and negatives — the fixture failing to construct its own case, with
    # the failure pointing at the subject. Same shape as every other one caught this week.
    _full = {"positive": [{"id": "p1", "expected": "fire"}],
             "negative": [{"id": "n1", "expected": "no_fire", "provenance": "a"},
                          {"id": "n2", "expected": "no_fire", "provenance": "b"}]}
    ok3, why3 = tg.gate(spec_for(NARROW), _full, corpus_prompts=[])
    check("an EMPTY corpus refuses rather than passing", not ok3, why3)
    check("...saying it cannot prove specificity", "corpus" in why3.lower(), why3)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
