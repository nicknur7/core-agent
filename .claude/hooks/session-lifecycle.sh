#!/usr/bin/env bash
# session-lifecycle.sh — the ONE controller for the session lifecycle.
#
# Phases:
#   start            orient only — anchor session-start time + brain truth-drift
#                    check. Read-only except the time anchor. (The rich
#                    SessionStart surfacing still lives in session-start-check.sh,
#                    which calls this for the anchor.)
#   close [MODE]     the single write path. MODE = full | defensive (default).
#                    Collapses the former stop-hook.sh + defensive-save.sh
#                    duplicate commit engines into one. full = /close-core
#                    sentinel path; defensive = walk-away (SessionEnd) path.
#   nightly          the only heavy build (graphify → entities+edges, all-org).
#
# Design rule (Phase 0, spec-brain-unfreeze-2026-05-28): every artifact is
# written by exactly ONE phase. The safety scan + commit logic lives here ONCE;
# the thin hooks call into it and never reimplement it.
set -uo pipefail

REPO=$(git rev-parse --show-toplevel 2>/dev/null || echo "${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}")
# shellcheck source=bin/core-paths.sh
source "$REPO/bin/core-paths.sh"
TODAY=$(date +%Y-%m-%d)

# ─────────────────────────────────────────────────────────────────────────────
# SHARED: pre-commit safety scan. Lifted verbatim from defensive-save.sh — the
# canonical 6-check version. Sets BLOCKED + BLOCK_REASONS[]. Single source now.
# ─────────────────────────────────────────────────────────────────────────────
# A FILE-LIST ALARM MUST CARRY A FILE LIST. Validate the value before alarming on it.
#
# On 2026-08-09 this emitted, into the log and into the /core-si queue:
#
#     !! CREDENTIAL FILES STAGED: [session-lifecycle] SELF-TESTS FAILED — see /tmp/…log
#     !! KEYCHAIN-ADJACENT FILES STAGED: [session-lifecycle] SELF-TESTS FAILED — see /tmp/…log
#     !! AUTO-MEMORY NAMESPACE FILES STAGED: [session-lifecycle] SELF-TESTS FAILED — see /tmp/…log
#
# An error message occupying the slot where a list of paths belongs, in all three alarms. Checked
# the substantive question directly and the answer was clean: no credential-shaped file was staged,
# and none is tracked. So the queue carried a red reading CREDENTIAL FILES STAGED for a day on a
# value that could not have been a file list.
#
# I DO NOT KNOW HOW THE VALUE GOT THERE, and this fix does not depend on knowing. The three
# constructions read `git diff --cached --name-only | grep -E …`, which cannot produce that string,
# and I would rather make corruption VISIBLE than ship a guess about its origin. Validating the
# shape is correct whatever the mechanism, and if the mechanism recurs the new reason names it as
# an internal fault instead of as a credential leak.
#
# The direction matters: a false CREDENTIAL alarm is the most expensive kind of false alarm, because
# it is the one nobody can safely ignore.
# Returns 0 when the value IS a file list (a real finding, block it) and 1 when it is an internal
# reporting fault (name it, but DO NOT BLOCK).
#
# 2026-08-12: relabelling was not enough. The guard correctly rendered the corrupt value as
# "INTERNAL: … treat as a reporting fault, not a leak" — and then the caller set BLOCKED=1 anyway,
# because it only checked that the variable was non-empty. So a reporting fault still wrote
# .last-save-blocked, and that marker pins .end-session-requested (stop-hook.sh:26-29), and every
# assistant turn then ran a FULL close: 105 on core-life and 92 on core-business on 2026-08-11,
# each dying in a 6-minute pg_dump inside a 60-second hook, orphaning 671 fragments and 128 GB.
#
# The whole chain starts here, with a save blocked for a reason that is not a leak. Naming the fault
# while still acting on it is the worst of both: it looks handled in the log and behaves exactly as
# if a credential had been staged. A reporting fault must be LOUD and INERT.
#
# I still cannot explain how the self-test's stderr reaches a variable built from
# `git diff --cached --name-only | grep -E … || true`, which cannot emit that string. That is
# recorded rather than guessed at. This fix does not depend on knowing: whatever the mechanism, a
# value that is not a file list is not evidence of a leak and must not stop a save.
_file_list_reason() {   # label, value  -> echoes the reason; exit 1 == internal fault, do not block
  # PARTITION PER LINE. A single _bad flag over the whole value was a credential-blocking HOLE,
  # caught by core-business on review 2026-08-12 by EXECUTING this function against mixed input
  # rather than reading it:
  #
  #     secrets/id_rsa
  #     config/prod — old.env      <- one em dash in a sibling line
  #     .ssh/id_ed25519
  #       -> whole alarm inert. TWO genuine private keys staged and NOT blocked.
  #
  # A filename containing an em dash or a leading bracket is not exotic, and it need not be
  # adversarial — an accidental name is enough. My own controls missed it because they tested a
  # PURE-clean value and a PURE-corrupt value and never a mixed one, which is the only case where
  # the distinction bites. A control that cannot fail on the interesting input is decoration.
  #
  # My comment on the previous version said "a false CREDENTIAL alarm is the most expensive kind of
  # false alarm, because it is the one nobody can safely ignore." That is true and it argued for
  # exactly the wrong trade: a false NEGATIVE here is worse, because nobody sees it at all. Loud and
  # wrong beats silent and wrong.
  #
  # So the two kinds coexist: real paths still block, prose lines are reported as an internal fault,
  # and one does not suppress the other.
  # MATCH THE PATTERN, DO NOT GUESS THE SHAPE. A punctuation heuristic cannot separate
  # "secrets/prod — copy.env" (a real .env whose NAME contains an em dash) from a prose line, and
  # guessing wrong in that direction is a silent credential pass-through.
  #
  # There is an exact signal available and the first two versions of this ignored it: every line in
  # $_val is the output of `git diff --cached --name-only | grep -E "$_pat"`, so every LEGITIMATE
  # line already matched $_pat by construction. Re-checking each line against the caller's own
  # pattern is therefore precise rather than heuristic — a line that does not match it could not
  # have come from that grep, which is exactly the anomaly worth naming.
  local _label="$1" _val="$2" _pat="${3:-}" _line _paths="" _faults=""
  while IFS= read -r _line; do
    [[ -z "$_line" ]] && continue
    if [[ -n "$_pat" ]]; then
      if printf '%s' "$_line" | grep -qE "$_pat"; then _paths+="${_line}"$'\n'
      else                                             _faults+="${_line}"$'\n'; fi
      continue
    fi
    # No pattern supplied (older callers): fall back to the shape check, which is weaker.
    case "$_line" in
      \[*|*" — "*) _faults+="${_line}"$'\n' ;;
      *)           _paths+="${_line}"$'\n'  ;;
    esac
  done <<< "$_val"

  if [[ -n "$_paths" ]]; then
    # Real paths present. BLOCK on those, and say so if noise rode along with them.
    printf '%s: %s' "$_label" "$(printf '%s' "$_paths" | tr '\n' ' ' | sed 's/ *$//')"
    [[ -n "$_faults" ]] && printf ' (plus %d non-path line(s) treated as a reporting fault)' \
      "$(printf '%s' "$_faults" | grep -c '^')"
    return 0
  fi

  printf 'INTERNAL: %s alarm carried a value that is not a file list (%s) — treat as a reporting fault, not a leak' \
    "$_label" "$(printf '%s' "$_val" | head -c 120 | tr '\n' ' ')"
  return 1
}

# Record a file-list finding. Blocks ONLY when the value is genuinely a file list; an internal
# fault is logged loudly and left inert. Every file-list alarm goes through here so the
# block-or-not decision exists in exactly one place — three copies of this rule is three chances
# for the next one to keep blocking on garbage.
_note_file_list() {   # label, value, [pattern the value was grepped with]
  local _reason
  if _reason=$(_file_list_reason "$1" "$2" "${3:-}"); then
    BLOCKED=1
    BLOCK_REASONS+=("$_reason")
  else
    BLOCK_REASONS+=("$_reason")
    echo "[session-lifecycle] $_reason" >&2
  fi
}

# A COUNT GUARD MUST DISTINGUISH "clean" FROM "the producer failed". They are not the same result,
# and treating them the same is failing OPEN — core-business, 2026-08-12.
#
# My first attempt at this was adding `=~ ^[0-9]+$` to three guards to match the one that had it.
# Dosed it: behaviour IDENTICAL. Empty and non-numeric were already inert under both idioms, so the
# validation changed nothing and I would have reported it as a fix. Numeric validation is the wrong
# tool — the guards were already effectively inert on garbage; the defect is that inert is SILENT.
#
# A crashed linter and a clean tree produce the same close. Not blocking on it (a lint crash is not
# a credential leak, and blocking every save on a broken tool is the over-correction that started
# this whole chain) — but it must be VISIBLE, exactly like the internal-fault path for the list
# alarms. Same treatment for the same reason: loud and inert beats silent and inert.
_count_or_report() {   # label, value  -> echoes a clean integer, or 0 after reporting the fault
  local _label="$1" _val="$2"
  if [[ "$_val" =~ ^[0-9]+$ ]]; then
    printf '%s' "$_val"
    return 0
  fi
  echo "[session-lifecycle] INTERNAL: ${_label} produced no usable count (got: $(printf '%s' "${_val:-<empty>}" | head -c 80 | tr '\n' ' ')) — guard is INERT this pass, not clean" >&2
  printf '0'
}

_safety_scan() {
  BLOCKED=0
  BLOCK_REASONS=()

  local CRED_FILES KEYCHAIN_FILES NAMESPACE_FILES STAGED_MD STAGED_CODE
  CRED_FILES=$(git diff --cached --name-only 2>/dev/null | grep -E \
    '(\.ssh/|\.pem$|\.key$|\.env$|credentials|\.p12$|\.pfx$|id_rsa|id_ed25519|\.secret)' \
    || true)
  if [[ -n "$CRED_FILES" ]]; then
    _note_file_list "CREDENTIAL FILES STAGED" "$CRED_FILES" '(\.ssh/|\.pem$|\.key$|\.env$|credentials|\.p12$|\.pfx$|id_rsa|id_ed25519|\.secret)'
  fi

  if git diff --cached --name-only 2>/dev/null | grep -q "settings.local.json"; then
    local ADDED_PERMS
    ADDED_PERMS=$(git diff --cached -- .claude/settings.local.json 2>/dev/null \
      | grep '^+' | grep -v '^+++' | grep -cE '"(Bash|Read|Write|WebFetch|WebSearch)\(' \
      | tr -d ' ')
    # NUMERIC VALIDATION, matching ORG-SCOPE below. core-business, 2026-08-12: these four count
    # guards were four different idioms and only ORG-SCOPE validated. `[[ "" -gt 0 ]]` on an empty
    # value errors and returns non-zero, so the guard SILENTLY PASSES — a crashed producer disarms
    # the check. For a safety gate failing OPEN is worse than the false-positive I spent tonight
    # fixing: nobody sees it. The correct idiom was already in this function, sixty lines away.
    ADDED_PERMS=$(_count_or_report "PERMISSION EXPANSION" "$ADDED_PERMS")
    if (( ADDED_PERMS > 0 )); then
      BLOCKED=1; BLOCK_REASONS+=("PERMISSION EXPANSION in settings.local.json: $ADDED_PERMS new permission line(s) added")
    fi
  fi

  KEYCHAIN_FILES=$(git diff --cached --name-only 2>/dev/null | grep -E \
    'Library/(Keychains|Preferences/com\.apple\.security|Application Support/com\.apple\.(keychain|security))' \
    || true)
  if [[ -n "$KEYCHAIN_FILES" ]]; then
    _note_file_list "KEYCHAIN-ADJACENT FILES STAGED" "$KEYCHAIN_FILES" 'Library/(Keychains|Preferences/com\.apple\.security|Application Support/com\.apple\.(keychain|security))'
  fi

  NAMESPACE_FILES=$(git diff --cached --name-only 2>/dev/null | grep -E \
    '^memory/feedback_.*\.md$' \
    || true)
  if [[ -n "$NAMESPACE_FILES" ]]; then
    _note_file_list "AUTO-MEMORY NAMESPACE FILES STAGED" "$NAMESPACE_FILES"
  fi

  STAGED_MD=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null | grep -E '\.md$' || true)
  if [[ -n "$STAGED_MD" ]] && [[ -x "$CORE_BIN_LINT_DOC_PATHS" ]]; then
    local LINT_OUTPUT LINT_BROKEN
    # shellcheck disable=SC2086
    # 2>&1 MERGES STDERR INTO A PARSED VARIABLE. Noted 2026-08-12 while looking for how the
    # self-test's stderr reached a file-list variable on 2026-08-09 — an origin still unexplained.
    # This is the SHAPE that would do it: any traceback or warning the linter writes lands in
    # $LINT_OUTPUT and is then parsed for a count.
    #
    # It is currently harmless, and harmless BY ACCIDENT rather than by design: the extraction is
    # `grep -oE '[0-9]+'`, so a traceback yields empty and the alarm does not fire. Change the
    # extraction to anything that passes text through and this becomes the file-list bug again.
    # Left as-is with the reason recorded, because narrowing it to 2>/dev/null would DISCARD linter
    # errors that are worth seeing — the fix is to keep the merge and never let the parsed value
    # reach a message unvalidated, which is what _note_file_list now enforces for the list alarms.
    LINT_OUTPUT=$(cd "$REPO" && python3 "$CORE_BIN_LINT_DOC_PATHS" --paths $STAGED_MD 2>&1 || true)
    LINT_BROKEN=$(echo "$LINT_OUTPUT" | grep -oE 'Broken in-repo path references: [0-9]+' | grep -oE '[0-9]+' | head -1)
    # `(( X > 0 ))` on non-numeric TEXT treats it as a variable name -> 0 -> false. Fails open.
    LINT_BROKEN=$(_count_or_report "DOC-PATH DRIFT" "$LINT_BROKEN")
    if (( LINT_BROKEN > 0 )); then
      BLOCKED=1; BLOCK_REASONS+=("DOC-PATH DRIFT in staged .md: ${LINT_BROKEN} broken in-repo path citation(s). Run \`bash bin/lint-doc-paths.sh --paths <files>\` to see them.")
    fi
  fi

  STAGED_CODE=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null | grep -E '\.(sh|py)$' || true)
  if [[ -n "$STAGED_CODE" ]] && [[ -x "$CORE_BIN_LINT_CODE_PATHS" ]]; then
    local CODE_LINT_OUTPUT CODE_LINT_BROKEN
    # shellcheck disable=SC2086
    CODE_LINT_OUTPUT=$(cd "$REPO" && python3 "$CORE_BIN_LINT_CODE_PATHS" --paths $STAGED_CODE 2>&1 || true)
    CODE_LINT_BROKEN=$(echo "$CODE_LINT_OUTPUT" | grep -oE 'hardcoded outside the registry: [0-9]+' | grep -oE '[0-9]+' | head -1)
    # Same as DOC-PATH above: -n is not a numeric check.
    CODE_LINT_BROKEN=$(_count_or_report "CODE-PATH DRIFT" "$CODE_LINT_BROKEN")
    if (( CODE_LINT_BROKEN > 0 )); then
      BLOCKED=1; BLOCK_REASONS+=("CODE-PATH DRIFT in staged .sh/.py: ${CODE_LINT_BROKEN} registry-tracked path(s) hardcoded outside the registry.")
    fi
  fi

  # Multi-tenant scope guard: if a brain-pg .py is staged, ensure no SQL string
  # interpolates org_id unsafely (current_setting / bound-param IN-list only).
  STAGED_BRAIN_PG=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null | grep -E '^scheduling/brain-pg/.*\.py$' || true)
  if [[ -n "$STAGED_BRAIN_PG" ]] && [[ -x "$CORE_BIN_LINT_ORG_SCOPING" ]]; then
    local ORG_LINT_COUNT
    ORG_LINT_COUNT=$(cd "$REPO" && python3 "$CORE_BIN_LINT_ORG_SCOPING" --count 2>/dev/null || echo 0)
    ORG_LINT_COUNT=$(_count_or_report "ORG-SCOPE RISK" "$ORG_LINT_COUNT")
    if (( ORG_LINT_COUNT > 0 )); then
      BLOCKED=1; BLOCK_REASONS+=("ORG-SCOPE RISK in staged brain-pg .py: ${ORG_LINT_COUNT} unsafe org_id interpolation(s). Run \`bash bin/lint-org-scoping.sh\` to see them.")
    fi
  fi
}

# Write the blocked marker (read by SessionStart check o / core-si sys-saveblock).
_write_blocked_marker() {
  local by="$1"
  mkdir -p "$(dirname "$CORE_LAST_SAVE_BLOCKED")" 2>/dev/null || true
  {
    echo "BLOCKED_AT=$(date +%Y-%m-%dT%H:%M:%S%z)"
    echo "BLOCKED_BY=$by"
    echo "STAGED_FILES=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')"
    local reason
    for reason in "${BLOCK_REASONS[@]}"; do echo "REASON=$reason"; done
  } > "$CORE_LAST_SAVE_BLOCKED"
}

# ─────────────────────────────────────────────────────────────────────────────
# PUSH GUARD — never let a Core's session state reach the shared baseline
#
# On 2026-06-19 a spawned Core auto-committed its own instance state into
# the baseline repo: its owner's identity.json, his personalised CLAUDE.md, his memory/,
# and a third party's email address. It also deleted the template scaffolding. The
# commit message on it, "Session … defensive auto-save (no explicit close)", is
# generated verbatim a few lines below this comment. His Core did exactly what this
# file told it to.
#
# The mechanism was two unguarded lines meeting one missing line:
#   · bin/init-multi-core.sh clones from the baseline repo and never repoints origin
#   · both push sites here ran a bare `git push` with no idea where origin pointed
# So every spawned Core pushed its session state to the shared template. The only
# reason this Core is unaffected is that its origin was repointed BY HAND.
#
# THE WRITER TEST IS NOT A SINGLE FLAG. hook_profile.role controls hook composition,
# not repository ownership — a fork can edit it, and a copied writer identity keeps
# it. So enrollment requires all three: the manifest names this Core as the baseline
# writer, the directory name matches, and a local-only marker exists that is excluded
# from baseline sync (so it can never be copied into another Core by a pull).
#
# This stops accidents and contaminated state. It is not a defence against a hostile
# operator with a shell, and it does not need to be.
# ─────────────────────────────────────────────────────────────────────────────
_normalize_remote() {
  # git@github.com:Owner/Repo.git · https://github.com/owner/repo/ · ssh://… → owner/repo
  #
  # `https\?` is a GNU-ism. BSD sed (macOS, which is the only platform this ships on)
  # treats \? literally, so the host never got stripped, every URL normalised to
  # "https://github.com/owner/repo", and it could never equal the manifest's
  # "owner/repo" — the guard would have been permanently open while looking correct.
  # Caught by testing the five URL forms rather than the happy path. `http[s]*` is
  # portable across both seds.
  printf '%s' "$1" \
    | sed -e 's#^git@\([^:]*\):#https://\1/#' -e 's#^ssh://git@#https://#' \
    | sed -e 's#^http[s]*://[^/]*/##' -e 's#\.git$##' -e 's#/$##' \
    | tr '[:upper:]' '[:lower:]'
}

_push_allowed() {
  # Echoes a one-line reason and returns 1 when the push must not happen.
  local url dest baseline writer_declared this_core
  url=$(git remote get-url origin 2>/dev/null || true)

  if [[ -z "$url" ]]; then
    # Unconfigured is a NORMAL state for a fresh Core, not a failure. A noisy error at
    # every single close is how people learn to ignore close output.
    echo "no origin configured — skipping push (set one with: git remote add origin <your-repo>)"
    return 1
  fi

  dest=$(_normalize_remote "$url")
  baseline=$(python3 -c "
import json,sys
try:
    m=json.load(open('$REPO/bin/sync-manifest.json'))
    print((m.get('baseline_repo') or '').lower())
except Exception:
    print('nicknur7/core-agent')
" 2>/dev/null || echo "nicknur7/core-agent")
  [[ -z "$baseline" ]] && baseline="nicknur7/core-agent"

  [[ "$dest" != "$baseline" ]] && return 0   # pushing to its own repo — always fine

  # Destination IS the shared baseline. Enrollment required.
  writer_declared=$(python3 -c "
import json
try:
    print((json.load(open('$REPO/bin/sync-manifest.json')).get('baseline_writer') or ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
  this_core=$(basename "$REPO")

  if [[ -n "$writer_declared" && "$writer_declared" == "$this_core" \
        && -f "$REPO/.claude/state/.baseline-writer-enrolled" ]]; then
    return 0
  fi

  echo "REFUSING to push session state to the shared baseline ($baseline).
  This Core's origin points at the template it was cloned from, which is how a
  spawned Core's private state reaches a shared repo (see 2026-06-19, ea2e780).
  Fix: git remote set-url origin <your-own-repo>
  Shared code belongs on the baseline via bin/sync-to-baseline.sh, never via this push."
  return 1
}

_guarded_push() {
  local reason
  if reason=$(_push_allowed); then
    git push 2>&1 | head -20 >&2 || true
  else
    printf '[session-lifecycle] %s\n' "$reason" >&2
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# CLOSE — the single write path. MODE: full (/close-core) | defensive (walk-away)
# ─────────────────────────────────────────────────────────────────────────────
lifecycle_close() {
  local MODE="${1:-defensive}"
  # SESSION-SCOPED MARKERS (2026-07-25). $2 = this hook's OWN session_id, parsed from its stdin
  # payload by the calling shim (stop-hook.sh / defensive-save.sh).
  #
  # Why the id must be passed in rather than read from a shared file: on `/close-core` then
  # `/clear`, the NEW session's SessionStart fires BEFORE the OUTGOING session's SessionEnd. A
  # globally-named marker (.full-close-this-session) is therefore clobbered by the incoming
  # session while the outgoing close still needs it — so the trailing-no-op branch below could
  # not fire, and every clean close was followed by a redundant defensive save that re-ran every
  # generator and re-stamped current-state as "no explicit close". Keying the marker to the
  # session that OWNS it makes that race structurally impossible. Same convention already used by
  # .brain-queried-<sid> / .agent-spawns-<sid>.json.
  #
  # Empty SID (a caller that could not parse stdin) falls back to the legacy global name so this
  # degrades to the old behaviour instead of silently never marking.
  local SID="${2:-}"
  local FULL_CLOSE_MARKER SYNC_MARKER
  if [[ -n "$SID" ]]; then
    FULL_CLOSE_MARKER="$CORE_INSTANCE/.claude/state/.full-close-$SID"
    SYNC_MARKER="$CORE_INSTANCE/.claude/state/.brain-synced-$SID"
  else
    FULL_CLOSE_MARKER="$CORE_INSTANCE/.claude/state/.full-close-this-session"
    SYNC_MARKER="$CORE_INSTANCE/.claude/state/.brain-synced-this-session"
  fi
  local NOW NOW_FULL LOG STATE_FILE SESSION_LOG
  NOW=$(date +"%H:%M %Z")
  NOW_FULL=$(date +"%Y-%m-%d %H:%M %Z")
  LOG="/tmp/session-lifecycle-$(basename "$REPO")-$TODAY.log"  # SL3 fix 2026-06-23: namespace per-instance (was shared across all Cores -> interleaved logs)
  STATE_FILE="$CORE_MEM_CURRENT_STATE"
  SESSION_LOG="$REPO/sessions/${TODAY}.md"
  cd "$REPO" || return 0
  echo "[$(date)] === close[$MODE] sid=${SID:-<none>} === " >> "$LOG"

  # Durable "reconcile is enforced on this Core" signal (committed in identity.json — a runtime
  # baseline can fail-open-vanish; this cannot). Used by BOTH the full-close gate and the
  # walk-away carry so a broken/missing machinery never silently drops the obligation.
  local RECONCILE_ENFORCE
  RECONCILE_ENFORCE=$(python3 -c "import json;print('1' if json.load(open('$CORE_IDENTITY_JSON')).get('reconcile_enforce') else '')" 2>/dev/null)
  if [[ $? -ne 0 ]] && [[ -f "$REPO/bin/reconcile-receipt.py" ]]; then
    # identity.json unreadable/corrupt but the machinery IS installed → fail CLOSED (enforce).
    # Never silently drop enforcement on a config-read failure (silent-turn-off class).
    RECONCILE_ENFORCE="1"
  fi

  # SL fix (2026-07-17): on a FULL (/close-core) close, set a durable per-session marker
  # that an EXPLICIT close ran. Set EARLY (before the generators below) so a stop-hook
  # timeout can't skip it. Cleared at real SessionStart (session-start-check.sh, NOT
  # lifecycle_start which is not wired to SessionStart). Gitignored (never committed).
  # The defensive branch reads it so a later walk-away autosave can't downgrade the
  # "(explicit close)" stamp to "no explicit close" — the observability bug that erased
  # every real close's proof (Nick runs /close-core, keeps the session open, session-end
  # defensive-save clobbered it, so memory always looked unclosed).
  if [[ "$MODE" == "full" ]]; then
    # RECONCILE GATE (2026-07-17, Codex-designed): a FULL close only counts as an explicit
    # reconciled close if reconciliation was dispositioned this session (or there were no
    # in-scope changes). The controller — NOT a Stop hook — owns this enforcement. If not
    # reconciled, DOWNGRADE to a defensive save: save the work (no data loss), carry the exact
    # unreconciled delta (.reconcile-pending), and do NOT mint the explicit-close marker.
    #
    # FAIL CLOSED WHEN ENFORCED (Codex rounds 4-5): the DURABLE, committed signal for "Phase 1
    # required on this Core" is identity.json `reconcile_enforce` — NOT the runtime baseline
    # (which can fail-open-vanish and would conflate "not installed" with "capture failed").
    # When enforced, a full close that is not VERIFIABLY reconciled — including missing/broken
    # machinery OR a failed baseline — DOWNGRADES to defensive and carries the obligation
    # forward. Never a silent clean close (that silent-turn-off class — fb8c8fc — is the exact
    # 06-23 breakage). Legacy explicit-close applies only where reconcile_enforce is unset.
    if [[ -n "$RECONCILE_ENFORCE" ]]; then
      if [[ -f "$REPO/bin/reconcile-receipt.py" ]] \
         && CORE_INSTANCE="$REPO" python3 "$REPO/bin/reconcile-receipt.py" check >> "$LOG" 2>&1; then
        touch "$FULL_CLOSE_MARKER" 2>/dev/null || true
      else
        # Not reconciled, OR the machinery is missing/broken → carry the obligation forward.
        if [[ -f "$REPO/bin/reconcile-receipt.py" ]]; then
          CORE_INSTANCE="$REPO" python3 "$REPO/bin/reconcile-receipt.py" pending >> "$LOG" 2>&1 || true
        elif [[ ! -f "$CORE_INSTANCE/.claude/state/.reconcile-pending.json" ]]; then
          # enforce=true but machinery gone = BROKEN INSTALL: write a fallback pending so the
          # reconciliation obligation is NOT lost (SessionStart warns loudly on the broken install).
          printf '{"changeset":{"total":1,"added":[],"modified":["<reconcile-machinery-missing>"],"deleted":[]},"carried":true,"broken_install":true,"schema":2}' > "$CORE_INSTANCE/.claude/state/.reconcile-pending.json" 2>/dev/null || true
        fi
        rm -f "$FULL_CLOSE_MARKER" 2>/dev/null || true
        echo "[$(date)] full close DOWNGRADED → defensive: reconcile not verified (unreconciled or gate unavailable)" >> "$LOG"
        MODE="defensive"
      fi
    else
      # reconcile_enforce unset → Phase 1 not enabled on this Core → legacy explicit-close.
      touch "$FULL_CLOSE_MARKER" 2>/dev/null || true
    fi
  fi

  # ── Trailing exit/clear after an explicit /close-core THIS session (2026-07-24 redesign) ──
  # Nick's workflow is `/close-core` THEN clear/exit. The full close already ran every generator
  # + brain-update; this trailing SessionEnd defensive pass is pure redundancy. Left un-gated it
  # re-runs the generators (whose churn became the spurious "no explicit close" commit) AND re-fires
  # brain-update (which collided with a peer Core's lock and left the stale .brain-update-deferred
  # markers — the exact thing Nick saw at session start). Short-circuit it: preserve any stray work
  # committed between /close-core and exit, release the session lock, and STOP. Do NOT re-close.
  # (Marker is cleared at SessionStart, so a genuinely NEW session's defensive save is unaffected.
  #  On a full→defensive downgrade the marker was removed above, so this correctly does not fire.)
  if [[ "$MODE" == "defensive" && -f "$FULL_CLOSE_MARKER" ]]; then
    echo "[$(date)] close[defensive]: explicit /close-core already ran this session — trailing no-op (preserve-only, no re-close, no brain-update re-fire)" >> "$LOG"
    if ! { git diff --quiet HEAD 2>/dev/null && [[ -z "$(git status --porcelain 2>/dev/null)" ]]; }; then
      git add -A 2>/dev/null || true
      _safety_scan
      if [[ "$BLOCKED" -eq 1 ]]; then
        local reason
        for reason in "${BLOCK_REASONS[@]}"; do echo "[$(date)]   !! $reason" >&2; echo "[$(date)]   !! $reason" >> "$LOG"; done
        _write_blocked_marker "session-lifecycle[trailing-autosave]"
      else
        # pipefail is set (no `|| true` here) so PIPESTATUS[0] is git commit's real exit —
        # only clear the blocked marker on a genuine success (Codex Medium: the old `|| true`
        # + unconditional clear reported success even when commit/push failed).
        git commit -m "Session ${TODAY} ${NOW}: trailing autosave after explicit /close-core" 2>&1 | head -20 >&2
        local TRAIL_COMMIT_EXIT=${PIPESTATUS[0]}
        _guarded_push
        if [[ "$TRAIL_COMMIT_EXIT" -eq 0 ]]; then
          rm -f "$CORE_LAST_SAVE_BLOCKED" 2>/dev/null || true
        else
          echo "[$(date)] close[defensive] trailing autosave: commit FAILED (exit=$TRAIL_COMMIT_EXIT) — blocked marker kept" >> "$LOG"
        fi
      fi
    fi
    # Deterministic ledger capture still runs (Codex High #4): the /close-core turns themselves
    # grew the session JSONL after the full close captured it — pick up that tail so nothing is
    # lost. Fast, no LLM, no brain lock; same block as the main close path below.
    if [[ "${CORE_LEDGER_CAPTURE:-1}" != "0" && -f "$REPO/scheduling/brain-pg/discover.py" ]]; then
      (
        export CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}"
        python3 "$REPO/scheduling/brain-pg/discover.py" >>"$LOG" 2>&1
        python3 "$REPO/scheduling/brain-pg/capture_worker.py" >>"$LOG" 2>&1
      ) &
    fi
    rm -f "$REPO/.claude/state/.session-lock" 2>/dev/null || true
    echo "[$(date)] === close[defensive] trailing no-op complete (capture spawned) ===" >> "$LOG"
    return 0
  fi

  # Prune current-state on every close (was defensive-only before; CLOSE owns it).
  if [[ -f "$REPO/bin/prune-current-state.py" ]]; then
    python3 "$REPO/bin/prune-current-state.py" "$STATE_FILE" >> "$LOG" 2>&1 || true
  fi

  # Discovery engine (2026-07-12): the single source of truth for "what Core has".
  # MUST run before gen-capabilities (which renders FROM the manifests) and before
  # the commit (so the manifests ride the same close commit → the snapshot publisher).
  # Emits memory/{core,host,fleet}-manifest.json. Deterministic + fail-open.
  if [[ -f "$REPO/bin/gen-core-manifest.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/bin/gen-core-manifest.py" >> "$LOG" 2>&1 || true
  fi

  # Self-knowledge maintenance (2026-06-09): regenerate capabilities.md ("what
  # this Core does") from LIVE config on EVERY close — walk-away AND full — so it
  # can't rot the way it did into an untouched template in business/school.
  # Deterministic + fail-open: a failure here NEVER blocks the commit below.
  # (Brain extraction + project-status reconciliation are judgment/LLM work that
  # can't run from a bash hook — those stay in the in-session /close-core path,
  # and check-self-knowledge.py at SessionStart surfaces any residual drift.)
  # As of 2026-07-12 it renders from memory/*-manifest.json (falls back to its own
  # live scan if the manifests are absent).
  if [[ -f "$REPO/bin/gen-capabilities.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/bin/gen-capabilities.py" >> "$LOG" 2>&1 || true
  fi

  # L1 fix (2026-06-23): close the learned self-tune loop. The corpus miner --detect
  # is a lightweight JSONL scan (no embed, no network); it writes .learned-resynth-due
  # when the correction corpus has grown past threshold, which learned-resynth-trigger.sh
  # consumes at the NEXT SessionStart. It was never auto-called → contracts froze 13 days.
  # Fail-open: a miner error never blocks the close.
  #
  # UNCONDITIONAL since 2026-08-05. This call used to be gated on the ABSENCE of
  # .si-unified-spine, on the reasoning that the friction engine becomes "the sole
  # miner/generator" after cutover. That reasoning was wrong in one specific way, and it cost
  # org 1 its entire correction corpus from 2026-07-23 to 2026-08-05:
  #
  #   - The RESYNTH half is genuinely retired by the cutover (it dropped a zombie
  #     "learned contracts may be stale" marker forever). That half is now suppressed inside
  #     learned-corpus-miner.py itself, where the condition belongs.
  #   - The MINING half is NOT retired. learned-corpus-miner.py --detect is the ONLY writer to
  #     pattern_observations, and friction_loop.py is a pure READER of that same table. Gating
  #     the writer on spine-adoption therefore deletes the writer and leaves a live consumer
  #     running on a frozen table. bin/correction-rate.py reads it too, so Criterion #1 — the
  #     metric the whole apparatus exists to move — silently stops updating on exactly the Cores
  #     that adopt the new spine, and nothing anywhere announces it.
  #
  # All four peer Cores independently refused to adopt the spine until this was separated
  # (core-bus #267, #269, #271, #276). Do not re-gate this on the marker.
  if [[ -f "$REPO/scheduling/claude-si/learned-corpus-miner.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/scheduling/claude-si/learned-corpus-miner.py" --detect >> "$LOG" 2>&1 || true
  fi

  # THE CONSOLIDATION PASS (Phase C, 2026-08-05) — the half of learning that is not about failure.
  # The miner above records what went WRONG. This records what WORKED: it segments the session at
  # each correction and keeps the stretches that ended in acceptance, so a recurring sequence can
  # become a Workflow the brain can actually express (Phase B added the representation).
  #
  # Only --prepare runs here, and that is on purpose. Preparing is mechanical — read transcripts,
  # segment, write a brief — and safe in a hook. The EXTRACTION is a judgement pass that needs a
  # model, and the writing must be verified before it touches the brain, so both happen in-session
  # via --apply. Same split learned-resynth.py uses; a hook that called a model and wrote whatever
  # came back is the failure mode that put 26 unverified checkpoints into a merge on 2026-06-11.
  # Fail-open, and CORE_CONSOLIDATE_OFF=1 disables it entirely (plan §3 C rollback).
  if [[ -f "$REPO/scheduling/brain-pg/consolidate_sessions.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/scheduling/brain-pg/consolidate_sessions.py" \
        --prepare --limit 5 >> "$LOG" 2>&1 || true
    if [[ -s "$REPO/.claude/state/consolidate-pending.json" ]]; then
      _n_wins=$(python3 -c "import json,sys; d=json.load(open('$REPO/.claude/state/consolidate-pending.json')); print(sum(len(s.get('windows',[])) for s in d.get('sessions',[])))" 2>/dev/null || echo 0)
      if [[ "${_n_wins:-0}" -gt 0 ]]; then
        printf '%s work window(s) ended in acceptance and are ready to consolidate into Workflows.\nRun: python3 scheduling/brain-pg/consolidate_sessions.py --prepare  (brief + material)\nthen the extraction pass, then --apply <result.json>\n' "$_n_wins" \
          > "$REPO/.claude/state/.consolidate-due"
      fi
    fi
  fi

  # ── COMPILE-TRUTH REFRESH, prepare half (2026-08-28) ────────────────────────────────────
  # Nick's placement rule, settled this session: "the goal is to have things that need an llm to
  # run on close and that would be good for the next session which could be like in the next
  # minute." A hub summary is written ONCE at extraction and never re-synthesised, so a stale hub
  # is exactly what damages the NEXT session's recall — a person hub whose summary was months
  # behind the relationship it described was how this surfaced. Measured 2026-08-28:
  # 534 of 68,296 non-Source entities ever compiled (0.78%), and EXACTLY ZERO on business,
  # school, finance and ops, because /refresh-truth is a manual life-side command nobody ran on
  # a peer.
  #
  # SAME SPLIT AS CONSOLIDATION, and for the same recorded reason: detect+partition are
  # MECHANICAL and safe in a hook; the synthesis is a JUDGEMENT pass that needs a model, and a
  # hook that called a model and wrote back whatever came out is the failure that put 26
  # unverified checkpoints into a merge on 2026-06-11. So the hook prepares and marks; the model
  # running /close-core does the synthesis in-session on the session's own subscription auth.
  # That is Nick's 2026-07-24 reversal of the headless path, honoured rather than re-litigated.
  #
  # THIS CORE'S ORG ONLY. A close knows its own seat; the nightly keeps the all-org sweep as the
  # catch-up for seats that never close. Fail-open.
  if [[ -f "$REPO/scheduling/brain-pg/compile-truth-refresh.py" ]]; then
    CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" PGOPTIONS='-c statement_timeout=120000' \
      python3 "$REPO/scheduling/brain-pg/compile-truth-refresh.py" --detect >> "$LOG" 2>&1 || true
    _CT_DRIFT=$(CORE_INSTANCE="$REPO" python3 - <<'PYCT' 2>/dev/null || echo 0
import json, os, glob
d = sorted(glob.glob(os.path.join(os.environ["CORE_INSTANCE"],
    "scheduling/brain-pg/compile-truth-work", "drift-report-org*.json")))
print(len(json.loads(open(d[-1]).read()).get("drifted", [])) if d else 0)
PYCT
)
    if [[ "${_CT_DRIFT:-0}" -gt 0 ]]; then
      CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" \
        python3 "$REPO/scheduling/brain-pg/compile-truth-refresh.py" --partition --batches 6 \
        >> "$LOG" 2>&1 || true
      printf '%s hub(s) drifted — summaries frozen since creation damage the NEXT session'"'"'s recall.\nBatches are prepared in scheduling/brain-pg/compile-truth-work/.\nSynthesise each refresh-batch-NN.json, write refresh-batch-NN-out.json, then:\n  python3 scheduling/brain-pg/compile-truth-refresh.py --ingest\n' \
        "$_CT_DRIFT" > "$REPO/.claude/state/.compile-truth-due"
      echo "[$(date)] close: compile-truth drift=$_CT_DRIFT, batches prepared" >> "$LOG"
    else
      rm -f "$REPO/.claude/state/.compile-truth-due" 2>/dev/null || true
    fi
  fi

  # Per-Core SELF-IMPROVEMENT loop (unified redesign step 4, Nick's B): after mining this Core's own
  # corpus, (1) seed the universal base contracts if this Core has none (fresh Core / fork bootstrap),
  # (2) autonomously INDUCE inject-only contracts for any recurring correction cluster this Core has
  # that isn't covered yet (blocking clauses are NEVER auto-created — they go to an approval list),
  # (3) regenerate this Core's classifier snapshot from its OWN DB (with data-driven triggers) so the
  # contracts actually fire. Every Core grows its OWN SI from its OWN corrections. Fail-open, org-scoped.
  # Kill-switch: CORE_SI_INDUCE=0.
  # WS1 cutover: once the unified spine is live, the friction engine is the SOLE generator — the old
  # inducer (si_seed_base + si_induct --induce + si_snapshot) retires. Skipped when the marker exists.
  if [[ "${CORE_SI_INDUCE:-1}" != "0" ]] && [[ ! -f "$REPO/.claude/state/.si-unified-spine" ]] \
       && [[ -f "$REPO/scheduling/claude-si/si_induct.py" ]]; then
    _SI_ENV=(CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}")
    env "${_SI_ENV[@]}" python3 "$REPO/scheduling/claude-si/si_seed_base.py" >> "$LOG" 2>&1 || true
    env "${_SI_ENV[@]}" python3 "$REPO/scheduling/claude-si/si_induct.py" --induce >> "$LOG" 2>&1 || true
    env "${_SI_ENV[@]}" python3 "$REPO/scheduling/claude-si/si_snapshot.py" >> "$LOG" 2>&1 || true
  fi

  # Legacy-retirement readiness (unified redesign step 5): tick the proof counter. When the NEW
  # memory-brain system (ledger + brain_status) sustains N clean close cycles, the retirement sweep
  # auto-surfaces at SessionStart (and auto-archives if enabled in .retire-legacy.json) — so the
  # sweep is never forgotten and only fires once the new system is proven. Fail-open.
  if [[ -f "$REPO/bin/retire-legacy.py" ]]; then
    CORE_INSTANCE="$REPO" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" CORE_ORG_ID="${CORE_ORG_ID:-1}" python3 "$REPO/bin/retire-legacy.py" --tick >> "$LOG" 2>&1 || true
  fi

  # core-si close pass (2026-06-27): the autonomy + visibility step. Detects live SI items,
  # auto-applies ONLY trusted + AUTO_SAFE + has-applier fixes (triple-gated, local/reversible
  # only), writes .claude/state/core-si-inbox.json (the visibility surface + app bridge), and
  # fires local notifications for critical-needs-you + first-auto-fire. Fail-open: it can never
  # break the close (its own top-level handler exits 0 on any error). This is "SI happens at
  # close, not a separate bot" — the close protocol IS the bot.
  if [[ -f "$REPO/bin/core-si-close.py" ]]; then
    CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" python3 "$REPO/bin/core-si-close.py" >> "$LOG" 2>&1 || true
  fi

  # Telemetry refresh (2026-06-29): rebuild hook-events.log from THIS session's JSONL
  # (merge-safe --merge → unions + dedupes, NEVER shrinks even if old transcripts rotate)
  # BEFORE the disposition stamp below. Was the real defect: refresh stamped dispositions
  # from a log frozen at the last manual backfill (~06-22), so ~19 hooks showed stale
  # last_fired. Now telemetry is current every close. Deterministic, fail-open.
  if [[ -f "$REPO/scheduling/system-health/backfill-hook-events.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/scheduling/system-health/backfill-hook-events.py" --merge >> "$LOG" 2>&1 || true
  fi

  # Matrix seed (Phase 3.5, 2026-07-10): ensure this Core HAS a hook-disposition matrix
  # before the refresh stamp below. Composes bin/hook-curation-base.json (universal L1) with
  # this Core's registered hooks (L2) → .claude/state/hook-dispositions.json (L3). Additive +
  # non-destructive (never overwrites local curation; only adds newly-registered hooks + flags
  # deregistered ones). Makes per-Core system views work on every Core — peers had NO matrix,
  # so their system tab rendered empty. A freshly-spawned Core gets its matrix on first close.
  # Deterministic, fail-open.
  if [[ -f "$REPO/bin/seed-hook-dispositions.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/bin/seed-hook-dispositions.py" >> "$LOG" 2>&1 || true
  fi

  # Hook-dashboard freshness (2026-06-27): stamp live fire-counts/last-fired onto
  # hook-dispositions.json from hook-events.log so the hook-health dashboard can't show
  # stale hand-entered numbers (the "DORMANT, loop broken" line that was fixed 06-23).
  # Deterministic, fail-open.
  if [[ -f "$REPO/bin/refresh-hook-dispositions.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/bin/refresh-hook-dispositions.py" >> "$LOG" 2>&1 || true
  fi

  # ESTATE SWEEP (2026-07-27). Runs AFTER the disposition refresh so it reads current evidence.
  # Adopts hooks that are live but unmanaged; only PROPOSES tuning and retirement, because a silent
  # hook may be broken or may be guarding something that stopped happening, and fire counts cannot
  # tell those apart. First run on any Core reports and applies nothing. Fail-open.
  # BRAIN DURABILITY, before anything else in the close touches the database.
  #
  # corebrain is the only durable record of every Core's learned layer — si_artifacts,
  # friction_cases, assertions, entities. There is no git copy. On 2026-07-27, asked
  # whether it was backed up, the answer was: snapshot.sh existed but nothing scheduled
  # it, and the newest dump on disk was TWENTY DAYS OLD. The whole session was spent
  # auditing the repo while the actual single point of failure sat unprotected.
  #
  # Cheap: no-ops entirely when a dump under 24h exists. Fail-open — a close must never
  # be blocked by a backup, but a missing backup should never be silent either.
  # DETACHED — 2026-08-12. "Cheap: no-ops when a dump under 24h exists" was true only
  # while a dump under 24h EXISTED. Once the newest aged out (2026-08-09 15:55), every
  # close ran a ~6-minute pg_dump inline, inside a Stop hook budgeted at 60s in
  # settings.json. The kill landed mid-dump, so:
  #   - the dump never completed, so the age check stayed stale, so it re-ran next close
  #   - every run orphaned a ~200MB .partial (671 of them, 128 GB, deleted 2026-08-12)
  #   - and EVERY STAGE BELOW THIS LINE STOPPED RUNNING. grade-gate and grade-intent
  #     froze at 2026-08-09T22:56:33Z — one minute after the backup window expired —
  #     and stayed frozen across 187 life closes and 223 business closes.
  #
  # A backup is not on the critical path of a close and must never again be able to
  # truncate one. Detached, so the hook's timeout cannot reach it; the script's own
  # mkdir lock prevents overlap, and its startup sweep reclaims anything a SIGKILL
  # still manages to orphan. Fail-open as before — a close is never blocked by a backup.
  if [[ -f "$REPO/bin/brain-backup.sh" ]]; then
    nohup bash "$REPO/bin/brain-backup.sh" >> "$LOG" 2>&1 &
    disown 2>/dev/null || true
    echo "[session-lifecycle] brain-backup detached (pid $!) — close continues" >> "$LOG"
  fi

  # MEASUREMENT, before the sweep that consumes it.
  #
  # bin/grade-gate.py could measure a gate's true fire rate against thousands of real
  # turns from the day it was written, and NOTHING EVER RAN IT — grep the repo and it
  # appears only inside the docstrings of the hooks it grades. Every tune was a human
  # running it by hand and noticing. That is the difference between a loop that measures
  # itself and one that can be measured: recall-gate sat at 4.8%, one interruption in
  # twenty, and looked identical in the sweep to a healthy hook, because the sweep
  # classifies on FIRE COUNTS and a fire count never knows how many chances it had.
  #
  # Now it runs every close and writes .claude/state/gate-grades.json. stdin is closed
  # because grading imports each hook module, and a hook that reads stdin at import will
  # otherwise block the close forever (hit while building this).
  # T046 PILE-UP LOCK (2026-08-17). Detaching without this was a real regression and
  # sentinel-code caught it: the inline versions could not overlap because they blocked, so two
  # closes firing near each other would now start a second grade-gate / grade-intent / run-all /
  # estate-sweep on top of the first, all writing the SAME git-tracked JSON —
  #
  # (Wording note: this comment said "spawn" until test_pipeline_exhaust_filter.py failed on it.
  # That test pairs a Spawn-anchor against a brain-producer token, and `extract-pending` already
  # appears in this file, so one word of my prose made a shell-command runner read as a subagent
  # dispatcher that mandates no opening phrase. The detector was right about what it saw; the
  # file is not a dispatcher. Reworded rather than adding an exemption — weakening a working
  # check to accommodate a comment is the wrong direction.)
  # gate-grades.json, intent-grades.json, hook-dispositions.json, .estate-sweep-ran.
  #
  # Both patterns this detach was copied from already carry a lock: brain-backup.sh:117-126 uses
  # mkdir, and eval.py has its own lockfile a few lines below in this same file. I took the
  # detach and left the discipline behind. mkdir is the atomic primitive that exists everywhere;
  # macOS has no flock. 120-minute stale reclaim matches brain-backup's.
  _detach_guarded() {                      # _detach_guarded <lockname> <cmd...>
    local _ln="$1"; shift
    local _lk="$REPO/.claude/state/.${_ln}.lock"
    if ! mkdir "$_lk" 2>/dev/null; then
      if [[ -n "$(find "$_lk" -maxdepth 0 -mmin +120 2>/dev/null)" ]]; then
        rmdir "$_lk" 2>/dev/null && mkdir "$_lk" 2>/dev/null || {
          echo "[session-lifecycle] ${_ln}: lock held, skipping" >> "$LOG"; return 0; }
      else
        echo "[session-lifecycle] ${_ln}: already running, skipping" >> "$LOG"; return 0
      fi
    fi
    nohup bash -c '_l="$1"; shift; "$@" ; rmdir "$_l" 2>/dev/null' _ "$_lk" "$@" \
      >> "$LOG" 2>&1 < /dev/null &
    disown 2>/dev/null || true
  }

  # T046 (2026-08-17): DETACHED. This is where a 60s-budgeted close was measured dying.
  if [[ -f "$REPO/bin/grade-gate.py" ]]; then
    _detach_guarded grade-gate env CORE_INSTANCE="$REPO" python3 "$REPO/bin/grade-gate.py" --all
  fi

  # INTENT check — the leg a rate cannot supply. grade-gate answers "is this noisy";
  # this answers "is it still doing the thing it was built for", by replaying each gate
  # against the positive and negative examples recorded in its own intent record.
  # Deterministic, no classifier — see tasks/research/kind-check-research-2026-07-27.md
  # for why the similarity-scoring version was measured and deleted.
  # T046 (2026-08-17): DETACHED. Measured at ~51s inside a 60s budget.
  if [[ -f "$REPO/bin/grade-intent.py" ]]; then
    _detach_guarded grade-intent env CORE_INSTANCE="$REPO" python3 "$REPO/bin/grade-intent.py" --write
  fi

  # LESSON EVICTION (wired 2026-07-30, master plan Phase 0.5). This tool has existed since
  # 2026-06-07 and was never called from anything, which is the same disease as the intent check
  # above: built, correct, connected to nothing. Worse, its header regex matched only `##` while
  # lessons.md writes active entries as `###`, so on the rare hand-run it reported "0 active
  # entries" against a 6,800-token file and looked healthy. Both fixed.
  #
  # --apply rather than proposal-only, per Nick 2026-07-30 ("no human gates, full unlock"). Safe
  # to run unattended for reasons that are structural, not optimistic: it MOVES entries to
  # lessons-archive.md and never deletes, and the DGM stepping-stone guard refuses to touch any
  # lesson whose body names a live hook. On the current file that protects 10 of 21 entries.
  # Fail-open — a bad eviction pass must never break a close.
  if [[ -f "$REPO/scheduling/core-si/lessons-evict.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/scheduling/core-si/lessons-evict.py" --apply \
      >> "$LOG" 2>&1 < /dev/null || true
  fi

  # STEERING COMPRESSION — the other three-quarters of the always-loaded set (2026-08-26).
  #
  # lessons-evict above covers tasks/lessons.md: ~2,500 tok of the ~11,100 this Core loads on every
  # prompt. steering-retire.py covers hooks and steering components. NOTHING covered the rules files
  # and CLAUDE.base.md — ~8,600 tok that could only grow — which is why the budget sat over its own
  # ratchet with a working ratchet AND a working evictor and no contradiction between them.
  #
  # Nick asked for a recurring mechanism rather than a one-time cut, and specifically for compression
  # over deletion so nothing of value is lost. This MOVES dated incident history to
  # docs/steering-detail/ and leaves the rule in place, so the reasoning survives at zero per-prompt
  # cost. It refuses to move anything imperative, second-person, or attributed to Nick — the first
  # proposal run selected one of his standing directives and the guard exists because of that.
  #
  # It stops the moment the seat is back under budget, caps moves per pass, and logs every move for
  # one-paste reversal. Fail-open, same as the evictor: a compression pass must never break a close.
  # BRAIN EXPORT — the layer the vault cannot rebuild (2026-08-27).
  #
  # core-brain holds the vault: 11,065 markdown files, on GitHub, safe. The Postgres database is a
  # separate thing in a separate place (/opt/homebrew/var/postgresql@17, not even in the home
  # folder) and is in no repo. Most of it does not need to be — entities, evidence and edges are
  # DERIVED from the vault and rebuildable. But the corrections corpus, the learned artifacts and
  # the steering telemetry never came from the vault; they came from watching Nick work, and there
  # is nothing to rebuild them from.
  #
  # Runs every close on purpose. A hand-run export is a backup that is six weeks stale at the moment
  # it is needed — the same failure as contract-fitness, which had a real producer and froze for
  # nine days because nothing measured its age.
  #
  # Fail-open: an export problem must never break a close. It skips quietly when Postgres is
  # unreachable and says so.
  if [[ -f "$REPO/bin/brain-export-si-layer.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/bin/brain-export-si-layer.py" \
      >> "$LOG" 2>&1 < /dev/null || true
  fi

  if [[ -f "$REPO/bin/steering-compress.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/bin/steering-compress.py" --apply \
      >> "$LOG" 2>&1 < /dev/null || true
  fi

  # SELF-TESTS. Six suites existed and nothing invoked them — a test nobody runs is a comment
  # with a shebang. Several guard bugs that are LATENT and arm silently later (channel
  # isolation arms on the first assistant_regex artifact the generator emits; the detect()
  # contract arms whenever a hook is edited), so they only do their job if something executes
  # them without anyone remembering to.
  #
  # Failures are surfaced to stderr, not swallowed: a quiet failing test is worse than no test,
  # because it reads as coverage. Never blocks the close — the session still ends cleanly.
  # T046 (2026-08-17): DETACHED. Measured at 166s — the single largest item in a 60s budget, so
  # under the real Stop hook this NEVER RAN. It was not providing coverage; it was consuming the
  # budget the commit needed and then being killed. Detaching makes it run MORE, not less.
  #
  # THE FAILURE SURFACING IS PRESERVED, which is the only thing that could have been lost here.
  # It moves from the close's stderr to the log, and the log is what the failure line already
  # pointed at ("see $LOG"). A subshell keeps the conditional intact rather than dropping it.
  if [[ -f "$REPO/bin/tests/run-all.sh" ]]; then
    _detach_guarded self-tests bash -c '
      if ! bash "$1/bin/tests/run-all.sh" --quiet >> "$2" 2>&1 < /dev/null; then
        echo "[session-lifecycle] SELF-TESTS FAILED — detached run, see above" >> "$2"
        tail -30 "$2" | grep -E "FAIL|self-tests:" >> "$2" || true
      fi
    ' _ "$REPO" "$LOG"
  fi

  # T046 (2026-08-17): DETACHED, same reason. It sits after run-all.sh and was never reached.
  if [[ -f "$REPO/bin/estate-sweep.py" ]]; then
    _detach_guarded estate-sweep env CORE_INSTANCE="$REPO" python3 "$REPO/bin/estate-sweep.py" --apply
  fi

  # Drift-gated recall-eval at close (Nick 2026-07-26: "this should be automatically done...
  # on close if it is needed"). Deterministic — eval.py replays the 30-query benchmark against
  # the live DB and writes tasks/research/brain-primitives-benchmark-YYYY-MM-DD.md, which the
  # core-si recall-eval detector reads. Gated to >7d so a normal close pays nothing; runs
  # BEFORE the commit below so the fresh report is committed. Fail-open, full closes only
  # (a walk-away must never block on a benchmark).
  # DETACHED, NOT INLINE — and this block was a self-perpetuating trap until 2026-08-08.
  #
  # core-business found it and I verified the whole chain on life's own disk (bus #714):
  #
  #   · eval.py replays a 30-query benchmark and does NOT finish inside a Stop-hook budget.
  #     business timed `session-lifecycle.sh close full` in the foreground: still running past 600s.
  #   · it writes the report only on COMPLETION, so the report never refreshes
  #   · so the >7d gate stays open, so the next full close runs it again, and also dies
  #   · and because stop-hook.sh:23 calls this controller SYNCHRONOUSLY, with the sentinel removal
  #     at its lines 28-29, the sentinel is never deleted — not retained by the blocked-close
  #     branch, simply never reached.
  #
  # So a Core more than 7 days behind on this benchmark could never complete another full close, and
  # nothing in the failure mode repaired it. business had been in that state since ~June 14; life's
  # newest report is 2026-07-26 and life was in it too. That is the stuck-sentinel-with-no-
  # .last-save-blocked pair I could not explain from my own disk.
  #
  # NOT REMOVED, because Nick asked for this explicitly (2026-07-26, quoted above): "this should be
  # automatically done ... on close if it is needed". Deleting the step would satisfy the bug report
  # and reverse his instruction — the exact mistake made earlier today with the Sentinel exclusions.
  # Detaching keeps the instruction and drops the hang: it still runs automatically when stale, the
  # close no longer waits, and the report lands for the NEXT close to commit.
  #
  # TRUE detachment (nohup + a subshell that exits), not a bare `&` — a bare `&` dies with the hook's
  # shell, which is the archived 2026-07-23 lesson on exactly this.
  #
  # PILE-UP GUARD: a lockfile, because without one a permanently-failing eval spawns another copy on
  # every close forever. The lock is cleared by the runner itself on exit, and treated as stale after
  # 6h so a killed run cannot block the benchmark permanently.
  if [[ "$MODE" == "full" ]] && [[ -f "$REPO/scheduling/brain-pg/eval.py" ]]; then
    local _EVAL_LATEST _EVAL_LOCK
    _EVAL_LATEST=$(ls -t "$REPO/tasks/research/"brain-primitives-benchmark*.md 2>/dev/null | head -1)
    _EVAL_LOCK="$REPO/.claude/state/.recall-eval-running"
    if [[ -f "$_EVAL_LOCK" ]] && [[ -n "$(find "$_EVAL_LOCK" -mmin +360 2>/dev/null)" ]]; then
      rm -f "$_EVAL_LOCK" 2>/dev/null || true    # stale lock from a killed run
    fi
    if [[ -z "$_EVAL_LATEST" ]] || [[ -n "$(find "$_EVAL_LATEST" -mtime +7 2>/dev/null)" ]]; then
      if [[ -f "$_EVAL_LOCK" ]]; then
        echo "[$(date)] close[full]: recall-eval stale but a run is already in flight — not spawning" >> "$LOG"
      else
        echo "[$(date)] close[full]: recall-eval >7d stale — spawning DETACHED (close does not wait)" >> "$LOG"
        date +%s > "$_EVAL_LOCK" 2>/dev/null || true
        ( cd "$REPO/scheduling/brain-pg" \
          && CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" \
             CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
             nohup sh -c 'python3 eval.py; rm -f "$1"' _ "$_EVAL_LOCK" >> "$LOG" 2>&1 & ) &
        disown 2>/dev/null || true
      fi
    fi
  fi

  # Archival doc-ref auto-repair at close (Nick 2026-07-26: "this should also be automatic").
  # When retire-legacy archives a file, live docs still cite the old path and the sys-docpath
  # detector nags forever. lint-doc-paths --fix-archival rewrites a broken ref ONLY when the
  # file demonstrably moved to an archive dir (exact-basename match) — deterministic,
  # git-reversible, never guesses. Fail-open.
  if [[ "$MODE" == "full" ]] && [[ -f "$CORE_BIN_LINT_DOC_PATHS" ]]; then
    CORE_INSTANCE="$REPO" python3 "$CORE_BIN_LINT_DOC_PATHS" --fix-archival >> "$LOG" 2>&1 || true
  fi

  # CLOSE LEDGER (Nick 2026-08-28: "make sure when a close happens it is tracked because i feel
  # like cores still dont know when the last actually slash command triggered close happens").
  # .last-session-start records the START of the session that closed — NOT when the close
  # happened — and it only ever wrote on MODE=full, so a defensive save left no trace at all.
  # Measured that day: school's marker read 2026-07-24 and ops's 2026-07-25 while both had
  # closed explicitly 9 and 7 times since. Two separate defects, one silent surface.
  #
  # This writes the actual close EVENT for BOTH modes: an append-only ledger plus a single-line
  # marker any hook can read in one cat. Fail-open — a bookkeeping write must never break a close.
  {
    _CLOSE_TS="$(date '+%Y-%m-%d %H:%M %Z')"
    _CLOSE_DIR="$CORE_INSTANCE/.claude/state"
    mkdir -p "$_CLOSE_DIR" 2>/dev/null || true
    printf '%s | mode=%s | session=%s\n' "$_CLOSE_TS" "$MODE" "${SID:-unknown}" \
      > "$_CLOSE_DIR/.last-close"
    printf '%s | mode=%s | session=%s\n' "$_CLOSE_TS" "$MODE" "${SID:-unknown}" \
      >> "$_CLOSE_DIR/close-ledger.log"
    if [[ "$MODE" == "full" ]]; then
      printf '%s | session=%s\n' "$_CLOSE_TS" "${SID:-unknown}" > "$_CLOSE_DIR/.last-full-close"
    fi
  } 2>/dev/null || true

  # full-close bookkeeping: persist session start + stamp session-log Started.
  if [[ "$MODE" == "full" ]]; then
    local START_PDT
    START_PDT=$(bash "$CORE_HOOK_GET_SESSION_START_TIME" 2>/dev/null || true)
    if [[ -n "$START_PDT" ]]; then
      echo "$START_PDT" > "$CORE_INSTANCE/.claude/state/.last-session-start"
      if [[ -f "$SESSION_LOG" ]] && grep -q '^Started:' "$SESSION_LOG"; then
        sed -i '' "s|^Started:.*|Started: $START_PDT|" "$SESSION_LOG"
      fi
    fi
    # access-log rotation (>500 lines → keep last 300).
    local ACCESS_LOG ACCESS_ARCHIVE ACCESS_LINES ARCHIVE_LINES
    ACCESS_LOG="$CORE_MEM_ACCESS_LOG"
    ACCESS_ARCHIVE="$REPO/memory/access-log-archive.md"
    if [[ -f "$ACCESS_LOG" ]]; then
      ACCESS_LINES=$(wc -l < "$ACCESS_LOG" 2>/dev/null | tr -d ' ')
      if [[ "$ACCESS_LINES" =~ ^[0-9]+$ ]] && (( ACCESS_LINES > 500 )); then
        ARCHIVE_LINES=$((ACCESS_LINES - 300))
        head -n "$ARCHIVE_LINES" "$ACCESS_LOG" >> "$ACCESS_ARCHIVE"
        tail -n 300 "$ACCESS_LOG" > "$ACCESS_LOG.tmp" && mv "$ACCESS_LOG.tmp" "$ACCESS_LOG"
      fi
    fi
  fi

  # Reconcile carry BEFORE the clean-tree early-return: an in-scope file that changed but
  # is git-ignored/untracked leaves the tree "clean" yet unreconciled (content-inventory
  # catches it where git can't — Codex edge). No-op when nothing in scope changed.
  if [[ "$MODE" == "defensive" ]]; then
    if [[ -f "$REPO/bin/reconcile-receipt.py" ]]; then
      CORE_INSTANCE="$REPO" python3 "$REPO/bin/reconcile-receipt.py" pending >> "$LOG" 2>&1 || true
    elif [[ -n "$RECONCILE_ENFORCE" ]] && [[ ! -f "$CORE_INSTANCE/.claude/state/.reconcile-pending.json" ]]; then
      # walk-away + enforce=true but machinery missing = broken install → don't lose the obligation.
      printf '{"changeset":{"total":1,"added":[],"modified":["<reconcile-machinery-missing>"],"deleted":[]},"carried":true,"broken_install":true,"schema":2}' > "$CORE_INSTANCE/.claude/state/.reconcile-pending.json" 2>/dev/null || true
    fi
  fi

  # C1 (2026-07-23, Codex #3 fix): the COMMIT is now an independent, OPTIONAL stage. A clean git tree
  # skips ONLY the commit — capture + brain-update (below) ALWAYS run. A walk-away's sole change is
  # often the external JSONL (no tracked file changed) → the tree is "clean" → the old early-return
  # here skipped capture + extraction entirely, silently leaving the brain stale. That was the exact
  # walk-away gap this redesign closes. Capture + brain-update now run unconditionally after this block.
  if git diff --quiet HEAD 2>/dev/null && [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
    echo "[$(date)] nothing to commit (capture + brain-update still run below)" >> "$LOG"
    # THE SAVE-BLOCK MARKER IS CLEARED IN THE COMMIT BRANCH (:944), WHICH A CLEAN TREE NEVER
    # REACHES. So a block that Nick resolved with a MANUAL commit left the marker on disk forever:
    # stop-hook.sh kept pinning the close sentinel and sys-saveblock (🔴) nagged at every close,
    # with no way for the condition to clear itself. That permanent-nag shape is the whole reason
    # this class kept landing in Nick's lap.
    #
    # STRICTER THAN THE COMMIT-BRANCH CLEAR, deliberately, and this is why sys-saveblock gets no
    # applier: a clean tree alone does NOT prove the blocked content is gone — it is equally the
    # signature of that content having been COMMITTED, and _guarded_push would then send it next
    # close. So clear only when the tree is clean AND nothing is waiting to be pushed. Default `1`
    # on the rev-list failure path keeps the marker when upstream cannot be resolved.
    if [[ -f "$CORE_LAST_SAVE_BLOCKED" ]] \
       && [[ "$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 1)" == "0" ]]; then
      mv "$CORE_LAST_SAVE_BLOCKED" "${CORE_LAST_SAVE_BLOCKED}.cleared" 2>/dev/null || true
      echo "[$(date)] save-block marker cleared: tree clean, nothing unpushed" >> "$LOG"
    fi
  else
  git add -A 2>/dev/null || true
  _safety_scan
  if [[ "$BLOCKED" -eq 1 ]]; then
    echo "[$(date)] BLOCKED — staged but not committed:" >> "$LOG"
    local reason
    for reason in "${BLOCK_REASONS[@]}"; do echo "[$(date)]   !! $reason" >&2; echo "[$(date)]   !! $reason" >> "$LOG"; done
    _write_blocked_marker "session-lifecycle[$MODE]"
    return 0
  fi

  # defensive-mode mechanical stamps (full-close stamps are done by /close-core
  # command before the sentinel drops, so skip them here to avoid double-stamp).
  if [[ "$MODE" == "defensive" ]]; then
    # Reconcile carry (2026-07-17): a walk-away has no model in the loop → it cannot
    # reconcile. Persist the exact unreconciled in-scope delta so the NEXT session's gate
    # catches it (no-op if nothing changed or a receipt already exists).
    if [[ -f "$REPO/bin/reconcile-receipt.py" ]]; then
      CORE_INSTANCE="$REPO" python3 "$REPO/bin/reconcile-receipt.py" pending >> "$LOG" 2>&1 || true
    fi
    if [[ -f "$STATE_FILE" ]] && grep -q '^Last updated:' "$STATE_FILE"; then
      # SL fix (2026-07-17): if an EXPLICIT /close-core ran THIS session (marker set in
      # the full path, cleared at SessionStart), a later walk-away autosave must NOT
      # downgrade the stamp to "no explicit close" — preserve the truth that a full
      # close (reconcile+extract) happened, just note the trailing autosave.
      if [[ -f "$FULL_CLOSE_MARKER" ]]; then
        sed -i '' "s|^Last updated:.*|Last updated: ${NOW_FULL} (explicit close · +autosave)|" "$STATE_FILE"
      else
        sed -i '' "s|^Last updated:.*|Last updated: ${NOW_FULL} (defensive-save — no explicit close)|" "$STATE_FILE"
      fi
    fi
    if [[ -f "$SESSION_LOG" ]] && ! grep -q "Session ended ${NOW_FULL}" "$SESSION_LOG"; then
      if [[ -f "$FULL_CLOSE_MARKER" ]]; then
        printf '\n---\n_Session ended %s via autosave after an explicit /close-core._\n' "$NOW_FULL" >> "$SESSION_LOG"
      else
        printf '\n---\n_Session ended %s via defensive-save (no explicit close)._\n' "$NOW_FULL" >> "$SESSION_LOG"
      fi
    fi
    git add -A 2>/dev/null || true
  fi

  local MSG COMMIT_EXIT
  if [[ "$MODE" == "full" ]]; then
    MSG="Session ${TODAY} ${NOW}: automated close via Stop hook"
  else
    MSG="Session ${TODAY} ${NOW}: defensive auto-save (no explicit close)"
  fi
  git commit -m "$MSG" 2>&1 | head -20 >&2 || true
  COMMIT_EXIT=${PIPESTATUS[0]}
  _guarded_push
  [[ "$COMMIT_EXIT" -eq 0 ]] && rm -f "$CORE_LAST_SAVE_BLOCKED" 2>/dev/null || true
  [[ "$COMMIT_EXIT" -eq 0 ]] && bash "$CORE_BIN_QUEUE_SHARED_PUSH" >/dev/null 2>&1 || true

  # Snapshot publisher (2026-07-12): fire the gated, DETACHED Core UX publisher so the
  # Render dashboard reflects this close automatically. Never blocks (fully detached).
  # Safe by construction: the publisher fingerprint-gates (no-op when nothing material
  # changed), locks + coalesces concurrent closes, and no-ops if core-ux or the deploy
  # remote is absent. Kill switch: `touch <core-ux>/.publish-disabled`.
  CORE_UX_DIR="$(dirname "$REPO")/core-ux"
  if [[ -x "$CORE_UX_DIR/scripts/publish-snapshot.sh" ]]; then
    ( cd "$CORE_UX_DIR" && nohup bash scripts/publish-snapshot.sh >/dev/null 2>&1 & ) 2>/dev/null || true
  fi
  fi   # ── close the commit-only guard opened at the clean-tree check (C1, Codex #3): everything
       #    below (capture + brain-update + lock release) runs on EVERY close, clean tree or not ──

  # Ledger capture (unified redesign step 1, 2026-07-18): register this Core's sessions into the
  # source-revision ledger + drain 'captured' jobs (export → verify vault md → mark done), so EVERY
  # close — walk-away included — reliably captures, instead of capture being nightly-only. Background
  # + fail-open (never blocks the close). Fork-safe (env-parameterized). Kill switch: CORE_LEDGER_CAPTURE=0.
  if [[ "${CORE_LEDGER_CAPTURE:-1}" != "0" && -f "$REPO/scheduling/brain-pg/discover.py" ]]; then
    (
      export CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}"
      python3 "$REPO/scheduling/brain-pg/discover.py" >>"$LOG" 2>&1
      python3 "$REPO/scheduling/brain-pg/capture_worker.py" >>"$LOG" 2>&1
    ) &
  fi

  # Brain update — SYNCHRONOUS-CLOSE model (2026-07-24: in-session extraction, no headless, no key):
  #   • full + .brain-synced-this-session  → the /close-core flow already ran capture+embed AND the
  #     in-session Haiku graph-extraction (foreground, verified). Brain is current NOW; nothing to spawn.
  #   • full, marker absent                → the in-session sync was skipped/failed. Spawn the detached
  #     worker as a PARTIAL fallback — but note (Codex): `fast` is now DETERMINISTIC (JSONL export +
  #     embed), so this recovers CAPTURE only, NOT graph extraction (extraction needs a live Agent()).
  #     The missed graph extraction is repaired by the NEXT in-session catch-up: SessionStart's
  #     `extract-pending --phase start` surfaces the pending directive, and the next /close-core drains
  #     it. So a marker-absent full close is capture-safe here + extraction-caught next session.
  #   • defensive (walk-away)              → deterministic capture + EMBED (2026-07-25). No LLM work:
  #     graph/assertion extraction need a live Agent(), so they alone carry to the next /close-core.
  if [[ "$MODE" == "full" && -f "$SYNC_MARKER" ]]; then
    echo "[$(date)] close[full]: brain synced in-session (marker present) — no worker spawned" >> "$LOG"
  elif [[ "$MODE" == "full" ]]; then
    echo "[$(date)] close[full]: NO sync marker — detached fallback recovers CAPTURE only (extraction caught at next start)" >> "$LOG"
    ( nohup bash "$CORE_HOOK_RUN_BRAIN_UPDATE" >/dev/null 2>&1 & ) 2>/dev/null || true
  else
    echo "[$(date)] close[defensive]: LLM work deferred to next /close-core; running deterministic drain" >> "$LOG"
    # DETERMINISTIC DRAIN ON WALK-AWAY (2026-07-25 — closes GAP 6).
    #
    # The operator's spec, 2026-07-24 06:30: whatever can happen without an LLM should happen the
    # moment the session exits or clears. The defensive path was honouring only
    # half of that — it captured the vault markdown but never embedded it, so a walk-away left BOTH
    # graph extraction (unavoidable: needs a live Agent()) and the evidence embed (entirely
    # deterministic, no LLM, no key) as debt. The embed half had no reason to wait.
    #
    # `run-brain-update.sh fast` = JSONL export + embed.py --incremental. No LLM. It queues on the
    # shared brain lock like everything else and resumes incrementally, so a slow/contended run
    # costs nothing. Detached + fail-open: a walk-away must never block Nick's exit.
    #
    # What still legitimately waits for the next /close-core: graph extraction and assertion
    # extraction. Those need a live session by construction — that is physics, not a design gap.
    ( nohup bash "$CORE_HOOK_RUN_BRAIN_UPDATE" fast >/dev/null 2>&1 & ) 2>/dev/null || true
    # Keep the health/status surfaces honest across walk-away sessions (Codex round-2 Medium):
    # run-brain-update.sh was the only thing refreshing .brain-health-status — with defensive
    # closes no longer spawning it, a stale RED (or stale GREEN) would persist indefinitely.
    # brain-health.py is read-only + deterministic (no LLM, no lock) → allowed on the defensive
    # path per the redesign's own rule. Backgrounded, fail-open.
    if [[ -f "$REPO/scheduling/brain-pg/brain-health.py" ]]; then
      (
        cd "$REPO/scheduling/brain-pg" || exit 0
        _H=$(CORE_INSTANCE="$REPO" python3 brain-health.py --quiet 2>/dev/null)
        [[ -n "$_H" ]] && echo "$_H" > "$CORE_INSTANCE/.claude/state/.brain-health-status" 2>/dev/null
        CORE_INSTANCE="$REPO" python3 brain-health.py --json 2>/dev/null \
          > "$CORE_INSTANCE/.claude/state/.brain-health.json" 2>/dev/null
      ) &
    fi
  fi
  # Release the one-Core-at-a-time session lock (written by session-start-check.sh).
  rm -f "$REPO/.claude/state/.session-lock" 2>/dev/null || true
  echo "[$(date)] === close[$MODE] complete (commit_exit=$COMMIT_EXIT) ===" >> "$LOG"
}

# ─────────────────────────────────────────────────────────────────────────────
# START — orient. Anchor the session-start time; (truth-drift surfacing stays in
# session-start-check.sh which calls `start`). Read-only beyond the anchor.
# ─────────────────────────────────────────────────────────────────────────────
lifecycle_start() {
  mkdir -p "$CORE_INSTANCE/.claude/state" 2>/dev/null || true
  local START_HUMAN
  START_HUMAN=$(bash "$CORE_HOOK_GET_SESSION_START_TIME" 2>/dev/null || true)
  [[ -n "$START_HUMAN" ]] && echo "$START_HUMAN" > "$CORE_SESSION_START" 2>/dev/null || true
  # Hygiene (Phase 2): sweep orphaned recall markers + brain-query breadcrumbs from
  # dead sessions (>2 days old). Keyed by session_id so they never mis-fire, but they
  # accumulate in state/. Harmless to remove; the active session's are fresh.
  find "$CORE_INSTANCE/.claude/state" -maxdepth 1 \( -name '.recall-required-*' -o -name '.brain-queried-*' \) -mtime +2 -delete 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────────────────
# NIGHTLY — the only heavy build. Delegates to the lock-wrapped helper.
# ─────────────────────────────────────────────────────────────────────────────
lifecycle_nightly() {
  # DEBT-GATED (2026-07-25, Nick's call). This used to `exec ... heavy` unconditionally at 02:00 —
  # a full graphify rebuild every night whether or not anything had changed. The operator's spec
  # for this job is a fallback — it should only run as a fallback, and otherwise not be used. A job that
  # runs every night regardless is routine heavy work, not a fallback.
  #
  # It now fires ONLY when there is debt it can actually fix: a failed/deferred prior chain, missing
  # embeddings, or checkpoints newer than graph.json. It deliberately does NOT wake for pending
  # graph/assertion extraction — that needs a live Agent(), so the nightly cannot drain it; that
  # debt surfaces at SessionStart and is drained by the next /close-core.
  #
  # The probe FAILS OPEN: if it cannot reach the DB it reports debt and the rebuild runs. A skipped
  # fallback is worse than a redundant one.
  local NLOG="/tmp/session-lifecycle-$(basename "$REPO")-$TODAY.log"

  # CORPUS INGEST RUNS UNCONDITIONALLY, ABOVE THE DEBT GATE (2026-08-20).
  #
  # Measured, not supposed: the nightly no-op'd four nights running ("BRAIN-DEBT: none") while
  # every Core's corpus sat 78-92h stale, because ingest had exactly two doors and both were shut.
  # Door one is a session close, and Nick's sessions run for days. Door two was BELOW this gate,
  # inside run-brain-update heavy — so the corpus could only grow on nights the GRAPH happened to
  # be stale. Those are unrelated questions. "Is graph.json older than the checkpoints" says
  # nothing about whether there are unmined corrections, and gating one on the other means the
  # learning loop's input is a side effect of an embedding schedule.
  #
  # This does NOT re-litigate Nick's 2026-07-25 call. That call was about the HEAVY REBUILD — "a
  # nightly job that is just a fall back ... it shouldn't be used" — and the heavy rebuild stays
  # debt-gated below, untouched. The miner is not heavy work: measured 2.1s on life's 22-file
  # archive, pure JSONL scan, no LLM, no graphify, no brain lock. Cheap enough that the fallback
  # framing does not apply to it.
  #
  # Safe to run concurrently with a close: dedup is structural behind uq_patobs_source_label_org
  # (added 2026-08-05 after Codex found in-process dedup could double-insert), and detect() is a
  # full deduped re-scan, so a redundant run inserts nothing.
  #
  # Fail-open: a miner failure must never cost the debt probe or the rebuild below.
  if [[ -f "$REPO/scheduling/claude-si/learned-corpus-miner.py" ]]; then
    CORE_INSTANCE="$REPO" python3 "$REPO/scheduling/claude-si/learned-corpus-miner.py" \
      --detect >> "$NLOG" 2>&1
    _MINER_EXIT=$?   # captured BEFORE anything else runs; `|| true` here would log the
                     # exit of `true` and report a healthy 0 for every failure forever.
    echo "[$(date)] nightly: learned-corpus detect exit=$_MINER_EXIT" >> "$NLOG"
  fi

  # RECALL-EVAL REFRESH — nightly-invoked, WEEKLY-GATED, detached (2026-08-25).
  #
  # Closes the "re-run eval.py + schedule nightly" half of the recall-eval SI item WITHOUT standing
  # up a scheduler, because Nick's 2026-08-25 directive is "each core should only have a bus monitor
  # thats it" and a new cron is precisely what that forbids. The launchd product jobs were exempted,
  # so this rides the one that already exists — the same argument that put the corpus miner above
  # this gate.
  #
  # Placed ABOVE the debt gate for the corpus miner's reason: benchmark staleness and graph
  # staleness are unrelated questions, and gating one on the other means the recall number only
  # refreshes on nights the embeddings happen to be behind.
  #
  # The script self-gates to 7 days and to the recall_eval_owner seat, so on six nights in seven
  # this costs a process spawn. It detaches because this function later `exec`s into the heavy
  # rebuild and the job carries ExitTimeOut 600 — a foreground eval would blow one or delay the
  # other. Fail-open: `|| true`, never costs the debt probe below.
  if [[ -x "$REPO/bin/recall-eval-refresh.sh" ]]; then
    CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" \
      bash "$REPO/bin/recall-eval-refresh.sh" >> "$NLOG" 2>&1 || true
    echo "[$(date)] nightly: recall-eval refresh checked (weekly-gated)" >> "$NLOG"
  fi

  # ── CROSS-CORE CORROBORATION (2026-08-28) ────────────────────────────────────────────────
  # corroborate.py wires the `same_as` edges that let a query in one Core see what another Core
  # knows. It was BUILT AND NEVER WIRED: all 19,977 edges carry created_at 2026-07-07, from a
  # single manual run. Everything all five brains learned in the 52 days since was unbridged.
  # Idempotent by construction (ON CONFLICT on the edge), pure SQL + embeddings — no model — so
  # it is safe on the nightly. Inherently all-org: it looks for the same concept ACROSS orgs.
  # ABOVE the debt gate deliberately: the gate `exec`s into the heavy rebuild, so anything below
  # it only runs on nights that happen to have no debt. Fail-open.
  # BOUNDED + LOCK-AWARE (Codex review, 2026-08-28). Three defects in the first cut:
  #   HIGH — unbounded. corroborate does a self-join with a vector distance over every entity and
  #     fetches all matches into memory. No statement timeout, no batch limit. This sits ABOVE the
  #     debt gate by necessity, so a hang here means the heavy rebuild NEVER runs. And `|| true`
  #     is not fail-open against a hang — it only swallows a completed nonzero exit.
  #   HIGH — no coordination with close-time brain writers. It opens its own transaction and can
  #     publish edges computed from a snapshot racing an embed/rebuild.
  #   There is no `timeout(1)` on this host, so the bound is imposed DB-side via statement_timeout,
  #   which is where the actual hang risk lives.
  _BRAIN_HASH="$(echo "${CORE_BRAIN:-$HOME/AI Projects/core-brain}" | md5 -q 2>/dev/null || echo x)"
  if [[ -d "/tmp/core-brain-${_BRAIN_HASH}.lock" ]]; then
    echo "[$(date)] nightly: brain lock HELD — skipping corroborate + drift (next run picks up)" >> "$NLOG"
  else
    if [[ -f "$REPO/scheduling/brain-pg/corroborate.py" ]]; then
      CORE_INSTANCE="$REPO" PGOPTIONS='-c statement_timeout=180000' \
        python3 "$REPO/scheduling/brain-pg/corroborate.py" >> "$NLOG" 2>&1 \
        && echo "[$(date)] nightly: cross-Core corroboration ran" >> "$NLOG" \
        || echo "[$(date)] nightly: corroborate failed/timed out (fail-open)" >> "$NLOG"
    fi
  fi

  # ── COMPILE-TRUTH DRIFT DETECTION, ALL ORGS (2026-08-28) ─────────────────────────────────
  # Measured that day: 534 of 68,296 non-Source entities had EVER been compiled (0.78%), and
  # business/school/finance/ops were at exactly ZERO — /refresh-truth is a life-side slash
  # command nobody ever ran on a peer. So every hub in four of five Cores carried the summary it
  # was born with, which is why person hubs read stale and dead projects still read as central.
  #
  # HONEST SCOPE: only --detect is wired here. The refresh itself is partition → MODEL → ingest,
  # and a hook cannot call Claude (the same wall ask_miner hit; si-drain.sh exists precisely
  # because of it). Detection is cheap and model-free, and it converts a SILENT staleness into a
  # counted, per-seat backlog that the nightly log and core-doctor can both report. Wiring the
  # model half belongs with si-drain, not here.
  if [[ -f "$REPO/scheduling/brain-pg/compile-truth-refresh.py" ]]; then
    # THIS SEAT ONLY — the five-org loop that stood here was a NO-OP and I shipped it tonight.
    # _env resolves org from identity.json and IGNORES CORE_ORG_ID when they disagree (it prints
    # "using 1"), so all five iterations ran as org 1 and the org-qualified filenames I added to
    # fix Codex's CRITICAL then labelled org-1 data as org5 — mislabelled, which is worse than
    # the overwriting it was meant to fix. A seat can only detect its OWN partition, which is
    # exactly the placement rule: peers detect at their own close (wired into lifecycle_close
    # 2026-08-28) and their own nightly.
    CORE_INSTANCE="$REPO" PGOPTIONS='-c statement_timeout=120000' \
      python3 "$REPO/scheduling/brain-pg/compile-truth-refresh.py" --detect >> "$NLOG" 2>&1 || true
    echo "[$(date)] nightly: compile-truth drift detected (this seat)" >> "$NLOG"
  fi

  if CORE_INSTANCE="$REPO" CORE_ORG_ID="${CORE_ORG_ID:-1}" \
     CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
     python3 "$REPO/bin/verify-brain-synced.py" --nightly-debt >> "$NLOG" 2>&1; then
    echo "[$(date)] nightly: debt found → running heavy rebuild" >> "$NLOG"
    exec bash "$CORE_HOOK_RUN_BRAIN_UPDATE" heavy
  fi
  echo "[$(date)] nightly: no debt → no-op (fallback job, nothing to catch up)" >> "$NLOG"
  return 0
}

PHASE="${1:-}"
case "$PHASE" in
  start)   lifecycle_start ;;
  close)   lifecycle_close "${2:-defensive}" "${3:-}" ;;   # $3 = caller's own session_id (marker scope)
  nightly) lifecycle_nightly ;;
  *) echo "usage: session-lifecycle.sh {start|close [full|defensive] [session_id]|nightly}" >&2; exit 2 ;;
esac
