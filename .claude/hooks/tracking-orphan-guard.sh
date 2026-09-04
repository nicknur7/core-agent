#!/usr/bin/env bash
# tracking-orphan-guard.sh — PreToolUse hook
#
# Blocks creation of NEW tracking-shape files (backlog, inventory, queue,
# pending, status, dashboard, registry, tracker, kanban, todo, board) under
# the instance's tasks/ or memory/ unless the filename is already referenced
# by an existing keeper (an agent CLAUDE.md, a hook script, a rule doc, or a
# slash-command).
#
# Why: cascade-fix-2026-05-16 traced ~half our drift bugs to "tracking surface
# without a keeper" (pending.md, canvas-by-day.md, capabilities.md, system-
# rundown.md all decayed because no agent walked them). Lesson logged at the
# top of tasks/lessons.md 2026-05-16. This hook is the structural version of
# that lesson — prose rules fail silently; hooks ARE the rules now.
#
# Behavior:
# - Fires only on `Write` tool (Edit operates on existing files; Write creates
#   or overwrites).
# - Passes if the file ALREADY EXISTS (Write-over-existing is not orphan creation).
# - Passes if the basename doesn't match the tracking-shape pattern.
# - Passes if the basename is already referenced in any keeper source file.
# - Otherwise BLOCKS with a message explaining how to proceed.
#
# Bypass: if a file legitimately needs a tracking-shape name but has a non-
# obvious keeper (e.g., it's written-from but not named in any .md), add an
# explicit `# tracking-orphan-guard: <keeper-path>` comment to the relevant
# keeper file and re-try.

set -uo pipefail

# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" tracking-orphan-guard PreToolUse "" 2>/dev/null || true

INPUT=$(cat)

TOOL_NAME=$(printf '%s' "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || true)

# Only intercept Write (creates new files). Edit always operates on existing files.
if [[ "$TOOL_NAME" != "Write" ]]; then
  exit 0
fi

FILE_PATH=$(printf '%s' "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null || true)

if [[ -z "$FILE_PATH" ]]; then
  exit 0
fi

# Resolve the instance root. FALLS BACK, because the comment this replaces was wrong on both counts.
#
# It said "other guards (state-claim-gate, etc.) still apply" — state-claim-gate was RETIRED on
# 2026-08-06 along with eight siblings, so the fallback it deferred to does not exist. And
# CORE_INSTANCE is not reliably present in hook context: every sibling hook reads
# `CORE_INSTANCE or CLAUDE_PROJECT_DIR` precisely because the runtime sets the second one and an
# operator sets the first. Measured: in a live subagent shell only CLAUDE_PROJECT_DIR is exported.
#
# So this guard has been exiting 0 — completely inert — in every environment where CORE_INSTANCE is
# unset, while its own comment explained why that was safe by naming a hook that no longer runs. A
# guard that fails into silence is indistinguishable from a guard that found nothing.
INSTANCE="${CORE_INSTANCE:-${CLAUDE_PROJECT_DIR:-}}"
if [[ -z "$INSTANCE" ]]; then
  # Derive from this script's own location as the last resort: .claude/hooks/<this> -> repo root.
  INSTANCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
if [[ -z "$INSTANCE" || ! -d "$INSTANCE" ]]; then
  exit 0
fi

# Path registry — exposes $CORE_* absolute paths so the index files below aren't
# hardcoded literals (2026-06-24: lint-code-paths flagged them → staged-code save-block risk).
source "$INSTANCE/bin/core-paths.sh"

# Only check files under instance tasks/ or memory/.
case "$FILE_PATH" in
  "$INSTANCE/tasks/"*) ;;
  "$INSTANCE/memory/"*) ;;
  *) exit 0 ;;
esac

# Already-existing files pass. Write can overwrite; that's not orphan creation.
if [[ -f "$FILE_PATH" ]]; then
  exit 0
fi

# Tracking-shape filename detection.
BASENAME=$(basename "$FILE_PATH")
STEM="${BASENAME%.md}"
TRACKING_PATTERN='(^|[-_])(backlog|inventory|queue|pending|status|dashboard|registry|tracker|kanban|todo|todos|board)([-_]|$)'

if ! echo "$STEM" | grep -qiE "$TRACKING_PATTERN"; then
  # Not tracking-shape — pass.
  exit 0
fi

# This IS a new tracking-shape file. Check for keeper references.
# Search the basename (with and without .md) across keeper source dirs.
KEEPER_DIRS=(
  "$INSTANCE/.claude/agents"
  "$INSTANCE/.claude/hooks"
  "$INSTANCE/.claude/rules"
  "$INSTANCE/.claude/commands"
  "$INSTANCE/memory/projects"
)

FOUND=0
for d in "${KEEPER_DIRS[@]}"; do
  if [[ ! -d "$d" ]]; then continue; fi
  if grep -rqF "$BASENAME" "$d" 2>/dev/null; then
    FOUND=1; break
  fi
  if grep -rqF "$STEM" "$d" 2>/dev/null; then
    FOUND=1; break
  fi
done

# 2026-06-23: also honor the meta-index files that legitimately catalog tracking
# surfaces (memory/projects/<project>.md per-project backlogs are covered by the
# dir above; these two are system-level indexes).
if (( FOUND == 0 )); then
  for f in "$CORE_MEM_CAPABILITIES" "$CORE_TASK_SYSTEM_RUNDOWN"; do
    if [[ -f "$f" ]] && grep -qF "$BASENAME" "$f" 2>/dev/null; then FOUND=1; break; fi
  done
fi

if (( FOUND == 1 )); then
  exit 0
fi

# Block with a helpful message on stderr (Claude Code surfaces this).
cat >&2 <<EOF
================================================================
  TRACKING-ORPHAN GUARD — ACTION BLOCKED
================================================================
  Creating new tracking-shape file: $FILE_PATH

  Filename "$STEM" matches tracking/aggregation pattern (one of:
  backlog|inventory|queue|pending|status|dashboard|registry|tracker|
  kanban|todo|board).

  NO keeper reference found searching:
    .claude/agents/*/CLAUDE.md
    .claude/hooks/*.{sh,py}
    .claude/rules/*.md
    .claude/commands/*.md

  Per tasks/lessons.md (logged 2026-05-16): any new tracking surface
  MUST be added to a keeper's scope in the SAME edit that creates it,
  otherwise it joins the orphan pile (pending.md / canvas-by-day.md
  drift class).

  To proceed, do ONE of:
    1. Add this file to .claude/agents/close-reconciler.md keeper-orphan list, OR
    2. Add to session-start-check.sh scan list, OR
    3. Fold its content into a file that already has a keeper, OR
    4. Rename so the basename doesn't match the tracking pattern
       (rare — only if the file isn't actually tracking-shaped).

  Then retry the Write.
================================================================
EOF

exit 2
