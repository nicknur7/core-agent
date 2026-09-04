#!/usr/bin/env bash
# Wrapper for state-claim-gate.py — Stop hook.
# Mirrors say-do-gap.sh pattern.
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" state-claim-gate Stop "" 2>/dev/null || true

exec python3 "$(dirname "$0")/state-claim-gate.py"
