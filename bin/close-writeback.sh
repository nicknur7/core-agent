#!/usr/bin/env bash
# close-writeback.sh — the deterministic half of the /close-core write-back.
#
# WHY: at close, the session-file close block and the current-state.md "Last
# updated:" stamp are DETERMINISTIC formatting the model used to hand-write in
# expensive prose (Opus, every close). This script does that mechanical surgery
# so the model supplies only the 1-2 sentence SUMMARY, not the whole edit dance.
# (Phase 1 of the Core OS unification plan, tasks/research/core-os-unification-plan-2026-07-11.md.)
#
# DELIBERATELY DOES NOT touch the free-form narrative of current-state.md
# (Latest/Prior demotion, Carried curation, pruning). That is judgment — a script
# pruning free-form markdown loses real carried context. Those stay with the model.
#
# Usage:
#   close-writeback.sh --summary "<one-liner>" --start "HH:MM" --end "HH:MM" \
#                      --wall "Xh Ym" --tz PDT --model "Opus 4.8" [--dry-run]
#
# Effects (idempotent per close-date):
#   1. Appends a standard close block to sessions/YYYY-MM-DD.md (skips if already present).
#   2. Stamps current-state.md line-1 "Last updated: <END> <TZ> (explicit close)".
#   3. Logs a close-cost line to .claude/state/close-cost.jsonl.
#      NOT read by anything as of 2026-08-13. This line said "for /core-si to trend";
#      /core-si does not mention cost at all, and 15 records have accrued since 07-12 with
#      no consumer. Naming a consumer that does not exist is worse than naming none — it
#      tells the next reader the data is already in use.
set -uo pipefail

REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
# Source the path registry so stable paths come from constants, not hardcoded
# strings (lint-code-paths gate). core-paths.sh self-derives CORE_INSTANCE via
# git and `return`s (not exit) on error, so sourcing here is safe at close time.
# shellcheck source=bin/core-paths.sh
source "$REPO/bin/core-paths.sh"
TODAY=$(date +%Y-%m-%d)

SUMMARY="" START="" END="" WALL="" TZ="PDT" MODEL="" DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --summary) SUMMARY="$2"; shift 2;;
    --start)   START="$2";   shift 2;;
    --end)     END="$2";     shift 2;;
    --wall)    WALL="$2";    shift 2;;
    --tz)      TZ="$2";      shift 2;;
    --model)   MODEL="$2";   shift 2;;
    --dry-run) DRY=1;        shift;;
    *) echo "close-writeback: unknown arg '$1'" >&2; exit 2;;
  esac
done

[[ -z "$SUMMARY" ]] && { echo "close-writeback: --summary required" >&2; exit 2; }

# Normalize START/END (2026-07-27, from core-business's mangled-header report):
# compute-session-duration.sh emits FULL values ("2026-07-26 18:41 PDT") while some
# callers pass bare times ("18:41"). The old template blindly prepended ${TODAY} and
# appended ${TZ}, producing "2026-07-27 2026-07-26 18:41 PDT → … PDT PDT". Parse each
# value into (date, time, tz) and compose exactly once. Cross-midnight closes keep
# both dates; same-day shows the date once.
_norm() {  # $1=value  → echoes "date|time|tz" (date/tz may be empty)
  local v="$1" d="" t="" z=""
  if [[ "$v" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})[[:space:]]+(.*)$ ]]; then
    d="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
  fi
  if [[ "$v" =~ ^(.*[0-9])[[:space:]]+([A-Z]{2,4})$ ]]; then
    t="${BASH_REMATCH[1]}"; z="${BASH_REMATCH[2]}"
  else
    t="$v"
  fi
  printf '%s|%s|%s' "$d" "$t" "$z"
}
IFS='|' read -r START_D START_T START_Z <<<"$(_norm "${START:-?}")"
IFS='|' read -r END_D END_T END_Z <<<"$(_norm "${END:-?}")"
TZ_OUT="${END_Z:-${START_Z:-$TZ}}"
HDR_START="${START_D:-$TODAY} ${START_T}"
if [[ -n "$END_D" && "$END_D" != "${START_D:-$TODAY}" ]]; then
  HDR_END="${END_D} ${END_T}"      # cross-midnight: keep the end date
else
  HDR_END="${END_T}"
fi

SESSION_FILE="$REPO/sessions/${TODAY}.md"
STATE_FILE="$CORE_MEM_CURRENT_STATE"
COST_LOG="$REPO/.claude/state/close-cost.jsonl"

CLOSE_BLOCK="
---
**${HDR_START} → ${HDR_END} ${TZ_OUT} (${MODEL:-Claude}${WALL:+, ${WALL} wall})** — session close.
${SUMMARY}"

# 1 · session-file close block (skip if today's file already has a close marker)
do_session() {
  if [[ -f "$SESSION_FILE" ]] && grep -q "session close.$" "$SESSION_FILE" 2>/dev/null; then
    echo "  [skip] session close block already present in ${SESSION_FILE##*/}"
    return
  fi
  if [[ $DRY -eq 1 ]]; then
    echo "  [dry] would append close block to ${SESSION_FILE##*/}:"; echo "$CLOSE_BLOCK" | sed 's/^/      /'
  else
    printf '%s\n' "$CLOSE_BLOCK" >> "$SESSION_FILE"
    echo "  [ok] appended close block to ${SESSION_FILE##*/}"
  fi
}

# 2 · current-state.md line-1 stamp (deterministic; narrative stays with the model)
do_state_stamp() {
  # 2026-07-29 FIX: this used ${END_T} alone, which is a TIME with no date, so a real close wrote
  # `Last updated: 20:16 PDT (explicit close)`. bin/core-doctor.sh parses a \d{4}-\d{2}-\d{2} out
  # of this line to compute staleness, so a date-less stamp silently degrades the check to "no
  # stamp found" — the staleness warning stops working and reports nothing rather than erroring.
  #
  # Not theoretical: core-business's current-state.md line 1 literally read
  # `Last updated: 20:16 PDT (explicit close)`, and when it ran a staleness scan its regex pulled
  # "20" out of that stamp. It logged that as a parse artifact rather than recognising the bug.
  # Flagged in the 2026-07-28 close as a one-line fix for the next session; this is that fix.
  #
  # END_D is already computed by _norm above and was simply never used here. Falls back through
  # START_D then TODAY so the stamp always carries a full date even on a malformed --end.
  local _stamp_date="${END_D:-${START_D:-$TODAY}}"
  local newline="Last updated: ${_stamp_date} ${END_T:-$(date +%H:%M)} ${TZ_OUT} (explicit close)"
  if [[ ! -f "$STATE_FILE" ]]; then echo "  [warn] $STATE_FILE missing; skipping stamp"; return; fi
  if [[ $DRY -eq 1 ]]; then
    echo "  [dry] would set current-state.md line 1 → '$newline'"
  else
    # replace only line 1 (the Last updated stamp); leave everything else untouched
    local tmp; tmp=$(mktemp)
    { printf '%s\n' "$newline"; tail -n +2 "$STATE_FILE"; } > "$tmp" && mv "$tmp" "$STATE_FILE"
    echo "  [ok] stamped current-state.md line 1"
  fi
}

# 3 · close-cost line for /core-si to trend (measure, don't guess)
do_cost_log() {
  local jsonl="{\"date\":\"${TODAY}\",\"end\":\"${END}\",\"wall\":\"${WALL}\",\"model\":\"${MODEL}\",\"mode\":\"explicit\"}"
  if [[ $DRY -eq 1 ]]; then
    echo "  [dry] would append to close-cost.jsonl: $jsonl"
  else
    mkdir -p "$(dirname "$COST_LOG")"
    printf '%s\n' "$jsonl" >> "$COST_LOG"
    echo "  [ok] logged close cost"
  fi
}

echo "close-writeback ($([[ $DRY -eq 1 ]] && echo DRY-RUN || echo LIVE)) — ${TODAY}"
do_session
do_state_stamp
do_cost_log
