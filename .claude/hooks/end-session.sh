#!/usr/bin/env bash
# Queue session-end close flow.
# Usage: ./.claude/hooks/end-session.sh
# Or call from Claude via Bash tool when Nick signals end of session.
# The next Stop hook fire will run the auto-commit and push.
STATE_DIR="${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/.claude/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true
touch "$STATE_DIR/.end-session-requested"
echo "Session-end queued."
