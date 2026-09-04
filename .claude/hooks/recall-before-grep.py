#!/usr/bin/env python3
"""PreToolUse — makes brain recall the DEFAULT for reconstructing past context.

The failure it fixes (2026-07-11, Core OS Phase 2, from a live core-ops case):
a Core reconstructs "what did we do / decide" by GREPPING session .md files
directly instead of querying the brain — the recurring correction Nick has given
("can you use claude brain to retrieve this info instead of whatever you did").
The brain is ground truth (compiled hubs + cross-session RRF); a raw session grep
misses all of that. Documented goal state: **recall primary, grep as fallback.**

Enforcement: a session-history grep is REFUSED until at least one genuine brain
query has run this session (breadcrumb `.brain-queried-<session>`, dropped by
recall-satisfied.py). After that first recall, grep unlocks as the fallback — so
a thin-corpus Core (like a fresh ops) still recalls FIRST, sees the thin result,
THEN greps. Session-scoped, so a normal session (recall happens early) never nags.

Only fires on cross-session RECONSTRUCTION (a Grep/Glob or Bash grep over
`sessions/`), never on reading a single named session file (Read is untouched).

Fail-open: any error → exit 0.
"""
import json
import os
import re
import sys
from pathlib import Path

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STATE_DIR = INSTANCE / ".claude" / "state"

# grep/rg must be at a COMMAND position (start, or after a shell separator) — not
# merely mentioned inside a quoted argument like a `git commit -m "...grep..."` message.
_BASH_GREP = re.compile(r"(^|[|;&]|&&|\|\|)\s*(grep|egrep|fgrep|rg|ag)\b", re.I)


def targets_session_history(tool_name: str, ti: dict) -> bool:
    """True only for a search ACROSS session history (reconstruction), not a
    single-file read."""
    if tool_name in ("Grep", "Glob"):
        # Check path/glob fields only — NOT the search pattern (a pattern that
        # happens to contain "sessions" shouldn't trigger). Matches a sessions
        # directory named bare ("sessions"), nested ("a/sessions"), or globbed.
        for v in (str(ti.get("path", "")), str(ti.get("glob", ""))):
            if "sessions/" in v or re.search(r"(^|/)sessions(/|$)", v):
                return True
        return False
    if tool_name in ("Bash", "BashOutput"):
        cmd = str(ti.get("command", ""))
        return bool(_BASH_GREP.search(cmd)) and "sessions/" in cmd
    return False


def detect(text):
    """A session-transcript grep — the shape that should have been a brain query.

    Takes the raw command string. Extracted 2026-07-28 for measurement and intent-grading;
    this gate reads tool input rather than prose, so its corpus is commands, not replies.
    """
    text = text or ""
    return [text[:120]] if (_BASH_GREP.search(text) and "sessions/" in text) else []


def main() -> int:
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("recall-before-grep", "PreToolUse")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    session_id = data.get("session_id") or data.get("sessionId") or ""
    if not session_id:
        return 0
    tool_name = data.get("tool_name") or data.get("toolName") or ""
    ti = data.get("tool_input") or data.get("toolInput") or {}
    if not isinstance(ti, dict):
        ti = {}
    if not targets_session_history(tool_name, ti):
        return 0
    # Already recalled this session → grep is the sanctioned fallback now.
    if (STATE_DIR / f".brain-queried-{session_id}").exists():
        return 0
    sys.stderr.write(
        "RECALL-BEFORE-GREP — you're about to grep session history to reconstruct "
        "past context, but no brain query has run this session. The brain is ground "
        "truth (compiled hubs + cross-session RRF); a raw session-file grep misses it. "
        "Recall FIRST: mcp__core-brain__recall_similar (or the claude-brain skill). If "
        "recall returns nothing useful (thin corpus), re-run this grep and it will pass "
        "— recall is the default, grep is the fallback.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
