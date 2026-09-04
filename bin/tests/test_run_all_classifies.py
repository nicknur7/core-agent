#!/usr/bin/env python3
"""THE TEST RUNNER MUST TELL FIVE OUTCOMES APART, and four of them are ways of not passing.

`bin/tests/run-all.sh` replaced an ad-hoc loop I retyped six times in one session. That loop judged
purely on exit code and discarded output, which conflates three distinct failures with success:

    a test that CRASHED before asserting        core-business's suite.py mapped rc==1 to "findings"
                                                 unconditionally, so an all-crashed casebook printed
                                                 "FINDINGS 33 — all controls green" (bus #975)
    a test that ASSERTED NOTHING and exited 0   the empty-suite shape; prints its banner, checks
                                                 nothing, indistinguishable from a real pass
    a test that SKIPPED                          test_trajectory_gate.sh refuses to run on a dirty
                                                 worktree — correct — and during active work the
                                                 tree is ALWAYS dirty. The old loop counted that as
                                                 green, so "4 shell suites green" was quoted all
                                                 session while one of the four never executed.

Each is planted here as a REAL fixture file and run through the REAL runner, because the property is
about process exit codes and captured output — the two things an in-process assertion cannot
exercise honestly.

THE VOCABULARY CASES ARE NOT DECORATION. The runner's evidence matcher was wrong on its first run
and flagged four working files as MUTE: it required `ok` at the start of a line and knew only three
success phrases, so `ALL PASS`, `SURVIVED — …`, `… MATCH` and a trailing `flagged 0/0 ok` all read as
"printed no evidence". That was the sixth matcher in one day that could not match what it was written
for — inside the runner built to catch that class. These four are compiled in as must-fire controls
so the next edit cannot quietly narrow it again.

Run: python3 bin/tests/test_run_all_classifies.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
RUNNER = ROOT / "bin" / "tests" / "run-all.sh"

# name -> (body, expected verdict)
FIXTURES = {
    "test_crash":   ("x = 1 / 0\n", "CRASH"),
    "test_fail":    ("print('  PASS  a')\nimport sys; sys.exit(1)\n", "FAIL"),
    "test_mute":    ("print('starting up')\n", "MUTE"),
    "test_skip":    ("print('  SKIP — worktree dirty')\n", "SKIP"),
    "test_ok":      ("print('  PASS  something real')\n", "ok"),
    # the four vocabularies that were wrongly flagged MUTE on the runner's first run
    "test_vocab_allpass":  ("print('ALL PASS')\n", "ok"),
    "test_vocab_survived": ("print('  SURVIVED — the script is byte-identical')\n", "ok"),
    "test_vocab_match":    ("print('  trailing comment   4aac19d8   4aac19d8   MATCH')\n", "ok"),
    "test_vocab_trailok":  ("print('     flagged  0/0  ok')\n", "ok"),
    # ── ABSTAIN: "I cannot decide here" is not "this is broken" (2026-08-13) ──────────────────
    # Five tests exit 2 on purpose when a seat has no fixture for them. The runner mapped every
    # nonzero to FAIL, so a deliberate refusal to certify was rendered as a defect — and a peer
    # cannot tell "your shared code is broken" from "this seat has no brain vault" when both say
    # FAIL. Measured on a clean clone of life at /tmp: 10 reds, 2 of them this.
    "test_abstain":     ("print('  UNDECIDABLE  no fixture on this seat')\n"
                         "import sys; sys.exit(2)\n", "ABSTAIN"),
    # THE TWO WAYS THIS COULD LAUNDER A REAL FAILURE, both pinned. Neither is hypothetical:
    # argparse exits 2 on a bad flag, and a test that dies mid-run can easily have printed the
    # word already. If either of these ever reports ABSTAIN, a genuine red has been recoloured as
    # "not applicable" — the fail-toward-PASS direction this suite exists to refuse.
    "test_bare_exit2":  ("print('  PASS  a')\nimport sys; sys.exit(2)\n", "FAIL"),
    "test_crash_exit2": ("import sys\nprint('Traceback (most recent call last):')\n"
                         "print('ValueError: boom')\nprint('UNDECIDABLE')\nsys.exit(2)\n", "CRASH"),
}


def run_env(seat, env_extra):
    """Invoke the runner with NO scan argument, so DISCOVERY is the only thing under test.

    Passing a scan dir would mask the defect: the runner would glob the path it was handed and look
    correct while CORE_INSTANCE was still ignored. The bug was specifically that the env var did not
    redirect the default.
    """
    env = dict(os.environ)
    env.update(env_extra or {})
    r = subprocess.run(["bash", str(RUNNER)], capture_output=True, text=True, timeout=300, env=env)
    return r.stdout + r.stderr, r.returncode


def run(scan_dir, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(["bash", str(RUNNER), str(scan_dir)],
                       capture_output=True, text=True, timeout=300, env=env)
    return r.stdout + r.stderr, r.returncode


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== run-all.sh classification ===\n")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for name, (body, _) in FIXTURES.items():
            (d / (name + ".py")).write_text(body)

        # THE PLANTING IS A MEASUREMENT TOO. core-business, bus #988, found its dose_response.py
        # planting 17 cases and verifying none — in the tool that JUDGES other detectors, where a
        # silently failed write does not lose a result, it produces "the count did not move with
        # dose": a FALSE ACCUSATION against a working detector, from the instrument whose whole job
        # is deciding whether other instruments work.
        #
        # This file has the same structure at smaller scale: it plants 9 fixtures and judges the
        # runner by the counts they produce. A fixture that failed to land would shift a count and
        # read as the RUNNER misclassifying, not as a missing file. The totals below happen to catch
        # it, but only implicitly — so assert the planting directly, before anything is judged.
        planted = sorted(x.name for x in d.glob("test_*.py"))
        check("all %d fixtures actually landed on disk" % len(FIXTURES),
              len(planted) == len(FIXTURES),
              "planted %d of %d: %s" % (len(planted), len(FIXTURES), planted))

        out, rc = run(d)
        check("the runner saw exactly the %d files planted" % len(FIXTURES),
              ("%d test files" % len(FIXTURES)) in out,
              "runner's own file count disagrees with the planting — every verdict below is "
              "measuring a different set than the one built")

        m = re.search(r"ok=(\d+)\s+FAIL=(\d+)\s+CRASH=(\d+)\s+MUTE=(\d+)\s+SKIP=(\d+)", out)
        if not m:
            print("  FAIL  could not parse the summary line\n%s" % out[:800])
            return 1
        got = dict(zip(("ok", "FAIL", "CRASH", "MUTE", "SKIP"), (int(x) for x in m.groups())))
        want = {"ok": 5, "FAIL": 2, "CRASH": 2, "MUTE": 1, "SKIP": 1}
        print("  counts: %s" % got)
        for k in want:
            check("%s counted correctly (%d)" % (k, want[k]), got[k] == want[k],
                  "got %d, wanted %d" % (got[k], want[k]))

        # PARSED SEPARATELY so the assertion above keeps working if the field ever moves. The
        # counter is checked as well as the row, because the two disagreeing is the exact defect
        # T035 recorded in this runner: `ok=100 FAIL=0` printed beside `exit 1`, both true, with
        # no way to see why.
        ma = re.search(r"ABSTAIN=(\d+)", out)
        check("the summary line carries an ABSTAIN counter", ma is not None,
              "a state the runner can report but not count is invisible to every seat that greps "
              "the summary — which is how every seat reads this")
        if ma:
            check("ABSTAIN counted correctly (1)", int(ma.group(1)) == 1,
                  "got %s, wanted 1" % ma.group(1))
        check("an ABSTAIN alone still exits nonzero",
              "ABSTAIN:" in out,
              "the explanatory block must print whenever the counter is non-zero, for the same "
              "reason LEAK's does: a counter the summary carries but never explains sends the "
              "reader looking in the wrong place")

        check("a suite with any non-pass exits nonzero", rc != 0, "rc=%d" % rc)

        print("\n--- each non-pass is named in the report, not just counted ---")
        for name, (_, verdict) in FIXTURES.items():
            if verdict == "ok":
                continue
            check("%s reported as %s" % (name, verdict),
                  re.search(r"%s\s+\S*%s" % (verdict, name), out) is not None,
                  "not found in report")

    print("\n--- the stale-test NOTE fires on a red test, and NOT on a declined one ---")
    # A test that DECLINED to run explains itself; only one that ran and came back red can be red
    # because this seat's copy is newer than this seat's code. The first version gated the note on
    # `[[ -f bin/.last-baseline-sync ]] || [[ -d .git ]]` -- the second disjunct is true in every
    # Core, so the "printed only when relevant" comment described a condition that did not exist and
    # the note fired on a bare SKIP. Advice that appears when it does not apply is how advice
    # becomes furniture.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "test_s.py").write_text("print('  SKIP — declined')\n")
        (d / "test_p.py").write_text("print('  PASS  real')\n")
        out, _ = run(d)
        check("a SKIP-only run does NOT print the stale-test note", "OLD TEST" not in out, out[-400:])
        (d / "test_f.py").write_text("print('  PASS  a')\nimport sys; sys.exit(1)\n")
        out, _ = run(d)
        check("...and a FAIL does", "OLD TEST" in out, out[-400:])

    print("\n--- CORE_INSTANCE must redirect DISCOVERY, not just be accepted ---")
    # core-business ran this runner against its own tree with CORE_INSTANCE set, and it reported
    # FAIL and CRASH rows for LIFE's tests — it cd'd to its own root and globbed there, so the env
    # var was accepted and ignored (bus #1034). It was one message from filing life's unsynced suite
    # as business's failures. The wrong-seat class, in the tool whose entire output is a verdict
    # ABOUT a seat.
    with tempfile.TemporaryDirectory() as td:
        other = Path(td) / "otherseat"
        (other / "bin" / "tests").mkdir(parents=True)
        (other / "bin" / "tests" / "test_only_here.py").write_text("print('  PASS  other seat')\n")
        out, rc = run_env(other, {"CORE_INSTANCE": str(other)})
        check("scans the seat CORE_INSTANCE names, not its own tree",
              "1 test files" in out and "test_only_here" in out, out[:300])
        check("...and PRINTS which seat it measured", "seat: %s" % other in out, out[:300])
        check("...so a run can never be silently about the wrong Core", "otherseat" in out, out[:200])

    print("\n--- IS THE DENOMINATOR REAL: a default run must cover the whole seat ---")
    # core-business, bus #1035: "the canary rule covers DOES IT FIRE. It does not cover IS THE
    # DENOMINATOR REAL." The runner defaulted to the single directory bin/tests, so it printed
    # "ALL GREEN — every file executed" over 70 files while six real tests under
    # scheduling/claude-si/tests were never scanned. A green verdict over a partial denominator is
    # worse than a red one, because nothing about it looks wrong.
    with tempfile.TemporaryDirectory() as td:
        seat = Path(td) / "twodir"
        for sub in ("bin/tests", "scheduling/claude-si/tests"):
            (seat / sub).mkdir(parents=True)
            (seat / sub / "test_x.py").write_text("print('  PASS  in %s')\n" % sub)
        (seat / "tasks" / "archive" / "dead").mkdir(parents=True)
        (seat / "tasks" / "archive" / "dead" / "test_retired.py").write_text("raise SystemExit(1)\n")

        out, rc = run_env(seat, {"CORE_INSTANCE": str(seat)})
        check("BOTH test directories are scanned by a default run",
              "bin/tests" in out and "claude-si/tests" in out, out[:600])
        check("...and the seat total names how many it covered",
              "2 test director" in out, out[-500:])
        check("...an archived test does not fail the run", rc == 0, "rc=%d\n%s" % (rc, out[-500:]))
        check("...but its exclusion is PRINTED, not silent",
              "excluded: 1 file" in out,
              "an excluded file nobody can see is indistinguishable from one that does not exist")

    print("\n--- invoked by a RELATIVE path from another cwd, the children must still run ---")
    # Discovery re-invokes this script once per test directory, and `$0` is whatever the caller
    # typed. Invoked as `bash seat/bin/tests/run-all.sh` from the seat's PARENT, every child died
    # with "No such file or directory" the moment the parent cd'd into the seat -- while the parent
    # still printed a tidy "NOT GREEN - at least one directory reported a non-pass". True, red, and
    # about nothing that ran. Found by an end-to-end dose from /tmp, not by any check in this file.
    with tempfile.TemporaryDirectory() as td:
        seat = Path(td) / "seat"
        (seat / "bin" / "tests").mkdir(parents=True)
        (seat / "bin" / "tests" / "run-all.sh").write_text(RUNNER.read_text())
        (seat / "bin" / "tests" / "test_x.py").write_text("print('  PASS  ran')\n")
        env = dict(os.environ, CORE_INSTANCE=str(seat))
        r = subprocess.run(["bash", "seat/bin/tests/run-all.sh"], cwd=td,
                           capture_output=True, text=True, timeout=300, env=env)
        out = r.stdout + r.stderr
        check("a relative invocation from another cwd still runs its children",
              "ok=1" in out and "No such file" not in out, out[:500])
        check("...and reports green rather than a red verdict about nothing",
              r.returncode == 0 and "ALL GREEN" in out, "rc=%d\n%s" % (r.returncode, out[:400]))

    print("\n--- runtime ratchet: a slowdown must be reported and must NOT become the new floor ---")
    # The branch that reports a regression is the one that has never fired, which is the shape this
    # whole suite exists to refuse. Planting a floor makes it reachable without waiting for a real
    # slowdown. The monotonic rule is core-business's finding (bus #976): a snapshot rewritten every
    # run shows a degradation for exactly one cycle and then normalises it away.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # Takes ~1.2s so the run has measurable duration. A 0s fixture is FASTER than any planted
        # floor, so it tightens instead — which the first version of this check asserted against and
        # was wrong about: the code was right and the fixture could not construct the case.
        (d / "test_a.py").write_text("import time\ntime.sleep(1.2)\nprint('  PASS  one')\n")
        base = d / ".runtime-baseline"

        base.write_text("v2 0\n")                   # pretend the suite once ran instantly
        out, rc = run(d, {"CORE_RUNTIME_MIN_DELTA": "0"})
        check("a slowdown is REPORTED", "SLOWED BY MORE THAN HALF" in out, out[-400:])
        check("...and the floor STAYS PUT (a slowdown never becomes the new normal)",
              base.read_text().split()[-1] == "0",
              "floor moved to %r — the degradation normalised itself" % base.read_text().strip())
        check("...and the output says the floor was not updated", "NOT updated" in out, out[-300:])

        base.write_text("v2 999999\n")              # pretend it once took far longer per file
        out2, rc2 = run(d)
        check("a faster run DOES tighten the floor", base.read_text().split()[-1] != "999999",
              "floor stayed at 999999 despite a faster run — the ratchet never tightens")
        check("...and reports the tightening", "floor tightened" in out2, out2[-300:])

    print("\n--- and the measure is PER FILE, so adding tests is not a regression ---")
    # THE PROPERTY THE FIRST VERSION GOT WRONG. It tracked a monotonic TOTAL, copied from the
    # steering ratchet — but that ratchet exists to RESIST growth, while a test suite is supposed to
    # grow. Against a fixed total, every legitimate new test widens the gap until the alarm fires on
    # good work, and an alarm that fires on good work gets silenced. Pinned here so a future
    # "simplification" back to a total turns red instead of quietly returning to crying wolf.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        body = "import time\ntime.sleep(0.4)\nprint('  PASS  x')\n"
        for i in range(2):
            (d / ("test_%d.py" % i)).write_text(body)
        run(d)                                       # records the floor at 2 files
        # The floor is version-tagged ("v2 <rate>") so pre-millisecond floors self-heal fleet-wide.
        small = (d / ".runtime-baseline").read_text().split()[-1]

        for i in range(2, 6):                        # now 6 files, same per-file cost
            (d / ("test_%d.py" % i)).write_text(body)
        out3, _ = run(d)
        check("tripling the file count does NOT report a slowdown",
              "SLOWED" not in out3,
              "adding tests read as a regression:\n          " + out3[-300:])
        # TOLERANCE WITH A FLOOR. Written as a bare fraction of `small`, this check failed about one
        # run in four -- when the suite ran fast enough that the recorded rate was 0, the tolerance
        # was 0 too and an exact match compared false. The flake was not noise: it was the only
        # thing reporting that the runner measured in whole seconds and could record a rate of 0,
        # which disables the ratchet outright. Fixed in run-all.sh; the tolerance keeps a floor so
        # this checks the per-file property rather than the clock's resolution.
        now = int((d / ".runtime-baseline").read_text().split()[-1])
        check("...and the recorded rate stays in the same range (%s ms/file)" % small,
              abs(now - int(small)) < max(int(small), 200),
              "rate moved from %s to %s — the measure is not per-file" % (small, now))

    print("\n--- THE DOSE: an all-passing suite must go green and exit 0 ---")
    # Without this the checks above are consistent with a runner that fails everything.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "test_a.py").write_text("print('  PASS  one')\n")
        (d / "test_b.py").write_text("print('2 passed, 0 failed')\n")
        out, rc = run(d)
        check("clean suite exits 0", rc == 0, "rc=%d\n%s" % (rc, out[:400]))
        check("...and says so", "ALL GREEN" in out, out[:300])

    print("\n--- an EMPTY suite must refuse, not report health ---")
    with tempfile.TemporaryDirectory() as td:
        out, rc = run(Path(td))
        check("no test files -> REFUSING", "REFUSING" in out and rc == 2, "rc=%d %s" % (rc, out[:200]))
        check("...and does NOT print ALL GREEN", "ALL GREEN" not in out)

    print("\n--- a bad target must refuse rather than silently scan its own tests ---")
    out, rc = run(Path(tempfile.gettempdir()) / "definitely-not-a-real-dir-9271")
    check("nonexistent directory -> REFUSING", "REFUSING" in out and rc == 2,
          "rc=%d %s" % (rc, out[:200]))
    check("...and reports no counts at all", "ok=" not in out, out[:200])

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
