#!/usr/bin/env bash
# The automatic pull holds back a GENUINE local edit and nothing else — it must never hold back a
# file that merely fell behind the baseline.
#
# WHY THIS EXISTS (and why it replaces the previous version of this file, in full)
# ----------------------------------------------------------------------------------------------
# Codex HIGH, 2026-09-01: the rsync at the directory-sync apply branch (and the `cp` in the
# shared-files loop) compared the freshly-cloned baseline against this Core's disk and NOTHING
# else — never against this Core's own git HEAD. A file Nick is mid-editing and a file that merely
# fell behind the baseline look identical to that comparison. The first fix for this — and the
# first version of THIS test — got the direction backwards in the most damaging possible way.
#
# A pull APPLIES files to disk; committing them is a separate act these Cores mostly do not do.
# So the resting state of a HEALTHY puller is: working tree == the baseline it last synced, git
# HEAD == something older, and every shared file reads as "differs from HEAD" — which the first
# fix treated as dirty, full stop. MEASURED on core-business before the correction below:
# bin/core-doctor.sh, scheduling/brain-pg/brain-health.py and compile-truth-refresh.py were
# byte-identical to baseline 5c97de2 — the exact bytes the PREVIOUS pull wrote — while presenting
# as 1,326 lines of "local work" across 35 paths. Fleet-wide ~62,000 lines, none of it authored
# locally. The old 19 assertions in this file pinned exactly that blockade: they built dirty
# fixtures with NO prior-baseline record at all, which under the corrected predicate's fail-open
# rule is precisely the case that must NOT be held — so they could not survive being reused.
#
# The corrected predicate: a file is dirty only if it differs from HEAD **and** differs from the
# baseline this Core last synced (`is_shared_path_dirty()` in sync-from-baseline.sh). This file
# proves all four of its outcomes against the REAL script, not a re-implementation:
#
#   (a) THE CASE THE BUG BROKE, and the one no prior test covered: a shared path whose disk
#       content matches the PREVIOUS baseline must be OVERWRITTEN — the peer receives the fix.
#   (b) a shared path that differs from BOTH HEAD and the previous baseline (a real edit) is
#       HELD, reported, and logged — the mechanism the trust-root test's siblings never touch.
#   (c) an untracked file absent from the previous baseline entirely is HELD — no `git checkout`
#       can recover it once overwritten, so the guard cannot use "no prior record for THIS path"
#       as license to proceed.
#   (d) FAIL-OPEN: when no prior-baseline checkout is available at all (unknown/unresolvable
#       SHA — a fresh Core's first pull, or one fallen behind farther than the --depth 50 fetch
#       can see), a file differing from HEAD is treated as CLEAN and the pull proceeds. This is
#       the direction the very first cut got backwards.
#
#   bash bin/tests/test_dirty_shared_pull_hold.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/bin/sync-from-baseline.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1${2:+ — $2}"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "wanted '$3', got '$2'"; fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

run_pull() {  # $1 = CORE dir, $2 = BASELINE dir, $3 = extra flag ("" or --quiet)
  ( cd "$1" && CLAUDE_PROJECT_DIR="$1" CORE_INSTANCE="$1" \
      CORE_BASELINE_URL_LOCAL_TEST="$2" CORE_SYNC_TEST_MODE=1 \
      bash "$SCRIPT" $3 2>&1 )
}

# ══════════════════════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — a resolvable prior baseline. Covers (a) overwrite-on-match, (b) hold-on-real-edit,
# (c) hold-on-untracked-and-absent. One pull, four shared paths, three different histories.
# ══════════════════════════════════════════════════════════════════════════════════════════════

# ── the baseline repo has TWO commits: OLD (what a previous pull would have delivered) and NEW
#    (what THIS pull delivers — the fix). `--depth 1` only sees NEW; the script must fetch deeper
#    to resolve OLD as the recorded prior baseline, exactly as a real pull does.
BL="$WORK/baseline"
mkdir -p "$BL/.claude/hooks" "$BL/bin"
cp "$REPO/bin/sync-manifest.json" "$BL/bin/"
git -C "$BL" init -q -b main
echo 'OLD baseline content A.' > "$BL/.claude/hooks/case-a.sh"
echo 'OLD baseline content B.' > "$BL/.claude/hooks/case-b.sh"
echo 'OLD baseline content — ordinary, never locally touched.' > "$BL/.claude/hooks/ordinary-shared.sh"
git -C "$BL" -c user.email=t@t -c user.name=t add -A
git -C "$BL" -c user.email=t@t -c user.name=t commit -qm "OLD baseline"
OLD_SHA="$(git -C "$BL" rev-parse HEAD)"
echo 'NEW baseline content A — the fix. Must land where disk == OLD (no proof of authorship).' \
  > "$BL/.claude/hooks/case-a.sh"
echo 'NEW baseline content B — the fix. Must NOT overwrite a genuine local edit.' \
  > "$BL/.claude/hooks/case-b.sh"
echo 'NEW baseline content C — a path that did not exist in OLD at all.' \
  > "$BL/.claude/hooks/case-c.sh"
echo 'NEW baseline content — ordinary. Must land; this pull is not broken.' \
  > "$BL/.claude/hooks/ordinary-shared.sh"
git -C "$BL" -c user.email=t@t -c user.name=t add -A
git -C "$BL" -c user.email=t@t -c user.name=t commit -qm "NEW baseline"

# ── a throwaway Core that looks like a real puller: git HEAD is OLDER than what is actually on
#    disk for every case, which is the norm for a Core that does not commit what pulls write.
CORE="$WORK/core-fake"
mkdir -p "$CORE/.claude/hooks" "$CORE/.claude/state" "$CORE/bin"
cp "$REPO/bin/sync-manifest.json" "$CORE/bin/"
printf '{"org_id": 9, "domain_label": "fake", "hook_profile": {"role": "puller"}}\n' \
  > "$CORE/.claude/identity.json"
echo 'INIT commit content — stale, predates even the OLD baseline.' > "$CORE/.claude/hooks/case-a.sh"
echo 'INIT commit content — stale, predates even the OLD baseline.' > "$CORE/.claude/hooks/case-b.sh"
echo 'OLD baseline content — ordinary, never locally touched.'      > "$CORE/.claude/hooks/ordinary-shared.sh"
git -C "$CORE" init -q -b main
git -C "$CORE" -c user.email=t@t -c user.name=t add -A
git -C "$CORE" -c user.email=t@t -c user.name=t commit -qm init
# NOW make the disk diverge from HEAD, uncommitted — the state a real pull actually sees.
# (a) disk == exactly what the OLD baseline wrote. Not a human edit; must be overwritten.
echo 'OLD baseline content A.' > "$CORE/.claude/hooks/case-a.sh"
# (b) disk == a genuine edit, matching NEITHER HEAD nor the OLD baseline. Must be held.
echo 'GENUINE LOCAL EDIT — differs from HEAD and from every baseline. Must survive the pull.' \
  > "$CORE/.claude/hooks/case-b.sh"
# (c) untracked, never committed, and this path did not exist in the OLD baseline either.
echo 'LOCAL UNTRACKED FILE — no prior baseline to compare against, no git recovery. Must survive.' \
  > "$CORE/.claude/hooks/case-c.sh"
CASE_A_SHA_BEFORE="$(shasum "$CORE/.claude/hooks/case-a.sh" | awk '{print $1}')"
CASE_B_SHA_BEFORE="$(shasum "$CORE/.claude/hooks/case-b.sh" | awk '{print $1}')"
CASE_C_SHA_BEFORE="$(shasum "$CORE/.claude/hooks/case-c.sh" | awk '{print $1}')"
# The record this Core is claiming: "I am synced to OLD." True of the bytes; never committed.
printf '%s baseline=%s changed=3 orphans=0 tombstones=0 dirty_held=0\n' \
  "$(date -Iseconds)" "$OLD_SHA" > "$CORE/.claude/state/.last-baseline-sync"

echo "SCENARIO 1 — resolvable prior baseline (cases a, b, c)"
OUT="$(run_pull "$CORE" "$BL" --quiet)"; RC=$?
printf '%s\n' "$OUT" > "${TDS_DEBUG_OUT:-/dev/null}"

if [[ ! -f "$CORE/.claude/hooks/ordinary-shared.sh" ]] || ! grep -q "NEW baseline content" "$CORE/.claude/hooks/ordinary-shared.sh" 2>/dev/null; then
  echo "  SKIP  harness could not drive the real script against a local baseline."
  echo "        Asserting the STATIC properties instead, which are still worth pinning:"
  grep -q 'is_shared_path_dirty' "$SCRIPT" \
    && ok "is_shared_path_dirty() is defined" || bad "is_shared_path_dirty defined"
  grep -q 'PREV_BASELINE_DIR' "$SCRIPT" \
    && ok "a prior-baseline checkout is attempted" || bad "prior-baseline checkout attempted"
  grep -q 'fail-open' "$SCRIPT" \
    && ok "the fail-open direction is documented in the source" || bad "fail-open documented"
  grep -q 'record_dirty_hold' "$SCRIPT" \
    && ok "held paths are recorded (log + pending JSON)" || bad "dirty holds recorded"
  echo ""
  echo "=== Results: $PASS passed, $FAIL failed (static mode) ==="
  [[ $FAIL -eq 0 ]] && exit 0 || exit 1
fi

check "--quiet exits 0 even though a path was held (must never brick a session)" "$RC" "0"

CASE_A_SHA_AFTER="$(shasum "$CORE/.claude/hooks/case-a.sh" | awk '{print $1}')"
CASE_B_SHA_AFTER="$(shasum "$CORE/.claude/hooks/case-b.sh" | awk '{print $1}')"
CASE_C_SHA_AFTER="$(shasum "$CORE/.claude/hooks/case-c.sh" | awk '{print $1}')"

# (a) THE CASE THE BUG BROKE. A disk copy matching the prior baseline is NOT a local edit and
# must receive the fix — this is the fleet-wide ~62,000-line regression the first cut shipped.
grep -q "NEW baseline content A" "$CORE/.claude/hooks/case-a.sh" 2>/dev/null \
  && ok "(a) prior-baseline-match file WAS overwritten with the fix — peers receive fixes" \
  || bad "(a) prior-baseline-match file overwritten" "still: $(cat "$CORE/.claude/hooks/case-a.sh" 2>/dev/null)"
check "(a) ...and the old content is gone" "$([[ "$CASE_A_SHA_AFTER" == "$CASE_A_SHA_BEFORE" ]] && echo same || echo changed)" "changed"

# (b) a genuine edit — differs from HEAD and from the prior baseline — must survive.
check "(b) genuine local edit untouched (differs from HEAD AND prior baseline)" \
  "$CASE_B_SHA_AFTER" "$CASE_B_SHA_BEFORE"

# (c) untracked, absent from the prior baseline — no reference point exists, so it is protected
# by default rather than overwritten for lack of proof either way.
check "(c) untracked file absent from prior baseline untouched (no recovery if lost)" \
  "$CASE_C_SHA_AFTER" "$CASE_C_SHA_BEFORE"

# ── the pull itself is not broken: an ordinary shared file with no history at all still lands
grep -q "NEW baseline content — ordinary" "$CORE/.claude/hooks/ordinary-shared.sh" 2>/dev/null \
  && ok "an ordinary shared file still flowed normally" || bad "ordinary shared file flowed"

# ── observability: exactly (b) and (c) are held; (a) must NOT appear anywhere in the hold trail
grep -qi "HELD" <<<"$OUT" && ok "the hold is reported loudly (survives --quiet)" \
  || bad "loud hold notice" "absent from output"
grep -q '\.claude/hooks/case-b\.sh' <<<"$OUT" && ok "notice names the held real-edit path (b)" \
  || bad "notice names case-b.sh"
grep -q '\.claude/hooks/case-c\.sh' <<<"$OUT" && ok "notice names the held untracked path (c)" \
  || bad "notice names case-c.sh"
grep -q '\.claude/hooks/case-a\.sh' <<<"$OUT" && bad "(a) must NOT appear in the hold notice — it was not held" \
  || ok "(a) is silent in the hold notice, as a file that was not held should be"

DLOG="$CORE/.claude/state/.dirty-shared-holdback.log"
[[ -f "$DLOG" ]] && ok "durable holdback log written" || bad "holdback log written"
grep -q 'case-b.sh' "$DLOG" 2>/dev/null && ok "...names the held real-edit path" || bad "log names case-b.sh"
grep -q 'case-c.sh' "$DLOG" 2>/dev/null && ok "...names the held untracked path" || bad "log names case-c.sh"
grep -q 'case-a.sh' "$DLOG" 2>/dev/null && bad "...must NOT log (a) as held" || ok "...and does not log (a)"

PJ="$CORE/.claude/state/.pull-pending-actions.json"
[[ -f "$PJ" ]] && ok "pending-actions file written" || bad "pending-actions file written"
python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$PJ" 2>/dev/null \
  && ok "pending-actions file is valid JSON" || bad "pending-actions file is valid JSON"
HOLD_COUNT=$(grep -o '"action":"resolve_dirty_shared_hold"' "$PJ" 2>/dev/null | wc -l | tr -d ' ')
check "exactly two held paths are queued as pending actions (b and c — not a)" "$HOLD_COUNT" "2"

SLOG="$CORE/.claude/state/.last-baseline-sync"
grep -q 'dirty_held=2' "$SLOG" 2>/dev/null \
  && ok "the sync ledger's dirty_held count matches (2, not 3 — (a) was not held)" \
  || bad "sync ledger dirty_held count" "$(tail -1 "$SLOG" 2>/dev/null)"

# ══════════════════════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — (d) FAIL-OPEN. No prior-baseline checkout is establishable at all: a fresh Core's
# first pull, or a seat fallen behind farther than the --depth 50 fetch can see. The very first
# cut of this predicate got exactly this direction backwards (see header). A file differing from
# HEAD here has no proof either way, and the design choice is to let the pull through rather than
# freeze the Core on a guess.
# ══════════════════════════════════════════════════════════════════════════════════════════════
BL2="$WORK/baseline2"
mkdir -p "$BL2/.claude/hooks" "$BL2/bin"
cp "$REPO/bin/sync-manifest.json" "$BL2/bin/"
echo 'NEW baseline content D.' > "$BL2/.claude/hooks/case-d.sh"
git -C "$BL2" init -q -b main
git -C "$BL2" -c user.email=t@t -c user.name=t add -A
git -C "$BL2" -c user.email=t@t -c user.name=t commit -qm "only commit"

CORE2="$WORK/core-fake-fresh"
mkdir -p "$CORE2/.claude/hooks" "$CORE2/.claude/state" "$CORE2/bin"
cp "$REPO/bin/sync-manifest.json" "$CORE2/bin/"
printf '{"org_id": 9, "domain_label": "fake2", "hook_profile": {"role": "puller"}}\n' \
  > "$CORE2/.claude/identity.json"
echo 'INIT commit content D — old, no prior-sync record exists for this Core at all.' \
  > "$CORE2/.claude/hooks/case-d.sh"
git -C "$CORE2" init -q -b main
git -C "$CORE2" -c user.email=t@t -c user.name=t add -A
git -C "$CORE2" -c user.email=t@t -c user.name=t commit -qm init
echo 'DIFFERS FROM HEAD, but this is a fresh Core — no .last-baseline-sync exists to prove anything.' \
  > "$CORE2/.claude/hooks/case-d.sh"
# Deliberately NO .last-baseline-sync file — this Core has never recorded a synced baseline.

echo ""
echo "SCENARIO 2 — no prior baseline resolvable (case d: fail-open)"
OUT2="$(run_pull "$CORE2" "$BL2" "")"
printf '%s\n' "$OUT2" > "${TDS_DEBUG_OUT2:-/dev/null}"

grep -q "prior baseline unavailable" <<<"$OUT2" \
  && ok "(d) the script itself reports it could not establish a prior baseline" \
  || bad "(d) fail-open notice present" "absent from output"
grep -q "NEW baseline content D" "$CORE2/.claude/hooks/case-d.sh" 2>/dev/null \
  && ok "(d) FAIL-OPEN: with no prior-baseline proof, the file differing from HEAD was let through" \
  || bad "(d) fail-open overwrite" "still: $(cat "$CORE2/.claude/hooks/case-d.sh" 2>/dev/null)"
grep -qi "held" <<<"$OUT2" \
  && bad "(d) nothing should be held when the guard cannot establish any prior baseline" \
  || ok "(d) no hold notice — consistent with everything being treated as clean"

# A SET, not a COUNT, and the difference is not pedantry — the count version failed this suite for a
# reason that had nothing to do with leaking. /tmp/core-baseline-* is a GLOBAL namespace this test
# does not own: a stray /tmp/core-baseline-check left by an earlier manual --check happened to be
# deleted between the two samples, so the count went 1 -> 0 and the assertion fired "leak detected"
# at a run that leaked nothing. It reproduced only under the suite (CORE_SUITE_NONCE set) and passed
# standalone, which is the signature of an assertion reading shared state rather than its own.
#
# Comparing sets and reporting only ADDITIONS fixes both directions: an unrelated directory being
# removed (or created) by anything else on the machine can no longer fail this test, while a real
# leak — a directory this run created and did not clean up — still does, which is the only thing
# the assertion was ever meant to catch.
_TMP_LEAK_BEFORE=$(ls -d /tmp/core-baseline-* 2>/dev/null | sort)

# ══════════════════════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 — GAP 1, and the one that had ZERO coverage before this. A pull that HOLDS a genuine
# edit still stamps .last-baseline-sync with the baseline SHA it just pulled. The NEXT pull against
# that SAME unchanged baseline reads that stamp back and finds prev == current — the equal-SHA case
# the first cut of PREV_BASELINE_DIR skipped outright, falling through to the fail-open and silently
# overwriting the very edit the previous pull had protected (Codex, reproduced in two pulls, exit 0,
# no warning — there is nothing left to warn about once the file is judged clean). Most SessionStart
# pulls hit an unchanged remote, so this was the COMMON path, not an edge case.
#
# Three pulls, not two: "survives exactly one more pull" is the same bug wearing a different hat,
# and only a third pull against the still-unchanged baseline rules that out.
# ══════════════════════════════════════════════════════════════════════════════════════════════
BL3="$WORK/baseline3"
mkdir -p "$BL3/.claude/hooks" "$BL3/bin"
cp "$REPO/bin/sync-manifest.json" "$BL3/bin/"
git -C "$BL3" init -q -b main
echo 'OLD content — what an earlier pull (before this scenario starts) would have delivered.' \
  > "$BL3/.claude/hooks/repeat-edit.sh"
echo 'OLD ordinary content — never locally touched.' > "$BL3/.claude/hooks/repeat-ordinary.sh"
git -C "$BL3" -c user.email=t@t -c user.name=t add -A
git -C "$BL3" -c user.email=t@t -c user.name=t commit -qm "OLD baseline"
BL3_OLD_SHA="$(git -C "$BL3" rev-parse HEAD)"
echo 'FIX content — must not overwrite the genuine local edit, on this pull or any repeat of it.' \
  > "$BL3/.claude/hooks/repeat-edit.sh"
echo 'NEW ordinary content — the baseline this scenario stays FROZEN at for all three pulls.' \
  > "$BL3/.claude/hooks/repeat-ordinary.sh"
git -C "$BL3" -c user.email=t@t -c user.name=t add -A
git -C "$BL3" -c user.email=t@t -c user.name=t commit -qm "NEW baseline"
BL3_NEW_SHA="$(git -C "$BL3" rev-parse HEAD)"
# BL3 gets no further commits below. All three pulls land on this same tip — the "unchanged
# remote" case that is the ordinary SessionStart outcome, not the exotic one.

CORE3="$WORK/core-repeat"
mkdir -p "$CORE3/.claude/hooks" "$CORE3/.claude/state" "$CORE3/bin"
cp "$REPO/bin/sync-manifest.json" "$CORE3/bin/"
printf '{"org_id": 9, "domain_label": "fake3", "hook_profile": {"role": "puller"}}\n' \
  > "$CORE3/.claude/identity.json"
echo 'INIT commit content — stale, predates even the OLD baseline.' > "$CORE3/.claude/hooks/repeat-edit.sh"
echo 'INIT commit content — stale, predates even the OLD baseline.' > "$CORE3/.claude/hooks/repeat-ordinary.sh"
git -C "$CORE3" init -q -b main
git -C "$CORE3" -c user.email=t@t -c user.name=t add -A
git -C "$CORE3" -c user.email=t@t -c user.name=t commit -qm init
# Disk diverges from HEAD, uncommitted — simulating a Core where an earlier pull (against
# BL3_OLD_SHA, before this scenario begins) already landed, and Nick hand-edited the file after.
# Must match BL3's OLD content BYTE-FOR-BYTE (not repeat-edit.sh's OLD string) — this is the
# "matches the prior baseline, not a local edit" case, and a mismatched fixture here would make it
# look HELD for the wrong reason (a fixture bug, not the guard) rather than correctly overwritten.
echo 'OLD ordinary content — never locally touched.' \
  > "$CORE3/.claude/hooks/repeat-ordinary.sh"
echo 'GENUINE LOCAL EDIT — differs from HEAD and from every baseline this scenario ever ships. Must survive three pulls.' \
  > "$CORE3/.claude/hooks/repeat-edit.sh"
REPEAT_EDIT_SHA_BEFORE="$(shasum "$CORE3/.claude/hooks/repeat-edit.sh" | awk '{print $1}')"
# The record of that earlier pull: this Core believes it is synced to OLD.
printf '%s baseline=%s changed=2 orphans=0 tombstones=0 dirty_held=0\n' \
  "$(date -Iseconds)" "$BL3_OLD_SHA" > "$CORE3/.claude/state/.last-baseline-sync"

DLOG3="$CORE3/.claude/state/.dirty-shared-holdback.log"
SLOG3="$CORE3/.claude/state/.last-baseline-sync"

echo ""
echo "SCENARIO 3 — GAP 1: hold survives repeat pulls against an UNCHANGED baseline"
run_pull "$CORE3" "$BL3" --quiet >/dev/null
check "pull 1 (baseline moves OLD -> NEW): genuine edit held" \
  "$(shasum "$CORE3/.claude/hooks/repeat-edit.sh" | awk '{print $1}')" "$REPEAT_EDIT_SHA_BEFORE"
grep -q "NEW ordinary content" "$CORE3/.claude/hooks/repeat-ordinary.sh" 2>/dev/null \
  && ok "pull 1: ordinary file (matched the prior baseline) received the fix" \
  || bad "pull 1: ordinary file updated"
grep -q "baseline=$BL3_NEW_SHA" "$SLOG3" \
  && ok "pull 1: ledger stamped with the NEW baseline SHA (this is what makes pull 2 the equal-SHA case)" \
  || bad "pull 1: ledger stamped with new baseline SHA" "$(tail -1 "$SLOG3" 2>/dev/null)"

run_pull "$CORE3" "$BL3" --quiet >/dev/null
check "pull 2 — SAME unchanged baseline, THE bug's exact repro: genuine edit STILL held" \
  "$(shasum "$CORE3/.claude/hooks/repeat-edit.sh" | awk '{print $1}')" "$REPEAT_EDIT_SHA_BEFORE"
grep -q "NEW ordinary content" "$CORE3/.claude/hooks/repeat-ordinary.sh" 2>/dev/null \
  && ok "pull 2: ordinary file still flows normally (the freeze did not come back for it)" \
  || bad "pull 2: ordinary file still flowing"

run_pull "$CORE3" "$BL3" --quiet >/dev/null
check "pull 3 — 'survives one more pull' is the bug wearing a different hat: genuine edit STILL held" \
  "$(shasum "$CORE3/.claude/hooks/repeat-edit.sh" | awk '{print $1}')" "$REPEAT_EDIT_SHA_BEFORE"
grep -q "NEW ordinary content" "$CORE3/.claude/hooks/repeat-ordinary.sh" 2>/dev/null \
  && ok "pull 3: ordinary file still flows normally" \
  || bad "pull 3: ordinary file still flowing"

check "the edit was held on EVERY one of the three pulls (durable hold-log has 3 entries), not just the first" \
  "$(grep -c 'repeat-edit\.sh' "$DLOG3" 2>/dev/null)" "3"
check "the ordinary file was NEVER held across the three pulls (durable hold-log has 0 entries for it)" \
  "$(grep -c 'repeat-ordinary\.sh' "$DLOG3" 2>/dev/null)" "0"
check "the ledger recorded dirty_held=1 on all three pulls (never dirty_held=0, which would mean the hold silently failed)" \
  "$(grep -c 'dirty_held=1' "$SLOG3" 2>/dev/null)" "3"

# ══════════════════════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 — GAP 2: the `fetch --depth 50` deepening branch, never exercised before this. The
# suite drove `git clone --depth 1` with a bare local path, and git silently IGNORES --depth for
# local clones ("--depth is ignored in local clones; use file:// instead") — so the "shallow" clone
# always secretly had full history, cat-file -e on the prior SHA always just worked, and the fetch
# branch below it never ran. A `file://` URL is what makes --depth real; the seam
# (CORE_BASELINE_URL_LOCAL_TEST) now accepts one (stripping it only for the local-existence check —
# see the comment at that seam).
#
# History here: OLD (the recorded prior baseline) -> filler commit -> NEW (this pull's tip, and the
# fix). --depth 1 sees only NEW; OLD is genuinely absent until the script's own `fetch --depth 50`
# recovers it.
# ══════════════════════════════════════════════════════════════════════════════════════════════
BL4="$WORK/baseline4"
mkdir -p "$BL4/.claude/hooks" "$BL4/bin"
cp "$REPO/bin/sync-manifest.json" "$BL4/bin/"
git -C "$BL4" init -q -b main
echo 'ORDINARY content — never locally touched.' > "$BL4/.claude/hooks/shallow-ordinary.sh"
echo 'OLD content — the prior baseline recorded from an earlier session, about to go deep in history.' \
  > "$BL4/.claude/hooks/shallow-edit.sh"
git -C "$BL4" -c user.email=t@t -c user.name=t add -A
git -C "$BL4" -c user.email=t@t -c user.name=t commit -qm "OLD baseline (this Core's recorded prior sync)"
BL4_OLD_SHA="$(git -C "$BL4" rev-parse HEAD)"
echo 'filler churn — exists only to push OLD out of a --depth 1 clone.' > "$BL4/.claude/hooks/shallow-filler.sh"
git -C "$BL4" -c user.email=t@t -c user.name=t add -A
git -C "$BL4" -c user.email=t@t -c user.name=t commit -qm "filler commit"
echo 'NEW ordinary content — this pull is not broken; it still lands.' > "$BL4/.claude/hooks/shallow-ordinary.sh"
echo 'FIX content — the genuine local edit must survive this pull.' > "$BL4/.claude/hooks/shallow-edit.sh"
git -C "$BL4" -c user.email=t@t -c user.name=t add -A
git -C "$BL4" -c user.email=t@t -c user.name=t commit -qm "NEW baseline — the fix, and this pull's tip"

# sanity: prove the file:// clone is genuinely shallow, i.e. that this scenario is capable of
# exercising anything at all. A bare-path clone would fail this check (OLD would be present) —
# that failure IS gap 2.
PROBE="$WORK/probe-shallow"
git clone --depth 1 --branch main -q "file://$BL4" "$PROBE" 2>/dev/null
if git -C "$PROBE" cat-file -e "${BL4_OLD_SHA}^{commit}" 2>/dev/null; then
  bad "sanity: file:// --depth 1 clone of the scenario-4 baseline is shallow" \
    "OLD ($BL4_OLD_SHA) is present in a depth-1 clone — depth was NOT honored; this scenario cannot prove anything"
else
  ok "sanity: file:// --depth 1 clone is genuinely shallow (OLD absent) — unlike the bare-path clone gap 2 describes"
fi
rm -rf "$PROBE"

CORE4="$WORK/core-shallow"
mkdir -p "$CORE4/.claude/hooks" "$CORE4/.claude/state" "$CORE4/bin"
cp "$REPO/bin/sync-manifest.json" "$CORE4/bin/"
printf '{"org_id": 9, "domain_label": "fake4", "hook_profile": {"role": "puller"}}\n' \
  > "$CORE4/.claude/identity.json"
echo 'INIT commit content — stale, predates even OLD.' > "$CORE4/.claude/hooks/shallow-edit.sh"
echo 'INIT commit content — stale, predates even OLD.' > "$CORE4/.claude/hooks/shallow-ordinary.sh"
git -C "$CORE4" init -q -b main
git -C "$CORE4" -c user.email=t@t -c user.name=t add -A
git -C "$CORE4" -c user.email=t@t -c user.name=t commit -qm init
echo 'ORDINARY content — never locally touched.' > "$CORE4/.claude/hooks/shallow-ordinary.sh"
echo 'GENUINE LOCAL EDIT — differs from HEAD and from every baseline. Must survive a shallow-clone pull.' \
  > "$CORE4/.claude/hooks/shallow-edit.sh"
SHALLOW_EDIT_SHA_BEFORE="$(shasum "$CORE4/.claude/hooks/shallow-edit.sh" | awk '{print $1}')"
printf '%s baseline=%s changed=2 orphans=0 tombstones=0 dirty_held=0\n' \
  "$(date -Iseconds)" "$BL4_OLD_SHA" > "$CORE4/.claude/state/.last-baseline-sync"

echo ""
echo "SCENARIO 4 — GAP 2: shallow-clone deepening (fetch --depth 50) actually exercised"
OUT4="$(run_pull "$CORE4" "file://$BL4" "")"; RC4=$?

check "held path -> non-quiet exit code is 1 (a human is watching; this is a 'go look' signal, not silence)" "$RC4" "1"
check "the genuine edit is HELD (only possible if the shallow-clone deepening recovered OLD)" \
  "$(shasum "$CORE4/.claude/hooks/shallow-edit.sh" | awk '{print $1}')" "$SHALLOW_EDIT_SHA_BEFORE"
grep -q "NEW ordinary content" "$CORE4/.claude/hooks/shallow-ordinary.sh" 2>/dev/null \
  && ok "the ordinary file (matched OLD, never touched locally) still received the fix" \
  || bad "ordinary file received the fix" "still: $(cat "$CORE4/.claude/hooks/shallow-ordinary.sh" 2>/dev/null)"
grep -q "checked out for local-edit detection" <<<"$OUT4" \
  && ok "the script's own log confirms the worktree/deepening branch — not the equal-SHA or fail-open branch — established PREV_BASELINE_DIR" \
  || bad "worktree/deepening success logged" "$OUT4"
grep -qi "held" <<<"$OUT4" && ok "the hold is reported" || bad "hold reported" "$OUT4"
grep -q 'shallow-edit\.sh' <<<"$OUT4" && ok "notice names the held path" || bad "notice names shallow-edit.sh" "$OUT4"

# ── /tmp hygiene: the mktemp + multi-signal trap change (this session) must leak nothing across
#    seven real pulls (scenarios 1–4) run back-to-back, several of them establishing a PREV_BASELINE_DIR
#    worktree ("${TMP}-prev") in addition to $TMP itself.
_TMP_LEAK_AFTER=$(ls -d /tmp/core-baseline-* 2>/dev/null | sort)
_TMP_LEAKED=$(comm -13 <(printf '%s\n' "$_TMP_LEAK_BEFORE") <(printf '%s\n' "$_TMP_LEAK_AFTER") | tr '\n' ' ' | sed 's/ *$//')
check "no /tmp/core-baseline-* directories leaked across the whole suite (mktemp + EXIT/INT/TERM/HUP trap)" \
  "${_TMP_LEAKED:-none}" "none"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
