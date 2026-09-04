#!/usr/bin/env python3
"""The financial detector must catch money and must not catch shell positionals.

WHAT WENT WRONG. `reply-observer._FINANCIAL` was:

    (\\$\\s?[\\d,]+(?:\\.\\d{2})?\\b|\\b\\d+(?:\\.\\d+)?\\s*(?:USD|dollars)\\b)

The first branch matches `$1`. core-life writes a great deal of awk, sed and shell into its replies,
so `awk '{print $1,$2}'` scored as an unsourced financial claim — and enough of them accumulated
that si-objective cleared its work-order threshold (10 absolute, 1.21/100) and filed a request for a
hand-written pre-emption hook. The behaviour someone was asked to go prevent was, on that seat,
substantially awk. Tightened 2026-08-25 to require a currency SHAPE.

WHY THIS TEST EXISTS RATHER THAN A COMMENT. Two independent near-misses, both worth pinning:

  · The tightening was described in a bus message and a commit as `\\$\\s?[\\d,]+` — a shorthand that
    silently dropped the optional cents group AND the entire `USD|dollars` alternation. core-ops
    read line 202 instead of the description, flagged that a rewrite done against the shorthand
    would lose the USD branch, and asked for confirmation. The branch had survived — but the
    original test covered `40 USD` and NOT `40 dollars`, so the one case ops asked about was
    precisely the one nothing checked. That branch matters most on core-finance: it is what catches
    a figure written without a `$`.

  · ops also widened the confound correctly. It is not an awk problem, it is any `$<digit>` in
    shell or regex text — `sed 's/$1/x/'`, `${1}`, `git diff $1`. A seat that discusses shell at all
    carries a floor unrelated to money, proportional to how much shell it writes.

  · And the digest-verification command life had been publishing to the fleet,
    `awk '{print $2,$4,$5,$6}'`, was itself a financial_figure to the old detector. The instruction
    for verifying a push scored as a money claim.

ACCEPTED LOSS, pinned here so it stays deliberate: a bare single-digit amount ($0, $5) no longer
counts. That is the price of excluding $1-$9, and it is right only because an ACCOUNT figure — the
thing Nick's pulled-live-never-recalled rule is about — is essentially never a bare single digit.

Run: python3 bin/tests/test_financial_detector_excludes_shell.py
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
OBSERVER = ROOT / ".claude" / "hooks" / "reply-observer.py"

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


spec = importlib.util.spec_from_file_location("_ro_fin", OBSERVER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
RX = mod._FINANCIAL

# Real money, in every shape the fleet actually writes.
MUST_MATCH = [
    ("the balance is $12,431.88",        "brokerage balance, comma + cents"),
    ("cash balance $1,204",              "comma, no cents"),
    ("costs ~$0.18 (fal+Sonnet)",        "sub-dollar API cost"),
    ("$10/$50 per MTok",                 "model pricing"),
    ("buying power $2,500.00",           "buying power"),
    ("about $50 a month",                "bare two-digit"),
    ("$211.95",                          "bare amount with cents"),
    # THE USD/dollars BRANCH — the one ops asked about. `40 dollars` was NOT covered before.
    ("roughly 40 USD",                   "USD suffix"),
    ("roughly 40 dollars",               "dollars suffix — ops's specific ask"),
    ("3.5 dollars",                      "fractional dollars"),
    ("12 USD",                           "small USD amount"),
]

# Shell and regex text. None of this is money.
MUST_NOT_MATCH = [
    ("awk '{print $1,$2}'",                    "awk positionals"),
    ("awk '{print $2,$4,$5,$6}' | sort",       "the fleet's own digest command"),
    ("| awk '{print $3}'",                     "single positional in a pipe"),
    ("sed 's/$1/x/'",                          "sed positional"),
    ("${1} positional refs quoted in prose",   "braced positional"),
    ("git diff $1",                            "positional as an argument"),
    ("for i in $1 $2; do",                     "loop over positionals"),
    ("the parent re-invokes $1 after cd'ing",  "prose about a positional"),
    ("head -1 | cut -d: -f1 | awk {print $2}", "full pipeline"),
]

print("=== money must still be caught ===")
for text, why in MUST_MATCH:
    m = RX.search(text)
    check(f"{why:38s} {text[:34]!r}", bool(m), "NOT matched — a real figure would go uncounted")

print()
print("=== shell must not be caught ===")
for text, why in MUST_NOT_MATCH:
    m = RX.search(text)
    # Detail built ONLY when there is a match. The first version interpolated m.group(0)
    # unconditionally, so it raised AttributeError on every PASS — a test that could only run
    # when it was failing.
    check(f"{why:38s} {text[:34]!r}", m is None,
          (f"matched {m.group(0)!r} — this is the bug that filed a work order") if m else "")

print()
print("=== the accepted loss, stated so it stays deliberate ===")
for text in ("scored at $0", "$5 flat"):
    check(f"bare single digit is NOT counted: {text!r}", RX.search(text) is None)

print()
print("=== the USD branch is structurally present, not incidentally passing ===")
check("pattern retains a USD/dollars alternation",
      "USD" in RX.pattern and "dollars" in RX.pattern, RX.pattern[:120])

print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
