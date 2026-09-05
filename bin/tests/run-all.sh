#!/usr/bin/env bash
# ONE TEST RUNNER, because the ad-hoc loop I retyped six times in one session could not tell a
# CRASH from a PASS.
#
# WHAT THIS REPLACES. Every suite run today was some variant of:
#
#     for t in bin/tests/test_*.py; do python3 "$t" >/dev/null 2>&1 && pass=$((pass+1)) ...
#
# Typed fresh each time, judging solely on exit code, discarding output. Three defects, all of which
# fail toward GREEN:
#
#   1. A test that dies on an ImportError before running a single check exits nonzero and reads as
#      RED — safe. But a test whose ASSERTIONS were silently skipped (an empty fixture list, a
#      `for` over nothing) prints its banner, asserts nothing, and exits 0. Indistinguishable from
#      a real pass. core-business found this exact shape in its suite.py, where an empty casebook
#      reported health.
#   2. Output discarded, so a test could print "3 FAILED" and still be counted green if it forgot
#      to set its exit code. Nothing checked that the two agreed.
#   3. Retyped each time. Six chances to type it differently, and no record of which variant
#      produced any given "54 green" I have quoted in a commit message.
#
# THE RULE IT ENFORCES, which is the governing law of this repo: a run that could not measure
# anything must not report health. Silence is not success.
#
# Usage:  bash bin/tests/run-all.sh [--quiet]
set -uo pipefail

# WHICH SEAT AM I MEASURING. core-business ran this against its own tree with CORE_INSTANCE set and
# the runner cheerfully reported FAIL and CRASH rows — for LIFE's tests, because it cd'd to its own
# root and globbed there. CORE_INSTANCE did not redirect discovery. It was "one message from
# reporting your unsynced test suite as business's failures" (bus #1034).
#
# That is the wrong-seat class this fleet has now hit in four separate tools, and it is worse in a
# RUNNER than anywhere else: the output is a verdict about a Core, and it named the wrong one.
#
# CORE_INSTANCE now redirects the whole run — cd there, discover there, so each test resolves its own
# root from its own location. And the seat is PRINTED on every run, because a number that does not
# say what it measured is exactly how this went wrong.
_SEAT="${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if [[ ! -d "$_SEAT" ]]; then
  echo "REFUSING: CORE_INSTANCE=$_SEAT is not a directory. Not falling back to my own tree —" >&2
  echo "that is precisely how a runner reports one Core's results under another Core's name." >&2
  exit 2
fi
# RESOLVE MY OWN PATH BEFORE THE cd. The discovery pass below re-invokes this script once per test
# directory, and `$0` is whatever the caller typed -- so a relative invocation from anywhere other
# than the seat root stops resolving the moment we cd. Caught by an end-to-end dose from /tmp: every
# child died with "No such file or directory" and the parent still printed a tidy
# "NOT GREEN - at least one directory reported a non-pass", which is TRUE, RED, and about nothing
# that ran. It errs toward red rather than green, which is the right direction to fail in, but the
# reason attached to it was fiction. Same family as the CORE_INSTANCE bug: a path that survives a
# cd only by luck of where it was typed.
_SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
if [[ ! -r "$_SELF" ]]; then
  echo "REFUSING: cannot resolve my own path ($0). Discovery re-invokes this script and would" >&2
  echo "produce a red verdict from children that never ran." >&2
  exit 2
fi
cd "$_SEAT" || exit 2
QUIET=0
FRESH=0    # --fresh: the fresh-clone contract — SKIP/ABSTAIN are named, not red; FAIL/CRASH/MUTE/LEAK still are
SCAN="bin/tests"
# HONOUR THE ARGUMENT OR REFUSE IT — never discard it. That is the defect core-business found in its
# own scanners (bus #971): a tool that accepts a path and silently measures its own seat produces a
# WRONG number rather than no number. Here the directory is honoured, which is also what makes this
# runner testable: bin/tests/test_run_all_classifies.py points it at planted fixtures and asserts
# each verdict. A runner nothing can aim is a runner nothing can check.
_SCAN_GIVEN=0
for a in "$@"; do
  case "$a" in
    --quiet) QUIET=1 ;;
    --fresh) FRESH=1 ;;
    -*) echo "REFUSING: unknown flag '$a'" >&2; exit 2 ;;
    *)  SCAN="$a"; _SCAN_GIVEN=1 ;;
  esac
done

# ===== IS THE DENOMINATOR REAL? =====
#
# core-business, bus #1035, on its own bad headline number: "the canary rule covers DOES IT FIRE. It
# does not cover IS THE DENOMINATOR REAL, and a harness that silently drops rows produces a plausible
# tally rather than an obvious failure."
#
# That lands here, in this runner's strongest sentence. `SCAN` defaulted to the single directory
# "bin/tests", so `bash bin/tests/run-all.sh` printed **ALL GREEN — every file executed** over 70
# files while SIX real tests under scheduling/claude-si/tests/ were never scanned. They were run by
# hand, when someone remembered, and "when someone remembered" is not a suite. The claim was about
# the SEAT; the denominator was one hardcoded directory — the same write-it-against-the-first-place-
# it-was-needed defect as the org-1 test, the "this Core is life" literal, and the ignore rule two
# commits ago.
#
# With no explicit target, DISCOVER every test directory in the seat and run all of them. Exclusions
# are COUNTED AND PRINTED, never silent: an excluded file that nobody can see is indistinguishable
# from a file that does not exist, which is the whole failure being fixed.
if [[ $_SCAN_GIVEN -eq 0 ]]; then
  _DIRS=""; _EXCL=0
  while IFS= read -r _f; do
    case "$_f" in
      */archive/*) _EXCL=$((_EXCL+1)); continue ;;
    esac
    _d=$(dirname "$_f")
    case "$(printf '\n%s\n' "$_DIRS")" in *"$(printf '\n%s\n' "$_d")"*) ;;
      *) _DIRS="${_DIRS:+$_DIRS
}$_d" ;; esac
  done < <(find . -path ./.git -prune -o -path ./node_modules -prune -o -path ./.venv -prune \
                  -o \( -name 'test_*.py' -o -name 'test_*.sh' \) -print | sort)

  if [[ -z "$_DIRS" ]]; then
    echo "REFUSING: no test files anywhere under $_SEAT."
    echo "An empty suite reports nothing and must not be read as reporting health."
    exit 2
  fi

  # NEWLINE-DELIMITED, NOT SPACE-DELIMITED. Codex (routed per codex-routing.md) found that
  # `for _d in $_DIRS` word-splits, so a seat containing "./component with spaces/tests" would run
  # the runner three times — on "./component", "with", and "spaces/tests" — and report verdicts for
  # directories that do not exist. Not hypothetical for this fleet: every Core lives under a path
  # with a space in it ("AI Projects"), and only the seat-RELATIVE form has kept this latent.
  _RC=0; _NDIR=0
  while IFS= read -r _d; do
    [[ -z "$_d" ]] && continue
    _NDIR=$((_NDIR+1))
    # bash 3.2 (macOS default) + `set -u`: expanding an EMPTY array is an unbound-variable error,
    # which killed the default recursive run the moment --fresh was added. The ${arr[@]+"${arr[@]}"}
    # idiom expands to nothing when empty and to the elements otherwise.
    _FL=(); [[ $QUIET -eq 1 ]] && _FL+=(--quiet); [[ $FRESH -eq 1 ]] && _FL+=(--fresh)
    bash "$_SELF" ${_FL[@]+"${_FL[@]}"} "$_d"; _crc=$?
    # A CHILD THAT COULD NOT START IS NOT A FAILING SUITE. 126/127 mean "not executable"/"not found":
    # no test ran, so reporting it as a non-pass would attribute a verdict to code that was never
    # executed -- the exact substitution this runner exists to refuse.
    if [[ $_crc -eq 126 || $_crc -eq 127 ]]; then
      echo "REFUSING: could not execute $_SELF for $_d (exit $_crc). No tests ran." >&2
      exit 2
    fi
    [[ $_crc -ne 0 ]] && _RC=1
  done <<< "$_DIRS"

  echo
  echo "=== SEAT TOTAL: $_NDIR test director$([[ $_NDIR -eq 1 ]] && echo y || echo ies) scanned ==="
  while IFS= read -r _d; do [[ -n "$_d" ]] && echo "    $_d"; done <<< "$_DIRS"
  # Printed on every run, green or red. A number that does not say what it left out is the shape
  # this block exists to remove.
  if [[ $_EXCL -gt 0 ]]; then
    echo "    excluded: $_EXCL file(s) under archive/ — deliberately retired, not silently missed"
  fi
  if [[ $_RC -ne 0 ]]; then echo "    NOT GREEN — at least one directory above reported a non-pass."
  elif [[ $FRESH -eq 1 ]]; then echo "    GREEN (fresh-clone contract) — no FAIL/CRASH/LEAK in any directory; SKIP/ABSTAIN named above."
  fi
  exit $_RC
fi
if [[ ! -d "$SCAN" ]]; then
  echo "REFUSING: '$SCAN' is not a directory. Not falling back to my own tests — that would report" >&2
  echo "a number for a target you did not ask about." >&2
  exit 2
fi

pass=0; fail=0; crash=0; mute=0; skip=0; leak=0; abstain=0
declare -a BAD=()

# A test must show EVIDENCE it checked something. The vocabularies in this tree differ by author —
# "N passed", "  PASS  ", "  ok ", "ALL CORRECT", "BOTH DIRECTIONS PASS" — so the runner accepts any
# of them rather than forcing a rewrite of nine working files. What it does NOT accept is a file
# that exits 0 having printed no evidence at all.
#
# THIS MATCHER WAS WRONG ON ITS FIRST RUN AND FLAGGED FOUR WORKING FILES. It required `ok` at the
# start of a line and knew only three success phrases, so `ALL PASS`, `SURVIVED — …`, `… MATCH`, and
# a trailing `flagged 0/0 ok` all read as "printed no evidence". Sixth matcher in one day that could
# not match what it was written for, and in the runner built to catch exactly that. The must-fire
# controls below exist so the next edit cannot quietly narrow it again.
assert_evidence() {
  grep -qiE "[0-9]+ passed|PASS\b|(^| )ok( |$)|ALL CORRECT|ALL PASS|DIRECTIONS PASS|SURVIVED|MATCH|round-trips|✓" <<< "$1"
}

# A SKIPPED TEST IS NOT A PASSING TEST. test_trajectory_gate.sh refuses to run when the worktree is
# dirty — correct, since it commits on scratch branches — and during active work the tree is ALWAYS
# dirty. The ad-hoc loop this file replaces counted that skip as green, so "4 shell suites green" was
# quoted all session while one of the four had not executed. Silence is not success, and neither is
# a deliberate refusal to measure.
is_skip() {
  grep -qE "^ *SKIP( |—|--|:)|^SKIP\b" <<< "$1"
}

# A Python traceback means the file DIED. core-business, bus #975: its suite mapped rc==1 to
# "findings" unconditionally, so a casebook where every artifact crashed printed "FINDINGS 33 — all
# controls green." A crash is not a result, whatever the exit code says.
is_crash() {
  grep -qE "^Traceback \(most recent call last\):|^[A-Za-z_.]*Error: |SyntaxError" <<< "$1"
}

# "I CANNOT DECIDE HERE" IS NOT "THIS IS BROKEN", and until 2026-08-13 this runner could not tell
# them apart: the arm below was `rc -ne 0 -> FAIL`, so a test that deliberately refused to certify
# without a fixture was rendered as a defect.
#
# Five tests use exit 2 for this on purpose — test_cwd_routing refuses without a brain vault,
# test_spec_schema_integrity refuses with "0 of 5 checks ran. Not a pass: this suite cannot certify
# a schema it never read." That refusal is the discipline the rest of this file exists to enforce,
# and the runner punished it.
#
# MEASURED, not reasoned about: a clean clone of life at /tmp showed 10 reds where the live seat
# shows 6. Two of the ten were these. core-finance chased 22 reds on their seat one at a time; an
# unknown share were this shape — a peer cannot distinguish "your shared code is broken" from
# "this seat has no fixture for that check" when both print FAIL.
#
# BOTH CONDITIONS ARE REQUIRED. rc==2 alone would launder a genuine crash that happens to exit 2
# (argparse does exactly that on a bad flag) into "not applicable" — the fail-toward-PASS direction,
# which is the one this suite refuses. The output must SAY it is undecidable.
#
# IT STILL COUNTS AS NON-GREEN. A check that did not run is not a check that passed — the same
# argument the SKIP arm is built on. This change alters the DIAGNOSIS, not the verdict: nothing red
# turns green, so it cannot manufacture a false pass. What it buys is that a peer reading UNDEC
# stops looking for a bug that is not there.
declined_to_certify() {
  grep -qE "UNDECIDABLE" <<< "$1"
}

run_one() {
  local t="$1" out rc
  if [[ "$t" == *.sh ]]; then
    out=$(bash "$t" 2>&1); rc=$?
  else
    out=$("$PYBIN" "$t" 2>&1); rc=$?
  fi

  if is_crash "$out"; then
    crash=$((crash+1)); BAD+=("CRASH  $t")
    [[ $QUIET -eq 0 ]] && printf '  CRASH  %s\n' "$t"
    return
  fi
  if [[ $rc -eq 2 ]] && declined_to_certify "$out"; then
    abstain=$((abstain+1))
    BAD+=("ABSTAIN $t  ($(grep -m1 -E "UNDECIDABLE" <<< "$out" | sed 's/^ *//' | cut -c1-96))")
    [[ $QUIET -eq 0 ]] && printf '  ABSTAIN %s  <- declined to certify here; no fixture, not a defect\n' "$t"
    return
  fi
  if [[ $rc -ne 0 ]]; then
    fail=$((fail+1)); BAD+=("FAIL   $t")
    [[ $QUIET -eq 0 ]] && printf '  FAIL   %s\n' "$t"
    return
  fi
  if is_skip "$out"; then
    skip=$((skip+1)); BAD+=("SKIP   $t  ($(grep -m1 -E '^ *SKIP' <<< "$out" | cut -c1-96))")
    [[ $QUIET -eq 0 ]] && printf '  SKIP   %s  <- did not execute\n' "$t"
    return
  fi
  if ! assert_evidence "$out"; then
    # Exited 0 having demonstrated nothing. This is the empty-suite shape and it is NOT a pass.
    mute=$((mute+1)); BAD+=("MUTE   $t  (exit 0, no evidence any check ran)")
    [[ $QUIET -eq 0 ]] && printf '  MUTE   %s  <- exit 0 but printed no evidence a check ran\n' "$t"
    return
  fi
  pass=$((pass+1))
  [[ $QUIET -eq 0 ]] && printf '  ok     %s\n' "$t"
}

shopt -s nullglob
FILES=("$SCAN"/test_*.py "$SCAN"/test_*.sh)
shopt -u nullglob

# AN EMPTY SUITE MUST NOT REPORT HEALTH. If the glob matches nothing — wrong cwd, a renamed
# directory, a bad checkout — every counter below is 0 and "0 failed" reads as perfect.
if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "REFUSING: no test files matched $SCAN/test_*.{py,sh} from $(pwd)."
  echo "An empty suite reports nothing and must not be read as reporting health."
  exit 2
fi

echo "=== $(basename "$(pwd)") : $SCAN — ${#FILES[@]} test files ==="
echo "    seat: $_SEAT"
# MEASURE IN MILLISECONDS, NOT SECONDS. `date +%s` truncates to whole seconds, so any directory
# whose suite finishes in under a second recorded a floor of 0 ms/file — and a floor of 0 is not a
# low floor, it is a DISABLED ratchet: nothing can ever tighten it and the comparison below stops
# meaning anything. scheduling/claude-si already runs in ~1s across 6 files, one machine-generation
# away from measuring itself at zero.
#
# Found as a FLAKE, which is why it is worth stating: this file's own per-file check failed roughly
# one run in four, and only because its tolerance is a FRACTION of the recorded rate — when the rate
# was 0 the tolerance was 0 and an exact match compared false. A flaky test is normally noise to be
# silenced; this one was the only thing reporting a real defect in the runner, and silencing it
# would have removed the report rather than the cause. BSD `date` has no %N, so the clock is python.
# ─── INTERPRETER SELECTION — pick one that can actually certify the DB-backed tests ────────────
# This runner invoked a bare `python3`, so it graded with whatever PATH happened to resolve. That is
# the ~39-site unpinned-interpreter split, landing in the one place it decides whether a seat is
# GREEN.
#
# It matters because several fixtures exercise the install gate, which proves an artifact is
# SPECIFIC by scoring it against a corpus sample living in Postgres. Without psycopg2 there is no
# corpus, the gate returns UNDECIDABLE, and those files can only ABSTAIN — and ABSTAIN blocks GREEN
# by design, because green means everything was certified and an abstain means something was not.
#
# core-school hit this: identical code, identical tests, PASS under 3.9 and not-green under 3.14,
# purely because their shell resolves an interpreter without the driver. Nothing about their seat
# was wrong.
#
# So: prefer the first python3 that can import psycopg2, fall back to bare python3, and SAY WHICH.
# Reporting the choice is half the point — a runner that silently picks a different interpreter than
# the reader assumes is how two seats disagree about the same commit.
_pick_python() {
  local c
  for c in $(command -v -a python3 2>/dev/null) /usr/bin/python3 /opt/homebrew/bin/python3; do
    [[ -x "$c" ]] || continue
    if "$c" -c 'import psycopg2' >/dev/null 2>&1; then echo "$c"; return; fi
  done
  command -v python3
}
PYBIN="${CORE_TEST_PYTHON:-$(_pick_python)}"
if "$PYBIN" -c 'import psycopg2' >/dev/null 2>&1; then
  _PYNOTE="has psycopg2 — DB-backed gates can certify"
else
  _PYNOTE="NO psycopg2 — DB-backed gates will ABSTAIN, and ABSTAIN blocks GREEN"
fi
echo "  interpreter: $PYBIN ($("$PYBIN" -V 2>&1 | cut -d' ' -f2)) — $_PYNOTE"

_now_ms() { "$PYBIN" -c 'import time;print(int(time.time()*1000))'; }

# A SUITE RUN MUST NOT CHANGE THE SEAT IT IS RUN FROM.
#
# Enforced HERE rather than per-test, because it belongs here: one digest, zero extra execution, and
# it covers every test in every directory including ones nobody has written yet. The per-file version
# I tried first was a textual sweep for an os.environ assignment — it accused ten working files and
# missed the form it was itself written in, which is the matcher-cannot-match-what-it-was-written-for
# class for the seventh time. Behaviour is checkable; spelling is not.
#
# What it caught: the claude-si tests isolate CLAUDE_PROJECT_DIR while the modules follow
# CORE_INSTANCE, so every run wrote fixtures into the live seat. life's active.json was found holding
# ONE artifact stamped org 2 in place of its own 22; business's held three stamped org 1; and
# bin/tests/test_tune_path_e2e.py — a SHARED file — put art_tune_e2e_probe into the live state of
# school, finance and ops, seats that never authored it (core-business, bus #1039-#1041).
# APPEND-ONLY TELEMETRY IS EXCLUDED, BY NAME, AND THE NAMES ARE PRINTED WHEN IT FIRES.
#
# Some tests exercise the REAL hooks in place, and a hook's STATE_DIR is derived from its own
# location — deliberately, so that no environment variable can move it. Those runs append a line to
# the event logs and there is no redirect that prevents it. Two pure-telemetry logs are therefore
# not part of the digest.
#
# The line NOT excluded is .sentinel-last-blocked, which is behaviour-bearing: a reviewer reads it
# back, and a passing test once left a bypass-shaped fixture there that core-business read as an
# attack by the Core under review. Tests that touch it restore it instead.
# WRITTEN BY LIVE HOOKS, NOT BY TESTS — the same reason hook-events.log and event-probe.log were
# already here, applied to the two files that share the property and were missed (2026-08-12, T034).
#
# The list being incomplete made this check fire on EVERY run in a live session, so `bin/tests`
# exited 1 with `ok=100 FAIL=0` and the verdict became noise. That is worse than a check that is
# slightly too permissive: a red that is always red trains the reader to skip it, which is how I
# reported "ALL GREEN" to Nick all night while the suite was exiting 1.
#
# ATTRIBUTED BY MEASUREMENT, not assumed. Hashing all four flagged files before and after an
# unpiped run:
#   .sentinel-last-blocked  IDENTICAL   test_trajectory_gate.sh saves and restores it
#   .sync-failures          IDENTICAL   test_digest_is_verifiable.py saves and restores it
#   gate-fires.log          CHANGED     ZERO tests reference it; recall-gate.py,
#                                       brain-recall-trigger.py and hooklog.py write it
#   .peer-digest            CHANGED     session-presence.py writes it on UserPromptSubmit; the one
#                                       test touching it saves AND restores (verified in its finally)
#
# THE COST, stated because widening a detector is the direction that needs it: a test that genuinely
# wrote gate-fires.log or .peer-digest would no longer be caught here. Accepted because neither is
# writable by a test that redirects CLAUDE_PROJECT_DIR — real hooks resolve their own state dir — so
# the only writer this hides is the one already excluded by name above it. Dosed in
# bin/tests/test_state_leak_still_detected.py: a planted file OUTSIDE this list must still trip LEAK.
# `.peer-digest` WAS HERE FOR TWENTY MINUTES AND core-finance was right to refuse it (2026-08-12).
# I said I would not ship this widening without a peer's eyes, shipped it anyway, and asked afterward
# whether either file is test-written on another tree. finance answered with line numbers:
# test_presence_cache_freshness.py deletes the LIVE .peer-digest (:95), writes a POISONED value
# (:106), writes GARBAGE (:122), and relies on its own cleanup at :127. Excluding it removes the only
# thing that would notice if that cleanup does not run — an exception between :106 and :127 leaves
# poisoned state behind and the detector blind to it BY CONSTRUCTION.
#
# So the justification I copied from the two entries above ("real hooks derive their state dir from
# their own location and cannot be redirected") was FALSE for this file specifically:
# session-presence.py:26 honours CLAUDE_PROJECT_DIR, so the test CAN be redirected — and now is.
# Reverted here; the test writes to a tmpdir instead.
#
# gate-fires.log stays: finance verified independently that its only writers are recall-gate.py,
# brain-recall-trigger.py and hooklog.py, and that no test writes it.
# READ FROM bin/tests/ambient-state.txt, not restated here. This was an inline literal while
# test_tests_do_not_write_live_state.py digested the same directory with NO exclusions at all — two
# implementations of one rule, disagreeing about the same run. The rationale that used to live in
# this comment block now lives in that file, beside the list it justifies.
#
# FAILS SAFE: no file, or a file with no usable lines, yields a pattern that matches nothing, so
# NOTHING is excluded and the digest over-reports. Over-reporting is the safe direction for a leak
# detector — the alternative is a missing list silently excusing every write.
_AMBIENT_LIST="$(dirname "$_SELF")/ambient-state.txt"
_STATE_NOISE='^$'
if [[ -r "$_AMBIENT_LIST" ]]; then
  _joined=$(grep -vE '^\s*(#|$)' "$_AMBIENT_LIST" 2>/dev/null | sed 's/\./\\./g' | paste -sd'|' -)
  [[ -n "$_joined" ]] && _STATE_NOISE="($_joined)\$"
fi
_state_digest() {
  [[ -d .claude/state ]] || { echo "-"; return; }
  find .claude/state -type f 2>/dev/null | grep -Ev "$_STATE_NOISE" | sort |
    xargs shasum -a 256 2>/dev/null | shasum -a 256 | awk '{print $1}'
}
# ONE RUN AT A TIME, PER SEAT.
#
# core-business ran this suite against my tree while I was committing to it and got three different
# answers from the same HEAD (ok=79 / ok=78 CRASH=1 / ok=78 SKIP=1). Four tests moved. It could not
# separate "inherently flaky" from "flaky under concurrent access" and said so, which is the right
# call — but the distinction only exists because nothing stopped two runs from overlapping.
#
# Several tests here read or assert the LIVE seat: one mutates .claude/rules/privacy.md and restores
# it, one asserts .claude/state is byte-identical afterwards, one derives a probe from peer trees.
# Each is correct alone and each is destroyed by a second process touching the same things. A fleet
# suite that cannot be trusted while a peer is running cannot gate a fleet.
#
# mkdir is the atomic primitive here — macOS ships no flock. A lock older than 30 minutes is treated
# as abandoned and broken, because a suite that can be permanently wedged by one killed run is worse
# than the race it prevents.
# RE-ENTRANT, because this runner invokes ITSELF. Discovery re-execs one child per directory, and
# bin/tests/test_run_all_classifies.py drives the runner as its subject — so a plain per-seat lock
# deadlocks the suite against its own children: the parent holds it, the child waits 60s, refuses,
# and the test that exercises the runner fails. Found immediately on the first full run after adding
# the lock, which is the argument for running the suite rather than reasoning about the change.
#
# The token is inherited through the environment, so anything spawned inside a locked run is already
# covered by the lock its ancestor holds.
_LOCK="${TMPDIR:-/tmp}/.core-run-all.$(printf '%s' "$_SEAT" | shasum -a 256 | cut -c1-12).lock"
_LOCK_HELD=0

# THE FIRST LOCK DELIVERED NONE OF ITS THREE GUARANTEES. core-business attacked it and all three
# failed under test, so each is fixed by binding the guard to something only a RUNNING holder can
# produce, rather than to a value that can be guessed, inherited, or aged out.
#
#   1. STALE-BREAK WAS AGE-ONLY. A live holder's lock could be stolen once its stamp aged past 30
#      minutes, and business demonstrated two runs going ALL GREEN concurrently. Age does not
#      distinguish "abandoned" from "still working" — a long suite is not a dead one. The lock now
#      records the holder's PID and is broken only when that process is GONE (`kill -0`).
#
#   2. THE RE-ENTRANCY TOKEN WAS DETERMINISTIC. It was shasum256(seat_path)[:12] — secret-free, so
#      any two processes handed that string skipped `mkdir` entirely and both "held" the lock. It is
#      now a per-run NONCE written INSIDE the lock directory; a child is re-entrant only if the
#      nonce it inherited MATCHES the one the live lock contains. An inherited-but-stale value now
#      fails closed instead of opening the gate.
#
#   3. THE TRAP MISSED SIGKILL, so after `kill -9` an unrelated healthy run waited 60s, exited 2
#      with zero tests, and repeated for 30 minutes. With the PID check, a dead holder's lock is
#      broken IMMEDIATELY — the 30-minute age rule is now only a backstop for a PID that was reused.
_LOCK_NONCE="$$-$(date +%s)-${RANDOM}${RANDOM}"
_holder_pid() { cat "$_LOCK/pid" 2>/dev/null || echo ""; }
_holder_alive() { local q; q="$(_holder_pid)"; [[ -n "$q" ]] && kill -0 "$q" 2>/dev/null; }
_lock_age() { local s; s=$(stat -f %m "$_LOCK/pid" 2>/dev/null || stat -c %Y "$_LOCK/pid" 2>/dev/null || echo 0); echo $(( $(date +%s) - s )); }

_LOCK_REENTRANT=0
if [[ -n "${CORE_SUITE_NONCE:-}" && -d "$_LOCK" ]]; then
  # Re-entrant ONLY if the live lock carries the exact nonce we inherited.
  if [[ "$(cat "$_LOCK/nonce" 2>/dev/null)" == "${CORE_SUITE_NONCE}" ]]; then
    _LOCK_REENTRANT=1
  fi
fi

if [[ $_LOCK_REENTRANT -eq 0 ]]; then
  for _try in 1 2 3 4 5 6 7 8 9 10; do
    if mkdir "$_LOCK" 2>/dev/null; then
      printf '%s' "$$" > "$_LOCK/pid"
      printf '%s' "$_LOCK_NONCE" > "$_LOCK/nonce"
      _LOCK_HELD=1
      export CORE_SUITE_NONCE="$_LOCK_NONCE"
      break
    fi
    if ! _holder_alive; then
      echo "  NOTE: breaking a lock whose holder (pid $(_holder_pid)) is gone" >&2
      rm -rf "$_LOCK" 2>/dev/null; continue
    fi
    if [[ $(_lock_age) -gt 1800 ]]; then
      echo "  NOTE: breaking a >30m lock held by a live pid $(_holder_pid) — assuming pid reuse" >&2
      rm -rf "$_LOCK" 2>/dev/null; continue
    fi
    [[ $_try -eq 1 ]] && echo "  waiting: another suite run holds this seat (pid $(_holder_pid))..." >&2
    sleep 6
  done
fi

if [[ $_LOCK_HELD -eq 0 && $_LOCK_REENTRANT -eq 0 ]]; then
  echo "REFUSING: another run has held this seat for over a minute. Two overlapping runs give" >&2
  echo "different answers from the same HEAD — reporting a number now would be worse than none." >&2
  exit 2
fi
# Only the process that CREATED the lock may remove it; a re-entrant child must not free its parent.
trap '[[ ${_LOCK_HELD:-0} -eq 1 ]] && rm -rf "$_LOCK" 2>/dev/null' EXIT INT TERM

_STATE0=$(_state_digest)
# A MARKER FILE, NOT AN EPOCH STRING. The first version localised the leak with
# `find -newermt "@<epoch>"`, and BSD find does not accept @epoch — so the leak was reported with an
# EMPTY list of what changed, which is the diagnosis equivalent of "something happened somewhere".
# Caught only because the dose printed nothing where it should have printed a path.
_MARK=$(mktemp 2>/dev/null || echo "/tmp/.core-run-all-mark.$$")
: > "$_MARK"
_T0=$(_now_ms)
for t in "${FILES[@]}"; do run_one "$t"; done
_ELAPSED_MS=$(( $(_now_ms) - _T0 ))
_ELAPSED=$(( _ELAPSED_MS / 1000 ))

echo
# LEAK IS PRINTED BECAUSE IT IS ACTED ON (T035, 2026-08-12). :502 fails the directory on
# fail||crash||mute||skip||LEAK — six counters — and this line showed five. So `ok=100 FAIL=0` and
# `exit 1` were both true simultaneously and the summary could not say why.
#
# That is not hypothetical: it is how I reported "ALL GREEN 100/100" to Nick and to the bus for  # privacy-ok: 'GREEN 100' status text, not a course code
# hours while bin/tests was exiting 1. The LEAK prose block below prints the reason every run, and
# every seat greps THIS line, so the reason was invisible to everyone who looked the normal way.
#
# It is the instrument-reports-X-acts-on-Y defect, inside the runner built to catch it. Both peers
# who declined to fix it said in their own words the change was correct — business: "it is additive,
# removes nothing, near-zero blast radius"; life: the same. It was blocked on nervousness, not on
# risk, and Nick named that as the non-reason.
# THE DIGEST IS TAKEN BEFORE THE COUNTER LINE, NOT AFTER. My first version of this change printed
# LEAK=${leak:-0} at this point while `leak` was still set 27 lines below — so the counter could
# never be anything but 0. That is the same defect the counter exists to remove, added by the fix
# for it, and it was caught by checking line ORDER rather than by the dose (which passed, because
# a file planted before the run is in both snapshots and correctly reports no change).
_STATE1=$(_state_digest)
[[ "$_STATE0" != "$_STATE1" ]] && leak=1

echo "  ok=$pass  FAIL=$fail  CRASH=$crash  MUTE=$mute  SKIP=$skip  ABSTAIN=${abstain:-0}  LEAK=${leak:-0}   (of ${#FILES[@]})"

# PRINTED WHENEVER IT IS NON-ZERO, for the same reason LEAK is: a counter the summary carries but
# never explains sends the reader looking in the wrong place. UNDEC is the one red a peer should
# NOT chase as a bug in shared code.
if [[ ${abstain:-0} -gt 0 ]]; then
  echo
  echo "  ABSTAIN: ${abstain} check(s) declined to certify on this seat — no fixture, not a defect."
  echo "  These exit 2 by design rather than pass on nothing. They are non-green because a check"
  echo "  that did not run is not a check that passed, but the shared code they test is NOT"
  echo "  implicated. Supply the fixture each one names to turn it into a real verdict."
fi

if [[ "$_STATE0" != "$_STATE1" ]]; then
  echo
  echo "  LEAK: this run CHANGED .claude/state on the live seat."
  echo "  A test must not write into the seat it is run from. Fixtures landing in live state are"
  echo "  invisible until a peer reads them back — that is how a foreign org's artifact ended up in"
  echo "  four Cores' running state. Usual cause: the test redirects CLAUDE_PROJECT_DIR but the"
  echo "  modules resolve their root from CORE_INSTANCE and fall back to their own repo root."
  echo "  (append-only telemetry is excluded from this digest: hook-events.log, event-probe.log —"
  echo "   real hooks derive their state dir from their own location and cannot be redirected.)"
  # BEFORE BLAMING A TEST, CHECK WHETHER THIS SEAT WAS BUSY (2026-08-13). This check compares a
  # state digest across the run; it CANNOT tell a test's write from a concurrent write by the live
  # session it is running inside. A LEAK=1 was raised here where the changed files were
  # .peer-digest / .last-activity / .sentinel-last-blocked — a background bus monitor and the
  # session's own hooks, no test involved. An immediate re-run came back LEAK=0.
  #
  # That is the same misattribution the rest of this suite keeps finding: an instrument naming a
  # cause it has no way to distinguish. Left as a WARNING rather than made silent, because the
  # failure it exists to catch is real and invisible; but a single LEAK is a prompt to re-run, not
  # a verdict about the tests.
  echo "  FIRST: re-run once. This digest cannot distinguish a test's write from a CONCURRENT"
  echo "  write by the live session (bus monitor, session hooks). If the second run is clean and"
  echo "  the files below are session/bus state rather than fixtures, no test leaked."
  echo "  Changed under .claude/state (newest first):"
  _NAMED=0
  while IFS= read -r _p; do echo "    $_p"; _NAMED=$((_NAMED+1)); done < <(
    find .claude/state -type f -newer "$_MARK" 2>/dev/null | head -8)
  # AN EMPTY LIST HERE IS ITSELF INFORMATION, and saying nothing would make this check look broken
  # the first time it happens. Two ways to get a changed digest with no newer file: something was
  # DELETED, or a hook fired from the surrounding SESSION rather than from a test — this runner
  # cannot tell those apart, because a live seat has writers other than the suite. A digest change
  # with nothing named is a prompt to look, not a verdict against a test.
  if [[ $_NAMED -eq 0 ]]; then
    echo "    (nothing newer than the run marker — a file was removed, or a hook fired from your"
    echo "     session while the suite ran. This check cannot distinguish those from a test write.)"
  fi
  echo "  If active.json moved, rebuild it — it is a projection, not the source of truth:"
  echo "    (cd scheduling/claude-si && python3 -c 'import si_project; si_project.project(<org>)')"
  leak=1
fi

# RUNTIME IS TRACKED PER FILE, NOT IN TOTAL — and the first version got this wrong.
#
# core-business put "watch the wall time each cycle" in its loop prompt and then caught itself doing
# the thing today disproved: resolutions do not survive, mechanisms do. So this is a mechanism.
#
# But the mechanism I shipped first was a MONOTONIC TOTAL, copied from the steering ratchet, and the
# analogy is false. The steering ratchet exists to RESIST growth: every added token is a cost paid on
# every prompt forever, so a floor that only falls is exactly right. A test suite is the opposite —
# it is SUPPOSED to grow, and today it grew from 58 files to 66. Against a fixed total, legitimate
# new tests widen the gap forever until the alarm fires on good work, and an alarm that fires on good
# work is one somebody silences. That is the same failure as the skip this block was built to catch,
# arriving from the other side.
#
# Seconds-per-file is invariant under adding tests and rises under a real slowdown, so it measures
# the thing meant rather than the thing convenient.
#
# THE FLOOR STILL ONLY FALLS (business, bus #976): a snapshot rewritten every run shows a
# degradation for exactly one cycle and then normalises it away. Recorded only on a FULL CLEAN run —
# a partial run is faster for reasons that say nothing about the suite.
_RT="$SCAN/.runtime-baseline"
# --fresh (2026-09-04, codex #5943 P0): a stranger's clone has no brain, no transcripts, no peers.
# Its DB-backed and live-seat checks ABSTAIN/SKIP by design, and under the strict contract that is
# red — correct for a seat, useless as a public acceptance test, so README normalised "about a dozen
# fail". The partition: --fresh grades FAIL/CRASH/MUTE/LEAK as red and lists SKIP/ABSTAIN by name.
# Nothing un-run is ever counted as passed; it is counted as named-and-unavailable.
_SOFT=$(( skip + ${abstain:-0} ))
if [[ $FRESH -eq 1 ]]; then skip=0; abstain=0; fi
if [[ $fail -eq 0 && $crash -eq 0 && $mute -eq 0 && $skip -eq 0 && $leak -eq 0 && ${abstain:-0} -eq 0 ]]; then
  # milli-seconds per file, integer, so bash can compare it
  _RATE=$(( _ELAPSED_MS / ${#FILES[@]} ))
  # A rate of 0 is unmeasurable rather than instant, and recording it would freeze the floor there.
  [[ $_RATE -lt 1 ]] && _RATE=1
  # THE FLOOR IS VERSIONED, because the unit changed under it. Floors recorded before the
  # millisecond clock came from `date +%s` and are not comparable — claude-si's was 166 ms/file,
  # derived from a 1-second reading over 6 files, which a true measurement will exceed by more than
  # 2x and trip the slowdown alarm on a suite that did not slow down. Rather than ask five Cores to
  # delete a gitignored file they do not know about, an unversioned floor reads as NO PRIOR
  # MEASUREMENT and the next clean run re-records it. Self-healing, once, everywhere.
  _PREV=""
  if [[ -f "$_RT" ]]; then
    read -r _v _n < "$_RT" 2>/dev/null || true
    [[ "${_v:-}" == "v2" ]] && _PREV=$(printf '%s' "${_n:-}" | tr -dc '0-9')
  fi
  _HUMAN=$(( _RATE / 100 ))          # tenths of a second, for display
  if [[ -z "$_PREV" ]]; then
    echo "v2 $_RATE" > "$_RT"
    printf '  runtime   %ss over %d files = %d.%ds/file — first clean measurement, recorded\n' \
      "$_ELAPSED" "${#FILES[@]}" $(( _HUMAN / 10 )) $(( _HUMAN % 10 ))
  elif [[ "$_RATE" -lt "$_PREV" ]]; then
    echo "v2 $_RATE" > "$_RT"
    printf '  runtime   %ss over %d files = %d.%ds/file  ↓ floor tightened\n' \
      "$_ELAPSED" "${#FILES[@]}" $(( _HUMAN / 10 )) $(( _HUMAN % 10 ))
  else
    # 1.5x the per-file rate AND at least 500ms/file absolute, so machine-load jitter does not cry
    # wolf. A gate that fires on noise is one someone silences.
    _MIN_RATE_DELTA="${CORE_RUNTIME_MIN_DELTA:-500}"
    _DELTA=$(( _RATE - _PREV ))
    if [[ $(( _RATE * 2 )) -gt $(( _PREV * 3 )) && "$_DELTA" -gt "$_MIN_RATE_DELTA" ]]; then
      printf '  runtime   %ss over %d files = %d.%ds/file — SLOWED BY MORE THAN HALF PER FILE\n' \
        "$_ELAPSED" "${#FILES[@]}" $(( _HUMAN / 10 )) $(( _HUMAN % 10 ))
      echo "            floor ${_PREV}ms/file, now ${_RATE}ms/file. The floor is NOT updated, so this"
      echo "            keeps reporting until the suite recovers and cannot decay into the new normal."
    else
      printf '  runtime   %ss over %d files = %d.%ds/file  (floor %dms/file)\n' \
        "$_ELAPSED" "${#FILES[@]}" $(( _HUMAN / 10 )) $(( _HUMAN % 10 )) "$_PREV"
    fi
  fi
else
  echo "  runtime   ${_ELAPSED}s — not recorded, this run was not clean"
fi
if [[ $fail -gt 0 || $crash -gt 0 || $mute -gt 0 || $skip -gt 0 || ${leak:-0} -gt 0 || ${abstain:-0} -gt 0 ]]; then
  echo
  # `${BAD[@]}` UNGUARDED IS UNBOUND WHEN A LEAK IS THE ONLY PROBLEM (2026-08-13). BAD is appended
  # to by the FAIL / CRASH / SKIP / MUTE arms only — never by the leak check, which is computed
  # after the per-file loop from a state digest. So a run with fail=crash=mute=skip=0 and leak=1
  # enters this branch with an EMPTY array, and macOS bash 3.2 under `set -u` errors on
  # "${BAD[@]}" rather than expanding to nothing:
  #
  #     run-all.sh: line 523: BAD[@]: unbound variable
  #
  # Observed on exactly that combination. The leak block itself had already printed, so the
  # failure was not silent — but the runner aborted the rest of its own summary, including the
  # behind-the-baseline note that tells a peer Core its FAILs may be stale tests rather than
  # broken code. The diagnostic most needed on a bad run is the one that broke.
  [[ ${#BAD[@]} -gt 0 ]] && printf '%s\n' "${BAD[@]}"
  _NOTE=0; (( fail > 0 || crash > 0 )) && _NOTE=1
  # A FAIL ON A CORE BEHIND THE BASELINE IS AMBIGUOUS, and this runner is the only thing positioned
  # to say so. core-business, bus #1034: of three real failures on its seat, TWO were stale TESTS
  # rather than broken code — its copies differed from the baseline's, and the baseline versions
  # passed. It checked before believing; finance and ops are two baselines behind and have nobody
  # telling them to.
  #
  # Printed only when there is something to be ambiguous about, so it never becomes furniture.
  #
  # THE CONDITION HAS TO BE A REAL ONE. The first version read `[[ -f bin/.last-baseline-sync ]] ||
  # [[ -d .git ]]`, and the second disjunct is true in every Core — so the comment above claimed a
  # conditional that did not exist, and the note fired on a SKIP, where there is no broken-vs-stale
  # ambiguity to resolve. A test that DECLINED TO RUN is self-explaining; only a test that ran and
  # came back red can be red because it is newer than this seat's code.
  if (( _NOTE )); then
    echo
    echo "  NOTE: a FAIL here means EITHER this seat is broken OR this seat has an OLD TEST."
    echo "  A test newer than the code it checks fails honestly and means only that this Core has"
    echo "  not pulled. why-red.sh answers it below by comparing each failing file to the writer's"
    echo "  copy — the check core-business ran by hand three times, which decided every one."
    _REDS=()
    for b in "${BAD[@]}"; do
      case "$b" in
        FAIL*|CRASH*) _REDS+=("$(awk '{print $2}' <<< "$b")") ;;
      esac
    done
    # ADVICE THE READER HAS TO ACT ON IS ADVICE MOST READERS SKIP. The note above told a puller to go
    # diff something; this runs the diff. It is diagnosis only — it cannot change a verdict or an
    # exit code, so a wrong answer here can mislead but can never turn a real failure green.
    if [[ -f bin/tests/why-red.sh && ${#_REDS[@]} -gt 0 ]]; then
      bash bin/tests/why-red.sh "${_REDS[@]}" || true
    fi
  fi
  rm -f "$_MARK" 2>/dev/null
  exit 1
fi
rm -f "$_MARK" 2>/dev/null
if [[ $FRESH -eq 1 && $_SOFT -gt 0 ]]; then
  echo "  GREEN (fresh-clone contract) — 0 FAIL / 0 CRASH / 0 LEAK; $_SOFT file(s) SKIP or ABSTAIN, named above: unavailable on a bare clone, not passed."
else
  echo "  ALL GREEN — every file executed AND demonstrated at least one check ran."
fi
