#!/usr/bin/env python3
"""The always-loaded steering set, measured ONCE for every caller that needs it.

WHY THIS FILE EXISTS (2026-08-20, found by core-business).

`bin/tests/test_steering_budget.py` summed NINE always-loaded files and recorded that sum as the
Core's ratchet. `artifact_generator.py`'s promote gate then read that nine-file ceiling and
subtracted the size of ONE file:

    _ceiling = int(json.loads(_bl.read_text())["ceiling"])   # a NINE-file budget
    _now     = CLAUDE_MD.stat().st_size // 4                 # ONE file
    _headroom = _ceiling + 200 - _now                        # nonsense

The comment on that line read "same coarse tok proxy the test uses". **The proxy was the same
(bytes//4). The scope was not.** So the gate reported enormous headroom on a seat that was over
budget — on business, 15,669 tok of "headroom" while 7,441 tok IN BREACH. It failed OPEN in
exactly the case it exists to catch, under a docstring promising "THREE GATES, ALL REQUIRED, ALL
FAIL-CLOSED." On life it reported ~9,500 tok free against a true headroom of 23.

Worse than the arithmetic: business reproduced the gate's own calculation to tell Nick a
directive write was safe, in a decision brief. The instrument and the check agreed because they
were the same wrong computation.

**So the list and the arithmetic live in ONE place and both callers import them.** A second
implementation of "how much steering does this Core load" is what produced the defect; adding a
corrected copy beside the old one would reproduce it.

Note `bytes // 4` is a coarse proxy, not a tokenizer. It is kept because the ratchet compares a
Core against ITS OWN earlier measurement — a consistent proxy is sufficient for a ratchet and a
real tokenizer would invalidate every recorded baseline on the fleet for no gain in what the
number is used for.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# The project contract, the baseline it imports, every rules file it names as load-on-session, and
# the active lessons. A Core lacking one (e.g. a peer has no rules-life/ overlay) contributes 0 —
# absence is not an error, it is a smaller load.
ALWAYS_LOADED = [
    "CLAUDE.md",
    ".claude/CLAUDE.base.md",
    ".claude/rules/memory.md",
    ".claude/rules/session.md",
    ".claude/rules/privacy.md",
    ".claude/rules/subagents.md",
    ".claude/rules/codex-routing.md",
    ".claude/rules-life/codex-routing.md",
    "tasks/lessons.md",
]

TOLERANCE = 200
BASELINE_REL = Path(".claude") / "state" / ".steering-budget-baseline.json"


def default_root() -> Path:
    return Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[1])


class UnstableMeasurement(RuntimeError):
    """The steering set changed underneath the reader. Callers must fail CLOSED, not guess.

    Raised rather than returned so the promote gate's existing `except Exception -> promote_skipped
    ("steering budget unverifiable - failing closed")` handles it with no new branch. An unstable
    measurement is the same class of thing as an absent baseline: the limit is UNKNOWN, and unknown
    means stop.
    """


def _measure_once(root: Path):
    rows, total = [], 0
    for rel in ALWAYS_LOADED:
        p = root / rel
        n = len(p.read_text(errors="ignore")) // 4 if p.exists() else 0
        rows.append((rel, n))
        total += n
    return rows, total


def measure(root: Path | None = None, _attempts: int = 3):
    """Return (rows, total) over ALWAYS_LOADED, or raise UnstableMeasurement.

    READS TWICE AND REQUIRES AGREEMENT. Two files in this set are live — `CLAUDE.md` is written by
    the directive promoter, `tasks/lessons.md` by the lessons loop — and the rules files are
    rewritten in place by a baseline pull. A read concurrent with an in-place rewrite can return a
    length matching NEITHER the before nor the after state.

    That is not hypothetical. On 2026-08-20 the same function against the same seat returned 10,552
    then 10,536 tokens minutes apart with no committed change; `.claude/rules/privacy.md` had been
    rewritten in place (mtime moved, content byte-identical to HEAD) inside that window. core-business
    ran three consecutive measurements identical to the token, establishing the variance was specific
    to the seat with the live session rather than noise in the method.

    16 tokens did not change any verdict — the gate's threshold is 80 and every seat was far from it.
    It is guarded anyway because the gate is a boundary test: the one place a transient error matters
    is exactly at the boundary, and a promote decision that depends on which microsecond it read
    CLAUDE.md is not a decision.

    Retries rather than raising on first disagreement, so an ordinary race costs a re-read instead of
    a spurious block.
    """
    root = Path(root) if root is not None else default_root()
    prev = None
    for _ in range(max(2, _attempts)):
        rows, total = _measure_once(root)
        if prev is not None and total == prev[1]:
            return rows, total
        prev = (rows, total)
    raise UnstableMeasurement(
        f"steering set under {root} did not settle across {max(2, _attempts)} reads "
        f"(last total {prev[1]}) - a file is being written; refusing to report a number"
    )


def baseline_path(root: Path | None = None) -> Path:
    root = Path(root) if root is not None else default_root()
    return root / BASELINE_REL


def load_baseline(root: Path | None = None):
    """Recorded ceiling for this seat, or None if never measured.

    None means UNKNOWN, never "no limit". Callers gate on that distinction: an unmetered
    directive-writer is least wanted on exactly the fresh seat or fork that has no baseline.
    """
    p = baseline_path(root)
    if not p.is_file():
        return None
    try:
        return int(json.loads(p.read_text())["ceiling"])
    except Exception:
        return None


def headroom(root: Path | None = None):
    """(headroom_tokens, total, ceiling) against this seat's ratchet; None ceiling if unrecorded.

    THE WHOLE POINT: `total` is the same nine-file sum the ratchet was recorded from. Comparing a
    nine-file ceiling to anything narrower is the 2026-08-20 defect.
    """
    ceiling = load_baseline(root)
    _, total = measure(root)
    if ceiling is None:
        return None, total, None
    return ceiling + TOLERANCE - total, total, ceiling
