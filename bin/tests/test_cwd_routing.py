"""Does every Core route to itself, and does an UNKNOWN Core get quarantined instead of folded in?

The old failure was silent and plausible: ops content inside life's partition looks like life
content. So the negative case here is not "does ops route right" — it is "does a Core nobody has
ever heard of still refuse to become life".
"""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _core import core_root as _core_root  # noqa: E402
import importlib.util
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _core import core_root as _core_root  # noqa: E402

# DERIVED. This hardcoded LIFE's export.py while template/ is shared — and business's live copy is
# MISSING the _discovered_core_rules() this range adds, so the two files under test differ right
# now and the test only ever exercised mine. It correctly avoids the copy-vs-shipped antipattern
# its sibling test_shipped_metrics.py names, then imported the wrong Core's real code.
_ROOT = _core_root()

# export.py DEMANDS $CORE_BRAIN AT IMPORT TIME, and this test imports it — so with the variable
# unset the file aborted during exec_module and the whole test read RED for a reason that has
# nothing to do with routing. A permanently-red test is switched off exactly as fast as a
# permanently-green one is ignored, and neither tells you anything about the subject.
#
# Derived from the sibling layout rather than exported by the caller, for the same reason every
# other path in bin/tests/ is derived: a variable the runner has to remember to set is a variable
# that is unset on the Core where it matters. Absent the vault, this REFUSES rather than skipping
# to green — the routing table is the thing that put 168 ops rows in life's partition.
import os  # noqa: E402
if not os.environ.get("CORE_BRAIN"):
    _vault = _ROOT.parent / "core-brain"
    if not (_vault / "projects").is_dir() and not _vault.is_dir():
        print("  UNDECIDABLE — no brain vault at %s and $CORE_BRAIN unset; cannot exercise the "
              "routing table that misfiled 168 rows. Refusing to report this as clean." % _vault)
        sys.exit(2)
    os.environ["CORE_BRAIN"] = str(_vault)

spec = importlib.util.spec_from_file_location(
    "exp", str(_ROOT / "template" / "brain" / "_build" / "export.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["exp"] = m
spec.loader.exec_module(m)

B = str(_core_root().parent) + "/"
CASES = [
    (B + "core-life", "life"),
    (B + "core-business", "business"),
    (B + "core-school", "school"),
    (B + "core-finance", "finance"),
    (B + "core-ops", "ops"),                       # WAS 'life' — the bug
    (B + "core-life/memory", "life"),                # subdirectory of a Core
    (B + "core-ops/tasks/research", "ops"),        # subdirectory of the bug
]
UNKNOWN = [
    (B + "core-medical", "unrouted-medical"),        # a Core that does not exist yet
    (B + "core-zzz/sub", "unrouted-zzz"),
]

bad = 0
print("\n  KNOWN CORES — each must route to itself\n")
for cwd, want in CASES:
    got = m.categorize_cwd(cwd)
    ok = got == want
    bad += not ok
    print("  %-6s %-42s -> %-18s %s" % ("ok" if ok else "FAIL", cwd.replace(B, ""), got,
                                        "" if ok else "(want %s)" % want))

print("\n  UNKNOWN CORE — must be QUARANTINED, never folded into life\n")
for cwd, want in UNKNOWN:
    got = m.categorize_cwd(cwd)
    ok = got == want
    bad += not ok
    flag = "" if ok else ("  <-- SILENTLY BECAME '%s'" % got)
    print("  %-6s %-42s -> %-18s%s" % ("ok" if ok else "FAIL", cwd.replace(B, ""), got, flag))

print("\n  %s\n" % ("ALL CORRECT" if not bad else "%d WRONG" % bad))
sys.exit(1 if bad else 0)
