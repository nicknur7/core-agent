#!/usr/bin/env bash
# Wrapper for brain-recall-trigger.py — keeps the hook invocation in
# settings.json on a stable path regardless of python implementation.
set -uo pipefail
# --- telemetry: record that this hook RAN (see lib/hookinvoke.sh) ---
"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" brain-recall-trigger UserPromptSubmit "" 2>/dev/null || true

exec python3 "$(dirname "${BASH_SOURCE[0]}")/brain-recall-trigger.py"
