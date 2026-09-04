#!/usr/bin/env bash
# Wrapper for learned-classifier.py — UserPromptSubmit hook, the INJECT half of the
# learned-workflow layer. Keyword-triggered (no embedding); injects the matched
# learned contract's guidance. Never blocks; fails open; kill-switch LEARNED_LAYER=0.
set -uo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/learned-classifier.py"
