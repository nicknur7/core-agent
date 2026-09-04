#!/usr/bin/env bash
# PreToolUse hook for Task — injects high-cost lesson reminders before each subagent dispatch.
# Per tasks/lessons.md these patterns each cost 100K+ tokens when forgotten:
#   - 2026-05-03: 8-subagent sandbox failure (~640K-1M tokens wasted)
#   - 2026-05-05: subagent-input-poisoning (Karpathy brief omitted graphify purpose)
#   - 2026-05-01: parallel spawn without heads-up
# Moves enforcement from session-start-only (forgotten by hour 4) to per-Task-call
# (just-in-time delivery, harder to forget).
#
# Template-extraction note (2026-05-13): the dates/incident-names below are
# Nick-historical anecdotes that earned real lessons. They make the reminder
# concrete. A fresh template can keep them as illustrative example, replace
# with the new deployment's own incident log, or strip dates to generic
# wording. Lesson content itself (brief includes purpose, heads-up before
# parallel, verify write-path on one agent first) is universal — keep it.
set -uo pipefail

_CORE_USER="$(python3 "$(dirname "${BASH_SOURCE[0]}")/lib/coreuser.py" 2>/dev/null || echo "the operator")"
[[ -n "$_CORE_USER" ]] || _CORE_USER="the operator"

# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" task-brief-reminder PreToolUse "" 2>/dev/null || true

INPUT=$(cat)
TOOL_NAME=$(printf '%s' "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || true)

# Only inject for Task — settings.json should already filter, but defense-in-depth
[[ "$TOOL_NAME" != "Task" ]] && exit 0

# Extract for context-aware reminder
PROMPT_LEN=$(printf '%s' "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('tool_input',{}).get('prompt','')))" 2>/dev/null || echo "0")
RUN_BG=$(printf '%s' "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('run_in_background',False))" 2>/dev/null || echo "False")
SUBAGENT=$(printf '%s' "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('subagent_type','general-purpose'))" 2>/dev/null || echo "general-purpose")

REMINDER="TASK PRE-FLIGHT (lessons that cost 100K+ tokens each when forgotten):
1. **Brief must include project purpose.** A brief that omits the project's actual purpose produces poisoned output, then I parrot the verdict back as 'independent research'. (2026-05-05 — Karpathy graphify brief incident.) Current brief: ${PROMPT_LEN} chars, type=${SUBAGENT}.
2. **Heads-up before parallel / token-heavy ops.** Announce 'Spawning N for X' to ${_CORE_USER} BEFORE the dispatch — they can intervene before the spend, not after. (2026-05-01)
3. **Verify write-path access on ONE test agent BEFORE parallel batches.** Subagent sandbox is tighter than main thread; bit lint-pass build twice + 8-agent brain dispatch (~640K-1M tokens wasted). (2026-05-03)"

if [[ "$RUN_BG" == "True" ]]; then
  REMINDER+="
4. **run_in_background: true detected.** Auto-denies non-allowlisted bash + peer-project writes (e.g., core-ui/ from core/). For new bash patterns or peer-project writes, use foreground instead. (2026-04-26)"
fi

REMINDER+="

If your brief violates 1, abort this dispatch and re-pose. The cost of an abort is ~0; the cost of a poisoned subagent is the full token spend + a wrong verdict."

# Inject as additional context — does NOT block the call
printf '%s' "$REMINDER" | python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': sys.stdin.read()
    }
}))"
exit 0
