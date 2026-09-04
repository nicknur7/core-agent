#!/usr/bin/env bash
# test-state-claim-gate.sh — Test harness for state-claim-gate.py
# Usage: bash test-state-claim-gate.sh
# Exits 0 if all pass, 1 if any fail.
#
# Strategy: write a patched copy of the hook to a temp file where PROJECTS_DIR
# is sourced from env var STATE_CLAIM_GATE_TEST_DIR, avoiding the heredoc-
# stdin conflict (python3 - <<'EOF' + pipe).
#
# False-positive note (audit Dim 2):
#   STATE_KEYWORDS includes broad tokens: "file", "directory", "folder",
#   "path", "memory", "session", "plan", "X", "Y", "Z".
#   The existential patterns ("there's no Y", "a file is valid") can fire
#   on ordinary technical prose.  T05 documents this known FP shape.
set -uo pipefail
export CORE_HOOKLOG_OFF=1  # drive detection without polluting the durable telemetry log

_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve hooks dir for both layouts: tests/hooks/ (public) and .claude/hooks/tests/ (legacy in-tree).
case "$_TESTS_DIR" in
  */.claude/hooks/tests) HOOKS_DIR="$(cd "$_TESTS_DIR/.." && pwd)" ;;
  *)                     HOOKS_DIR="$(cd "$_TESTS_DIR/../../.claude/hooks" && pwd)" ;;
esac

PASS=0
FAIL=0
FAILURES=""

TMPDIR_LOCAL=$(mktemp -d /tmp/test-state-claim-gate-XXXXXX)
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

# ---------------------------------------------------------------------------
# Write the patched hook to a temp file (mirrors state-claim-gate.py exactly;
# only PROJECTS_DIR is sourced from env var STATE_CLAIM_GATE_TEST_DIR).
# ---------------------------------------------------------------------------
# RUN THE REAL HOOK. This used to write an inlined COPY of state-claim-gate.py to a temp file and
# test that — so changing or breaking the real hook could not fail this suite. The copy had
# already drifted: it still logged to the abandoned global /tmp/core-hook-events.log, which is
# where the phantom "hook=say-do-gap-test" events on a freshly spawned Core came from. The
# bleed and the dead test were one bug. (core-business, 2026-07-28.)
#
# The copy existed to override PROJECTS_DIR, which the hook computed from cwd. The real hook
# now honours CORE_PROJECTS_DIR, so no duplicate is needed.
HOOK_PY="$HOOKS_DIR/state-claim-gate.py"
[[ -f "$HOOK_PY" ]] || { echo "FATAL: $HOOK_PY not found — cannot test the real hook"; exit 1; }

# ---------------------------------------------------------------------------
# Helper: write a synthetic JSONL fixture.
# make_jsonl <dest_file> <text> [tool1 tool2 …]
# Writes directly to dest_file (does not print to stdout).
# ---------------------------------------------------------------------------
make_jsonl() {
    local dest_file="$1"
    local text="$2"
    shift 2
    local tools=("$@")

    # Write a small Python builder script to avoid heredoc-stdin conflicts.
    local py_builder="$TMPDIR_LOCAL/jsonl_builder_$$.py"
    cat > "$py_builder" <<'PYBUILD'
import json, sys
dest = sys.argv[1]
text = sys.argv[2]
tool_names = [a for a in sys.argv[3:] if a]
content = [{"type": "text", "text": text}]
for t in tool_names:
    content.append({"type": "tool_use", "name": t, "id": "x", "input": {}})
with open(dest, "w") as f:
    f.write(json.dumps({"type": "assistant", "message": {"content": content}}) + "\n")
PYBUILD
    python3 "$py_builder" "$dest_file" "$text" "${tools[@]:-}"
}

# ---------------------------------------------------------------------------
# run_case: write fixture, pipe Stop-hook JSON to patched hook, check result.
# run_case <name> <text> <stop_hook_active:true|false> <expect_block:true|false>
#           [tool1 tool2 …]
# ---------------------------------------------------------------------------
run_case() {
    local name="$1"
    local text="$2"
    local stop_hook_active="${3:-false}"
    local expect_block="${4:-false}"
    shift 4
    local tools=("$@")

    local case_dir
    case_dir=$(mktemp -d "$TMPDIR_LOCAL/case-XXXXXX")
    make_jsonl "$case_dir/session-test.jsonl" "$text" "${tools[@]:-}"

    local sha_bool="false"
    [[ "$stop_hook_active" == "true" ]] && sha_bool="true"
    local payload="{\"stop_hook_active\":${sha_bool},\"transcript_path\":\"/dev/null\"}"

    local stdout_out
    stdout_out=$(printf '%s' "$payload" \
        | CORE_PROJECTS_DIR="$case_dir" python3 "$HOOK_PY" 2>/dev/null)

    local got_block="false"
    if printf '%s' "$stdout_out" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('decision')=='block' else 1)" 2>/dev/null; then
        got_block="true"
    fi

    if [[ "$got_block" == "$expect_block" ]]; then
        printf "  PASS  %s\n" "$name"
        (( PASS++ )) || true
    else
        printf "  FAIL  %s  (expected_block=%s actual_block=%s)\n" "$name" "$expect_block" "$got_block"
        if [[ -n "$stdout_out" ]]; then
            printf "        stdout: %s\n" "$(printf '%s' "$stdout_out" | head -2)"
        fi
        FAILURES="${FAILURES}\n  - $name: expected_block=$expect_block got_block=$got_block"
        (( FAIL++ )) || true
    fi
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
echo "=== state-claim-gate.py test harness ==="
echo ""
echo "--- TRUE POSITIVES: hook should block ---"

# T01: Classic state-claim — "the hook is broken" with no Read tool
run_case "T01 'the hook is broken', no Read tool" \
    "The say-do-gap hook is broken and has been misfiring all session. It needs to be fixed before the next release goes out." \
    "false" "true"

# T02: Existential claim — "there is no session file" with no Bash/Read
run_case "T02 'there is no session file', no Read tool" \
    "There is no session file for today yet. The session log has not been created which means the open items were never recorded anywhere." \
    "false" "true"

# T03: Recency assertion — "the latest session is complete"
run_case "T03 'the latest session is complete', no Read tool" \
    "The latest session log is complete and all open items from yesterday have been resolved. The memory file is current and accurate." \
    "false" "true"

# T04: Fired-pattern assertion — "hook already fired"
#      The pattern is: \b\w+\s+(?:has|had|already)\s+fired\b
#      "sentinel already fired" matches; "didn't fire" does NOT (verb is "fire" not "fired").
run_case "T04 'hook already fired', no Read tool" \
    "The sentinel hook already fired during the last push, which means the approval gate ran. The pretooluse guard is therefore confirmed active across both stop hooks in the session." \
    "false" "true"

echo ""
echo "--- AUDIT FP REGRESSION (2026-05-22): subordinate-clause fragments should NOT block ---"
echo "    (these were the dominant FP class in the 2026-05-21 hook precision audit)"

# T05: Audit FP "that exist" — relative pronoun before "exist"
run_case "T05 audit FP: 'that exist' relative clause — must NOT block" \
    "Looking at the inventory, the files that exist in the repo right now cover all the core flows we need to maintain. Nothing else is missing from what was specified in the original plan." \
    "false" "false"

# T05b: Audit FP "check exists" — fragment in procedural sentence
run_case "T05b audit FP: 'check exists in flow' fragment — must NOT block" \
    "Looking at the run-brain-update.sh script, that check exists in the early-exit branch already so we don't need to re-add it. The chain stays clean as it stands today." \
    "false" "false"

# T05c: Audit FP "There's no extra" — non-system noun
run_case "T05c audit FP: 'There's no extra' benign — must NOT block" \
    "Looking at this option list and comparing it to what we already had, there's no extra effort needed since the structure was already in place. So we can move on to the next thing." \
    "false" "false"

# T05d: Audit FP "the current listing is" — was pattern 5 (removed)
run_case "T05d audit FP: 'the current listing is at price \$5' — must NOT block" \
    "Pulling from the Etsy API tool result above, the current listing is at price \$5.00 and the tags array is well within the 13-tag limit. So that all aligns with the playbook recommendations from last week." \
    "false" "false"

echo ""
echo "--- AUDIT FN RECOVERY: known-missed real claims must STILL block ---"

# T06b: Audit FN — absence claim about a slash command. Per audit, the previous
#       regex missed this; the new pattern 3 (with exclusion list) still allows
#       \w+ when not in the exclusion set, so "command" matches.
run_case "T06b audit FN: 'No /goal slash command exists' — must block" \
    "Looking at both repos' commands dir and at ~/.claude/skills/, no /goal slash command exists in either location, so the handoff phrasing in the spec file is referring to something we never built. We'd have to add it before any /goal-driven flow could work." \
    "false" "true"

echo ""
echo "--- STOP_HOOK_ACTIVE SHORT-CIRCUIT ---"

# T06: stop_hook_active=true — hook must produce no block, even with state-claim text
run_case "T06 stop_hook_active=true with state-claim text" \
    "The hook is broken and the session log is missing. There is no file for today's session at all." \
    "true" "false"

echo ""
echo "--- EMPTY / SHORT MESSAGE NO-OP ---"

# T07: Empty text — hook skips (last_text is empty)
run_case "T07 empty assistant text, no tools" \
    "" \
    "false" "false"

# T08: Short message (<80 chars) — hook's short-message guard skips it
run_case "T08 short message under 80 chars — no block" \
    "Done, the hook ran." \
    "false" "false"

echo ""
echo "--- ACCURATE CASES: hook should NOT fire ---"

# T09: State-claim WITH a Read tool call — satisfied, no block
run_case "T09 'hook is broken' WITH Read tool call — no block" \
    "After reading the file, the hook is broken and needs a fix — confirmed by the JSONL content shown above in the tool result." \
    "false" "false" \
    "Read"

# T10: State-claim WITH a Bash tool call — satisfied, no block
# T10 EXPECTATION CORRECTED 2026-07-28. This asserted that ONE Bash call satisfies the gate.
# It does not, and should not: this is an ABSENCE claim ("the session log is missing"), and
# per memory.md an absence needs a multi-file grep, not a single read — one command that found
# nothing is exactly how a wrong absence claim gets made. state-claim-gate implements that as
# >=2 read-shaped calls in the turn. The old expectation dated from before that rule and only
# ever passed because the suite was testing an inlined COPY that never had it.
run_case "T10 ABSENCE claim with only ONE read-shaped tool — must block" \
    "The session log is missing for today — confirmed by the bash output above which shows no file at sessions/2026-05-13.md exists." \
    "false" "true" \
    "Bash"

# T10b: the same claim with TWO reads is satisfied. Without this the fix above could have been
# "absence always blocks", which passes T10 and breaks the feature.
run_case "T10b same ABSENCE claim with TWO read-shaped tools — must NOT block" \
    "The session log is missing for today — confirmed by the bash output above which shows no file at sessions/2026-05-13.md exists." \
    "false" "false" \
    "Bash" "Grep"

# T11: State-claim WITH an MCP read tool — satisfied, no block.
#      Uses mcp__apple-events__get_events so is_read_tool() matches the "get" verb prefix.
run_case "T11 state assertion WITH mcp__*__get_* tool — no block" \
    "The calendar event is active and scheduled for 14:00 PDT today — confirmed by the MCP read result shown above in the context." \
    "false" "false" \
    "mcp__apple-events__get_events"

# T12: Pure conversational response with no state-claim language — no block
run_case "T12 pure conversational response — no block" \
    "That's a good approach. The refactoring strategy you described would reduce complexity and improve readability. I'd suggest tackling the data layer first since it has the most coupling. Once that's clean the view layer changes become much more straightforward to reason about." \
    "false" "false"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
    printf "\nFailed cases:%b\n" "$FAILURES"
    exit 1
fi
exit 0
