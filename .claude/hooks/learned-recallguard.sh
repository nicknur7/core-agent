#!/usr/bin/env bash
# Wrapper for learned-recallguard.py — learned-workflow blocking guard. Fails open; kill-switch LEARNED_LAYER=0.
set -uo pipefail
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" learned-recallguard Stop "" 2>/dev/null || true

exec python3 "$(dirname "${BASH_SOURCE[0]}")/learned-recallguard.py"
