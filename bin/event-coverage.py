#!/usr/bin/env python3
"""Which hook events does this harness actually dispatch, and what do they carry?

Reads .claude/state/event-probe.log (written by .claude/hooks/event-probe.py) and reports, per
event: whether it ever fired, how often, over how many sessions, and which payload keys it
carries. The payload keys matter as much as the fire count — they decide what a gate ported to
that event could assert on.

The operator, 2026-07-30: there are 30 different aspects of a turn, so make sure everything is
being used where it needs to be. This answers the first half of that with evidence: which of the ~30 are
real HERE. What to do with the ones that fire is a judgement call and stays a judgement call.

  python3 bin/event-coverage.py
"""
import collections
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[1])
LOG = ROOT / ".claude" / "state" / "event-probe.log"

# Registered as of 2026-07-30, for the contrast column.
IN_USE = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "Stop", "SubagentStop", "SessionEnd"}

# What each probed event would be FOR, if it fires. Written before the data arrives on purpose:
# deciding the use after seeing the counts is how you end up justifying whatever happened to be
# frequent. Sources: tasks/research/hook-event-coverage-2026-07-30.md.
PURPOSE = {
    "PostToolBatch": "fires after a tool batch resolves, BEFORE the next model call — the only "
                     "place a gate can assert on what the turn DID and still reach composition",
    "PostToolUseFailure": "tool failures are unrecorded today; a failing tool is a correction "
                          "waiting to happen",
    "PostCompact": "compaction is unrecorded and is a prime suspect for mid-session drift",
    "PreCompact": "allowed by the registry allowlist already, never used",
    "InstructionsLoaded": "fires when CLAUDE.md + rules load — the steering surface Phase 2 has "
                          "to measure and currently cannot see load",
    "UserPromptExpansion": "slash-command expansion before the model sees it",
    "SubagentStart": "we record SubagentStop only, so spawn/finish cannot be paired and subagent "
                     "cost cannot be attributed",
    "StopFailure": "turns ending in an API error are invisible",
    "Notification": "allowed already, never used",
    "MessageDisplay": "TESTED 2026-08-06, not assumed: it SEES the full reply (64-415 char chunks, paragraph-wise, with index + final) and CANNOT stop it — a probe returning exit 2 on a sentinel chunk was ignored and the sentinel reached the terminal. So there is NO surface that intercepts model text before it is pasted; text-inspecting gates cannot be relocated pre-reply, only replaced. Its real value is FREE MEASUREMENT: it can record a violation with zero interruption, which is the number the SI loop could not otherwise produce. Was: cannot change what the model sees",
}


def main() -> int:
    if not LOG.exists():
        print(f"no probe log yet at {LOG}")
        print("The probe records events as they fire. Run a normal session first, then re-run this.")
        return 0
    rows = [l.rstrip("\n").split("\t") for l in LOG.read_text(errors="ignore").splitlines() if l.strip()]
    if not rows:
        print("probe log is empty — no probed event has dispatched yet.")
        return 0

    n = collections.Counter()
    sess = collections.defaultdict(set)
    keys = collections.defaultdict(collections.Counter)
    for r in rows:
        ev = r[1] if len(r) > 1 else "UNKNOWN"
        n[ev] += 1
        if len(r) > 2 and r[2]:
            sess[ev].add(r[2])
        if len(r) > 3 and r[3]:
            keys[ev][r[3]] += 1

    print(f"event coverage — {len(rows)} probe rows, {len(n)} distinct events\n")
    print(f"{'event':24} {'fires':>6} {'sessions':>9}  payload keys")
    print("-" * 100)
    for ev, c in n.most_common():
        mark = "*" if ev in IN_USE else " "
        top = keys[ev].most_common(1)
        print(f"{mark}{ev:23} {c:6} {len(sess[ev]):9}  {top[0][0][:56] if top else ''}")

    silent = [e for e in PURPOSE if e not in n]
    if silent:
        print(f"\nPROBED BUT NEVER DISPATCHED ({len(silent)}) — do not port anything to these:")
        for e in sorted(silent):
            print(f"  {e:24} would have been: {PURPOSE[e][:70]}")

    live = [e for e in n if e in PURPOSE]
    if live:
        print(f"\nDISPATCHES AND IS UNUSED ({len(live)}) — candidates, in evidence order:")
        for e in sorted(live, key=lambda x: -n[x]):
            print(f"  {e:24} {n[e]:5} fires · {PURPOSE[e][:64]}")
    print("\n* = already registered by this Core")
    return 0


if __name__ == "__main__":
    # SEAT-LOCKED: REFUSE AN ARGUMENT RATHER THAN DISCARD IT. core-business, bus #971. It aimed a
    # seat-locked scanner at life's tree; Python accepted the stray argv silently, the tool scanned
    # its OWN seat, and the two runs "converged" — which reads as a regressed fix rather than as a
    # tool that never moved. A WRONG NUMBER, not no number. That is the whole defect: not the
    # seat-locking, which is fine, but claiming to honour a tree it discards.
    if len(sys.argv) > 1:
        sys.exit("REFUSING: %s is seat-locked and cannot scan %r.\n"
                 "It resolves its own Core from __file__. To measure another Core, run it from "
                 "that Core's tree or use seat_matrix staging." % (Path(__file__).name, sys.argv[1]))
    sys.exit(main())
