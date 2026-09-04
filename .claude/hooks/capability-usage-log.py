#!/usr/bin/env python3
"""capability-usage-log — PostToolUse hook.

WHY:       autonomous maintenance cannot retire a capability it cannot measure. Skills and slash
           commands are invoked through the Skill tool, and nothing was counting those invocations,
           so a generated capability could fire constantly (or never) with no signal either way.
           Proven observable 2026-07-27: planting .recall-required-<session>, invoking
           Skill(recall-similar), and finding the marker cleared confirms PostToolUse receives
           tool_name == "Skill" with the name in tool_input.
CREATED:   2026-07-27.
EVENT:     PostToolUse.
DISPOSITION: tracked in .claude/state/hook-dispositions.json.

Records one line per capability invocation onto the same telemetry bus the hooks use, so hooks,
artifacts and capabilities are all counted on one axis. Read-only, fail-open, never blocks.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

HOOK = "capability-usage-log"
EVENT = "PostToolUse"


def main() -> int:
    if os.environ.get("LEARNED_LAYER") == "0" or os.environ.get("CORE_HOOKS") == "0":
        return 0
    try:
        import hooklog
    except Exception:
        return 0
    try:
        hooklog.invoked(HOOK, EVENT)
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        if (data.get("tool_name") or data.get("toolName")) != "Skill":
            return 0
        tin = data.get("tool_input") or data.get("toolInput") or {}
        if not isinstance(tin, dict):
            return 0
        # the skill/command name; bounded and charset-restricted before it is ever written, so a
        # crafted value cannot smuggle delimiters into the log line
        name = str(tin.get("skill") or tin.get("name") or "").strip().lower()[:64]
        name = "".join(c for c in name if c.isalnum() or c in "-_:")
        if not name:
            return 0
        session = data.get("session_id") or data.get("sessionId") or ""
        # verdict=capability keeps these out of hook fire counts while sharing one bus
        hooklog.log(f"capability:{name}", "Skill", verdict="capability", session=str(session)[:64])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
