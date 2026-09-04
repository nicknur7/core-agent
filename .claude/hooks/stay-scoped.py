#!/usr/bin/env python3
"""stay-scoped.py — PreToolUse guardrail on cross-Core (peer-MCP) reads.

Fires the "stay scoped to the current Core" ask (recurring 8x in Nick's corrections) at the moment it
matters: right before a peer-Core MCP read. NON-BLOCKING advisory — it surfaces the scoping check so a
cross-Core read is a deliberate choice, never reflexive fishing. Rate-limited to once per (session, peer)
so genuinely cross-Core tasks aren't nagged on every call. Fail-open: any error → no output, tool proceeds.

This is the RIGHT tier for this ask (context-triggered on the exact action) vs a CLAUDE.md prose line —
which is why it lives here as a hook, not in CLAUDE.md.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # user name from identity.json, never hardcoded
import hashlib
import json
import os
import re
import sys
from pathlib import Path



def _hooklog():
    """hooklog.emit prints the payload AND records tokens_injected. Fail-open to a bare print."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog
        return hooklog
    except Exception:
        return None

def main() -> int:
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("stay-scoped", "PreToolUse")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    tool = data.get("tool_name") or data.get("toolName") or ""
    m = re.match(r"mcp__(peer-[a-z]+)__", tool)
    if not m:
        return 0  # not a peer-Core read
    peer = m.group(1)
    session = str(data.get("session_id", ""))[:120]
    # parents[2], not parents[1]. This file lives at .claude/hooks/, so parents[1] is `.claude`
    # and the state dir resolved to `.claude/.claude/state/` — two marker files were created
    # there and got swept into commits. Production was unaffected because the harness sets
    # CLAUDE_PROJECT_DIR, so it only surfaced when the hook was run by hand; but this file SHIPS
    # via .claude/hooks, so the wrong fallback shipped with it. CORE_INSTANCE added first to match
    # every sibling hook. (Fable, re-review.)
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CORE_INSTANCE")
                or Path(__file__).resolve().parents[2])
    state = root / ".claude" / "state"
    key = hashlib.sha1(f"{session}|{peer}".encode()).hexdigest()[:12]
    marker = state / f".stay-scoped-{key}"
    if marker.exists():
        return 0  # already reminded for this peer this session — don't nag genuine cross-Core work
    try:
        state.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
    except Exception:
        pass
    # PREFIX-ANCHORED, same reason: an MCP server named `x-peer-y` would be mangled.
    dom = peer[len("peer-"):] if peer.startswith("peer-") else peer
    msg = (f"🔒 Cross-Core read ({peer}). 'Stay scoped to the current Core' — proceed ONLY if {_U.possessive()} "
           f"request genuinely needs {dom}-Core data, not reflexive fishing. If this isn't warranted by "
           f"his actual ask, stop and use this Core's own memory/brain instead.")
    # session id from the payload, not "". The emit migration hardcoded str(""), which left every
    # stay-scoped row in the ledger with no session attribution — so its cost could never be
    # divided by sessions. (Fable, blast-radius review.)
    _hl = _hooklog()
    if _hl:
        _hl.emit("stay-scoped", "PreToolUse", msg, session=session)
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
