#!/usr/bin/env bash
# Wrapper for say-do-gap.py — Stop hook that detects say/do gaps in the last
# assistant message and blocks the stop if found.
set -uo pipefail
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" say-do-gap Stop "" 2>/dev/null || true

exec python3 "$(dirname "${BASH_SOURCE[0]}")/say-do-gap.py"
