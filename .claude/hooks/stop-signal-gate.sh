#!/usr/bin/env bash
# Wrapper for stop-signal-gate.py — structural enforcement of stop/no/frustration
# signals (the non-graduating correction patterns per measure-rule-fitness).
set -uo pipefail
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" stop-signal-gate UserPromptSubmit "" 2>/dev/null || true

exec python3 "$(dirname "${BASH_SOURCE[0]}")/stop-signal-gate.py"
