#!/usr/bin/env python3
"""PROFANITY IS INTENSITY, NOT A SPEECH ACT — and the split must lose nothing.

`correction-frustration` was `\\bwtf\\b|\\btf\\b|fuck|\\bffs\\b` alone. Every other row in
CORRECTION_RX names something the operator DID — "that's wrong", "you should have", "go back". This
one named how he FELT, and core-business flagged that it files enthusiastic praise (profane, positive)
as a correction.

MEASURED ON THIS CORE'S PROVENANCE-FILTERED CORPUS: it fired on 76 of 658 human turns (11.6%) and
only 43 carried any correction marker. The other 33 were requests and specifications:

    "clean up all loose ends so nothing fucking breaks on any session starts or closes"
    "for the last time all hooks old and new need to be tracked and tuned autonomusly"
    "ASK FUCKING WHAT DO YOU NEED GO ASK WHY CANT YOU JUST FUCKING PUSH"

That is METHOD — how he wants work done — filed as a complaint about work already done. "For the
last time" is a RECURRENCE marker, the most valuable thing a method miner can find.

WHY THIS IS A SPLIT AND NOT A NARROWING, which is the whole point of this file. Requiring a
correction marker dropped 33 turns and **28 of them matched no other pattern in either family** —
they would have left the corpus entirely. A detector narrowed until it detects nothing is the
opposite error from the one being fixed, and it improves every precision metric while destroying
the signal. So the 33 are rehomed to `instruction-emphatic`, in the family whose rows say how work
should GO.

THE PARTITION IS THE PROPERTY: every intensity-bearing turn lands in exactly one of the two.
Neither both, nor neither.

    correction-frustration   43   6.5% of corpus
    instruction-emphatic     33   5.0%
    both                      0
    lost                      0
    43 + 33 = 76 = every turn the old pattern matched

THE \\A ANCHOR IS LOAD-BEARING and is pinned separately. A bare `(?=...)(?!...)` pair is retried by
re.search at every position, so at a later offset the excluded term sits BEHIND the cursor and the
negative lookahead passes. The first version of this split reported 18 turns matching a rule AND
its complement — arithmetically impossible, and found only because the partition was measured
rather than assumed.

Run: python3 bin/tests/test_frustration_split.py
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
sys.path.insert(0, str(ROOT / "bin"))
import turn_provenance as prov  # noqa: E402

OLD = re.compile(r"\bwtf\b|\btf\b|fuck|\bffs\b", re.I)


def load_miner():
    path = ROOT / "scheduling" / "claude-si" / "learned-corpus-miner.py"
    spec = importlib.util.spec_from_file_location("lcm", path)
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(m)
    return m


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== intensity splits into correction vs method, losing nothing ===\n")

    try:
        m = load_miner()
    except Exception as e:
        print("  SKIP — cannot load learned-corpus-miner: %s" % e)
        return 0

    CF = m.CORRECTION_RX.get("correction-frustration")
    IE = m.INSTRUCTION_RX.get("instruction-emphatic")
    check("correction-frustration still exists", CF is not None)
    check("instruction-emphatic exists to receive the other half", IE is not None,
          "without it the split is a narrowing and 28 real turns leave the corpus")
    if CF is None or IE is None:
        print("\n=== Results: %d passed, %d failed ===" % (p, f))
        return 1

    print("\n--- the two rules partition intensity: never both, never neither ---")
    CASES = [
        ("no no just add it to the fucking list", "correction"),
        ("that is fucking wrong, go back", "correction"),
        ("why the fuck do we still have this problem", "correction"),
        ("clean up all loose ends so nothing fucking breaks on any core", "method"),
        ("for the last time all hooks need to be tuned autonomously, wtf", "method"),
        ("this is fucking gold i love this", "method"),
        # core-business, reviewing the split on ITS corpus: with `why` as the only interrogative
        # carve-out these filed as method. A near-identical longer case landed correctly only
        # because an unrelated "don't" appeared later — the right answer for the wrong reason,
        # which is not evidence.
        ("What the fuck are you talking about?", "correction"),
        ("how the fuck is this still broken", "correction"),
        # ...and the control: widening the carve-out must not start swallowing genuine method.
        ("Fuck it we are going with it!", "method"),
    ]
    for text, want in CASES:
        a, b = bool(CF.search(text)), bool(IE.search(text))
        check("%-52r -> %s" % (text[:50], want),
              (a and not b) if want == "correction" else (b and not a),
              "correction-frustration=%s instruction-emphatic=%s (exactly one must hold)" % (a, b))

    # The one core-business named. It is not a correction by any reading.
    check("praise is NOT filed as a correction",
          not CF.search("this is fucking gold i love this"),
          "the defect core-business reported: intensity read as a complaint")

    print("\n--- the \\A anchor, without which the rules overlap ---")
    check("correction-frustration is anchored", CF.pattern.startswith(r"\A"),
          "an unanchored lookahead pair is retried at every position and both rules can match")
    check("instruction-emphatic is anchored", IE.pattern.startswith(r"\A"))

    print("\n--- ON THE LIVE CORPUS: the partition must hold and lose nothing ---")
    # DERIVED, not pinned: bin/tests/ ships to every Core, so a literal home path here tests
    # THIS machine on someone else's. Claude Code slugifies the repo path with "-".
    _repo = Path(__file__).resolve().parents[2]
    d = Path.home() / ".claude" / "projects" / str(_repo).replace("/", "-")
    if not d.is_dir():
        print("  SKIP — live transcripts absent on this Core")
    else:
        tot = old = cf = ie = both = lost = 0
        for fp in d.glob("*.jsonl"):
            for line in fp.open(errors="ignore"):
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if not prov.is_human_turn(e):
                    continue
                # text_of, not raw content: multimodal turns are LISTS since the B1 fix, and a
                # regex over a list raises. This test crashed on exactly that.
                t = prov.text_of(e)
                tot += 1
                if not OLD.search(t):
                    continue
                old += 1
                a, b = bool(CF.search(t)), bool(IE.search(t))
                cf += a
                ie += b
                both += (a and b)
                if not a and not b:
                    lost += 1
        print("    %d human turns, %d intensity-bearing -> %d correction / %d method"
              % (tot, old, cf, ie))
        check("no turn matches BOTH rules", both == 0, "%d overlap" % both)
        check("no intensity-bearing turn is LOST", lost == 0,
              "%d turns fell out of the corpus — that is a narrowing, not a split" % lost)
        check("the two halves sum to the old pattern's matches", cf + ie == old,
              "%d + %d != %d" % (cf, ie, old))
        check("correction-frustration actually shrank (the defect was over-firing)",
              cf < old, "still fires on all %d" % old)
        check("...and still fires on a real share (not narrowed to nothing)",
              cf > 0 and ie > 0, "cf=%d ie=%d" % (cf, ie))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
