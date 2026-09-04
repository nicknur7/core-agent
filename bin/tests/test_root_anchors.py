"""Which bin/ tools ACTUALLY resolve the wrong Core cross-seat?

I told Nick and core-business that ELEVEN tools carry the vulnerable __file__ anchor. That number
came from grepping for the PATTERN without checking whether the same function also honours
CORE_INSTANCE / CLAUDE_PROJECT_DIR first — grade-gate.py does, and is fine. A grep for a shape is
not a measurement of a behaviour.

Vulnerable = resolves its root from __file__ with NO environment override anywhere in the file.
"""
import pathlib
import re

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _core import core_root as _core_root  # noqa: E402

# DERIVED. This file's whole job is catching tools that resolve the wrong Core, and it hardcoded
# life. Run from business it scanned LIFE and printed "The measured number is 0" at exit 0 — the
# instrument that produced my "zero remaining" could not have produced anything else.
ROOT = _core_root()
ANCHOR = re.compile(r"(parents\[\d\]|parent\.parent|dirname\(os\.path\.dirname)")
ENVOVR = re.compile(r"CORE_INSTANCE|CLAUDE_PROJECT_DIR|core_root|caller_core_root|show-toplevel")

vuln, safe = [], []
for f in sorted(ROOT.glob("bin/*.py")):
    try:
        src = f.read_text(errors="replace")
    except OSError:
        continue
    if not ANCHOR.search(src):
        continue
    (safe if ENVOVR.search(src) else vuln).append(f.name)

print("\n  ROOT-RESOLUTION AUDIT — bin/*.py\n")
print("  %d file(s) anchor on __file__ AND honour an env override (safe cross-seat):" % len(safe))
for n in safe:
    print("     ok   %s" % n)
print("\n  %d file(s) anchor on __file__ with NO override (resolve the WRONG Core cross-seat):"
      % len(vuln))
for n in vuln:
    print("     VULN %s" % n)
print("\n  Core measured: %s" % ROOT.name)
print("  vulnerable: %d" % len(vuln))
import sys
sys.exit(1 if vuln else 0)
