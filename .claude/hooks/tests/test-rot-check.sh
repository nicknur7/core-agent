#!/usr/bin/env bash
# Tests for rot-check.py — Core-ASI v2 demand-side rot detection + ABA hook.
# Validates: trigger logic, threshold τ (see the TAU constant), 50-turn window, marker once-only.
# Usage: bash test-rot-check.sh
set -euo pipefail

HOOK="${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}/.claude/hooks/rot-check.py"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

PASS=0
FAIL=0

# Build a session JSONL with two phases:
#   stable phase (first N turns): single tool, similar text length, friendly user msgs
#   degraded phase (next M turns): varied tools, varied text lengths, correction + block user msgs
build_session() {
    python3 - "$@" <<'PY'
import json, sys, random
path = sys.argv[1]
stable_n = int(sys.argv[2])
degraded_m = int(sys.argv[3])
degraded_mode = sys.argv[4]  # "corrections", "blocks", "mixed", "none"

random.seed(42)

stable_text = "a" * 200
stable_tools = ["Read"]

degraded_phrases = {
    "corrections": ["no thats not right", "wrong way to do it", "stop and rethink",
                    "actually do it different", "you forgot the spec",
                    "wtf is this bruh", "you didn't read the file",
                    "thats not what i asked", "wait hold on", "ugh redo this"],
    "blocks": ["STATE-CLAIM GATE: response blocked. Add a tool call.",
               "SAY/DO GAP — claim made without action",
               "SENTINEL GUARD — ACTION BLOCKED — Outward-facing action requires Sentinel review",
               "Stop hook feedback: STATE-CLAIM GATE missing read",
               "VERIFICATION TRIGGER detected in user prompt"],
    "mixed": ["no thats wrong",
              "STATE-CLAIM GATE: missing read",
              "stop please redo",
              "SAY/DO GAP - missing action",
              "actually redo entirely",
              "wtf bruh thats off",
              "you forgot the spec",
              "SENTINEL GUARD blocked",
              "wrong direction completely",
              "Stop hook feedback fired"],
    "none": ["ok continue", "great keep going", "yes that works",
             "ok next step", "sure proceed"],
}
degraded_tools = ["Bash", "Write", "Edit", "Grep", "Glob"]

with open(path, "w") as f:
    # Stable phase
    for i in range(stable_n):
        user_text = "ok turn " + str(i)
        f.write(json.dumps({"type": "user", "message": {"content": user_text}}) + "\n")
        content = [
            {"type": "text", "text": stable_text},
            {"type": "tool_use", "name": stable_tools[0], "input": {}}
        ]
        f.write(json.dumps({"type": "assistant", "message": {"content": content}}) + "\n")

    # Degraded phase
    pool = degraded_phrases[degraded_mode]
    for i in range(degraded_m):
        user_text = pool[i % len(pool)]
        f.write(json.dumps({"type": "user", "message": {"content": user_text}}) + "\n")
        # Vary tool choice and text length to drop T_sel and B_length
        tool = degraded_tools[i % len(degraded_tools)]
        text_len = 30 + (i * 47) % 800  # range 30..830
        text = "x" * text_len
        content = [
            {"type": "text", "text": text},
            {"type": "tool_use", "name": tool, "input": {}}
        ]
        f.write(json.dumps({"type": "assistant", "message": {"content": content}}) + "\n")
PY
}

run_hook() {
    local jsonl="$1"
    local key
    key=$(printf '%s' "$jsonl" | shasum -a 256 | cut -c1-16)
    rm -f "/tmp/rot-check-fired-${key}"
    local stdin_json
    stdin_json=$(printf '{"transcript_path":"%s","cwd":"'"${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"'"}' "$jsonl")
    printf '%s' "$stdin_json" | python3 "$HOOK" 2>&1 || true
}

check_test() {
    local name="$1" output="$2" should_fire="$3"
    local fired="no"
    if [[ -n "$output" ]] && printf '%s' "$output" | grep -q "ROT WARNING"; then
        fired="yes"
    fi
    if [[ "$fired" == "$should_fire" ]]; then
        echo "  PASS: $name (expected=$should_fire, got=$fired)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name (expected=$should_fire, got=$fired)"
        if [[ -n "$output" ]]; then
            local asi_line
            asi_line=$(printf '%s' "$output" | grep -o "Core-ASI [0-9.]\+" | head -1)
            echo "    Detected: $asi_line"
        fi
        FAIL=$((FAIL + 1))
    fi
}

echo "=== rot-check.py (Core-ASI v2) tests ==="

# Test 1: too short — no fire (needs 70 assistant turns minimum)
jsonl="$TMPDIR/short.jsonl"
build_session "$jsonl" 50 0 "none"
check_test "too-short-no-fire" "$(run_hook "$jsonl")" "no"

# Test 2: long + stable + friendly → no fire (high ASI)
jsonl="$TMPDIR/stable.jsonl"
build_session "$jsonl" 80 0 "none"
check_test "stable-session-no-fire" "$(run_hook "$jsonl")" "no"

# Test 3: stable then degraded with corrections → fire
jsonl="$TMPDIR/corrections.jsonl"
build_session "$jsonl" 50 50 "corrections"
check_test "corrections-fire" "$(run_hook "$jsonl")" "yes"

# Test 4: stable then degraded with hook blocks → fire
jsonl="$TMPDIR/blocks.jsonl"
build_session "$jsonl" 50 50 "blocks"
check_test "hook-blocks-fire" "$(run_hook "$jsonl")" "yes"

# Test 5: stable then degraded mixed → fire
jsonl="$TMPDIR/mixed.jsonl"
build_session "$jsonl" 50 50 "mixed"
check_test "mixed-corruption-fire" "$(run_hook "$jsonl")" "yes"

# Test 6: marker prevents re-fire
jsonl="$TMPDIR/marker.jsonl"
build_session "$jsonl" 50 50 "corrections"
key=$(printf '%s' "$jsonl" | shasum -a 256 | cut -c1-16)
rm -f "/tmp/rot-check-fired-${key}"
stdin=$(printf '{"transcript_path":"%s","cwd":"'"${CORE_INSTANCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"'"}' "$jsonl")
first=$(printf '%s' "$stdin" | python3 "$HOOK" 2>&1 || true)
second=$(printf '%s' "$stdin" | python3 "$HOOK" 2>&1 || true)
if printf '%s' "$first" | grep -q "ROT WARNING" && [[ -z "$second" ]]; then
    echo "  PASS: marker-once-only (first fired, second silent)"
    PASS=$((PASS + 1))
else
    echo "  FAIL: marker-once-only"
    FAIL=$((FAIL + 1))
fi
rm -f "/tmp/rot-check-fired-${key}"

# Test 7: ABA re-anchors the DRAGGING DIMENSION and stays inside its payload budget.
#
# This test used to assert the opposite — that ABA injected CLAUDE.md and the rules files
# verbatim. That was 8,198 tokens of files already sitting in context, i.e. a duplicate of the
# steering surface rather than an anchor, and at a 30% session fire rate it was the single
# largest injection cost in the system. Phase 0.4 replaced it with a dimension-targeted anchor.
#
# The budget assertion is the point: without a ceiling in the suite, the payload grows back one
# helpful paragraph at a time, which is exactly how it reached 8,198 in the first place.
jsonl="$TMPDIR/aba.jsonl"
build_session "$jsonl" 50 50 "mixed"
output=$(run_hook "$jsonl")
ctx=$(printf '%s' "$output" | python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit
print((d.get("hookSpecificOutput") or {}).get("additionalContext",""))')
n_chars=${#ctx}
budget=6000   # ~1,500 tok, the Phase 0.4 ceiling
if printf '%s' "$ctx" | grep -q "RE-ANCHOR: " \
   && printf '%s' "$ctx" | grep -q "ROT WARNING" \
   && [ "$n_chars" -gt 200 ] && [ "$n_chars" -le "$budget" ]; then
    echo "  PASS: aba-anchors-dragging-dimension (${n_chars} chars, budget ${budget})"
    PASS=$((PASS + 1))
else
    echo "  FAIL: aba-anchors-dragging-dimension (${n_chars} chars, budget ${budget})"
    FAIL=$((FAIL + 1))
fi

# Test 8: Option B surfacing instruction is in payload
output=$(run_hook "$jsonl")
if printf '%s' "$output" | grep -q "PREPEND this exact line"; then
    echo "  PASS: option-b-surface-instruction"
    PASS=$((PASS + 1))
else
    echo "  FAIL: option-b-surface-instruction"
    FAIL=$((FAIL + 1))
fi

# Test 9: malformed JSONL → exits silently
jsonl="$TMPDIR/malformed.jsonl"
printf 'this is not json\n{"broken": \n' > "$jsonl"
output=$(run_hook "$jsonl")
if [[ -z "$output" ]]; then
    echo "  PASS: malformed-jsonl-silent"
    PASS=$((PASS + 1))
else
    echo "  FAIL: malformed-jsonl-silent"
    FAIL=$((FAIL + 1))
fi

# Test 10: empty stdin → exits silently
output=$(printf '' | python3 "$HOOK" 2>&1 || true)
if [[ -z "$output" ]]; then
    echo "  PASS: empty-stdin-silent"
    PASS=$((PASS + 1))
else
    echo "  FAIL: empty-stdin-silent"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] || exit 1
