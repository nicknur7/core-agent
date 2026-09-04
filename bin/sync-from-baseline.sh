#!/usr/bin/env bash
# sync-from-baseline.sh — pull shared subset from nicknur7/core-agent into current Core.
#
# Wired into the SessionStart hook (--quiet) on PULL-ONLY cores so it runs every
# open. The baseline-writer core (manifest .baseline_writer) is SKIPPED in --quiet
# mode to avoid rsync'ing the older baseline over its own unpushed shared edits.
# Manual fire via /sync slash command (normal mode — writer-guard does not apply).
#
# Flags:
#   --check   dry-run; report what would change. No writes.
#   --quiet   suppress per-file rsync output (hook use).
#   (default) normal: print summary at end.
#
# Spec: tasks/specs/spec-self-hosted-cores-2026-05-19.md Phase 7.
set -uo pipefail

MODE="normal"
# --check and --quiet are MUTUALLY EXCLUSIVE and this refuses rather than picking one.
#
# Until 2026-08-16 both flags wrote this ONE variable in this loop, so the LAST one won:
#     --check --quiet  ->  MODE=quiet  ->  A REAL SYNC, from a command whose first flag says
#                                          "dry run". Ledger written, files installed, and
#                                          reconcile-hooks took the --apply branch at :490.
#     --quiet --check  ->  MODE=check  ->  dry run
#
# That is a guard bypass, not a cosmetic bug. pretooluse-guard.sh:497 passes read-only modes
# (--check/--dry-run/--help) through UNGATED, so `--check --quiet` was an UNREVIEWED baseline
# pull: the guard read the first flag, the script obeyed the second. core-business ran two of
# them tonight believing they were free reads, and life had told the whole fleet --check needed
# no approval — true of --check alone, false of the pair.
#
# Found by core-business (bus #1643/#1677) from its own before/after state, and core-finance
# (#1681) named the fix: the SIBLING script already gets this right. sync-to-baseline.sh:19-30
# keeps `CHECK_ONLY` as a SEPARATE variable orthogonal to MODE, so there --check and --quiet
# compose instead of clobbering. Mirroring that fully here is the correct end state and is
# tracked as T048; it touches seven `MODE == "check"` sites and is not a change to make at
# 21:45 with two seats mid-pull.
#
# Refusing is the minimum-blast-radius close: it cannot silently do the wrong thing in either
# direction, only stop. A caller who meant a dry run gets an error, never a live sync.
_SEEN_CHECK=0
_SEEN_QUIET=0
for arg in "$@"; do
  case "$arg" in
    --check) MODE="check"; _SEEN_CHECK=1 ;;
    --quiet) MODE="quiet"; _SEEN_QUIET=1 ;;
    --ref=*) SYNC_REF="${arg#--ref=}"; _SEEN_REF=1 ;;
    --help|-h)
      sed -n '2,12p' "$0"; exit 0 ;;
    # REFUSE WHAT THIS COPY DOES NOT IMPLEMENT. There was no `*)` branch, so an unrecognised flag
    # matched nothing, fell through in silence, and the sync proceeded with MODE unchanged. On a seat
    # that predates `--ref`, `--quiet --ref=<sha>` is therefore a FULL HEAD pull wearing the shape of
    # a staged one — the caller asked to advance one hop and got every hop, with nothing said.
    #
    # The mutual-exclusion guard FOUR LINES BELOW already learned this exact lesson: it exists
    # because "passing both silently ran a REAL SYNC (last flag won)". Same defect class, one line
    # apart — closed for two KNOWN flags and left open for every unknown one. Found by core-ops,
    # who could not apply it themselves because `bin/` is shared and they are a puller.
    #
    # This has to live HERE rather than in a fleet-wide instruction, and that is the whole argument:
    # **a seat too old to have `--ref` is precisely the seat that cannot be told about `--ref`.** A
    # warning that depends on the reader already having the thing they are missing reaches nobody.
    *)
      echo "sync-from-baseline: REFUSED — unknown option: $arg" >&2
      echo "  This copy does not implement it. If you expected --ref, this seat predates it and" >&2
      echo "  would have done a FULL pull instead of a staged one. Pull normally first." >&2
      exit 2 ;;
  esac
done
if [[ "$_SEEN_CHECK" -eq 1 && "$_SEEN_QUIET" -eq 1 ]]; then
  echo "sync-from-baseline: REFUSED — --check and --quiet are mutually exclusive." >&2
  echo "  Passing both silently ran a REAL SYNC (last flag won), from a command the guard" >&2
  echo "  exempts as read-only. Pass exactly one:" >&2
  echo "    --check   dry run, writes nothing" >&2
  echo "    --quiet   real pull, trust-root files held back for a separate apply" >&2
  exit 2
fi

# Locate current Core: prefer $CLAUDE_PROJECT_DIR, fall back to script-dir-relative.
CORE_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MANIFEST="$CORE_DIR/bin/sync-manifest.json"

if [[ ! -f "$MANIFEST" ]]; then
  echo "sync-from-baseline: manifest not found at $MANIFEST" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "sync-from-baseline: requires jq" >&2
  exit 1
fi

BASELINE_REPO=$(jq -r '.baseline_repo' "$MANIFEST")
BASELINE_BRANCH=$(jq -r '.baseline_branch' "$MANIFEST")

# Writer-guard: SessionStart auto-pull (--quiet) on the baseline-writer core would
# rsync the older baseline OVER the writer's own unpushed shared edits (baseline
# wins). The writer is the source of truth, so skip the auto-pull there. Manual
# `/sync` (normal/check mode) is unaffected — only the --quiet path is guarded.
WRITER=$(jq -r '.baseline_writer // empty' "$MANIFEST" | sed 's#^core-##')
THIS_SLUG=$(basename "$CORE_DIR" | sed 's#^core-##')
if [[ "$MODE" == "quiet" && -n "$WRITER" && "$THIS_SLUG" == "$WRITER" ]]; then
  exit 0
fi

SYNC_LOG="$CORE_DIR/.claude/state/.last-baseline-sync"
TS=$(date +%Y%m%d-%H%M%S)
# mktemp, not a hand-built path. "/tmp/core-baseline-${TS}-$$" is fully predictable from the clock
# and a PID, so anything that can write /tmp could pre-create it — and this directory becomes the
# SOURCE that rsync copies onto a Core's hooks and gates. git clone into an existing EMPTY dir is
# fine, which is exactly what mktemp -d hands back, so nothing downstream changes. The readable
# prefix is kept for anyone reading `ls /tmp` during a sync.
TMP=$(mktemp -d "/tmp/core-baseline-${TS}-XXXXXX") || { echo "[sync-from-baseline] cannot create temp dir" >&2; exit 1; }

log() { [[ "$MODE" != "quiet" ]] && echo "$@"; }

cleanup() { rm -rf "$TMP" "${TMP}-prev"; }
trap cleanup EXIT INT TERM HUP
# ^ EXIT alone leaks on a kill. A SessionStart pull is a child of a session the user can Ctrl-C or
# close the terminal on, and each of those leaves a full baseline checkout (plus its -prev worktree,
# which is also a registration inside the clone's .git) behind in /tmp permanently.
# ^ "${TMP}-prev" is the prior-baseline worktree added below (PREV_BASELINE_DIR). It is a SIBLING
# path to $TMP, not a child of it, so `rm -rf "$TMP"` alone never touched it — every pull that
# reached the worktree-add branch leaked a full checked-out baseline tree into /tmp forever.
# Referencing it here before it exists is fine: this is a string, not a stat.

# SYNC_INCOMPLETE — one flag shared by the fetch, hook-reconciliation and migration steps below,
# so a failure in ANY of them (a) still lets the OTHER, independent steps run and report their own
# status instead of one failure hiding whether anything after it worked, and (b) is never lost by
# the time this script decides its own exit code or whether to stamp .last-baseline-sync.
#
# Fixes three CRITICAL/MEDIUM findings (Codex review, 2026-08-31) that were all the same shape:
# `|| true` / an unconditional `exit 0` turned a real failure into a reported success. This script
# is how every pull-only Core receives shared code and runs UNATTENDED at SessionStart in --quiet
# — a lying exit code is how four Cores sat two baselines behind a broken compile-truth detector
# for weeks with nothing ever showing red.
#
# Deliberately mode-aware rather than a blanket fail-hard: a manual `/sync pull` or `--check` has
# a human watching and is owed a loud, non-zero failure. --quiet is SessionStart — a hard-fail
# there bricks every session open on a bad network day — so it stays fail-soft, but "fail-soft"
# now means "surface it and don't lie about the marker", not "swallow it".
SYNC_INCOMPLETE=0
SYNC_INCOMPLETE_REASONS=""
mark_incomplete() {
  SYNC_INCOMPLETE=1
  SYNC_INCOMPLETE_REASONS+="$1"$'\n'
  printf '%s sync-from-baseline incomplete: %s\n' "$(date -Iseconds 2>/dev/null || date)" "$1" \
    >> "$CORE_DIR/.claude/state/.sync-failures" 2>/dev/null || true
}

log "[sync-from-baseline] mode=$MODE  core=$CORE_DIR  baseline=$BASELINE_REPO@$BASELINE_BRANCH"

# TEST SEAM, deliberately crippled so it cannot become a supply-chain hole.
#
# The trust-root hold below is the most safety-relevant behaviour in this script, and it was
# untestable: the baseline URL was built inline, so no test could drive a real pull. That is the same
# shape as the codex fence, which sat unverifiable for weeks because proving it worked required
# writing the thing it blocked — so a seam is warranted here.
#
# But an env var that redirects where a Core fetches its shared CODE from is exactly how you would
# attack this fleet. So the override accepts ONLY AN EXISTING LOCAL DIRECTORY: no scheme, no host, no
# network. A test can point at a throwaway repo on disk; an attacker gains nothing they did not
# already have, because writing to a local path they control means they already had local write.
# TWO variables are required, not one. sentinel-code's advisory on this seam: with a single var, a
# STALE EXPORTED value in someone's shell could silently redirect a real SessionStart pull at a local
# path. It could never get a trust-root file through — the quiet-mode exclusion below is
# unconditional and does not care where the baseline came from — but sourcing ordinary shared code
# from an unintended place is still not something an accident should be able to arrange. Demanding an
# explicit intent marker alongside the path means a leftover variable on its own does nothing.
BASELINE_URL="https://github.com/${BASELINE_REPO}.git"
if [[ -n "${CORE_BASELINE_URL_LOCAL_TEST:-}" ]]; then
  if [[ "${CORE_SYNC_TEST_MODE:-}" != "1" ]]; then
    echo "[sync-from-baseline] REFUSED test seam: CORE_BASELINE_URL_LOCAL_TEST is set but" >&2
    echo "  CORE_SYNC_TEST_MODE=1 is not. A stale variable must not redirect a real pull." >&2
    exit 1
  fi
  # Strip a leading `file://` ONLY for the existence check below — the variable itself, and what
  # ends up in BASELINE_URL, is untouched. Plain local paths make `git clone --depth 1` silently
  # ignore --depth (git's own warning: "--depth is ignored in local clones; use file:// instead"),
  # so a test driving this seam with a bare path can never exercise the `fetch --depth 50`
  # deepening branch below — the shallow clone is never actually shallow. Found 2026-09-01: the
  # shipped test suite had zero coverage of that branch for exactly this reason.
  # Still "no scheme, no host, no network": the only scheme accepted is `file://`, itself
  # network-incapable, and after stripping it the result must still resolve to an EXISTING local
  # `.git` — an attacker gains nothing a bare-path override didn't already give them.
  _seam_check="${CORE_BASELINE_URL_LOCAL_TEST#file://}"
  if [[ -d "$_seam_check/.git" ]]; then
    BASELINE_URL="$CORE_BASELINE_URL_LOCAL_TEST"
    echo "[sync-from-baseline] TEST SEAM ACTIVE — baseline is a LOCAL path: $BASELINE_URL" >&2
  else
    echo "[sync-from-baseline] REFUSED test seam: CORE_BASELINE_URL_LOCAL_TEST is not a local git repo" >&2
    exit 1
  fi
fi

# Shallow clone baseline
# --- STAGED PULL (`--ref=<sha>`) - how a seat that has fallen behind can catch up at all ---------
#
# Rule 7 of sentinel-code ASKs when a sync exceeds 50 file changes, and its stated remedy is "Nick
# should confirm before the gate opens". There is NO code path that turns his confirmation into a
# mint: sentinel-approve requires a literal `VERDICT: APPROVE`, records that ASK is not an approval,
# and forbids re-running for a better verdict. So an ASK on Rule 7 is a dead end.
#
# And Rule 7 keys on FILE COUNT, which measures how far behind a seat is, NOT how risky the change
# is. core-ops stated the consequence exactly: **the further behind a seat falls, the more certain
# it is that it cannot catch up.** They hit it at 58 files, having hit the same wall once before -
# on 2026-08-21 Nick ran the sync by hand for the same reason.
#
# This does NOT relax Rule 7, raise the ceiling, or add an ASK-override - each of which would weaken
# the gate to buy convenience. It removes the thing that made the gate unsatisfiable: a seat can now
# advance to an INTERMEDIATE published commit, every hop small enough to be reviewed on its own
# merits and every hop reviewed normally. Many small reviewed steps instead of one unreviewable leap.
#
# FORWARD-ONLY, ON THE PUBLISHED LINE. The ref must be an ancestor of the baseline branch head (so it
# is real published history, not an attacker's side commit) and a strict descendant of whatever this
# seat last synced (so `--ref` can never roll a Core BACK onto superseded code - that would be a
# downgrade attack wearing a convenience flag).
if [[ "${_SEEN_REF:-0}" -eq 1 ]]; then
  # `--ref=` with an EMPTY value used to leave SYNC_REF unset, so the `-n` test was false and the
  # run fell through to an ordinary full-HEAD pull — silently doing something other than asked,
  # which is the same defect the `*)` branch above exists to close. Refuse it.
  if [[ -z "${SYNC_REF:-}" ]]; then
    echo "[sync] REFUSED --ref: empty value. Pass a commit sha, or omit --ref for a full pull." >&2
    exit 2
  fi
  if ! [[ "$SYNC_REF" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    echo "[sync] REFUSED --ref: not a commit sha: $SYNC_REF" >&2; exit 2
  fi
  if ! CLONE_ERR=$(git clone --branch "$BASELINE_BRANCH" --quiet "$BASELINE_URL" "$TMP" 2>&1); then
    echo "[sync] WARN: baseline clone FAILED - ${CLONE_ERR}" >&2; exit 1
  fi
  if ! (cd "$TMP" && git cat-file -e "${SYNC_REF}^{commit}" 2>/dev/null); then
    echo "[sync] REFUSED --ref: $SYNC_REF is not a commit in the baseline." >&2; exit 2
  fi
  if ! (cd "$TMP" && git merge-base --is-ancestor "$SYNC_REF" HEAD 2>/dev/null); then
    echo "[sync] REFUSED --ref: $SYNC_REF is not an ancestor of ${BASELINE_BRANCH}." >&2
    echo "  Only commits on the published baseline line can be synced to." >&2; exit 2
  fi
  # A WELL-FORMED RECORD, NOT ANY LINE MENTIONING A SHA.
  #
  # This was `grep -o 'baseline=<sha>' <file> | tail -1`, which scans EVERY line and takes the last
  # textual hit. Append any line containing an older `baseline=<sha>` — a comment, a stray note, a
  # truncated write — and it becomes the seat's "current" baseline, after which `--ref` will happily
  # "advance" from that stale point to something that is a real downgrade. sentinel-code reproduced
  # it live (STAGED PULL, rc=0) while adversarially testing the fix for the previous hole in these
  # same lines.
  #
  # MY FIRST FIX FOR IT WAS WRONG in the obvious way: I read the file's LAST LINE, and the decoy IS
  # the last line, so it changed nothing. The test passed anyway because I had pointed --ref at the
  # same sha as the decoy, so it refused as a no-op — a vacuous pass, right verdict, wrong reason.
  #
  # So: match only lines in the RECORD FORMAT this file is written in (ISO timestamp, then
  # `baseline=<sha>`), and take the last of those. A prose line can no longer answer the question.
  # A fully FORGED record still can — but writing one requires local write access to
  # `.claude/state/`, which is the same tier as editing the script itself, and is the trust boundary
  # this guard already sits inside rather than a gap in it.
  #
  # An unmatched or malformed file yields an empty `_LAST`, which the fail-closed check below
  # refuses. Safe direction, and the reason that check runs first.
  _LAST=$(grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[^ ]+ baseline=[0-9a-fA-F]{7,40}' \
            "$CORE_DIR/.claude/state/.last-baseline-sync" 2>/dev/null \
          | tail -n 1 | grep -oE '[0-9a-fA-F]{7,40}$')
  # FAIL CLOSED WHEN THE SEAT'S CURRENT BASELINE CANNOT BE ESTABLISHED.
  #
  # The strict-descendant check below is the ONLY thing standing between `--ref` and a downgrade,
  # and it used to live inside `if [[ -n "$_LAST" ]] && git cat-file -e ...`. When
  # `.last-baseline-sync` is missing, empty, unparseable, or names a commit the clone cannot
  # resolve, that compound condition is simply FALSE — so the whole block was skipped, in silence,
  # and `--ref=<any ancestor of the branch head>` succeeded. That is precisely a fresh seat, a reset
  # seat, or a seat whose state file an attacker deleted: the three cases where you would most want
  # the guard, and the exact three where it did not run.
  #
  # Found by sentinel-code on review, which also noted the shipped test never exercised it — every
  # fixture wrote a valid record, so the suite was green over the one condition that mattered.
  #
  # Refusing costs nothing: a seat that cannot say where it is can still pull normally to HEAD,
  # which is by definition the freshest code and never a downgrade.
  if [[ -z "$_LAST" ]]; then
    echo "[sync] REFUSED --ref: this seat has no readable .last-baseline-sync record, so there is" >&2
    echo "  no way to prove $SYNC_REF comes AFTER what it already has. Pull normally (no --ref) —" >&2
    echo "  a full pull to HEAD is always the freshest code and can never be a downgrade." >&2
    exit 2
  fi
  if ! (cd "$TMP" && git cat-file -e "${_LAST}^{commit}" 2>/dev/null); then
    echo "[sync] REFUSED --ref: this seat's recorded baseline $_LAST is not resolvable in the" >&2
    echo "  baseline clone, so the forward-only check cannot run. Pull normally (no --ref)." >&2
    exit 2
  fi
  if true; then
    if [[ "$_LAST" == "$SYNC_REF"* || "$SYNC_REF" == "$_LAST"* ]]; then
      echo "[sync] REFUSED --ref: already at $SYNC_REF - nothing to advance to." >&2; exit 2
    fi
    if ! (cd "$TMP" && git merge-base --is-ancestor "$_LAST" "$SYNC_REF" 2>/dev/null); then
      echo "[sync] REFUSED --ref: $SYNC_REF does not come AFTER this seat's current baseline" >&2
      echo "  $_LAST. --ref advances a seat; it never rolls one back onto older code." >&2
      exit 2
    fi
  fi
  (cd "$TMP" && git checkout --quiet "$SYNC_REF")
  echo "[sync] STAGED PULL -> $SYNC_REF (an intermediate published baseline)"
elif ! CLONE_ERR=$(git clone --depth 1 --branch "$BASELINE_BRANCH" --quiet \
  "$BASELINE_URL" "$TMP" 2>&1); then
  # VISIBLE even in --quiet (which only gates log() stdout, not stderr) + a
  # persistent marker, so a silently-failing sync is never invisible again.
  # 2026-06-04: a stale gh/keychain credential made every SessionStart pull a
  # silent no-op (exit 0) — the whole multi-Core drift hid behind this.
  echo "[sync-from-baseline] WARN: baseline clone FAILED — shared code NOT synced this session. ${CLONE_ERR}" >&2
  printf '%s sync-from-baseline clone-failed: %s\n' "$(date -Iseconds 2>/dev/null || date)" "${CLONE_ERR%%$'\n'*}" \
    >> "$CORE_DIR/.claude/state/.sync-failures" 2>/dev/null || true
  # UNCONDITIONAL `exit 0` HERE WAS THE BUG (Codex CRITICAL, :254). Every caller — the /sync
  # command, Nick running this by hand, a script checking $? — saw SUCCESS on a pull that synced
  # NOTHING, because a clone failure is a total-loss failure: nothing past this point in the
  # script runs, so .last-baseline-sync was never at risk of being falsely stamped here — the lie
  # was purely in the exit code. --quiet stays exit 0 on purpose: a hard-fail at SessionStart
  # bricks the Core, and the failure is already visible (stderr survives --quiet; .sync-failures
  # is a persistent marker). A manual/`--check` caller has no such constraint and is owed a real
  # non-zero exit, not a shell that happily chains past a no-op as if it were a success.
  if [[ "$MODE" == "quiet" ]]; then
    exit 0
  fi
  exit 1
fi

BASELINE_SHA=$(cd "$TMP" && git rev-parse HEAD)
log "[sync-from-baseline] baseline at $BASELINE_SHA"

# PREVIOUS-BASELINE CHECKOUT — what makes is_shared_path_dirty() able to tell a human edit from an
# uncommitted prior pull. See that function's header for why the distinction is the whole feature.
#
# Best-effort by design, and it FAILS OPEN: if the previously-synced SHA is unknown, unreachable in
# this clone (a --depth 1 fetch usually cannot see it), or the worktree cannot be made, the variable
# stays empty and every differs-from-HEAD file is treated as CLEAN and overwritten normally. That is
# the correct direction to fail — the alternative froze four Cores on stale code to protect edits
# that did not exist. A pull that cannot prove local authorship should deliver fixes.
PREV_BASELINE_DIR=""
_PREV_SHA=$(grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[^ ]+ baseline=[0-9a-fA-F]{7,40}' \
              "$CORE_DIR/.claude/state/.last-baseline-sync" 2>/dev/null \
            | tail -n 1 | grep -oE '[0-9a-fA-F]{7,40}$')
if [[ -n "${_PREV_SHA:-}" && "$_PREV_SHA" == "$BASELINE_SHA" ]]; then
  # THE BASELINE HAS NOT MOVED SINCE THE LAST PULL — and this is the COMMON case, not the exotic
  # one: most SessionStart pulls hit an unchanged remote. The prior baseline is therefore the clone
  # already sitting in $TMP. No worktree, no fetch, no deepening — same commit, same bytes.
  #
  # The first cut treated equal SHAs as "nothing to check" and skipped the block entirely, leaving
  # PREV_BASELINE_DIR empty and handing the file to the fail-open below. Codex found what that
  # costs and reproduced it in two pulls: pull 1 correctly HOLDS a genuine local edit and stamps
  # .last-baseline-sync with the new SHA; pull 2 reads that stamp back, sees prev == current, skips
  # the checkout, fails open, and rsync overwrites the very edit pull 1 protected — silently, exit
  # 0, and not even a suppressed warning, because there is nothing left to warn about once the file
  # is judged clean. A hold that survives exactly one pull is not a hold; it is a delay.
  #
  # So equal SHAs are the case where proof is CHEAPEST and most certain, and it was the one case
  # getting no proof at all. Assigning $TMP makes the unchanged-baseline pull the best-protected
  # pull instead of the only unprotected one.
  PREV_BASELINE_DIR="$TMP"
  log "[sync-from-baseline] baseline unchanged since last pull (${_PREV_SHA:0:7}) — comparing against it directly"
elif [[ -n "${_PREV_SHA:-}" ]]; then
  if ! git -C "$TMP" cat-file -e "${_PREV_SHA}^{commit}" 2>/dev/null; then
    git -C "$TMP" fetch -q --depth 50 origin "$BASELINE_BRANCH" 2>/dev/null || true
  fi
  if git -C "$TMP" cat-file -e "${_PREV_SHA}^{commit}" 2>/dev/null; then
    if git -C "$TMP" worktree add -q --detach "${TMP}-prev" "$_PREV_SHA" 2>/dev/null; then
      PREV_BASELINE_DIR="${TMP}-prev"
      log "[sync-from-baseline] prior baseline ${_PREV_SHA:0:7} checked out for local-edit detection"
    fi
  fi
fi
[[ -n "$PREV_BASELINE_DIR" ]] || log "[sync-from-baseline] prior baseline unavailable — differs-from-HEAD files will be treated as clean (fail-open)"

# Decide WHAT to sync using the freshly-cloned manifest, not the stale local copy.
# The local manifest is what-we-had-before-this-pull; if the baseline added a new
# shared dir (e.g. scheduling/claude-si when the learned layer shipped), the local
# manifest wouldn't know about it and the dir wouldn't sync until a SECOND pull
# (the "pull twice" lag). Reading the cloned manifest collapses that to one pull —
# any manifest change applies on the same sync, for every fork. Repo/branch/writer
# were already read from the local manifest above (needed to locate + auth the clone);
# only the what-to-copy lists (shared.dirs/files, per_core_keep, per_core_extras) move
# to the cloned source. Falls back to local if the clone lacks the file.
#
# BUT NOT ON AN UNATTENDED PULL, as of 2026-08-06. core-finance found the hole (bus #685) and the
# paragraph above is its own best statement of the mechanism: "any manifest change applies on the same
# sync, for every fork."
#
# per_core_keep is therefore NOT a local veto. The baseline supplies the list of what the baseline may
# not replace, and that list takes effect on the pull that delivers it. One baseline commit dropping
# `.claude/agents/sentinel.md` from per_core_keep replaces every Core's Sentinel spec on the next
# automatic SessionStart pull — rule change and file replacement in one atomic operation, no second
# pull in which anyone could notice.
#
# Adding the manifest to TRUST_ROOT_PATHS alone does NOT fix this, and I nearly shipped believing it
# did: that list is not built until line ~175, and the excludes are not applied until ~223, so the
# cloned manifest has already been chosen as the governing one. Holding the FILE back stops it being
# installed while the pull still OBEYS it. The exclusion is necessary and it is not sufficient.
#
# So the rule is scoped by mode rather than removed. A deliberate `/sync pull` is gated — it routes
# through pretooluse-guard to sentinel-code, whose Rule 6 already treats a manifest change as
# reviewable — and keeps the no-lag behaviour. An automatic pull, which has no reviewer and no agent
# in the loop, keeps using the manifest this Core already had and reports the divergence instead.
#
# Cost of the scoping, stated honestly: a genuinely new shared dir now takes two pulls to reach a Core
# that never runs /sync pull manually. That is the lag this branch was written to remove. It is worth
# paying, because the same mechanism that makes a new dir arrive one pull sooner makes a REMOVAL OF
# PROTECTION arrive one pull sooner, and only one of those is reversible.
MANIFEST_PENDING=""
if [[ -f "$TMP/bin/sync-manifest.json" ]]; then
  if [[ "$MODE" == "quiet" ]] && ! cmp -s "$TMP/bin/sync-manifest.json" "$CORE_DIR/bin/sync-manifest.json" 2>/dev/null; then
    MANIFEST_PENDING="bin/sync-manifest.json"
    log "[sync-from-baseline] baseline manifest DIFFERS — unattended pull keeps the local one"
  else
    MANIFEST="$TMP/bin/sync-manifest.json"
    log "[sync-from-baseline] using cloned manifest for sync decisions (no pull-twice lag)"
  fi
fi

# Capture baseline SHA at start so successful pull below can stamp it into
# .last-baseline-sync. Added 2026-05-22 — sync-to-baseline had a parallel
# fix to stamp on successful push; this side was already partly wired but
# is reaffirmed here as the authoritative log of pulls.

CHANGED=0
DRIFT_WARNINGS=""

# Read per_core_keep list once (used per-dir below to build rsync excludes).
# C1 fix 2026-05-19: pull-side was missing per_core_keep filtering. Push side
# excluded trust-root paths correctly; pull side did not. Asymmetric. If
# baseline ever carried any per_core_keep path (e.g. via a manual git push
# bypassing sync-to-baseline), an unfiltered pull would overwrite each
# Core's trust-root files. Defense-in-depth: mirror the push-side per-dir
# exclude logic so the pull side is symmetrically protected.
PER_CORE_KEEP=$(jq -r '.per_core_keep[]' "$MANIFEST")

# ─────────────────────────────────────────────────────────────────────────────
# DELIVERY IS NOT APPLICATION. This is the gap the whole pending-action mechanism exists to close.
#
# Nick built the SessionStart auto-pull so every Core updates itself with zero friction, and for
# CODE that works exactly as intended. What changed is what gets shipped. Three things a pull
# physically cannot finish on its own:
#
#   1. REGISTER A HOOK. `.claude/hooks/` is shared so the FILE arrives, but `.claude/settings.json`
#      is per_core_keep so the REGISTRATION never travels. Verified 2026-08-06: a new gate shipped
#      to four Cores would sit on disk, registered 0 times, doing nothing, forever.
#   2. REVIEW A TRUST-ROOT CHANGE. The 2026-07-10 unfreeze (decisions-log:468, the operator's
#      go-ahead to unfreeze) was made on the explicit premise that "each peer's own Sentinel-code
#      ASKs the operator per-Core (the human recursion-breaker)". A SessionStart hook cannot invoke Sentinel — it is not a tool call
#      and there is no agent in the loop — so that safeguard exists on the manual path and nowhere
#      else. The premise was sound and the mechanism could not carry it.
#   3. RUN ANYTHING NEEDING A MODEL. The ask-distillation step is the standing example: 76% of
#      recorded corrections sat unextracted because nothing without an LLM could process them.
#
# THIS POSITION MOVED TWICE ON 2026-08-06 AND THE HISTORY MATTERS, so it is recorded rather than
# tidied away. (1) The script held trust-root files back from the automatic pull. (2) I removed the
# hold, arguing "an un-arrived file and an un-reviewed file are equally un-reviewed". (3) sentinel-code
# BLOCKED that push and refuted the argument in one line — AN UN-ARRIVED FILE CANNOT EXECUTE — and
# named the consequence: a bad guard installed automatically becomes the operative guard immediately,
# so the "we will review it next session" step is adjudicated by the replaced guard itself. Nick was
# shown that argument and chose to hold ONLY the three trust-root files.
#
# So the rule is: everything lands automatically, EXCEPT the three files that decide what this Core is
# allowed to do. Those wait for a deliberate pull, which is gated. Anything else that rsync cannot
# finish — a hook that arrived but is not registered, a migration to run — is WRITTEN DOWN as a pending
# action and worked at the start of the next session, which is the only place in the system where a
# registration or a review can actually happen.
# INTENTIONAL hardcodes — these must stay REPO-RELATIVE. Every entry is joined against BOTH "$TMP" (the
# baseline clone) and "$CORE_DIR", so an absolute constant such as core_paths.HOOK_PRETOOLUSE_GUARD
# cannot serve both trees: it would resolve to this Core and silently stop excluding anything in the
# clone, which is a security regression dressed as a lint fix. Flagged by bin/lint-code-paths.py, and
# that flag blocked core-school's autosave for four days — the lint is right that these are hardcodes
# and wrong that they are drift.
TRUST_ROOT_PATHS=(
  ".claude/hooks/pretooluse-guard.sh"     # the guard itself — relative by design, NOT core_paths.HOOK_PRETOOLUSE_GUARD (absolute; cannot address the clone)
  ".claude/hooks/sentinel-approve.sh"     # mints the approval token
  ".claude/hooks/sentinel-receipt.sh"     # writes the receipt the token requires — the un-forgeable link
  # THE MANIFEST GOVERNS WHAT THE THREE ABOVE ARE WORTH. Added 2026-08-06; core-finance found the hole
  # (bus #685) and this script's own comment at the clone-manifest branch states the mechanism plainly:
  # "any manifest change applies on the same sync, for every fork."
  #
  # Read that together with per_core_keep and the consequence is exact. per_core_keep is NOT a local
  # veto — the pull reads the CLONED manifest, so the baseline supplies the list of what the baseline
  # may not replace. One baseline commit dropping `.claude/agents/sentinel.md` from per_core_keep
  # replaces every Core's Sentinel spec on the very next automatic SessionStart pull, atomically: the
  # rule change and the file replacement ride the same operation, with no second pull to notice it in.
  #
  # Holding the manifest is not a claim that anyone would do that. It is that a file which decides
  # which files are un-replaceable is doing the same job as the guard, and an automatic pull cannot
  # review either. Excluding the guard while letting the list of protected paths through ungated
  # protects the lock and ships the keyring.
  #
  # Strictly conservative: this only ever holds MORE back. A held manifest means the pull keeps using
  # the Core's existing lists, which is the status quo, and /sync pull applies it through sentinel-code
  # (Rule 6 already treats a manifest change as reviewable) with Nick in the loop.
  "bin/sync-manifest.json"                # decides what per_core_keep protects — including the three above
  # THE TRAJECTORY GATE AND ITS TIER-B EVALUATOR, added 2026-08-28. Found by sentinel-code while
  # reviewing an unrelated peer-pull: both files are named CRITICAL trust-root in that agent's own
  # Rule 1, and neither was in this list NOR in per_core_keep — so a --quiet SessionStart pull had
  # ZERO structural hold-back for either, on every seat, since the gate was built.
  #
  # It was inert at the moment it was found — the reviewer ran cmp and both files are byte-identical
  # across life and all four peers, so nothing had ever moved through the hole. That is exactly why
  # it is worth closing now rather than after: the mechanism had no gate, and the only thing standing
  # in for one was that no baseline commit had happened to touch them yet.
  #
  # trajectory-gate.py re-execs from a frozen trusted checkout before grading, and gate_tier_b.py is
  # the evaluator it runs there. A pull that can replace either can decide what "trusted" means on the
  # next run, which is the same job the three hooks above do — and the same reason the manifest sits
  # in this list. Holding them back only ever means the Core keeps its existing copy; /sync pull
  # applies a real change through sentinel-code with Nick in the loop.
  "bin/trajectory-gate.py"                # re-execs from the frozen checkout; decides what trusted means
  "bin/gate_tier_b.py"                    # the evaluator trajectory-gate runs inside that checkout
)
TRUST_PENDING=""
if [[ "$MODE" == "quiet" ]]; then
  for trp in "${TRUST_ROOT_PATHS[@]}"; do
    # The manifest is in TRUST_ROOT_PATHS for its EXCLUSION only — MANIFEST_PENDING above already
    # detected and reports it. sentinel-code caught the double-book (2026-08-06): both fire on the
    # identical condition (quiet && differs), so a manifest-only change queued apply_trust_root AND
    # apply_manifest and printed two overlapping notices for one file. Not contradictory — both said
    # "held, resolve via /sync pull" — but it is two mechanisms reporting one fact, which is the
    # accretion this session has spent its time removing. One owner: the exclusion is TRUST_ROOT_PATHS,
    # the reporting is MANIFEST_PENDING, and the more specific notice is the one worth keeping because
    # it explains what the manifest actually governs.
    [[ "$trp" == "bin/sync-manifest.json" ]] && continue
    if [[ -f "$TMP/$trp" ]] && ! cmp -s "$TMP/$trp" "$CORE_DIR/$trp" 2>/dev/null; then
      TRUST_PENDING+="$trp"$'\n'
    fi
  done
fi

# ── DIRTY-SHARED-PATH GUARD ─────────────────────────────────────────────────────────────────────
# Codex HIGH, 2026-09-01, verified against the code below: the rsync at the apply branch (and the
# `cp` in the shared-files loop further down) compare the freshly-cloned BASELINE against this
# Core's disk. Neither ever asks whether this Core's OWN copy already differs from this Core's OWN
# git history. To rsync, a file Nick is mid-editing and a file that merely fell behind the baseline
# look identical — both simply "differ from $src" — so both get overwritten the same way: no dirty
# check, no warning, no record. That is the defect exactly as found; there was no mitigating check
# anywhere else in this script.
#
# MEASURED THE SAME NIGHT (before touching anything): business, school and finance were each sitting
# on ~19-20 shared paths with real uncommitted diffs against their own HEAD (up to 700+ changed
# lines per Core) — every one of them destroyed with zero warning by the very next `--quiet`
# SessionStart pull. ops had none. The number the sentinel review quoted earlier that night (three
# files, judged cosmetically equivalent) was a sample, not the exposure — this script had no
# mechanism to see the other sixteen-plus.
#
# CHOSE REFUSE-AND-REPORT over the other two options on the table:
#   - back the local copy up beside it: writes a new untracked file INTO a shared directory on every
#     hold — exactly the drift class the tombstone/orphan passes above exist to clean up, and it
#     stacks a new backup on every subsequent pull if nobody looks.
#   - apply and emit a patch of what was overwritten: the working-tree copy is destroyed FIRST; the
#     patch is a post-mortem, not a save, and only helps if someone reads it before the next edit
#     touches the same file.
#   Refusing changes NOTHING about the file on disk — it is already dirty; leaving it alone is a
#   no-op. Same shape as the `--check --quiet` fix earlier in this file: it cannot silently do the
#   wrong thing in either direction, only stop.
#
# Fail-OPEN when git itself cannot answer (no repo, no `git`): every real Core is a git checkout, so
# this only ever fires in a stripped-down fixture, and refusing to sync AT ALL there is a worse
# failure than the one this guard exists to prevent. The actual protection is the per-path check
# below, not this fallback.
_GIT_OK=0
git -C "$CORE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 && _GIT_OK=1

# A file "differs from the Git index" two ways, and an untracked one is the WORSE case: there is no
# `git checkout` to bring it back once rsync has overwritten it, so a pre-existing untracked file at
# a path the pull wants to write is treated as dirty even though `git diff` has no opinion on it.
# DIRTY MEANS "A HUMAN CHANGED IT", NOT "IT DIFFERS FROM HEAD" (corrected 2026-09-01).
#
# The first cut of this function returned true for anything differing from git HEAD. That is wrong
# here in the most damaging possible direction. A pull APPLIES files to disk; committing them is a
# separate act these Cores mostly do not perform. So the normal resting state of a healthy puller is:
# working tree == the baseline it last synced, git HEAD == something older, and therefore every
# shared file reads as "dirty".
#
# MEASURED on core-business before this correction: bin/core-doctor.sh, brain-health.py and
# compile-truth-refresh.py were byte-identical to baseline 5c97de2 — the exact bytes the PREVIOUS
# pull wrote — while presenting as 1,326 lines of "local work" across 35 paths. Fleet-wide ~62,000
# lines, none of it authored locally. Holding those back would have denied all four peers the fixes
# and pinned them on the stale code: the precise opposite of what a pull is for.
#
# The operator caught it in one line — why let peers pull broken code.
# Codex's original finding had the right predicate and I shipped half of it: it said refuse when the
# path differs from the index *unless the bytes match the previous baseline*, and that second clause
# is the whole difference between a guard and a blockade.
#
# A file is dirty only if it differs from HEAD **and** from the baseline this Core last synced —
# the residue no sync can account for, i.e. a genuine human edit. Without a previous-baseline
# checkout we fail OPEN and treat it as clean, because a puller that cannot prove local authorship
# should receive fixes rather than be frozen on a guess.
#
# An untracked file that is ALSO absent from the previous baseline stays protected: no `git checkout`
# brings it back once rsync has overwritten it.
is_shared_path_dirty() {
  local rel="$1"
  [[ "$_GIT_OK" -eq 1 ]] || return 1
  [[ -e "$CORE_DIR/$rel" ]] || return 1
  if git -C "$CORE_DIR" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
    git -C "$CORE_DIR" diff --quiet HEAD -- "$rel" 2>/dev/null && return 1
  fi
  # FAIL OPEN LIVES HERE, NOT AS PART OF THE CMP BELOW (fixed 2026-09-01, same day it shipped).
  #
  # The first cut folded this into the next `if`'s condition: `[[ -n "$PREV_BASELINE_DIR" &&
  # -f ... ]]`. That reads as "fail open when there's no prior baseline" but does the opposite —
  # when PREV_BASELINE_DIR is empty the whole block is simply skipped, execution falls through to
  # the unconditional `return 0`, and the file is marked DIRTY. That is fail-CLOSED: the exact
  # scenario this predicate exists to fix (a file that only differs from HEAD because a pull wrote
  # it and nobody committed) gets held again, every time PREV_BASELINE_DIR can't be established —
  # missing `.last-baseline-sync`, a `--depth 50` fetch too shallow for how far behind the seat is,
  # or a fresh seat's first pull. Those are not rare; they are exactly the seats furthest behind,
  # i.e. the ones a hold-back does the most damage to. Caught reviewing this same edit, before ANY
  # peer pulled under it — same shape as the bug it was written to fix, one `if` away from shipping.
  #
  # Returning here, before the cmp is even considered, makes "no proof available" go the intended
  # direction: let the pull through. It does not affect case (c) below (untracked, absent from the
  # prior baseline) because that case only arises when PREV_BASELINE_DIR IS set and simply lacks
  # this path — this check has already returned by then.
  [[ -n "${PREV_BASELINE_DIR:-}" ]] || return 1
  if [[ -f "$PREV_BASELINE_DIR/$rel" ]]; then
    cmp -s "$CORE_DIR/$rel" "$PREV_BASELINE_DIR/$rel" && return 1
  fi
  return 0
}

DIRTY_HELD=""
DIRTY_HELD_COUNT=0
DIRTY_WOULD_HOLD=0
DIRTY_LOG="$CORE_DIR/.claude/state/.dirty-shared-holdback.log"
# The durable answer to "what did the last pull overwrite?" for this defect class specifically:
# it never overwrote these, and here is proof plus a timestamp. Append-only, same convention as
# .sync-failures / .learned-activation.log elsewhere in this script. Never called from --check —
# --check writes nothing, full stop (see the guard around PENDING_JSON further down).
record_dirty_hold() {
  local rel="$1"
  DIRTY_HELD+="$rel"$'\n'
  DIRTY_HELD_COUNT=$((DIRTY_HELD_COUNT+1))
  mkdir -p "$(dirname "$DIRTY_LOG")" 2>/dev/null || true
  printf '%s REFUSED overwrite (local edit protected, baseline=%s): %s\n' \
    "$(date -Iseconds 2>/dev/null || date)" "$BASELINE_SHA" "$rel" >> "$DIRTY_LOG" 2>/dev/null || true
}

# Sync each shared dir
while IFS= read -r d; do
  [[ -z "$d" ]] && continue
  src="$TMP/$d/"
  dst="$CORE_DIR/$d/"
  if [[ ! -d "$src" ]]; then continue; fi
  mkdir -p "$dst"

  # Per-dir excludes: pycache always; plus any per_core_keep path nested in $d
  PER_DIR_EXCLUDES=(--exclude __pycache__ --exclude '*.pyc')
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    if [[ "$p" == "$d/"* ]]; then
      rel="${p#$d/}"
      rel="${rel%/\*\*}"
      PER_DIR_EXCLUDES+=(--exclude "$rel")
    fi
  done <<< "$PER_CORE_KEEP"
  # THE THREE TRUST-ROOT FILES ARE HELD BACK ON THE AUTOMATIC PULL. Everything else lands.
  #
  # I argued the opposite earlier on 2026-08-06 — "an un-arrived file and an un-reviewed file are
  # equally un-reviewed" — and sentinel-code refuted it in one line: AN UN-ARRIVED FILE CANNOT
  # EXECUTE. That is the whole difference and I had it backwards.
  #
  # The scenario the exclusion exists for: if a bad pretooluse-guard.sh ever reached the baseline,
  # a land-then-review pull installs it LIVE at session start, it immediately becomes the operative
  # guard for every subsequent tool call, and the "next session will review it" step is then
  # adjudicated BY THE GUARD THAT WAS JUST REPLACED. It marks its own homework. That is precisely the
  # recursion sentinel-code's Rule 1 human-confirmation exists to break, and it slips in through THIS
  # file — which is not itself a trust-root path, so Rule 1's own STEP 0 does not catch it.
  #
  # Nick's call, having been shown that argument (option 2 of three): hold ONLY these three, keep
  # every other shared file on the frictionless auto-update he built this for. Guard changes are rare;
  # the cost is one deliberate `/sync pull` per Core when one happens, and that pull routes through
  # pretooluse-guard -> sentinel-code -> his confirmation, which is the review the July unfreeze
  # assumed would happen per-Core and that a hook can never perform.
  if [[ "$MODE" == "quiet" ]]; then
    for trp in "${TRUST_ROOT_PATHS[@]}"; do
      if [[ "$trp" == "$d/"* ]]; then
        PER_DIR_EXCLUDES+=(--exclude "${trp#$d/}")
      fi
    done
  fi

  # Computed ONCE, ahead of both branches below, so --check and the real apply can never disagree
  # about which files are at risk — the same discipline as PER_DIR_EXCLUDES just above.
  DIFF_ITEMS=$(rsync -a --checksum --dry-run --itemize-changes "${PER_DIR_EXCLUDES[@]}" "$src" "$dst" 2>/dev/null | grep -vE '^\.[df]\.\.t\.*[[:space:]]')
  # NOT `()`. macOS ships bash 3.2.57 (last GPLv2 release, still the default `/bin/bash` on every
  # Mac this fleet runs on) and under `set -u` that version throws "unbound variable" on
  # `"${arr[@]}"` when the array has ZERO elements — fixed only in bash 4.4+. The common case for
  # this array IS zero elements (most directories have no dirty conflict), so `DIR_DIRTY_EXCLUDES=()`
  # would have broken the apply branch on every ordinary pull, on every Mac, the first time it ran.
  # Caught by test_trust_root_pull_hold.sh, an UNRELATED test, which is its own lesson: this exact
  # bug is invisible to any test that only exercises the dirty-path case, because that case always
  # has at least one element and never triggers it.
  DIR_DIRTY_EXCLUDES=(--exclude '.dirty-guard-placeholder-never-matches-anything')
  DIR_DIRTY_LIST=""
  while IFS= read -r item; do
    [[ -z "$item" ]] && continue
    relf="${item#* }"
    [[ "$relf" == */ ]] && continue   # directory-creation entries — nothing on disk to clobber
    fullrel="$d/$relf"
    if is_shared_path_dirty "$fullrel"; then
      DIR_DIRTY_LIST+="$fullrel"$'\n'
      DIR_DIRTY_EXCLUDES+=(--exclude "$relf")
    fi
  done <<< "$DIFF_ITEMS"

  if [[ "$MODE" == "check" ]]; then
    # NO `head` HERE. This output is a REVIEW ARTIFACT, not a status line.
    #
    # It was `| head -30`, PER DIRECTORY. A pull touching 31+ files in one shared dir showed
    # 30 and silently dropped the rest, with nothing indicating truncation — so the reviewer
    # (human or sentinel-code) formed a verdict on a partial picture and could not tell.
    # Found by core-business on a pull-test, 2026-07-27. It affects every sync review either
    # Core has ever run, including reviews of trust-root changes.
    #
    # The count at the apply branch below was never capped (it pipes to wc -l with no head),
    # so "N files changed" was always right while the LIST under it could be short. A number
    # that disagrees with the list beneath it is worse than either alone.
    #
    # Full list now, with an explicit tail marker when it is long, so length is a visible
    # property of the change rather than a silent property of the tool.
    if [[ -n "$DIFF_ITEMS" ]]; then
      DIFF_N=$(printf '%s\n' "$DIFF_ITEMS" | wc -l | tr -d ' ')
      echo "[check] $d would change ($DIFF_N):"; printf '%s\n' "$DIFF_ITEMS" | sed 's/^/    /'
      CHANGED=$((CHANGED+DIFF_N))
    fi
    if [[ -n "$DIR_DIRTY_LIST" ]]; then
      while IFS= read -r hp; do
        [[ -z "$hp" ]] && continue
        echo "[check] $hp — REFUSED: local edit differs from git HEAD; a real pull would NOT overwrite it"
        DIRTY_WOULD_HOLD=$((DIRTY_WOULD_HOLD+1))
      done <<< "$DIR_DIRTY_LIST"
    fi
  else
    BEFORE=$(printf '%s\n' "$DIFF_ITEMS" | grep -c . || true)
    rsync -a "${PER_DIR_EXCLUDES[@]}" "${DIR_DIRTY_EXCLUDES[@]}" "$src" "$dst"
    # 2026-08-24: BOTH branches used to add 1 per DIRECTORY while the shared.files loop below adds 1
    # per FILE — so `changed=N` mixed two units, which is exactly why it looked plausible. core-ops
    # measured its own pull: logged changed=3, three directories hit, 25 files actually written. Four
    # seats then read their own pull output as their own uncommitted work. BEFORE/DIFF_N were already
    # computed here (with --checksum, pure-mtime rows filtered) and thrown away after a boolean test.
    #
    # 2026-09-01: BEFORE now excludes nothing that was subsequently held back for being dirty — a
    # file added to DIR_DIRTY_EXCLUDES was never actually written by the `rsync -a` line above it, so
    # counting it into CHANGED would report a disk write that did not happen. DIRTY_N is subtracted
    # for that reason, not merely to have a number for the notice below.
    DIRTY_N=$(printf '%s\n' "$DIR_DIRTY_LIST" | grep -c . || true)
    CHANGED=$((CHANGED+BEFORE-DIRTY_N))
    while IFS= read -r hp; do
      [[ -z "$hp" ]] && continue
      record_dirty_hold "$hp"
    done <<< "$DIR_DIRTY_LIST"
  fi
done < <(jq -r '.shared.dirs[]' "$MANIFEST")

# Sync each shared file + collect for drift check
SHARED_FILES_LIST=$(jq -r '.shared.files[]' "$MANIFEST")
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  src="$TMP/$f"
  dst="$CORE_DIR/$f"
  if [[ ! -f "$src" ]]; then continue; fi
  mkdir -p "$(dirname "$dst")"
  if [[ "$MODE" == "check" ]]; then
    if ! cmp -s "$src" "$dst" 2>/dev/null; then
      if is_shared_path_dirty "$f"; then
        echo "[check] $f — REFUSED: local edit differs from git HEAD; a real pull would NOT overwrite it"
        DIRTY_WOULD_HOLD=$((DIRTY_WOULD_HOLD+1))
      else
        echo "[check] $f would update"; CHANGED=$((CHANGED+1))
      fi
    fi
  else
    if ! cmp -s "$src" "$dst" 2>/dev/null; then
      # Same dirty-shared-path guard as the directory loop above — see its comment block for why
      # refuse-and-report was chosen over backup-beside-it or apply-then-patch.
      if is_shared_path_dirty "$f"; then
        record_dirty_hold "$f"
      else
        cp "$src" "$dst"; CHANGED=$((CHANGED+1))
      fi
    fi
  fi
done <<< "$SHARED_FILES_LIST"

# Drift check: .claude/commands/ files NOT in shared.files AND NOT in this Core's per_core_extras
CORE_SLUG=$(basename "$CORE_DIR" | sed 's/^core-//')
EXTRAS_LIST=$(jq -r --arg slug "$CORE_SLUG" '.per_core_extras[$slug] // [] | .[]' "$MANIFEST")
if [[ -d "$CORE_DIR/.claude/commands" ]]; then
  for f in "$CORE_DIR/.claude/commands"/*.md; do
    [[ -e "$f" ]] || continue
    cmd=".claude/commands/$(basename "$f")"
    if ! grep -Fxq "$cmd" <<< "$SHARED_FILES_LIST" && ! grep -Fxq "$cmd" <<< "$EXTRAS_LIST"; then
      DRIFT_WARNINGS="$DRIFT_WARNINGS$cmd"$'\n'
    fi
  done
fi

DRIFT_COUNT=$(printf '%s' "$DRIFT_WARNINGS" | grep -c . || true)

# ── Tombstone pass — the ONLY way a baseline DELETION reaches a puller ────────────────────────
#
# WHY (core-finance, 2026-08-13, measured on four seats). The rsync above is `rsync -a` with NO
# --delete: additive by design. The orphan-cleanup pass below is bounded to scheduling/ and is
# driven by THIS MANIFEST's shared.dirs, never by a baseline deletion. So `git rm` on the baseline
# removes a file THERE AND NOWHERE ELSE, and pulling a thousand times will not remove it. Six
# retired files were still on 3-4 peer seats when this was written, the oldest retired 2026-05-15.
#
# bin/retire-legacy.py moves retirements into scheduling/_archive/, which is not a shared dir, so
# the archive never arrives either: on the writer the file is gone AND archived; on a puller it is
# simply still there.
#
# THE HALF WITH TEETH is documentation, not code. .claude/rules/privacy.md stated the
# close-reconciler dir-form spec was "removed" — true on the writer, FALSE on three seats where it
# still sits beside the flat form carrying the OLD output contract, in the very file that warns
# readers not to cite a dir-form spec. And privacy.md's own prescribed remedy — verify "against a
# fresh baseline clone" — CANNOT catch it: a fresh clone shows the file removed while three seats
# still have it. The check that works is a fresh PULLER, or this pass.
#
# A LIST AND NOT `rsync --delete`. --delete removes anything in the destination absent from the
# source, so a single imperfect exclude takes a peer's per_core_keep data with it — on four seats
# at once, unattended, with no review. A tombstone can only ever remove exactly what is named.
# Entries are safe to keep forever: once every seat has pulled, each is a no-op.
TOMB_REMOVED=0
while IFS= read -r tomb; do
  [[ -z "$tomb" ]] && continue
  # Refuse anything that could escape the Core, and anything the manifest also calls per_core_keep
  # — a path in both lists is a manifest bug, and deleting a peer's own data on that basis is the
  # one outcome worse than the stale file this pass exists to remove.
  case "$tomb" in
    /*|*..*) log "[sync-from-baseline] tombstone REFUSED (not Core-relative): $tomb"; continue ;;
  esac
  # GLOB-AWARE, AND IT WAS NOT (core-finance, 2026-08-13, hours after this shipped).
  #
  # This read `select(. == $p)` — an EXACT STRING MATCH against a list that is 15 of 31 GLOB
  # PATTERNS. It refused the literal string `secrets/**`, which nobody would ever tombstone, and
  # did NOT refuse `secrets/keys.json`, which is the file that pattern exists to protect. Same for
  # memory/**, sessions/**, and BOTH sentinel trust-root specs.
  #
  # Exposure when found was ZERO — none of the six tombstones falls under a glob — and that is
  # precisely why it had to be fixed before it mattered. This guard exists so the NEXT entry is
  # safe, in a mechanism that deletes files on four seats unattended. A guard whose first real
  # exercise is the entry that needed it has never been tested at all.
  #
  # Strip a trailing /** and match the path itself or any child of it.
  if jq -e --arg p "$tomb" '.per_core_keep[]? | (sub("/\\*\\*$";"")) as $k | select($p == $k or ($p | startswith($k + "/")))' "$MANIFEST" >/dev/null 2>&1; then
    log "[sync-from-baseline] tombstone REFUSED (also per_core_keep): $tomb"
    continue
  fi
  [[ ! -e "$CORE_DIR/$tomb" ]] && continue
  if [[ "$MODE" == "check" ]]; then
    echo "[check] retired file would be removed: $tomb"
    TOMB_REMOVED=$((TOMB_REMOVED+1))
  else
    # 2026-08-24: this used to be `if git rm ...; then : ; else rm -f; fi` — and `--ignore-unmatch`
    # makes `git rm` EXIT 0 on a path it does not track. So for an UNTRACKED retired file the `if`
    # succeeded, the `rm -f` fallback never ran, the file survived, and the log line below printed
    # "removed" anyway. core-business reported exactly this and core-ops, core-school, core-finance
    # and life each "disproved" it by testing only the TRACKED case — four refutations of a correct
    # claim, all the same error. No seat tracks any retired path, so every seat was affected.
    # Unconditional rm -f: the tracked path still stages as `D` for commit, the untracked one is
    # actually deleted, and the log line is finally true in both cases.
    git -C "$CORE_DIR" rm -q --ignore-unmatch -- "$tomb" 2>/dev/null || true
    rm -f "$CORE_DIR/$tomb"
    log "[sync-from-baseline] tombstone: removed retired $tomb"
    TOMB_REMOVED=$((TOMB_REMOVED+1))
  fi
done < <(jq -r '.retired[]?' "$MANIFEST" 2>/dev/null)
[[ "$TOMB_REMOVED" -gt 0 && "$MODE" != "quiet" ]] && \
  log "[sync-from-baseline] tombstone pass: $TOMB_REMOVED retired path(s) handled"

# Orphan-cleanup pass — pull-side mirror of sync-to-baseline.sh Layer 2.
# Closes the same asymmetry on the pull direction: when a shared.dir is
# removed from manifest, the rsync (additive only) leaves the local copy
# on disk as an orphan. This pass walks SCAN_PARENTS in the local Core and
# `git rm`s any subdir not in current shared.dirs. Bounded to `scheduling/`
# only (same scope as push side). Uses `git rm` not `rm -rf` — deletions
# stage in the local Core's git for the next auto-commit.
SCAN_PARENTS=("scheduling")
CURRENT_SHARED_DIRS=$(jq -r '.shared.dirs[]?' "$MANIFEST" 2>/dev/null)
# Per-Core preserved scheduling dirs — per_core_keep entries of the form
# "scheduling/<name>" or "scheduling/<name>/**". These are legit per-Core
# assets (e.g. business's quarterly-review launchd plist) that share a
# parent with shared dirs but are NOT orphans.
PER_CORE_KEEP_SCAN_DIRS=$(jq -r '.per_core_keep[]?' "$MANIFEST" 2>/dev/null \
  | grep -E '^scheduling/[^/]+(/\*\*)?$' \
  | sed -E 's|/\*\*$||')
ORPHAN_REMOVED=0
for parent in "${SCAN_PARENTS[@]}"; do
  [[ ! -d "$CORE_DIR/$parent" ]] && continue
  for entry in "$CORE_DIR/$parent"/*/; do
    [[ ! -d "$entry" ]] && continue
    relpath="${entry#$CORE_DIR/}"
    relpath="${relpath%/}"
    if grep -qFx "$relpath" <<< "$CURRENT_SHARED_DIRS"; then
      continue
    fi
    if grep -qFx "$relpath" <<< "$PER_CORE_KEEP_SCAN_DIRS"; then
      [[ "$MODE" != "quiet" ]] && log "[sync-from-baseline] orphan-cleanup: preserving $relpath (per_core_keep)"
      continue
    fi
    if [[ "$MODE" == "check" ]]; then
      echo "[check] orphan dir would be removed: $relpath (not in manifest shared.dirs)"
      ORPHAN_REMOVED=$((ORPHAN_REMOVED+1))
    else
      # Try git rm first (tracked); fall back to nothing if untracked
      if (cd "$CORE_DIR" && git rm -r --quiet "$relpath" 2>/dev/null); then
        log "[sync-from-baseline] orphan-cleanup: git-removed $relpath (staged for next commit)"
        ORPHAN_REMOVED=$((ORPHAN_REMOVED+1))
      else
        log "[sync-from-baseline] orphan-cleanup: $relpath untracked or git-rm failed — left in place (manual rm needed)"
      fi
    fi
  done
done

# Self-register this Core's hook set into settings.json. settings.json is per_core_keep, so
# rsync cannot carry hook REGISTRATION across Cores/forks — this is the transport (one
# `/sync pull` wires a fork). Atomic + fail-safe: settings.json is untouched on any error.
#
# 2026-07-27 CONSOLIDATION. There used to be TWO registries: bin/shared-hooks.json (applied
# here automatically, 15 hooks, additive-only) and bin/hook-registry.json (30 hooks, role- and
# override-aware, add AND remove) — which nothing ever invoked. So 13 hooks silently never
# propagated, including friction-dispatch (the SI injector) and shared-write-guard (the
# security gate that stops a pull-only Core editing shared code). Measured drift on 2026-07-27:
# business/school/finance were each missing 6-7 managed hooks. That gap IS the recurring
# "automate baseline sync so pull-only Cores don't need manual follow-up" ask.
#
# hook-registry.json is a strict superset (it carries sync-from-baseline and shared-write-guard
# at scope=puller, honors identity.json overrides, and never touches sentinel-flagged hooks),
# so it is now the single source of truth and reconcile-hooks.py is the single installer.
RECONCILE="$CORE_DIR/bin/reconcile-hooks.py"
if [[ -f "$RECONCILE" ]]; then
  # `|| true` ON BOTH BRANCHES WAS THE BUG (Codex CRITICAL, :667,670,672) — UNCONDITIONALLY, in
  # every mode, with no stderr write on the failure path at all. A reconcile-hooks crash (bad
  # settings.json, a python traceback, a missing dep) reported NOTHING: not a warning, not a
  # .sync-failures line, nothing — a Core could run this pull a hundred times with hook
  # registration silently broken every single time. Capture the real exit code and act on it.
  if [[ "$MODE" == "check" ]]; then
    RH_OUT=$(python3 "$RECONCILE" --core "$CORE_DIR" --check 2>&1); RH_RC=$?
    [[ -n "$RH_OUT" ]] && echo "$RH_OUT"
    # rc==1 IS NOT A FAILURE HERE — reconcile-hooks.py's own docstring contract for --check is
    # "exit 0 if clean / 1 if drift" (bin/reconcile-hooks.py:19). Drift is the ORDINARY, expected
    # state for a dry-run review (that is the whole point of --check) and is already fully
    # reported via the MISSING/EXTRA lines just printed above. Flagging rc==1 as incomplete would
    # have made every --check with pending hook changes report "FAILED", which is not what
    # Codex's finding was about and would be a false alarm on the common case. Only a code OUTSIDE
    # that documented {0,1} contract (an uncaught exception before reaching it, a bad --registry
    # path, etc.) is a genuine reconcile-hooks malfunction worth failing loudly over.
    if [[ "$RH_RC" -gt 1 ]]; then
      echo "[sync-from-baseline] ERROR: reconcile-hooks --check failed (rc=$RH_RC) — see output above." >&2
      mark_incomplete "reconcile-hooks --check failed rc=$RH_RC"
    fi
  else
    RH_OUT=$(python3 "$RECONCILE" --core "$CORE_DIR" --apply 2>&1); RH_RC=$?
    [[ -n "$RH_OUT" && "$MODE" != "quiet" ]] && echo "$RH_OUT"
    if [[ "$RH_RC" -ne 0 ]]; then
      echo "[sync-from-baseline] ERROR: reconcile-hooks --apply failed (rc=$RH_RC):" >&2
      printf '%s\n' "$RH_OUT" | sed 's/^/    /' >&2
      mark_incomplete "reconcile-hooks --apply failed rc=$RH_RC"
    fi
  fi
fi

if [[ "$MODE" == "check" ]]; then
  # 2026-08-25: TOMB_REMOVED was counted at :448/:461, logged on its own line at :465, and then
  # left OUT of both the total and this breakdown — so a run with 5 pending tombstone deletions
  # printed "0 items would change (0 rsync + 0 orphan-removals)" directly under a line saying
  # "tombstone pass: 5 retired path(s) handled". Same defect class as the directories-vs-files
  # counter fixed the day before, in the same function: a real deletion category absent from the
  # headline number. Found by reading school's post-pull --check, not reported by anything.
  TOTAL=$((CHANGED + ORPHAN_REMOVED + TOMB_REMOVED))
  echo "[sync-from-baseline] check complete. $TOTAL items would change ($CHANGED rsync + $ORPHAN_REMOVED orphan-removals + $TOMB_REMOVED tombstone-removals)."
  # DIRTY_WOULD_HOLD is deliberately NOT folded into TOTAL — these are the paths a real pull would
  # REFUSE to touch (dirty-shared-path guard, above), so they are the opposite of a pending change.
  # Reported on its own line for the same reason tombstones got one on 2026-08-25: a real category
  # silently absent from the headline number is worse than either number alone.
  if [[ "$DIRTY_WOULD_HOLD" -gt 0 ]]; then
    echo "[sync-from-baseline] check: $DIRTY_WOULD_HOLD shared path(s) above are locally dirty — a real pull would HOLD them, not overwrite them."
  fi
  if [[ "$DRIFT_COUNT" -gt 0 ]]; then
    echo "[sync-from-baseline] drift warnings (local-only commands not in manifest):"
    printf '%s' "$DRIFT_WARNINGS" | sed 's/^/    /'
  fi
  # --check is a manual/reviewer path, not the fail-soft SessionStart one — a reconcile-hooks
  # failure above (SYNC_INCOMPLETE) must not be reported as a clean dry run.
  if [[ "$SYNC_INCOMPLETE" -eq 1 ]]; then
    echo "[sync-from-baseline] check FAILED — a required step errored (see above); this is NOT a clean dry run." >&2
    exit 1
  fi
  exit 0
fi

# Activate the learned-workflow layer when its files are present but not yet wired.
# settings.json is per_core_keep + the DB schema/starter contracts can't propagate via
# rsync — this idempotent installer is the transport. Runs once per fork (on the pull
# that first brings the layer in), no-ops thereafter. Fail-open: never blocks a pull.
LEARNED_INSTALL="$CORE_DIR/bin/install-learned-layer.sh"
# A CUT-OVER SEAT REMOVED THE CLASSIFIER ON PURPOSE (2026-08-28, found by core-finance on the
# 902e805 verification pass — the exact "check my work" ask that pull request came back to).
#
# The gate below was `! grep -q learned-classifier settings.json` alone, which cannot tell
# "this layer was never installed" from "this seat deliberately removed it". On a post-cutover
# seat the classifier is absent BY DESIGN — identity.json overrides it to "off" — so the gate
# opened and the installer put it back. Only the ECHO was gated on MODE != quiet, so the
# SessionStart --quiet auto-pull did this SILENTLY on every session open.
#
# finance hit it live pulling a50d016: reconcile reported "+0 -0, re-verified clean", then the
# activation ran and left the seat at 42 desired / 45 present — EXTRA learned-classifier,
# learned-recallguard, learned-stopguard. Repaired with --apply (+0 -3).
#
# AND THE VERIFICATION BELOW MADE IT WORSE, not better, on exactly these seats: its SUCCESS
# condition is "classifier wired" (the state the cutover exists to remove) and its WARN
# condition is "classifier absent" (the seat being CORRECT). A correctly cut-over seat got a
# stderr WARN and a .sync-failures line for being right. That hardening is correct for a fresh
# fork and inverted for a cut-over seat; both are true, which is why the MARKER — not the
# symptom — has to be what decides.
#
# MEASURED EXPOSURE at the time of the fix: life, business, school AND finance all carry
# .si-unified-spine with the classifier correctly absent, so all four would have been re-wired
# on their next pull. (finance's own report listed business as unexposed; business cut over
# after that message was written.) ops is pre-cutover and legitimately has it registered — a
# seat with no marker is unaffected by this line, so ops and every fresh fork activate exactly
# as before.
if [[ -f "$LEARNED_INSTALL" ]] \
   && [[ ! -f "$CORE_DIR/.claude/state/.si-unified-spine" ]] \
   && ! grep -q "learned-classifier" "$CORE_DIR/.claude/settings.json" 2>/dev/null; then
  [[ "$MODE" != "quiet" ]] && echo "[sync-from-baseline] activating learned-workflow layer (one-time)…"
  # Do NOT swallow the installer silently. The old `>/dev/null 2>&1 || true` meant a
  # half-failed activation (DB unreachable, wrong COREBRAIN_DB, missing dep) left a
  # files-but-no-layer Core with ZERO signal — the exact invisible-degradation class
  # forks keep hitting. Capture full output to a marker, and verify the classifier
  # actually got wired (install-learned-layer.sh is fail-soft per-step, so a 0 exit
  # is NOT proof). WARN visibly (stderr, survives --quiet) on any real failure.
  ACT_LOG="$CORE_DIR/.claude/state/.learned-activation.log"
  mkdir -p "$(dirname "$ACT_LOG")" 2>/dev/null || true
  bash "$LEARNED_INSTALL" >"$ACT_LOG" 2>&1
  if grep -q "learned-classifier" "$CORE_DIR/.claude/settings.json" 2>/dev/null; then
    printf '%s OK learned-layer activated (classifier wired)\n' "$(date -Iseconds 2>/dev/null || date)" >> "$ACT_LOG"
    [[ "$MODE" != "quiet" ]] && echo "[sync-from-baseline] learned-workflow layer activated."
  else
    echo "[sync-from-baseline] WARN: learned-layer activation ran but classifier is NOT wired — see $ACT_LOG" >&2
    printf '%s sync-from-baseline learned-activation-failed (classifier not wired)\n' "$(date -Iseconds 2>/dev/null || date)" \
      >> "$CORE_DIR/.claude/state/.sync-failures" 2>/dev/null || true
  fi
fi

# Apply any pending brain-pg migrations that arrived in this pull, so a fork's DB
# (peers included) matches the code it just received — the missing step that broke
# an external fork historically (2026-07-07). Idempotent: the schema_migrations tracker
# skips already-applied files.
MIG_RUNNER="$CORE_DIR/bin/run-migrations.sh"
if [[ -f "$MIG_RUNNER" ]] && command -v psql >/dev/null 2>&1; then
  # THE DB SELECTED AT INSTALL TIME, NOT THE PROCESS DEFAULT (Codex CRITICAL, :751). run-migrations.sh
  # resolves its target with `DB="${COREBRAIN_DB:-corebrain}"` — correct ONLY when COREBRAIN_DB is
  # already an exported var in ITS process. This script is invoked from SessionStart, a hook
  # subprocess, not an interactive login shell — the exact "third path" scheduling/brain-pg/_env.py's
  # own docstring names for embed.py's $VOYAGE_API_KEY ("bash subprocesses that never see zshenv").
  # Same defect shape here: `setup-brain.sh` documents `COREBRAIN_DB=foo bash bin/setup-brain.sh` as
  # how a Core selects a non-default DB, but nothing PERSISTS that choice — it lived only in the
  # shell that ran setup. Every later unattended pull ran migrations against literal 'corebrain'
  # regardless of what was actually provisioned: silent when they happen to agree (the common case),
  # silently WRONG — against a DB that is not this Core's, or does not exist — when they don't.
  #
  # Matches _env.py's own convention rather than inventing a second one: `load_secrets()` reads
  # `~/.claude/secrets.env` (the canonical on-disk store, mirrored into interactive shells by
  # ~/.zshenv) and fills ONLY gaps, never overriding an already-exported value. run-migrations.sh has
  # no Python in its path, so this reads the same file the bash way. A Core that never customized
  # COREBRAIN_DB has no such line and this is a no-op — the common case is unaffected.
  # NOTE: another pass is fixing this same class in scheduling/brain-pg/_env.py itself (Python
  # callers of connect_corebrain()); this is the bash-side equivalent, scoped to this script only.
  if [[ -z "${COREBRAIN_DB:-}" && -f "$HOME/.claude/secrets.env" ]]; then
    _installed_db=$(grep -E '^[[:space:]]*(export[[:space:]]+)?COREBRAIN_DB=' "$HOME/.claude/secrets.env" 2>/dev/null \
      | tail -n 1 | sed -E 's/^[[:space:]]*(export[[:space:]]+)?COREBRAIN_DB=//' \
      | sed -E "s/^[\"']|[\"']\$//g" | sed 's/[[:space:]]*#.*$//')
    [[ -n "$_installed_db" ]] && export COREBRAIN_DB="$_installed_db"
  fi
  MIG_LOG="$CORE_DIR/.claude/state/.migrations.log"
  mkdir -p "$(dirname "$MIG_LOG")" 2>/dev/null || true
  if bash "$MIG_RUNNER" >>"$MIG_LOG" 2>&1; then
    [[ "$MODE" != "quiet" ]] && echo "[sync-from-baseline] brain-pg migrations up to date (idempotent, DB=${COREBRAIN_DB:-corebrain})."
  else
    # MEDIUM (Codex, :747,758-760): this used to WARN and move on with NOTHING downstream aware of
    # it — the baseline SHA got recorded as synced a few lines below regardless, so
    # ".last-baseline-sync" read "baseline=$SHA changed=N" identically whether the migration ran
    # clean or failed outright. mark_incomplete surfaces this the same way as before (stderr WARN +
    # .sync-failures line) AND stops that record from claiming this Core is fully caught up; a
    # manual/`--check` caller now also gets a non-zero exit for it (see the end of this script).
    # Still fail-SOFT in the sense that matters most: it does not abort the sync outright, so
    # rsync'd code and other independent steps below still get their chance.
    echo "[sync-from-baseline] WARN: brain-pg migrations failed (DB=${COREBRAIN_DB:-corebrain}) — see $MIG_LOG" >&2
    mark_incomplete "brain-pg migrations failed (DB=${COREBRAIN_DB:-corebrain}), see $MIG_LOG"
  fi
fi

# Record sync
mkdir -p "$(dirname "$SYNC_LOG")"
# 2026-08-25: tombstones added. This line is the ONLY durable record of what a pull did — every
# seat's "what changed on my disk" question is answered from here — and it silently omitted the
# tombstone deletions entirely. A seat reading its own sync history could not see that a pull had
# removed files at all.
#
# NOT WRITTEN WHEN SYNC_INCOMPLETE (Codex MEDIUM, :747,758-760, and the same shape as the fetch
# and reconcile fixes above). $SYNC_LOG is the ONLY thing state-feeder, audit-gap-check, and
# `--ref`'s forward-only descendant check trust as "this Core is confirmed AT $BASELINE_SHA".
# Stamping it after hook reconciliation or a migration failed would make the ledger claim
# synchronized while the Core silently runs unregistered hooks or an un-migrated DB — precisely
# the "marker lying is worse than the failure" defect this whole pass exists to close. The failure
# itself is already durable in .sync-failures via mark_incomplete(); this just declines to ALSO
# write a success record over it. Nothing already landed (rsync ran earlier in this script) is
# rolled back, and every step here is idempotent, so the next pull retries only the part that did
# not finish — this Core is behind by exactly the steps that failed, never by more, and never by
# a false "current" reading.
if [[ "$SYNC_INCOMPLETE" -eq 0 ]]; then
  # dirty_held added 2026-09-01 alongside the dirty-shared-path guard above, same reasoning as the
  # tombstones field added 2026-08-25: this line is the ONLY durable per-pull ledger, so a category
  # of "what this pull did NOT do to your disk" belongs in it as much as what it did.
  printf '%s\n' "$(date -Iseconds) baseline=$BASELINE_SHA changed=$CHANGED orphans=$ORPHAN_REMOVED tombstones=$TOMB_REMOVED dirty_held=$DIRTY_HELD_COUNT" >> "$SYNC_LOG"
  log "[sync-from-baseline] done. $CHANGED items updated. SHA=$BASELINE_SHA"
  if [[ "$DIRTY_HELD_COUNT" -gt 0 ]]; then
    # Unconditional stderr, not log() — this must survive --quiet the same way the clone-failure
    # and trust-root notices elsewhere in this script do. A held local edit is exactly the kind of
    # thing a --quiet SessionStart pull must never let scroll past unseen.
    {
      echo "[sync-from-baseline] $DIRTY_HELD_COUNT shared path(s) HELD (local edits protected, not overwritten):"
      printf '%s' "$DIRTY_HELD" | sed 's/^/    /'
      echo "  See $DIRTY_LOG for the durable record."
    } >&2
  fi
else
  echo "[sync-from-baseline] INCOMPLETE — baseline $BASELINE_SHA NOT recorded as synced (required step failed):" >&2
  printf '%s' "$SYNC_INCOMPLETE_REASONS" | sed 's/^/    - /' >&2
  echo "  See $CORE_DIR/.claude/state/.sync-failures for the durable record." >&2
fi
if [[ "$DRIFT_COUNT" -gt 0 && "$MODE" != "quiet" ]]; then
  echo "[sync-from-baseline] drift: $DRIFT_COUNT local-only command file(s):"
  printf '%s' "$DRIFT_WARNINGS" | sed 's/^/    /'
fi

# ── PENDING ACTIONS: what this pull DELIVERED but could not FINISH ─────────────────────────────
#
# Everything landed. This records the parts rsync cannot complete, so the next session's first move
# is to work them. Written as a small JSON file rather than only printed, because the SessionStart
# pull happens BEFORE anyone reads anything — a notice on stderr scrolls past before the human or the
# agent is looking, which is why the previous version of this reported into the void.
#
# Fail-soft throughout: a problem building this list must never break the sync that already succeeded.
# NOTE ON THE FAIL-SOFT SHAPE: the first version wrapped this whole block in `{ ... } 2>/dev/null`,
# which swallowed its own `>&2` notices — a fail-soft wrapper that silenced exactly the output the
# block exists to produce. Caught by test_trust_root_pull_hold.sh. Errors are now tolerated per
# risky command instead of by muting the whole section.
# --check WRITES NOTHING, and this guard is why. sentinel-code ASKed the push over it (2026-08-06).
#
# This block was not gated on $MODE, so `--check` — documented at the top of this file as "dry-run;
# report what would change. No writes." — wrote both the pending-actions JSON and, worse, the one-shot
# TRUST_MARKER below. The bootstrap notice fires only when that marker is ABSENT, so the review step
# itself would consume the single warning: I run `--check` to capture the diff FOR sentinel-code, that
# silently sets the marker on the real Core, and the genuine unattended SessionStart pull later finds
# the marker present and prints nothing. The exact "must not pass silently" failure the notice exists
# to close, defeated by the act of reviewing it.
#
# Untested until it was found, and the reason is worth naming: test_trust_root_pull_hold.sh only ever
# called `run_pull --quiet`. `--check` had no coverage at all, so 43/43 and 11/11 both passed over it.
# A mode that writes nothing is a claim, and an unexercised claim is just a comment.
if [[ "$MODE" == "check" ]]; then
  log "--check: no pending-actions written (dry-run writes nothing, including the one-shot marker)"
else
PENDING_JSON="$CORE_DIR/.claude/state/.pull-pending-actions.json"
{
  PEND_ITEMS=""

  # (1) TRUST-ROOT CHANGE HELD. The file did NOT land — see the exclusion above. What is owed is a
  #     deliberate `/sync pull`, which routes through pretooluse-guard -> sentinel-code -> Nick's
  #     confirmation. That is the per-Core review the 2026-07-10 unfreeze assumed would happen and
  #     that a SessionStart hook structurally cannot perform: it is not a tool call and there is no
  #     agent in the loop to invoke a reviewer.
  if [[ -n "$TRUST_PENDING" ]]; then
    while IFS= read -r trp; do
      [[ -z "$trp" ]] && continue
      PEND_ITEMS+="{\"action\":\"apply_trust_root\",\"path\":\"$trp\",\"baseline\":\"$BASELINE_SHA\"},"
    done <<< "$TRUST_PENDING"
    {
      echo ""
      echo "  TRUST-ROOT CHANGE HELD — this file did NOT land, on purpose."
      printf '%s' "$TRUST_PENDING" | sed 's/^/      /'
      echo "  baseline $BASELINE_SHA carries a change to a file that governs what this Core is"
      echo "  ALLOWED to do. An automatic pull is not gated (a hook is not a tool call), and a bad"
      echo "  guard installed automatically would then be the thing judging its own review."
      echo "  To apply it deliberately:  /sync pull   (routes through sentinel-code -> the operator)"
      echo "  Everything else in this pull HAS landed normally."
      echo ""
    } >&2
  fi

  # (1a) MANIFEST HELD. The unattended pull declined to adopt new sync rules it cannot review. Loud,
  #      because the whole failure mode is a rule change nobody notices — and silent-but-correct is
  #      indistinguishable from silent-and-broken to the next reader.
  if [[ -n "$MANIFEST_PENDING" ]]; then
    PEND_ITEMS+="{\"action\":\"apply_manifest\",\"path\":\"$MANIFEST_PENDING\",\"baseline\":\"$BASELINE_SHA\"},"
    {
      echo ""
      echo "  SYNC MANIFEST CHANGED and was NOT adopted by this automatic pull."
      echo "  bin/sync-manifest.json decides which paths per_core_keep protects — including the guard,"
      echo "  sentinel-approve and sentinel-receipt. The pull reads the CLONED manifest, so a change to"
      echo "  that list would take effect on the same sync that delivers it, with no reviewer present."
      echo "  This Core kept its existing lists. Everything else in this pull landed normally."
      echo "  To adopt it deliberately:  /sync pull   (routes through sentinel-code -> the operator)"
      echo ""
    } >&2
  fi

  # (1c) DIRTY SHARED PATHS HELD — the data-loss defect this section of the file exists to close
  #      (Codex HIGH, 2026-09-01; see the dirty-shared-path guard above the directory-sync loop for
  #      the full mechanism and why refuse-and-report was chosen). Reported here too, not only via
  #      the stderr notice near the sync-log stamp, so a Core reading .pull-pending-actions.json
  #      programmatically (the same file hook-registration and trust-root holds already write into)
  #      sees this without having to scrape stderr.
  if [[ -n "$DIRTY_HELD" ]]; then
    while IFS= read -r dhp; do
      [[ -z "$dhp" ]] && continue
      PEND_ITEMS+="{\"action\":\"resolve_dirty_shared_hold\",\"path\":\"$dhp\",\"baseline\":\"$BASELINE_SHA\"},"
    done <<< "$DIRTY_HELD"
  fi

  # (1b) THE BOOTSTRAP HOLE, reported because it cannot be prevented.
  #
  #      core-finance refused this pull (bus #600) and was right about why. THE HOLD-BACK LOGIC SHIPS
  #      INSIDE THE PULL THAT CARRIES IT. A peer still running the pre-hold-back script does its
  #      automatic SessionStart pull with THAT script — which has no trust-root exclusion at all — so
  #      the guard lands ungated exactly once, and prints nothing while doing it. Verified rather than
  #      argued: on baseline 686e34c, the commit the peers are actually installed from, grepping the
  #      old script for the hold-back notice returns 0.
  #
  #      So on a first run the ABSENCE of the notice above is ambiguous: it means either "no trust-root
  #      change in this pull" or "the exposure already happened, silently, one pull ago". I had told the
  #      peers on the bus that seeing the notice was the correct signal, which invited exactly the wrong
  #      reading — a puller who sees nothing concludes the fix worked.
  #
  #      It cannot be fixed retroactively; code that already ran, ran. What it can do is refuse to pass
  #      silently. On the first pull under hold-back this says so, and checks the one thing that
  #      separates the two cases: whether the local trust-root files already match the baseline.
  #      Matching means they arrived; differing means they are being held now.
  TRUST_MARKER="$CORE_DIR/.claude/state/.trust-root-holdback-active"
  if [[ ! -f "$TRUST_MARKER" ]]; then
    _ALREADY=""
    for trp in "${TRUST_ROOT_PATHS[@]}"; do
      # $TMP is the baseline clone (line 55, removed by the EXIT trap). I first wrote $CLONE here from
      # memory; it does not exist, so `[[ -f "$CLONE/$trp" ]]` was false for every file and the notice
      # would have reported "none arrived ungated" unconditionally — the flattering answer, printed with
      # total confidence, on the one check whose entire job is to tell a peer it may already be exposed.
      if [[ -f "$CORE_DIR/$trp" && -f "$TMP/$trp" ]] && cmp -s "$CORE_DIR/$trp" "$TMP/$trp"; then
        _ALREADY+="      $trp"$'\n'
      fi
    done
    {
      echo ""
      echo "  FIRST PULL UNDER TRUST-ROOT HOLD-BACK on this Core."
      echo "  Any trust-root change delivered by an EARLIER automatic pull arrived UNGATED and printed"
      echo "  no warning — the hold-back logic ships inside the pull that carries it, so it could not"
      echo "  protect the pull that installed it. One-time property, per Core."
      if [[ -n "$_ALREADY" ]]; then
        echo "  These already MATCH the baseline byte-for-byte, i.e. they are already installed:"
        printf '%s' "$_ALREADY"
        echo "  Whether that arrived reviewed or ungated is NOT knowable from here. Diff them against a"
        echo "  fresh baseline clone and have sentinel-code read them before trusting the guard."
      else
        echo "  None of the trust-root files currently match the baseline, so none arrived ungated"
        echo "  through this path; whatever is pending is being held now, as intended."
      fi
      echo ""
    } >&2
    PEND_ITEMS+="{\"action\":\"verify_trust_root_bootstrap\",\"baseline\":\"$BASELINE_SHA\"},"
    mkdir -p "$(dirname "$TRUST_MARKER")" 2>/dev/null || true
    printf 'hold-back active since baseline %s\n' "$BASELINE_SHA" > "$TRUST_MARKER" 2>/dev/null || true
  fi

  # (2) HOOK REGISTRATION. .claude/hooks is shared, .claude/settings.json is per_core_keep — so a new
  #     hook file arrives and registers itself NOWHERE. reconcile-hooks already knows how to compare
  #     desired against present, so this asks it rather than reimplementing the comparison.
  #     --apply, NOT --check (2026-08-28). core-finance recommended this in the same message that
  #     reported the activation defect; I fixed the gate and skipped this half, and ninety minutes
  #     later core-business pulled d9af7f9 and watched FOUR retired hooks come back —
  #     learned-classifier, learned-recallguard, learned-stopguard, learned-validator — with this
  #     block printing "HOOK REGISTRATION DRIFT after pull, desired 41 present 45" and repairing
  #     nothing. business had to run reconcile --apply by hand (+0 -4).
  #
  #     WHY A WARNING WAS NEVER ENOUGH HERE, and it is an ordering problem, not a gate problem:
  #     reconcile runs at line 655 with --apply, THEN the learned-layer activation block runs, THEN
  #     this. So the pull prints "+0 -0, applied and re-verified clean" and re-registers hooks
  #     BELOW it. business: "Anyone who reads only the first line concludes the pull was clean."
  #
  #     AND THE MARKER GUARD ABOVE CANNOT SAVE THE PULL THAT DELIVERS IT. sentinel-code caught this
  #     before business hit it: the running bash process is the OLD script, already loaded from the
  #     seat's own disk, and rsync's temp+rename does not retarget an open fd. So the fixed gate
  #     only takes effect on the NEXT pull, and every seat crosses exactly one pull where the old
  #     activation still fires. Repairing after the fact is the only thing that covers that pull.
  #
  #     Safe to apply unattended: reconcile-hooks derives settings.json from bin/hook-registry.json
  #     joined with this seat's own identity.json, so it can only ever converge the file onto what
  #     the seat has already declared. It is the same call line 655 already makes.
  if [[ -f "$CORE_DIR/bin/reconcile-hooks.py" ]] && command -v python3 >/dev/null 2>&1; then
    RH_OUT=$(cd "$CORE_DIR" && python3 bin/reconcile-hooks.py --apply 2>/dev/null | tr '\n' ' ')
    if ! printf '%s' "$RH_OUT" | grep -q "in sync"; then
      PEND_ITEMS+="{\"action\":\"reconcile_hooks\",\"detail\":\"$(printf '%s' "$RH_OUT" | tr -d '"' | cut -c1-220)\"},"
      echo "  HOOK REGISTRATION REPAIRED after pull (drift found and applied):" >&2
      echo "      $RH_OUT" >&2
    fi
  fi

  # (2b) A RETIREMENT MUST ANNOUNCE ITSELF, IN ITS OWN WORDS.
  #
  #      core-school raised the alarm on 2026-08-06 (bus #610) that the pull had "silently
  #      unregistered the three anti-pattern gates". It had. The retirement was deliberate — Nick's
  #      no-post-reply directive, 12 entries tombstoned with written reasons — and school retracted
  #      once it read the registry. But the alarm was the CORRECT response to what it could see, and
  #      that is the defect: reconcile-hooks reported a bare "+3 -10" and left each Core to discover
  #      that ten registrations had gone and guess whether it was policy or breakage. Two Cores read
  #      the same fact in opposite directions within one minute of each other.
  #
  #      Turning off a Core's enforcement layer is not a routine sync line. The tombstones already
  #      carry `retired_reason`; nothing was reading them out. This does, so the reason travels with
  #      the removal instead of sitting in a file nobody thought to open.
  if [[ -f "$CORE_DIR/bin/hook-registry.json" ]] && command -v python3 >/dev/null 2>&1; then
    RET_OUT=$(cd "$CORE_DIR" && python3 -c '
import json, re, sys
try:
    txt = open("bin/hook-registry.json").read()
    reg = json.loads(txt)
except Exception:
    sys.exit(0)
out = []
def walk(o):
    if isinstance(o, dict):
        if o.get("retired"):
            cmd = str(o.get("command") or o.get("script") or "?")
            m = re.search(r"([A-Za-z0-9._-]+\.(?:py|sh))", cmd)
            out.append((m.group(1) if m else cmd[:40],
                        str(o.get("retired_reason") or "no reason recorded")))
        for v in o.values():
            walk(v)
    elif isinstance(o, list):
        for x in o:
            walk(x)
walk(reg)
if not out:
    sys.exit(0)
# Only report the ones this Core still has ON DISK — a tombstone for a file that was never here is
# not news, and padding the notice with those is how a real one gets skimmed past.
import os
live = [(n, r) for n, r in out if os.path.exists(os.path.join(".claude", "hooks", n))]
if not live:
    sys.exit(0)
print(f"{len(live)} RETIRED hook(s) — unregistered ON PURPOSE, files kept on disk:")
seen = set()
for n, r in live:
    if n in seen:
        continue
    seen.add(n)
    print(f"      {n}: {r[:150]}")
' 2>/dev/null)
    if [[ -n "$RET_OUT" ]]; then
      { echo ""; printf '  %s\n' "$RET_OUT"; echo ""; } >&2
    fi
  fi

  if [[ -n "$PEND_ITEMS" ]]; then
    printf '{"pulled_at":"%s","baseline":"%s","items":[%s]}\n' \
      "$(date -Iseconds 2>/dev/null || date)" "$BASELINE_SHA" "${PEND_ITEMS%,}" > "$PENDING_JSON"
  else
    rm -f "$PENDING_JSON" 2>/dev/null || true
  fi
  rm -f "$CORE_DIR/.claude/state/.trust-root-pending" 2>/dev/null || true
} || true
fi   # end: --check writes nothing

# FINAL EXIT. --check already returned earlier (above), so only normal/--quiet reach here.
# --quiet stays exit 0 regardless — SessionStart must never hard-fail a session open — but a
# manual/normal `/sync pull` that hit a fetch/reconcile/migration failure (SYNC_INCOMPLETE, set by
# mark_incomplete() above) is no longer allowed to report success. This is the other half of the
# same fix as the clone-failure exit code: a swallowed failure anywhere in this script must not
# read as a clean exit to whatever called it.
if [[ "$MODE" != "quiet" && "$SYNC_INCOMPLETE" -eq 1 ]]; then
  echo "[sync-from-baseline] FAILED — one or more required steps did not complete:" >&2
  printf '%s' "$SYNC_INCOMPLETE_REASONS" | sed 's/^/    - /' >&2
  echo "  Files that already landed are NOT rolled back. baseline was NOT recorded as synced." >&2
  exit 1
fi
# DIRTY-HOLD EXIT (Codex HIGH, 2026-09-01). The ACTION is identical in both modes — refuse, never
# overwrite, always log to $DIRTY_LOG and .pull-pending-actions.json — only the exit code differs,
# per the constraint this guard was built under. A manual/normal `/sync pull` has a human watching
# it right now, so a hold is a "go look at this before it happens again" signal, not a clean run.
# --quiet has no one watching — that is the whole reason refuse-and-report is safe to leave
# unattended in the first place — so it stays exit 0 regardless, same as SYNC_INCOMPLETE above.
if [[ "$MODE" != "quiet" && "$DIRTY_HELD_COUNT" -gt 0 ]]; then
  echo "[sync-from-baseline] $DIRTY_HELD_COUNT shared path(s) HELD — local edits protected, not overwritten:" >&2
  printf '%s' "$DIRTY_HELD" | sed 's/^/    - /' >&2
  echo "  Commit or discard the local edit, then re-run, to let that path sync." >&2
  echo "  See $DIRTY_LOG for the durable record." >&2
  exit 1
fi
exit 0
