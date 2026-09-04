#!/usr/bin/env bash
# A file-list alarm carrying a NON-file-list must be loud and INERT — never block a save.
#
# THE CHAIN THIS GUARDS, measured end to end on 2026-08-11:
#
#   a file-list alarm was handed the self-test's stderr instead of a list of paths
#     -> _safety_scan set BLOCKED=1 because the variable was merely non-empty
#          -> .last-save-blocked written, reason "CREDENTIAL FILES STAGED: … SELF-TESTS FAILED …"
#               -> that marker pins .end-session-requested (stop-hook.sh:26-29)
#                    -> EVERY assistant turn ran a FULL close: 105 on life, 92 on business, in a day
#                         -> each died in a ~6-minute pg_dump inside a 60-second hook
#                              -> 671 orphaned fragments, 128 GB, on a disk with 45 GiB free
#                                   -> and every close stage after brain-backup stopped running,
#                                      freezing grade-gate and grade-intent for three days
#
# An earlier fix (2026-08-10) made the corrupt value RENDER correctly as
# "INTERNAL: … treat as a reporting fault, not a leak" — and then blocked anyway. Naming a fault
# while still acting on it is the worst of both: it reads as handled in the log and behaves exactly
# as if a credential had been staged. That is why this file tests the BLOCK DECISION, not the
# message text.
#
# The second case is the one that matters most. It is easy to "fix" a false credential alarm by
# making the alarm never fire; the control asserts a REAL credential list still blocks. Without it
# this test would pass just as happily against a safety scan that had been switched off.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIFECYCLE="$REPO/.claude/hooks/session-lifecycle.sh"
pass=0; fail=0

check() {  # label, actual, expected
  if [[ "$2" == "$3" ]]; then
    echo "  PASS  $1"; pass=$((pass + 1))
  else
    echo "  FAIL  $1"; echo "          expected BLOCKED=$3, got BLOCKED=$2"; fail=$((fail + 1))
  fi
}

echo "test_safety_scan_internal_fault"
[[ -f "$LIFECYCLE" ]] || { echo "  FAIL  session-lifecycle.sh missing"; exit 1; }

# Source only the two functions under test, so this cannot run a real close as a side effect.
FUNCS=$(sed -n '/^_file_list_reason()/,/^}/p;/^_note_file_list()/,/^}/p' "$LIFECYCLE")
[[ -n "$FUNCS" ]] || { echo "  FAIL  could not extract _file_list_reason/_note_file_list"; exit 1; }

run_case() {  # value -> echoes resulting BLOCKED
  bash -c "
    $FUNCS
    BLOCKED=0; BLOCK_REASONS=()
    _note_file_list 'CREDENTIAL FILES STAGED' \"\$1\" '(\.ssh/|\.pem$|\.key$|\.env$|credentials|\.p12$|\.pfx$|id_rsa|id_ed25519|\.secret)' 2>/dev/null
    echo \$BLOCKED
  " _ "$1"
}

# 1. The exact corrupt value observed on 2026-08-09.
check "self-test stderr in a file-list alarm does NOT block" \
      "$(run_case '[session-lifecycle] SELF-TESTS FAILED — see /tmp/session-lifecycle-core-life.log')" 0

# 2. A bracketed line, the other shape the shape-check rejects.
check "a bracketed non-path does NOT block" "$(run_case '[some tool] emitted this')" 0

# 3. CONTROL — a real credential list MUST still block. Without this the test would pass against a
#    safety scan that had simply been disabled, which is the obvious wrong way to fix case 1.
check "a REAL credential file list still blocks" "$(run_case 'secrets/id_rsa
config/.env')" 1

# 4. CONTROL — an ordinary path with no prose markers still blocks.
check "an ordinary staged path still blocks" "$(run_case 'memory/credentials.json')" 1

# ---------------------------------------------------------------------------------------------
# 5-7. THE MIXED CASES. These are the ones that matter, and cases 1-4 could not catch them.
#
# The first version of this fix used a SINGLE _bad flag over the whole value, so ONE prose-shaped
# line made the ENTIRE alarm inert — every genuine credential path beside it included. core-business
# found it on review by EXECUTING the function against mixed input rather than reading it.
#
# Cases 1-4 all pass against that broken version, because each is either purely clean or purely
# corrupt. The distinction only bites when both shapes appear together, and none of them tried it.
# A control that cannot fail on the interesting input is decoration — which is the same defect this
# whole file exists to guard against, committed inside the guard.
#
# A filename with an em dash or a leading bracket is not exotic and need not be adversarial. An
# accidental name is enough.
check "a real .env whose name contains an em dash still blocks" \
      "$(run_case 'secrets/prod — copy.env')" 1

check "a real private key in a bracketed path still blocks" \
      "$(run_case '[archive] id_rsa')" 1

check "TWO private keys still block when a SIBLING line looks like prose" \
      "$(run_case 'secrets/id_rsa
config/prod — old.env
.ssh/id_ed25519')" 1

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
