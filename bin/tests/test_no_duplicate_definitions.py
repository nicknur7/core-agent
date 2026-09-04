#!/usr/bin/env python3
"""A SCRIPTED EDIT DUPLICATED 680 LINES OF THE FLEET'S PRIMARY MEASUREMENT INSTRUMENT AND `ast.parse`
SAID IT WAS FINE.

AUTHORED AND RUN ON core-finance. NOT INSTALLED HERE — finance is a puller and bin/tests/ is
baseline-shared, so per the routing life decided 2026-08-12 the source is returned on the bus and
life commits it. Intended install path: bin/tests/test_no_duplicate_definitions.py

**GREEN TODAY. Installable immediately.** Unlike probe 5 this is not red-by-design — it asserts an
invariant the tree currently satisfies, so it goes in as a fence rather than as a permanently-red
row in the suite.

WHAT HAPPENED, 2026-08-12 04:20 (core-life, reported on core-bus):

    "I tried to apply it with a scripted regex edit across both sites. It DUPLICATED 680 LINES of
     measure-contract-fitness.py — 706 lines to 1385, two `def _confounded`. `ast.parse` passed.
     The suite would probably have passed. I caught it by counting call sites, which I only did
     because the probe was still red and I went looking for why."

They reverted cleanly. The near-miss is the point: the corruption was caught by a human noticing an
unrelated symptom, not by any check.

WHY SYNTAX CHECKING CANNOT CATCH THIS — measured, not assumed:

    ast.parse on a file containing two `def foo`   ->  PARSES CLEAN, no error
    which body actually executes                   ->  THE SECOND ONE
    ast can list the duplicates trivially          ->  ['foo']

Python permits duplicate top-level definitions and silently binds the last one. So a corrupted
module can pass syntax, pass import, pass its whole test suite, and run a DIFFERENT FUNCTION BODY
than the one a reader is looking at. The file contains two truths and only one executes — the same
documentation-outlives-execution asymmetry this suite keeps finding, expressed in the parser.

That is precisely the failure mode a post-edit `ast.parse` gives false comfort about. `ast.parse`
answers "is this valid Python", and the question that mattered was "is this the same program".

WHAT THIS ASSERTS

  1. CONTROL / MUTATION, in-band — a synthetic module with a duplicate def MUST be flagged, and
     `ast.parse` MUST accept that same module. Both halves matter: the first proves the detector
     discriminates, the second proves syntax checking is genuinely insufficient rather than merely
     assumed to be. Without this the sweep below could pass by finding nothing for any reason.
  2. THE REAL INVARIANT — no module in the shared engine defines the same top-level function or
     class twice. Measured 2026-08-12 after widening: 183 modules scanned, 0 with duplicates.
     Green, and it fails on life's corrupted intermediate — dosed with their actual planted `def
     _corpus_baseline` duplicate, which ast.parse accepted.

Scope, stated so it is not read as broader than it is: TOP-LEVEL functions and classes only, across
scheduling/claude-si, scheduling/brain-pg, bin and bin/tests. Methods inside a class body are not
checked, nor are conditionally-defined functions inside if/try blocks, which are legitimate. Dotfiles
are skipped as deliberate fixtures. A duplicated 680-line block of top-level defs is the shape this
catches.

Read-only. No writes, no DB, no live state, temp files via TemporaryDirectory.

Run: python3 tasks/si-verification/probes/test_no_duplicate_definitions.py
"""
import ast
import importlib.util
import sys
import tempfile
from pathlib import Path


def _root() -> Path:
    p = Path(__file__).resolve()
    for cand in p.parents:
        if (cand / "scheduling" / "claude-si").is_dir() and (cand / "bin").is_dir():
            return cand
    raise SystemExit("SKIP - could not locate Core root")


ROOT = _root()

# WIDENED 2026-08-12 to the whole shared engine, after core-life pointed out that its FIRST
# corruption landed in an in-scope file and its SECOND (planted during the dose) did not — "that is
# luck, not coverage." Measured before widening rather than estimated:
#
#     scheduling/claude-si   25 modules  0.044s  0 duplicates
#     scheduling/brain-pg    28 modules  0.039s  0 duplicates
#     bin                    52 modules  0.065s  0 duplicates   1 unparseable (a dotfile fixture)
#     bin/tests              79 modules  0.060s  0 duplicates
#
# life's stated cost — "test files that legitimately shadow names" — does NOT apply: this check is
# PER FILE, so 79 test modules each defining `main()` is not shadowing. Total sweep ~0.2s, zero
# false positives. There was no reason to keep the narrow scope.
SCAN_DIRS = [ROOT / "scheduling" / "claude-si", ROOT / "scheduling" / "brain-pg",
             ROOT / "bin", ROOT / "bin" / "tests"]

# Dotfiles are excluded: they are not shipped modules. Concretely `bin/.lint-pragma-scope-fixture.py`
# is a DELIBERATELY malformed fixture for bin/tests/test_lint_pragma_scope.py — its body is
# `L = "tasks/lessons.md"  -- lint-code-paths: ignore`, which is not valid Python on purpose. Without
# this exclusion the "every scanned module parses" assertion would fail on a file whose whole job is
# to be unparseable.
def _modules(d: Path):
    return sorted(f for f in d.glob("*.py") if not f.name.startswith("."))

DUP_SRC = "def foo():\n    return 'FIRST'\n\n\ndef foo():\n    return 'SECOND'\n"


def duplicate_top_level(path: Path):
    """Names defined more than once at module top level. Empty list = clean."""
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except SyntaxError:
        return None  # unparseable is a different failure; reported separately
    names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return sorted({n for n in names if names.count(n) > 1})


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== no module may define the same top-level name twice ===\n")

    # ---- 1. CONTROL / MUTATION -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        dup = Path(td) / "synthetic_dup.py"
        dup.write_text(DUP_SRC)

        parses = True
        try:
            ast.parse(DUP_SRC)
        except SyntaxError:
            parses = False

        check("CONTROL - ast.parse ACCEPTS a module with a duplicate def "
              "(so syntax checking cannot catch this)",
              parses is True,
              "ast.parse rejected it, which would mean a post-edit syntax check IS sufficient and "
              "this probe is unnecessary")

        check("CONTROL - the detector FLAGS that same module",
              duplicate_top_level(dup) == ["foo"],
              "got %r — the sweep below cannot be trusted if the detector does not fire on a known "
              "duplicate" % (duplicate_top_level(dup),))

        # The consequence, asserted rather than described: the LAST definition is what runs.
        spec = importlib.util.spec_from_file_location("synthetic_dup", dup)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        check("CONTROL - the SECOND definition is the one that executes",
              mod.foo() == "SECOND",
              "got %r — if the first won, a duplicate would be inert rather than silently "
              "replacing behaviour, and the severity argument here changes" % mod.foo())

    # ---- 2. THE REAL INVARIANT --------------------------------------------------------------
    print("\n--- sweeping the shared engine ---")
    offenders, unparseable, scanned = {}, [], 0
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for src in _modules(d):
            scanned += 1
            dups = duplicate_top_level(src)
            if dups is None:
                unparseable.append(src.name)
            elif dups:
                offenders[str(src.relative_to(ROOT))] = dups

    check("CONTROL - the sweep actually scanned modules "
          "(an empty sweep passes vacuously)",
          scanned >= 10, "only %d module(s) scanned across %s" % (scanned, [str(d) for d in SCAN_DIRS]))

    check("no module defines the same top-level name twice",
          not offenders,
          "duplicates found: %r — a scripted edit may have doubled a block. `ast.parse` will NOT "
          "flag this and the suite may still pass while a different function body executes. Check "
          "line count and def count against HEAD before trusting the file." % offenders)

    check("every scanned module parses",
          not unparseable, "unparseable: %r" % unparseable)

    print("\n  scanned %d module(s); %d with duplicate top-level names" % (scanned, len(offenders)))
    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
