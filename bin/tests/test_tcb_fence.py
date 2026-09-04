"""Both directions on the fence check. A check never seen to FAIL is indistinguishable from dead.

This one was dead twice: defined-but-never-called, AND filtered through a hardcoded three-name
whitelist so it could not report a new breach even if called. The first fix (merely calling it)
passed a syntax check, ran clean, and still missed a planted breach.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _core import core_root as _core_root  # noqa: E402

# DERIVED, and the mutation now happens on a COPY. This test wrote a mutated version over the LIVE
# gating script and restored it in a finally — so a kill between write and restore leaves the Core
# running a permanently broken gate, silently. Shipped to five Cores, all mutating life's file.
ROOT = _core_root()
RUNNER = ROOT / "bin" / "casebook-run.py"
CWD = str(ROOT)


def run(runner=None, cwd=None):
    """Run a specific copy of the runner. Defaults to the live one for the read-only phases."""
    r = subprocess.run(["python3", str(runner or RUNNER)], capture_output=True, text=True,
                       cwd=str(cwd or CWD), timeout=1200)
    return r.returncode, r.stdout + r.stderr


_FAILED = False

print("\n  NEGATIVE — fence consistent, must NOT cry wolf")
code, out = run()
quiet = "FENCE INCONSISTENT" not in out
print("     %s (exit %s)" % ("ok — silent" if quiet else "FAIL — fired on a clean fence", code))
_FAILED = _FAILED or not quiet

print("\n  LIVENESS — remove a genuinely-executed file from TCB, must FIRE")
# NEVER MUTATE THE LIVE RUNNER. The previous version wrote a broken copy over the real
# bin/casebook-run.py and restored it in a `finally`. core-business reproduced it with a SIGKILL
# immediately after the write: THE REAL FILE WAS LEFT MUTATED ON DISK, predicate line gone, no
# signal anywhere. This test runs at every session close on every Core via run-all.sh, so any hard
# interruption — OOM, timeout race, sleep — leaves that Core's TCB fence permanently and silently
# under-fenced. Which is the ORIGINAL BUG this file's own docstring exists to describe.
#
# My previous commit said "the mutation now happens on a COPY". It did not. Second time today a
# comment asserted a property the code lacked, and business found both.
#
# The mutation now happens in a scratch tree. Nothing under the live Core is written at any point.
scratch = pathlib.Path(tempfile.mkdtemp(prefix="tcb-fence-")) / "core"
shutil.copytree(ROOT / "bin", scratch / "bin",
                ignore=shutil.ignore_patterns("__pycache__"))
for extra in ("eval", ".claude"):
    src = ROOT / extra
    if src.is_dir():
        shutil.copytree(src, scratch / extra, ignore=shutil.ignore_patterns("__pycache__"),
                        dirs_exist_ok=True)
SCRATCH_RUNNER = scratch / "bin" / "casebook-run.py"
try:
    # THE PLANTING IS ALSO A MEASUREMENT, AND IT CAN FAIL SILENTLY. core-business, bus #987: its
    # anchor string was absent, `.replace()` no-opped, and the tool ran against an UNMODIFIED file
    # and passed — which reads as "this detector cannot fire", the opposite of the truth.
    #
    # Here the no-op fails toward RED rather than green (an unmodified runner keeps a consistent
    # fence, so `fired` stays False and the test goes red), which is the safe direction. But the
    # MESSAGE would be wrong: "still blind, the check is dead" accuses the fence when the truth is
    # that the fixture never installed. That is the wrong-cause class — the operator debugs the
    # wrong file. So the anchor is asserted, and a missing anchor says so in its own words.
    _anchor = '    "bin/casebook_predicates.py",\n'
    _before = SCRATCH_RUNNER.read_text()
    t = _before.replace(_anchor, "", 1)
    if t == _before:
        print("     FAIL — THE FIXTURE DID NOT INSTALL, so nothing below measures the fence.")
        print("            anchor not found in casebook-run.py: %r" % _anchor)
        print("            The TCB list was reformatted; re-point this anchor. The fence itself")
        print("            is NOT implicated by this failure.")
        _FAILED = True
        raise SystemExit(1)
    SCRATCH_RUNNER.write_text(t)
    code, out = run(SCRATCH_RUNNER, scratch)
    fired = "FENCE INCONSISTENT" in out
    print("     %s (exit %s)" % ("ok — DETECTED the breach" if fired
                                 else "FAIL — still blind, the check is dead", code))
    _FAILED = _FAILED or not fired
    if fired:
        for line in out.splitlines():
            if "casebook_predicates" in line or "FENCE INCONSISTENT" in line:
                print("       | %s" % line.strip()[:96])
finally:
    shutil.rmtree(scratch.parent, ignore_errors=True)

print("\n  RESTORED — must be silent again")
code, out = run()
print("     %s\n" % ("ok" if "FENCE INCONSISTENT" not in out else "FAIL — not restored"))

# Exit non-zero on any detected failure. This printed "FAIL — still blind, the check is dead" and
# then exited 0, so bin/tests/run-all.sh recorded a pass — a test whose failure is invisible to its
# own runner is the same defect it is testing for.
import sys
sys.exit(1 if _FAILED else 0)
