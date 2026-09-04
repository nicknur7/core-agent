#!/usr/bin/env bash
# Wrapper for learned-validator.py — Stop hook, the BLOCKING half of the
# learned-workflow layer. Blocks when a HALT signal in the prior turn was
# followed by a mutating tool with no acknowledgement. Fails open; kill-switch
# LEARNED_LAYER=0.
set -uo pipefail
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" learned-validator Stop "" 2>/dev/null || true

exec python3 "$(dirname "${BASH_SOURCE[0]}")/learned-validator.py"
