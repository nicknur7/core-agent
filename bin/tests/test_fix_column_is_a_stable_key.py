#!/usr/bin/env python3
"""A detector's FIX column is half of the trust key, so it must be STABLE.

`si-fix-admission.py` counts consecutive approvals for an exact `(signal_key, fix_action)` pair.
`fix_action` is the FIX column of the `add()` call in `scheduling/core-si/detect.sh`. So any fix
string that interpolates a date, a path, a SHA or a count produces a DIFFERENT key every time the
detector fires — and the streak can never reach 2. The fix can never be trusted, so it can never be
auto-applied, and nothing anywhere says why.

THIS WAS NOT HYPOTHETICAL. Measured on core-life 2026-08-26, before the fix:

    core_si_fix_approvals, signal_key='sys-brainlint':
        approve | "act inline on real gaps"
        approve | "act inline on real gaps (see /Users/.../brain-lint-reports/2026-08-12.md)"
    core_si_trusted_fixes, signal_key='sys-brainlint':
        0 rows

Nick approved the same fix twice and it never graduated, because the dated report path in the fix
column rotated the key between his two approvals. Under the evidence-seeded gate the same defect is
worse: an applier's own successful run regenerates the dated artifact, which rotates the key, which
de-trusts the applier that just succeeded.

THE RULE: volatile detail belongs in the DETECTED column, which is not part of the key. The FIX
column names the remedy and nothing else.

Run: python3 bin/tests/test_fix_column_is_a_stable_key.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
DETECT = ROOT / "scheduling" / "core-si" / "detect.sh"

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


check("detect.sh exists", DETECT.is_file())
src = DETECT.read_text()

# Join continuation lines so a multi-line add() is one logical call.
joined = re.sub(r"\\\s*\n\s*", " ", src)

# add "SEV" "DOMAIN" "DETECTED" "FIX" "FITNESS" "KEY"
CALL = re.compile(r'^\s*add\s+"([^"]*)"\s+"([^"]*)"\s+"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"\s+"([^"]*)"\s+"([^"]*)"',
                  re.M)
calls = CALL.findall(joined)
check("found the add() calls", len(calls) >= 8, f"found {len(calls)}")

# Volatile things that must not appear in a FIX string.
VOLATILE = [
    (re.compile(r"\$\{?LATEST_\w+"), "an interpolated latest-file path"),
    (re.compile(r"\$\{?\w*(DATE|TIME|STAMP)\w*"), "an interpolated date/time"),
    (re.compile(r"\$\{?\w*(COUNT|_N|NUM)\b"), "an interpolated count"),
    (re.compile(r"\$\{?\w*(SHA|HEAD|COMMIT)\w*"), "an interpolated SHA"),
    (re.compile(r"/Users/"), "an absolute path"),
    (re.compile(r"\b20\d\d-\d\d-\d\d\b"), "a literal date"),
]

print()
print("=== every FIX column must be a constant remedy, not a rendered artifact ===")
for sev, domain, detected, fix, fitness, key in calls:
    bad = [why for rx, why in VOLATILE if rx.search(fix)]
    check(f"{key or '(no key)'} fix column is stable", not bad,
          f"contains {', '.join(bad)}: {fix.strip()[:70]}")

print()
print("=== the specific regression that cost Nick two approvals ===")
brainlint = [c for c in calls if c[5] == "sys-brainlint"]
check("sys-brainlint still has an add() call", bool(brainlint))
for c in brainlint:
    check("sys-brainlint's fix no longer embeds the dated report path",
          "LATEST_LINT" not in c[3], c[3].strip()[:70])
    check("...and the path moved into the DETECTED column where it belongs",
          "LATEST_LINT" in c[2] or "LINT_DATE" in c[2], c[2].strip()[:70])

print()
print("=== every add() supplies a non-empty key (the other half of the pair) ===")
for sev, domain, detected, fix, fitness, key in calls:
    check(f"key present for: {detected.strip()[:44]!r}", bool(key.strip()))

print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
