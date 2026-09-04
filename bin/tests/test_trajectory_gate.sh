#!/usr/bin/env bash
# End-to-end proof that the trajectory gate DISCRIMINATES — not that it runs.
#
# A gate that always says REVERT is as useless as one that always says KEEP, and both look like a
# working gate from one side. So this builds a MATCHED PAIR: two candidates with the SAME declared
# target and the SAME genuine improvement, differing only in that one also breaks something it did
# not declare. Anything less than a matched pair cannot tell "the gate works" from "the gate is
# stuck".
#
# That case is exactly the one the design panel's adversary said the top-ranked architecture could
# NOT distinguish: "a good fix and a bad change both return KEEP, because the untargeted regression
# check is scored against already-recorded history."
#
# SLOW ON PURPOSE (~10-15 min): each verdict materialises two trees and runs the full casebook
# against each. Not wired into run-all.sh for that reason — run it when the gate changes.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 1
PASS=0; FAIL=0

# ── DIRTY-TREE CHECK — ABOVE THE TRAP, AND THAT PLACEMENT IS THE ENTIRE FIX ──────────────────────
#
# This check already existed and was already correct. It sat BELOW `trap cleanup EXIT`, so on the
# SKIP path the trap still fired and `cleanup()` ran `git checkout -q -f "$START_BRANCH"` — REPO-WIDE
# FORCE — destroying the very uncommitted work the check had just detected and declined to run
# against.
#
# A GUARD PLACED AFTER ITS OWN CLEANUP TRAP CANNOT PROTECT ANYTHING. It correctly identifies the
# condition, exits, and the exit itself does the damage.
#
# Measured on 2026-08-10: three verified fixes — two to bin/trajectory-gate.py, one to
# test_sentinel_approval_chain.sh — were applied, dosed, confirmed working, and silently reverted by
# running the shell suite before committing. The commit that followed announced two of them and
# contained neither; core-business caught it with `git show --stat`. I diagnosed it as "the test uses
# -f" and shipped a SECOND guard higher up, which worked by accident of placement while leaving two
# mechanisms for one job. This is the consolidation.
#
# .claude/state/** churns on literally every turn, so a plain dirty check skips forever and a
# plain checkout ABORTS mid-test ("local changes would be overwritten"). Measured both. Ignore the
# churn, refuse only on real edits — the distinction the Tier B router had to learn an hour ago.
# bin/.gate-trusted-sha IS PER-SEAT STATE, not source. `--promote` rewrites it on a normal run of
# this very test, so leaving it out made the check fire on its own side effect. It was carved out of
# the sync manifest for the same reason (it holds a git SHA local to one Core's history), and
# gate_tier_b.py's NOISE_PREFIXES already groups it with .claude/state/, sessions/ and memory/ —
# core-business's point that the SCORER treated it as per-seat state while the MANIFEST treated it as
# shared code. This is the third list that had to agree with the other two.
# T057, 2026-08-17 — memory/ AND sessions/ REMOVED FROM THIS EXCLUSION. They were data loss.
#
# This list was written to mean "ignore churn when deciding whether the tree is clean enough to
# run." Its ACTUAL effect is the inverse, because everything downstream is `git checkout -q -f`:
# a path excluded here is not ignored, it is DESTROYED WITHOUT WARNING. The list did not mark
# files safe to skip over; it marked them safe to overwrite.
#
# core-ops lost four tracked files to this on 2026-08-17 — memory/pending.md (a P0 write-up) plus
# three manifests. The guard saw a "clean" tree precisely BECAUSE the only dirty paths were ones it
# had been told not to look at. Reflog signature confirmed independently on ops, business and life.
#
# .claude/state/ and bin/.gate-trusted-sha STAY excluded, and that part was always right:
# .gate-trusted-sha is per-seat state THIS TEST REWRITES via --promote, so including it made the
# check fire on its own side effect. That is genuine self-caused churn.
#
# memory/ and sessions/ are Nick's writing. They are not churn, and nothing about this test causes
# them to be dirty. Excluding them bought nothing and cost four files.
DIRTY="$(git status --porcelain | grep -vE '^.. (\.claude/state/|bin/\.gate-trusted-sha)' || true)"
if [[ -n "$DIRTY" ]]; then
  # A DIRTY TREE MUST NOT MEAN THIS TEST NEVER RUNS.
  #
  # It used to exit 0 here. During active work the tree is ALWAYS dirty, so the most
  # security-relevant test in the suite — the one proving a candidate cannot become its own grader —
  # effectively never executed. That is the exact "a skipped test reads as green" failure the runner
  # was built to expose, still present in the runner's own suite, and it skipped on nearly every run
  # today while I quoted the suite as green.
  #
  # It refuses the live tree for a real reason: it commits on scratch branches and `git checkout`
  # aborts on local changes. So run it somewhere with no local changes — a CLONE of HEAD. The gate
  # under test is the code at HEAD, which is exactly what a clone gives, and nothing here needs the
  # working-tree edits.
  #
  # Guarded against recursion: the clone runs this same script, and without the marker it would
  # clone itself forever.
  # THE GUARD MUST BE SOMETHING ONLY THIS SCRIPT CAN PRODUCE.
  #
  # It was a PRESENCE check on GATE_TEST_IN_CLONE, and core-business showed that an inherited value
  # plus a non-excluded dirty file makes the most security-relevant test exit 0 HAVING RUN NOTHING.
  # A presence check cannot tell its own recursive child from a variable that leaked in — and env
  # leakage across seats is not hypothetical in this fleet: CORE_ORG_ID leaked into my tree twice
  # today and produced a false failure both times.
  #
  # So the guard is now a NONCE this run generates and writes INTO the clone. A child is recognised
  # only when the value it inherited matches the file its own parent wrote; anything else is treated
  # as a fresh run, which fails toward RUNNING the test rather than skipping it.
  # THE NONCE MUST NOT LIVE IN THE WORKTREE. 2026-08-12: it did, at $(pwd)/.gate-test-nonce, and
  # that is precisely what made the clone dirty. The parent wrote the marker INTO the clone to
  # identify its own child; the marker is untracked, so `git status` in the child reported dirty;
  # the child then matched the nonce and hit the "should be impossible" branch and SKIPPED.
  #
  # So the file whose only job was to recognise the child is what guaranteed the child never ran.
  # The test could not execute whenever the parent tree was dirty — which is most of the time, and
  # it reported that as a SKIP rather than a failure. run-all.sh counts a SKIP as non-green on
  # purpose, so this was visible; but "SKIP — should be impossible" reads like an edge case rather
  # than "this test has never run here", and it survived that way.
  #
  # .git/ is inside the repo and outside the worktree: git never reports it as a change. Reading the
  # legacy worktree path too, so a clone made by an older parent is still recognised.
  _NONCE_FILE="$(git rev-parse --git-dir 2>/dev/null)/gate-test-nonce"
  if [[ -n "${GATE_TEST_NONCE:-}" ]] \
     && { [[ -f "$_NONCE_FILE" && "$(cat "$_NONCE_FILE" 2>/dev/null)" == "${GATE_TEST_NONCE}" ]] \
       || [[ -f "$(pwd)/.gate-test-nonce" && "$(cat "$(pwd)/.gate-test-nonce" 2>/dev/null)" == "${GATE_TEST_NONCE}" ]]; }; then
    echo "  SKIP — clone is itself dirty, which should be impossible; not recursing further"
    exit 0
  fi
  _CLONE="$(mktemp -d)"
  if git clone --quiet --no-hardlinks . "$_CLONE" 2>/dev/null; then
    echo "  tree is dirty — running against a CLONE of HEAD instead of skipping ($_CLONE)"
    [[ -f bin/.gate-trusted-sha ]] && cp bin/.gate-trusted-sha "$_CLONE/bin/.gate-trusted-sha" 2>/dev/null
    _NONCE="$$-$(date +%s)-${RANDOM}"
    # Into .git/, NOT the worktree — writing it to $_CLONE/.gate-test-nonce is what made every
    # clone dirty and turned this whole branch into a permanent SKIP.
    printf '%s' "$_NONCE" > "$_CLONE/.git/gate-test-nonce"
    GATE_TEST_NONCE="$_NONCE" CORE_INSTANCE="$_CLONE" bash "$_CLONE/bin/tests/test_trajectory_gate.sh"
    _rc=$?
    rm -rf "$_CLONE" 2>/dev/null
    exit $_rc
  fi
  rm -rf "$_CLONE" 2>/dev/null
  echo "  SKIP — real uncommitted changes present and the clone fallback failed:"
  sed -n '1,5p' <<<"$DIRTY"
  exit 0
fi

START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
SAVED_TRUST="$(cat bin/.gate-trusted-sha 2>/dev/null || true)"

# LEAVE .sentinel-last-blocked AS FOUND. Grading a candidate runs the REAL pretooluse-guard, whose
# STATE_DIR is derived from the hook's own location — deliberately, so no environment variable can
# move it — which means this test cannot be redirected away from the live seat and must instead
# clean up after itself. It matters because the file it overwrites is READ BY REVIEWERS: core-business
# once found a bypass-shaped fixture there, left by a PASSING guard test, and read it as an attack by
# the Core under review. A test that fabricates evidence of an attack is worse than one that fails.
# THE MARKER SET IS PART OF WHAT MUST BE LEFT AS FOUND. Grading a candidate runs the real close
# path, which drops a `.friction-installed-<hash>` marker into the seat's live state — keyed by
# CONTENT, so it is NOT idempotent: each graded candidate leaves a NEW one. Two appeared from a
# single run of this test. Caught by run-all.sh's state digest, which fired on the first run after
# every commit and then went quiet, because an already-present marker is not a change.
#
# Only markers this run CREATED are removed; anything present beforehand is left alone.
SAVED_MARKERS_F="${TMPDIR:-/tmp}/.gate-saved-markers.$$"
ls .claude/state/.friction-installed-* 2>/dev/null | sort > "$SAVED_MARKERS_F" || true

SAVED_BLOCKED_F="${TMPDIR:-/tmp}/.gate-saved-blocked.$$"
if [[ -f .claude/state/.sentinel-last-blocked ]]; then
  cp .claude/state/.sentinel-last-blocked "$SAVED_BLOCKED_F" 2>/dev/null || true
fi

cleanup() {
  git checkout -q -f "$START_BRANCH" 2>/dev/null
  # _gate_base was missing here, so every run leaked one branch; core-business found a live one at
  # 714a1f3. A cleanup that names two of the three things it creates is worse than none, because the
  # tree looks tended.
  git branch -D _gate_good _gate_bad _gate_base >/dev/null 2>&1
  [[ -n "$SAVED_TRUST" ]] && printf '%s\n' "$SAVED_TRUST" > bin/.gate-trusted-sha
  if [[ -f "$SAVED_MARKERS_F" ]]; then
    ls .claude/state/.friction-installed-* 2>/dev/null | sort |
      comm -13 "$SAVED_MARKERS_F" - 2>/dev/null |
      while IFS= read -r _m; do [[ -n "$_m" ]] && rm -f "$_m"; done
    rm -f "$SAVED_MARKERS_F" 2>/dev/null
  fi
  if [[ -f "$SAVED_BLOCKED_F" ]]; then
    cp "$SAVED_BLOCKED_F" .claude/state/.sentinel-last-blocked 2>/dev/null || true
    rm -f "$SAVED_BLOCKED_F" 2>/dev/null
  else
    rm -f .claude/state/.sentinel-last-blocked 2>/dev/null
  fi
}
trap cleanup EXIT


# WHERE THE LAST GATE RUN'S OUTPUT GOES, so a failing arm can say WHY.
#
# verdict_of runs inside `$( )`, so its stdout IS the verdict and a global assignment cannot escape
# the subshell — the reason a failing arm printed nothing but "wanted KEEP, got REVERT". On
# 2026-08-10 that cost four probe attempts and the diagnosis was still not reached: the test could
# detect the regression and could not describe it. A check that reports a symptom it cannot explain
# hands the next reader an investigation instead of a finding.
#
# A file, not a variable, for exactly the subshell reason above.
GATE_OUT_FILE="${TMPDIR:-/tmp}/.gate-last-output.$$"

verdict_of() {   # sha -> KEEP | REVERT | ERROR
  local out
  out="$(python3 bin/trajectory-gate.py --candidate "$1" 2>&1)"
  printf '%s\n' "$out" > "$GATE_OUT_FILE"
  # TWO VERDICT SHAPES, AND THE TEST ONLY KNEW ONE. bin/trajectory-gate.py prints the banner
  # "  TRAJECTORY GATE — <verdict>" at line 557 for a GRADED verdict, but every EARLY-EXIT refusal
  # (lines 196, 210, 219, 225, 544 — no trust root, unstampable frozen tree, no such commit) prints
  # a bare "REVERT — <reason>" and returns before the banner exists.
  #
  # So a correct, deliberate REVERT was read as ERROR. Verified directly: with the trust root moved
  # away the gate prints "REVERT — no trust root…" and this matcher scored it ERROR, which is the
  # test misreading the gate rather than the gate misbehaving — the same shape as the approval-chain
  # fixture encoding a superseded output contract.
  if grep -q "TRAJECTORY GATE — KEEP" <<<"$out"; then echo KEEP
  elif grep -q "TRAJECTORY GATE — REVERT" <<<"$out" || grep -qE '^REVERT — ' <<<"$out"; then echo REVERT
  else echo ERROR; fi
}

check() {        # label, want, got
  if [[ "$2" == "$3" ]]; then printf '  PASS  %s (%s)\n' "$1" "$3"; PASS=$((PASS+1))
  else
    printf '  FAIL  %s — wanted %s, got %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1))
    # PRINT THE GATE'S OWN REASONING. Without this the failure is "wanted KEEP, got REVERT" and the
    # reader has to re-run the gate by hand to learn anything — which is what happened, four times.
    if [[ -s "$GATE_OUT_FILE" ]]; then
      printf '        ---- what the gate actually said ----\n'
      sed 's/^/        /' "$GATE_OUT_FILE"
      printf '        ---- end ----\n'
    fi
  fi
}

echo
echo "=== building a matched pair on scratch branches ==="

# THE PAIR PLANTS ITS OWN BASELINE. It used to "fix one real S3 defect" already present in
# memory/access-log.md, using its OWN regex to find the line. Two problems, both live today:
#
#   1. S3 PASSES on a clean tree — s3_violations() reports zero — so there was no defect to fix and
#      the candidate improved nothing. Verdict: "no declared target improved", REVERT, and the
#      matched pair could not be built at all.
#   2. The fixture's matcher and the ITEM's matcher disagreed. The fixture flagged a line the item
#      skips (a `- **Why:** …` sub-field), so it "fixed" something that was never a finding. TWO
#      INSTRUMENTS ON ONE SUBJECT, inside the gate's own test.
#
# So the base commit PLANTS a defect the item genuinely flags, and the candidates fix it. That makes
# the pair self-contained: it no longer depends on the repo happening to be broken in the right way.
git checkout -q -f -B _gate_base
printf -- '- 2026-01-01 | WRITE to memory/fixture.md | gate test fixture, no completion stamp\n' \
  >> memory/access-log.md
git commit -q -am "test-fixture: plant one S3 defect for the matched pair

Casebook-Target: S3
"
BASE_WITH_DEFECT="$(git rev-parse HEAD)"

# Sanity: the ITEM must actually flag what we planted, or the pair proves nothing. Uses the SHARED
# matcher the item uses, never a second copy of the rule.
if ! python3 -c "
import sys; sys.path.insert(0, '.claude/hooks')
from casebook_matchers import s3_violations
n = len(s3_violations(open('memory/access-log.md').read()))
sys.exit(0 if n else 1)
"; then
  echo "  SETUP FAILED — the planted S3 defect is not flagged by the item's own matcher." >&2
  exit 2
fi

# GOOD: fixes exactly that defect, declaring S3. Nothing else changes.
git checkout -q -f -B _gate_good
python3 - <<'PY'
import pathlib
p = pathlib.Path("memory/access-log.md")
lines = p.read_text().splitlines(keepends=True)
for i, ln in enumerate(lines):
    if "gate test fixture, no completion stamp" in ln:
        lines[i] = ln.rstrip("\n") + " — done.\n"
        break
p.write_text("".join(lines))
PY
# THE FIX IS ASSERTED TOO, closing core-business's noted asymmetry. Its reasoning is why this is
# symmetry rather than a hole, and worth keeping: an UNFLAGGED PLANT makes the test vacuous and
# SILENT — it passes while proving nothing — whereas an UNCLEARED FIX makes it FAIL LOUDLY, because
# there is no improvement, so no KEEP, so the assertion breaks and someone looks. Guard the silent
# direction; the loud one guards itself. Both are guarded now because the second one is cheap.
if ! python3 -c "
import sys; sys.path.insert(0, '.claude/hooks')
from casebook_matchers import s3_violations
n = len(s3_violations(open('memory/access-log.md').read()))
sys.exit(1 if n else 0)
"; then
  echo "  SETUP FAILED — GOOD's edit did not clear the planted violation, so it is not an" >&2
  echo "                 improvement and a KEEP would be right for the wrong reason." >&2
  exit 2
fi

git commit -q -am "test-fixture: fix the planted S3 defect

Casebook-Target: S3
"
GOOD="$(git rev-parse HEAD)"

# BAD: the SAME fix, plus a claim that a retired hook enforces something — a regression on S1,
# which the candidate does not declare.
#
# BRANCHED FROM THE PLANTED BASE, NOT FROM GOOD. It used to branch from whatever was checked out,
# which was _gate_good — so BAD's parent already contained the S3 fix and the diff the gate saw was
# the REGRESSION ALONE. It still returned REVERT, so the assertion passed, but it was not testing a
# matched pair: the whole point is two candidates with the SAME improvement differing only in the
# undeclared side effect, and "anything less than a matched pair cannot tell 'the gate works' from
# 'the gate is stuck'" — this file's own opening paragraph.
git checkout -q -f -B _gate_bad "$BASE_WITH_DEFECT"
python3 - <<'PY'
import pathlib
p = pathlib.Path("memory/access-log.md")
lines = p.read_text().splitlines(keepends=True)
for i, ln in enumerate(lines):
    if "gate test fixture, no completion stamp" in ln:
        lines[i] = ln.rstrip("\n") + " — done.\n"
        break
p.write_text("".join(lines))
PY
printf '\n- **recall-gate** — `recall-gate.py` (UserPromptSubmit) enforces recall-before-answering.\n' \
  >> memory/capabilities.md
git commit -q -am "test-fixture: the same S3 fix, plus an undeclared S1 regression

Casebook-Target: S3
"
BAD="$(git rev-parse HEAD)"

git checkout -q -f "$START_BRANCH"
python3 bin/trajectory-gate.py --promote HEAD >/dev/null 2>&1

echo
echo "=== the pair ==="
check "GOOD — real improvement, no side effect" KEEP   "$(verdict_of "$GOOD")"
check "BAD  — same improvement, undeclared regression" REVERT "$(verdict_of "$BAD")"
# BAD MUST REVERT FOR THE RIGHT REASON. core-business, bus #1013: before tonight's fix BOTH arms
# emitted the same environment refusal ("CORE_GATE_TRUSTED_ROOT ... does not point at the trusted
# tree"), and BAD "passed" because it wanted REVERT and got one — for a cause with nothing to do
# with an undeclared regression. So the pair was not discriminating AT ALL, and 3-of-4 green hid it.
# A pair whose arms fail identically reports as 75% healthy.
#
# That is the stable-verdict rule landing on the gate's own test: a verdict that matches expectation
# is not evidence unless it was reached for the reason under test.
if grep -qE "CORE_GATE_TRUSTED_ROOT|no trust root|not a valid object" "$GATE_OUT_FILE" 2>/dev/null; then
  printf '  FAIL  BAD reverted on an ENVIRONMENT refusal, not the regression — the pair is not\n'
  printf '        discriminating, and its PASS above is not evidence.\n'
  sed 's/^/        /' "$GATE_OUT_FILE"
  FAIL=$((FAIL+1))
else
  printf '  PASS  ...and BAD reverted for a GRADED reason, not an environment refusal\n'
  PASS=$((PASS+1))
fi

echo
echo "=== the refusals that must hold regardless ==="
# A change with no declared hypothesis is never silently kept: without this a candidate can alter
# anything, have nothing get worse, and be KEPT — p-hacking with extra steps.
git checkout -q -f -B _gate_bad "$START_BRANCH"
printf '\n<!-- no trailer -->\n' >> memory/capabilities.md
git commit -q -am "test-fixture: a change declaring no target"
check "no Casebook-Target trailer" REVERT "$(verdict_of "$(git rev-parse HEAD)")"
git checkout -q "$START_BRANCH"

# No trust root means no frozen evaluator, which is UNDECIDABLE — and undecidable is REVERT.
mv bin/.gate-trusted-sha bin/.gate-trusted-sha.bak 2>/dev/null
check "no trust root" REVERT "$(verdict_of "$GOOD")"
mv bin/.gate-trusted-sha.bak bin/.gate-trusted-sha 2>/dev/null

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]]
