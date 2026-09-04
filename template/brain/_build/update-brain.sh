#!/usr/bin/env bash
# Incremental brain vault update. Called by Core's Stop hook at session end.
# Exports only NEW sessions (--skip-existing), rebuilds hub pages, then auto-
# commits + pushes to <your-org>/core-brain so the brain is versioned and
# survives Mac death. Idempotent — empty diffs produce no commit.
set -uo pipefail

# Fail loud if $CORE_BRAIN unset — hardcoded fallback masked the 2026-05-14→15
# read/write divergence for ~24h. Per spec-cascade-fix-2026-05-16.md Phase 1.
: "${CORE_BRAIN:?CORE_BRAIN env var required — set in shell before invoking update-brain.sh}"
VAULT="$CORE_BRAIN"
LOG="/tmp/brain-update-$(date +%Y-%m-%d).log"

echo "[$(date)] Brain update starting" >> "$LOG"

python3 "$VAULT/_build/export.py" --skip-existing >> "$LOG" 2>&1
EXPORT_EXIT=$?

if [[ "$EXPORT_EXIT" -ne 0 ]]; then
    echo "[$(date)] export.py failed (exit $EXPORT_EXIT)" >> "$LOG"
    exit "$EXPORT_EXIT"
fi

python3 "$VAULT/_build/consolidate.py" >> "$LOG" 2>&1
CONSOLIDATE_EXIT=$?

if [[ "$CONSOLIDATE_EXIT" -ne 0 ]]; then
    echo "[$(date)] consolidate.py failed (exit $CONSOLIDATE_EXIT)" >> "$LOG"
    exit "$CONSOLIDATE_EXIT"
fi

# ── AUTO-COMMIT + PUSH ─────────────────────────────────────────────────────
# Brain is a versioned record. Most runs produce empty diffs (regen of
# unchanged content). Only commit + push when something actually changed.
cd "$VAULT" || exit "$CONSOLIDATE_EXIT"
# DESTINATION GUARD (2026-08-26). This is the TEMPLATE, so every Core spawned from it inherits
# whatever is here — including forks. The live core-life brain copy had this same bare push fixed
# earlier today; fixing only that instance left every FUTURE Core starting life with the hole,
# which is the instance-vs-class distinction core-business named on the bus and which I had
# walked straight past.
#
# The risk: this is an unattended nightly that publishes the ENTIRE brain vault — every session
# transcript, every person, every private fact. PreToolUse cannot see a push made from a launchd
# subprocess, so no Sentinel review can ever gate it. If origin were repointed — a mis-typed
# `git remote set-url`, a clone from the wrong place, a remote inherited during a spawn — this job
# would publish the whole history to whatever it now points at, including the shared baseline that
# every peer Core and every fork pulls from. That is the 2026-06-19 incident class.
#
# A destination allowlist is the only thing that can refuse here. NOTE FOR A SPAWNED CORE: the
# pattern below is core-life's. If your brain vault lives in a differently-named private repo,
# change the pattern to match YOUR repo — do not widen it, and never remove the guard.
_brain_push() {
    local _dest
    _dest=$(git remote get-url origin 2>/dev/null)
    case "$_dest" in
      # ANCHORED TO THE REPO NAME, not a bare substring. `*core-brain*` also matched
      # `someoneelse/core-brain-public` — flagged by sentinel-code on review. Owner is deliberately
      # NOT pinned (a spawned Core has its own), but the final path segment must BE core-brain.
      */core-brain|*/core-brain.git|*:core-brain|*:core-brain.git)
        # -u on the FIRST push only. Without an upstream `git push` fails with "no upstream
        # branch", and @{u} then does not resolve — which is what made the retry below a no-op
        # on exactly the run it existed for. Setting it once makes both work thereafter.
        if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
          git push >> "$LOG" 2>&1
        else
          git push -u origin HEAD >> "$LOG" 2>&1
        fi
        return $? ;;
      *)
        echo "[$(date)] REFUSING push: origin is '$_dest', which is not a core-brain repo." >> "$LOG"
        echo "[$(date)] The brain vault is private and is never pushed anywhere else." >> "$LOG"
        return 0 ;;
    esac
}

if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    git add -A >> "$LOG" 2>&1
    git commit -m "Brain auto-update $(date +'%Y-%m-%d %H:%M %Z')" >> "$LOG" 2>&1
    PUSH_EXIT=0
    _brain_push || PUSH_EXIT=$?
    if [[ "$PUSH_EXIT" -ne 0 ]]; then
        echo "[$(date)] git push failed (exit $PUSH_EXIT) — committed locally only" >> "$LOG"
    else
        echo "[$(date)] Brain auto-commit + push complete" >> "$LOG"
    fi
else
    # RETRY GAP (fixed live 2026-08-26; ported to the template 2026-08-29). The push was gated on
    # a DIRTY worktree, so a push that failed on one run and was followed by no new brain changes
    # never retried — the commits just sat local forever, and the vault silently stopped being
    # backed up while every log line still said "complete".
    #
    # This fix lived only in the author's vault for three days because the vault copy and this
    # template are two copies of one pipeline with no channel between them. That is the drift this
    # file keeps paying for; see the header note.
    # Unpushed count WITHOUT assuming an upstream exists. `git rev-list '@{u}..HEAD'` errors when
    # the branch has never been pushed, the `|| echo 0` swallows it, and the retry then declines to
    # fire in the one situation it was written for. Flagged in review 2026-08-29.
    _unpushed() {
        if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
            git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0
        elif git remote get-url origin >/dev/null 2>&1; then
            git rev-list --count HEAD 2>/dev/null || echo 0   # remote exists, never pushed
        else
            echo 0                                            # no remote: nothing to retry
        fi
    }
    if [[ "$(_unpushed)" -gt 0 ]]; then
        if _brain_push; then
            echo "[$(date)] retry-push of unpushed commits succeeded" >> "$LOG"
        else
            echo "[$(date)] retry-push failed — still committed locally only" >> "$LOG"
        fi
    else
        echo "[$(date)] Brain unchanged — no commit needed" >> "$LOG"
    fi
fi

echo "[$(date)] Brain update complete (export=$EXPORT_EXIT consolidate=$CONSOLIDATE_EXIT)" >> "$LOG"
exit 0
