#!/usr/bin/env python3
"""THE RUNNER MUST REFUSE A SEAT IT CANNOT HONESTLY SCORE — and say which of the three reasons it is.

core-business measured the fleet from file presence (bus #997) and the result is why this file
exists. Verified independently here by the same method, no transcript reads:

    seat       items  runner  predicates  gate
    ops       NO     NO      NO          NO
    business   yes    yes     NO          NO
    finance    NO     NO      NO          NO
    life       yes    yes     yes         yes
    school     NO     yes     NO          NO      <- a runner with nothing to score, TODAY

Four of five Cores can produce a number that describes a SUBSET while looking like the whole thing.
That is the empty-suite shape at fleet scale, and school is sitting in it right now: it has this
runner and no item set.

THREE WAYS TO BE UNSCOREABLE, and all three must fail loud rather than score partially:

    no item set          school's state. Must FATAL, never emit a scalar over zero items.
    no casebook_matchers business's state. S3/S4 cannot be scored — the matcher is loaded FROM THE
                         SEAT on purpose, so the item measures the rules that seat actually
                         enforces; substituting the runner's copy would score a Core against rules
                         it does not run.
    stale si-objective   business's state. T6/T7/T8 need `tally_distinct` (added 2026-08-10). The
                         seat's copy is the right one to load, because si-objective resolves its own
                         REPO from `Path(__file__).parents[1]` and reads THAT tree's state — loading
                         the runner's copy would score life's data against a peer's items, a silent
                         blend where this is a loud refusal.

The last two produced bare `ModuleNotFoundError` / `AttributeError` until 2026-08-10, and
core-business read them as "life's predicates ERROR where mine succeed" — filing four items as LOST
in a borrow comparison. The refusals were right; the messages accused the wrong thing. Both now name
the seat's missing dependency and say the fix is to sync, not to weaken the item.

Run: python3 bin/tests/test_casebook_refuses_unscoreable.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
RUNNER = ROOT / "bin" / "casebook-run.py"


def run_on(seat):
    r = subprocess.run([sys.executable, str(RUNNER), "--json"],
                       env=dict(os.environ, CORE_INSTANCE=str(seat)),
                       capture_output=True, text=True, timeout=900)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== a seat that cannot be scored must not produce a number ===\n")

    with tempfile.TemporaryDirectory() as td:
        seat = Path(td)
        (seat / ".claude" / "state").mkdir(parents=True)
        (seat / ".claude" / "identity.json").write_text('{"core":"probe"}')
        rc, out, err = run_on(seat)
        check("a seat with NO item set exits nonzero", rc != 0, "rc=%d" % rc)
        check("...and says it cannot read the item set", "casebook-v1.json" in (out + err),
              (out + err)[:200])
        check("...and emits NO scalar at all", '"scalar"' not in out,
              "a score was produced for a seat with zero items: %s" % out[:200])

    print("\n--- THE DOSE: a scoreable seat must still score, or 'refuses' means nothing ---")
    # Without this, every check above is satisfied by a runner that refuses everything.
    #
    # THE DOSE NEEDS A SCOREABLE SEAT, AND NOT EVERY SEAT IS ONE (2026-08-12, reported by
    # core-finance, who ran this suite from a Core that had not yet synced `eval/`).
    #
    # This block asks whether THIS seat scores. On a seat with no item set the answer is "no", and
    # reporting that as a FAIL says the RUNNER is broken when the true statement is that the SEAT
    # has no data. `eval/**` is per_core_keep (2026-09-02), not shared.dirs — the item set is
    # per-Core, never distributed by the baseline sync — so absence is the NORMAL case for any seat
    # that has not authored its own casebook, not a sync failure. A shared test may not assume any
    # seat has one, and a red suite whose redness means "you have no data" is one people learn to
    # ignore.
    #
    # Skipped, not passed. The checks above are all refusals, and a file made only of refusals is
    # satisfied by a runner that refuses everything — which is the exact hazard the comment above
    # names. Losing the control silently would leave that hazard uncovered while still printing
    # green, so the skip is loud and says what it could not test.
    # SCOPED TO THE eval-DEPENDENT BLOCK ONLY. My first version of this guard used an early
    # `return`, which also skipped the "dependency refusals must NAME the seat's missing piece"
    # block below — three checks that read RUNNER as SOURCE TEXT and do not touch
    # eval/casebook-v1.json at all. They run fine on a stale seat. Caught by sentinel-code on the
    # baseline-push review, and it is the same defect in miniature as the one this commit fixes:
    # I reached for the broadest exit rather than the narrowest, and silently dropped coverage that
    # had nothing to do with the absent file.
    if not (ROOT / "eval" / "casebook-v1.json").is_file():
        # `note:` NOT `SKIP` — FOUR checks ran above this line, and run-all reads a leading SKIP
        # as a file-level verdict, hiding all four. Measured on core-finance: 6 passed, shown as
        # "did not execute".
        print("  note: this seat has no eval/casebook-v1.json, so 'a scoreable seat still scores'\n"
              "        cannot be tested here. The refusal checks above still ran, but on their own\n"
              "        they are also satisfied by a runner that refuses EVERYTHING. Author a local\n"
              "        item set (eval/ is per-Core data, not synced) and re-run to restore the"
              "\n        control.")
    else:
        rc2, out2, err2 = run_on(ROOT)
        check("this Core's own seat scores", rc2 == 0 or '"scalar"' in out2,
              "rc=%d %s" % (rc2, (err2 or out2)[:200]))
        try:
            d = json.loads(out2)
            check("...over the full item set, not a subset", len(d.get("results", [])) >= 13,
                  "scored %d items" % len(d.get("results", [])))
        except Exception as e:
            check("...and returns parseable JSON", False, str(e)[:120])

    print("\n--- the two dependency refusals must NAME the seat's missing piece ---")
    # Checked as source, not by constructing a stale peer: building a fake seat with an older
    # si-objective would be planting a fixture I cannot verify landed correctly, which is the
    # failure core-business hit twice today. The strings are the contract; their behaviour was
    # dosed live against core-business's real seat when they were written.
    src = RUNNER.read_text()
    check("the matcher refusal names the seat, not the predicate",
          "CANNOT SCORE S3/S4 ON THIS SEAT" in src and "MISSING DEPENDENCY on the seat" in src)
    check("the tally_distinct refusal names version skew",
          "VERSION SKEW, not a broken predicate" in src)
    check("...and both say to sync rather than weaken the item",
          src.count("Fix by syncing the seat") >= 2,
          "only %d of 2 refusals point at the sync" % src.count("Fix by syncing the seat"))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
