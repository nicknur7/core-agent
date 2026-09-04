#!/usr/bin/env bash
# SessionEnd defensive auto-save — fires when the session ends without an
# explicit /close-core (/exit, terminal close, /clear). Thin shim over the ONE
# lifecycle controller's defensive close. (Phase 0 refactor 2026-05-28,
# spec-brain-unfreeze: was a duplicate of stop-hook.sh's commit engine.)
#
# Two-tier close model (2026-07-24 synchronous-close redesign):
#   /close-core → in-session sync (extract→merge→embed, foreground) → stop-hook.sh → `close full`
#   walk-away   → SessionEnd → this → `close defensive` (deterministic save/capture ONLY —
#                 no extraction, no embed; the debt is drained by the next /close-core)
#   /close-core THEN exit/clear → this still fires, but lifecycle_close sees
#                 .full-close-this-session and short-circuits to a trailing no-op
#                 (preserve-only commit + capture) — no second close, no brain re-fire.
set -uo pipefail
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" defensive-save SessionEnd "" 2>/dev/null || true

REPO=$(git rev-parse --show-toplevel 2>/dev/null || echo "${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}")
# Session id from THIS hook's stdin payload (2026-07-25). This is the load-bearing half of the
# marker fix: on `/close-core` then `/clear`, THIS hook fires for the OUTGOING session while the
# incoming session's SessionStart has already run. Reading the id from our own payload is what
# lets the trailing-no-op branch recognise "my session already did a full close" instead of
# re-running every generator and downgrading the current-state stamp.
SID=""
if [ ! -t 0 ]; then
  SID=$(cat 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id') or d.get('sessionId') or '')" 2>/dev/null || echo "")
fi
bash "$REPO/.claude/hooks/session-lifecycle.sh" close defensive "$SID"
exit 0
