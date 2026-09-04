#!/usr/bin/env python3
"""A FINDING MUST BE IDENTICAL IN EVERY ARM, or the gate reports a regression no candidate caused.

bin/trajectory-gate.py compares per-item findings AS STRING SETS between two arms materialised into
DIFFERENT temp directories. So any finding that embeds a path differs between arms by construction,
and the difference reads as `REGRESSION: <item> gained findings`.

THE INSTANCE, AND THEN THE CLASS. I added `(seat=%s)` to a RuntimeError for diagnosis on 2026-08-10;
T11, T12 and T13 then reported a permanent phantom regression and the gate's own matched-pair test
went from KEEP to REVERT for a candidate with no side effect at all. Deleting that format string
closed the instance.

core-business named the class: nobody has to type a format string to hit it.

    rec["findings"] = [f"{type(e).__name__}: {e}"]

Any exception whose message renders a path — FileNotFoundError, PermissionError, a RuntimeError built
from a Path, a JSONDecodeError naming a file — becomes arm-specific for free. It proved the class by
materialising the SAME COMMIT into two temp dirs and scoring both with one frozen runner: content
byte-identical, and at 0352ca1 three items differed, every one carrying `/private/var/folders/…`.

> **A diagnostic that varies per arm is not a diagnostic in a comparison.** It is noise wearing a
> finding's clothes, and it fails toward REVERT, which reads as caution rather than as a bug.

Run: python3 bin/tests/test_findings_are_arm_stable.py
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
spec = importlib.util.spec_from_file_location("cr", str(ROOT / "bin" / "casebook-run.py"))
cr = importlib.util.module_from_spec(spec)
sys.modules["cr"] = cr
spec.loader.exec_module(cr)

# Two arms, same defect. Every pair must render to ONE string.
ARM_A = "/private/var/folders/rx/aaa111/T/core-gate-arms-1/base"
ARM_B = "/private/var/folders/rx/zzz999/T/core-gate-arms-2/cand"

PAIRS = [
    ("RuntimeError carrying the seat",
     RuntimeError("no transcript directory resolved for this Core (seat=%s)" % ARM_A),
     RuntimeError("no transcript directory resolved for this Core (seat=%s)" % ARM_B)),
    ("FileNotFoundError on a materialised path",
     FileNotFoundError("[Errno 2] No such file or directory: '%s/eval/casebook-v1.json'" % ARM_A),
     FileNotFoundError("[Errno 2] No such file or directory: '%s/eval/casebook-v1.json'" % ARM_B)),
    # NO REAL CORE NAME IN A FIXTURE. test_no_cross_core_paths caught this file on its first run —
    # the third of my fixtures it has caught today. A test naming a Core is indistinguishable from a
    # hardcoded dependency on it, and the property here is about HOME-RELATIVE ABSOLUTE PATHS, not
    # about which Core the path happens to name.
    ("a message embedding a home path",
     ValueError("could not read /Users/someone/Projects/checkout/bin/x.json"),
     ValueError("could not read /Users/other/Projects/checkout/bin/x.json")),
]

# These must survive untouched — over-sanitising destroys the diagnosis the finding exists to carry.
KEEP = [
    ValueError("S3 line 12 has no completion stamp"),
    FileNotFoundError("memory/access-log.md missing"),
    RuntimeError("bin/casebook_predicates.py not present"),
]


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== findings must be arm-stable ===\n")

    for label, a, b in PAIRS:
        sa, sb = cr._stable_finding(a), cr._stable_finding(b)
        check("%s renders identically in both arms" % label, sa == sb,
              "A: %s\n          B: %s" % (sa, sb))
        # THE CONTROL. Without it a PASS proves nothing: two identical inputs would also match.
        raw_a = "%s: %s" % (type(a).__name__, a)
        raw_b = "%s: %s" % (type(b).__name__, b)
        check("...and WOULD have differed unsanitised (so the check has teeth)", raw_a != raw_b,
              "the fixture does not actually vary by arm")

    print("\n--- relative paths and ordinary text must survive ---")
    for e in KEEP:
        raw = "%s: %s" % (type(e).__name__, e)
        check("unchanged: %s" % raw[:58], cr._stable_finding(e) == raw, cr._stable_finding(e))

    print("\n--- every exception-to-finding site goes through it ---")
    # A sanitiser one raise site forgets is the same defect with a longer fuse.
    src = (ROOT / "bin" / "casebook-run.py").read_text()
    leftovers = src.count('[f"{type(e).__name__}: {e}"]') + src.count('["%s: %s" % (type(e).__name__, e)]')
    check("no exception is rendered into a finding without sanitising", leftovers == 0,
          "%d raw render site(s) remain" % leftovers)
    check("...and the sanitiser is actually used", src.count("_stable_finding(e)") >= 3,
          "only %d call site(s)" % src.count("_stable_finding(e)"))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
