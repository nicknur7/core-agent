#!/usr/bin/env python3
"""A TEST MUST NOT WRITE INTO THE SEAT IT IS RUN FROM.

core-business, bus #1039/#1040. The claude-si tests isolate by overriding CLAUDE_PROJECT_DIR. The
modules under test resolve their root from CORE_INSTANCE, falling back to their OWN repo root when it
is unset. So the isolation covered the variable the TEST controls and missed the one the CODE
follows, and every run wrote its fixtures into the live seat:

    core-life     .claude/state/friction-artifacts/active.json    1 artifact,  org 2   <- business's
    core-business .claude/state/friction-artifacts/active.json    3 artifacts, org 1   <- life's

Each seat carrying the other's test fixtures, and on life the fixtures were stamped org 1 — the real
org here — so nothing downstream filtered them out. Dosed by running the pre-fix tests once more:
they replaced 22 live artifacts with 3 fixtures, immediately, with no error and no warning.

NOTHING WAS LOST, AND THAT IS LUCK RATHER THAN DESIGN. active.json is a projection of Postgres, so
`si_project.project(org)` rebuilt all 22. The next primitive that leaks this way may not have an
authoritative store behind it.

TWO CHECKS, AND THE STATIC ONE IS THE DURABLE GUARD:

  STATIC   every test that redirects CLAUDE_PROJECT_DIR must redirect CORE_INSTANCE to the SAME
           place. This is what catches a NEW test file written the old way, which is how the class
           comes back. Costs nothing and risks nothing.
  DOSE     actually run one and prove the live seat is byte-identical afterwards. Without it the
           static rule is an assertion about a convention rather than about behaviour.

Run: python3 bin/tests/test_tests_do_not_write_live_state.py
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SI_TESTS = ROOT / "scheduling" / "claude-si" / "tests"
LIVE_STATE = ROOT / ".claude" / "state"


def all_test_files():
    """Every test file in the seat, however many directories they live in."""
    out = []
    for p in ROOT.rglob("test_*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if any(rel.startswith(x) or "/%s/" % x in rel
               for x in (".venv", "node_modules", ".git", "archive")):
            continue
        out.append(p)
    return out


def ambient_pattern():
    """Regex compiled from bin/tests/ambient-state.txt, mirroring run-all.sh's _STATE_NOISE build.

    THE SAME LIST run-all.sh's LEAK digest uses, read from bin/tests/ambient-state.txt rather than
    restated. Originally this returned a literal set() and matched with `name in skip` — exact
    equality. That was fine while every entry was a fixed basename (hook-events.log, ...), but it
    silently assumed the shell side matched the same way. It does not: run-all.sh already treats
    every line as an END-ANCHORED REGEX (escaping only literal dots, then OR-ing the lines) — the
    file's own header says "matched at the END of the path", which is regex language, not set
    language. The two consumers agreed by coincidence, not by construction.

    That coincidence broke the day a THIRD entry needed a wildcard: delegation-gate.py's PreToolUse
    counter is named `.delegation-run-<session>`, a different suffix every session, so no finite
    set of literal basenames can list it. The shell side could already express that as a plain
    regex line; this side could not until it also treated the file as regex. Rebuilt to run the
    EXACT SAME transform (escape literal dots, OR the lines, anchor with $) so both consumers read
    the one file identically — the drift this file exists to prevent (measured 2026-08-13: one run
    `ok=128 FAIL=0 LEAK=1`, the next `ok=127 FAIL=1 LEAK=1` from the same ambient writes, because the
    two implementations disagreed about the same directory in the same run).

    FAILS SAFE: unreadable or empty file -> None -> nothing excluded -> over-reports.
    """
    f = Path(__file__).resolve().parent / "ambient-state.txt"
    try:
        lines = [ln.strip() for ln in f.read_text().splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return None
    if not lines:
        return None
    escaped = [ln.replace(".", r"\.") for ln in lines]
    return re.compile("(" + "|".join(escaped) + ")$")


def state_paths() -> dict:
    """path -> content hash, for every non-ambient file under .claude/state."""
    pat = ambient_pattern()
    out = {}
    for p in sorted(LIVE_STATE.rglob("*")):
        if not p.is_file() or (pat is not None and pat.search(p.name)):
            continue
        rel = str(p.relative_to(LIVE_STATE))
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out[rel] = "<unreadable>"
    return out


def state_digest() -> str:
    """One digest over every file under .claude/state — content AND the set of paths."""
    h = hashlib.sha256()
    for rel, dig in state_paths().items():
        h.update(rel.encode())
        h.update(dig.encode())
    return h.hexdigest()


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== tests must not write into the live seat ===\n")

    if not SI_TESTS.is_dir():
        print("  SKIP — %s absent on this Core" % SI_TESTS)
        return 0

    print("--- STATIC: redirecting CLAUDE_PROJECT_DIR obliges redirecting CORE_INSTANCE ---")
    # SWEEP THE WHOLE SEAT, NOT THE DIRECTORY WHERE THIS WAS FIRST SEEN. The first version globbed
    # scheduling/claude-si/tests only -- and bin/tests/test_tune_path_e2e.py had the identical defect,
    # in a SHARED dir, so it shipped to every Core and wrote art_tune_e2e_probe into the live state of
    # school, finance and ops (core-business, bus #1041). A guard scoped to the one place the bug was
    # found is the same defect as the bug: correct where written, absent everywhere else it applies.
    # ADVISORY, NOT A VERDICT — and that demotion is itself a finding.
    #
    # This started as a per-file FAIL: any test mentioning CLAUDE_PROJECT_DIR had to contain a literal
    # `os.environ["CORE_INSTANCE"] = ...`. It accused TEN working files, including THIS ONE, which
    # sets the variable as `dict(os.environ, CORE_INSTANCE=td)` — a form the pattern could not see.
    # Seventh matcher today that could not match what it was written for, in the file written to
    # catch a class of exactly that kind.
    #
    # A sweep's N is a candidate list, not a finding list. Spelling is not checkable; BEHAVIOUR is,
    # and the behavioural check now lives in run-all.sh, which digests .claude/state around every run
    # at zero cost and covers every directory including ones not yet written. What remains here is a
    # pointer for a human reading a leak report, printed and never scored.
    candidates = [t.relative_to(ROOT).as_posix() for t in sorted(all_test_files())
                  if "CLAUDE_PROJECT_DIR" in t.read_text(errors="ignore")
                  and "CORE_INSTANCE" not in t.read_text(errors="ignore")]
    print("  note: %d test file(s) redirect CLAUDE_PROJECT_DIR without naming CORE_INSTANCE." %
          len(candidates))
    print("        Not a verdict — several are ABOUT seat resolution and set it by other means.")
    for c in candidates[:8]:
        print("          %s" % c)
    check("the sweep examined a plausible number of files", len(all_test_files()) >= 10,
          "only %d test files found — a narrowed glob would make this file's own advisory vacuous"
          % len(all_test_files()))

    print("\n--- DOSE: run them and prove the live seat is byte-identical afterwards ---")
    before_paths = state_paths()
    before = state_digest()
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, CORE_INSTANCE=td, CLAUDE_PROJECT_DIR=td)
        ran = 0
        for t in sorted(SI_TESTS.glob("test_*.py")):
            # sys.executable, NOT bare `python3`. A bare name resolves through PATH, so this
            # harness could run its children under a DIFFERENT interpreter than the one running the
            # harness — and on a seat where that one lacks psycopg2 the SI fixtures abstain, leaving
            # the shared temp seat in a different state for whichever fixture runs next. The
            # resulting failure looks like a defect in the LAST fixture rather than in the harness.
            r = subprocess.run([sys.executable, str(t)], capture_output=True, text=True,
                               timeout=300, env=env)
            ran += 1
            # EXIT 2 IS ABSTAIN, NOT DIRTY. run-all.sh grades `exit 2 + UNDECIDABLE` as "declined
            # to certify here; no fixture, not a defect", and a fixture that declines has by
            # definition written nothing. Three SI fixtures started abstaining on 2026-08-26 when
            # they learned to report a missing Postgres corpus instead of dying on a
            # FileNotFoundError — an honesty improvement that this check read as a regression.
            # What this test is FOR is live-state writes; the digest comparison below is what
            # actually enforces that, and it is unaffected by the exit code.
            _clean = r.returncode == 0 or (r.returncode == 2 and "UNDECIDABLE" in (r.stdout or ""))
            check("%s exits clean under a redirected seat" % t.name, _clean,
                  (r.stdout + r.stderr)[-400:])
        check("the dose ran every file", ran == len(list(SI_TESTS.glob("test_*.py"))))

    after_paths = state_paths()
    after = state_digest()
    # NAME WHAT MOVED. This reported only that two hashes differed, which tells the reader a write
    # happened somewhere in a directory of 779 files — the diagnosis equivalent of "something
    # happened". run-all.sh's LEAK block already prints its changed paths and this did not, so the
    # same event was diagnosable from one instrument and opaque from the other.
    #
    # It also matters for ATTRIBUTION. This digest cannot distinguish a test's write from a
    # concurrent write by the live session (bus monitor, session hooks, a push touching
    # .sentinel-last-blocked) — the same confound run-all.sh states in its own LEAK note and answers
    # with "re-run once". Printing the paths is what lets a reader make that call instead of
    # guessing; NOT excusing them is deliberate, because a detector that decides for itself which
    # writes were ambient is one bad guess away from excusing a real leak.
    moved = sorted(set(before_paths) ^ set(after_paths)) + \
        sorted(k for k in set(before_paths) & set(after_paths) if before_paths[k] != after_paths[k])
    check("the live seat's .claude/state is UNCHANGED by the run", before == after,
          "a test wrote into the seat it was run from — OR the live session did, concurrently.\n"
          "          changed (%d): %s\n"
          "          Re-run with no session activity before treating this as a test leak.\n"
          "          If active.json moved, rebuild it with\n"
          "          python3 -c \"import si_project; si_project.project(1)\" from scheduling/claude-si"
          % (len(moved), ", ".join(moved[:6]) or "(none — content changed under an ambient name)"))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
