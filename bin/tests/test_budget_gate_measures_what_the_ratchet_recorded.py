#!/usr/bin/env python3
"""The promote gate must compare the ratchet against the SAME set the ratchet was recorded from.

WHY THIS EXISTS (2026-08-20, found by core-business, confirmed independently by all four peers).

`test_steering_budget.py` sums NINE always-loaded files and records that total as the seat's
ratchet. `artifact_generator.py`'s promote gate read that nine-file ceiling and subtracted the
size of ONE file:

    _ceiling = json.loads(baseline)["ceiling"]     # nine-file total
    _now     = CLAUDE_MD.stat().st_size // 4       # one file
    _headroom = _ceiling + 200 - _now

The comment on that line said "same coarse tok proxy the test uses". The PROXY was the same
(bytes//4). The SCOPE was not. Measured on every seat the same afternoon:

    seat       gate reported free    actually
    business        15,669 tok       **7,441 OVER**
    school          12,166 tok       **3,739 OVER**
    finance          9,871 tok       **2,747 OVER**
    ops            11,878 tok       **2,674 OVER**
    life            ~9,500 tok       23 under

**It failed OPEN in exactly the case it exists to catch**, under a docstring promising "THREE
GATES, ALL REQUIRED, ALL FAIL-CLOSED." And it did not merely fail silently: business reproduced
the gate's own arithmetic to tell Nick in a decision brief that a directive write was cleared,
and finance had relayed the same gate to him days earlier. The instrument and the check agreed
because they were the same wrong computation — which is why "I checked" was not protection.

WHAT THIS ASSERTS

  1. FUNCTIONAL — a synthetic seat whose nine files exceed its recorded ceiling yields NEGATIVE
     headroom. This is the case every real seat was in while the gate said "clear".
  2. FUNCTIONAL — headroom tracks a file that is NOT CLAUDE.md. Under the old arithmetic, growing
     lessons.md (67% of business's load) moved the true total and nothing the gate looked at.
  3. STRUCTURAL — the gate calls the shared measurement and no longer sizes a single file inside
     its budget block. Kept alongside the functional checks because the defect was not a wrong
     number, it was two modules disagreeing about what to count; that only shows up in source.
"""
import re
import sys
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin"))
import steering_load as sl  # noqa: E402

failures, checks = [], 0


def check(label, ok, detail=""):
    global checks
    checks += 1
    print(("  ok     " if ok else "  FAIL   ") + label + (("\n           " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(label)


def make_seat(tmp: Path, sizes: dict) -> Path:
    """Build a synthetic Core root with the given per-file byte sizes."""
    for rel in sl.ALWAYS_LOADED:
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * sizes.get(rel, 0))
    return tmp


def record(tmp: Path, ceiling: int):
    p = sl.baseline_path(tmp)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ceiling": ceiling}))


# --- 1. an over-budget seat must read as over budget -----------------------------------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    # CLAUDE.md deliberately TINY, lessons.md enormous — the exact business shape.
    make_seat(tmp, {"CLAUDE.md": 400, "tasks/lessons.md": 60_000})
    record(tmp, 4_000)
    hr, total, ceiling = sl.headroom(tmp)
    check("an over-budget seat yields NEGATIVE headroom",
          hr is not None and hr < 0,
          f"headroom={hr} total={total} ceiling={ceiling} — this is the shape that read as CLEAR")

    # the old arithmetic, reproduced, to show it would have passed
    old_headroom = ceiling + sl.TOLERANCE - (len((tmp / "CLAUDE.md").read_text()) // 4)
    check("the OLD single-file arithmetic would have passed this same seat",
          old_headroom >= 80,
          f"old={old_headroom} — if this fails the regression demo is wrong, not the fix")

# --- 2. headroom must respond to a non-CLAUDE.md file ----------------------------------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    make_seat(tmp, {"CLAUDE.md": 400, "tasks/lessons.md": 400})
    record(tmp, 10_000)
    before = sl.headroom(tmp)[0]
    (tmp / "tasks" / "lessons.md").write_text("x" * 40_000)
    after = sl.headroom(tmp)[0]
    check("growing lessons.md reduces headroom",
          after < before,
          f"before={before} after={after} — lessons.md was 67% of business's load and invisible to the gate")

# --- 3. an unmeasured seat reports UNKNOWN, never unlimited ----------------------------------
with tempfile.TemporaryDirectory() as d:
    tmp = make_seat(Path(d), {"CLAUDE.md": 400})
    hr, _, ceiling = sl.headroom(tmp)
    check("a seat with no recorded baseline returns ceiling=None (unknown, not unlimited)",
          ceiling is None and hr is None)

# --- 4. an unsettled measurement must RAISE, not return a guess ------------------------------
# The gate's threshold is 80 tok and life's live session moved the set by 16 mid-read on
# 2026-08-20 (privacy.md rewritten in place, content byte-identical, mtime moved). A boundary
# test that depends on which microsecond it read CLAUDE.md is not a test.
_real = sl._measure_once
try:
    seq = iter([([], 1000), ([], 1016), ([], 1032)])   # never settles
    sl._measure_once = lambda root: next(seq)
    raised = False
    try:
        sl.measure(Path("/nonexistent"))
    except sl.UnstableMeasurement:
        raised = True
    check("a steering set that never settles raises UnstableMeasurement", raised,
          "returning a guess here lets a race decide a promote")

    # and a set that settles on the second read must NOT raise — an ordinary race costs a re-read
    seq2 = iter([([], 1000), ([], 1016), ([], 1016)])
    sl._measure_once = lambda root: next(seq2)
    settled = sl.measure(Path("/nonexistent"))[1] == 1016
    check("a set that settles after one retry returns normally", settled,
          "raising on first disagreement would spuriously block on any live seat")
finally:
    sl._measure_once = _real

# --- 5. structural: the gate uses the shared measurement -------------------------------------
src = (ROOT / "scheduling" / "claude-si" / "artifact_generator.py").read_text()
m = re.search(r"_bl\s*=.*?steering-budget-baseline\.json(.*?)promote_blocked", src, re.S)
block = m.group(1) if m else ""
check("the budget gate calls the shared steering measurement",
      "steering_load" in src and ".headroom(" in src)
check("the budget gate no longer sizes a single file",
      "CLAUDE_MD.stat()" not in block,
      "a lone CLAUDE.md stat inside the budget block is the 2026-08-20 defect returning")

print()
if failures:
    print("  FAIL=%d of %d" % (len(failures), checks))
    for f in failures:
        print("    - " + f)
    sys.exit(1)
print("  ok=%d  FAIL=0 — gate and ratchet measure the same set" % checks)
