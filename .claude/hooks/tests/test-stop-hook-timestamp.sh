#!/usr/bin/env bash
# test-stop-hook-timestamp.sh — Tests the Last-updated timestamp validator logic
# from stop-hook.sh in isolation.
# Usage: bash test-stop-hook-timestamp.sh
# Exits 0 if all pass, 1 if any fail.
set -uo pipefail

PASS=0
FAIL=0

# Inline the validator function (matches stop-hook.sh exactly)
check_timestamp() {
  local state_file="$1"
  local warning=""
  if [[ -f "$state_file" ]]; then
    local last_updated_line
    last_updated_line=$(grep -m1 '^Last updated:' "$state_file" || true)
    if [[ -z "$last_updated_line" ]]; then
      warning="⚠ current-state.md Last-updated line missing or malformed timestamp"
    elif ! echo "$last_updated_line" | grep -qE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} [A-Z]{2,4}'; then
      warning="⚠ current-state.md Last-updated line missing or malformed timestamp"
    fi
  fi
  printf '%s' "$warning"
}

run_case() {
  local name="$1"
  local content="$2"
  local expect_warn="$3"   # "warn" or "ok"

  local tmpfile
  tmpfile=$(mktemp /tmp/test-current-state-XXXXXX.md)
  printf '%s' "$content" > "$tmpfile"

  local result
  result=$(check_timestamp "$tmpfile")
  rm -f "$tmpfile"

  local got_warn="ok"
  [[ -n "$result" ]] && got_warn="warn"

  if [[ "$got_warn" == "$expect_warn" ]]; then
    printf "  PASS  %s\n" "$name"
    (( PASS++ )) || true
  else
    printf "  FAIL  %s  (expected=%s actual=%s)\n" "$name" "$expect_warn" "$got_warn"
    (( FAIL++ )) || true
  fi
}

echo "=== test-stop-hook-timestamp.sh ==="
echo ""

# T1: Good line — should NOT warn
run_case "T1 good timestamp (PDT)" \
  "# Current State
Last updated: 2026-04-27 14:30 PDT
Some content here." \
  "ok"

# T2: Bad line — date only, no time — should warn
run_case "T2 bad timestamp (date only)" \
  "# Current State
Last updated: 2026-04-27
Some content here." \
  "warn"

# T3: Missing Last-updated line entirely — should warn
run_case "T3 missing Last-updated line" \
  "# Current State
Some content without a Last updated line." \
  "warn"

# T4: Good line with UTC timezone
run_case "T4 good timestamp (UTC)" \
  "Last updated: 2026-01-01 00:00 UTC" \
  "ok"

# T5: Bad line — free-form text after colon
run_case "T5 bad timestamp (free-form text)" \
  "Last updated: today around noon" \
  "warn"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
