#!/usr/bin/env bash
# auto-pipeline.sh — deterministic brain → graph → viz pipeline entry point.
#
# Run on cron (nightly 02:00 PT) OR manually:
#   bash scheduling/graphify-brain/auto-pipeline.sh
#
# The chunk-extraction step (LLM calls) CANNOT fire from cron — it requires a
# running Claude Code session. This script runs everything deterministic and writes
# dispatch-pending-brief.md whenever there are unprocessed files so the next
# Claude session can fire the LLM step with one command.
#
# Idempotent: safe to run multiple times in the same hour. The brief is
# overwritten each run; the graph artifacts are regenerated in place.

set -uo pipefail

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
TODAY=$(date +%Y-%m-%d)
# Per-Core: date alone is not unique when several Cores run the pipeline the same day.
LOGFILE="/tmp/auto-pipeline-$(basename "${CORE_INSTANCE:-core}")-${TODAY}.log"
# Fail loud on missing env vars (per spec-cascade-fix-2026-05-16.md Phase 1).
: "${CORE_INSTANCE:?CORE_INSTANCE required}"
: "${CORE_BRAIN:?CORE_BRAIN required}"
# Brief output: instance-side personal data. Engine ships clean; brief lives
# in $CORE_INSTANCE/tasks/graphify-brain/ so the engine repo stays free of
# generated personal artifacts.
INSTANCE="$CORE_INSTANCE"
BRIEF_DIR="$INSTANCE/tasks/graphify-brain"
mkdir -p "$BRIEF_DIR"
BRIEF="$BRIEF_DIR/dispatch-pending-brief.md"
# Per spec-graphify-out-relocation-2026-05-16.md — pipeline outputs in brain repo.
PIPELINE_OUT="$CORE_BRAIN/_build/output"
CHECKPOINTS_DIR="$PIPELINE_OUT/checkpoints"
GRAPHIFY_OUT_DIR="$PIPELINE_OUT/graphify-out"
mkdir -p "$CHECKPOINTS_DIR" "$GRAPHIFY_OUT_DIR"

log() {
  local msg="[$(date +%H:%M:%S)] $*"
  echo "$msg"
  echo "$msg" >> "$LOGFILE"
}

log "=== auto-pipeline.sh START ==="
log "REPO:        $REPO"
log "SCRIPT_DIR:  $SCRIPT_DIR"
log "Log:         $LOGFILE"

# ---------------------------------------------------------------------------
# Step 1: Refresh tracker
# ---------------------------------------------------------------------------
log "--- Step 1: build-tracker.py ---"
cd "$SCRIPT_DIR"
python3 build-tracker.py 2>&1 | tee -a "$LOGFILE"

# ---------------------------------------------------------------------------
# Step 2: Count unprocessed files (brain vault only; wiki handled separately)
# ---------------------------------------------------------------------------
log "--- Step 2: count unprocessed ---"
UNPROCESSED_JSON=$(python3 - <<'PYEOF' 2>>"$LOGFILE"
import json, os, sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent if "__file__" in dir() else Path(".")
_BRAIN_ENV = os.environ.get("CORE_BRAIN")
if not _BRAIN_ENV:
    print("auto-pipeline (inline): $CORE_BRAIN not set", file=sys.stderr)
    sys.exit(1)
BRAIN = Path(_BRAIN_ENV)
CHECKPOINTS = BRAIN / "_build" / "output" / "checkpoints"
CHECKPOINTS.mkdir(parents=True, exist_ok=True)

processed = set()
for p in CHECKPOINTS.glob("chunk-body-*.json"):
    try:
        data = json.loads(p.read_text())
        sf = data.get("metadata", {}).get("source_file")
        if sf:
            processed.add(sf)
    except Exception:
        pass

BRAIN_DIRS = sorted({
    str(p.relative_to(BRAIN))
    for p in (BRAIN / "projects").glob("*/sessions")
    if p.is_dir()
} | {
    str(p.relative_to(BRAIN))
    for p in (BRAIN / "projects").glob("*/subagents")
    if p.is_dir()
})

unprocessed = []
for d in BRAIN_DIRS:
    full = BRAIN / d
    if full.is_dir():
        for p in sorted(full.glob("*.md")):
            rel = f"{d}/{p.name}"
            if rel not in processed:
                unprocessed.append(rel)

print(json.dumps(unprocessed))
PYEOF
)

UNPROCESSED_PYTHON_EXIT=$?
if [[ $UNPROCESSED_PYTHON_EXIT -ne 0 || -z "$UNPROCESSED_JSON" ]]; then
  log "ERROR: failed to count unprocessed files (exit $UNPROCESSED_PYTHON_EXIT). Aborting."
  exit 1
fi

# Parse count: write to tmpfile to avoid shell interpolation of path strings
UNPROCESSED_TMPFILE=$(mktemp /tmp/auto-pipeline-unprocessed.XXXXXX.json)
printf '%s' "$UNPROCESSED_JSON" > "$UNPROCESSED_TMPFILE"
UNPROCESSED_COUNT=$(python3 -c "import json,sys; print(len(json.load(open('$UNPROCESSED_TMPFILE'))))" 2>/dev/null || echo 0)

log "Unprocessed count: $UNPROCESSED_COUNT"

# ---------------------------------------------------------------------------
# Step 3: Always run merge chain (partial chunks still useful)
# ---------------------------------------------------------------------------
# Cosmetic-viz step timeout (2026-07-24): the graph-viz steps (community naming, 2D/3D
# layout, report) are NON-recall enrichment for Core-UX. On 2026-07-24 name-communities.py
# ran away to 29min+ of CPU on a 25k-node graph (pathological — memory thrash / degenerate
# case) and HUNG the whole nightly heavy, holding the shared brain lock hostage before the
# recall-critical embed and blocking session closes. A cosmetic step must NEVER be able to do
# that. Bound them: a step that exceeds the cap is killed and SKIPPED (viz degrades, recall is
# untouched, the chain continues). macOS has no `timeout(1)`, so this is bash-native.
VIZ_STEP_TIMEOUT="${VIZ_STEP_TIMEOUT:-240}"   # 240s — ~40x the seconds a 25k-node graph needs
run_step_bounded() {
  # $1 = seconds, $2 = step script. Output to $LOGFILE (no tee — a killed pipe is messy).
  # Returns the step's exit code, or 124 on timeout (mirrors GNU timeout).
  local secs="$1" step="$2" pid waited=0
  python3 "$step" >> "$LOGFILE" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if (( waited >= secs )); then
      log "!! ${step} exceeded ${secs}s — killing (cosmetic viz; chain continues, recall unaffected)"
      kill -TERM "$pid" 2>/dev/null; sleep 2; kill -KILL "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 2; waited=$((waited + 2))
  done
  wait "$pid"; return $?
}

run_merge_chain() {
  local label="$1"
  local step_failed=0
  # Per system-integration-audit 2026-05-17: previously `python3 X | tee` swallowed
  # X's exit code via the tee on stdout. Use PIPESTATUS to capture python's true exit.
  # 2026-07-24: cosmetic viz steps run BOUNDED (run_step_bounded) so a runaway can't hang
  # the pipeline / lock; merge.py + reconcile-isolated.py (graph-STRUCTURAL) stay unbounded.
  local rc
  for step in merge.py name-communities.py make-2d.py make-3d.py make-report.py reconcile-isolated.py; do
    log "--- ${label}: ${step} ---"
    case "$step" in
      name-communities.py|make-2d.py|make-3d.py|make-report.py)
        run_step_bounded "$VIZ_STEP_TIMEOUT" "$step"; rc=$? ;;
      *)
        python3 "$step" 2>&1 | tee -a "$LOGFILE"; rc=${PIPESTATUS[0]} ;;
    esac
    if [[ $rc -eq 124 ]]; then
      log "!! ${step} TIMED OUT — skipped (cosmetic viz; chain continues clean, NOT a chain failure)"
    elif [[ $rc -ne 0 ]]; then
      log "!! ${step} exited with rc=${rc} — chain step failed (continuing chain but flagging)"
      step_failed=1
    fi
  done

  log "--- ${label}: re-merge to absorb reconciliation ---"
  python3 merge.py 2>&1 | tee -a "$LOGFILE"
  local rc=${PIPESTATUS[0]}
  if [[ $rc -ne 0 ]]; then
    log "!! re-merge exited with rc=${rc}"
    step_failed=1
  fi

  # Defensive cleanup: rewrite stale brain paths (2026-05-01/14 vault moves)
  # in graphify output artifacts. Conservative — path-shaped only, leaves prose
  # references untouched. Wired 2026-05-17 after the operator asked for everything to be clean.
  log "--- ${label}: cleanup-stale-paths.py ---"
  python3 cleanup-stale-paths.py 2>&1 | tee -a "$LOGFILE"
  local cleanup_rc=${PIPESTATUS[0]}
  if [[ $cleanup_rc -ne 0 ]]; then
    log "!! cleanup-stale-paths exited with rc=${cleanup_rc} (non-fatal)"
  fi

  return $step_failed
}

# ---------------------------------------------------------------------------
# Branch: unprocessed == 0 → full clean run, exit clean
# ---------------------------------------------------------------------------
if [[ "$UNPROCESSED_COUNT" -eq 0 ]]; then
  log "All files extracted. Running full merge chain."
  run_merge_chain "full-run"
  CHAIN_RC=$?

  # Remove stale brief if one exists (all done)
  if [[ -f "$BRIEF" ]]; then
    rm -f "$BRIEF"
    log "Removed stale dispatch-pending-brief.md (nothing pending)."
  fi

  rm -f "$UNPROCESSED_TMPFILE"
  if [[ $CHAIN_RC -ne 0 ]]; then
    log "=== auto-pipeline.sh DONE WITH ERRORS (clean run, but $CHAIN_RC chain step(s) failed) ==="
    exit 2
  fi
  log "=== auto-pipeline.sh DONE (clean, no pending) ==="
  exit 0
fi

# ---------------------------------------------------------------------------
# Branch: unprocessed > 0 → write brief, still run merge chain
# ---------------------------------------------------------------------------
log "Unprocessed files found ($UNPROCESSED_COUNT). Writing dispatch-pending-brief.md."

# Build file list for the brief (use tmpfile to avoid shell interpolation issues)
# Note: heredoc is unquoted so $UNPROCESSED_TMPFILE expands. Backticks in python
# output must be avoided inside the heredoc; use a print-wrapper to emit them.
FILE_LIST=$(python3 - <<PYEOF2
import json
data = json.load(open("$UNPROCESSED_TMPFILE"))
lines = []
bt = chr(96)
for i, f in enumerate(data, 1):
    basename = f.split("/")[-1].replace(".md", "")
    checkpoint_name = "chunk-body-" + basename + ".json"
    lines.append(str(i) + ". " + bt + f + bt + "  ->  " + bt + "\$CORE_BRAIN/_build/output/checkpoints/" + checkpoint_name + bt)
print("\n".join(lines))
PYEOF2
)

BATCH_SIZE=5
NUM_BATCHES=$(python3 -c "import math; print(min(14, math.ceil($UNPROCESSED_COUNT / $BATCH_SIZE)))")

cat > "$BRIEF" <<BRIEFEOF
# dispatch-pending-brief.md

**Generated:** ${TODAY} by auto-pipeline.sh
**Unprocessed vault files:** ${UNPROCESSED_COUNT}
**Suggested batches:** ${NUM_BATCHES} (${BATCH_SIZE} files each, max 14 subagents)

---

## What to do

Paste this brief to a new Claude session (or use it inline) and say:

> "Read tasks/graphify-brain/dispatch-pending-brief.md and dispatch the subagents per its instructions."

The subagents will extract chunk JSON from each unprocessed file and write them to \`\$CORE_BRAIN/_build/output/checkpoints/\`. After all subagents complete, re-run \`bash scheduling/graphify-brain/auto-pipeline.sh\` to rebuild the graph.

---

## Extraction prompt reference

The body-extraction-prompt.md lives at:

\`\`\`
${CORE_INSTANCE:?CORE_INSTANCE env var must be set}/scheduling/graphify-brain/body-extraction-prompt.md
\`\`\`

Each subagent MUST read this file in full before extracting. It defines the schema, node types, edge keys, and reasoning-pass rules.

---

## Files to extract (${UNPROCESSED_COUNT} total)

${FILE_LIST}

---

## Hard rules for extraction subagents

1. **Filename prefix:** Output files MUST be named \`chunk-body-<basename>.json\` (no other prefix). The basename is the source filename without \`.md\`. Example: \`2026-05-12_core_session.md\` → \`chunk-body-2026-05-12_core_session.json\`.

2. **Write location:** All chunk files go in \`${CORE_BRAIN:?CORE_BRAIN env var must be set}/_build/output/checkpoints/\`.

3. **Edge keys:** Use the exact edge type names from body-extraction-prompt.md: \`motivated_by\`, \`learned_from\`, \`supersedes\`, \`cross_impacts\`, \`references\`. No invented edge types.

4. **metadata block required:** Every chunk JSON must include a top-level \`metadata\` object with at minimum:
   \`\`\`json
   {
     "metadata": {
       "source_file": "<relative-to-brain-root path>",
       "extraction_date": "${TODAY}",
       "extractor": "claude-sonnet"
     },
     "nodes": [...],
     "edges": [...]
   }
   \`\`\`

5. **No duplicate nodes:** Each node \`id\` must be unique within the chunk. Cross-chunk deduplication is handled by merge.py.

6. **Reasoning pass is mandatory:** Per the extraction prompt, a substantive session file should yield 5–15 Decision/Lesson/Rule/Incident nodes. Zero reasoning nodes from a long session is almost certainly wrong.

7. **Do not skip short files:** Even single-exchange subagent logs may contain a Lesson or Rule. Extract them; let merge.py handle weight.

8. **Do not modify existing chunk files** — only write the new ones listed above.

---

## Suggested batch dispatch

Spawn \`${NUM_BATCHES}\` Sonnet 4.6 subagents in parallel, \`${BATCH_SIZE}\` files each.

For each batch subagent brief:
- The brief MUST OPEN with the exact words "Brain-graph extraction worker". **Also include the literal token CORE-PIPELINE-EXHAUST/v1 anywhere in the brief.** That token is what the filter keys on now: an opening SENTENCE can be reworded by accident — this dispatcher's own history is three hub-refresh phrasings none of which matched — and a fixed token cannot. The phrase requirement above stays for briefs written before the token existed. NEVER put this token on a Sentinel, census or research brief: those carry real signal and must keep flowing into the evidence pool (extract-pending.sh:145). The extraction filter
  keys on opening phrases to keep worker transcripts out of the evidence pool; this dispatcher
  mandated none, so its workers invented openings and their transcripts came back as pending
  evidence, which spawns more workers — the recursion the filter exists to kill. Same phrase as
  extract-pending.sh on purpose: this is the same work, and two phrases for one job is how the
  07-28 leak happened. If it must change, edit \`scheduling/graphify-brain/pipeline-exhaust.json\`
  in the SAME commit.
- Goal: extract chunk JSON for the assigned files
- Read body-extraction-prompt.md in full before starting
- Write each chunk to \`checkpoints/chunk-body-<basename>.json\`
- Allowed tools: Read, Write
- No bash execution required — pure read + write
- Return: list of files written and node/edge counts per file

After all batches complete, run:
\`\`\`bash
bash "${CORE_INSTANCE:?CORE_INSTANCE env var must be set}/scheduling/graphify-brain/auto-pipeline.sh"
\`\`\`

This will rebuild the tracker, merge all new chunks into the graph, and clear this brief.

BRIEFEOF

log "Brief written: $BRIEF"

# Still run merge chain — partial chunks are better than stale graph
log "Running merge chain (partial chunks still useful)."
run_merge_chain "partial-run"
CHAIN_RC=$?

rm -f "$UNPROCESSED_TMPFILE"
if [[ $CHAIN_RC -ne 0 ]]; then
  log "=== auto-pipeline.sh DONE WITH ERRORS (${UNPROCESSED_COUNT} pending; $CHAIN_RC chain step(s) failed) ==="
  exit 2
fi
log "=== auto-pipeline.sh DONE (${UNPROCESSED_COUNT} files pending — brief written) ==="
exit 0
