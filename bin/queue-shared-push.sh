#!/usr/bin/env bash
# queue-shared-push.sh — Inspect the most recent commit and append any files
# that belong to the multi-Core shared subset (defined in bin/sync-manifest.json)
# to .claude/state/.pending-push-marker. The next SessionStart's Check (m)
# surfaces the marker so Claude can run sync-to-baseline.sh (Sentinel-code
# gates).
#
# Called by:
#   - .claude/hooks/stop-hook.sh (after /close-core commit)
#   - .claude/hooks/defensive-save.sh (after /exit / terminal-close commit)
#
# Extracted from the inline block previously living only in stop-hook.sh
# (lines 192-237) so defensive-save also queues shared changes — fix for
# B4 in tasks/research/session-close-start-brain-audit-2026-05-22.md.
#
# Idempotent: appending to the marker is safe (sort -u dedups).
# Silent: writes only to the marker file; stdout/stderr discarded so the
# parent hook's JSON output isn't polluted.
set -uo pipefail

REPO=$(git rev-parse --show-toplevel 2>/dev/null || echo "${CORE_INSTANCE:-}")
[[ -z "$REPO" || ! -d "$REPO/.git" ]] && exit 0

SYNC_MANIFEST="$REPO/bin/sync-manifest.json"
STATE_DIR="$REPO/.claude/state"
PUSH_MARKER="$STATE_DIR/.pending-push-marker"

[[ ! -f "$SYNC_MANIFEST" ]] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

# Single-writer policy (2026-06-02): only the designated baseline_writer Core
# queues shared-code pushes. On every other Core this is a no-op — they're
# pull-only, so surfacing a push marker would only produce a push attempt that
# sync-to-baseline.sh now refuses anyway. Skipping here keeps it noise-free.
# Absent/null baseline_writer (fresh template) → policy disabled, queue normally.
BASELINE_WRITER=$(jq -r '.baseline_writer // empty' "$SYNC_MANIFEST" 2>/dev/null)
CORE_NAME=$(basename "$REPO")
[[ -n "$BASELINE_WRITER" && "$CORE_NAME" != "$BASELINE_WRITER" ]] && exit 0

COMMITTED_FILES=$(git -C "$REPO" diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null || true)
[[ -z "$COMMITTED_FILES" ]] && exit 0

SHARED_DIRS=$(jq -r '.shared.dirs[]' "$SYNC_MANIFEST" 2>/dev/null)
SHARED_FILES=$(jq -r '.shared.files[]' "$SYNC_MANIFEST" 2>/dev/null)
PER_CORE_KEEP=$(jq -r '.per_core_keep[]' "$SYNC_MANIFEST" 2>/dev/null)

# Filter function: check if path $1 is per_core_keep OR not in shared subset.
# Returns 0 (keep-out-of-marker) if path should NOT be in the marker, else 1.
should_keep() {
  local f="$1"
  while IFS= read -r k; do
    [[ -z "$k" ]] && continue
    kp="${k%/\*\*}"
    if [[ "$f" == "$k" || "$f" == "$kp"/* ]]; then return 0; fi
  done <<< "$PER_CORE_KEEP"
  return 1
}
is_shared() {
  local f="$1"
  while IFS= read -r d; do
    [[ -z "$d" ]] && continue
    if [[ "$f" == "$d"/* ]]; then return 0; fi
  done <<< "$SHARED_DIRS"
  while IFS= read -r sf; do
    [[ -z "$sf" ]] && continue
    if [[ "$f" == "$sf" ]]; then return 0; fi
  done <<< "$SHARED_FILES"
  return 1
}

# Combine (existing marker + newly committed files), filter EACH against
# per_core_keep + shared subset, and rewrite the marker. This ensures stale
# entries (added before per_core_keep was extended, or by an older buggy
# revision) get evicted on every run — fix for 2026-05-22 Sentinel-code
# flag (a-per-seat/CLAUDE.md was lingering in the marker despite being
# in per_core_keep).
SHARED_HITS=""
EXISTING_MARKER=""
[[ -f "$PUSH_MARKER" ]] && EXISTING_MARKER=$(cat "$PUSH_MARKER")
ALL_CANDIDATES=$(printf '%s\n%s' "$EXISTING_MARKER" "$COMMITTED_FILES" | grep -v '^$' | sort -u)

while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  should_keep "$f" && continue       # per_core_keep -> drop
  is_shared "$f" || continue          # not in shared subset -> drop
  SHARED_HITS="${SHARED_HITS}${f}"$'\n'
done <<< "$ALL_CANDIDATES"

if [[ -n "$SHARED_HITS" ]]; then
  mkdir -p "$STATE_DIR" 2>/dev/null
  printf '%s' "$SHARED_HITS" | grep -v '^$' | sort -u > "${PUSH_MARKER}.tmp" \
    && mv "${PUSH_MARKER}.tmp" "$PUSH_MARKER"
else
  rm -f "$PUSH_MARKER" 2>/dev/null
fi
exit 0
