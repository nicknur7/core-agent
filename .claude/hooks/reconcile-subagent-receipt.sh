#!/usr/bin/env bash
# SubagentStop hook — authenticated evidence that close-reconciler actually ran.
#
# Registered in settings.json under SubagentStop with matcher "close-reconciler",
# so it fires ONLY when that subagent terminates. This is the un-forgeable link in
# the reconcile-enforcement chain (Codex-specified 2026-07-17): the model cannot fake
# a subagent termination, so this marker proves the reconciler genuinely ran this
# session. The disposition receipt (reconcile-receipt.py) then requires this marker,
# and the close controller requires the receipt. "Ran" ≠ "reconciled" — this only
# proves RAN; disposition is attested separately.
#
# Writes .claude/state/.reconcile-ran (cleared at genuine SessionStart). Also stashes
# the reconciler's final message as the report-of-record for the receipt digest.
# Fail-open: never blocks. Exit 0 always.
set -uo pipefail

# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" reconcile-subagent-receipt SubagentStop "" 2>/dev/null || true

INSTANCE="${CORE_INSTANCE:-${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}"
STATE_DIR="$INSTANCE/.claude/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

INPUT="$(cat 2>/dev/null || true)"

# FAIL-CLOSED agent_type gate (2026-07-18 forgery fix): mint the RAN marker ONLY when the
# stopping subagent is EXACTLY "close-reconciler". The old check (`-n AGENT_TYPE && != close-reconciler`)
# failed OPEN on an empty/missing agent_type — so ANY subagent's SubagentStop minted .reconcile-ran,
# letting reconcile-receipt.py write a receipt with no real reconciler run (verified this session: a
# general-purpose subagent forged the marker). Now anything not exactly "close-reconciler" — including
# empty/unknown — exits without minting. Matcher config is NOT trusted as authentication.
AGENT_TYPE="$(printf '%s' "$INPUT" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("agent_type",""))
except Exception: print("")' 2>/dev/null || echo "")"
if [[ "$AGENT_TYPE" != "close-reconciler" ]]; then
  exit 0
fi

# Stash the reconciler's final message (its report) for the receipt digest.
printf '%s' "$INPUT" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); sys.stdout.write(d.get("last_assistant_message","") or "")
except Exception: pass' > "$STATE_DIR/.reconcile-report" 2>/dev/null || true

# The authenticated RAN marker.
{ date -u '+%Y-%m-%dT%H:%M:%SZ'; } > "$STATE_DIR/.reconcile-ran" 2>/dev/null || true

exit 0
