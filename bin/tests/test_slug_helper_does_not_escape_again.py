#!/usr/bin/env python3
"""The project-slug helper may not gain a SIXTH independent copy.

WHY THIS EXISTS (2026-08-12). `core_seat.transcripts_dir` is the canonical way to turn a Core root
into its Claude Code session directory. It has been consolidated once and ESCAPED TWICE since:
business found a fourth copy (recorded at gate_tier_b.py:298 — "the fix consolidated three and left
a fourth"), and core-finance then found a fifth in `bin/correction-rate-clean.py`, which this commit
routes through core_seat.

THE DIVERGENCE IS REAL BUT LATENT. Canonical replaces EVERY non-alphanumeric:

    re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve()))

The copies replace only the two characters their author happened to think of — `/` and ` `. On a
path with no other punctuation the two agree, which is why nothing is broken on life today and why
five copies survived review. Measured:

    /Users/x/AI Projects/core-life     canonical == copy
    /Users/x/AI Projects/core-v1.5     -core-v1-5   vs  -core-v1.5      DIVERGES
    /Users/x/AI Projects/core_test (2) -core-test--2- vs -core_test-(2) DIVERGES

WHY A RATCHET AND NOT A BAN. A hard "no file outside core_seat may compute this" fails on 18
existing files the moment it ships, and a permanently-red test is one people learn to route around —
the same alarm-fatigue argument that decided the INERT-vs-blocking question for lint-org-scoping
earlier today. So this pins the CURRENT count and fails only when it GROWS. The number is meant to
go down; it may never go up without someone editing this file and saying why.

THE HONEST STATE OF THE 18. Three are hooks retired on 2026-08-06 (say-do-gap, state-claim-gate,
time-claim-gate) — dead weight in the count, not live defects. The remaining fifteen are live, and
each is a latent divergence rather than a present bug. Fixing them is a real sweep and is NOT
smuggled into a test file.

Deliberately COMMENT-STRIPPED before matching: a docstring mentioning core_seat is not an import,
and prose naming `.replace("/", "-")` is not a call. That distinction cost seven separate false
assertions on 2026-08-12 before it became a rule in this suite.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The count as measured on 2026-08-12, after consolidating the 7 non-hook copies through core_seat.
# Was 18; the sweep took it to 11. LOWER THIS when you consolidate another. Raising it requires
# explaining, here, why another independent copy of a helper that has already escaped twice is the
# right call.
#
# THE REMAINING 11 ARE ALL .claude/hooks/, and that is deliberate rather than lazy: 3 of them are
# hooks retired 2026-08-06 (dead weight in the count, not live defects), and the other 8 are live
# PreToolUse/Stop hooks where a bad edit breaks the gate itself. They are a separate, individually
# dosed pass — not something to fold into a sweep that was going well.
BASELINE = 11

SEARCH_ROOTS = ("bin", "scheduling", ".claude/hooks")

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _code(path: Path) -> str:
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        return ""
    return "\n".join(re.sub(r"(?<!['\"])#.*$", "", ln) for ln in text.splitlines())


def offenders() -> list[str]:
    out = []
    seen = set()
    for root in SEARCH_ROOTS:
        for p in sorted((REPO / root).rglob("*.py")):
            if p in seen:
                continue
            seen.add(p)
            # core_seat itself IS the canonical implementation; archived code is retired by
            # definition; this suite's own fixtures quote the idiom on purpose.
            if p.name == "core_seat.py" or "archive" in p.parts or "tests" in p.parts:
                continue
            code = _code(p)
            if not re.search(r'replace\(\s*["\']/["\']\s*,\s*["\']-["\']\s*\)', code):
                continue
            # Only a PROJECTS-directory slug counts. `.replace("/", "-")` has other legitimate uses
            # — labels, filenames — and counting those would inflate the number and make the ratchet
            # meaningless. Measured: 2 such files exist and are correctly not offenders.
            if not re.search(r"\.claude[\"']?\s*/?\s*[\"']?projects|\.claude/projects", code):
                continue
            if re.search(r"\b(import\s+core_seat|from\s+core_seat\s+import)\b", code):
                continue
            out.append(str(p.relative_to(REPO)))
    return out


def main() -> int:
    print("test_slug_helper_does_not_escape_again")

    seat = REPO / "bin" / "core_seat.py"
    check("core_seat still provides the canonical resolver", seat.is_file()
          and "def transcripts_dir" in seat.read_text(),
          "the thing everything is supposed to consolidate ONTO has moved or gone; this ratchet "
          "would then be counting copies of nothing")

    # The canonical rule must still be the strict one. If core_seat were ever loosened to the
    # two-character form, the 18 would silently become correct and this file would be guarding a
    # distinction that no longer exists.
    #
    # SCOPED TO transcripts_dir's BODY, COMMENTS STRIPPED. The first version searched the whole
    # file, and core_seat.py:31 carries a comment quoting a DIFFERENT file's old rule — so the
    # assertion matched documentation and the mutation control passed while the real implementation
    # at :145 was untouched. I found that only because I checked whether my own dose had actually
    # changed anything. Eighth instance of comment-vs-code in one day, and the second inside a test
    # written to catch the defect it committed.
    seat_code = _code(seat)
    fn = re.search(r"\ndef transcripts_dir\(.*?\n(.*?)(?=\ndef )", seat_code, re.S)
    check("the canonical slug still normalises EVERY non-alphanumeric",
          fn is not None
          and re.search(r'sub\(\s*r?["\']\[\^A-Za-z0-9\]["\']', fn.group(1)) is not None,
          "core_seat.transcripts_dir no longer uses the strict rule. If that was deliberate the "
          "copies may now agree with it, and this test needs re-deriving rather than silencing.")

    found = offenders()
    # WHY THIS MIGHT BE RED ON A SEAT THAT ADDED NOTHING (core-finance's caution).
    #
    # BASELINE is a count, and a count is a per-seat fact — three separate censuses differed by seat
    # tonight (35 hooks vs 31, 30 logging invoke vs 20, 11 without invoke vs 3). Checked before
    # accepting the concern: all the files this counts live in bin/, scheduling/ and .claude/hooks/,
    # which are ALL baseline-shared, so post-pull the trees converge and the number is stable
    # fleet-wide. Nothing in per_core_keep contributes.
    #
    # The residual is a transient one: between life pushing a consolidation and a puller's next
    # SessionStart, that seat still holds the un-consolidated copy and counts one MORE than the
    # baseline. So the failure names both readings, because "you have not pulled" and "someone added
    # a copy" call for opposite responses — the same distinction the INERT-vs-clean call turned on.
    check(f"no NEW independent slug computation ({len(found)} found, baseline {BASELINE})",
          len(found) <= BASELINE,
          "TWO READINGS, and they call for opposite responses:\n"
          "          (a) THIS SEAT HAS NOT PULLED. life consolidates these and pushes to baseline;\n"
          "              until a puller syncs it still holds the old copy. Run `/sync pull` first —\n"
          "              if the count drops, that was it and nothing is wrong here.\n"
          "          (b) a genuinely NEW copy. This helper was consolidated once and has escaped\n"
          "              twice already; route it through core_seat.transcripts_dir(root).\n"
          "          Counted here:\n"
          "          " + "\n          ".join(sorted(set(found))))

    if len(found) < BASELINE:
        print(f"  NOTE  {BASELINE - len(found)} fewer than baseline — lower BASELINE to "
              f"{len(found)} so the ratchet keeps its grip.")

    # A ratchet whose census silently matched nothing would pass forever. Prove it can still see.
    check("the census is not vacuous — it still finds the known copies",
          len(found) > 0,
          "zero offenders would mean either total consolidation (lower BASELINE to 0 and say so) "
          "or a broken matcher; the second is far likelier and passes silently")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
