#!/usr/bin/env bash
# learned-resynth-trigger.sh — SessionStart hook. When this Core's correction corpus
# has grown past threshold (the .learned-resynth-due marker, dropped automatically by
# learned-corpus-miner.py --detect), emit a MANDATORY directive telling the live
# session to re-synthesize THIS Core's contracts from its own org-scoped corpus and
# apply them — hands-off, no manual step. Mirrors session-start-truth-drift.sh.
#
# This is what makes each Core SELF-TUNE: a fresh Core ships on the generic starter,
# accumulates its own corrections, and once enough pile up this fires and replaces the
# starter with contracts mined from THIS Core's experience.
#
# SAFETY: --apply only updates injected guidance (required_shape / forbidden_moves);
# it NEVER changes the blocking clauses (those stay hand-defined). So an auto-run can
# only change what's SUGGESTED, never what BLOCKS. Org-scoped — each Core tunes to its
# own corrections. Fail-open + kill-switch (LEARNED_LAYER=0): any issue → silent exit 0.
set -uo pipefail

CORE_INSTANCE="${CORE_INSTANCE:-${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}}"
_CORE_USER="$(python3 "$CORE_INSTANCE/.claude/hooks/lib/coreuser.py" 2>/dev/null || echo "the operator")"
[[ -n "$_CORE_USER" ]] || _CORE_USER="the operator"
DUE="$CORE_INSTANCE/.claude/state/.learned-resynth-due"
RESYNTH="$CORE_INSTANCE/scheduling/claude-si/learned-resynth.py"
WORK="$CORE_INSTANCE/scheduling/claude-si/resynth-work"
BRIEF="$WORK/resynth-brief.json"

[[ "${LEARNED_LAYER:-1}" == "0" ]] && exit 0

# Source guard (2026-06-09): fire only on a REAL session startup. SessionStart
# also re-fires on compact/resume inside the same session — those must not
# re-trigger a resynth (same class of bug as the truth-drift re-partition).
if [ ! -t 0 ]; then
  _SRC=$(cat 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('source') or 'startup')" 2>/dev/null || echo startup)
  [[ "$_SRC" == "startup" || -z "$_SRC" ]] || exit 0
fi

[[ -f "$DUE" ]] || exit 0
[[ -f "$RESYNTH" ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

mkdir -p "$WORK"
# Dump the synthesis brief (current contracts + fresh corpus samples), org-scoped.
python3 "$RESYNTH" --prepare --out "$BRIEF" >/dev/null 2>&1 || exit 0
[[ -s "$BRIEF" ]] || exit 0

NEW=$(head -1 "$DUE" 2>/dev/null | cut -c1-80)

cat <<EOF
🎯 LEARNED-LAYER RESYNTH DUE at session start — this Core's correction corpus grew past threshold (${NEW}).
   Synthesis brief ready: ${BRIEF}

   MANDATORY PRE-RESPONSE ACTION: before answering, re-tune THIS Core's contracts to its own corpus.
   1. Spawn ONE foreground Sonnet subagent (run_in_background:false), Tools: Read ONLY — it must NOT
      write files (dispatch-verify standard 2026-06-09: subagent writes silently fail/lie; the parent
      writes). The brief MUST OPEN with the exact words "Learned-contract resynth worker" — the
      extraction filter keys on opening phrases to keep worker transcripts out of the brain's
      evidence pool, and this dispatcher mandated none, so its worker opened "Read-only task. Do NOT
      write any files" and leaked into core-ops's pending backlog (found 2026-08-04). That opening
      is deliberately NOT registered as a phrase: anchoring it would swallow every legitimate
      read-only brief. If this wording changes, edit scheduling/graphify-brain/pipeline-exhaust.json
      in the SAME commit. Brief: read ${BRIEF}; for each situation, rewrite required_shape[] (<=4 bullets) +
      forbidden_moves[] (<=3) grounded in that situation's corpus samples (tighter, Core-specific
      guidance). INJECT-ONLY: do NOT propose blocking clauses (any such idea goes in a top-level
      "proposals" key for ${_CORE_USER}, never into a situation). Its FINAL MESSAGE must be ONLY the raw JSON,
      keyed by situation (this exact shape — it is what learned-resynth.py apply() joins on):
      {"<situation-key>": {"required_shape": [...], "forbidden_moves": [...]}, ..., "proposals": [...]}
   2. Write the returned JSON to ${WORK}/resynth-out.json YOURSELF (Write tool), then verify it parses
      and apply (org-scoped, inject-only):
        CORE_INSTANCE="\$(git rev-parse --show-toplevel)" python3 scheduling/claude-si/learned-resynth.py --apply ${WORK}/resynth-out.json
      The apply MUST report >0 contracts updated — 0 means a shape/key mismatch; STOP and inspect.
   3. Clear the marker:  rm -f "$DUE"
   4. Proceed to ${_CORE_USER}'s question; confirm in one line that the layer self-tuned.

   Safe to run silently — --apply only changes injected guidance, never what blocks.
EOF
exit 0
