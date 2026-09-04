#!/usr/bin/env python3
"""delegation-gate.py — PreToolUse. Fires when Core is grinding inline instead of delegating.

WHY THIS IS A HOOK AND NOT A RULE (2026-08-31)
==============================================
`.claude/rules/subagents.md` has said "spawn a subagent for independent parallelizable work" since
it was written. It loads every session. It did not work: measured 454 mined observations mention
delegation or model routing, and the operator's summary of the pattern: he has been asking for
this since the beginning and it still does not happen without being asked.

That is precisely the failure this repo's own thesis predicts — prose degrades, hooks do not — and
the honest reading is that a rule I re-read every session and still violate is not a rule, it is a
wish. The measured shape of the violation is specific: delegation happens when work looks RISKY
(blast radius -> Fable/Codex/sentinel) and not when it merely looks LARGE. Large-and-parallel is
exactly the case that should fan out, and it is the case that gets ground out inline instead.

WHAT IT MEASURES
================
Consecutive mechanical tool calls (Bash/Read/Edit/Write/Grep/Glob) since the last delegation. An
Agent/Task/Workflow call resets the counter. Crossing a threshold means: this turn has become a
long serial grind with nothing farmed out, which is the observable signature of the miss.

It cannot know whether the work was PARALLELISABLE — that needs judgment. So it does not block and
does not claim certainty. It surfaces the count and the routing table at the moment the pattern is
visible, which is the most a counter can honestly do. Advisory, fail-open, rate-limited per stride.
"""
import json
import os
import sys
from pathlib import Path

# Tool names that mean "Core is doing the work itself".
MECHANICAL = {"Bash", "Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit", "MultiEdit"}
# Tool names that mean "Core handed work to someone else".
DELEGATING = {"Agent", "Task", "Workflow"}

# First fire at 14 consecutive calls, then every 14 after. 14 is ~2-3 minutes of solid grinding;
# below that a serial run is usually genuine sequencing (read -> edit -> verify) and nagging it
# would train the reader to ignore this hook, which is how the last five retired gates died.
STRIDE = 14


def _emit(msg: str, session: str) -> None:
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
        import hooklog
        hooklog.emit("delegation-gate", "PreToolUse", msg, session=session)
    except Exception:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": msg}}))


def main() -> int:
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
        import hooklog as _hl
        _hl.invoked("delegation-gate", "PreToolUse")
    except Exception:
        pass

    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail open

    tool = data.get("tool_name") or data.get("toolName") or ""
    session = str(data.get("session_id", ""))[:120]

    # parents[2]: this file is at .claude/hooks/, so parents[1] would be `.claude` and the state
    # dir would resolve to `.claude/.claude/state`. Same trap stay-scoped.py documents.
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CORE_INSTANCE")
                or Path(__file__).resolve().parents[2])
    state = root / ".claude" / "state"
    counter = state / f".delegation-run-{session[:16] or 'nosession'}"

    if tool in DELEGATING:
        try:
            counter.unlink()          # delegated — the streak is broken, which is the point
        except Exception:
            pass
        return 0

    if tool not in MECHANICAL:
        return 0

    try:
        n = int(counter.read_text().strip() or "0")
    except Exception:
        n = 0
    n += 1
    try:
        state.mkdir(parents=True, exist_ok=True)
        counter.write_text(str(n))
    except Exception:
        return 0  # cannot track — say nothing rather than fire on bad data

    if n % STRIDE != 0:
        return 0

    msg = (
        f"🔀 DELEGATION CHECK — {n} mechanical tool calls in a row, 0 delegations.\n"
        f"If the remaining work is parallelisable or mechanical, it should not be running here. "
        f"Route it now, do not finish inline and route the next thing:\n"
        f"  • bulk reads / classification / extraction → pinned Haiku subagent\n"
        f"  • 'do X across N files or N Cores', execution batches → pinned Sonnet, one per target, in parallel\n"
        f"  • adversarial review before a blast-radius change → Codex (read-only) or Fable\n"
        f"  • hardest architecture call → Fable, pinned up; the session stays where it is\n"
        f"NEVER let a fan-out inherit the session tier — pin every agent in a >=3 fan-out explicitly.\n"
        f"If this genuinely IS serial work that cannot be split, continue; this is advisory. "
        f"But the recurring miss is delegating only when work looks RISKY and never when it is "
        f"merely LARGE, and {n} calls deep is what that looks like from outside."
    )
    _emit(msg, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
