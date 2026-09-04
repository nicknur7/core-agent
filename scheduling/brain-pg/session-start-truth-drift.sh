#!/usr/bin/env bash
# session-start-truth-drift.sh — detect compile-truth drift at SessionStart.
#
# If drift > 0:
#   1. Partition drifted hubs into batches (writes refresh-batch-NN.json to compile-truth-work/)
#   2. Emit a directive block to stdout that tells Claude to fan out Sonnet subagents
#      (via Agent() — Max-sub usage, NOT API key spend) BEFORE first user response,
#      then run --ingest.
#
# Else: emit nothing.
#
# Called by .claude/hooks/session-start-check.sh and folded into the SessionStart
# additionalContext. Stays within the SessionStart timeout budget (15s) — detect+
# partition together are ~3-5s.
#
# Design rationale: brain compiled_truth_md staleness was a 11-day silent gap until
# 2026-05-28. Wiring this into SessionStart means the brain auto-refreshes its
# narrative content as part of session warmup, not via a separate manual cadence.
# Subagent fan-out happens during the active Claude conversation, so the spend
# goes against Nick's Max subscription, not the Anthropic API key billing.

set -uo pipefail

CORE_INSTANCE="${CORE_INSTANCE:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}"
REFRESH_SCRIPT="$CORE_INSTANCE/scheduling/brain-pg/compile-truth-refresh.py"
WORK_DIR="$CORE_INSTANCE/scheduling/brain-pg/compile-truth-work"

# Fail-open: if any prerequisite missing, exit silent.
[[ -f "$REFRESH_SCRIPT" ]] || exit 0
[[ -d "$CORE_BRAIN" ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

# Detect drift (silent — discard human-readable output, parse JSON report).
CORE_BRAIN="$CORE_BRAIN" CORE_INSTANCE="$CORE_INSTANCE" \
  python3 "$REFRESH_SCRIPT" --detect >/dev/null 2>&1 || exit 0

# Parse most recent drift report.
REPORT=$(ls -t "$WORK_DIR"/drift-report-*.json 2>/dev/null | head -1)
[[ -f "$REPORT" ]] || exit 0

DRIFT_COUNT=$(python3 -c "
import json, sys
try:
    r = json.load(open('$REPORT'))
    print(len(r.get('drift_records', [])))
except Exception:
    print(0)
" 2>/dev/null)
[[ "$DRIFT_COUNT" =~ ^[0-9]+$ ]] || exit 0
(( DRIFT_COUNT > 0 )) || exit 0

# Pull cost estimate + top 3 names for the directive.
TOP_INFO=$(python3 -c "
import json
r = json.load(open('$REPORT'))
cost = r.get('estimated_cost_usd', '?')
top3 = [rec['name'] for rec in r.get('drift_records', [])[:3]]
print(f'{cost}|' + ','.join(top3))
" 2>/dev/null)
EST_COST="${TOP_INFO%%|*}"
TOP_NAMES="${TOP_INFO#*|}"

# Partition into batches. N = min(14, ceil(count/7)) — ~7 entities per batch.
BATCH_N=$(python3 -c "import math; print(min(14, max(1, math.ceil($DRIFT_COUNT / 7))))")
CORE_BRAIN="$CORE_BRAIN" CORE_INSTANCE="$CORE_INSTANCE" \
  python3 "$REFRESH_SCRIPT" --partition --batches "$BATCH_N" >/dev/null 2>&1 || exit 0

# List partitioned batch input files.
BATCH_FILES=$(ls "$WORK_DIR"/refresh-batch-*.json 2>/dev/null | grep -v -- '-out\.json' | sort | sed 's|^|    - |')

# Emit directive.
cat <<EOF
🧠 BRAIN TRUTH DRIFT DETECTED at session start — ${DRIFT_COUNT} hub(s) have stale compiled_truth_md.
   Top drifted: ${TOP_NAMES}
   Estimated subagent cost: \$${EST_COST} (Max-sub via Agent() — NOT API key spend)
   Batches pre-partitioned (${BATCH_N} batches, ~7 entities each):
${BATCH_FILES}

   MANDATORY PRE-RESPONSE ACTION: before answering the operator's first message, refresh the brain:
   1. Spawn ${BATCH_N} parallel foreground Sonnet subagents (run_in_background:false), one per refresh-batch-NN.json.
      Tools allowed: Read ONLY — subagents must NOT write files (dispatch-verify standard 2026-06-09:
      on 06-09, 5 of 7 write-briefed agents claimed success while writing nothing; the parent writes).
      Each subagent brief MUST OPEN with the exact words "Compiled-truth hub refresh" **Also include the literal token CORE-PIPELINE-EXHAUST/v1 anywhere in the brief.** That token is what the filter keys on now: an opening SENTENCE can be reworded by accident — this dispatcher's own history is three hub-refresh phrasings none of which matched — and a fixed token cannot. The phrase requirement above stays for briefs written before the token existed. NEVER put this token on a Sentinel, census or research brief: those carry real signal and must keep flowing into the evidence pool (extract-pending.sh:145). — the
      extraction filter keys on opening phrases to keep worker transcripts out of the evidence
      pool, and THIS dispatcher is the one that never mandated a phrase. A producer told no
      phrase invents its own every run: this one minted three distinct openings and leaked 24
      transcripts, which became 57% of a pending backlog (2026-07-26/28). Reword this line and
      the recursion comes back silently, so if it must change, edit
      scheduling/graphify-brain/pipeline-exhaust.json in the SAME commit.
      Brief shape (per .claude/commands/refresh-truth.md Step 4): each subagent reads its batch file,
      reads each entity's hub_path + up to 5 most-recent source_files, synthesizes a 2-3 paragraph
      compiled_truth_md with inline date citations + confidence 0.0-1.0. Its FINAL MESSAGE must be
      ONLY the raw JSON (no prose, no fences), kind/name VERBATIM from the batch file:
      {"results":[{"kind":"...","name":"...","compiled_truth_md":"...","confidence":0.0}]}
   2. As each returns, YOU write its JSON to refresh-batch-NN-out.json (Write tool), then verify every
      out-file parses and each (kind,name) matches its batch input before ingesting:
        CORE_BRAIN="\$HOME/AI Projects/core-brain" CORE_INSTANCE="\$(git rev-parse --show-toplevel)" \\
          python3 scheduling/brain-pg/compile-truth-refresh.py --ingest
      The ingest count MUST equal the drifted-hub count — a shortfall means lost batches; STOP and re-run those.
   3. Then proceed to the operator's question.

   If drift count > 20 or estimated cost > \$3, briefly tell the operator the size BEFORE fan-out so
   they can intervene. Below those thresholds, run silently and confirm completion in your first response.
EOF
exit 0
