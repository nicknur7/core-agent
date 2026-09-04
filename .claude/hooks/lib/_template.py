#!/usr/bin/env python3
"""<hook-name> — <Event> hook.

WHY:       <the correction / incident / need that spawned this hook — 1-2 sentences>.
CREATED:   <YYYY-MM-DD>.
EVENT:     <SessionStart | UserPromptSubmit | PreToolUse | PostToolUse | Stop | SessionEnd>.
DISPOSITION: tracked in .claude/state/hook-dispositions.json (keep|tune|fix|merge|remove).

Contract (every Core hook follows this):
- Reads the event JSON from stdin (Claude Code hook protocol).
- FAIL-OPEN: any error -> exit 0. A hook must never break a turn.
- KILL-SWITCH: env LEARNED_LAYER=0 disables (also CORE_HOOKS=0 honored).
- TRACKABLE: calls hooklog.log() once per fire so it appears in /system (System Health).
- Exit codes: 0 = pass/allow; 2 = block (PreToolUse + Stop only) with a stderr reason.

To ship a NEW hook, see lib/NEW-HOOK.md — the 4-step checklist (file -> settings.json
-> dispositions.json -> test). A hook that isn't in dispositions.json shows as
"untracked" in System Health, which is the prompt to add it.
"""
import json
import os
import sys
from pathlib import Path

# make the shared telemetry helper importable from the hooks/ root
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hooklog  # noqa: E402

# --- identity (keep in sync with the filename + dispositions.json key) --------
HOOK = "REPLACE-ME"            # base name == this file's name without .py
EVENT = "UserPromptSubmit"     # the event this hook is registered on


def main() -> int:
    if os.environ.get("LEARNED_LAYER") == "0" or os.environ.get("CORE_HOOKS") == "0":
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    session = data.get("session_id") or data.get("sessionId") or ""

    # ---- 1) DETECT ----------------------------------------------------------
    # UserPromptSubmit: prompt = data.get("prompt")
    # PreToolUse:       tool = data.get("tool_name"); tin = data.get("tool_input")
    # Stop:             inspect last assistant turn / transcript
    fired = False
    trigger = ""   # short label of WHAT matched — shown in System Health telemetry
    # ... detection logic here; set fired=True and trigger="<label>" on a match ...

    if not fired:
        return 0  # silent pass — optionally: hooklog.log(HOOK, EVENT, "pass", "", session)

    # ---- 2) ACT + RECORD ----------------------------------------------------
    # A) inject context (SessionStart / UserPromptSubmit / non-blocking PreToolUse):
    hooklog.log(HOOK, EVENT, verdict="inject", trigger=trigger, session=session)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": "..."}}))
    return 0

    # B) BLOCK (PreToolUse / Stop only) — uncomment instead of the inject above:
    # hooklog.log(HOOK, EVENT, verdict="block", trigger=trigger, session=session)
    # sys.stderr.write("<HOOK> — <why this is blocked + what to do instead>\n")
    # return 2


if __name__ == "__main__":
    sys.exit(main())
