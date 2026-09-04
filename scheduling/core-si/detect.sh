#!/usr/bin/env bash
# detect.sh — the ONE detection engine for /core-si.
#
# All three core-si surfaces reuse this single script (no duplicated detector
# logic): the /core-si command, the SessionStart first-reply lead, and the
# statusline badge. Detect → rank → emit. It does NOT apply anything (the
# command handles approval + apply); it only surfaces what needs a decision.
#
# Domains: BEHAVIOR (recurring corrections/frustration), RECALL (S2),
#          SYSTEM (drift/staleness), LIVENESS (a producer went dark).
#
# Modes:
#   (default)  print the ranked markdown table + summary; refresh cache
#   --count    print just the integer item count (cheap; for the statusline)
#   --tsv      raw TSV rows: SEV<TAB>DOMAIN<TAB>DETECTED<TAB>FIX<TAB>FITNESS<TAB>KEY
#
# Fail-open: Postgres down -> skip DB detectors, file-based ones still run.
# Spec: tasks/specs/spec-core-si-2026-05-26.md
set -uo pipefail

CORE_INSTANCE="${CORE_INSTANCE:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
# Path registry: source so registry-tracked paths are referenced by constant,
# not hardcoded literal (enforced by bin/lint-code-paths.py). CORE_INSTANCE is
# resolved above and core-paths.sh respects it — stays launchd-safe (no cwd
# dependence). Untracked paths (skills-interests.md, goals.md) remain literal.
[[ -f "${CORE_INSTANCE}/bin/core-paths.sh" ]] && source "${CORE_INSTANCE}/bin/core-paths.sh"
ORG="${CORE_ORG_ID:-1}"
STATE_DIR="${CORE_INSTANCE}/.claude/state"
CACHE_COUNT="${STATE_DIR}/.core-si-count"
CACHE_TSV="${STATE_DIR}/.core-si-items.tsv"
mkdir -p "$STATE_DIR"

MODE="${1:-table}"
ITEMS=()   # each: SEV \t DOMAIN \t DETECTED \t FIX \t FITNESS \t KEY

# COREBRAIN_DB resolver (2026-08-31 fix) — was hardcoded "corebrain"; every other live entry
# point (setup-brain.sh, core-doctor.sh, spawn-core) resolves it the same way.
COREBRAIN_DB="${COREBRAIN_DB:-corebrain}"
psql_q() { psql -d "$COREBRAIN_DB" -t -A -F $'\t' -c "$1" 2>/dev/null; }
have_pg() { command -v psql >/dev/null 2>&1 && psql -d "$COREBRAIN_DB" -t -A -c "SELECT 1;" >/dev/null 2>&1; }

add() { ITEMS+=("$1"$'\t'"$2"$'\t'"$3"$'\t'"$4"$'\t'"$5"$'\t'"$6"); }

# ---- BEHAVIOR — RETIRED 2026-06-05 (learned-workflow layer) ---------------
# The correction-pattern promote/escalate/retire buckets AND the claude-si
# detector-liveness check are retired. The behavior half of SI is now the
# learned-workflow interpretation layer (tasks/specs/spec-learned-workflow-layer-2026-06-05.md):
# corrections are retrieved at prompt time and injected as typed contracts, not
# promoted into rules. Floor enforcement stays in the Stop hooks (say-do-gap /
# state-claim-gate / time-claim-gate) + stop-signal-gate. The old detector
# scripts are archived under scheduling/archive/claude-si-behavior-loop/.
# (Removed the whole `have_pg` block — RECALL/SYSTEM detectors below are
# DB-independent, so this leaves no empty `if` and no orphaned liveness alarm.)

# ---- SYSTEM (file-backed; runs even if Postgres is down) ------------------
# 4b. LEARNED-workflow — contract resynth due (corpus grew past threshold since
# last synth; the miner's --detect drops this marker at close).
if [[ -f "${CORE_INSTANCE}/.claude/state/.learned-resynth-due" ]]; then
  add "🟡" "Behavior" "learned contracts may be stale — $(head -1 "${CORE_INSTANCE}/.claude/state/.learned-resynth-due" 2>/dev/null | cut -c1-48)" \
      "resynth: prepare → subagent regen → apply" "—" "learned-resynth"
fi

# 4c. ORACLE / PRE-EMPT WORK ORDERS — the SI loop asked for a hand-written hook.
# Written by friction_loop's flag_needs_oracle (D3: an artifact that FIRED and whose correction
# recurred anyway — obeyed and still wrong, so more prose cannot fix it) and by si-objective's
# propose_preempts (a violation class over threshold that no cheap supply can reach). Both land in ONE
# queue on purpose: two mechanisms writing two queues is the accretion the consolidate directive
# forbids, and a second queue is a second thing to forget to read.
ORACLE_Q="${CORE_INSTANCE}/.claude/state/oracle-request-queue.json"
if [[ -s "$ORACLE_Q" ]]; then
  OQ=$(python3 -c "
import json,sys
try: q=json.load(open('$ORACLE_Q'))
except Exception: sys.exit(0)
if not q: sys.exit(0)
retire=[x for x in q if x.get('recommended_action')=='retire']
write=[x for x in q if x.get('recommended_action')!='retire']
bits=[]
if retire: bits.append('%d superseded by an existing oracle -> RETIRE' % len(retire))
if write:  bits.append('%d need a hand-written hook' % len(write))
print('%d work order(s): %s' % (len(q), '; '.join(bits)))
" 2>/dev/null)
  if [[ -n "$OQ" ]]; then
    add "🟡" "Behavior" "$OQ" \
        "read .claude/state/oracle-request-queue.json — retire the superseded, write or decline the rest" \
        "—" "si-oracle-queue"
  fi
fi

# 4d. DETECTOR LIVENESS — is the instrument the objective depends on still working?
# A reply-observer detector that has silently stopped matching reports 0 violations, which is
# indistinguishable from perfect behaviour. That ambiguity was the whole defect in the objective this
# replaced, so an unverified or failing probe is a first-class item. Reads the stamp si-objective
# writes rather than re-running the probe — seven subprocesses is too much for a path that fires on
# every SessionStart and every statusline refresh.
LIVE_STAMP="${CORE_INSTANCE}/.claude/state/.si-liveness.json"
if [[ -f "${CORE_INSTANCE}/.claude/hooks/reply-observer.py" ]]; then
  LV=$(python3 -c "
import json,time,os
p='$LIVE_STAMP'
if not os.path.isfile(p):
    print('NEVER|the reply-observer liveness probe has never run — every 0 in the objective is unverifiable')
else:
    try: d=json.load(open(p))
    except Exception: d={}
    fail=d.get('failing') or []
    age=(time.time()-int(d.get('checked_at') or 0))/86400
    if fail:
        print('FAIL|%d of %d reply-observer detector(s) FAILED liveness: %s' % (len(fail), d.get('total') or 0, ', '.join(fail[:3])))
    elif age > 7:
        print('STALE|liveness last verified %.0fd ago — a detector could have died since' % age)
" 2>/dev/null)
  if [[ -n "$LV" ]]; then
    LV_KIND="${LV%%|*}"; LV_MSG="${LV#*|}"
    LV_SEV="🟡"; [[ "$LV_KIND" == "FAIL" ]] && LV_SEV="🔴"
    add "$LV_SEV" "System" "$LV_MSG" \
        "python3 bin/si-objective.py  (fix the detector before reading any violation count)" \
        "—" "si-detector-liveness"
  fi
fi

# 5. doc-path drift — TWO KEYS, because the admission unit is the KEY (2026-08-26).
#
# This raised ONE item for a population containing both a mechanically-provable half (a ref whose
# basename sits in exactly ONE archive dir) and a judgment half (everything else) that
# auto-safe.txt's HARD FLOOR explicitly reserves: "sys-docpath real-ref fixes". An applier
# registered on the bare key would fix the first half, return True, and flip the WHOLE item —
# judgment half included — to a status that reads `auto-applied`. The floor would be violated
# without a single line of auto-safe.txt changing.
#
# So: `sys-docpath-archival` is auto-safe and has an applier; `sys-docpath` stays notify-only and
# keeps the floor literally true. Both counts come from lint-doc-paths.split_counts() — ONE
# predicate, called here and by the applier, never re-derived.
if [[ -f "$CORE_BIN_LINT_DOC_PATHS" ]]; then
  SPLIT=$(python3 "$CORE_BIN_LINT_DOC_PATHS" --split 2>/dev/null)
  FIXABLE=$(printf '%s' "$SPLIT" | python3 -c "import json,sys;print(json.load(sys.stdin)['archival_fixable'])" 2>/dev/null || echo 0)
  OTHER=$(printf '%s' "$SPLIT" | python3 -c "import json,sys;print(json.load(sys.stdin)['other'])" 2>/dev/null || echo 0)
  if [[ "$FIXABLE" =~ ^[0-9]+$ ]] && (( FIXABLE > 0 )); then
    add "🟢" "System" "${FIXABLE} broken doc-path ref(s) whose file sits in exactly one archive dir" \
        "rewrite archival refs to their archive location" "—" "sys-docpath-archival"
  fi
  if [[ "$OTHER" =~ ^[0-9]+$ ]] && (( OTHER > 0 )); then
    add "🟢" "System" "${OTHER} broken doc-path ref(s) needing a real fix (not archival)" \
        "fix or delete the real broken refs" "—" "sys-docpath"
  fi
fi

# 5b. RECALL — eval freshness (eval.py writes tasks/research/brain-primitives-benchmark*.md)
# 2026-06-24: gate on identity.recall_eval_owner. Only the Core that OWNS the eval
# set runs eval.py (Nick's call: the recall benchmark lives in life). Peers have no
# eval set, so this check was a guaranteed false-positive on every peer session — it
# fired on all of business/school/finance and got punted to life. Default false → the
# row only appears for the owner.
RECALL_EVAL_OWNER=$(jq -r '.recall_eval_owner // false' "$CORE_IDENTITY_JSON" 2>/dev/null || echo false)
if [[ "$RECALL_EVAL_OWNER" == "true" ]]; then
  EVAL_REPORT=$(ls -t "${CORE_INSTANCE}/tasks/research/"brain-primitives-benchmark*.md 2>/dev/null | head -1)
  if [[ -z "$EVAL_REPORT" ]]; then
    add "🟡" "Recall" "no recall-eval benchmark on disk — eval.py not run/saved recently" \
        "run eval.py + schedule it nightly" "—" "recall-eval"
  elif [[ -n "$(find "$EVAL_REPORT" -mtime +7 2>/dev/null)" ]]; then
    add "🟡" "Recall" "recall-eval >7d stale ($(basename "$EVAL_REPORT"))" \
        "re-run eval.py + schedule nightly" "—" "recall-eval"
  fi
fi

# 6. pending-push marker present -> verify it's not stale
MARKER="${STATE_DIR}/.pending-push-marker"
if [[ -s "$MARKER" ]]; then
  PN=$(grep -c '^' "$MARKER" 2>/dev/null | tr -d ' '); PN=${PN:-0}
  if (( PN > 0 )); then
    add "🟡" "System" "pending-push marker lists ${PN} shared file(s)" \
        "verify vs baseline; clear if already pushed" "—" "sys-marker"
  fi
fi

# ---- SYSTEM / LIVENESS — folded from SessionStart checks (S3, 2026-05-27) --
# These six were standalone SessionStart `WARNINGS+=` lines (g,p,o,d5,d6,l).
# Folded into core-si so the gate is the single surface; detection LOGIC is
# preserved verbatim, only the output target changed (an add() row instead of
# its own warning line). All file/network-based — run regardless of Postgres.
# Launchd-safe: $CORE_INSTANCE / $STATE_DIR only, never cwd (the dark-detector
# bug). Was: .claude/hooks/session-start-check.sh checks (g)(p)(o)(d5)(d6)(l).

# 7. (o) sys-saveblock — last defensive-save / Stop hook BLOCKED (working tree
#    dirty for a real reason). Event-driven: silent when there's no block.
BLOCKED_MARKER="${STATE_DIR}/.last-save-blocked"
if [[ -s "$BLOCKED_MARKER" ]]; then
  BLOCKED_BY=$(grep -m1 '^BLOCKED_BY=' "$BLOCKED_MARKER" | cut -d= -f2-)
  STAGED_N=$(grep -m1 '^STAGED_FILES=' "$BLOCKED_MARKER" | cut -d= -f2-)
  REASONS=$(grep '^REASON=' "$BLOCKED_MARKER" | cut -d= -f2- | tr '\n' '|' | sed 's/|$//; s/|/ ; /g')
  add "🔴" "System" "save BLOCKED (${BLOCKED_BY}): ${STAGED_N} file(s) staged, uncommitted — ${REASONS}" \
      "fix the cause, commit manually (marker clears on next save)" "—" "sys-saveblock"
fi

# 8. (p) sys-embed — last brain-pipeline run failed (recall stale). Prefer the
#    durable marker run-brain-update.sh writes on ANY non-zero stage (extract /
#    embed / consolidate); fall back to scraping the /tmp log if no marker yet.
BRAIN_FAIL_MARKER="$CORE_INSTANCE/.claude/state/.brain-update-failed"
# MUST match run-brain-update.sh's namespacing or this reads another Core's failures as
# its own. Legacy shared path kept as a fallback only when this Core has no log yet.
BRAIN_LOG="/tmp/brain-stop-hook-$(basename "$CORE_INSTANCE").log"
[[ -f "$BRAIN_LOG" ]] || BRAIN_LOG="/tmp/brain-stop-hook.log"
if [[ -f "$BRAIN_FAIL_MARKER" ]]; then
  M_EXIT=$(grep -E '^CHAIN_EXIT=' "$BRAIN_FAIL_MARKER" | cut -d= -f2)
  M_WHEN=$(grep -E '^FAILED_AT=' "$BRAIN_FAIL_MARKER" | cut -d= -f2)
  M_LOG=$(grep -E '^LOG=' "$BRAIN_FAIL_MARKER" | cut -d= -f2)
  add "🔴" "Liveness" "brain pipeline FAILED (chain exit=${M_EXIT:-?} at ${M_WHEN:-?}) — recall may be stale" \
      "inspect tail ${M_LOG:-$BRAIN_LOG}; re-run embed; marker clears on clean run" "—" "sys-embed"
elif [[ -f "$BRAIN_LOG" ]]; then
  LAST_EMBED=$(grep -E "run-brain-update\[(fast|heavy)\]: embed exit=" "$BRAIN_LOG" | tail -1)
  if [[ -n "$LAST_EMBED" ]]; then
    EMBED_EXIT=$(echo "$LAST_EMBED" | grep -oE 'exit=[0-9]+' | cut -d= -f2)
    if [[ -n "$EMBED_EXIT" ]] && (( EMBED_EXIT != 0 )); then
      EMBED_WHEN=$(echo "$LAST_EMBED" | grep -oE '^\[[^]]+\]' | tr -d '[]')
      add "🔴" "Liveness" "brain embed FAILED (exit=${EMBED_EXIT} at ${EMBED_WHEN}) — recall may be stale" \
          "inspect tail /tmp/brain-stop-hook.log; re-run embed" "—" "sys-embed"
    fi
  fi
fi

# 9. (d6) sys-brainpush — brain repo has unpushed commits (push likely failed).
#    CORE_BRAIN may be unset here (detect.sh doesn't source core-paths.sh), so
#    derive the sibling-repo fallback exactly as core-paths.sh does, then guard.
BRAIN_REPO="${CORE_BRAIN:-$(dirname "$CORE_INSTANCE")/core-brain}"
if [[ -d "$BRAIN_REPO/.git" ]]; then
  UNPUSHED=$(git -C "$BRAIN_REPO" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
  if [[ "$UNPUSHED" =~ ^[0-9]+$ ]] && (( UNPUSHED > 0 )); then
    add "🟡" "System" "brain repo: ${UNPUSHED} unpushed commit(s) — last push likely failed" \
        "cd ${BRAIN_REPO} && git push (check network)" "—" "sys-brainpush"
  fi
fi

# 10. (d5) sys-brainlint — fire ONLY on gap-memory (the actionable drift signal).
#     2026-07-11: the old "fire if gap-topics+gap-memory+orphans > 0" made this a
#     PERMANENT 🟢 — in a 7,540-topic brain gap-topics (196) and orphan-pages (590,
#     capped to 30 in the report) are NEVER zero: most topics are referenced only
#     from their own source session, so "no inbound reference" is the steady-state
#     floor, not a defect. Zeroing them would mean deleting hundreds of real brain
#     pages. gap-MEMORY is the one that means "act": memory tracks a project/person
#     the brain hasn't corroborated in RECENT_DAYS — real drift worth reconciling.
#     gap-topics/orphans still live in the report (${LATEST_LINT}) for reference.
# SPLIT BY CAUSE, NOT BY VERDICT (2026-08-26). Two separate conditions were sharing one key:
#   · the report is MISSING or STALE  -> mechanizable: regenerate it. New key, auto-safe.
#   · a FRESH report shows gap-memory -> genuine drift reconciliation. Stays Nick's, stays on the
#     HARD FLOOR, and the floor line needs no amendment because the key is different.
#
# This also closes a live blind spot: brain-lint reports STOPPED on 2026-08-12 and the last one
# reads 0 gap-memory, so the drift detector could not fire at all. Dead supply was indistinguishable
# from health — the same shape as every other silent no-op found this week.
#
# AND THE DATED PATH IS OUT OF THE FIX COLUMN. Trust streaks key on the exact
# (signal_key, fix_action) pair, so a fix string embedding a dated report path rotates the trust key
# every time the report regenerates. PROVEN LIVE on this seat: sys-brainlint has TWO 'approve' rows
# from Nick with two different fix strings, and zero trusted rows. He approved it twice and it could
# never graduate. The path belongs in the DETECTED column, which is not part of the key.
LINT_DIR="${CORE_INSTANCE}/memory/brain-lint-reports"
if [[ -d "$LINT_DIR" ]]; then
  LATEST_LINT=$(ls -t "$LINT_DIR"/*.md 2>/dev/null | head -1)
  if [[ -z "$LATEST_LINT" ]] || [[ -n "$(find "$LATEST_LINT" -mtime +7 2>/dev/null)" ]]; then
    add "🟡" "System" "brain-lint report missing or >7d stale — gap-memory drift is unmeasured" \
        "regenerate the brain-lint report" "—" "sys-brainlint-refresh"
  elif [[ -n "$LATEST_LINT" ]]; then
    LINT_DATE=$(basename "$LATEST_LINT" .md)
    SUMMARY_LINE=$(awk '/^## Summary/{getline; getline; print; exit}' "$LATEST_LINT")
    GAP_MEMORY=$(echo "$SUMMARY_LINE" | grep -oE '[0-9]+ gap-memory' | grep -oE '[0-9]+' | head -1)
    if [[ "${GAP_MEMORY:-0}" -gt 0 ]]; then
      add "🟢" "System" "brain lint (${LINT_DATE}, ${LATEST_LINT}): ${GAP_MEMORY} memory entr(y/ies) the brain forgot recently" \
          "reconcile the drifted memory entries" "—" "sys-brainlint"
    fi
  fi
fi

# 11. (l) sys-baseline — has nicknur7/core-agent advanced since our last pull?
#     Lightweight git ls-remote (SHA only), 3s perl-alarm timeout, silent on
#     network error / timeout. The pull itself is Sentinel-code gated.
SYNC_MANIFEST="${CORE_INSTANCE}/bin/sync-manifest.json"
SYNC_LOG="${STATE_DIR}/.last-baseline-sync"
if [[ -f "$SYNC_MANIFEST" ]] && command -v jq >/dev/null 2>&1; then
  BASELINE_REPO=$(jq -r '.baseline_repo' "$SYNC_MANIFEST" 2>/dev/null || echo "")
  BASELINE_BRANCH=$(jq -r '.baseline_branch // "main"' "$SYNC_MANIFEST" 2>/dev/null || echo "main")
  if [[ -n "$BASELINE_REPO" && "$BASELINE_REPO" != "null" ]]; then
    REMOTE_SHA=$(perl -e 'alarm 3; exec @ARGV or die' git ls-remote -h "https://github.com/${BASELINE_REPO}.git" "$BASELINE_BRANCH" 2>/dev/null | awk '{print $1}' | head -1)
    LOCAL_SHA=""
    [[ -f "$SYNC_LOG" ]] && LOCAL_SHA=$(tail -1 "$SYNC_LOG" 2>/dev/null | grep -oE 'baseline=[a-f0-9]+' | head -1 | cut -d= -f2)
    # WHO IS ASKING CHANGES THE ANSWER. This item proposed "sync-from-baseline.sh to review+apply"
    # on every Core, and that fix was wrong on all five, for two different reasons (measured
    # 2026-08-04 across the fleet):
    #
    #   on the WRITER  — the baseline is DOWNSTREAM of it. A pull rsyncs the older baseline over
    #                    its own unpushed shared edits. On core-life the proposed action would have
    #                    reverted a test fix committed 18 minutes earlier and deleted two archive
    #                    dirs as orphans. That is why --quiet mode already skips the writer; this
    #                    detector did not know to.
    #   on a PULLER    — the manual path dead-ends. pretooluse-guard gates it, sentinel-code ASKs on
    #                    any trust-root content change and may never APPROVE, and sentinel-approve.sh
    #                    cannot mint a token for that class by construction. core-finance spent an
    #                    afternoon there. The path that WORKS is the SessionStart auto-pull, which is
    #                    hook-invoked and therefore never passes through PreToolUse.
    #
    # So the detector now names the route that can actually succeed for this Core, rather than the
    # one that reads plausible. Same defect class as the rest of today: a check reporting a proxy
    # (SHAs differ) instead of the thing (what should this Core DO about it).
    _SI_WRITER=$(jq -r '.baseline_writer // ""' "$SYNC_MANIFEST" 2>/dev/null)
    _SI_SELF=$(basename "$CORE_INSTANCE")
    if [[ -n "$REMOTE_SHA" && -n "$LOCAL_SHA" && "$REMOTE_SHA" != "$LOCAL_SHA" ]]; then
      if [[ -n "$_SI_WRITER" && "$_SI_WRITER" == "$_SI_SELF" ]]; then
        add "🟡" "System" "baseline ${BASELINE_REPO}@${BASELINE_BRANCH} advanced (remote=${REMOTE_SHA:0:7}, local=${LOCAL_SHA:0:7}) — this Core is the WRITER" \
            "do NOT pull (regresses unpushed shared edits); run sync-to-baseline.sh --check and push if it lists your work" "—" "sys-baseline"
      else
        add "🟡" "System" "baseline ${BASELINE_REPO}@${BASELINE_BRANCH} advanced (remote=${REMOTE_SHA:0:7}, local=${LOCAL_SHA:0:7})" \
            "restart the session — the SessionStart auto-pull is ungated; the manual sync-from-baseline.sh dead-ends at the Sentinel trust-root ASK" "—" "sys-baseline"
      fi
    elif [[ -n "$REMOTE_SHA" && -z "$LOCAL_SHA" ]]; then
      add "🟡" "System" "baseline ${BASELINE_REPO}@${BASELINE_BRANCH} present (remote=${REMOTE_SHA:0:7}); no local sync log" \
          "sync-from-baseline.sh to establish baseline" "—" "sys-baseline"
    fi
  fi
fi

# 12. (g) sys-memstale — personal/project memory files whose `Last updated:`
#     stamp is >30d old (verify-before-quote). Frontmatter date, not mtime.
#     Org-1 excludes */business/* (those use Status: markers, not stamps).
THIRTY_DAYS=$(( 30 * 86400 ))
NOW_EPOCH=$(date +%s)
STALE_MEM=()
_SF=("$CORE_MEM_ABOUT_ME" "$CORE_MEM_PREFERENCES" "${CORE_INSTANCE}/memory/skills-interests.md" "${CORE_INSTANCE}/memory/goals.md")
# 2026-06-24: apply the business-planning-dir exemption in EVERY Core, not just org-1.
# memory/projects/business/* are Status-marked working docs (competitive.md, moat.md,
# shareable.md, technical-built/future) — not memory-of-self — in whichever Core holds
# them. The old `ORG==1` guard meant business (org-2) false-flagged its OWN planning docs
# as stale every session. School/finance have no business/ dir → the exclusion is a no-op.
_BIZ_EXCLUDE=(-not -path "*/business/*")
while IFS= read -r -d '' _f; do _SF+=("$_f"); done < <(find "${CORE_INSTANCE}/memory/relationships" "${CORE_INSTANCE}/memory/projects" -name '*.md' -not -path '*/archive/*' ${_BIZ_EXCLUDE[@]+"${_BIZ_EXCLUDE[@]}"} -print0 2>/dev/null)
for MF in "${_SF[@]}"; do
  [[ -f "$MF" ]] || continue
  [[ "$MF" == *"/archive/"* ]] && continue
  # Status-marker docs (LOCKED/COMPLETE/EXTRACTED/DRAFT/SKELETON/SUPERSEDED) track state
  # via a Status: line, not a Last-updated stamp — exempt like business/ docs (2026-06-18).
  grep -m1 -iE '^Status:[[:space:]]*(LOCKED|COMPLETE|EXTRACTED|DRAFT|SKELETON|SUPERSEDED)' "$MF" >/dev/null 2>&1 && continue
  LU=$(grep -m1 -iE '^last updated:' "$MF" 2>/dev/null | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 || true)
  if [[ -z "$LU" ]]; then STALE_MEM+=("$(basename "$MF"):no-stamp"); continue; fi
  LU_EPOCH=$(date -j -f "%Y-%m-%d" "$LU" "+%s" 2>/dev/null || echo 0)
  if (( NOW_EPOCH - LU_EPOCH > THIRTY_DAYS )); then
    STALE_MEM+=("$(basename "$MF"):$(( (NOW_EPOCH - LU_EPOCH) / 86400 ))d")
  fi
done
# SPLIT (2026-08-26) — bin/memstale.py is THE predicate; both rows come from one --json call, and
# the applier calls the same module. The bash scan above still produces the human-readable list.
#
# proven  = >30d stale AND a real CONTENT edit exists after the stamp, so there is a defensible
#           date to write: the date of that edit, never today's.
# residual= no stamp, or no proven edit. NOT a judgment call being deferred — there is no
#           non-lying value to write. Any date there asserts a freshness nothing supports and
#           destroys the only staleness signal the file has.
_MEMSTALE_JSON=$(python3 "${CORE_INSTANCE}/bin/memstale.py" --json 2>/dev/null)
if [[ -n "$_MEMSTALE_JSON" ]]; then
  _MS_PROVEN=$(printf '%s' "$_MEMSTALE_JSON" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['proven']))" 2>/dev/null || echo 0)
  _MS_RESID=$(printf '%s' "$_MEMSTALE_JSON" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['residual']))" 2>/dev/null || echo 0)
  if [[ "$_MS_PROVEN" =~ ^[0-9]+$ ]] && (( _MS_PROVEN > 0 )); then
    add "🟢" "System" "${_MS_PROVEN} memory file(s) with a proven content edit newer than the stamp" \
        "bump Last updated to the date of the proven edit" "—" "sys-memstale-proven"
  fi
  if [[ "$_MS_RESID" =~ ^[0-9]+$ ]] && (( _MS_RESID > 0 )); then
    SML=$(IFS=, ; echo "${STALE_MEM[*]}")
    add "🟢" "System" "${_MS_RESID} stale memory file(s) with no proven edit (${SML})" \
        "verify with the operator before quoting; bump Last updated" "—" "sys-memstale"
  fi
elif (( ${#STALE_MEM[@]} > 0 )); then
  SML=$(IFS=, ; echo "${STALE_MEM[*]}")
  add "🟢" "System" "${#STALE_MEM[@]} stale memory file(s) >30d: ${SML}" \
      "verify with the operator before quoting; bump Last updated" "—" "sys-memstale"
fi

# ---- learned-contract fitness: NOT-BINDING contracts (B3, 2026-06-08) ------
# measure-contract-fitness.py writes .claude/state/contract-fitness.json — the restored MEASURE organ.
# A contract that FIRES but whose correction keeps RECURRING is not binding (the escape hatch) and
# needs a structural gate. This is /core-si finally ACTING on the loop's own feedback instead of just
# re-surfacing corrections. Surface each as a 🔴 decision item.
#
# THE REMEDY IS DERIVED, NOT NAMED. It read, hardcoded:
#
#     "wire the binding gate (approval-gate / recall-gate) for this class"
#
# BOTH OF THOSE ARE TOMBSTONED — `retired: true` in bin/hook-registry.json, absent from
# settings.json. So every 🔴 this branch raised carried a fix that was unactionable by construction,
# and two of them sat red in the queue on that basis. Fourth instance in one day of a prescription
# outliving its premise, after Tier B's stale diagnostic, a decisions-log entry grouping two items
# that do not share an instrument, and tracking-orphan-guard's fail-open comment deferring to a hook
# retired on 2026-08-06.
#
# bin/enforcement-audit.py exists for exactly this class — "no doc may claim a retired hook enforces
# anything" — and passes clean, because it scans .md ONLY. It cannot see a retired-hook prescription
# baked into a .sh source string that this loop emits into a live queue at runtime. The checker
# built to stop documents making unbacked enforcement claims was blind to the GENERATOR of them.
#
# Deriving, rather than adding a second scanner: core-business's call and mine — a second scanner is
# a second instrument on one subject, chasing generators one file type at a time.
#
# AND THE TAIL, WHICH IS THE HALF I MISSED (core-business): deriving makes it impossible to NAME a
# retired hook and does NOT make it impossible to prescribe nothing. When every gate for a class is
# retired — which is exactly the state plan-not-execute and stop-and-plan are in — the honest output
# is not a quieter remedy, it is NO GATE EXISTS FOR THIS CLASS. That is a finding about the system
# and should read as one. A remedy field that can be empty will be, and an empty remedy looks like
# nothing to do.
# SCOPED TO THE GATES THAT COULD BIND THIS CLASS, not to every blocking gate on the Core. A first
# version listed all seven live blockers, which is the 35-filename failure again: a remedy that names
# everything prescribes nothing. These two are the ones this item was always about — the
# approval/recall pair that gates "you were told to plan, and you executed".
_BINDING_CANDIDATES="approval-gate recall-gate recall-first-gate"
_binding_gates() {
  # $CORE_INSTANCE, not $REPO. `REPO` is never defined in this file — `set -u` (line 19) therefore
  # aborted this function on every single run, `2>/dev/null` hid the error, and `|| true` turned the
  # abort into an empty string. An empty string selects the "NO GATE EXISTS FOR THIS CLASS" fallback
  # below, so that branch has been UNCONDITIONAL since it shipped: no registry was ever read, no gate
  # was ever looked for, on any Core.
  #
  # The cost was not a wrong string, it was a wrong instruction. The 🔴 stop-and-plan item has been
  # telling every reader "every blocking gate that could bind it is retired ... a new PreToolUse
  # mechanism has to be designed before this item can be actioned" — an item that reads as
  # un-actionable by construction. `recall-first-gate` is live at PreToolUse in this very registry
  # and is not retired, so the true remedy was always the actionable branch: wire the existing gate.
  # Three independent safety mechanisms each did their job and the composition still failed silently,
  # which is the argument for why a fallback branch must never be reachable by accident.
  python3 - "$CORE_INSTANCE/bin/hook-registry.json" $_BINDING_CANDIDATES <<'PYGATE'
import json, sys
try:
    reg = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
wanted = set(sys.argv[2:])
hooks = reg.get("hooks", reg) if isinstance(reg, dict) else reg
live = []
for h in (hooks if isinstance(hooks, list) else hooks.values()):
    if not isinstance(h, dict) or h.get("name") not in wanted:
        continue
    if h.get("retired"):
        continue                      # tombstoned — naming it is the defect being fixed
    live.append(h["name"])
print(" / ".join(sorted(set(live))))
PYGATE
}
_BINDING_GATES="$(_binding_gates 2>/dev/null || true)"
if [[ -n "$_BINDING_GATES" ]]; then
  _BINDING_REMEDY="wire the binding gate (${_BINDING_GATES}) for this class"
else
  _BINDING_REMEDY="NO GATE EXISTS FOR THIS CLASS — every blocking gate that could bind it is retired. This is a finding about the system, not a task: a new PreToolUse mechanism has to be designed before this item can be actioned."
fi

# LEARNED-CONTRACT FITNESS IS A PRE-CUTOVER MEASUREMENT (2026-08-28).
#
# contract-fitness.json grades rows in `learned_contracts` — the table the SI-spine cutover
# retires. On a seat carrying .si-unified-spine those verdicts describe a spine the seat no longer
# runs, so every one of them is an alert about dead machinery, and the remedy text tells the reader
# to go wire a gate for a contract that is no longer dispatched.
#
# Measured on core-school, which cut over 2026-08-28 01:35: five red "NOT BINDING" rows, all
# computed from a contract-fitness.json last written 2026-08-17 — eleven days old and predating its
# own cutover. Those five were the entire remaining red backlog on that seat.
#
# Gated on the MARKER, not on a version or a date, for the same reason the validator itself is:
# a PRE-cutover seat still runs learned_contracts legitimately and its fitness verdicts are real
# findings there. This suppresses nothing on those seats.
CF_FILE="${STATE_DIR}/contract-fitness.json"
if [[ -f "${CORE_INSTANCE}/.claude/state/.si-unified-spine" ]]; then
  CF_FILE=""   # post-cutover: these verdicts are about a retired spine
fi
if [[ -n "$CF_FILE" && -f "$CF_FILE" ]] && command -v python3 >/dev/null 2>&1; then
  while IFS=$'\t' read -r _name _why; do
    [[ -z "$_name" ]] && continue
    add "🔴" "Enforcement" "learned contract '${_name}' fires but its correction keeps recurring — NOT BINDING" \
        "${_BINDING_REMEDY}" "${_why}" "si-notbind-${_name}"
  done < <(python3 -c "
import json,sys
try: d=json.load(open('$CF_FILE'))
except Exception: sys.exit(0)
for c in d.get('contracts',[]):
    if c.get('verdict')=='NOT-BINDING':
        print(c['contract']+chr(9)+c.get('rationale','')[:70])
" 2>/dev/null)
fi

# ---- enforcement honesty (2026-08-31) --------------------------------------
# measure-contract-fitness.py now derives, per loop artifact, whether anything MECHANICALLY acts
# on it (enforces) or it is inject-only prose (advises) — and flags two states that are defects
# rather than facts:
#
#   claims-enforce-dead-event  an enforced block whose event never dispatches — claims a net that
#                              is not there, the exact class CLAUDE.base.md's "you relax against a
#                              net that was taken down" lesson names. 🔴 at any count. Read from
#                              the file UNGATED by the cutover marker: this tier describes
#                              si_artifacts, the LIVE spine on a post-cutover seat.
#   placeholder                a learned_contracts row still carrying si_induct's boilerplate body
#                              — a parked ask wearing a contract's name, counted as coverage by
#                              nothing anymore. Gated on the marker like the contract verdicts
#                              above, because the contract spine is retired post-cutover.
#
# The COUNT (N enforce / N advise) is deliberately NOT an item — a standing "97% advisory" alert
# would train the reader to ignore the queue, and a low enforce count may be exactly right (most
# asks have no oracle). It renders as one headline line in the default emit below instead:
# ambient honesty, not a task.
EH_FILE="${STATE_DIR}/contract-fitness.json"
EH_LINE=""
if [[ -f "$EH_FILE" ]] && command -v python3 >/dev/null 2>&1; then
  _EH_PRECUT=1
  [[ -f "${CORE_INSTANCE}/.claude/state/.si-unified-spine" ]] && _EH_PRECUT=0
  while IFS=$'\t' read -r _kind _a _b; do
    case "$_kind" in
      DEAD) add "🔴" "Enforcement" "artifact '${_a}' is enforced=true on an event that never dispatches — it claims to enforce and cannot" \
                "flip it to shadow or retire it — a block that cannot fire is prose wearing an enforcement flag" "${_b}" "si-deadblock-${_a}" ;;
      PLACEHOLDER) add "🟡" "Enforcement" "${_a} learned contract(s) still carry si_induct's placeholder body — parked asks counted as installed contracts" \
                "route each through the typer to a real terminal (or retire it): ${_b}" "—" "si-placeholder" ;;
      LINE) EH_LINE="${_a}" ;;
    esac
  done < <(python3 -c "
import json,sys
try: d=json.load(open('$EH_FILE'))
except Exception: sys.exit(0)
t=chr(9)
for a in d.get('si_artifacts',[]):
    e=a.get('enforcement') or {}
    if e.get('tier')=='claims-enforce-dead-event':
        print('DEAD'+t+str(a.get('artifact_id'))+t+e.get('note','')[:70])
ph=[c['contract'] for c in d.get('contracts',[]) if (c.get('enforcement') or {}).get('tier')=='placeholder']
if ph and int('${_EH_PRECUT}'):
    print('PLACEHOLDER'+t+str(len(ph))+t+', '.join(sorted(set(ph))[:5]))
s=d.get('enforcement_summary')
if s:
    def fmt(x): return ' · '.join(f'{v} {k}' for k,v in sorted(x.items())) or 'none'
    print('LINE'+t+'loop estate — artifacts: '+fmt(s.get('si_artifacts',{}))+' | contracts: '+fmt(s.get('contracts',{}))+' (derived; advisory artifacts steer, they do not stop)')
" 2>/dev/null)
fi

# ---- rank: 🔴 < 🟡 < 🟢 ---------------------------------------------------
sev_rank() { case "$1" in "🔴") echo 0;; "🟡") echo 1;; *) echo 2;; esac; }
SORTED=()
for sev in "🔴" "🟡" "🟢"; do
  for it in "${ITEMS[@]:-}"; do
    [[ -z "$it" ]] && continue
    [[ "${it%%$'\t'*}" == "$sev" ]] && SORTED+=("$it")
  done
done

N=${#SORTED[@]}

# ---- refresh cache (cheap reads for the statusline badge) -----------------
printf '%s\n' "$N" > "$CACHE_COUNT"
: > "$CACHE_TSV"
for it in "${SORTED[@]:-}"; do [[ -n "$it" ]] && printf '%s\n' "$it" >> "$CACHE_TSV"; done

# ---- emit -----------------------------------------------------------------
case "$MODE" in
  --count) echo "$N" ;;
  --tsv)   for it in "${SORTED[@]:-}"; do [[ -n "$it" ]] && printf '%s\n' "$it"; done ;;
  *)
    if (( N == 0 )); then echo "core-si: 0 items — clean."; exit 0; fi
    RED=0; YEL=0; GRN=0
    for it in "${SORTED[@]}"; do case "${it%%$'\t'*}" in "🔴") ((RED++));; "🟡") ((YEL++));; *) ((GRN++));; esac; done
    echo "core-si — $(date '+%Y-%m-%d %H:%M %Z') · ${N} need you  (${RED} 🔴 · ${YEL} 🟡 · ${GRN} 🟢)"
    [[ -n "$EH_LINE" ]] && echo "$EH_LINE"
    echo ""
    echo "| # | Pri | Domain | Detected | Proposed fix | Fitness |"
    echo "|---|-----|--------|----------|--------------|---------|"
    i=0
    for it in "${SORTED[@]}"; do
      i=$((i+1))
      IFS=$'\t' read -r sev dom det fix fit key <<< "$it"
      echo "| ${i} | ${sev} | ${dom} | ${det} | ${fix} | ${fit} |"
    done
    echo ""
    echo "Reply: \`approve all\` · \`approve 1,3\` · \`edit N: <text>\` · \`reject N reason:<why>\` · \`details N\` · \`skip\`"
    ;;
esac
