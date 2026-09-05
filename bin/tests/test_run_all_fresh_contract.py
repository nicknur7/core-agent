#!/usr/bin/env python3
"""`run-all.sh --fresh` softens ONLY SKIP and ABSTAIN. FAIL, CRASH, MUTE and LEAK stay red.

WHY (codex, bus #5948, 2026-09-04). --fresh was added as the stranger's acceptance contract and
shipped with no test of its own, so its one measured "green" was an environment fact, not a
property. This plants the same fixtures test_run_all_classifies.py uses and asserts the verdict
under --fresh, dose by dose:
  - only-skip / only-abstain / both      -> exit 0, "GREEN (fresh-clone contract)"
  - skip + one FAIL                      -> exit 1, NOT green
  - skip + one CRASH                     -> exit 1
  - skip + one MUTE (ran, checked nothing) -> exit 1
  - the strict default with the same abstain-only dose -> exit 1 (unchanged semantics)
  - the SKIP/ABSTAIN counters are still REPORTED under --fresh (never zeroed), so the runtime
    ratchet's "full clean run" predicate cannot be satisfied by a partial run.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "bin" / "tests" / "run-all.sh"
p = f = 0

FIX = {
    "ok":      "print('  PASS  something real')\n",
    "skip":    "print('  SKIP — no database on this seat')\n",
    "abstain": "print('  UNDECIDABLE  no fixture on this seat')\nimport sys; sys.exit(2)\n",
    "fail":    "print('  PASS  a')\nimport sys; sys.exit(1)\n",
    "crash":   "x = 1 / 0\n",
    "mute":    "print('starting up')\n",
    # prints a verdict and contradicts it with the exit code — the printed FAIL must win
    "liar":    "print('  FAIL  the thing under test is wrong')\nprint('  SKIP — and then pretends to skip')\n",
}


def check(label, cond, detail=""):
    global p, f
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
    p, f = (p + 1, f) if cond else (p, f + 1)


FIX["leak"] = ("import os, pathlib\n"
               "pathlib.Path(os.environ['CORE_INSTANCE'], '.claude', 'state', 'planted-by-fixture').write_text('x')\n"
               "print('  PASS  wrote into the live seat (a LEAK)')\n")


def run(names, fresh, quiet=False):
    """Plant the dose in a SYNTHETIC seat (its own .claude/state, CORE_INSTANCE pointed at it) so a
    LEAK dose can mutate a monitored path without touching this repo, and so we can see whether the
    runner wrote a runtime floor. Returns (rc, stdout, ratchet_written)."""
    with tempfile.TemporaryDirectory() as td:
        seat = Path(td) / "seat"
        (seat / ".claude" / "state").mkdir(parents=True)
        d = seat / "tests"; d.mkdir()
        for i, n in enumerate(names):
            (d / f"test_{n}_{i}.py").write_text(FIX[n])
        env = dict(os.environ, CORE_INSTANCE=str(seat), CLAUDE_PROJECT_DIR=str(seat))
        args = ["bash", str(RUNNER)] + (["--fresh"] if fresh else []) + (["--quiet"] if quiet else []) + [str(d)]
        r = subprocess.run(args, capture_output=True, text=True, timeout=300, env=env, stdin=subprocess.DEVNULL, cwd=str(seat))
        return r.returncode, r.stdout, (d / ".runtime-baseline").exists()


def main() -> int:
    print("=== run-all.sh --fresh contract ===\n")
    for label, dose in (("only SKIP", ["ok", "skip"]), ("only ABSTAIN", ["ok", "abstain"]), ("SKIP + ABSTAIN", ["ok", "skip", "abstain"])):
        rc, out, ratchet = run(dose, fresh=True)
        check(f"--fresh: {label} -> exit 0 and named GREEN", rc == 0 and "GREEN (fresh-clone contract)" in out, out[-400:])
        check(f"--fresh: {label} -> the soft counters are still reported, not zeroed",
              ("SKIP=1" in out if "skip" in dose else True) and ("ABSTAIN=1" in out if "abstain" in dose else True), out[-400:])
        check(f"--fresh: {label} -> the runtime ratchet was NOT written (partial run is not a full clean run)", not ratchet, "found .runtime-baseline")
    for label, dose in (("one FAIL", ["ok", "skip", "fail"]), ("one CRASH", ["ok", "skip", "crash"]), ("one MUTE", ["ok", "skip", "mute"]), ("one LEAK", ["ok", "skip", "leak"])):
        rc, out, _ = run(dose, fresh=True)
        check(f"--fresh: SKIP + {label} -> still exit 1, not green", rc == 1 and "GREEN (fresh-clone contract)" not in out, out[-400:])
    rc, out, _ = run(["ok", "abstain"], fresh=False)
    check("strict default: an ABSTAIN alone is still exit 1 (semantics unchanged)", rc == 1, out[-300:])
    # Codex review of the P0 repair (2026-09-04): two ways the fresh contract could go GREEN falsely.
    for fresh in (True, False):
        rc, out, _ = run(["ok", "liar"], fresh=fresh)
        check(f"{'--fresh' if fresh else 'strict'}: a file that prints FAIL and exits 0 is a FAIL, not a SKIP",
              rc == 1 and "FAIL=1" in out and "SKIP=0" in out and "GREEN" not in out, out[-400:])
    rc, out, _ = run(["ok", "skip", "abstain"], fresh=True, quiet=True)
    check("--fresh --quiet (what `make test-fresh` runs): GREEN, and the SKIP/ABSTAIN files are NAMED, not just counted",
          rc == 0 and "GREEN (fresh-clone contract)" in out
          and re.search(r"^SKIP +\S*test_skip_1\.py", out, re.M) and re.search(r"^ABSTAIN +\S*test_abstain_2\.py", out, re.M),
          out[-600:])
    rc, out, ratchet = run(["ok"], fresh=True)
    check("--fresh with nothing soft prints the ordinary ALL GREEN and MAY record the ratchet (it was a full clean run)", rc == 0 and "ALL GREEN" in out and ratchet, out[-300:])
    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
