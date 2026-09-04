"""A PROTECTION LIST MUST BE SHOWN TO MATCH THE FILES IT PROTECTS.

core-business's rule, made executable. It found that Rule 1's trust-root list named only DIR globs
— `.claude/agents/sentinel/**` — while the specs that actually load are FLAT FILES. So a diff
rewriting the reviewer's own instructions was not a trust-root content change: Rule 1 never
engaged, the cap at ASK never applied, and Nick's confirmation was never demanded. That is the
exact attack sentinel-code.md's own text describes.

A list is a CLAIM ABOUT THE FILESYSTEM. An unverified claim about the filesystem is the same defect
as an unverified claim about a mechanism — and this one guarded the recursion-breaker.

BOTH DIRECTIONS, because either alone is uninformative:
  COVERAGE  every non-legacy entry matches at least one real file
  REACH     the files that ACTUALLY LOAD are matched by at least one entry

The second is the one that was broken: the list had entries, they matched *something*, and the
thing they matched was not the thing that runs.

Run: python3 bin/tests/test_protection_list_covers.py
"""
import fnmatch
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SPEC = ROOT / ".claude" / "agents" / "sentinel-code.md"

# Entries kept deliberately for Cores that have not pulled the flat-format migration. Absent here
# is CORRECT, so their absence must not fail the run — but they are named, not silently skipped.
LEGACY = {".claude/agents/sentinel/**", ".claude/agents/sentinel-code/**"}

# Real, current, non-legacy — but PER-SEAT RUNTIME STATE, not source. bin/trajectory-gate.py's own
# docstring: "bin/.gate-trusted-sha is PER-SEAT STATE, not source — written on first `--promote`.
# Untracked (not in .gitignore because nothing creates it except that command; a fresh clone or a
# Core that has never promoted a trust root simply does not have it yet. It still MUST be covered
# on the REACH side (Rule 1 must engage on it once it exists) — this exemption only means COVERAGE
# does not fail the run for a file this seat legitimately has not created yet.
NOT_YET_CREATED = {"bin/.gate-trusted-sha"}

# The files that actually load and therefore MUST be covered. This is the reach side: the point is
# not that the list has entries, it is that these specific files are behind one.
MUST_BE_COVERED = [
    ".claude/agents/sentinel.md",
    ".claude/agents/sentinel-code.md",
    ".claude/hooks/pretooluse-guard.sh",
    ".claude/hooks/sentinel-approve.sh",
    "bin/trajectory-gate.py",
    "bin/.gate-trusted-sha",
]

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % label)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (label, detail))


text = SPEC.read_text()
# BOUNDED EXTRACTION. A first cut grabbed every later bullet list in the file — credential
# patterns, dangerous commands, verdict names — and reported 25 of 34 entries "dead". The list was
# fine; the extractor was greedy. Stop at the first blank line after the block.
m = re.search(r"Trust-root paths[^\n]*\n\n((?:- .*\n)+)", text)
entries = re.findall(r"- `([^`]+)`", m.group(1)) if m else []

print("\n  trust-root list: %d entries\n" % len(entries))
check("the list is non-empty and parseable", bool(entries), "(extraction found nothing)")

files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
         if p.is_file() and ".git/" not in str(p)]

print("\n  COVERAGE — every non-legacy entry matches a real file")
for e in entries:
    if e in LEGACY:
        hits = [f for f in files if fnmatch.fnmatch(f, e)]
        print("  note  %-46s legacy, %s" % (e, "present" if hits else "absent (expected)"))
        continue
    hits = [f for f in files if f == e or fnmatch.fnmatch(f, e)]
    if not hits and e in NOT_YET_CREATED:
        print("  note  %-46s per-seat runtime state, %s" % (
              e, "present" if hits else "not created yet on this seat (expected)"))
        continue
    check("%-46s" % e, bool(hits), "matches nothing on disk")

print("\n  REACH — every file that actually loads is behind an entry")
for f in MUST_BE_COVERED:
    covered = any(f == e or fnmatch.fnmatch(f, e) for e in entries)
    check("%-46s" % f, covered, "NOT protected by any entry — Rule 1 will not engage on it")

print("\n=== Results: %d passed, %d failed ===\n" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
