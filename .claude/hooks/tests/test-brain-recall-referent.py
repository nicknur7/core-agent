#!/usr/bin/env python3
"""brain-recall-trigger must fire on names and not on things that merely contain them.

WHY THIS TEST EXISTS. The hook built one regex per file in memory/relationships/ and fired
wherever the slug appeared. `jordan.md` exists, so a stray lowercase mention of "jordan" in an
unrelated sentence fired a full recall advisory and set the .recall-required response gate.
Graded against 353 real prompts, 11 of 34 person-slug matches were not references to the
person at all.

Every MUST_FIRE row below is shaped on a REAL true positive pulled from that same corpus (names
anonymized for this public repo — the grammar and code path are unchanged), and they are here
for a specific reason: the plan's admissibility rule (3.3) is REPLAY SUPERSET, not narrow-only.
core-business proved narrow-only unenforceable, and my own gmail-guard tune removed real
coverage while looking like a narrowing. A tune is admissible only if it keeps every confirmed
true positive. If a future tightening drops one of these, this test fails.

The MUST_NOT_FIRE rows are shaped on the measured false positives, same anonymization.

Run: python3 .claude/hooks/tests/test-brain-recall-referent.py
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[3])
HOOK = ROOT / ".claude" / "hooks" / "brain-recall-trigger.py"

spec = importlib.util.spec_from_file_location("brt", HOOK)
brt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brt)


# SYNTHETIC PATTERNS, not this Core's actual roster — and that correction came from running the
# suite on core-business after a pull. The first version called brt._person_patterns(), which
# reads memory/relationships/ — a per_core_keep directory. Every Core's roster is different (life's
# three real contacts are not business's three), so nine fixtures naming life's people "DROPPED a
# true positive" on business, and the suite would have been red on every peer forever.
#
# Third instance of the same mistake in one push (the steering budget and the replay corpus were
# the others), all three found by actually testing the pull rather than reasoning about it. The
# subject under test is the REFERENT LOGIC — determiners, extensions, brand words, verb particles
# — and that logic has nothing to do with who this Core happens to know. Building the patterns
# here makes the test measure its own subject and behave identically on every Core.
import re as _re

_FIXTURE_NAMES = ["jordan", "briar", "dana"]
_FIXTURE_PATTERNS = [(n, _re.compile(rf"\b{_re.escape(n)}\b", _re.I)) for n in _FIXTURE_NAMES]


def person_hits(text):
    return [s for s, p in _FIXTURE_PATTERNS if brt._referent_hit(p, text)]


# Real corpus lines. Each names a person and must keep firing.
MUST_FIRE = [
    ("plain reference",        "The whole idea with Jordan was to create a baseline repo where he could pull from"),
    ("subject of a verb",      "Next time Jordan sync pulls his stuff, it is clean and works."),
    ("possessive",             "Nail down Jordan's relationship shape (beta user / operational partner)"),
    ("lowercase, object",      "go see the most recent email from jordan"),
    ("lowercase, vocative",    "jordan i feel like that info is stale? also adress the rest"),
    ("determiner + capital",   "Maintain the Dana Whitfield relationship, close the thank-you loop"),
    ("possessive, capital",    "for Briar's birthday on Monday, I know there is a spot in town"),
    ("lowercase in a list",    "ordered all stuff for briar and her birtday passed"),
    ("the plan's own fixture", "Jordan said the sync was clean on his end"),
]

# Real corpus lines. Each contains a name and refers to no person.
MUST_NOT_FIRE = [
    ("determiner, lowercase", "circle back and make sure we push this to the jordan before end of "
                              "day, no more waffling."),
    ("verb particle",    "let's jordan out the context window on this one"),
    ("product tier",     "i dont want use it lightly i just upgraded to the 20X jordan plan so i want it baked in"),
    ("pasted statusline","Opus 4.8 with high effort - Claude Jordan   ~/AI Projects/core-ops   1 MCP server"),
    ("file path",        "sentinel: jordan.md (x2) -> a life-Core example used to illustrate a verdict"),
    ("file path, quoted","the `jordan.md` example verdict, or a cross-Core ref to another instance"),
    ("path segment",     "check memory/relationships/jordan.md and see whether the shape line is current"),
]


def main() -> int:
    p = f = 0
    print("=== brain-recall-trigger: name matches must be referents ===\n")
    print("--- MUST FIRE (corpus true positives — the superset floor) ---")
    for label, text in MUST_FIRE:
        hits = person_hits(text)
        if hits:
            print(f"  PASS  fires {hits}: {label}")
            p += 1
        else:
            print(f"  FAIL  DROPPED a true positive: {label}\n        {text[:90]}")
            f += 1
    print("\n--- MUST NOT FIRE (measured false positives) ---")
    for label, text in MUST_NOT_FIRE:
        hits = person_hits(text)
        if not hits:
            print(f"  PASS  silent: {label}")
            p += 1
        else:
            print(f"  FAIL  fired {hits} on a non-referent: {label}\n        {text[:90]}")
            f += 1
    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
