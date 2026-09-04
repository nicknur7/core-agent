#!/usr/bin/env bash
# SessionStart hook for Core.
# Checks whether the last session closed cleanly and surfaces warnings to Claude.
set -uo pipefail
# Operator name comes from .claude/identity.json (per_core_keep), never hardcoded.
# 2026-08-29: a fresh clone of the public baseline printed "ask Nick" to strangers.
_CORE_USER="$(python3 "${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/.claude/hooks/lib/coreuser.py" 2>/dev/null || echo "the operator")"
[[ -n "$_CORE_USER" ]] || _CORE_USER="the operator"


# ── HOOK SOURCE (2026-06-09 fix) ─────────────────────────────────────────────
# SessionStart fires on startup AND on compact/resume re-fires INSIDE the same
# session. Side effects (clock stamp, truth-drift partition, extraction notice)
# must run once per REAL session — a mid-session re-fire on 06-09 re-stamped
# .session-start and re-partitioned compile-truth-work, destroying in-flight
# refresh out-files twice. Parse the hook's stdin JSON `source` and gate.
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" session-start-check SessionStart "" 2>/dev/null || true

HOOK_SOURCE="startup"
if [ ! -t 0 ]; then
  HOOK_SOURCE=$(cat 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('source') or 'startup')" 2>/dev/null || echo startup)
  [[ -n "$HOOK_SOURCE" ]] || HOOK_SOURCE="startup"
fi

# ── CORE-BUS auto-arm (2026-08-08) ────────────────────────────────────────────────────────
# The operator wanted something that keeps the bus up and running across a session start, so it
# does not need to be re-armed by hand every time.
#
# A hook cannot arm a Monitor — that is an agent-side tool — so the closest honest thing is to
# make arming unmissable and paste-ready on turn one.
#
# It lives INSIDE emit_context on purpose. Appending it near the main path's printf looked
# right and emitted NOTHING on a real SessionStart, because this hook has early-exit paths
# that emit and return long before there. Every exit route goes through this function, so
# this is the only placement that covers all of them.
#
# The brief text lives in the bus (`peer-msg.py session-brief`), NOT here: five copies in five
# per_core_keep hooks is exactly the drift that left the verdict contract at zero on three
# Cores. Fail-open and silent when the bus is absent, so a fork is unaffected.
_bus_brief() {
  local bus_msg="$HOME/AI Projects/core-bus/peer-msg.py"
  [[ -f "$bus_msg" ]] || return 0
  # BOUNDED. sentinel-code flagged this on the push review: a hang in peer-msg.py is not a
  # failure any of the fail-open checks catch, and it would hang SessionStart on EVERY Core that
  # shares this machine's bus. Missing-file and non-zero exit were already fail-open; a hang was
  # not. 5s is generous for a file read.
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 python3 "$bus_msg" session-brief 2>/dev/null || true
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout 5 python3 "$bus_msg" session-brief 2>/dev/null || true
  else
    python3 "$bus_msg" session-brief 2>/dev/null || true
  fi
}

# Any rule claiming a hook enforces something, checked against what is actually registered.
#
# WHY AT SESSION START, and not at close. On 2026-08-09 three claims in rules/memory.md and one
# in CLAUDE.base.md promised gates that had not run since the 2026-08-06 retirement — including
# `time-claim-gate.sh` in the SHARED baseline, which every Core loaded on every turn. A false
# promise of a net is the most expensive staleness class there is: you relax against it, and the
# cost shows up only as a mistake nobody caught. Catching that at close means a whole session was
# already spent trusting it. This is supply-side for the same reason the retired gates were
# replaced by supply — tell me the truth BEFORE I rely on it.
#
# Same placement logic as _bus_brief: inside emit_context, because the early-exit paths return
# before the main printf. Silent when clean, fail-open when absent.
_enforcement_brief() {
  local audit="$REPO_GUESS/bin/enforcement-audit.py"
  [[ -f "$audit" ]] || return 0
  # THREE STATES, NOT TWO. core-business blocked the first cut of this wiring on exactly the
  # defect the auditor exists to find: stderr was discarded, `|| true` swallowed the exit code,
  # and the hook spoke only if "UNBACKED" appeared on stdout. So a HEALTHY auditor with nothing
  # to report and a CRASHED auditor that never started were observably IDENTICAL — both silent.
  # On its Core the script could not even import, and the wiring called that a clean bill of
  # health at every session start, forever, until someone ran it by hand.
  #
  # A tool whose purpose is catching rules that promise a net which is not there must not have
  # wiring that promises a net which is not there. Fail-open on STARTUP is right — a crash here
  # must never block a session. Fail-open on the VERDICT is not. Those are separable and this
  # separates them.
  # HANG BOUND, SYMMETRIC WITH _bus_brief (2026-08-28, found by core-business, bus #5571).
  # _bus_brief falls back timeout -> gtimeout -> unbounded; this checked only `timeout` and then went
  # straight to unbounded, so one of the two lost its gtimeout branch. On a box with coreutils-as-g*
  # the bus read was bounded and the enforcement audit was not.
  #
  # WHY IT MATTERS NOW: this Mac mini has NEITHER `timeout` NOR `gtimeout` — verified on the machine
  # the whole fleet was migrated onto today. So both calls currently run unbounded here, and a
  # `command -v` guard degrades SILENTLY: it never reports that the bound it promises is absent.
  # That is the same shape as a dead counter — a protection that publishes nothing when it is off.
  local out rc
  if command -v timeout >/dev/null 2>&1; then
    out="$(timeout 5 python3 "$audit" 2>&1)"; rc=$?
  elif command -v gtimeout >/dev/null 2>&1; then
    out="$(gtimeout 5 python3 "$audit" 2>&1)"; rc=$?
  else
    # No bound available on this host. Say so ONCE rather than degrading in silence — the operator
    # decision (install coreutils on the shared host) is not a code fix and cannot be made by a
    # guard that never speaks.
    printf 'ℹ️  no timeout/gtimeout on this host — enforcement audit and bus read run UNBOUNDED\n'
    out="$(python3 "$audit" 2>&1)"; rc=$?
  fi

  if [[ $rc -gt 1 ]] || { [[ $rc -ne 0 ]] && ! grep -q "UNBACKED" <<<"$out"; }; then
    # Exit 1 WITH findings is the audit working. Anything else non-zero, or an empty result from
    # a file that exists, is the audit failing to run — which is not a passing audit.
    printf '⚠️  ENFORCEMENT AUDIT COULD NOT RUN (exit %s) — enforcement claims are UNVERIFIED this session, not verified clean.\n    %s\n' \
      "$rc" "$(head -3 <<<"$out" | tr '\n' ' ')"
    return 0
  fi
  if [[ -z "${out//[[:space:]]/}" ]]; then
    printf '⚠️  ENFORCEMENT AUDIT PRODUCED NO OUTPUT — treat as UNVERIFIED, not clean.\n'
    return 0
  fi
  # Only speak when something is unbacked. A clean audit is not news.
  grep -q "UNBACKED" <<<"$out" 2>/dev/null || return 0
  printf '⚠️  UNBACKED ENFORCEMENT CLAIM — a loaded rule promises a hook that is registered in nothing.\n%s\n' \
    "$(grep -A2 "UNBACKED" <<<"$out" | head -20)"
}

# Emit hook JSON and exit — used by the early-exit paths below and the main path.
emit_context() {
  # REPO is set well below this function; the early-exit paths can reach emit_context before
  # that assignment, so resolve independently rather than depending on statement order.
  local REPO_GUESS
  REPO_GUESS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  local _bb; _bb="$(_bus_brief)"
  local _eb; _eb="$(_enforcement_brief)"
  [[ -n "$_eb" ]] && set -- "$1"$'\n'"$_eb"$'\n'
  [[ -n "$_bb" ]] && set -- "$1"$'\n'"$_bb"$'\n'
  printf '%s' "$1" | python3 -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': sys.stdin.read()
    }
}))"
  exit 0
}

for cmd in python3 git; do
  command -v "$cmd" >/dev/null 2>&1 || echo "[$(basename "$0")] WARN: $cmd not found — some checks may be skipped" >&2
done

REPO=$(git rev-parse --show-toplevel 2>/dev/null || echo "${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}")
# shellcheck source=bin/core-paths.sh
source "$REPO/bin/core-paths.sh"
# Per-worktree narrative file: school worktree uses current-state-school.md
# (gitignored per-worktree refactor 2026-05-12); main uses current-state.md.
CURR_BR=$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [[ "$CURR_BR" == "school" && -f "$REPO/memory/current-state-school.md" ]]; then
  STATE_FILE="$REPO/memory/current-state-school.md"
else
  STATE_FILE="$CORE_MEM_CURRENT_STATE"
fi
STATE_FILE_REL="memory/$(basename "$STATE_FILE")"
SESSIONS_DIR="$CORE_SESSIONS_DIR"
NOW=$(date +%s)
TWELVE_HOURS=43200
WARNINGS=()

# ── CURRENT SESSION CLOCK CAPTURE ────────────────────────────────────────────
# Stamp this session's start time so Claude has a structurally-sourced anchor
# instead of guessing "we started around X." Different file from
# .last-session-start (which stop-hook writes at /close-core time for the
# just-ended session). This file is for the SESSION JUST STARTING.
# Fix for 2026-05-15 spec-time-awareness-fix Phase 1.
mkdir -p "$CORE_STATE_DIR" 2>/dev/null || true
NOW_HUMAN=$(date +"%Y-%m-%d %H:%M %Z")
SESSION_START_EPOCH=$NOW
SESSION_LOCK="$CORE_STATE_DIR/.session-lock"

# Non-startup re-fire (compact/resume): never re-stamp the clock, never run
# side effects. Surface a clock refresh and stop.
if [[ "$HOOK_SOURCE" != "startup" ]]; then
  EXISTING_START=$(head -1 "$CORE_SESSION_START" 2>/dev/null || echo "(unknown)")
  emit_context "SESSION-START RE-FIRE (source=${HOOK_SOURCE}) — side effects skipped (clock stamp, truth-drift partition, extraction notice are one-time-per-session). Session started ${EXISTING_START}; current time ${NOW_HUMAN}. TIME-CLAIM RULE still applies: re-run \`date\` for any clock claim."
fi

# One-Core-at-a-time lock (2026-06-09): a second live session of THIS Core must not
# re-run side effects against shared state. PID of the live claude process is a
# good-enough identity; a dead-PID lock is stale and gets taken over.
#
# The Core name and the operator name are RESOLVED, not hardcoded (2026-08-29). This string is
# emitted to the user, and it used to read "SECOND core-life SESSION" and "Tell Nick to close one
# window" on every seat — so a forked Core named the wrong repo and addressed the wrong person in
# the one message whose whole job is telling you which window to close. Same class as the
# coreuser.py fix below; this site was missed because it is a runtime string, not steering prose.
LOCK_PID=$(head -1 "$SESSION_LOCK" 2>/dev/null || true)
if [[ -n "$LOCK_PID" && "$LOCK_PID" != "$PPID" ]] && kill -0 "$LOCK_PID" 2>/dev/null; then
  EXISTING_START=$(head -1 "$CORE_SESSION_START" 2>/dev/null || echo "(unknown)")
  _CORE_NAME=$(python3 -c 'import json,os,sys
try:
    d=json.load(open(os.environ["CORE_IDENTITY_JSON"]))
    print(d.get("core_slug") or d.get("core_label") or os.path.basename(os.environ.get("CORE_INSTANCE","")) or "this Core")
except Exception:
    print(os.path.basename(os.environ.get("CORE_INSTANCE","")) or "this Core")' 2>/dev/null || echo "this Core")
  _OPERATOR=$(python3 "$CORE_INSTANCE/.claude/hooks/lib/coreuser.py" 2>/dev/null || echo "the operator")
  emit_context "⛔ SECOND ${_CORE_NAME} SESSION DETECTED — a live session (lock PID ${LOCK_PID}, started ${EXISTING_START}) already holds .claude/state/.session-lock. One Core at a time: this window runs WITHOUT session side effects (no clock stamp, no truth-drift partition, no extraction, no resynth). Tell ${_OPERATOR} to close one window. Concurrent memory/ writes WILL clobber each other."
fi
echo "$PPID" > "$SESSION_LOCK" 2>/dev/null || true

SESSION_START_HUMAN="$NOW_HUMAN"
echo "$SESSION_START_HUMAN" > "$CORE_SESSION_START" 2>/dev/null || true

# SL fix (2026-07-17): a genuine new session has not been explicitly closed yet — clear
# the full-close marker (set by /close-core in session-lifecycle.sh) so the defensive
# close downgrades the current-state stamp correctly on a true walk-away. ONLY on real
# startup: a compact/resume re-fire must NOT clear a marker a /close-core set earlier
# this same session. (This is the SessionStart hook that actually runs — lifecycle_start
# is never wired here, verified 2026-07-17.)
if [[ "$HOOK_SOURCE" == "startup" || "$HOOK_SOURCE" == "clear" ]]; then
  # SESSION-SCOPED MARKERS (2026-07-25) — DO NOT clear by a global name here.
  #
  # This block used to `rm -f .full-close-this-session` / `.brain-synced-this-session`. On the
  # documented workflow (`/close-core` THEN `/clear`) the INCOMING session's SessionStart fires
  # BEFORE the OUTGOING session's SessionEnd — so this deletion removed the marker the outgoing
  # close still needed. Its trailing-no-op branch then could not fire: every clean close was
  # followed by a full redundant defensive save that re-ran every generator and re-stamped
  # current-state.md as "(defensive-save — no explicit close)". Observed 2026-07-24 11:23→11:25.
  #
  # Markers are now keyed to the session that owns them (.full-close-<sid> / .brain-synced-<sid>,
  # written by session-lifecycle.sh from each hook's OWN stdin payload). A new session cannot
  # collide with a prior session's marker, so nothing needs clearing for correctness — only
  # garbage collection. Age-based, never name-based: deleting "the" marker is what broke this.
  find "$CORE_STATE_DIR" -maxdepth 1 -type f \
       \( -name '.full-close-*' -o -name '.brain-synced-*' \) -mtime +2 \
       -delete 2>/dev/null || true
  # Legacy global-name markers from before the 2026-07-25 rename: harmless once the fallback
  # path stops being taken, but clear them on startup so a stale one can never be read as a
  # current claim by a caller that failed to parse its session id.
  rm -f "$CORE_STATE_DIR/.full-close-this-session" "$CORE_STATE_DIR/.brain-synced-this-session" 2>/dev/null || true
fi

# Reconcile baseline (2026-07-17): capture the in-scope content inventory at genuine
# session start so close-reconciliation has a start→close changeset to reconcile
# against (Codex-specified: content inventory, not git diff). Fail-open. Startup/clear
# only — a compact/resume re-fire must keep the ORIGINAL baseline for correct attribution.
if [[ "$HOOK_SOURCE" == "startup" || "$HOOK_SOURCE" == "clear" ]] && [[ -f "$REPO/bin/reconcile-inventory.py" ]]; then
  CORE_INSTANCE="$REPO" python3 "$REPO/bin/reconcile-inventory.py" capture >/dev/null 2>&1 || true
fi

# Reconcile enforcement (2026-07-17): on genuine start, clear this session's stale reconcile
# evidence (fresh session), and if LAST session left in-scope work unreconciled (a walk-away
# has no model to reconcile), surface it LOUD — the close gate refuses a clean close until it
# clears. .reconcile-pending is NOT cleared here; it's cleared when reconcile-receipt.py writes.
if [[ "$HOOK_SOURCE" == "startup" || "$HOOK_SOURCE" == "clear" ]]; then
  rm -f "$CORE_STATE_DIR/.reconcile-ran" "$CORE_STATE_DIR/.reconcile-report" "$CORE_STATE_DIR/.reconcile-receipt.json" 2>/dev/null || true
  if [[ -f "$CORE_STATE_DIR/.reconcile-pending.json" ]]; then
    _pend=$(python3 -c "import json;print(json.load(open('$CORE_STATE_DIR/.reconcile-pending.json')).get('changeset',{}).get('total','?'))" 2>/dev/null || echo "?")
    WARNINGS+=("⚠ UNRECONCILED WORK carried from last session: ${_pend} in-scope file(s) changed but never reconciled (last session walked away without reconciling). Spawn close-reconciler, disposition its findings, run \`python3 bin/reconcile-receipt.py write\` — the close gate will refuse a clean /close-core until this clears. Delta: .claude/state/.reconcile-pending.json")
  fi
fi
# Previous session's start (written by stop-hook on /close-core) if present.
PREV_START=""
if [[ -f "$CORE_LAST_SESSION_START" ]]; then
  PREV_START=$(head -1 "$CORE_LAST_SESSION_START" 2>/dev/null | tr -d '\n')
fi

# Hygiene: sweep stale Sentinel approval tokens (TTL 120s; orphan tokens from
# unmatched approve attempts otherwise accumulate forever in .claude/hooks/).
# Silent — no warning surfaced; this is housekeeping not a failure mode.
find "$REPO/.claude/state" -maxdepth 1 -name '.sentinel-approved-*' -mmin +5 -delete 2>/dev/null

# Hygiene: sweep stale rot-score + brain-recall cache files (>=7d old).
# These accumulate one per session-hash / one per Claude Code UUID forever.
# Audit 2026-05-22 (H1): 87 rot-score + 13 brain-recall files in state dir.
# Per-session files are useful for the active session only; week-old is dead.
# Note: find -mtime +6 means strictly more than 6 days, i.e. 7+ days old.
find "$REPO/.claude/state" -maxdepth 1 -name '.rot-score-*.json' -mtime +6 -delete 2>/dev/null
find "$REPO/.claude/state" -maxdepth 1 -name '.brain-recall-*.json' -mtime +6 -delete 2>/dev/null

# Check (ag): Sentinel agent registration (2026-07-16). The security reviewers
# `sentinel` + `sentinel-code` register ONLY from flat .claude/agents/<name>.md
# files — the directory form .claude/agents/<name>/CLAUDE.md is SILENTLY IGNORED by
# the Claude Code subagent loader (verified via claude-code-guide; the loader reads
# flat *.md, not <name>/CLAUDE.md). On 2026-06-25 the Core-partitioning close
# (fb8c8fc) deleted the flat files, silently disabling sentinel-code fleet-wide
# (dir form left behind looked valid) until each Core was hand-fixed one at a time.
# This makes that regression class LOUD at session open instead of surfacing as an
# "Agent type not found" error mid-outward-action or mid-sync-review.
for _agent in sentinel sentinel-code; do
  _agentmd="$REPO/.claude/agents/${_agent}.md"
  if [[ ! -f "$_agentmd" ]]; then
    _fixhint="cp .claude/agents/${_agent}/CLAUDE.md .claude/agents/${_agent}.md, prepend YAML frontmatter (name/description/tools/model), restart"
    if [[ -f "$REPO/.claude/agents/${_agent}/CLAUDE.md" ]]; then
      WARNINGS+=("SENTINEL REGISTRATION BROKEN: .claude/agents/${_agent}.md is MISSING — the '${_agent}' reviewer will NOT register (the directory form present at .claude/agents/${_agent}/ is silently ignored by the loader). Outward-action / baseline-sync review is DISABLED this session until restored: ${_fixhint}. Regression class: 2026-06-25 partitioning-close deletion (fb8c8fc).")
    else
      WARNINGS+=("SENTINEL REGISTRATION BROKEN: neither .claude/agents/${_agent}.md nor .claude/agents/${_agent}/CLAUDE.md exists — the '${_agent}' reviewer is GONE. Restore it from the baseline or a peer Core before any outward action / sync.")
    fi
  # FRONTMATTER-VALIDITY check (2026-07-18): file-exists is NOT enough. The Claude Code loader
  # registers a subagent type ONLY from a valid YAML frontmatter header (`name:` field). A flat
  # .md with the body but no `---`/`name:` block silently fails to register — [ -f ] passes while
  # the type is 'not found' at spawn. This exact silent failure hid on ALL 4 peer Cores until
  # 2026-07-18 (flat files created from the dir-form CLAUDE.md without frontmatter; per_core_keep
  # meant life's correct version never synced). Validate the header, not just presence.
  elif ! head -1 "$_agentmd" | grep -q '^---[[:space:]]*$' || ! grep -qE "^name:[[:space:]]*${_agent}([[:space:]]|$)" "$_agentmd"; then
    WARNINGS+=("SENTINEL REGISTRATION BROKEN: .claude/agents/${_agent}.md EXISTS but has NO valid YAML frontmatter ('name: ${_agent}') — the loader will NOT register '${_agent}' as a spawnable type (file-exists is not registration). Baseline-sync / outward-action review is DISABLED this session. Fix: prepend the '---' / 'name: ${_agent}' / … / '---' block (copy the header from a Core where it registers), restart. Regression class: 2026-07-18 frontmatter-less flat agent files on peers.")
  fi
done

# Liveness alarm: brain-recall-trigger writes a .brain-recall-<session> file every
# time it fires; rot-check writes a .rot-score per prompt. If recent sessions logged
# rot-scores but ZERO brain-recall fires exist, the recall nudge is likely DEAD — the
# exact silent failure (CORE_INSTANCE dropped from hook env) that went unread for weeks
# and caused reason-from-assumption slips. An empty telemetry set is the alarm; ring it.
ROT_FILES=$(find "$REPO/.claude/state" -maxdepth 1 -name '.rot-score-*.json' 2>/dev/null | wc -l | tr -d ' ')
RECALL_FILES=$(find "$REPO/.claude/state" -maxdepth 1 -name '.brain-recall-*.json' 2>/dev/null | grep -vc 'test')
if (( ROT_FILES >= 2 )) && (( RECALL_FILES == 0 )); then
  WARNINGS+=("brain-recall hook may be DEAD: ${ROT_FILES} rot-score telemetry file(s) but 0 brain-recall fires (last 7d). The claude-brain recall nudge is not firing — check brain-recall-trigger.py self-resolves CORE_INSTANCE (post-mortem: tasks/research/brain-recall-dead-hook-postmortem-2026-05-31.md).")
fi

# Brain-health: surface a regressed reliability invariant from the last close.
# brain-health.py runs at every brain-update close and writes a one-line status
# here; a FAIL means a verified invariant broke (dead hook, recency, coverage).
BRAIN_HEALTH_STATUS="$REPO/.claude/state/.brain-health-status"
# Fire ONLY on a non-zero FAIL count. The old `grep -q 'FAIL'` matched the substring
# "FAIL" inside "0 FAIL", so a clean report (e.g. "11 PASS · 1 WARN · 0 FAIL") tripped its
# own regression alarm every session (cry-wolf — D2, 2026-06-08). WARN is a tracked open
# defect, not a regression; only a real FAIL (>=1) means a verified invariant broke.
if [[ -f "$BRAIN_HEALTH_STATUS" ]] && grep -qE '[1-9][0-9]* FAIL' "$BRAIN_HEALTH_STATUS" 2>/dev/null; then
  WARNINGS+=("BRAIN HEALTH REGRESSION: $(cat "$BRAIN_HEALTH_STATUS"). Run \`python3 scheduling/brain-pg/brain-health.py\` for the full report — a verified invariant broke.")
fi

# Connectivity drift (brain-connectivity fix, 2026-07-06): the drift-gate in
# run-brain-update writes this marker when per-org graph isolation regressed
# since the last brain update. Cleared automatically on recovery.
BRAIN_CONN_DEGRADED="$REPO/.claude/state/.brain-connectivity-degraded"
if [[ -f "$BRAIN_CONN_DEGRADED" ]]; then
  WARNINGS+=("BRAIN CONNECTIVITY DRIFT: $(head -1 "$BRAIN_CONN_DEGRADED"). Run \`python3 scheduling/brain-pg/connectivity-report.py\` — graph isolation rose since last update.")
fi

# Deferred brain-update (2026-07-18): run-brain-update.sh skipped on a busy/unreclaimable lock and
# RECORDED the miss instead of silently exit-0'ing (the old behavior lost capture debt with no trace).
# Surface it so the lag is visible; a successful run clears the marker.
BRAIN_UPDATE_DEFERRED="$REPO/.claude/state/.brain-update-deferred"
if [[ -f "$BRAIN_UPDATE_DEFERRED" ]]; then
  _defn=$(wc -l < "$BRAIN_UPDATE_DEFERRED" 2>/dev/null | tr -d ' ')
  WARNINGS+=("BRAIN UPDATE DEFERRED: ${_defn} brain-update run(s) were skipped on a busy lock and not yet retried (last: $(tail -1 "$BRAIN_UPDATE_DEFERRED" 2>/dev/null)). Capture may lag — the next close resolves it, or re-run run-brain-update.sh fast.")
fi

# CONSOLIDATION DUE (2026-08-05, Phase C): the close segmented the last sessions and found work
# windows that ended in ACCEPTANCE rather than a correction — candidate Workflows. The close can
# only PREPARE them (mechanical); the extraction is a judgement pass and the write must be verified,
# so both happen in-session. Surfaced here because a marker nothing reads is the exact defect that
# froze the correction corpus for 13 days: the writer ran, the reader never existed, and everything
# downstream looked healthy. If this warning is ever absent while consolidate-pending.json has
# windows, the surfacing is broken, not the pass.
CONSOLIDATE_DUE="$REPO/.claude/state/.consolidate-due"
if [[ -f "$CONSOLIDATE_DUE" ]]; then
  WARNINGS+=("CONSOLIDATION DUE — $(head -1 "$CONSOLIDATE_DUE" 2>/dev/null) This is how Core learns what WORKED rather than only what went wrong; the corrective miner covers the failure half. Run: python3 scheduling/brain-pg/consolidate_sessions.py --prepare, do the extraction against payload['brief'], then --apply. Disable with CORE_CONSOLIDATE_OFF=1.")
fi

# C2 (2026-07-23, Codex #1): SELF-HEAL a brain-update that DIED MID-CHAIN. run-brain-update.sh writes
# .brain-update-inflight on start and clears it via its EXIT trap on any graceful exit, so the marker
# survives ONLY an ungraceful death (SIGKILL / power loss / terminal killed) — the exact case brain_status
# can't detect (the session reads 'captured' but extraction/merge/embed never finished, so status shows
# READY). If the marker is present AND no brain-pipeline process is running, the prior close died mid-update
# → clear its stale lock and RESUME in the background (every stage is idempotent). This makes "crash → next
# start finishes it" REAL rather than a warning nobody acts on. NOTE (2026-07-24): `fast` is now
# DETERMINISTIC (JSONL export + embed) — this resume recovers CAPTURE+EMBED, NOT graph extraction
# (which needs a live Agent()). A pending graph extraction is surfaced separately below by
# `extract-pending --phase start` for the session to drain in-session; it is NOT auto-run here.
INFLIGHT="$REPO/.claude/state/.brain-update-inflight"
if [[ ( "$HOOK_SOURCE" == "startup" || "$HOOK_SOURCE" == "clear" ) && -f "$INFLIGHT" ]]; then
  if pgrep -f "run-brain-update.sh|merge.py|embed.py" >/dev/null 2>&1; then   # extract-headless.py retired 2026-07-24
    WARNINGS+=("BRAIN UPDATE IN PROGRESS: the last close's brain-update is still running — recall may briefly lag until it finishes.")
  else
    _bhash=$(echo "${CORE_BRAIN:-$HOME/AI Projects/core-brain}" | md5 -q 2>/dev/null || echo "${CORE_BRAIN:-x}" | md5sum | cut -d' ' -f1)
    rm -rf "/tmp/core-brain-${_bhash}.lock" 2>/dev/null || true   # clear the dead worker's stale lock so resume can run
    ( CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
        nohup bash "$CORE_HOOK_RUN_BRAIN_UPDATE" fast >/dev/null 2>&1 & ) 2>/dev/null || true
    WARNINGS+=("BRAIN CAPTURE RESUMED: the last close's brain-update died mid-chain (power loss / killed) — auto-resuming CAPTURE+EMBED in the background. If graph extraction was also pending, the pending-extraction notice below will surface it to drain in-session.")
  fi
fi

# CORPUS INGEST ON SESSION START (2026-08-20) — the fleet's only universal ingest door.
#
# Measured 08-20: every Core's corpus was frozen 70-92h. Ingest had two doors, a session close
# and the 02:00 nightly, and the nightly exists on EXACTLY ONE SEAT — com.nick.brain-pipeline is
# pinned CORE_INSTANCE=core-life, CORE_ORG_ID=1. business/school/finance/ops have no scheduled
# path at all, so a seat Nick opens and walks away from ingests nothing, ever.
#
# WHY HERE AND NOT FOUR MORE LAUNCHD JOBS. Installing a plist per seat would be four new parallel
# mechanisms for one 2.15s script, on a fleet whose standing directive is to unify rather than add
# a second path beside the first. This hook already runs on all five seats and .claude/hooks/ is
# shared, so the fix propagates on the next pull with no system-level install and nothing for Nick
# to approve on his machine.
#
# COST: none on the session. Backgrounded exactly like the resume above, so start latency is
# unchanged. Measured 2.15s standalone; nothing waits on it.
#
# SAFE TO RACE a concurrent close's miner: detect() is a full deduped re-scan and duplicates are
# structurally impossible behind uq_patobs_source_label_org (added 2026-08-05 after Codex found
# in-process dedup could double-insert). A redundant run inserts zero rows.
#
# startup|clear ONLY — a compact re-fire is not a new session and must not trigger side effects,
# which is the same gate the two blocks above use.
if [[ ( "$HOOK_SOURCE" == "startup" || "$HOOK_SOURCE" == "clear" ) \
      && -f "$REPO/scheduling/claude-si/learned-corpus-miner.py" ]]; then
  # CORE_ORG_ID is deliberately NOT passed. `_env.py` resolves org from this Core's own
  # identity.json and identity ALWAYS wins over the variable — so a value here cannot
  # mis-route a write, but a WRONG one (`${CORE_ORG_ID:-1}` defaults to LIFE's org) makes
  # every peer print "CORE_ORG_ID=1 but identity.json says org_id=N" into this log on every
  # session start, forever. Passing nothing is both correct and quiet. Note the resume block
  # above still uses the :-1 default; that path hands the value to run-brain-update.sh rather
  # than to _env.py, so it is not the same call and was left alone rather than changed blind.
  ( CORE_INSTANCE="$REPO" \
      nohup python3 "$REPO/scheduling/claude-si/learned-corpus-miner.py" --detect \
      >> "/tmp/corpus-ingest-$(basename "$REPO")-$(date +%F).log" 2>&1 & ) 2>/dev/null || true
fi

# Typed brain STATUS (unified redesign step 3): compares the ledger (what's captured) against DISK
# reality + the job queue — the self-announcing staleness the old brain-vs-brain freshness-gate
# could NEVER see (it flashed green while whole sessions rotted). Surfaces LAGGING/FAILED/UNAVAILABLE
# loudly; DB-down is UNAVAILABLE, never 'fresh'. Fail-open (startup only, side-effect-free read).
if [[ "$HOOK_SOURCE" == "startup" || "$HOOK_SOURCE" == "clear" ]] && [[ -f "$REPO/scheduling/brain-pg/brain_status.py" ]]; then
  BSTATUS_JSON=$(CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" python3 "$REPO/scheduling/brain-pg/brain_status.py" --json 2>/dev/null)
  if [[ -n "$BSTATUS_JSON" ]]; then
    BOVERALL=$(printf '%s' "$BSTATUS_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('overall',''))" 2>/dev/null || echo "")
    BDETAIL=$(printf '%s' "$BSTATUS_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('detail',''))" 2>/dev/null || echo "")
    if [[ -n "$BOVERALL" && "$BOVERALL" != "READY" ]]; then
      WARNINGS+=("BRAIN STATUS [${BOVERALL}]: ${BDETAIL}")
    fi
  fi
fi

# Legacy-retirement readiness surface (unified redesign step 5): shows progress toward the auto-sweep
# ("N/10 clean cycles" → "READY — run /retire-legacy"), so the retirement of the superseded parts
# (freshness-gate etc.) is impossible to forget and fires only once the new system is proven. Fail-open.
if [[ "$HOOK_SOURCE" == "startup" || "$HOOK_SOURCE" == "clear" ]] && [[ -f "$REPO/bin/retire-legacy.py" ]]; then
  RETIRE_LINE=$(CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" python3 "$REPO/bin/retire-legacy.py" --status 2>/dev/null)
  [[ -n "$RETIRE_LINE" ]] && WARNINGS+=("$RETIRE_LINE")
fi

# Check (a): current-state.md staleness
if [[ -f "$STATE_FILE" ]]; then
  HEADER_DATE=$(grep -m1 '^Last updated:' "$STATE_FILE" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)
  HEADER_EPOCH=0
  if [[ -n "$HEADER_DATE" ]]; then
    HEADER_EPOCH=$(date -j -f "%Y-%m-%d" "$HEADER_DATE" "+%s" 2>/dev/null || echo 0)
  fi
  FILE_MTIME=$(stat -f "%m" "$STATE_FILE" 2>/dev/null || echo 0)
  BEST_TS=$(( HEADER_EPOCH > FILE_MTIME ? HEADER_EPOCH : FILE_MTIME ))
  AGE=$(( NOW - BEST_TS ))
  if (( AGE > TWELVE_HOURS )); then
    HOURS=$(( AGE / 3600 ))
    WARNINGS+=("${STATE_FILE_REL} is ~${HOURS}h old — may be stale. Update before diving in.")
  fi
else
  WARNINGS+=("${STATE_FILE_REL} not found.")
fi

# Check (g): PERSONAL/PROJECT MEMORY FILES OLDER THAN 30 DAYS.
#
# `.claude/rules/memory.md` has told every Core for weeks that "SessionStart (Check g) warns at
# >30d" and describes exactly how to read the warning. THERE WAS NO SUCH CHECK. The only staleness
# warning in this hook is the current-state.md hours check above.
#
# That is the failure CLAUDE.base.md names in its own words — *a rule that tells you a gate will
# catch you when no gate runs is worse than silence, because you relax against a net that was taken
# down* — and I made it worse today by COMPRESSING that sentence in rules/memory.md without checking
# whether the mechanism behind it existed. A false claim, tightened rather than verified.
#
# Building it rather than deleting the claim, because the claim describes something worth having:
# on this Core right now three files are >30d, the oldest at 43 days.
#
# The no-stamp clause is here for a design reason, not a current one. A file with no
# `Last updated:` line is invisible to any age check, so the LEAST-maintained files would be the
# ones that escape scrutiny — exactly backwards. (I first reported five such files here; that was my
# own bad measurement, a 600-character read window with an unanchored pattern. Re-measured properly:
# zero. The clause stays because the hole is real even when it is empty.)
#
# ONE LINE, and only when there is something to say. A warning that appears every session becomes
# furniture, which is the same way a gate that fires on noise gets silenced.
if [[ -d "$REPO/memory" ]]; then
  MEM_STALE=0; MEM_NOSTAMP=0; MEM_OLDEST=""; MEM_OLDEST_D=0
  while IFS= read -r _mf; do
    case "$_mf" in *"/archive/"*) continue ;; esac
    grep -q '^Status: archived' "$_mf" 2>/dev/null && continue
    _md=$(grep -m1 '^Last updated:' "$_mf" 2>/dev/null | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)
    if [[ -z "$_md" ]]; then
      MEM_NOSTAMP=$(( MEM_NOSTAMP + 1 )); continue
    fi
    _me=$(date -j -f "%Y-%m-%d" "$_md" "+%s" 2>/dev/null || echo 0)
    [[ "$_me" -eq 0 ]] && continue
    _days=$(( (NOW - _me) / 86400 ))
    if (( _days > 30 )); then
      MEM_STALE=$(( MEM_STALE + 1 ))
      if (( _days > MEM_OLDEST_D )); then
        MEM_OLDEST_D=$_days; MEM_OLDEST="${_mf#$REPO/}"
      fi
    fi
  done < <(find "$REPO/memory" -maxdepth 2 -name '*.md' \
             \( -path '*/relationships/*' -o -path '*/projects/*' -o -name 'about-me.md' \
                -o -name 'preferences.md' -o -name 'goals.md' -o -name 'skills-interests.md' \) \
             2>/dev/null)
  if (( MEM_STALE > 0 || MEM_NOSTAMP > 0 )); then
    _msg="Stale memory files (>30d): ${MEM_STALE}"
    (( MEM_STALE > 0 )) && _msg="${_msg} (oldest ${MEM_OLDEST} at ${MEM_OLDEST_D}d)"
    (( MEM_NOSTAMP > 0 )) && _msg="${_msg}; ${MEM_NOSTAMP} carry no 'Last updated:' stamp and are invisible to this check"
    _msg="${_msg}. Facts may have drifted — verify with $_CORE_USER before quoting these as current."
    WARNINGS+=("$_msg")
  fi
fi

# Check (b): commits on main since the previous /close-core anchor.
# Was previously gated on current-state.md "Last updated:" header date, but
# current-state.md is gitignored — its header has no relationship to the
# tracked git timeline, so the prior version fired meaningless warnings
# every session. 2026-05-22 fix (B5): use .last-session-start instead, which
# is the authoritative previous-close anchor (written by stop-hook on the
# /close-core path via get-session-start-time.sh).
if [[ -n "$PREV_START" ]]; then
  PREV_DATE=$(echo "$PREV_START" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)
  PREV_TIME=$(echo "$PREV_START" | grep -oE '[0-9]{2}:[0-9]{2}' | head -1)
  if [[ -n "$PREV_DATE" && -n "$PREV_TIME" ]]; then
    COMMIT_COUNT=$(git -C "$REPO" log main --oneline --since="$PREV_DATE $PREV_TIME" 2>/dev/null | wc -l | tr -d ' ')
    # Suppress the noisy 1-2 commit case (defensive-save + access-log churn
    # routinely produces 1-2 commits per close cycle). Surface only when
    # the count looks like real divergence (>=3) suggesting work happened
    # in another worktree or outside the normal close path.
    if (( COMMIT_COUNT >= 3 )); then
      LATEST=$(git -C "$REPO" log main --oneline -1 2>/dev/null)
      WARNINGS+=("${COMMIT_COUNT} commit(s) on main since prior /close-core (${PREV_START}) — latest: ${LATEST}. May indicate parallel-worktree work.")
    fi
  fi
fi

# Check (c): missing session log for most recent git activity date
LAST_COMMIT_DATE=$(git -C "$REPO" log --format="%as" -1 2>/dev/null || true)
if [[ -n "$LAST_COMMIT_DATE" ]]; then
  SESSION_FILE="$SESSIONS_DIR/${LAST_COMMIT_DATE}.md"
  if [[ ! -f "$SESSION_FILE" ]]; then
    WARNINGS+=("No session log at sessions/${LAST_COMMIT_DATE}.md — last git activity was $LAST_COMMIT_DATE.")
  fi
fi

# Check (sk): self-knowledge currency (2026-06-09) — surfaces the unwireable residue
# (CLAUDE.md prose drift, missing stamps). capabilities.md itself is regenerated at
# close, so this is the thin backstop, not the mechanism. Fail-open.
if [[ -f "$REPO/bin/check-self-knowledge.py" ]]; then
  SK_OUT=$(CORE_INSTANCE="$REPO" python3 "$REPO/bin/check-self-knowledge.py" --quiet 2>/dev/null)
  if [[ $? -eq 10 && -n "$SK_OUT" ]]; then
    WARNINGS+=("Self-knowledge drift — $(echo "$SK_OUT" | tr '\n' ' ' | sed 's/  */ /g'). The Core's 'what it is/does' is behind reality; refresh before relying on it.")
  fi
fi

# ── SI CHECKS FOLDED + DEAD CHECKS DELETED (S3, 2026-05-27) ───────────────────
# This hook used to run 17 checks (a–q) inline. They were split three ways
# (full liveness audit: tasks/specs/spec-core-si-2026-05-26.md §3):
#
#   KEEP   (a)(b)(c) above — operational session-hygiene nags, not SI signal.
#   FOLD   (n)(q)(k)(m)(g)(p)(o)(d5)(d6)(l) → now detected by the ONE engine,
#          scheduling/core-si/detect.sh, and surfaced as the ranked core-si
#          table below. Detection LOGIC moved verbatim; only the surface
#          changed (one ranked table instead of 10 scattered warning lines).
#   DELETE (d)(f)(j)(d4) — DEAD: each pointed at a source that died and never
#          re-fired (re-probed 2026-05-27, all still silent):
#            (d)  sister-project markers — only sister job_hunter is paused;  # privacy-ok: 'sister project' is a codebase term for a sibling project, not a family relation
#            (f)  job-hunter missed-run — no LaunchAgent plist on disk;
#            (j)  brain dispatch — read scheduling/graphify-brain/dispatch-pending-brief.md
#                 which doesn't exist (the live brief lives under tasks/, read on demand);
#            (d4) canvas/HW — canvas-*.md live in core-school, not life.
#          This dead-check class is exactly what detect.sh's LIVENESS layer
#          now catches, so it can't silently re-accrete.
#   (d2)(d3) Obsidian/VSCode markers + (h)(i) cross-worktree survey were
#          removed earlier (2026-05-24 / 2026-05-13); see git history.
#
# core-si table (first-reply lead): run the one engine. It prints the full
# ranked decision table AND refreshes the .core-si-count / .core-si-items.tsv
# cache the statusline badge reads. SILENT when N=0 (no empty table). The same
# engine output is what the /core-si command shows on demand — identical both
# places. Launchd-safe: detect.sh keys off $CORE_INSTANCE, never cwd.
CORE_SI_TABLE=""
CORESI_SH="$REPO/scheduling/core-si/detect.sh"
if [[ -f "$CORESI_SH" ]]; then
  CORE_SI_TABLE=$(CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" bash "$CORESI_SH" 2>/dev/null)
  CORE_SI_N=$(cat "$CORE_STATE_DIR/.core-si-count" 2>/dev/null || echo 0)
  # Silent when clean (N=0) or if the engine errored (non-numeric count).
  if ! [[ "$CORE_SI_N" =~ ^[0-9]+$ ]] || (( CORE_SI_N == 0 )); then
    CORE_SI_TABLE=""
  fi
fi

# Brain compiled_truth_md drift — runs detect+partition; emits MANDATORY
# pre-response refresh directive only when drift > 0. Silent on clean state.
# Wired 2026-05-28 to close the 11-day staleness gap that triggered Nick's
# audit. Subagent fan-out runs in-session via Agent() = Max-sub spend, not API.
BRAIN_TRUTH_DRIFT=""
TRUTH_DRIFT_SH="$REPO/scheduling/brain-pg/session-start-truth-drift.sh"
if [[ -x "$TRUTH_DRIFT_SH" ]]; then
  BRAIN_TRUTH_DRIFT=$(CORE_INSTANCE="$REPO" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" bash "$TRUTH_DRIFT_SH" 2>/dev/null)
fi

# Brain extraction backlog — lightweight NOTICE only at start (Phase 1,
# spec-brain-unfreeze). Mandatory extraction happens at CLOSE (close-core.md);
# here we just surface the un-extracted count so it's visible. Fail-open.
BRAIN_EXTRACT_NOTICE=""
EXTRACT_PENDING_SH="$REPO/scheduling/graphify-brain/extract-pending.sh"
if [[ -x "$EXTRACT_PENDING_SH" ]]; then
  BRAIN_EXTRACT_NOTICE=$(CORE_INSTANCE="$REPO" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" bash "$EXTRACT_PENDING_SH" --phase start 2>/dev/null)
fi

# Two-tier startup hydration (unified redesign step 3, Tier 2): recall recent DECISIONS from the
# brain (supersession-aware — superseded excluded) so the model starts knowing "what did we decide"
# FROM the brain, not just local files. Tier 1 (operational state: reconcile debt, brain status,
# core-si) is the warnings/status above. Suppressed when the store is unavailable. Startup only, read-only.
BRAIN_START_BRIEF=""
if [[ "$HOOK_SOURCE" == "startup" || "$HOOK_SOURCE" == "clear" ]] && [[ -f "$REPO/scheduling/brain-pg/start_brief.py" ]]; then
  BRAIN_START_BRIEF=$(CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" python3 "$REPO/scheduling/brain-pg/start_brief.py" 2>/dev/null)
fi

CLOCK_BLOCK="CLOCK: now = ${NOW_HUMAN} · session_start = ${SESSION_START_HUMAN}"
if [[ -n "$PREV_START" ]]; then
  CLOCK_BLOCK+=$'\n'"  PREV SESSION START (from .last-session-start, last /close-core): ${PREV_START}"
fi
# LAST CLOSE — the close EVENT, not the start of the session that closed (2026-08-28).
# .last-session-start answers "when did the session that last closed begin", which is hours off
# from the close and wrote only on MODE=full. .last-close is written by session-lifecycle.sh on
# EVERY close with its mode, so this line answers the question Nick actually asks.
_LAST_CLOSE_F="${CORE_INSTANCE:-$REPO}/.claude/state/.last-close"
_LAST_FULL_F="${CORE_INSTANCE:-$REPO}/.claude/state/.last-full-close"
if [[ -f "$_LAST_CLOSE_F" ]]; then
  CLOCK_BLOCK+=$'\n'"  LAST CLOSE: $(head -1 "$_LAST_CLOSE_F" 2>/dev/null)"
  if [[ -f "$_LAST_FULL_F" ]]; then
    CLOCK_BLOCK+=$'\n'"  LAST EXPLICIT /close-core: $(head -1 "$_LAST_FULL_F" 2>/dev/null)"
  else
    CLOCK_BLOCK+=$'\n'"  LAST EXPLICIT /close-core: none recorded since the ledger shipped 2026-08-28"
  fi
else
  CLOCK_BLOCK+=$'\n'"  LAST CLOSE: no .last-close yet — ledger ships 2026-08-28; first close will write it"
fi
CLOCK_BLOCK+=$'\n'"  TIME-CLAIM RULE: any 'we worked X hours / started at Y / this morning' statement must be backed by a tool call this turn (date / cat ${CORE_SESSION_START} / JSONL read). The CLOCK line above is a starting anchor, NOT a live signal — re-run \`date\` for current time."

TIER_REMINDER="MODEL CHECK: model identity is best-effort (system prompt + visible /model history). Route by work-type, not tier — judgment/synthesis stays in this session, mechanical bulk can offload. If a routing decision turns on current model, ask $_CORE_USER."

ANTIPATTERN_REMINDER="ANTI-PATTERNS (audited 2026-05-04, see top of CLAUDE.md): (1) Never claim system state from memory — verify with a tool call this turn. (2) When $_CORE_USER references 'X' — first tool call must read X. (3) Never use future-tense action language without doing the action this turn. Run \`bash .claude/scripts/core-status.sh\` whenever you'd be tempted to claim deliver/marker/calendar/pending/JH state from memory."

# Assemble context. Order: clock → tier → core-si table (the lead actionable
# list, silent at N=0) → then either the operational (a)(b)(c) warnings, or —
# when those are clean — the anti-pattern reminder. The core-si table is what
# replaced the 10 folded SI warning lines that used to be emitted here.
CONTEXT="SESSION-START CHECK:"$'\n'
CONTEXT+="  ${CLOCK_BLOCK}"$'\n'
CONTEXT+="  ${TIER_REMINDER}"$'\n'
if [[ -n "$CORE_SI_TABLE" ]]; then
  CONTEXT+=$'\n'"${CORE_SI_TABLE}"$'\n'
fi
if [[ -n "$BRAIN_TRUTH_DRIFT" ]]; then
  CONTEXT+=$'\n'"${BRAIN_TRUTH_DRIFT}"$'\n'
fi
if [[ -n "$BRAIN_EXTRACT_NOTICE" ]]; then
  CONTEXT+=$'\n'"${BRAIN_EXTRACT_NOTICE}"$'\n'
fi
if [[ -n "$BRAIN_START_BRIEF" ]]; then
  CONTEXT+=$'\n'"${BRAIN_START_BRIEF}"$'\n'
fi

# Check (pn): UNREAD PULL NOTES (2026-07-30). A pull already applies files, reconciles hook
# registrations and runs new migrations — all unattended. What it could not do was tell this Core
# what any of it MEANT, so a Core could pull a change needing a one-time adoption, apply every byte
# correctly, and sit in a state that looks broken with nothing having told it what to expect.
#
# docs/PULL-NOTES.md carries that intent and bin/pull-notes.py acts on it. Surfaced here rather
# than run here: SessionStart should inform, and the actions are one command away. A `Needs Operator`
# line never blocks — trust-root approval is un-forgeable by design, not a queue.
if [[ -f "$REPO/bin/pull-notes.py" ]]; then
  PN_BRIEF="$(cd "$REPO" && CORE_INSTANCE="$REPO" python3 bin/pull-notes.py --brief 2>/dev/null || true)"
  [[ -n "$PN_BRIEF" ]] && WARNINGS+=("$PN_BRIEF")
fi
# Check (pa): PENDING ACTIONS FROM THE LAST PULL (2026-08-06). The other half of the pull.
#
# `sync-from-baseline.sh --quiet` runs as a SessionStart hook and applies every shared file, but
# three kinds of change it CANNOT finish on its own: registering a hook (settings.json is
# per_core_keep, so a delivered hook registers nowhere), reviewing a trust-root change (a hook cannot
# invoke Sentinel — which is the safeguard the 2026-07-10 unfreeze assumed, decisions-log:468), and
# anything needing a model. Those are recorded in .pull-pending-actions.json.
#
# This surfaces them FIRST, ahead of the ordinary warnings, because they are the only entries that
# are owed WORK rather than owed attention. Nick's framing on 2026-08-06: the goal is that every Core
# just auto-updates, and "some updates might need the model to actually implement some aspects" — so
# the pull stays frictionless and the leftover lands here, where an agent exists to do it.
PA_FILE="$REPO/.claude/state/.pull-pending-actions.json"
if [[ -f "$PA_FILE" ]]; then
  PA_BRIEF="$(python3 - "$PA_FILE" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
items = d.get("items") or []
if not items:
    sys.exit(0)
parts = []
# Operator name from identity.json — see .claude/hooks/lib/coreuser.py. Never hardcoded:
# a fresh clone of the public baseline used to tell strangers to "ask Nick", so a forked
# Core addressed the wrong person in its own steering. Falls back to "the operator".
try:
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.join(_o.environ.get("CLAUDE_PROJECT_DIR", "."), ".claude/hooks/lib"))
    import coreuser as _cu
    _u = _cu.name()
except Exception:
    _u = "the operator"

for it in items:
    a = it.get("action")
    if a == "apply_manifest":
        parts.append(
            f"The baseline's bin/sync-manifest.json changed and the automatic pull did NOT adopt it "
            f"(baseline {str(it.get('baseline'))[:8]}). That file decides which paths per_core_keep "
            f"protects — including pretooluse-guard, sentinel-approve and sentinel-receipt — and the "
            f"pull reads the CLONED manifest, so a change to the protected list would take effect on "
            f"the same sync that delivers it, unreviewed. This Core kept its existing lists. Adopt it "
            f"with /sync pull, which routes through sentinel-code to {_u}.")
    elif a == "verify_trust_root_bootstrap":
        parts.append(
            "FIRST pull under trust-root hold-back on this Core (baseline "
            f"{str(it.get('baseline'))[:8]}). The hold-back logic ships inside the pull that carries "
            "it, so it could not protect the pull that installed it — any trust-root change delivered "
            "by an EARLIER automatic pull arrived ungated and printed nothing. Diff "
            ".claude/hooks/{pretooluse-guard,sentinel-approve,sentinel-receipt}.sh against a fresh "
            "baseline clone and have sentinel-code read them before relying on the guard. One-time; "
            "the marker means this will not be asked again.")
    # elif, not if — sentinel-code caught this twice. As a separate top-level `if`, a
    # verify_trust_root_bootstrap item matched here AND fell through to the generic `else` below,
    # appending a raw dict-repr after the written message. Cosmetic, but it puts unparsed JSON next to
    # a carefully worded trust-root notice, which is exactly where a reader stops reading.
    elif a in ("apply_trust_root", "review_trust_root"):
        parts.append(f"A trust-root change to {it.get('path')} is WAITING and was deliberately NOT "
                     f"applied by the automatic pull (baseline {str(it.get('baseline'))[:8]}). "
                     f"Everything else in that pull landed. To take it: run /sync pull, which routes "
                     f"through sentinel-code to {_u}. Do NOT apply it any other way — an automatic "
                     f"pull is ungated, and a bad guard installed that way would be the thing "
                     f"judging its own review.")
    elif a == "reconcile_hooks":
        parts.append(f"WIRE the delivered hooks — registration drift after the last pull: "
                     f"{str(it.get('detail'))[:140]}. A hook file arrives via sync; its registration "
                     f"never does (settings.json is per_core_keep).")
    else:
        parts.append(f"{a}: {str(it)[:120]}")
print("PENDING FROM LAST PULL — do these before other work: " + " | ".join(parts))
PY
)"
  [[ -n "$PA_BRIEF" ]] && WARNINGS=("$PA_BRIEF" "${WARNINGS[@]+"${WARNINGS[@]}"}")
fi

if (( ${#WARNINGS[@]} == 0 )); then
  CONTEXT+="  ${ANTIPATTERN_REMINDER}"$'\n'
else
  for W in "${WARNINGS[@]}"; do
    CONTEXT+="  • $W"$'\n'
  done
fi

emit_context "$CONTEXT"
