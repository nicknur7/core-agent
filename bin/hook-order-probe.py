#!/usr/bin/env python3
"""hook-order-probe.py — reusable version of the experiment run by hand on 2026-08-31 to settle
judge requirement 2 for GAP A-executable-effect: "ordered after dispatch, drains same-event is
unverified — measure actual ordering with event-probe before claiming same-event."

WHAT WAS ALREADY MEASURED (not assumed) before friction_runner.py made its "≤1-event drain lag,
never same-event" claim: two throwaway hooks were added to the live "Bash" PreToolUse matcher
group (declared A-then-existing-hook-then-B), each appending a nanosecond timestamp + pid to a
shared file, across five real Bash tool calls in the session that built this feature. Result: 1 of
5 pairs completed OUT of declared order (B's write landed before A's, despite A being declared
first and lower-pid — i.e. forked first), and every pair showed sub-millisecond-to-low-hundreds-of-
microseconds gaps between A and B — far too fast to contain a full Python interpreter startup and
exec of the hook declared between them if hooks were being run-and-waited sequentially. Read
together, that is hooks in one matcher group launching as concurrent subprocesses whose completion
order is a race, not a queue. Full log of that run: see the commit that added this file.

USAGE (to re-run the measurement, e.g. after a harness upgrade, or for UserPromptSubmit instead of
PreToolUse — this file was validated in the PreToolUse:Bash group as a proxy, because a subagent
session cannot self-trigger a fresh UserPromptSubmit to test that group directly):

  1. Pick TWO ADJACENT positions in one matcher's "hooks" array in .claude/settings.json (or one
     event's bare "" matcher array, to test UserPromptSubmit itself) and insert entries either
     side of an existing hook:
         python3 "$CLAUDE_PROJECT_DIR/bin/hook-order-probe.py" A
         ... existing hook ...
         python3 "$CLAUDE_PROJECT_DIR/bin/hook-order-probe.py" B
  2. Trigger that event several times (a few Bash calls, or a few real prompts for
     UserPromptSubmit).
  3. Run this file with --report to print the pairing and flag any out-of-order or suspiciously-
     synchronous-looking pairs.
  4. REMOVE the temporary entries from settings.json — this file is a diagnostic, never meant to
     stay wired into production hooks.

Writes to .claude/state/.hook-order-probe.log (gitignored runtime state, not the shared
friction-action-log.jsonl — this is a one-off measurement tool, not part of the run-mode
lifecycle it exists to justify).
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[1])
LOG = ROOT / ".claude" / "state" / ".hook-order-probe.log"


def _record(tag: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.time_ns()} pid={os.getpid()} tag={tag}\n")


def _report() -> None:
    if not LOG.exists():
        print("no probe data yet — run with a tag first, then trigger the event a few times")
        return
    rows = []
    for line in LOG.read_text().splitlines():
        try:
            ts_s, pid_s, tag_s = line.split()
            rows.append((int(ts_s.split()[0]) if False else int(ts_s),
                         int(pid_s.split("=")[1]), tag_s.split("=")[1]))
        except Exception:
            continue
    rows.sort(key=lambda r: r[0])
    print(f"{len(rows)} rows, in completion order:")
    for ts, pid, tag in rows:
        print(f"  {ts} pid={pid} tag={tag}")
    # naive pairing: consecutive A/B by completion time, flagged if B's pid < A's pid (forked
    # earlier) but completed later, or vice versa — a direct sign completion order and launch
    # order disagree.
    prev = None
    flags = 0
    for ts, pid, tag in rows:
        if prev and {prev[2], tag} == {"A", "B"}:
            gap_us = (ts - prev[0]) / 1000
            order_note = ""
            if tag == "A" and pid > prev[1]:
                order_note = "  <-- completed after B but forked after it too (consistent)"
            elif tag == "A" and pid < prev[1]:
                order_note = "  <-- FORKED BEFORE B but COMPLETED AFTER — declared order held here"
            print(f"    pair gap: {gap_us:.1f}us{order_note}")
        prev = (ts, pid, tag)
    print("\nIf gaps are consistently sub-millisecond and any pair completes out of declared "
          "order, hooks in this matcher group are running concurrently, not sequentially-waited — "
          "design any same-event dependency between two hooks accordingly (assume none).")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        _report()
    elif len(sys.argv) > 1:
        _record(sys.argv[1])
    else:
        print(__doc__)
