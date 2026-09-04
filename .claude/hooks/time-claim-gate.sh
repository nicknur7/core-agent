#!/usr/bin/env bash
# Wrapper for time-claim-gate.py — Stop hook.
# Mirrors state-claim-gate.sh pattern.
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" time-claim-gate Stop "" 2>/dev/null || true

exec python3 "$(dirname "$0")/time-claim-gate.py"
