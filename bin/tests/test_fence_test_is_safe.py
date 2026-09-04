"""business's exact scenario: SIGKILL the fence test mid-run. Does the live runner survive?

The old version wrote a mutated copy over the real bin/casebook-run.py and restored it in a
`finally`. A `finally` does not run on SIGKILL — so a hard interruption left the Core's TCB fence
permanently and silently under-fenced. This runs at every session close on every Core.
"""
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root as _core_root  # noqa: E402

# DERIVED. Hardcoded life when written — caught by the cross-Core path gate on its next run.
# Third time today the gate found a hardcode I had just introduced, each in a test written to
# verify a fix for hardcoded paths.
ROOT = _core_root()
RUNNER = ROOT / "bin" / "casebook-run.py"


def digest():
    return hashlib.sha256(RUNNER.read_bytes()).hexdigest()[:12]


before = digest()
print("\n  live runner before : %s" % before)

proc = subprocess.Popen([sys.executable, str(ROOT / "bin/tests/test_tcb_fence.py")],
                        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# Kill well INTO the run — past the negative phase, inside the liveness mutation window.
time.sleep(12)
try:
    os.kill(proc.pid, signal.SIGKILL)
    print("  SIGKILL sent at t+12s (mid-liveness)")
except ProcessLookupError:
    print("  process already exited before the kill window")
proc.wait()
time.sleep(1)

after = digest()
print("  live runner after  : %s" % after)
print("\n  %s\n" % ("SURVIVED — the live gating script is byte-identical after a hard kill"
                    if before == after else
                    "MUTATED — a SIGKILL leaves this Core's TCB fence silently under-fenced"))
sys.exit(0 if before == after else 1)
