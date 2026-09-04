#!/usr/bin/env bash
# Stop hook — fires on EVERY assistant turn. Acts ONLY when /close-core queued a
# close (the .end-session-requested sentinel is present); otherwise instant
# no-op. The actual close work lives in the ONE lifecycle controller — this is a
# thin shim. (Phase 0 refactor 2026-05-28, spec-brain-unfreeze: stop-hook.sh and
# defensive-save.sh were duplicate commit engines; both now call close.)
set -uo pipefail
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" stop-hook Stop "" 2>/dev/null || true

REPO=$(git rev-parse --show-toplevel 2>/dev/null || echo "${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}")
# shellcheck source=bin/core-paths.sh
source "$REPO/bin/core-paths.sh"
[[ -f "$CORE_END_SESSION_SENTINEL" ]] || exit 0
# Session id from THIS hook's stdin payload — scopes the close markers to the session that owns
# them (2026-07-25). Must be read here, not from a shared file: on `/close-core` then `/clear` the
# incoming session's SessionStart runs before the outgoing SessionEnd, so a shared file would
# already hold the wrong id. Empty on parse failure → controller falls back to the legacy name.
SID=""
if [ ! -t 0 ]; then
  SID=$(cat 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id') or d.get('sessionId') or '')" 2>/dev/null || echo "")
fi
bash "$REPO/.claude/hooks/session-lifecycle.sh" close full "$SID"
# SL1 fix (2026-06-23): consume the close sentinel ONLY if the close committed.
# A blocked close (safety-scan fail) leaves $CORE_LAST_SAVE_BLOCKED on disk (it is
# removed on a successful commit) — in that case keep the sentinel so the next Stop
# turn retries, instead of deleting it up-front and stranding the close (the 6/19 bug).
if [[ ! -f "$CORE_LAST_SAVE_BLOCKED" ]]; then
  rm -f "$CORE_END_SESSION_SENTINEL"
fi
exit 0
