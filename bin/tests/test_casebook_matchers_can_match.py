#!/usr/bin/env python3
"""
AUTHORED BY core-finance (T012, 2026-08-13). INSTALLED BY core-life, VERBATIM — it already
uses the upward-walk root form, so it needed no change to run from bin/tests/.

finance cannot write bin/tests (pull-only Core, shared-write-guard): it authors and RUNS,
life reviews and installs. Author and runner being different seats is the evidence.
T012 — planted-defect regime for the casebook STATIC matchers S3 and S4.

AUTHORED AND RUN BY core-finance, 2026-08-13, per the T012 routing (bus #1094 -> #1105 -> #1387):
finance authors and RUNS, life installs. `.claude/hooks/casebook_matchers.py` is the TRUST PATH and
is baseline-shared, so this ships as a proposal.

    intended install path: bin/tests/test_casebook_matchers_can_match.py

WHY. Same argument as the detector regime, one layer over: S3 and S4 grade every steering document
in the fleet, and a matcher that silently stops matching reports ZERO VIOLATIONS in exactly the way
a clean corpus does. The casebook's own score is then computed from that zero. `scanned-zero !=
found-zero`, applied to the instrument the scores are made of.

The matchers are imported from the trust path and called directly — never re-derived here. A probe
that re-implements the rule it is checking can disagree with the thing it probes, which is the
defect the trust-path import exists to prevent (casebook-run.py:355).

STRUCTURE. For each matcher, four kinds of planted text:

    POSITIVE        must FIRE   — the violation the matcher exists to catch
    NEAR-MISS       must STAY CLEAN — prose that resembles it and is legitimate
    FENCE           must STAY CLEAN — the POSITIVE, verbatim, inside a code fence
    SECOND KEY      the honour_markers contract, in BOTH directions

The near-miss is the load-bearing half: a matcher that fires on everything reports violations as
confidently as one that works, and it inflates the casebook score in the safe-looking direction.

THE SECOND KEY is worth its own note. `honour_markers` defaults to False so that a caller who
forgets it is STRICTER, never laxer — the exemption requires an explicit opt-in at the call site.
Both directions are asserted, because a default that silently honoured markers would let any
document exempt itself and nothing would look different.

CHARACTERIZATION, NOT ENDORSEMENT (the two `LINE-SCOPED` checks). Both matchers test the whole LINE,
so a completion word or an instrument word anywhere on it vouches for an unrelated claim:

    "- write the summary (the file was created earlier)"   S3 stays silent; the write is not done
    "- coverage is 85% and nothing was measured"           S4 stays silent; nothing was measured

Those two assertions record what the code DOES; they are not claims that it is right. They are
written as characterization so that scoping either matcher to the action it is actually about turns
them red on purpose, with this docstring saying why. **A test that pins current behaviour without
saying so is how a defect becomes a requirement** — core-finance argued exactly that against a
regression test of core-life's earlier tonight (#1384), so it would be poor form to do it silently
here.

LIVE EXPOSURE OF THAT WEAKNESS, MEASURED ON THIS SEAT: nil. access-log.md has 262 lines, 66 listy,
2 carrying an S3 action verb, and both are suppressed by a done-word — but reading them shows both
suppressions are CORRECT: the real stamp is a structural `**Result:** DONE` prefix at column 12 and
the action verb appears later inside quoted narrative. A gap-from-the-action-verb heuristic called
both suspicious and was measuring the wrong thing, because the format puts the stamp FIRST. So the
defect is real as a matcher property and has zero consequence here. `defect-exists !=
consequence-follows`, and the proxy pointed backwards.

SCOPE — NOT COVERED. S1, S2 and S5 are in bin/casebook-run.py rather than the trust path, and S5
takes no argument: it reads DOC_TARGETS off the real repo, so planting for it needs a temp Core tree
and a redirected REPO rather than a string. That is a separate probe and this one does not pretend
to cover it. Nothing here tests casebook-run.py's WIRING either — that STATIC maps these names to
these functions, and that anything consumes the result.

Read-only. Compiles the matcher module from source text (never importlib: a .pyc validates on
(mtime, size), and a same-size fix arriving by rsync would be answered for by stale bytecode — which
is the exact situation a non-author seat re-checking a landed fix is in). Writes nothing, touches no
live state.

Run: python3 tasks/si-verification/probes/authored_T012_casebook_static_regime.py
"""
import sys
import types
from pathlib import Path


def _find_root(start: Path) -> Path:
    """Walk up for the directory that CONTAINS the matchers, never a fixed depth.

    A sibling probe used `parents[3]`, which encodes the AUTHORING depth and resolves to the wrong
    directory once installed at bin/tests/ — found by core-life on install (bus #1390). A probe
    written on one seat to run on another cannot locate its target by counting directories.
    """
    for cand in [start] + list(start.parents):
        if (cand / ".claude" / "hooks" / "casebook_matchers.py").is_file():
            return cand
    raise SystemExit("SKIP - could not locate a Core root containing .claude/hooks/casebook_matchers.py")


ROOT = _find_root(Path(__file__).resolve().parent)
MATCHERS = ROOT / ".claude" / "hooks" / "casebook_matchers.py"

# (label, planted text, must_fire)
S3_CASES = [
    ("POSITIVE  bare action, no stamp",       "- write the summary to memory",                      True),
    ("POSITIVE  scheduled, not confirmed",    "- schedule the follow-up for Thursday",              True),
    ("NEAR-MISS action WITH a stamp",         "- write the summary to memory (done)",               False),
    ("NEAR-MISS past tense reads as complete", "- sent the package to Jordan",                       False),
    ("NEAR-MISS not a list line",             "I will write the summary to memory",                 False),
    ("NEAR-MISS declared subfield",           "- **why: we need to write it down",                  False),
]

S4_CASES = [
    ("POSITIVE  bare percentage",             "- coverage is 85% now",                              True),
    ("POSITIVE  decimal percentage",          "- the gate fires on 12.5% of turns",                 True),
    ("NEAR-MISS percentage + script",         "- coverage is 85% measured by bin/x.py",             False),
    ("NEAR-MISS percentage + date",           "- coverage is 85% as of 2026-08-13",                 False),
    ("NEAR-MISS no percentage at all",        "- coverage is good and rising",                      False),
]


def load():
    m = types.ModuleType("casebook_matchers_probe")
    m.__file__ = str(MATCHERS)
    exec(compile(MATCHERS.read_text(), str(MATCHERS), "exec"), m.__dict__)
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

    print("=== T012 — planted-defect regime for casebook STATIC matchers S3/S4 ===\n")
    if not MATCHERS.is_file():
        print("  SKIP - casebook_matchers.py absent")
        return 0
    m = load()

    check("CONTROL - both matchers are importable from the trust path",
          callable(getattr(m, "s3_violations", None)) and callable(getattr(m, "s4_violations", None)),
          "a regime run against absent matchers passes vacuously")
    check("CONTROL - empty text yields no violations",
          not m.s3_violations("") and not m.s4_violations(""),
          "the matchers fire on nothing at all, so every NEAR-MISS below passes for free")
    print()

    for name, fn, cases in (("S3", m.s3_violations, S3_CASES), ("S4", m.s4_violations, S4_CASES)):
        for label, text, must_fire in cases:
            hit = bool(fn(text))
            check("%s %-42s %s" % (name, label, "FIRES" if must_fire else "stays clean"),
                  hit == must_fire,
                  "planted %r -> %r, expected %s. %s" % (
                      text, fn(text), "a hit" if must_fire else "no hit",
                      "A silent matcher reports zero violations exactly like a clean corpus."
                      if must_fire else
                      "A matcher that fires on legitimate prose inflates the casebook score."))
        # The POSITIVE, verbatim, inside a fence. Fenced examples are how these rules get
        # DOCUMENTED, so a matcher that scores its own documentation would flag every file that
        # explains it.
        pos = next(t for _, t, fires in cases if fires)
        check("%s FENCE   - the positive inside a code fence stays clean" % name,
              not fn("```\n%s\n```" % pos),
              "documentation of the rule scores as a violation of it")
        print()

    # THE SECOND KEY, both directions.
    ex3 = "- write the summary <!-- casebook-exempt: S3 -->"
    check("SECOND KEY - a marker WITHOUT honour_markers is still flagged",
          bool(m.s3_violations(ex3)),
          "the default honours exemptions, so any document can exempt itself and a caller who "
          "forgets the flag is LAXER rather than stricter — the inverse of the stated contract")
    check("SECOND KEY - the same marker WITH honour_markers=True is exempt",
          not m.s3_violations(ex3, True),
          "the opt-in does nothing, so declared exemptions cannot be honoured at all")

    # CHARACTERIZATION — see the docstring. These record what the code does, not what it should do.
    check("LINE-SCOPED (characterization) - an unrelated completion word suppresses S3",
          not m.s3_violations("- write the summary (the file was created earlier)"),
          "S3 is now scoped to the action rather than the line — GOOD. Update this assertion and "
          "the docstring section that explains it; it was written to be turned red by this fix.")
    check("LINE-SCOPED (characterization) - an unrelated instrument word suppresses S4",
          not m.s4_violations("- coverage is 85% and nothing was measured"),
          "S4 is now scoped to the figure rather than the line — GOOD. Same as above.")

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
