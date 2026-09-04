#!/usr/bin/env bash
# sync-to-baseline.sh — push shared subset from current Core to nicknur7/core-agent.
#
# HARDENED 2026-05-19 after Phase 7 incident:
#   1. NO --delete flag (additive only; cannot wipe baseline files).
#   2. per_core_keep paths excluded from push (cannot leak personal data).
#   3. Source CORE_DIR sentinel-file check (cannot push from fake/empty dir).
#   4. Push refused if >50 file changes in one commit (sanity limit; bump
#      with --allow-large for legit large updates).
#   5. Commit msg identifies the Core unambiguously.
#
# Wired into Stop hook (Phase 8) with --quiet --only-if-changed. Sentinel
# can't gate `git push` from a subprocess context, so the safeguards above
# enforce safety regardless of whether Sentinel sees the push.
set -uo pipefail

ONLY_IF_CHANGED=0
ALLOW_LARGE=0
MODE="normal"
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --only-if-changed) ONLY_IF_CHANGED=1 ;;
    --quiet) MODE="quiet" ;;
    --allow-large) ALLOW_LARGE=1 ;;
    --check|--dry-run) CHECK_ONLY=1 ;;
    --force-writer) FORCE_WRITER=1 ;;
    --help|-h) sed -n '2,20p' "$0"; exit 0 ;;
  esac
done
FORCE_WRITER="${FORCE_WRITER:-0}"

CORE_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Source the path registry so sentinel paths come from the central JSON
# rather than hardcoded literals. Removes registry drift the code-paths lint
# was flagging (2026-05-22 catch-up to mirror core-school's fix).
export CORE_INSTANCE="${CORE_INSTANCE:-$CORE_DIR}"
# shellcheck source=bin/core-paths.sh
source "$CORE_DIR/bin/core-paths.sh"
MANIFEST="$CORE_DIR/bin/sync-manifest.json"

# Safeguard 3: source CORE_DIR must contain known sentinel files.
SENTINEL_FILES=(
  "$CORE_HOOK_STOP"
  "$CORE_HOOK_PRETOOLUSE_GUARD"
  "$CORE_DIR/scheduling/brain-pg/embed.py"
  "$MANIFEST"
)
for sf in "${SENTINEL_FILES[@]}"; do
  if [[ ! -f "$sf" ]]; then
    echo "sync-to-baseline: ABORT — source CORE_DIR ($CORE_DIR) missing sentinel file: $sf" >&2
    echo "sync-to-baseline: this is the safeguard added after the 2026-05-19 13:59 incident." >&2
    exit 3
  fi
done

if [[ ! -f "$MANIFEST" ]]; then
  echo "sync-to-baseline: manifest not found at $MANIFEST" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "sync-to-baseline: requires jq" >&2
  exit 1
fi

BASELINE_REPO=$(jq -r '.baseline_repo' "$MANIFEST")
BASELINE_BRANCH=$(jq -r '.baseline_branch' "$MANIFEST")
TS=$(date +%Y%m%d-%H%M%S)
TMP="/tmp/core-baseline-push-${TS}-$$"
CORE_NAME=$(basename "$CORE_DIR")

# Safeguard 6 (2026-06-02): single-writer policy. Only the designated
# baseline_writer Core may push shared code upstream. Every other Core is
# pull-only — this prevents a non-life Core from clobbering the baseline (and
# all other Cores via baseline-wins pull) with edits made in its own context.
# `baseline_writer` is null/absent on a fresh template → policy disabled, any
# Core may push (back-compat). Override with --force-writer for the rare case
# a shared fix is genuinely authored inside another Core (still Sentinel-gated).
BASELINE_WRITER=$(jq -r '.baseline_writer // empty' "$MANIFEST")
if [[ -n "$BASELINE_WRITER" && "$CORE_NAME" != "$BASELINE_WRITER" && "$FORCE_WRITER" != "1" ]]; then
  echo "sync-to-baseline: REFUSED — '$CORE_NAME' is not the baseline writer ('$BASELINE_WRITER')." >&2
  echo "sync-to-baseline: this Core is pull-only by policy. Author shared-code changes in" >&2
  echo "sync-to-baseline:   '$BASELINE_WRITER' and let them flow down, or re-run with --force-writer" >&2
  echo "sync-to-baseline:   if this change is genuinely meant to originate here." >&2
  exit 4
fi

log() { [[ "$MODE" != "quiet" ]] && echo "$@"; }
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

log "[sync-to-baseline] core=$CORE_NAME  baseline=$BASELINE_REPO@$BASELINE_BRANCH"

if ! CLONE_ERR=$(git clone --depth 1 --branch "$BASELINE_BRANCH" --quiet \
  "https://github.com/${BASELINE_REPO}.git" "$TMP" 2>&1); then
  # VISIBLE failure + persistent marker — never a silent exit-0 no-op again.
  # 2026-06-04: a stale gh/keychain credential silently blocked baseline pushes.
  echo "[sync-to-baseline] WARN: baseline clone FAILED — push DID NOT happen. ${CLONE_ERR}" >&2
  printf '%s sync-to-baseline clone-failed: %s\n' "$(date -Iseconds 2>/dev/null || date)" "${CLONE_ERR%%$'\n'*}" \
    >> "$CORE_DIR/.claude/state/.sync-failures" 2>/dev/null || true
  exit 0
fi

# M4 fix 2026-05-19: detect sync conflict (baseline advanced since this Core's
# last pull). If yes, warn loudly — current policy is last-pusher-wins, so
# pushing without a fresh pull silently overwrites baseline edits from
# another Core. Doesn't block (preserves existing behavior) but surfaces the
# risk so Nick can decide whether to pull first.
LAST_SYNC_FILE="$CORE_DIR/.claude/state/.last-baseline-sync"
CURRENT_BASELINE_SHA=$(cd "$TMP" && git rev-parse HEAD)
if [[ -f "$LAST_SYNC_FILE" ]]; then
  LAST_SYNCED_SHA=$(grep -oE 'baseline=[a-f0-9]+' "$LAST_SYNC_FILE" 2>/dev/null | tail -1 | cut -d= -f2)
  if [[ -n "$LAST_SYNCED_SHA" && "$LAST_SYNCED_SHA" != "$CURRENT_BASELINE_SHA" ]]; then
    log "[sync-to-baseline] CONFLICT WARNING: baseline advanced $LAST_SYNCED_SHA → $CURRENT_BASELINE_SHA"
    log "[sync-to-baseline]   since this Core's last pull. Another Core (or manual edit) pushed in between."
    log "[sync-to-baseline]   Current policy is last-pusher-wins — proceeding will overwrite intermediate edits."
    log "[sync-to-baseline]   To avoid this, run \`bash bin/sync-from-baseline.sh\` first, then re-push."
  fi
fi

# Capture baseline's PREVIOUS manifest before rsync overwrites it.
# Used by the orphan-cleanup pass after rsync to detect shared.dirs /
# shared.files entries that were removed from the manifest — those baseline
# files become orphans (rsync without --delete leaves them in place) and
# this pass `git rm`s them to keep baseline in sync with the manifest.
# Added 2026-05-19 (longevity-test prep — manifest-driven cleanup gap).
PREV_MANIFEST="/tmp/core-baseline-prev-manifest-$$.json"
cp "$TMP/bin/sync-manifest.json" "$PREV_MANIFEST" 2>/dev/null || true

# Read per_core_keep list once (used per-dir below)
PER_CORE_KEEP=$(jq -r '.per_core_keep[]' "$MANIFEST")

# Gitignore-honoring push guard (2026-07-27). per_core_keep is a hand-maintained
# allowlist, so any personal-data file someone parks in a shared dir leaks until a
# human remembers to list it. That is exactly how scheduling/claude-si/friction-cases.jsonl
# — Nick's verbatim prompts, correctly .gitignore'd in this Core — got rsync'd into
# the baseline repo: rsync copies the working tree and does not consult .gitignore.
# Now it does. A local ignore rule is sufficient to keep a file out of the baseline;
# per_core_keep stays as the mechanism for TRACKED per-Core files.
GITIGNORED_PATHS=$(git -C "$CORE_DIR" ls-files --others --ignored --exclude-standard --directory 2>/dev/null || true)

# Copy shared dirs (additive; NO --delete). Per-dir, compute the subset of
# per_core_keep that's NESTED inside this shared dir and convert to
# rsync-source-relative exclude patterns. This is what makes per_core_keep
# enforcement work on the sync-TO direction (bug fixed 2026-05-19 post-Phase 10).
while IFS= read -r d; do
  [[ -z "$d" ]] && continue
  src="$CORE_DIR/$d/"
  dst="$TMP/$d/"
  [[ ! -d "$src" ]] && continue
  mkdir -p "$dst"

  # Per-dir excludes: pycache always; plus any per_core_keep path nested in $d
  PER_DIR_EXCLUDES=(--exclude __pycache__ --exclude '*.pyc')
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    # If $p starts with "$d/", strip prefix and add as exclude
    if [[ "$p" == "$d/"* ]]; then
      rel="${p#$d/}"
      # Strip "/**" suffix so rsync sees the dir name
      rel="${rel%/\*\*}"
      PER_DIR_EXCLUDES+=(--exclude "$rel")
    fi
  done <<< "$PER_CORE_KEEP"

  # Same prefix-strip treatment for anything .gitignore'd inside this shared dir.
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    if [[ "$p" == "$d/"* ]]; then
      rel="${p#$d/}"
      rel="${rel%/}"
      PER_DIR_EXCLUDES+=(--exclude "$rel")
    fi
  done <<< "$GITIGNORED_PATHS"

  rsync -a "${PER_DIR_EXCLUDES[@]}" "$src" "$dst"
done < <(jq -r '.shared.dirs[]' "$MANIFEST")

# Copy shared files individually
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  src="$CORE_DIR/$f"
  dst="$TMP/$f"
  [[ ! -f "$src" ]] && continue
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
done < <(jq -r '.shared.files[]' "$MANIFEST")

# Regenerate the baseline's OWN template .claude/settings.json hooks block from the
# just-copied bin/hook-registry.json (2026-08-31). WHY: .claude/settings.json is
# per_core_keep, so the rsync/cp passes above never touch it — it is the one file a
# fresh `git clone` boots from that this script previously left completely alone.
# It was hand-committed 2026-05-19 and never touched again while the registry grew
# from ~12 entries to 58, so a fresh clone was registering 11 of 44 managed hooks —
# including missing sync-from-baseline@SessionStart itself, so a fresh Core could not
# even self-heal on first open. Regenerating it HERE, on every push that changes the
# registry, is what makes this a durable fix instead of the one-time hand-edit that
# produced the original bug: the template can no longer silently drift from the
# registry it is derived from. --role puller because that's what identity.json's own
# template ships with (per_core_keep, untouched by this rsync too) — the WRITER
# Core's real settings.json is personalized and never comes from this template at all.
if [[ -f "$TMP/bin/reconcile-hooks.py" && -f "$TMP/bin/hook-registry.json" ]]; then
  if ! python3 "$TMP/bin/reconcile-hooks.py" --emit-template "$TMP/.claude/settings.json" \
        --registry "$TMP/bin/hook-registry.json" --role puller; then
    log "[sync-to-baseline] WARN: settings.json template regeneration FAILED — baseline's" >&2
    log "[sync-to-baseline]   template settings.json left as-is (fail-safe; see reconcile-hooks.py)." >&2
  fi
fi

# Orphan-cleanup pass — manifest-driven baseline cleanup.
# Closes the asymmetry: rsync without --delete leaves removed shared.dirs /
# shared.files as orphans in baseline. Two detection layers (belt-and-
# suspenders, both safe):
#
#   (1) PREV-vs-CURR manifest diff — catches entries removed IN this same
#       push. Compares baseline's previous manifest (captured right after
#       clone, before rsync overwrote it) to the current manifest.
#
#   (2) Filesystem-vs-manifest scan — catches pre-existing orphans (entries
#       removed from manifest in a prior push but never cleaned up). Walks
#       baseline-clone's `scheduling/` parent — anything there that's not in
#       current shared.dirs is an orphan. (We only walk `scheduling/` and
#       not `.claude/` because `.claude/` contains shared FILES tracked
#       individually; walking it would mis-classify per-file-tracked
#       subdirs.) Future shared parents added to manifest scope should be
#       added to the SCAN_PARENTS list below.
#
# Both layers use `git rm -r` (stages deletion via git, NOT `rm -rf` and
# NOT rsync --delete). Safety: only acts on declared-removed-or-not-
# declared entries; no heuristic file matching, no path traversal.

# Layer 1: PREV-vs-CURR manifest diff
if [[ -f "$PREV_MANIFEST" ]]; then
  PREV_DIRS=$(jq -r '.shared.dirs[]?' "$PREV_MANIFEST" 2>/dev/null | sort)
  CURR_DIRS=$(jq -r '.shared.dirs[]?' "$MANIFEST" 2>/dev/null | sort)
  REMOVED_DIRS=$(comm -23 <(printf '%s\n' "$PREV_DIRS") <(printf '%s\n' "$CURR_DIRS") || true)
  PREV_FILES=$(jq -r '.shared.files[]?' "$PREV_MANIFEST" 2>/dev/null | sort)
  CURR_FILES=$(jq -r '.shared.files[]?' "$MANIFEST" 2>/dev/null | sort)
  REMOVED_FILES=$(comm -23 <(printf '%s\n' "$PREV_FILES") <(printf '%s\n' "$CURR_FILES") || true)
  while IFS= read -r d; do
    [[ -z "$d" ]] && continue
    if [[ -d "$TMP/$d" ]]; then
      (cd "$TMP" && git rm -r --quiet "$d") && \
        log "[sync-to-baseline] orphan-cleanup: removed dir $d (removed from manifest this push)"
    fi
  done <<< "$REMOVED_DIRS"
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    if [[ -f "$TMP/$f" ]]; then
      (cd "$TMP" && git rm --quiet "$f") && \
        log "[sync-to-baseline] orphan-cleanup: removed file $f (removed from manifest this push)"
    fi
  done <<< "$REMOVED_FILES"
  rm -f "$PREV_MANIFEST"
fi

# Layer 2: Filesystem-vs-manifest scan (only for SCAN_PARENTS)
SCAN_PARENTS=("scheduling")
CURRENT_SHARED_DIRS=$(jq -r '.shared.dirs[]?' "$MANIFEST" 2>/dev/null)
for parent in "${SCAN_PARENTS[@]}"; do
  [[ ! -d "$TMP/$parent" ]] && continue
  for entry in "$TMP/$parent"/*/; do
    [[ ! -d "$entry" ]] && continue
    relpath="${entry#$TMP/}"
    relpath="${relpath%/}"  # strip trailing slash
    if ! grep -qFx "$relpath" <<< "$CURRENT_SHARED_DIRS"; then
      (cd "$TMP" && git rm -r --quiet "$relpath") && \
        log "[sync-to-baseline] orphan-cleanup: removed dir $relpath (not in manifest shared.dirs)"
    fi
  done
done

# Layer 3: retired[] tombstones — the half of the mechanism that only ever ran on PULLERS.
#
# `retired[]` retires an individual FILE that still lives inside a still-shared dir
# (bin/init-brain.sh inside `bin/`). Layers 1 and 2 above cannot see it: layer 1 diffs
# shared.dirs/shared.files between manifests and `bin` was never removed from either, and layer 2
# only walks SCAN_PARENTS for whole directories. The brain records the original design in as many
# words — the retired list is "consumed by a new pass in sync-from-baseline.sh" — so the cleanup was
# built for the PULL side and the push side was never written. Peers lose the file; the baseline
# keeps it forever.
#
# MEASURED, and not cosmetic. At d11e06c, 7 of 8 retired[] entries were still in the baseline tree
# while absent from life. Two live consequences:
#   1. Every pull re-rsyncs them into the peer and the tombstone pass deletes them again in the same
#      run — churn on every sync, forever.
#   2. why-red.sh's subject_drift compares the seat's `bin/` against the baseline's `bin/`, finds
#      init-brain.sh and sync-from-engine.sh "missing here", and calls the whole subject STALE. That
#      turned 15 tests UNDECIDABLE on core-business — a seat that had just pulled to baseline HEAD
#      and was byte-current. A prior review saw the inconsistency and called it "harmless in
#      effect"; it was the entire cause of a fully-current Core's suite reading red.
#
# Same tool and same safety envelope as the layers above: `git rm` on paths the manifest EXPLICITLY
# declares retired — no filesystem heuristic, no glob, no traversal. An entry already absent is
# skipped, so this is idempotent and self-clearing: once the backlog drains it does nothing.
RETIRED_REMOVED=0
while IFS= read -r rp; do
  [[ -z "$rp" ]] && continue
  # Never git rm a per_core_keep path. It should not be in the baseline at all, and if one ever is,
  # deleting it here is not this pass's call. sync-from-baseline's tombstone pass refuses
  # per_core_keep for the same reason.
  # REFUSE PATH ESCAPE FIRST, exactly as the pull side does. `git rm -r` on an absolute path, or one
  # containing `..`, would reach outside the baseline clone entirely.
  case "$rp" in
    /*|*..*) log "[sync-to-baseline] retired-cleanup: REFUSED (not repo-relative): $rp"; continue ;;
  esac
  # GLOB-AWARE, AND MY FIRST VERSION WAS NOT — the identical defect the PULL side already had and
  # fixed (core-finance found it 2026-08-13, hours after that guard shipped), reintroduced here
  # hours after I read the comment describing it.
  #
  # `select(. == $p)` is an EXACT STRING MATCH against a list that is largely GLOB PATTERNS. It
  # refuses the literal string `secrets/**`, which nobody would ever tombstone, and does NOT refuse
  # `secrets/keys.json`, which is the file that pattern exists to protect. The brain records the
  # concrete case from the pull-side incident: `.claude/agents/sentinel/CLAUDE.md` WOULD DELETE
  # despite being protected by `.claude/agents/sentinel/**`. Same shape for memory/**, sessions/**,
  # tasks/**.
  #
  # A sentinel-code review noted it as a follow-up hardening and I pushed without doing it; a Codex
  # verification sweep caught it still open. Exposure is ZERO today — no current retired[] entry
  # falls under a per_core_keep glob, checked individually — and that is exactly why it gets fixed
  # BEFORE it matters, in a pass that deletes files from the shared baseline which four peers then
  # pull. In the pull side's own words about this same defect: a guard whose first real exercise is
  # the entry that needed it has never been tested at all.
  #
  # Strip a trailing /** and match the path itself or any child of it.
  if jq -e --arg p "$rp" '.per_core_keep[]? | (sub("/\\*\\*$";"")) as $k | select($p == $k or ($p | startswith($k + "/")))' "$MANIFEST" >/dev/null 2>&1; then
    log "[sync-to-baseline] retired-cleanup: SKIPPED $rp (declared per_core_keep — not ours to delete)"
    continue
  fi
  if [[ -e "$TMP/$rp" ]]; then
    if (cd "$TMP" && git rm -r --quiet -- "$rp" 2>/dev/null); then
      RETIRED_REMOVED=$((RETIRED_REMOVED+1))
      log "[sync-to-baseline] retired-cleanup: removed $rp (declared retired in manifest)"
    fi
  fi
done < <(jq -r '.retired[]?' "$MANIFEST" 2>/dev/null)
[[ "$RETIRED_REMOVED" -gt 0 ]] && \
  log "[sync-to-baseline] retired-cleanup: $RETIRED_REMOVED tombstoned path(s) removed from baseline"

cd "$TMP"
git add -A
if git diff --cached --quiet; then
  log "[sync-to-baseline] no shared changes; skipping push."
  exit 0
fi

# Safeguard 4: large-change sanity limit
CHANGE_COUNT=$(git diff --cached --name-only | wc -l | tr -d ' ')
# `&& CHECK_ONLY -eq 0` — THE ESCAPE HATCH WAS BEHIND THE GATE IT EXISTS TO AVOID (2026-08-13).
#
# This abort sat above the `--check` branch (~L371), so it fired on `--check` too and exited 4
# before the count or the DIGEST-INPUT block could be printed. The notice below then told the
# operator to "get the count for free first (--check needs no approval)" — advice that is
# IMPOSSIBLE TO FOLLOW at >50 changes, because --check aborted at exactly the same line. The
# larger the pending set, the more certainly the recommended remedy failed.
#
# And the notice's own premise is false on this path: reaching here on a --check run means NO
# token was minted and none consumed, because --check never passes the guard. So it announced a
# spent approval to an operator who had not spent one — the same misattribution shape the comment
# below was written to PREVENT, reproduced by the fix itself.
#
# Found because it made test_digest_is_verifiable go red: with 51 shared files pending, --check
# emitted no digest at all. The test was right and the abort was the cause.
#
# --check pushes nothing, so the safety limit has nothing to protect against on that path. It
# still REPORTS the overage below, because the count is the thing the operator came for.
if [[ "$CHANGE_COUNT" -gt 50 && "$ALLOW_LARGE" -eq 0 && "$CHECK_ONLY" -eq 0 ]]; then
  echo "[sync-to-baseline] ABORT — $CHANGE_COUNT file changes exceeds safety limit (50)." >&2
  echo "[sync-to-baseline] If this is intentional (e.g., initial Phase 10 push), re-run with --allow-large." >&2
  echo "" >&2
  # SAY THAT THE APPROVAL WAS SPENT. Reaching this line means the guard already let the invocation
  # through, which means an approval token was minted AND CONSUMED — by a run that pushed nothing.
  #
  # Without this notice the operator re-runs with --allow-large, gets "no fresh APPROVE receipt",
  # and reads it as SENTINEL DECLINED rather than YOUR TOKEN WAS SPENT ON AN ABORTED RUN. That is
  # the misdirection shape core-business documented for the non-ASCII binding bug: the failure
  # names the wrong cause, so the operator retries the review instead of the actual blocker.
  #
  # The ORDER cannot be fixed here. CHANGE_COUNT is not knowable until the baseline is cloned and
  # rsynced, which is long after the guard ran. `--check` computes it WITHOUT needing approval, so
  # the honest fix is to say so at the moment it matters.
  echo "[sync-to-baseline] NOTE: your Sentinel approval was consumed by this aborted run." >&2
  echo "[sync-to-baseline]       Nothing was pushed. Get the count for free first (--check needs" >&2
  echo "[sync-to-baseline]       no approval), then obtain a fresh one for the --allow-large form." >&2
  echo "" >&2
  echo "[sync-to-baseline] First 20 affected files:" >&2
  git diff --cached --name-only | head -20 | sed 's/^/    /' >&2
  exit 4
fi

# NO `head` HERE. This list is the REVIEW ARTIFACT for a fleet-wide push.
#
# It was `head -20` while CHANGE_COUNT above is the true total, so a 29-file push printed
# "29 file changes:" and then listed 20. On 2026-07-27 the review artifact for a real push
# was assembled from this list, and 9 files reached the baseline without sentinel-code ever
# seeing them: the close-degradation fix, a migration, the system-health benchmark, and
# others. Nothing harmful was in them, which is luck rather than process.
#
# A count that disagrees with the list beneath it is worse than either alone — the number
# looks authoritative and the list looks complete. Print all of them; length is a property
# of the change, not something the tool should decide to hide.
CHANGED_FILES=$(git diff --cached --name-only | sed 's/^/    /')
log "[sync-to-baseline] $CHANGE_COUNT file changes:"
log "$CHANGED_FILES"

# CONTENT DIGEST — what a peer approval should actually be approving.
#
# core-business, bus #1025, refusing to have its APPROVE used to satisfy the 50-file ceiling, and
# the structural half of its argument is the reason this exists:
#
#     "the receipt would attest that business approved a COMMAND, at a TIME. It would say nothing
#      about which 112 files rode along. You would be spending a ceiling designed around content on
#      a token that cannot express content."
#
# It is right, and the gap is not theoretical — it is the thing I have been closing BY HAND all
# evening, holding a push three times because the approval named a SHA and the sync copies the
# WORKING TREE, so anything committed afterwards rides along unreviewed. That discipline worked
# because I chose to apply it. A mechanism should not depend on that.
#
# The digest covers PATH + BLOB HASH for every staged change, sorted, so it changes if any file
# changes, if a file is added, or if one is dropped. `--raw` gives the post-image hash directly;
# hashing paths alone would miss an edit to a file already in the list.
#
# It is printed by --check, which needs no approval, so the reviewer can be handed the exact set
# and the digest together. sentinel-approve.sh then requires the approval to carry that digest AND
# recomputes it at mint time: if the tree moved between review and push, the digest no longer
# matches and the mint refuses. That is the hand-discipline made structural.
# THE INPUTS ARE CAPTURED, NOT JUST THE HASH — because the reviewer could not verify the hash.
#
# core-business, twice: it recomputed the digest I sent and got e3b0c442..., the sha256 of the EMPTY
# STRING, because `git diff --cached --raw` is only non-empty inside the transient staging state on
# THIS machine. By the time a reviewer looks, the staging area is gone and there is nothing to hash.
# Its words: "the reviewer is asked to approve a hash that only exists inside a transient staging
# state on the producer's machine. This is not carelessness on your part; the protocol cannot work
# as written."
#
# It was right, and mint-time recomputation does not fix it: that catches DRIFT between approval and
# push, which protects ME, and gives the REVIEWER nothing to check. So the sorted lines the hash is
# taken over are captured here and can be emitted on request. A reviewer pipes them through the same
# shasum and compares — turning a number taken on faith into one that can be independently derived.
#
# Landing before finance and ops pull, per business: they are two baselines behind and will inherit
# whatever the protocol becomes.
# RENAME HOLE — closed 2026-08-12. core-business attacked this line when asked for a path where the
# digest matches but the pushed content differs, and found one. Reproduced in a scratch repo here:
#
#   git mv docs/note.md .claude/hooks/evil.sh
#   :100644 100644 a4fc220 a4fc220 R100   docs/note.md   .claude/hooks/evil.sh
#   old formula  awk '{print $4, $6}'  ->  "a4fc220 docs/note.md"
#
# Rename detection collapses a move into ONE row whose $6 is the SOURCE path. So a reviewer approves
# a digest naming `docs/note.md` and the content lands in `.claude/hooks/` — the trust root. At the
# old 1800s TTL the window was short; at the 24h TTL this was about to get, a stale approval for an
# innocuous docs change would authorise a file arriving in the trust root. That is why step 1
# (bind the digest) has to be provably correct before step 2 (extend the TTL), and it was not.
#
# business proposed `-M0`. I ran it rather than taking it: `-M0` is rename detection at a 0 PERCENT
# similarity threshold — MORE aggressive, not disabled — and it still emitted the single R100 row
# with only the source path. `--no-renames` is the flag that actually splits a move into
# D <source> + A <destination>, so BOTH paths reach the reviewer. Right diagnosis, wrong flag; the
# difference was only visible by executing it.
#
# Each field earns its place:
#   $2  new mode      — a chmod +x with identical content was otherwise invisible (100644 -> 100755)
#   $4  blob hash     — --no-abbrev so it is the full 40 hex, not 7
#   $5  status letter — A/D/M/R, so a delete cannot masquerade as a modify
#   $6  path          — with --no-renames this is the real path for both halves of a move
SYNC_DIGEST_INPUT=$(git diff --cached --raw --no-abbrev --no-renames \
                      | awk '{print $2, $4, $5, $6}' | sort)
SYNC_DIGEST=$(printf '%s\n' "$SYNC_DIGEST_INPUT" | shasum -a 256 | awk '{print $1}')
log "[sync-to-baseline] content digest: $SYNC_DIGEST"

# --check / --dry-run: stop here. Report count + file list, no commit, no push.
# Exit code 10 = changes pending (vs 0 = no changes). Phase 8 SessionStart /
# Stop hooks use this to surface "shared changes pending push" without
# actually doing the push (Sentinel-code review gates the apply step).
if [[ "$CHECK_ONLY" -eq 1 ]]; then
  if [[ "$MODE" != "quiet" ]]; then
    echo "[sync-to-baseline] --check: $CHANGE_COUNT shared file(s) would be pushed."
    # The overage is REPORTED here rather than aborted above, so --check stays usable as the free
    # way to learn the count — which is precisely what the abort's own advice tells you to do.
    if [[ "$CHANGE_COUNT" -gt 50 && "$ALLOW_LARGE" -eq 0 ]]; then
      echo "[sync-to-baseline] NOTE: over the 50-file safety limit — a real push needs --allow-large."
    fi
    echo "$CHANGED_FILES"
  fi
  # ALWAYS on its own line, even in quiet mode: this is the value a reviewer must be given and the
  # value sentinel-approve.sh parses back. Printing it only in verbose mode would make the mint's
  # recomputation depend on a display setting.
  echo "DIGEST: $SYNC_DIGEST"
  # THE VERIFIABLE HALF. Set SHOW_DIGEST_INPUT=1 to emit the exact lines the digest is taken over,
  # so a reviewer can derive the number instead of trusting it:
  #
  #   SHOW_DIGEST_INPUT=1 bash <this script> --check \
  #     | sed -n '/^DIGEST-INPUT-BEGIN$/,/^DIGEST-INPUT-END$/p' | grep -v '^DIGEST-INPUT' \
  #     | shasum -a 256
  #
  # Behind a flag so routine --check output stays short — the reviewer asks for it when reviewing,
  # which is the only moment it matters.
  if [[ "${SHOW_DIGEST_INPUT:-0}" == "1" ]]; then
    echo "DIGEST-INPUT-BEGIN"
    printf '%s\n' "$SYNC_DIGEST_INPUT"
    echo "DIGEST-INPUT-END"
  fi
  exit 10
fi

git -c user.email="${GIT_AUTHOR_EMAIL:-nicknur7@users.noreply.github.com}" \
    -c user.name="${GIT_AUTHOR_NAME:-Multi-Core sync}" \
    commit -m "Multi-Core baseline sync from $CORE_NAME $(date '+%Y-%m-%d %H:%M %Z')

Source Core: $CORE_NAME
Files changed: $CHANGE_COUNT
Manifest: $MANIFEST" >/dev/null

log "[sync-to-baseline] pushing..."
if git push origin "$BASELINE_BRANCH" 2>&1 | tail -3; then
  log "[sync-to-baseline] push OK"
  # Update .last-baseline-sync log + clear .pending-push-marker so SessionStart
  # check (l) doesn't keep falsely reporting "baseline has new commits" after
  # WE were the ones who advanced it. 2026-05-22 fix — was previously letting
  # the warnings linger past push success.
  NEW_BASELINE_SHA=$(git rev-parse HEAD)
  STATE_DIR_CORE="$CORE_DIR/.claude/state"
  mkdir -p "$STATE_DIR_CORE" 2>/dev/null
  echo "$(date +%Y-%m-%dT%H:%M:%S%z) baseline=${NEW_BASELINE_SHA} via=sync-to-baseline.sh source=${CORE_NAME}" \
    >> "$STATE_DIR_CORE/.last-baseline-sync"
  rm -f "$STATE_DIR_CORE/.pending-push-marker" 2>/dev/null
  log "[sync-to-baseline] state updated: .last-baseline-sync stamped, .pending-push-marker cleared"
else
  log "[sync-to-baseline] push FAILED" >&2
  exit 2
fi
exit 0
