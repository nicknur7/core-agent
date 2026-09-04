#!/usr/bin/env bash
# recall-eval-refresh — keep the recall benchmark from going stale, without a new scheduler.
#
# WHY THIS EXISTS. The SI loop raises a 🟡 when the newest
# tasks/research/brain-primitives-benchmark*.md is more than 7 days old, and its remedy reads
# "re-run eval.py + schedule nightly". The re-run half is easy; the scheduling half had two traps.
#
# TRAP 1 — DO NOT CREATE A CRON JOB. The operator, 2026-08-25: disarm and kill every cron loop —
# each Core should only have a bus monitor, nothing else. A new scheduler is exactly what that
# directive forbids. The launchd product jobs were explicitly exempted ("Leave them running"), so
# the correct home is INSIDE the existing com.nick.brain-pipeline nightly, which already runs at
# 02:00 and already hosts the corpus miner above its debt gate for the same reason.
#
# TRAP 2 — NOT ACTUALLY NIGHTLY. The staleness bar is 7 days; a full eval is tens of minutes of
# Voyage round-trips. Running it nightly would burn ~7x the API cost to satisfy a weekly check, and
# the nightly's own charter is to be a fallback that "shouldn't be used". So this is nightly-INVOKED
# and weekly-GATED: it exits in milliseconds on six nights out of seven.
#
# TRAP 3 — NEVER --ablation HERE. `eval.py --ablation` takes a different branch that returns early:
# it writes .claude/state/.brain-leg-ablation.json and NEVER writes the benchmark report. A refresh
# job using that flag would run for forty minutes, exit 0, and leave the 🟡 exactly where it was.
# (Learned the direct way on 2026-08-25 — the flag was chosen for thoroughness and silently did not
# do the one job that mattered.) Per-leg ablation is a separate, deliberate investigation.
#
# Detached on purpose: the launchd job carries ExitTimeOut 600 and later `exec`s into the heavy
# rebuild when there is debt. A foreground eval would either blow that ceiling or delay the rebuild.
# This starts the work and returns.
#
# Fail-open and side-effect-free on every path — a broken refresh must never cost the nightly.
set -uo pipefail

REPO="${CORE_INSTANCE:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[[ -z "$REPO" || ! -d "$REPO" ]] && exit 0

IDENTITY="$REPO/.claude/identity.json"
EVAL_PY="$REPO/scheduling/brain-pg/eval.py"
EVAL_SET="$REPO/scheduling/brain-pg/eval-set-v2.json"
REPORT_DIR="$REPO/tasks/research"
LOG="/tmp/recall-eval-refresh-$(basename "$REPO").log"
STALE_DAYS=7

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG" 2>/dev/null || true; }

# ONE SEAT OWNS THIS. The eval measures the shared brain, so five Cores running it would be five
# times the API spend for one number, and detect.sh already gates its 🟡 on the same flag.
OWNER="$(python3 -c "
import json,sys
try: print(str(json.load(open('$IDENTITY')).get('recall_eval_owner', False)).lower())
except Exception: print('false')
" 2>/dev/null || echo false)"
if [[ "$OWNER" != "true" ]]; then
  log "not the recall_eval_owner — skip"
  exit 0
fi

[[ -f "$EVAL_PY" && -f "$EVAL_SET" ]] || { log "eval.py or eval-set missing — skip"; exit 0; }

# Weekly gate. `find -mtime +N` on the NEWEST report; no report at all also counts as stale.
#
# `find -print0 | xargs -0 ls -t`, NOT `ls -t $GLOB`. The Core path contains a space
# ("~/AI Projects/<core>"), so an unquoted glob variable word-splits into "/Users/<you>/AI"
# and "Projects/..." and matches nothing. The first version of this script did exactly that and
# logged "no benchmark on disk at all" with a benchmark sitting in that directory — which would have
# fired a full eval every single night, the precise cost this weekly gate exists to avoid, while
# looking like it was working. (detect.sh gets away with the glob form only because it keeps the
# expansion inline and adjacent to the quoted prefix, never through a variable.)
NEWEST="$(find "$REPORT_DIR" -maxdepth 1 -name 'brain-primitives-benchmark*.md' -print0 2>/dev/null \
          | xargs -0 ls -t 2>/dev/null | head -1)"
if [[ -n "$NEWEST" ]]; then
  if [[ -z "$(find "$NEWEST" -mtime +$STALE_DAYS 2>/dev/null)" ]]; then
    log "fresh ($(basename "$NEWEST")) — no run"
    exit 0
  fi
  log "stale: $(basename "$NEWEST") older than ${STALE_DAYS}d"
else
  log "no benchmark on disk at all"
fi

# Don't stack runs. A previous night's eval that is somehow still going owns the slot.
if pgrep -f "brain-pg/eval.py --eval-set" >/dev/null 2>&1; then
  log "an eval is already running — skip"
  exit 0
fi

log "starting eval.py in report mode (no leg-ablation flag, so write_report runs)"
CORE_INSTANCE="$REPO" \
CORE_ORG_ID="${CORE_ORG_ID:-1}" \
CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
  nohup python3 "$EVAL_PY" --eval-set "$EVAL_SET" >> "$LOG" 2>&1 &
log "detached as pid $!"
exit 0
