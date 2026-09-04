#!/usr/bin/env bash
# Backward-compat shim. core-status.sh was promoted to bin/core-doctor.sh
# per audit tasks/core-audit-report-2026-05-11.md item #13 (Dim 9 #1).
# Update any caller (CLAUDE.md, scripts, hooks) to call the new path directly.
exec bash "$CORE_ENGINE/bin/core-doctor.sh" "$@"
