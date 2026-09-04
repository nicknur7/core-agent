#!/usr/bin/env bash
# hookinvoke.sh — record that a shell hook RAN, regardless of whether it matched.
#
# Shell counterpart of hooklog.invoked(). Deliberately pure bash with no python startup:
# this runs on every hook on every turn, so a ~50ms interpreter launch would be a real tax.
#
# WHY this exists: hooklog.log() is called once per FIRE. That makes a hook which runs
# constantly but never matches look identical to one that is not registered at all — both
# emit nothing. Those need opposite treatment (retune the trigger vs. retire the hook), and
# autonomous maintenance cannot tell them apart without an invocation signal.
#
# Emitted as verdict=invoke so it never pollutes fire counts; readers filter on verdict.
# Fail-open by contract: never returns non-zero, never writes to stdout (stdout belongs to
# the hook protocol), never touches stdin.
#
#   Usage:  "$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" <hook-name> <event> [session]
set -u
[[ -n "${CORE_HOOKLOG_OFF:-}" ]] && exit 0
{
  ROOT="${CORE_INSTANCE:-${CLAUDE_PROJECT_DIR:-}}"
  if [[ -n "$ROOT" && -d "$ROOT/.claude/state" ]]; then
    TS="$(date -u +%Y-%m-%dT%H:%M:%S).000Z"
    printf '%s | hook=%s | event=%s | verdict=invoke | session=%s | excerpt=\n' \
      "$TS" "${1:-unknown}" "${2:-unknown}" "${3:-}" >> "$ROOT/.claude/state/hook-events.log"
  fi
} 2>/dev/null
exit 0

# ── WHO THIS CORE BELONGS TO (2026-08-29) ────────────────────────────────────────────────
# Item 2 of the 2026-05-11 strip protocol: "every user-specific list moves to config or env".
# It never shipped. Measured on a fresh clone of the public baseline: runtime strings told a
# stranger to "ask Nick" — their assistant addressed the wrong person, in its own steering,
# every session. The name is per-Core data: it lives in .claude/identity.json (per_core_keep,
# never synced) and nowhere else. Falls back to "the operator", which is the correct thing to
# say on a fresh fork that has not been personalised yet.
core_user_name() {
  local _root _n
  if [[ -n "${CORE_USER_NAME:-}" ]]; then printf '%s' "$CORE_USER_NAME"; return 0; fi
  _root="${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
  _n="$(python3 "$_root/.claude/hooks/lib/coreuser.py" 2>/dev/null)"
  printf '%s' "${_n:-the operator}"
}
