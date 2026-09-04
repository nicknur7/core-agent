#!/usr/bin/env bash
# core-doctor — unified health surface for Core.
#
# macOS-only (uses BSD `stat -f`, `date -j`, `date -v`). Run-to-completion
# target: <3s. Output ~30-50 lines, color-friendly. No persistent log writes.
#
# Promoted from .claude/scripts/core-status.sh per audit
# tasks/audits/core-audit-report-2026-05-11.md item #13 (Dim 9, finding #1).
# Old path remains as a 5-line shim for backward compat.
#
# Usage:  bash bin/core-doctor.sh
# Exit:   0 = clean, or warnings only. Non-zero (1) = at least one FAILED INVARIANT — something
#         that means the Core does not work (a required hook file absent, a DB table the
#         self-improvement loop cannot run without). A WARNING (stale timestamp, coverage
#         percentage, advisory drift, an idle container) never moves the exit code.
#
#         Every check below still RUNS and PRINTS regardless (`set +e`, no early exit) — the
#         report is never truncated by an earlier failure. Only the final exit reflects them.
#         This is the fix for the defect measured 2026-08-31: this file had zero `exit N`
#         statements in 400+ lines, so `bash bin/core-doctor.sh; echo $?` printed 0 even with
#         `learned_contracts` dropped and the report correctly showing
#         "✗ SI-spine tables MISSING: learned_contracts" — any caller gating on `$?` (setup-
#         brain.sh's own verification step) saw nothing. See bin/tests/test_fresh_spawn_install.sh
#         ITEM 6 for the behavioral reproduction this fix targets.
#
#         No separate "warnings-only" exit code (e.g. 2): considered it, decided against it.
#         "Clean" and "warnings-only" are both safe-to-proceed states for the one real caller
#         (setup-brain.sh) and for a human reading the report — splitting them buys nothing and
#         risks a future `if [[ $? -ne 0 ]]` caller treating a warnings-only 2 as a hard failure,
#         which is the exact false-alarm class this fix exists to remove.
#
#         Follow-up (2026-08-31, same day): the SI-spine check below made THIS EXACT non-zero
#         exit fire on every fresh install, before install-learned-layer.sh (docs/SETUP.md step
#         3) had ever run — setup-brain.sh's own step-8 call to this file happens first. See the
#         comment at the SI-spine check for the schema_migrations-based fix that tells "step 3
#         hasn't run yet" (WARNING) apart from "step 3 ran and something broke" (still FAILED).

set +e  # don't abort on individual failures — every check is independent

CORE="${CORE_INSTANCE:?CORE_INSTANCE env var must be set}"
# shellcheck source=bin/core-paths.sh
source "$CORE/bin/core-paths.sh"
BRAIN="$CORE_BRAIN"
TODAY=$(date "+%Y-%m-%d")
NOW=$(date "+%Y-%m-%d %H:%M %Z")
NOW_EPOCH=$(date "+%s")

# ── Invariant tracking (2026-08-31) ──────────────────────────────────────
# Plain assignment, not `((FAILED_INVARIANTS++))` — post-increment of a var starting at 0
# evaluates to 0 (falsy), which would make the FIRST increment itself return a nonzero
# status from the `((...))` command. Harmless under `set +e` today, but a plain assignment
# is correct regardless of how this file's error-handling evolves, so use it everywhere below.
FAILED_INVARIANTS=0

# ── Color setup (only when stdout is a TTY) ─────────────────────────────
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    RED=$'\033[31m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    BLUE=$'\033[34m'
    RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

ok()   { printf "  ${GREEN}✓${RESET} %s\n"  "$*"; }
warn() { printf "  ${YELLOW}⚠${RESET} %s\n" "$*"; }
bad()  { printf "  ${RED}✗${RESET} %s\n"    "$*"; }
info() { printf "  %s\n" "$*"; }
hdr()  { printf "\n${BOLD}${BLUE}── %s ──${RESET}\n" "$*"; }

# ── Sister-project marker lookup (parameterized via identity.json) ──────  # privacy-ok: 'sister project' is a codebase term for a sibling project, not a family relation
# Reads .claude/identity.json sister_projects.<name>.markers.<key>. Engine
# template ships with no entries — instance fills in only the sister  # privacy-ok: 'sister project' is a codebase term for a sibling project, not a family relation
# integrations it has. Returns empty string if not configured.
identity_marker() {
    local proj="$1" key="$2"
    local id_file="$CORE_IDENTITY_JSON"
    [[ -f "$id_file" ]] || { echo ""; return; }
    python3 - "$id_file" "$proj" "$key" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    print(d.get("sister_projects", {}).get(sys.argv[2], {}).get("markers", {}).get(sys.argv[3], ""))
except Exception:
    pass
PY
}

JH_SUCCESS_PREFIX=$(identity_marker job_hunter success_prefix)
JH_FAILED_PREFIX=$(identity_marker job_hunter failed_prefix)

printf "${BOLD}════════════════════════════════════════════════════════${RESET}\n"
printf "${BOLD} core-doctor — %s${RESET}\n" "$NOW"
printf "${BOLD}════════════════════════════════════════════════════════${RESET}\n"

# ── 1. LaunchAgent fires (today) ─────────────────────────────────────────
# Driven by identity.json sister_projects.*.markers — Section silently no-ops
# in engine clones where no sister projects are configured.  # privacy-ok: 'sister project' is a codebase term for a sibling project, not a family relation
hdr "LaunchAgent fires (today)"
SISTER_MARKERS=()
[[ -n "$JH_SUCCESS_PREFIX" ]] && SISTER_MARKERS+=("${JH_SUCCESS_PREFIX}${TODAY}")
[[ -n "$JH_FAILED_PREFIX"  ]] && SISTER_MARKERS+=("${JH_FAILED_PREFIX}${TODAY}")
if (( ${#SISTER_MARKERS[@]} == 0 )); then
    info "${DIM}(no sister-project markers configured in $CORE_IDENTITY_JSON)${RESET}"
else
    for marker in "${SISTER_MARKERS[@]}"; do
        if [[ -e "$marker" ]]; then
            sz=$(stat -f%z "$marker" 2>/dev/null || echo "?")
            ts=$(stat -f "%Sm" "$marker" 2>/dev/null || echo "?")
            if [[ "$marker" == *failed* ]]; then
                bad "$marker (${sz}b, $ts)"
            else
                ok "$marker (${sz}b, $ts)"
            fi
        else
            info "${DIM}· no marker: $marker${RESET}"
        fi
    done
fi

# ── 2. LaunchAgent registered status ─────────────────────────────────────
hdr "LaunchAgent registered status"
LA_OUT=$(launchctl list 2>/dev/null | grep -E "com\.${USER}\.")
if [[ -n "$LA_OUT" ]]; then
    echo "$LA_OUT" | awk -v g="$GREEN" -v r="$RED" -v R="$RESET" '
        {
            label=$3; pid=$1; ex=$2;
            color = (ex == "0" || ex == "-") ? g : r
            printf "  %s%-40s%s pid=%-7s exit=%s\n", color, label, R, pid, ex
        }'
else
    info "${DIM}(no com.${USER}.* agents loaded)${RESET}"
fi

# ── 3. pending.md (the user's open items) ────────────────────────────────────
hdr "pending.md (open items, top 10)"
if [[ -f "$CORE_MEM_PENDING" ]]; then
    PEND=$(grep -E '^- \[ \]' "$CORE_MEM_PENDING" 2>/dev/null | head -10)
    if [[ -n "$PEND" ]]; then
        echo "$PEND" | sed "s/^/  /"
    else
        info "${DIM}(no open items)${RESET}"
    fi
else
    info "${DIM}(no pending.md)${RESET}"
fi

# ── 4. current-state.md last-updated stamp ───────────────────────────────
hdr "current-state.md timestamp"
if [[ -f "$CORE_MEM_CURRENT_STATE" ]]; then
    STAMP=$(head -10 "$CORE_MEM_CURRENT_STATE" | grep -iE "updated|timestamp" | head -1)
    if [[ -n "$STAMP" ]]; then
        # age check: stamp date vs today
        STAMP_DATE=$(echo "$STAMP" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)
        if [[ -n "$STAMP_DATE" ]]; then
            STAMP_EPOCH=$(date -j -f "%Y-%m-%d" "$STAMP_DATE" "+%s" 2>/dev/null || echo 0)
            AGE_HOURS=$(( (NOW_EPOCH - STAMP_EPOCH) / 3600 ))
            if (( AGE_HOURS > 24 )); then
                warn "$STAMP  (${AGE_HOURS}h old)"
            else
                ok "$STAMP"
            fi
        else
            info "  $STAMP"
        fi
    else
        warn "no Last-updated stamp found in first 10 lines"
    fi
else
    bad "$CORE_MEM_CURRENT_STATE missing"
fi

# ── 5. Brain lint last report ────────────────────────────────────────────
hdr "Brain lint last report"
LAST_LINT=$(ls -t "$CORE/memory/brain-lint-reports"/*.md 2>/dev/null | head -1)
if [[ -n "$LAST_LINT" ]]; then
    LINT_DATE=$(basename "$LAST_LINT" .md)
    LINT_EPOCH=$(date -j -f "%Y-%m-%d" "$LINT_DATE" "+%s" 2>/dev/null || echo 0)
    LINT_AGE_DAYS=$(( (NOW_EPOCH - LINT_EPOCH) / 86400 ))
    if (( LINT_AGE_DAYS > 2 )); then
        warn "$LAST_LINT (${LINT_AGE_DAYS}d old)"
    else
        ok "$LAST_LINT"
    fi
    head -3 "$LAST_LINT" | sed 's/^/    /'
else
    info "${DIM}(no lint reports)${RESET}"
fi

# ── 6. Recent session files ──────────────────────────────────────────────
hdr "Recent session files (top 3)"
if [[ -d "$CORE/sessions" ]]; then
    # Paths contain spaces ("AI Projects") so awk-by-column splits the path.
    # Print just the basename + a human-readable mtime to dodge the issue.
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        ts=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$f" 2>/dev/null || echo "?")
        printf "  %s  (%s)\n" "$(basename "$f")" "$ts"
    done < <(ls -t "$CORE/sessions"/*.md 2>/dev/null | head -3)
else
    info "${DIM}(no sessions dir)${RESET}"
fi

# ── 7. Stop-hook health (executable bits) ────────────────────────────────
# FAILED-INVARIANT vs WARNING here is NOT uniform across the four files — read what each one
# is actually wired to, not its name:
#   CORE_HOOK_STOP (stop-hook.sh)      — registered in .claude/settings.json as a bare-path
#                                         Stop-event command (no `bash` prefix ahead of it), so
#                                         it needs to BOTH exist AND be +x — either miss and the
#                                         Stop pipeline (auto-commit, brain update) silently
#                                         never fires. FAILED INVARIANT either way.
#   CORE_HOOK_END_SESSION (end-session.sh) — never an automatic hook; invoked explicitly as
#                                         `bash ".../end-session.sh"` by /close-core and
#                                         rules/session.md step 5, so the exec bit is moot —
#                                         only its EXISTENCE is load-bearing. FAILED if missing,
#                                         WARNING (unchanged) if merely non-executable.
#   CORE_HOOK_SAY_DO_GAP / CORE_HOOK_STATE_CLAIM_GATE — both were retired as Stop hooks on
#                                         2026-08-06 and are UNREGISTERED in settings.json today
#                                         (confirmed by grep + docs/enforcement-audit-2026-08-09.md,
#                                         which names both explicitly: "None is registered in any
#                                         settings file"). Their absence changes nothing about
#                                         whether Core runs — still shown red (worth noticing,
#                                         e.g. a fork that deleted them cleanly vs. one that
#                                         hasn't) but never counted as a FAILED INVARIANT.
hdr "Stop-hook script health"
for f in \
    "$CORE_HOOK_STOP" \
    "$CORE_HOOK_END_SESSION" \
    "$CORE_HOOK_SAY_DO_GAP" \
    "$CORE_HOOK_STATE_CLAIM_GATE"; do
    if [[ -x "$f" ]]; then
        ok "exec: $(basename "$f")"
    elif [[ -f "$f" ]]; then
        warn "no-exec: $(basename "$f")"
        [[ "$f" == "$CORE_HOOK_STOP" ]] && FAILED_INVARIANTS=$((FAILED_INVARIANTS+1))
    else
        bad "missing: $(basename "$f")"
        if [[ "$f" == "$CORE_HOOK_STOP" || "$f" == "$CORE_HOOK_END_SESSION" ]]; then
            FAILED_INVARIANTS=$((FAILED_INVARIANTS+1))
        fi
    fi
done

# ── 7b. Brain Postgres + brain_app role ──────────────────────────────────
# Honour COREBRAIN_DB. This block hardcoded 'corebrain', so when setup-brain.sh ran
# against any other database and then called core-doctor as its final verification
# step, the doctor reported green about a DIFFERENT database than the one just
# provisioned — and on a machine with no 'corebrain' at all it validated nothing.
# (Found 2026-07-27 running setup-brain.sh against a throwaway DB.)
DOCTOR_DB="${COREBRAIN_DB:-corebrain}"
hdr "Brain Postgres ($DOCTOR_DB) + brain_app role"
if ! command -v psql >/dev/null 2>&1; then
    info "${DIM}(psql not installed — brain recall runs in grep-fallback mode)${RESET}"
elif ! psql -d "$DOCTOR_DB" -c 'SELECT 1' >/dev/null 2>&1; then
    # WARNING, not FAILED INVARIANT: this line's own message says why — recall has a real,
    # designed fallback (scheduling/brain-pg/query.py's grep_baseline(), confirmed on read) that
    # runs when Postgres is unreachable. Degraded, not broken.
    warn "$DOCTOR_DB DB unreachable — recall silently degrades to grep. Start Postgres / create the DB + apply schema.sql."
else
    ok "$DOCTOR_DB reachable"
    if psql -d "$DOCTOR_DB" -tAc "SELECT 1 FROM pg_roles WHERE rolname='brain_app'" 2>/dev/null | grep -q 1; then
        ok "brain_app role exists"
    else
        # WARNING, not FAILED INVARIANT, for the same reason as DB-unreachable above: this
        # exact message says "recall falls back to grep" — bin/init-brain-roles.sh's own header
        # confirms it ("recall silently degrades to the grep fallback"). A missing role
        # degrades recall; it does not stop the Core from running.
        bad "brain_app role MISSING — query.py/embed.py can't connect, recall falls back to grep. Fix: bash bin/init-brain-roles.sh"
    fi
    # SI-spine schema presence. The doctor previously reported the brain "green"
    # while every learned-layer table was absent, which is how the phantom-migration
    # bug stayed invisible.
    _missing=""
    for _t in si_artifacts si_projection_state friction_cases learned_contracts pattern_observations; do
        psql -d "$DOCTOR_DB" -tAc "SELECT to_regclass('public.$_t') IS NOT NULL" 2>/dev/null | grep -q '^t$' || _missing="$_missing $_t"
    done
    if [[ -n "$_missing" ]]; then
        # FAILED INVARIANT vs WARNING here is NOT unconditional — measured 2026-08-31 running
        # the documented fresh install (docs/SETUP.md steps 2-3: `setup-brain.sh`, THEN
        # `install-learned-layer.sh`) on a brand-new scratch DB. setup-brain.sh's own step-8
        # verification calls this file BEFORE install-learned-layer.sh has ever run — and these
        # tables are install-learned-layer.sh's to create (step 3, out of setup-brain.sh's
        # scope per docs/SETUP.md's own step table). So on EVERY first-ever install this branch
        # was reached with the FAILED-INVARIANT classification 130a697 gave it, and setup-
        # brain.sh exited 1 before step 3 could even run — a hard blocker on every fresh Core,
        # not the wiring-audit misattribution first suspected (section 14 below never touches
        # FAILED_INVARIANTS — grep confirms it — so that audit was never the cause).
        #
        # The fix distinguishes "step 3 hasn't run YET" (expected, not broken) from "step 3 ran
        # and something broke since" (the genuine regression 130a697 exists to catch — e.g.
        # `DROP TABLE learned_contracts` on an established Core). schema_migrations already
        # carries that signal for free: 2026-07-19-learned-contracts-rls.sql is DEFERRED by
        # run-migrations.sh (see that file's apply_tolerant()) until learned_contracts exists,
        # and only gets RECORDED once install-learned-layer.sh's own --ensure re-run picks it
        # up (see that script's step 1 comment). Recorded-but-now-missing = regression = FAILED
        # INVARIANT, unchanged from 130a697. Never-recorded = step 3 pending = WARNING. Checked
        # against $DOCTOR_DB specifically (not settings.json's hook registration) so this stays
        # correct under a COREBRAIN_DB override, same as every other check in this section.
        if psql -d "$DOCTOR_DB" -tAc "SELECT 1 FROM schema_migrations WHERE filename = '2026-07-19-learned-contracts-rls.sql'" 2>/dev/null | grep -q 1; then
            bad "SI-spine tables MISSING:$_missing — the self-improvement loop cannot run. Fix: bash bin/run-migrations.sh --ensure && bash bin/install-learned-layer.sh"
            FAILED_INVARIANTS=$((FAILED_INVARIANTS+1))
        else
            warn "SI-spine tables not yet installed:$_missing — docs/SETUP.md step 3 hasn't run on this Core yet. Fix: bash bin/install-learned-layer.sh"
        fi
    else
        ok "SI-spine schema present (artifacts, projection, friction, contracts, observations)"
        # EVIDENCE COVERAGE. The evidence write is fail-soft by design — an install must not die
        # over bookkeeping — but a soft failure reproduces the original symptom exactly: no
        # evidence, so nothing to narrow against, so the watchdog quarantines instead of tuning.
        # That is invisible unless something counts it. (core-business, finding 9b.)
        #
        # SCOPED TO THIS CORE'S ORG. The first version had no org_id filter, so a freshly
        # spawned Core reported "0/26" — core-life's artifact count — while owning zero
        # artifacts of its own. Caught by spawning a Core from the baseline and reading what
        # its own doctor said, not by reading this query. Same cross-Core class as the
        # /tmp/core-hook-events.log bleed and the hardcoded backfill path, in a panel added
        # the same day as the test written to catch that class.
        _org=$(python3 -c "import json,sys;print(json.load(open('$CORE_IDENTITY_JSON')).get('org_id',''))" 2>/dev/null)
        [[ -n "$_org" ]] || _org="${CORE_ORG_ID:-}"
        # Must be a bare integer before it is interpolated into SQL. org_id IS an integer
        # column, so validating is the correct fix rather than quoting. sentinel-code flagged
        # the unquoted interpolation as a nit and judged it non-exploitable from this push —
        # true, since the value comes from per-Core identity.json — but "not reachable today"
        # is how a real injection surface gets shipped to a baseline an external fork pulls.
        [[ "$_org" =~ ^[0-9]+$ ]] || _org=""
        if [[ -n "$_org" ]]; then
            _ev=$(psql -d "$DOCTOR_DB" -tAc "SELECT count(*) FILTER (WHERE evidence IS NOT NULL), count(*) FROM si_artifacts WHERE active AND provenance <> 'legacy' AND org_id = $_org" 2>/dev/null)
        else
            _ev=""
        fi
        if [[ -n "$_ev" ]]; then
            _with="${_ev%%|*}"; _tot="${_ev##*|}"
            if [[ "${_tot:-0}" -gt 0 && "${_with:-0}" -lt "${_tot:-0}" ]]; then
                warn "artifact evidence: ${_with}/${_tot} — the rest cannot be tuned, only quarantined"
            elif [[ "${_tot:-0}" -gt 0 ]]; then
                ok "artifact evidence: ${_with}/${_tot} artifacts can be tuned"
            fi
        fi
    fi
fi

# ── 8. Brain repo push lag (audit Dim 9 #1 fix-a) ────────────────────────
hdr "Brain repo push lag"
if [[ -d "$BRAIN/.git" ]]; then
    UNPUSHED=$(cd "$BRAIN" 2>/dev/null && git rev-list --count '@{u}..HEAD' 2>/dev/null)
    UNPUSHED=${UNPUSHED:-0}
    if [[ "$UNPUSHED" =~ ^[0-9]+$ ]] && (( UNPUSHED > 0 )); then
        warn "brain repo: ${UNPUSHED} unpushed commit(s) — run \`git push\` in '$BRAIN'"
    else
        ok "brain repo: in sync with upstream"
    fi
else
    info "${DIM}(no brain repo at $BRAIN)${RESET}"
fi

# ── 9. Stop-hook recent activity (brain auto-update commits, last 24h) ──
hdr "Stop-hook recent activity (brain commits, 24h)"
if [[ -d "$BRAIN/.git" ]]; then
    BRAIN_24H=$(git -C "$BRAIN" log --oneline --since='24.hours.ago' 2>/dev/null | wc -l | tr -d ' ')
    BRAIN_24H=${BRAIN_24H:-0}
    if (( BRAIN_24H == 0 )); then
        warn "0 brain commits in 24h — Stop hook may not be firing"
    elif (( BRAIN_24H > 30 )); then
        warn "${BRAIN_24H} brain commits in 24h (high — possible loop?)"
    else
        ok "${BRAIN_24H} brain commit(s) in last 24h"
    fi
else
    info "${DIM}(no brain repo)${RESET}"
fi

# ── 10. Colima / Docker (untrusted-reader sandbox) ───────────────────────
hdr "Colima / Docker"
if command -v colima >/dev/null 2>&1; then
    CSTATUS=$(colima status 2>&1)
    # Order matters: check "not running"/"stopped" BEFORE "running" — the
    # "is not running" fatal message contains the substring "running".
    if echo "$CSTATUS" | grep -qiE "not running|stopped|fatal"; then
        info "${DIM}colima: stopped (idle — fine; on-demand starts via colima-ensure.sh)${RESET}"
    elif echo "$CSTATUS" | grep -qi "running"; then
        UPTIME=$(echo "$CSTATUS" | grep -iE "uptime|started|created" | head -1 | sed 's/^[[:space:]]*//')
        ok "colima: running ${UPTIME:+(${UPTIME})}"
    else
        info "$(echo "$CSTATUS" | head -1)"
    fi
else
    info "${DIM}(colima not installed)${RESET}"
fi

# ── 11. Hook block events (last 24h) ─────────────────────────────────────
# THIS CORE's log, not a machine-global one. /tmp/core-hook-events.log is shared by every
# Core on the box AND is the path hooklog.py migrated OFF of, so this panel was reporting
# another Core's stale events as if they were this one's. Caught in a spawn dress rehearsal:
# a brand-new Core reported "24 events, hook=say-do-gap-test" — all of them core-life's.
# Same class as the hardcoded-path bug in backfill-hook-events.py.
hdr "Hook block events (last 24h, .claude/state/hook-events.log)"
HOOK_LOG="$CORE/.claude/state/hook-events.log"
# Legacy fallback so a Core that has not written a durable log yet still reports something,
# but only when it has no log of its own — never in preference to it.
[[ -f "$HOOK_LOG" ]] || HOOK_LOG="/tmp/core-hook-events.log"
if [[ -f "$HOOK_LOG" ]]; then
    # PARSE THE FORMAT WE ACTUALLY WRITE. hooklog.py emits:
    #   2026-07-28T09:06:37.964Z | hook=NAME | event=E | verdict=V | session=S | excerpt=...
    # The old parser hunted for a token matching -(hook|gap|gate|guard|trigger), which misses
    # every hook whose name lacks that suffix (recall-satisfied, brain-recall-trigger, ...) and
    # is confused by the pipe separators. It reported 1580 of 2084 events as "unknown" — a panel
    # that is 76% unknown is not telling anyone anything.
    SINCE=$(date -u -v-24H "+%Y-%m-%dT%H:%M" 2>/dev/null)
    RECENT=$(awk -v since="$SINCE" 'substr($0,1,16) >= since' "$HOOK_LOG" 2>/dev/null)
    TOTAL=$(printf '%s' "$RECENT" | grep -c . 2>/dev/null)
    TOTAL=${TOTAL:-0}
    if (( TOTAL == 0 )); then
        info "${DIM}(0 events in last 24h)${RESET}"
    else
        # BLOCKS ARE THE HEADLINE. The panel is titled "block events" but was counting every
        # invocation, so a healthy Core looked like it was blocking thousands of times a day.
        # Blocks are what a human needs to act on; invocations are volume.
        BLOCKS=$(printf '%s\n' "$RECENT" | grep -c "verdict=block" 2>/dev/null); BLOCKS=${BLOCKS:-0}
        info "${BOLD}${BLOCKS}${RESET} block(s), ${TOTAL} total event(s) in last 24h"
        if (( BLOCKS > 0 )); then
            printf '%s\n' "$RECENT" | grep "verdict=block" \
              | sed -n 's/.*hook=\([^ |]*\).*/\1/p' \
              | sort | uniq -c | sort -rn | head -8 | sed 's/^/    blocked: /'
        fi
        printf '%s\n' "$RECENT" | sed -n 's/.*hook=\([^ |]*\).*/\1/p' \
          | sort | uniq -c | sort -rn | head -5 | sed 's/^/    /'
    fi
else
    info "${DIM}(no $HOOK_LOG yet — hook event logger not deployed)${RESET}"
fi

# ── 12. Job Hunter recent runs (success markers) ─────────────────────────
# Skipped entirely if job_hunter not configured in identity.json.
if [[ -n "$JH_SUCCESS_PREFIX" ]]; then
    hdr "Job Hunter recent runs"
    JH_MARKERS=$(ls -t "${JH_SUCCESS_PREFIX}"* 2>/dev/null | head -5)
    if [[ -n "$JH_MARKERS" ]]; then
        while IFS= read -r m; do
            ts=$(stat -f "%Sm" "$m" 2>/dev/null || echo "?")
            mtime=$(stat -f "%m" "$m" 2>/dev/null || echo 0)
            age_h=$(( (NOW_EPOCH - mtime) / 3600 ))
            date=$(basename "$m" | sed "s|$(basename "$JH_SUCCESS_PREFIX")||")
            if (( age_h > 96 )); then
                warn "$date  (${age_h}h ago — ${ts})"
            else
                ok "$date  (${age_h}h ago)"
            fi
        done <<< "$JH_MARKERS"
    else
        info "${DIM}(no ${JH_SUCCESS_PREFIX}* markers — likely cleared on reboot)${RESET}"
    fi
    # Also surface any current failure markers
    if [[ -n "$JH_FAILED_PREFIX" ]]; then
        JH_FAILS=$(ls "${JH_FAILED_PREFIX}"* 2>/dev/null)
        if [[ -n "$JH_FAILS" ]]; then
            while IFS= read -r m; do
                bad "FAILURE marker present: $m"
            done <<< "$JH_FAILS"
        fi
    fi
fi

# ── 13. Cross-worktree status (ahead/behind vs main) ─────────────────────
hdr "Worktrees (ahead/behind vs main)"
THIS_REPO=$(git -C "$CORE" rev-parse --show-toplevel 2>/dev/null)
CURR_WT=""; CURR_BR=""
WT_ANY=0
while IFS= read -r LINE; do
    if [[ "$LINE" == worktree\ * ]]; then
        CURR_WT="${LINE#worktree }"; CURR_BR=""
    elif [[ "$LINE" == branch\ refs/heads/* ]]; then
        CURR_BR="${LINE#branch refs/heads/}"
    elif [[ -z "$LINE" ]]; then
        if [[ -n "$CURR_WT" && -n "$CURR_BR" && -d "$CURR_WT" ]]; then
            WT_ANY=1
            DIRTY=$(git -C "$CURR_WT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
            DIRTY=${DIRTY:-0}
            if [[ "$CURR_BR" == "main" ]]; then
                AHEAD=0; BEHIND=0
            else
                AHEAD=$(git -C "$CORE" rev-list --count "main..${CURR_BR}" 2>/dev/null || echo 0)
                BEHIND=$(git -C "$CORE" rev-list --count "${CURR_BR}..main" 2>/dev/null || echo 0)
            fi
            NAME=$(basename "$CURR_WT")
            LINE_OUT=$(printf "%-20s %-12s ahead=%-3s behind=%-3s dirty=%s" "$NAME" "$CURR_BR" "$AHEAD" "$BEHIND" "$DIRTY")
            if (( DIRTY > 0 )) || { (( AHEAD > 0 )) && [[ "$CURR_BR" != "main" ]]; }; then
                warn "$LINE_OUT"
            else
                ok "$LINE_OUT"
            fi
        fi
        CURR_WT=""; CURR_BR=""
    fi
done < <(git -C "$CORE" worktree list --porcelain 2>/dev/null; echo "")
if (( WT_ANY == 0 )); then
    info "${DIM}(no worktrees found)${RESET}"
fi

# ── 14. Wiring audit — is every built component reachable from something that RUNS? ──
# Added 2026-08-28. Three subsystems were found built-but-never-wired the same day
# (compile-truth-refresh, corroborate, artifact_utility). Each failed SILENTLY, because a
# component that never runs is indistinguishable from one with nothing to do. This is the
# instrument that would have caught all three. Fail-open: a doctor must never break a session.
hdr "14. Wiring audit (built vs. actually reachable)"
if [[ -f "$CORE/bin/wiring-audit.py" ]]; then
    WIRING_OUT="$(CORE_INSTANCE="$CORE" python3 "$CORE/bin/wiring-audit.py" --json 2>/dev/null || true)"
    if [[ -n "$WIRING_OUT" ]]; then
        python3 - "$WIRING_OUT" <<'PYW' 2>/dev/null || info "(wiring audit unreadable)"
import json,sys
try: d=json.loads(sys.argv[1])
except Exception: raise SystemExit
u=d.get("unreachable_undeclared") or []
m=d.get("manual_only") or []
print(f"  scanned {d.get('scanned')} · auto-wired {d.get('auto_wired')} · manual-only {len(m)} · undeclared {len(u)}")
if u:
    print("  ✗ BUILT BUT NEVER WIRED (undeclared):")
    for r in u[:12]: print(f"      {r}")
    if len(u)>12: print(f"      … and {len(u)-12} more")
    print("  → wire it, or declare it in bin/wiring-allowlist.json with a reason")
else:
    print("  ✅ nothing built is unreachable and undeclared")
PYW
    else
        info "(wiring audit produced no output)"
    fi
else
    info "(bin/wiring-audit.py not present on this seat yet)"
fi

# ── 15. Close sequence — does the code still match the declared order? ───────────────
# Added 2026-08-28. The close is 27 invocations across 732 lines and had no declared order,
# no phases, and no way to see which steps the NEXT session depends on. Six of that file's
# reversals began with a step moving without a shared picture of what the close is.
# .claude/close-sequence.json is that picture; this asserts the code has not drifted from it.
# Fail-open.
hdr "15. Close sequence (declared vs. actual)"
if [[ -f "$CORE/bin/verify-close-sequence.py" ]]; then
    CLOSE_OUT="$(CORE_INSTANCE="$CORE" python3 "$CORE/bin/verify-close-sequence.py" --json 2>/dev/null || true)"
    if [[ -n "$CLOSE_OUT" ]]; then
        python3 - "$CLOSE_OUT" <<'PYC' 2>/dev/null || info "(close-sequence output unreadable)"
import json,sys
try: d=json.loads(sys.argv[1])
except Exception: raise SystemExit
print(f"  {d.get('declared')} declared / {d.get('invoked')} invoked · order {'OK' if d.get('order_matches') else 'DRIFTED'}")
for k,lab in (("missing","declared but NOT invoked"),("undeclared","invoked but NOT declared")):
    if d.get(k): print(f"  ✗ {lab}: {', '.join(d[k])}")
for x in d.get("sync_drift") or []:
    print(f"  ✗ sync drift: {x['step']} declared {x['declared']}, actually {x['actual']}")
for s in d.get("declared_missing_from_close") or []:
    print(f"  ⚠ declared missing from the close: {s}")
if not (d.get("missing") or d.get("undeclared") or d.get("sync_drift")) and d.get("order_matches"):
    print("  ✅ close matches its declared sequence")
PYC
    fi
else
    info "(bin/verify-close-sequence.py not present on this seat yet)"
fi

# ── Footer ──────────────────────────────────────────────────────────────
printf "\n${BOLD}════════════════════════════════════════════════════════${RESET}\n"
if (( FAILED_INVARIANTS > 0 )); then
    printf "${BOLD}${RED} End core-doctor — %d FAILED INVARIANT(s) above. Exit 1.${RESET}\n" "$FAILED_INVARIANTS"
else
    printf "${BOLD} End core-doctor — verify any state claim against this output.${RESET}\n"
fi
printf "${BOLD}════════════════════════════════════════════════════════${RESET}\n"

# Exit status (2026-08-31): see header. `set +e` above already let every check above run and
# print regardless of what earlier checks found — this is the ONLY place the script can exit,
# so the full report is never truncated by a caller-visible failure.
if (( FAILED_INVARIANTS > 0 )); then
    exit 1
fi
exit 0
