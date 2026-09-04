#!/usr/bin/env bash
# Brain-update helper. Two modes (fast / heavy), shared brain lock.
#
# Modes:
#   fast  (default; Stop hook):  extract this session JSONL → markdown,
#                                then embed.py --incremental. ~10 sec.
#   heavy (LaunchAgent + /rebuild-graph): full graphify pipeline +
#                                lint + tracker + embed.py --incremental. ~90 sec.
#
# Mode selection: first arg, or BRAIN_UPDATE_MODE env var, defaults to fast.
#
# Lock semantics:
# - Keyed on $CORE_BRAIN — all Cores share one lock per brain so two Cores
#   closing simultaneously serialize writes to the brain repo.
# - `mkdir <lock>` is atomic on POSIX.
# - Stale-lock reclaim after 600s.
#
# Each Core runs ENTIRELY on its own files: $CORE_INSTANCE/scheduling/...
# No engine refs at runtime (Phase 4 of spec-self-hosted-cores).
set -uo pipefail

: "${CORE_BRAIN:?CORE_BRAIN env var required — set in shell before invoking}"
: "${CORE_INSTANCE:?CORE_INSTANCE env var required — set in shell before invoking}"

MODE="${1:-${BRAIN_UPDATE_MODE:-fast}}"
case "$MODE" in fast|heavy) ;; *) echo "Unknown mode: $MODE" >&2; exit 1 ;; esac

BRAIN_HASH=$(echo "$CORE_BRAIN" | md5 -q 2>/dev/null || echo "$CORE_BRAIN" | md5sum | cut -d' ' -f1)
BRAIN_LOCK="/tmp/core-brain-${BRAIN_HASH}.lock"
# The LOCK is deliberately shared — one brain, one lock, all Cores queue on it.
# The LOG is not. Every Core wrote to the same /tmp/brain-stop-hook.log, and
# scheduling/core-si/detect.sh READS it to decide whether THIS Core's brain pipeline
# failed — so core-life could report a failure that was actually core-business's embed,
# or miss its own beneath another Core's lines. Namespaced per-Core, matching the fix
# session-lifecycle.sh already applied to its own log for the same reason (2026-06-23).
LOG="/tmp/brain-stop-hook-$(basename "${CORE_INSTANCE:-core}").log"
STALE_AFTER=600   # applies ONLY to pid-less (legacy-style) locks — see fallback below

# ── Unified blocking brain lock (2026-07-24 redesign; supersedes skip-on-busy + blanket 600s reclaim) ──
# Nick's call: all Cores QUEUE on the shared lock — wait until it's free, run to completion. NO
# timeout: a live, working holder is never abandoned for being slow. Crash safety comes from
# IDENTITY, not time: the holder records holder.pid + holder.cmd (its `ps` command line) inside the
# lock dir; a waiter reclaims ONLY when the recorded process is provably gone —
#   • pid dead (kill -0 fails)                      → crashed holder
#   • pid alive but command line ≠ recorded cmd     → PID recycled to an unrelated process
# Reclaim is ATOMIC-RENAME (mv to a waiter-private name): of N racing waiters exactly one wins the
# mv; nobody can ever delete a successor's freshly-created live lock (the TOCTOU in naive
# rm-rf-then-mkdir reclaim). embed.py speaks this same protocol for direct invocations.
# LEGACY COMPAT (until the fleet push): old peers/embeds create this dir with NO pid file and
# self-reclaim on 600s mtime. So (a) a pid-less lock falls back to the legacy 600s-mtime staleness
# rule — identical semantics old code applies to itself; and (b) our holder TOUCHES the dir every
# 60s so an old peer's mtime-based reclaim can never kill a live new-style holder mid-run.
LOCK_PID_FILE="$BRAIN_LOCK/holder.pid"
LOCK_CMD_FILE="$BRAIN_LOCK/holder.cmd"
LOCK_STARTED_FILE="$BRAIN_LOCK/holder.started"   # acquisition epoch — NOT touched by the toucher
# BACKSTOP (2026-07-24): last-resort reclaim of a holder wedged for an absurdly long time. This is
# NOT the "fail if slow" timer Nick rejected — it never fires on normal slowness (a fast close is
# ~2min; even a big cold embed is well under an hour). It ONLY reclaims a holder that has HELD the
# lock for BACKSTOP_AFTER, i.e. genuinely runaway/wedged (the 2026-07-24 name-communities case that
# hit 48min and climbing). The wedged holder is KILLED first so it can't un-wedge and write
# concurrently. Uses holder.started (immune to the toucher), so it measures true hold-time.
BACKSTOP_AFTER="${BRAIN_LOCK_BACKSTOP:-3600}"    # 60 min

_holder_cmd() { ps -p "$1" -o command= 2>/dev/null | sed 's/[[:space:]]*$//'; }
_atomic_reclaim() {
  # Rename-then-delete: mv is atomic, so of N racing waiters exactly one succeeds; the losers'
  # mv fails and they loop back to mkdir. The corpse name is waiter-private ($$).
  local corpse="${BRAIN_LOCK}.reap.$$"
  if mv "$BRAIN_LOCK" "$corpse" 2>/dev/null; then
    rm -rf "$corpse" 2>/dev/null || true
  fi
}
_tree_pids() {
  # Print root + ALL descendant pids in one snapshot (macOS pgrep -P walk). The holder is a shell
  # whose CHILDREN (embed.py / auto-pipeline.sh) are the real DB writers, so the whole tree must
  # die. Codex round 2: DON'T re-walk between TERM and KILL — a TERM-surviving child reparents to
  # launchd and a fresh `pgrep -P root` misses it. Snapshot the set ONCE, then signal that same set.
  local root="$1" kid
  echo "$root"
  for kid in $(pgrep -P "$root" 2>/dev/null); do _tree_pids "$kid"; done
}

_WAIT_LOGGED=0
while ! mkdir "$BRAIN_LOCK" 2>/dev/null; do
  HOLDER_PID=$(cat "$LOCK_PID_FILE" 2>/dev/null || echo "")
  if [[ "$HOLDER_PID" =~ ^[0-9]+$ ]]; then
    HOLDER_CMD=$(cat "$LOCK_CMD_FILE" 2>/dev/null | sed 's/[[:space:]]*$//')
    LIVE_CMD=$(_holder_cmd "$HOLDER_PID")
    if [[ -n "$LIVE_CMD" && ( -z "$HOLDER_CMD" || "$LIVE_CMD" == "$HOLDER_CMD" ) ]]; then
      # Live holder with matching identity (or cmd not yet recorded — sub-second window).
      # BACKSTOP first: if this holder has HELD the lock longer than BACKSTOP_AFTER, it is
      # wedged/runaway — kill it and reclaim (last resort; see note at top). holder.started is
      # immune to the toucher, so this measures true hold-time, not freshness.
      LOCK_STARTED=$(cat "$LOCK_STARTED_FILE" 2>/dev/null || echo "")
      if [[ "$LOCK_STARTED" =~ ^[0-9]+$ ]] && (( $(date +%s) - LOCK_STARTED > BACKSTOP_AFTER )); then
        # Re-verify identity IMMEDIATELY before killing — NARROWS the check→kill PID-reuse window.
        # It cannot fully close it (macOS has no race-free pid kill / pidfd), so a microsecond
        # window remains where the pid could die+recycle between recheck and kill. ACCEPTED
        # residual (Codex round 2 High): it only fires after a 60-MINUTE wedge, and killing one
        # unlucky recycled process is strictly better than an infinite lock jam. Only proceed if
        # this pid is STILL the recorded holder with a matching command.
        RECHECK_CMD=$(_holder_cmd "$HOLDER_PID")
        if [[ "$(cat "$LOCK_PID_FILE" 2>/dev/null)" == "$HOLDER_PID" && -n "$RECHECK_CMD" \
              && ( -z "$HOLDER_CMD" || "$RECHECK_CMD" == "$HOLDER_CMD" ) ]]; then
          echo "[$(date)] run-brain-update[$MODE]: BACKSTOP — holder pid=$HOLDER_PID wedged >${BACKSTOP_AFTER}s — killing process TREE + reclaiming" >> "$LOG"
          # Snapshot the tree ONCE, then TERM the set, then KILL the SAME set (a TERM-survivor
          # that reparents is still in the snapshot, so it is not missed — Codex round 2 Critical).
          BACKSTOP_TREE=$(_tree_pids "$HOLDER_PID")
          for _p in $BACKSTOP_TREE; do kill -TERM "$_p" 2>/dev/null || true; done
          sleep 2
          for _p in $BACKSTOP_TREE; do kill -KILL "$_p" 2>/dev/null || true; done
          _atomic_reclaim
        fi
        continue
      fi
      # Otherwise wait our turn. Blocking, no timeout (Nick 2026-07-24: run until the queue clears).
      if (( _WAIT_LOGGED == 0 )); then
        echo "[$(date)] run-brain-update[$MODE]: brain lock held by live pid=$HOLDER_PID — queuing (blocking, no timeout)" >> "$LOG"
        _WAIT_LOGGED=1
      fi
      sleep 3; continue
    fi
    # pid dead, or alive under a DIFFERENT command (recycled pid) → provably not our holder.
    echo "[$(date)] run-brain-update[$MODE]: lock holder gone (pid=$HOLDER_PID, live_cmd='${LIVE_CMD:-none}') — atomic reclaim" >> "$LOG"
    _atomic_reclaim
    continue
  fi
  # No pid recorded → legacy-style holder (old peer / old embed.py) OR an acquirer that died in
  # the mkdir→write-pid window. Apply the legacy 600s mtime rule to exactly this class.
  LOCK_MTIME=$(stat -f %m "$BRAIN_LOCK" 2>/dev/null || echo 0)
  LOCK_AGE=$(( $(date +%s) - LOCK_MTIME ))
  if (( LOCK_AGE > STALE_AFTER )); then
    echo "[$(date)] run-brain-update[$MODE]: pid-less lock stale (${LOCK_AGE}s) — legacy reclaim" >> "$LOG"
    _atomic_reclaim
    continue
  fi
  if (( _WAIT_LOGGED == 0 )); then
    echo "[$(date)] run-brain-update[$MODE]: pid-less (legacy) lock in flight (${LOCK_AGE}s) — queuing" >> "$LOG"
    _WAIT_LOGGED=1
  fi
  sleep 3
done

# We OWN the lock. Record identity for waiters' liveness checks, sweep any orphaned reclaim
# corpses, and start the mtime toucher (compat shim vs old peers' 600s reclaim; remove with them).
# Write holder.started FIRST (Codex round: if a holder wedged AFTER pid/cmd but BEFORE started,
# started would never appear and the backstop could never fire → wait forever). Started-first
# guarantees: pid present ⇒ started present. A wedge before pid → pid-less → the mtime path.
date +%s > "$LOCK_STARTED_FILE" 2>/dev/null || true   # true acquisition time (backstop reads this)
echo "$$" > "$LOCK_PID_FILE" 2>/dev/null || true
_holder_cmd "$$" > "$LOCK_CMD_FILE" 2>/dev/null || true
rm -rf "${BRAIN_LOCK}".reap.* 2>/dev/null || true
# Toucher hardening (Codex round-2 Critical): if the holder is SIGKILLed the EXIT trap never
# fires and this subshell is orphaned. A waiter then reclaims the dead holder's dir — and a
# naive `touch` would CREATE A FILE at the lock path, permanently poisoning mkdir for every
# Core. Two guards: (a) exit unless holder.pid still names our (dead-or-alive) parent — an
# orphan stops within one cycle of any reclaim; (b) `touch -c` never creates the path if it
# is gone. ($$ inside the subshell still expands to the parent script's PID — that's the
# ownership identity we want.)
( while :; do
    sleep 60
    [[ "$(cat "$LOCK_PID_FILE" 2>/dev/null)" == "$$" ]] || exit 0
    touch -c "$BRAIN_LOCK" 2>/dev/null || exit 0
  done ) &
LOCK_TOUCHER_PID=$!

_release_lock() {
  kill "$LOCK_TOUCHER_PID" 2>/dev/null || true
  # Ownership check: only remove the lock if it is still OURS. After a (wrongful or crash-path)
  # reclaim the recreated lock belongs to a successor — never delete another holder's live lock.
  if [[ "$(cat "$LOCK_PID_FILE" 2>/dev/null)" == "$$" ]]; then
    rm -rf "$BRAIN_LOCK" 2>/dev/null || true
  fi
  rm -f "$CORE_INSTANCE/.claude/state/.brain-update-inflight" 2>/dev/null
}
trap '_release_lock' EXIT
# We're running now, so any prior deferral is being resolved. Clear the marker.
rm -f "$CORE_INSTANCE/.claude/state/.brain-update-deferred" 2>/dev/null || true

# Signal nested embed.py that the shared brain lock is already held here, so its
# own defense-in-depth self-lock (acquire_brain_lock) skips re-acquiring the same
# dir — otherwise it would see the lock we hold and abort the update.
export BRAIN_LOCK_HELD=1

echo "[$(date)] run-brain-update[$MODE]: lock acquired by PID $$" >> "$LOG"

# C2 (2026-07-23, Codex #1): in-flight sentinel. Written now (work is about to start), removed by the EXIT
# trap on ANY graceful exit (success OR handled non-zero). It therefore SURVIVES only an UNGRACEFUL death —
# SIGKILL, power loss, terminal killed mid-chain — the exact case brain_status can't detect (the session
# shows 'captured' but extraction/merge/embed never finished, so it reports READY). SessionStart looks for
# this marker with NO live worker and RESUMES the update (every stage is idempotent), so a died-mid-chain
# close self-heals instead of leaving the graph silently stale.
mkdir -p "$CORE_INSTANCE/.claude/state" 2>/dev/null || true
printf '%s | mode=%s | pid=%s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$MODE" "$$" \
  > "$CORE_INSTANCE/.claude/state/.brain-update-inflight" 2>/dev/null || true

case "$MODE" in
  fast)
    # Export this session's JSONL → brain markdown, then embed new files into Postgres.
    python3 "$CORE_INSTANCE/scheduling/graphify-brain/extract-core-sessions.py" >> "$LOG" 2>&1
    EXTRACT_EXIT=$?
    echo "[$(date)] run-brain-update[fast]: extract exit=$EXTRACT_EXIT" >> "$LOG"

    # GRAPH EXTRACTION is NO LONGER here (2026-07-24): it moved OUT of run-brain-update and INTO the
    # in-session /close-core orchestration (spawn Haiku Agent() subagents on the session's subscription
    # auth → parent verifies → merge → embed --graph-nodes). The headless extractor + ANTHROPIC_API_KEY
    # are retired. `fast` mode is now purely DETERMINISTIC (no LLM, no key): JSONL export (above) + the
    # evidence-body embed (below). Safe to run anywhere, including the defensive/background paths.
    # EMBED_EXIT defaults to FAILURE (Codex 2026-07-24): if embed.py or python3 is missing, embedding
    # never ran — that is NOT success. The old `${EMBED_EXIT:-0}` treated the unset var as 0, so a
    # broken install silently satisfied the close's synced-marker gate. Default 1; only a real run sets it.
    EMBED_EXIT=1
    if command -v python3 >/dev/null 2>&1 && [[ -f "$CORE_INSTANCE/scheduling/brain-pg/embed.py" ]]; then
      python3 "$CORE_INSTANCE/scheduling/brain-pg/embed.py" --incremental >> "$LOG" 2>&1
      EMBED_EXIT=$?
      echo "[$(date)] run-brain-update[fast]: embed exit=$EMBED_EXIT" >> "$LOG"
    else
      echo "[$(date)] run-brain-update[fast]: embed.py or python3 MISSING — embed did NOT run (chain FAILS)" >> "$LOG"
    fi
    # CHAIN_EXIT must reflect EVERY stage, not just extract. A failed/absent embed
    # previously reported chain success (the 2026-05-28 silent exit=1 bug). First non-zero wins.
    CHAIN_EXIT=$EXTRACT_EXIT
    [[ "$EMBED_EXIT" -ne 0 ]] && CHAIN_EXIT=$EMBED_EXIT
    ;;

  heavy)
    # Ensure the DB schema matches the code BEFORE the rebuild — apply any pending
    # brain-pg migrations (idempotent; the tracker skips applied files). Self-heal so  # privacy-ok: generic engineering vocabulary
    # a fork's DB (peers included) is never behind the code it pulled. Fail-soft.
    if [[ -f "$CORE_INSTANCE/bin/run-migrations.sh" ]] && command -v psql >/dev/null 2>&1; then
      if bash "$CORE_INSTANCE/bin/run-migrations.sh" >> "$LOG" 2>&1; then
        echo "[$(date)] run-brain-update[heavy]: migrations up to date" >> "$LOG"
      else
        echo "[$(date)] run-brain-update[heavy]: WARN migrations failed (continuing)" >> "$LOG"
      fi
    fi
    # A4 single-read-dual-output (2026-06-08): derive hub reports from the chunk-body checkpoints
    # the extraction already produced (NO second LLM read), BEFORE consolidate.py runs inside
    # update-brain.sh — so the Layer-3 hub layer stays current off the same read. This is also the
    # durable unfreeze (regenerate-reports.py never existed; the May-17 reports were frozen).
    if [[ -f "$CORE_INSTANCE/scheduling/graphify-brain/reports-from-checkpoints.py" ]]; then
      CORE_BRAIN="$CORE_BRAIN" python3 "$CORE_INSTANCE/scheduling/graphify-brain/reports-from-checkpoints.py" >> "$LOG" 2>&1
      echo "[$(date)] run-brain-update[heavy]: reports-from-checkpoints exit=$?" >> "$LOG"
    fi
    # Full graphify pipeline + lint + tracker + embed.
    bash "$CORE_BRAIN/_build/update-brain.sh" >> "$LOG" 2>&1 \
      && bash "$CORE_INSTANCE/scheduling/brain-lint/lint-pass.sh" >> "$LOG" 2>&1
    CHAIN_EXIT=$?
    echo "[$(date)] run-brain-update[heavy]: update+lint exit=$CHAIN_EXIT" >> "$LOG"

    python3 "$CORE_INSTANCE/scheduling/graphify-brain/build-tracker.py" >> "$LOG" 2>&1
    echo "[$(date)] run-brain-update[heavy]: tracker exit=$?" >> "$LOG"

    bash "$CORE_INSTANCE/scheduling/graphify-brain/auto-pipeline.sh" >> "$LOG" 2>&1
    echo "[$(date)] run-brain-update[heavy]: auto-pipeline exit=$?" >> "$LOG"

    if command -v python3 >/dev/null 2>&1 && [[ -f "$CORE_INSTANCE/scheduling/brain-pg/embed.py" ]]; then
      python3 "$CORE_INSTANCE/scheduling/brain-pg/embed.py" --incremental >> "$LOG" 2>&1
      EMBED_EXIT=$?
      echo "[$(date)] run-brain-update[heavy]: embed exit=$EMBED_EXIT" >> "$LOG"
      [[ "$EMBED_EXIT" -ne 0 ]] && CHAIN_EXIT=$EMBED_EXIT
    fi

    # Learned-workflow layer (replaced the retired detect-patterns/measure-rule-fitness
    # loop): grow the correction corpus from recent JSONL (detect mode; no embedding).
    # Spec: tasks/specs/spec-learned-workflow-layer-2026-06-05.md.
    # UN-GATED 2026-08-05. This site was gated on 2026-07-26 to stop a zombie
    # ".learned-resynth-due" marker from resurfacing in /core-si + the statusline forever
    # (observed re-drop 2026-07-25 01:28, two days after the resynth loop was retired). The
    # marker problem was real; taking the MINER down with it was not the fix. The resynth marker
    # is now suppressed inside learned-corpus-miner.py under the same condition, which leaves
    # this call free to do the one thing only it does: write pattern_observations.
    #
    # Keeping the gate here meant org 1 — the only Core carrying .si-unified-spine — had no
    # automatic corpus writer for 13 days, while friction_loop.py (a pure reader) and
    # bin/correction-rate.py went on reading a table that had silently stopped growing.
    # See session-lifecycle.sh for the full reasoning; do not re-gate either site.
    if [[ -f "$CORE_INSTANCE/scheduling/claude-si/learned-corpus-miner.py" ]]; then
      python3 "$CORE_INSTANCE/scheduling/claude-si/learned-corpus-miner.py" --detect >> "$LOG" 2>&1
      echo "[$(date)] run-brain-update[heavy]: learned-corpus detect exit=$?" >> "$LOG"
    fi
    # MEASURE organ (B2, restored 2026-06-08): after the corpus grows, recompute per-contract
    # recurrence-since-shipped -> .claude/state/contract-fitness.json, which /core-si reads (B3).
    # This is the feedback half the learned-layer pivot dropped; closes the loop.
    if [[ -f "$CORE_INSTANCE/scheduling/claude-si/measure-contract-fitness.py" ]]; then
      python3 "$CORE_INSTANCE/scheduling/claude-si/measure-contract-fitness.py" >> "$LOG" 2>&1
      echo "[$(date)] run-brain-update[heavy]: measure-contract-fitness exit=$?" >> "$LOG"
    fi
    # SI trajectory export — the visible self-improvement surface (graduated/live/new
    # + contract verdicts + pending proposals) Core OS renders on /system (Nick, 2026-07-02).
    if [[ -f "$CORE_INSTANCE/scheduling/claude-si/export-si-trajectory.py" ]]; then
      python3 "$CORE_INSTANCE/scheduling/claude-si/export-si-trajectory.py" >> "$LOG" 2>&1
      echo "[$(date)] run-brain-update[heavy]: export-si-trajectory exit=$?" >> "$LOG"
    fi
    ;;
esac

# Standing brain-health verification — assert reliability invariants every close
# (embedding coverage, GUC, recall-hook liveness, recency-works, etc.) so a
# regressed invariant surfaces at next SessionStart instead of silently rotting.
# Informational: does NOT fail the chain. See tasks/research/brain-reliability-audit-2026-05-31.md.
if [[ -f "$CORE_INSTANCE/scheduling/brain-pg/brain-health.py" ]]; then
  HEALTH_OUT=$(cd "$CORE_INSTANCE/scheduling/brain-pg" && python3 brain-health.py --quiet 2>/dev/null)
  mkdir -p "$CORE_INSTANCE/.claude/state" 2>/dev/null || true
  echo "$HEALTH_OUT" > "$CORE_INSTANCE/.claude/state/.brain-health-status" 2>/dev/null || true
  # Also cache the full per-check JSON so the Core OS Health drill can show WHICH
  # invariant is flagged + its detail (instant read, no live DB round-trip on click).
  (cd "$CORE_INSTANCE/scheduling/brain-pg" && python3 brain-health.py --json 2>/dev/null) \
    > "$CORE_INSTANCE/.claude/state/.brain-health.json" 2>/dev/null || true
  echo "[$(date)] run-brain-update[$MODE]: $HEALTH_OUT" >> "$LOG"
fi

# Connectivity drift-gate (2026-07-06, brain-connectivity fix): after embed,
# measure per-org graph isolation and flag .brain-connectivity-degraded on
# regression (>3pp rise or >50% ceiling) so it surfaces at next SessionStart
# instead of silently rotting. Informational — does NOT fail the chain.
# See tasks/research/brain-connectivity-2026-07-03.md.
if [[ -f "$CORE_INSTANCE/scheduling/brain-pg/connectivity-report.py" ]]; then
  (cd "$CORE_INSTANCE/scheduling/brain-pg" && CORE_INSTANCE="$CORE_INSTANCE" python3 connectivity-report.py --gate --quiet >> "$LOG" 2>&1) || true
  echo "[$(date)] run-brain-update[$MODE]: connectivity gate ran" >> "$LOG"
fi

# Surface failures loudly: write a marker the SessionStart check reads, so a
# non-zero chain can never again pass silently. Cleared on a clean run.
BRAIN_FAIL_MARKER="$CORE_INSTANCE/.claude/state/.brain-update-failed"
if [[ "$CHAIN_EXIT" -ne 0 ]]; then
  mkdir -p "$CORE_INSTANCE/.claude/state" 2>/dev/null || true
  {
    echo "FAILED_AT=$(date +%Y-%m-%dT%H:%M:%S%z)"
    echo "MODE=$MODE"
    echo "CHAIN_EXIT=$CHAIN_EXIT"
    echo "EXTRACT_EXIT=${EXTRACT_EXIT:-na}"
    echo "EMBED_EXIT=${EMBED_EXIT:-na}"
    echo "LOG=$LOG"
  } > "$BRAIN_FAIL_MARKER" 2>/dev/null || true
  echo "[$(date)] run-brain-update[$MODE]: FAILED (chain exit=$CHAIN_EXIT) — marker written" >> "$LOG"
else
  rm -f "$BRAIN_FAIL_MARKER" 2>/dev/null || true
fi

echo "[$(date)] run-brain-update[$MODE]: done, chain exit=$CHAIN_EXIT, releasing lock" >> "$LOG"
exit $CHAIN_EXIT
