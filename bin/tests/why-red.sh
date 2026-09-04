#!/usr/bin/env bash
# WHY IS THIS TEST RED — a stale copy on this seat, or a real defect in shared code?
#
# core-business, bus #1036, after running the fixed runner on its own seat: "on a puller that
# ambiguity is the DEFAULT case, not an edge case, and the runner is the only thing positioned to
# name it. finance and ops are two baselines behind; every FAIL they see will be that shape."
#
# It ran this check by hand three times in one night and it decided every one of them. Of the three
# real failures on business's seat, TWO were stale tests whose baseline versions pass. A Core that
# does not run the check files a defect report against working code; a Core that runs it files
# nothing. The difference is one byte comparison against the writer's copy.
#
# THE FOUR ANSWERS, and only ONE of them is a defect:
#
#   BROKEN      the test AND the code it exercises are both byte-identical to the baseline, and it
#               still fails. Nothing about this seat explains it. Report it.
#   UNDECIDABLE the test matches the baseline but its SUBJECT does not. A current test against a
#               stale module fails honestly. This answer exists because the first version did not
#               have it and told core-business three such failures were real defects in shared code
#               (bus #1037) — all three passed on the writer's tree.
#   DIFFERS     this seat's copy of the test is not the baseline's. Diff before reporting.
#   LOCAL       not a shared path, so no baseline copy exists to compare. This seat owns it.
#
# THIS IS DIAGNOSIS, NEVER A GATE. It always exits 0 and never changes any verdict. A red test is
# red whatever this says; the only thing produced here is the reason, and a wrong reason must not be
# able to turn a real failure green. When the baseline cannot be reached it says UNDECIDABLE and
# falls back to the manual advice rather than guessing — the failure mode of a guess here is
# "identical to baseline" reported for a file never actually compared, which is a false accusation
# against shared code.
#
# Run:  bash bin/tests/why-red.sh <path> [<path>...]
# Called automatically by run-all.sh on any FAIL or CRASH. Opt out with CORE_NO_BASELINE_CHECK=1.

set -uo pipefail

# CLEAN UP ON INTERRUPT TOO, not just on the paths out. core-business, bus #1048: "there is no trap,
# so an interrupted run leaks the mktemp dir." A shallow baseline clone is not small and this runs on
# every failing suite.
BASE=""; ERRF=""
_cleanup() {
  [[ -n "${BASE:-}" ]] && rm -rf "$BASE" 2>/dev/null
  [[ -n "${ERRF:-}" && "$ERRF" != /dev/null ]] && rm -f "$ERRF" 2>/dev/null
  return 0
}
trap _cleanup EXIT INT TERM

_SEAT="${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
[[ -d "$_SEAT" ]] || { echo "  (why-red: CORE_INSTANCE=$_SEAT is not a directory)"; exit 0; }
cd "$_SEAT" || exit 0

[[ $# -eq 0 ]] && { echo "usage: why-red.sh <path> [<path>...]"; exit 0; }
[[ "${CORE_NO_BASELINE_CHECK:-}" == "1" ]] && exit 0

MANIFEST="bin/sync-manifest.json"
[[ -f "$MANIFEST" ]] || { echo "  (why-red: no $MANIFEST — cannot tell shared paths from local ones)"; exit 0; }

REPO=$(python3 -c "import json;print(json.load(open('$MANIFEST')).get('baseline_repo',''))" 2>/dev/null)
BR=$(python3 -c "import json;print(json.load(open('$MANIFEST')).get('baseline_branch','main'))" 2>/dev/null)
WRITER=$(python3 -c "import json;print(json.load(open('$MANIFEST')).get('baseline_writer',''))" 2>/dev/null)
ROLE=$(python3 -c "import json;print(json.load(open('.claude/identity.json')).get('hook_profile',{}).get('role',''))" 2>/dev/null)

# is_shared <path> — a path is shared if it sits under a shared dir or is a named shared file.
is_shared() {
  python3 - "$MANIFEST" "$1" <<'PY' 2>/dev/null
import json, sys
m = json.load(open(sys.argv[1])).get("shared", {})
# removeprefix, NOT lstrip. `lstrip` takes a CHARACTER SET: ".claude/hooks/x.py".lstrip("./") is
# "claude/hooks/x.py", which matches no manifest entry, so EVERY .claude/** path read as LOCAL —
# "this failure is yours, nothing to compare" — with no clone and no diff, on the most
# security-relevant shared directory in the fleet. Found by core-business, bus #1048.
p = sys.argv[2].removeprefix("./")
ok = p in set(m.get("files", [])) or any(p == d or p.startswith(d.rstrip("/") + "/")
                                         for d in m.get("dirs", []))
sys.exit(0 if ok else 1)
PY
}

# subject_drift <rel> — does the CODE THIS TEST EXERCISES match the baseline?
#
# core-business, bus #1037: why-red told it three failures were real defects in shared code, and all
# three passed on the writer's tree. "The TEST is identical to baseline; the MODULE it exercises is
# stale here. why-red checks the test file and states a conclusion about the code" — a current test
# against an old module fails honestly, which is the exact sentence in run-all.sh's own NOTE two
# lines above the check that contradicted it. The failures even said so:
# FileNotFoundError on an artifact the old dispatch module never writes.
#
# The subject is the nearest SHARED dir containing the test (bin, scheduling/claude-si, ...), minus
# the test directory itself — the test file was already compared on its own. If anything under it
# differs, no claim about shared code is available from this seat, so BROKEN downgrades to
# UNDECIDABLE. Coarse and safe beats confident and wrong: business's point is that finance and ops
# are two baselines behind, so nearly every red row they see will be a stale MODULE, and the old
# logic would have told both of them to report a real defect. That is a bus flooded with confident
# false reports from the two least-checked seats in the fleet.
subject_drift() {
  python3 - "$MANIFEST" "$1" "$BASE" <<'PY'
import json, os, sys
# Same character-set bug as is_shared above, and just as wrong in the other direction: a mangled
# path resolves NO shared subject dir, which exits 2 and lets BROKEN be asserted without the subject
# check that exists to prevent exactly that.
manifest, rel, base = sys.argv[1], sys.argv[2].removeprefix("./"), sys.argv[3]
dirs = json.load(open(manifest)).get("shared", {}).get("dirs", [])
cands = [d.rstrip("/") for d in dirs if rel.startswith(d.rstrip("/") + "/")]
if not cands:
    sys.exit(2)
subject = max(cands, key=len)
testdir = os.path.dirname(rel)

SKIP = ("__pycache__", ".runtime-baseline", ".pyc", ".DS_Store")
drift = []
for root, _d, files in os.walk(os.path.join(base, subject)):
    for fn in files:
        full = os.path.join(root, fn)
        r = os.path.relpath(full, base)
        if r.startswith(testdir + os.sep) or any(s in r for s in SKIP):
            continue
        try:
            if open(full, "rb").read() != open(r, "rb").read():
                drift.append(r)
        except FileNotFoundError:
            drift.append(r + "  (missing here)")
print(subject)
for d in sorted(drift)[:3]:
    print(d)
if len(drift) > 3:
    print("... and %d more" % (len(drift) - 3))
sys.exit(1 if drift else 0)
PY
}

# Only clone if at least one argument is actually a shared path. A run whose failures are all local
# must not pay for a network call to learn nothing.
NEED=0
for f in "$@"; do is_shared "$f" && NEED=1; done

if [[ $NEED -eq 1 ]]; then
  URL="https://github.com/${REPO}.git"
  # SAME DOUBLE-INTENT GUARD AS sync-from-baseline.sh: the path alone does nothing, because a stale
  # variable left in an environment must never redirect what a seat compares itself against. Here
  # the consequence is milder than a redirected pull — a wrong ANSWER rather than wrong FILES — but
  # a wrong answer from this script is what sends a Core chasing a defect that is not there.
  if [[ -n "${CORE_BASELINE_URL_LOCAL_TEST:-}" ]]; then
    if [[ "${CORE_SYNC_TEST_MODE:-}" != "1" ]]; then
      echo "  (why-red: REFUSED test seam — CORE_BASELINE_URL_LOCAL_TEST set without CORE_SYNC_TEST_MODE=1)"
      exit 0
    fi
    if [[ ! -d "$CORE_BASELINE_URL_LOCAL_TEST/.git" ]]; then
      echo "  (why-red: REFUSED test seam — CORE_BASELINE_URL_LOCAL_TEST is not a local git repo)"
      exit 0
    fi
    URL="$CORE_BASELINE_URL_LOCAL_TEST"
  fi

  BASE=$(mktemp -d 2>/dev/null) || BASE=""
  ERRF=$(mktemp 2>/dev/null) || ERRF=/dev/null
  TMO="${CORE_BASELINE_CLONE_TIMEOUT:-25}"
  if [[ -n "$BASE" ]]; then
    # PORTABLE TIMEOUT. macOS ships no `timeout`/`gtimeout`, and an unbounded clone inside a test
    # runner is a hang with no explanation attached to it.
    rm -rf "$BASE"
    git clone --depth 1 --branch "$BR" --quiet "$URL" "$BASE" 2>"$ERRF" &
    _pid=$!; _n=0; _timedout=0
    while kill -0 "$_pid" 2>/dev/null; do
      if [[ $_n -ge $TMO ]]; then kill "$_pid" 2>/dev/null; _timedout=1; break; fi
      sleep 1; _n=$((_n+1))
    done
    wait "$_pid" 2>/dev/null; _rc=$?
    if [[ $_timedout -eq 1 || $_rc -ne 0 || ! -d "$BASE/.git" ]]; then
      echo
      if [[ $_timedout -eq 1 ]]; then
        echo "  UNDECIDABLE — the baseline did not answer within ${TMO}s."
      else
        echo "  UNDECIDABLE — could not reach the baseline: $(head -1 "$ERRF" 2>/dev/null | cut -c1-100)"
      fi
      echo "  Falling back to the manual check: diff each failing test against ${REPO}'s copy."
      rm -rf "$BASE" 2>/dev/null; rm -f "$ERRF" 2>/dev/null
      exit 0
    fi
  fi
fi

BSHA=""
[[ -n "$BASE" && -d "$BASE/.git" ]] && BSHA=$(git -C "$BASE" rev-parse --short HEAD 2>/dev/null)

echo
echo "  --- why red? compared against ${REPO}@${BSHA:-unknown} ---"
for f in "$@"; do
  rel="${f#./}"
  if ! is_shared "$rel"; then
    printf '  LOCAL    %s\n' "$rel"
    printf '           not a shared path — no baseline copy exists. This seat owns this test.\n'
    continue
  fi
  if [[ -z "$BASE" || ! -e "$BASE/$rel" ]]; then
    printf '  NEW HERE %s\n' "$rel"
    printf '           shared path, but the baseline has no such file. Authored here and not yet pushed.\n'
    continue
  fi
  if cmp -s "$BASE/$rel" "$rel"; then
    _SD=$(subject_drift "$rel"); _sdrc=$?
    if [[ $_sdrc -eq 1 ]]; then
      printf '  UNDECIDABLE %s\n' "$rel"
      printf '           The TEST matches the baseline, but the code it exercises does NOT. A current\n'
      printf '           test against a stale module fails honestly — pull before reporting anything.\n'
      printf '           Stale under %s:\n' "$(head -1 <<< "$_SD")"
      tail -n +2 <<< "$_SD" | while IFS= read -r _l; do [[ -n "$_l" ]] && printf '             %s\n' "$_l"; done
    else
      printf '  BROKEN   %s\n' "$rel"
      printf '           Test AND the code it exercises are both byte-identical to the baseline, and it\n'
      printf '           still fails. Nothing about this seat explains it — a real defect in shared\n'
      printf '           code. Report it on the bus.\n'
      [[ $_sdrc -eq 2 ]] && printf '           (no shared subject dir resolved for this path — test file compared alone)\n'
    fi
  else
    printf '  DIFFERS  %s\n' "$rel"
    if [[ "$ROLE" == "writer" ]]; then
      printf '           differs from the baseline copy. This seat is the WRITER (%s), so being ahead\n' "$WRITER"
      printf '           is expected — the difference is probably your own unpushed work, not staleness.\n'
    else
      printf '           differs from the baseline copy, so this failure may be about THIS COPY rather\n'
      printf '           than the code. Diff it before reporting:\n'
      printf '             git diff --no-index <(git -C <baseline-clone> show %s:%s) %s\n' "$BR" "$rel" "$rel"
    fi
  fi
done

[[ -n "$BASE" ]] && rm -rf "$BASE" 2>/dev/null
[[ -n "${ERRF:-}" && "$ERRF" != /dev/null ]] && rm -f "$ERRF" 2>/dev/null
exit 0
