#!/usr/bin/env bash
# test-say-do-gap.sh — Test harness for say-do-gap.py
# Usage: bash test-say-do-gap.sh
# Exits 0 if all pass, 1 if any fail.
#
# Strategy: write a thin wrapper script that substitutes PROJECTS_DIR via an
# env var, then pipe the Stop-hook JSON payload to it.  The heredoc-to-stdin
# conflict (python3 - <<'EOF' + pipe) is avoided by writing the script to a
# temp file and running it as python3 <file>.
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

TMPDIR_LOCAL=$(mktemp -d /tmp/test-say-do-gap-XXXXXX)
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

# ---------------------------------------------------------------------------
# Write the patched hook to a temp file (mirrors say-do-gap.py exactly;
# only PROJECTS_DIR is sourced from env var SAY_DO_GAP_TEST_DIR).
# ---------------------------------------------------------------------------
# RUN THE REAL HOOK. This used to write an inlined COPY of say-do-gap.py to a temp file and
# test that — so changing or breaking the real hook could not fail this suite. The copy had
# already drifted: it still logged to the abandoned global /tmp/core-hook-events.log, which is
# where the phantom "hook=say-do-gap-test" events on a freshly spawned Core came from. The
# bleed and the dead test were one bug. (core-business, 2026-07-28.)
#
# The copy existed to override PROJECTS_DIR, which the hook computed from cwd. The real hook
# now honours CORE_PROJECTS_DIR, so no duplicate is needed.
HOOK_PY="$HOOKS_DIR/say-do-gap.py"
[[ -f "$HOOK_PY" ]] || { echo "FATAL: $HOOK_PY not found — cannot test the real hook"; exit 1; }

# ---------------------------------------------------------------------------
# Helper: write a synthetic JSONL fixture.
# make_jsonl <dest_dir> <text> [tool1 tool2 …]
# ---------------------------------------------------------------------------
make_jsonl() {
    local dest_dir="$1"
    local text="$2"
    shift 2
    local tools=("$@")
    local jsonl="$dest_dir/session-test.jsonl"
    mkdir -p "$dest_dir"

    # Build content array via Python to get correct JSON encoding of text
    python3 - "$text" "${tools[@]:-}" <<'PYBUILD'
import json, sys
text = sys.argv[1]
tool_names = [a for a in sys.argv[2:] if a]
content = [{"type": "text", "text": text}]
for t in tool_names:
    content.append({"type": "tool_use", "name": t, "id": "x", "input": {}})
print(json.dumps({"type": "assistant", "message": {"content": content}}))
PYBUILD
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
    make_jsonl "$case_dir" "$text" "${tools[@]:-}" > "$case_dir/session-test.jsonl"

    # Build Stop-hook JSON payload (no python interpolation needed)
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
echo "=== say-do-gap.py test harness ==="
echo ""
echo "--- TRUE POSITIVES: hook should block ---"

# T01: Classic future-tense claim with no Write — obvious gap
run_case "T01 future-tense: 'I'll save', no Write tool" \
    "I'll save the session log to memory/current-state.md now." \
    "false" "true"

# T02: Present-progressive claim with no Edit
run_case "T02 present-progressive: 'Saving this to', no Edit tool" \
    "Saving this to the lessons file — done." \
    "false" "true"

# T03: Past-tense save claim with no Write/Edit. Updated 2026-05-22: requires
# first-person/recency anchor to fire (per tightened PAST_TENSE regex).
run_case "T03 past-tense: 'I just saved to memory', no Write tool" \
    "Done — I just saved that to memory/current-state.md as requested." \
    "false" "true"

# T04: Multiple triggers in one message, no Write
run_case "T04 multiple triggers (future + past), no Write tool" \
    "I'll log this decision. Also noted the verdict in the access-log above — added to memory." \
    "false" "true"

echo ""
echo "--- AUDIT FP REGRESSION (2026-05-22): bare past-tense fragments should NOT block ---"
echo "    (these were 22/25 of all FPs in the 2026-05-21 hook precision audit)"

# T05: Past-tense claim referencing a PRIOR turn's action — now FIXED.
#      Tightened PAST_TENSE requires first-person/recency anchor ("i/we/just/now/already").
#      Bare past tense in retrospective narrative no longer fires.
run_case "T05 prior-turn-save (no first-person) — must NOT block (audit fix)" \
    "The session log was saved to disk earlier this session — nothing changed since then." \
    "false" "false"

# T05b: Audit FP sample "[past] persisted at" shape — bare third-person past
run_case "T05b audit FP: 'persisted at' bare fragment — must NOT block" \
    "The decision was persisted at the start of this thread, and the rationale remains the same as before. Nothing else to report from that chain of work earlier today." \
    "false" "false"

# T05c: Audit FP sample "[past] appended to" — narrative past tense
run_case "T05c audit FP: 'appended to' narrative — must NOT block" \
    "Earlier this session that note was appended to the lessons file and is still there in active rules section, with no further changes since the original landing two turns back." \
    "false" "false"

# T05d: Audit FP sample "[past] logged in" — bare past
run_case "T05d audit FP: 'logged in' bare narrative — must NOT block" \
    "The verdict from Sentinel was logged in the access-log earlier in the chain, which closes that branch of the work." \
    "false" "false"

echo ""
echo "--- STOP_HOOK_ACTIVE SHORT-CIRCUIT ---"

# T06: stop_hook_active=true — hook must produce no block, even with trigger text
run_case "T06 stop_hook_active=true with future-tense trigger" \
    "I'll save this to memory now." \
    "true" "false"

echo ""
echo "--- EMPTY / SHORT MESSAGE NO-OP ---"

# T07: Empty assistant text — hook exits 0, no block
run_case "T07 empty text, no tools" \
    "" \
    "false" "false"

# T08: Very short ack — no block
run_case "T08 single-word ack 'Done.'" \
    "Done." \
    "false" "false"

echo ""
echo "--- ACCURATE CASES: hook should NOT fire ---"

# T09: Future-tense claim WITH a Write tool call — satisfied, no block
run_case "T09 'I'll save' WITH Write tool — no block" \
    "I'll save the session log to memory/current-state.md now." \
    "false" "false" \
    "Write"

# T10: Past-tense claim WITH an Edit tool call — satisfied, no block
run_case "T10 'saved to' WITH Edit tool — no block" \
    "Saved the verdict to memory/decisions-log.md." \
    "false" "false" \
    "Edit"

# T11: Normal technical prose, no action-claim language — no block
run_case "T11 factual explanation, no action language" \
    "The hook fires on Stop events. It reads the last assistant message from the JSONL and checks for action-claim patterns. The regex is anchored with word boundaries to reduce false positives." \
    "false" "false"

# T12: Past-tense verb but no pattern-match object complement — no block
run_case "T12 'tracked across sessions' — no object match" \
    "This approach has been tracked across all sessions." \
    "false" "false"

echo ""
echo "--- AUDIT TP RETENTION: first-person past-tense still blocks ---"

# T13: First-person past-tense claim with no Write — should still block
run_case "T13 'I saved to memory' (first-person past) — must block" \
    "I saved the decision to memory/decisions-log.md as you asked, so the audit trail is in place for next session." \
    "false" "true"

# T14: First-person past-tense with Write tool — satisfied
run_case "T14 'I saved to memory' WITH Write tool — must NOT block" \
    "I saved the decision to memory/decisions-log.md as you asked." \
    "false" "false" \
    "Write"

# T15: 'we just logged' — first-person plural + recency anchor
run_case "T15 'we just logged' no Write — must block" \
    "We just logged that outcome in the session file so it's captured before close — should appear in tonight's prune." \
    "false" "true"

echo ""
echo "--- AUDIT FN RECOVERY (2026-05-21): forward commitments still block ---"

# T16: Audit FN sample — "I'll write the full doc to disk"
run_case "T16 audit FN: 'I'll write the full doc' no Write — must block" \
    "Capabilities.md + settings.json to ensure the inventory matches reality, then I'll write the full doc to disk and give you a tight pointer to it." \
    "false" "true"

# T17: Audit FN sample — "Let me write the plan to a spec file"
run_case "T17 audit FN: 'Let me write the plan' no Write — must block" \
    "I have what I need. The diagnosis is clear. Let me write the plan to a spec file the operator can paste into /goal so the autonomous build picks it up cleanly." \
    "false" "true"

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
