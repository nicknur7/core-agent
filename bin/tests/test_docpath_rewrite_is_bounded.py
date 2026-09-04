#!/usr/bin/env python3
"""The archival doc-path rewriter must not manufacture the breakage it exists to fix.

`bin/lint-doc-paths.py --fix-archival` has run at every full close since 2026-07-26
(`session-lifecycle.sh:848`). It rewrote a broken citation to its archive location with a plain
`text.replace(cited, target)` — a GLOBAL SUBSTRING replace. So a document citing both
`tasks/foo.md` and `tasks/foo.md.bak` had the second one rewritten too:

    tasks/foo.md.bak  ->  tasks/archive/foo.md.bak

which is a path that never existed. The fixer manufactured a fresh broken reference, and its own
detector then nagged about it at every close, forever. A self-feeding loop that looks like activity.

Two more properties this pins, both of which were absent:

  · REVIEWER CHARTERS ARE REPORT-ONLY. `.claude/agents/sentinel*.md` are the live contracts the
    outward-action gate is reviewed against. A basename-collision rewrite inside one re-aims the
    security gate's cited contract at an ARCHIVED document, while the linter reports clean.
  · WRITES ARE CONDITIONAL. `doc.write_text(text)` ran unconditionally on every scanned document,
    bumping mtimes and dirtying the tree even when nothing changed — which defensive-save then
    committed as noise.

And the split the whole core-si key design rests on: `split_counts()` must separate the
mechanically-provable population from the judgment population, because the KEY is the admission
unit and `auto-safe.txt`'s HARD FLOOR reserves the latter.

Run: python3 bin/tests/test_docpath_rewrite_is_bounded.py
"""
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
LINT = ROOT / "bin" / "lint-doc-paths.py"

_passed = 0
_failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


check("lint-doc-paths.py exists", LINT.is_file())
src = LINT.read_text()

print()
print("=== the rewrite must be word-bounded, not a global substring replace ===")
check("no bare text.replace(cited, ...) survives in the fix path",
      "text.replace(cited" not in src,
      "a global substring replace corrupts longer citations")
check("the rewrite uses a negative-lookahead boundary",
      re.search(r"re\.sub\(rf?\"\{re\.escape\(cited\)\}\(\?!", src) is not None
      or "(?![\\w./\\-])" in src or r"(?![\w./\-])" in src,
      "expected a (?![\\w./-]) guard after the escaped citation")

# Demonstrate the property itself, independent of the implementation.
print()
print("=== demonstrated on the exact shape that used to corrupt ===")
text = "see tasks/foo.md and also tasks/foo.md.bak for detail"
cited, target = "tasks/foo.md", "tasks/archive/foo.md"
naive = text.replace(cited, target)
bounded = re.sub(rf"{re.escape(cited)}(?![\w./\-])", lambda _m: target, text)
check("the OLD approach corrupts the longer citation (this is the bug)",
      "tasks/archive/foo.md.bak" in naive, naive)
check("the NEW approach leaves the longer citation alone",
      "tasks/foo.md.bak" in bounded and "tasks/archive/foo.md.bak" not in bounded, bounded)
check("...while still fixing the real one", bounded.startswith("see tasks/archive/foo.md "), bounded)

print()
print("=== reviewer charters are report-only ===")
check("a REPORT_ONLY_PREFIX exists", "REPORT_ONLY_PREFIX" in src)
check("it covers .claude/agents/", '".claude/agents/"' in src or "'.claude/agents/'" in src)
check("the fix loop consults it",
      "report_only" in src and "archival_target" in src,
      "the guard must be applied per-document inside the rewrite loop")

print()
print("=== writes are conditional ===")
check("the document is only written when it changed",
      "if text != original:" in src,
      "an unconditional write bumps mtimes on every scanned doc")

print()
print("=== ONE predicate, exported for the detector AND the applier ===")
check("split_counts() is defined", "def split_counts(" in src)
check("archival_target() is defined", "def archival_target(" in src)
close_src = (ROOT / "bin" / "core-si-close.py").read_text()
check("the applier shells out to --split rather than re-deriving the rule",
      "--split" in close_src and "ARCHIVE_GLOBS" not in close_src,
      "a second copy of the predicate is how the previous doc-path bug survived")

print()
print("=== the bare judgment key must NOT be auto-safe ===")
safe = (ROOT / "scheduling" / "core-si" / "auto-safe.txt").read_text()
effective = [ln.strip() for ln in safe.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
check("sys-docpath-archival IS admitted", "sys-docpath-archival" in effective, str(effective))
check("bare sys-docpath is NOT admitted", "sys-docpath" not in effective,
      "admitting the bare key would mark the reserved judgment half auto-applied")

print()
print("=== --split runs and returns both populations ===")
try:
    r = subprocess.run([sys.executable, str(LINT), "--split"], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=180)
    import json as _json
    d = _json.loads(r.stdout.strip().splitlines()[-1])
    ok = "archival_fixable" in d and "other" in d
except Exception as e:
    d, ok = str(e), False
check("--split emits {archival_fixable, other}", ok, str(d)[:120])

print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
