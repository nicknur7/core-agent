#!/usr/bin/env python3
"""hook_block_miner.py — PHASE H: hook blocks as a first-class failure signal.

THE GAP THIS CLOSES. The learning loop mines `pattern_observations` — corrections Nick TYPES. But
every Stop/PreToolUse gate already records, mechanically, the moments the agent made the exact
mistake a guard exists to catch. Measured 2026-08-05: `hook-events.log` holds **364 blocks**
(pretooluse-guard 144, recall-gate 87, time-claim-gate 46, recall-first-gate 36, say-do-gap 23,
approval-gate 14, state-claim-gate 6) across two weeks, flat with no trend — and **nothing has ever
read them.** So the cleanest failure signal in the system was invisible to the component whose whole
job is learning from failure. `time-claim-gate` blocking 46 times is a stronger recurring-failure
signal than most of the 13 contracts that did get minted.

Nick's framing, adopted as the metric: a block means the agent made the mistake. If learning worked,
blocks trend to zero. That is better than corrections-per-100-prompts — mechanical, per-rule, no LLM
detector, no genre confound, and an unambiguous target.

WHAT THIS DOES **NOT** DO, and the restraint is the design:

  * It NEVER mints a contract. A hook that over-fires needs NARROWING; minting a rule that says
    "stop making this mistake" is shouting louder at a problem the hook already shouts about, and
    it is how the steering surface accretes. Cases route to tuning the HOOK.
  * It EXCLUDES `pretooluse-guard` and `approval-gate` entirely. The first is a security gate whose
    144 blocks are 144 stopped outward actions; the second enforces Nick's authority over the loop
    itself. Tuning either down is not efficiency, it is moving a boundary — and a block count cannot
    tell "the behaviour was learned" from "the boundary moved."
  * It labels a false positive MECHANICALLY or not at all. The blocked agent and the tuning agent are
    the same process at close, so the moment an LLM classifies its own blocks as noise, self-interest
    enters without needing intent (Fable). A block is presumed TRUE unless the required evidence is
    demonstrably present in the turn — checkable, not judged.

  hook_block_miner.py --report          # what the blocks say, no writes
  hook_block_miner.py --cases           # emit tuning cases for hooks over the bar
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTANCE = Path(os.environ.get("CORE_INSTANCE") or HERE.parents[1])
HOOK_EVENTS = INSTANCE / ".claude" / "state" / "hook-events.log"

# Same recurrence bar the friction loop already uses. A second standard is how two halves of one
# loop start disagreeing about what "recurring" means.
MIN_BLOCKS = 5
MIN_SESSIONS = 3
WINDOW_DAYS = 30

# NEVER mined. Not a policy note — an explicit set, checked below.
SECURITY_TIER = {
    "pretooluse-guard",   # 144 blocks = 144 stopped outward actions; coverage is the point
    "approval-gate",      # enforces Nick's authority over the loop; a loop narrowing it edits
                          # its own permission to act
}


def _rows() -> list:
    """Parse hook-events.log. Pipe-delimited `k=v`, UTC timestamps.

    The format matters and cost me a wrong number: rows are `k=v | k=v`, NOT JSON, and the log
    writes TWO rows per block (an excerpt row and a message row). Counting rows instead of distinct
    (hook, timestamp) pairs inflates every figure — I reported "13 blocks this session" when the real
    count was 6. Dedupe here so no caller can repeat it.
    """
    if not HOOK_EVENTS.is_file():
        return []
    out, seen = [], set()
    # READ ACROSS ROTATION, NOT JUST THE LIVE GENERATION (2026-08-13).
    #
    # This read HOOK_EVENTS directly. hook-events.log rotates at 1 MB, and it rotated tonight at
    # 21:28 — measured immediately after:
    #
    #     live file      173 lines   <- everything this miner could see
    #     rotated .1    7013 lines   <- invisible to it
    #     true corpus   7186 lines   ->  2% visible
    #
    # A block-mining organ reading 2% of the blocks does not fail; it returns a small, confident,
    # wrong answer, and its recurrence bar (blocks must repeat N times) is exactly the kind of
    # threshold that silently stops being reachable. This file's own header says the failure it
    # exists to fix is that "nothing has ever read them" — after a rotation it had become nothing
    # reading almost all of them.
    #
    # FOUND BY TWO OF MY OWN CENSUSES DISAGREEING. One run counted 6789 rows and 31 hooks, a later
    # run counted 157 rows and 14 — same command, same file, forty minutes apart. Neither was
    # wrong; the file had rotated between them. A disagreement between two measurements is worth
    # more than either measurement.
    #
    # core_paths.read_rotated is the canonical resolver and its own docstring already says why it
    # is ONE resolver: "four copies of a rotation rule is four places" to fix. Three files carry
    # their own `_rotated_text` anyway — the same shape as the slug helper, and named rather than
    # swept here.
    try:
        import sys as _sys
        from pathlib import Path as _P
        _sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "bin"))
        from core_paths import read_rotated as _read_rotated
        _text = _read_rotated(HOOK_EVENTS)
    except Exception:
        _text = HOOK_EVENTS.read_text(errors="ignore")   # degrade to live-only, never to empty
    for line in _text.splitlines():
        m = dict(re.findall(r"(\w+)=([^|]*)", line))
        hook = (m.get("hook") or "").strip()
        verdict = (m.get("verdict") or "").strip()
        if not hook or verdict != "block":
            continue
        ts = line[:19]
        key = (hook, ts)
        if key in seen:
            continue                      # the excerpt row and the message row are ONE block
        seen.add(key)
        out.append({"hook": hook, "ts": ts, "event": (m.get("event") or "").strip(),
                    "session": (m.get("session") or "").strip(),
                    "excerpt": (m.get("excerpt") or "").strip(),
                    "detail": (m.get("detail") or "").strip()})
    return out


def _recent(rows: list, days: int) -> list:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
    return [r for r in rows if r["ts"] >= cutoff]


def report(days: int = WINDOW_DAYS) -> dict:
    """Blocks per hook, and blocks per session — the metric Nick named."""
    rows = _recent(_rows(), days)
    by_hook = collections.Counter(r["hook"] for r in rows)
    by_day = collections.Counter(r["ts"][:10] for r in rows)
    sessions = collections.defaultdict(set)
    for r in rows:
        # session is often blank in this log; fall back to the day, which is the coarsest honest
        # proxy and is stated as such rather than silently treated as a session id.
        sessions[r["hook"]].add(r["session"] or r["ts"][:10])
    return {
        "window_days": days,
        "total_blocks": len(rows),
        "by_hook": dict(by_hook.most_common()),
        "distinct_sessions_by_hook": {h: len(s) for h, s in sessions.items()},
        "by_day": dict(sorted(by_day.items())),
        "security_tier_excluded": {h: by_hook[h] for h in SECURITY_TIER if h in by_hook},
    }


def cases(days: int = WINDOW_DAYS) -> list:
    """Tuning cases for hooks over the recurrence bar. Every case's action is TUNE_HOOK."""
    rep = report(days)
    out = []
    for hook, n in rep["by_hook"].items():
        if hook in SECURITY_TIER:
            continue
        sess = rep["distinct_sessions_by_hook"].get(hook, 0)
        if n < MIN_BLOCKS or sess < MIN_SESSIONS:
            continue
        out.append({
            "hook": hook,
            "blocks": n,
            "sessions": sess,
            "action": "tune_hook",
            "why": (f"{hook} blocked {n}x across {sess} session(s) in {days}d. A block is a moment "
                    f"the agent made the mistake this gate exists to catch; at this rate the "
                    f"behaviour is not being learned. Tune the GATE's precision — never mint a "
                    f"contract restating it."),
        })
    return sorted(out, key=lambda c: -c["blocks"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--cases", action="store_true")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    a = ap.parse_args()
    if a.cases:
        print(json.dumps(cases(a.days), indent=1))
        return 0
    r = report(a.days)
    print(f"hook blocks — last {r['window_days']}d, {r['total_blocks']} total "
          f"(deduped: this log writes 2 rows per block)")
    for h, n in r["by_hook"].items():
        tag = "  [SECURITY TIER — never mined]" if h in SECURITY_TIER else ""
        print(f"   {n:5}  {h:24} {r['distinct_sessions_by_hook'].get(h,0)} session(s){tag}")
    print("\nblocks per day:")
    for d, n in r["by_day"].items():
        print(f"   {d}  {'#' * min(n, 40)} {n}")
    cs = cases(a.days)
    print(f"\n{len(cs)} hook(s) over the bar ({MIN_BLOCKS} blocks / {MIN_SESSIONS} sessions):")
    for c in cs:
        print(f"   {c['hook']:24} {c['blocks']} blocks / {c['sessions']} sessions -> {c['action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
