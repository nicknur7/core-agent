#!/usr/bin/env bash
# Wrapper for lint-doc-paths.py — runs the markdown doc-path lint.
exec python3 "$(dirname "$0")/lint-doc-paths.py" "$@"
