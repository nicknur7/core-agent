#!/usr/bin/env bash
# Wrapper for lint-org-scoping.py — flags unsafe org_id interpolation in brain-pg SQL.
exec python3 "$(dirname "$0")/lint-org-scoping.py" "$@"
