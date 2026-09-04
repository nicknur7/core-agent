#!/usr/bin/env bash
# Wrapper for lint-code-paths.py — checks .sh/.py for hardcoded registry-tracked paths.
exec python3 "$(dirname "$0")/lint-code-paths.py" "$@"
