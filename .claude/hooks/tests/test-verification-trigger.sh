#!/usr/bin/env bash
# test-verification-trigger.sh — Test harness for verification-trigger.py
# Usage: bash test-verification-trigger.sh
# Exits 0 if all pass, 1 if any fail.
#
# verification-trigger.py is a UserPromptSubmit hook, not a Stop hook.
# Its stdin shape is: {"prompt": "..."}  (the user's raw prompt)
# It outputs hookSpecificOutput.additionalContext when triggered, or nothing.
#
# Unlike the Stop hooks, it does NOT read JSONL — it only inspects the prompt.
# So we pipe the JSON prompt directly to python3 and check the output.
#
# False-positive note (audit Dim 2):
#   The hook carries hardcoded operator-specific project slugs and school course
#   patterns (ECON 301, etc.). Audit Dim 2 flags this as a templating blocker.  # privacy-ok: illustrative course code, invented — see verification-triggers.json
#   T05 documents the FP: "what happened to the team meeting?" fires the
#   'what-question' trigger even though no system-state file is referenced.
set -uo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd .. && pwd)"
HOOK="$HOOKS_DIR/verification-trigger.py"

PASS=0
FAIL=0
FAILURES=""

TMPDIR_LOCAL=$(mktemp -d /tmp/test-verification-trigger-XXXXXX)
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

# ---------------------------------------------------------------------------
# Helper: run the real hook (verification-trigger.py) with a given prompt.
# Returns stdout. Hook is read-only (no JSONL dep) so we run it directly.
# ---------------------------------------------------------------------------
run_hook() {
    local prompt="$1"
    local stop_hook_active="${2:-false}"
    local json
    json=$(python3 -c "import json,sys; print(json.dumps({'prompt': sys.argv[1], 'stop_hook_active': False}))" "$prompt")
    printf '%s' "$json" | python3 "$HOOK"
}

run_case() {
    local name="$1"
    local prompt="$2"
    local expect_trigger="${3:-false}"   # "true" → expect additionalContext in output

    local stdout_out
    stdout_out=$(run_hook "$prompt" 2>/dev/null || true)

    local got_trigger="false"
    if echo "$stdout_out" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('hookSpecificOutput',{}).get('additionalContext') else 1)" \
        2>/dev/null; then
        got_trigger="true"
    fi

    if [[ "$got_trigger" == "$expect_trigger" ]]; then
        printf "  PASS  %s\n" "$name"
        (( PASS++ )) || true
    else
        printf "  FAIL  %s  (expected_trigger=%s actual_trigger=%s)\n" "$name" "$expect_trigger" "$got_trigger"
        if [[ -n "$stdout_out" ]]; then
            printf "        stdout: %s\n" "$(echo "$stdout_out" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('hookSpecificOutput',{}).get('additionalContext','')[:120])" 2>/dev/null || echo "$stdout_out" | head -1)"
        fi
        FAILURES="${FAILURES}\n  - $name: expected_trigger=$expect_trigger got_trigger=$got_trigger"
        (( FAIL++ )) || true
    fi
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
echo "=== verification-trigger.py test harness ==="
echo ""
echo "--- TRUE POSITIVES: hook should inject additionalContext ---"

# T01: Classic trigger phrase "check the memory file"
run_case "T01 'check the memory file'" \
    "Can you check the memory file for the last session's open items?" \
    "true"

# T02: "show me" trigger
run_case "T02 'show me the session log'" \
    "Show me the current session log for today." \
    "true"

# T03: "why" trigger with state question
run_case "T03 'why isn't the hook firing'" \
    "Why isn't the say-do gap hook firing on the last response?" \
    "true"

# T04: Project slug mentioned — team-assistant
run_case "T04 project slug 'team-assistant'" \
    "What's the status of the team-assistant project?" \
    "true"

# T05a: Course slug trigger — econ-301
run_case "T05a course slug 'econ 301'" \
    "Can you help me with the ECON 301 simulation decisions for Q6?" \
    "true"

# T05b: State-keyword + "?" heuristic
run_case "T05b state-keyword + ? ('is the hook done?')" \
    "Is the hook done?" \
    "true"

echo ""
echo "--- FALSE POSITIVES (audit Dim 2): over-broad trigger patterns ---"
echo "    (EXPECTED-CURRENT-BEHAVIOR — documented, not hard failures)"

# T06: "what happened" fires 'what-question' even for non-system queries.
#      Audit Dim 2: broad 'what-question' / 'how-question' patterns added
#      2026-05-05 catch almost any factual question, including casual chat.
_t06_out=$(run_hook "What happened to the team meeting yesterday?" 2>/dev/null || true)
_t06_triggered=false
echo "$_t06_out" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('hookSpecificOutput',{}).get('additionalContext') else 1)" \
    2>/dev/null && _t06_triggered=true || true
printf "  INFO  T06 broad 'what-question' FP (EXPECTED-CURRENT-BEHAVIOR): hook triggers=%s\n" "$_t06_triggered"
printf "        Audit Dim 2: 'what happened to the team meeting' has no system-state referent\n"
printf "        but fires 'what-question'. Known FP — future tightening opportunity.\n"
(( PASS++ )) || true

# T07: "how do" fires 'how-question' on a general programming question
_t07_out=$(run_hook "How do you reverse a string in Python?" 2>/dev/null || true)
_t07_triggered=false
echo "$_t07_out" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('hookSpecificOutput',{}).get('additionalContext') else 1)" \
    2>/dev/null && _t07_triggered=true || true
printf "  INFO  T07 'how do' generic FP (EXPECTED-CURRENT-BEHAVIOR): hook triggers=%s\n" "$_t07_triggered"
printf "        'how do you reverse a string' has no state-file referent.\n"
(( PASS++ )) || true

echo ""
echo "--- EMPTY / TRIVIAL PROMPT NO-OP ---"

# T08: Empty prompt — hook should produce no output
run_case "T08 empty prompt (len < 4)" \
    "" \
    "false"

# T09: Single-word prompt — too short to match anything meaningful
run_case "T09 single word 'ok'" \
    "ok" \
    "false"

echo ""
echo "--- ACCURATE CASES: hook should NOT fire ---"

# T10: Normal conversational message with no trigger verbs or state refs
run_case "T10 casual chat, no trigger patterns" \
    "Thanks, that looks good. Let's move on to the next item." \
    "false"

# T11: Code question with no state keywords or trigger verbs
run_case "T11 code question, no trigger or state keyword" \
    "Can you write a Python function that sorts a list of dicts by a given key?" \
    "false"

# T12: "read" in context that the hook's pattern excludes ("read me" / "read y")
#      The hook pattern is: r'\bread\b(?!\s*(?:me\b|y\b))'
#      So "read me this story" should NOT trigger.
run_case "T12 'read me this story' — excluded by negative lookahead" \
    "Could you read me this story out loud?" \
    "false"

# T13: Question mark present but no state keyword in heuristic pattern
run_case "T13 '?' with no state keyword — heuristic should not fire" \
    "Can you help me pick a restaurant for dinner tonight?" \
    "false"

# ---------------------------------------------------------------------------
# T14-T17 (2026-07-30, master plan Phase 0.2): "see" after a discourse opener.
#
# Measured on 353 real prompts: "well see that is how i want you to answer", "ok see this is
# the problem" — Nick starting a sentence, not asking anyone to look at anything. 5 spans.
#
# T16/T17 are the SUPERSET floor (plan 3.3): a tune is admissible only if every confirmed true
# positive still fires. Both are real corpus lines. If a later tightening of this rule drops
# them, that is coverage loss wearing the costume of a narrowing, and it fails here.
# ---------------------------------------------------------------------------
run_case "T14 'well see that is' — discourse marker, not a look-at request" \
    "well see that is how i want you to answer your own questions" \
    "false"

run_case "T15 'ok see this is the problem' — discourse marker" \
    "ok see this is the problem this is all human gated" \
    "false"

run_case "T16 'go see the most recent email' — real request, must survive" \
    "What I do want is for you to go see the most recent email from the vendor" \
    "true"

run_case "T17 'lemme see this first' — real request, must survive" \
    "maybe compact? /context lemme see this first" \
    "true"

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
