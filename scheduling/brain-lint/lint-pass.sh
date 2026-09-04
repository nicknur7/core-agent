#!/usr/bin/env bash
# Brain lint pass — runs lint.py (v1) only.
#
# The v3 contradiction-detection brief generator (lint-v3.py) was OBSOLETED
# 2026-05-18 — its use case is now subsumed by compile-truth (brain-pg Step 3:
# per-hub synthesis with confidence scores) + the hybrid RRF query layer at
# scheduling/brain-pg/query.py. The `--contradictions` flag is therefore
# removed; older callers should drop that flag (it is no longer accepted).
#
# Usage:
#   lint-pass.sh   →  v1 only (gap-topics + orphan-pages + gap-memory)
#
# Output: memory/brain-lint-reports/YYYY-MM-DD.md (v1 report)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATE=$(date +%Y-%m-%d)
# Per-Core: every Core linting on the same day shared one log and interleaved.
LOG="/tmp/brain-lint-$(basename "${CORE_INSTANCE:-core}")-$DATE.log"

# Reject any arg — keep the surface small. If callers pass --contradictions
# (the obsoleted flag), error loudly so they know to update their callsite.
if (( $# > 0 )); then
  echo "lint-pass.sh: no flags accepted. v3 contradiction pass was obsoleted 2026-05-18 — drop --contradictions if you were passing it." >&2
  exit 2
fi

# Prefer uv-managed Python (has graphify deps); fall back to system python3
PYTHON="$HOME/.local/share/uv/tools/graphifyy/bin/python"
[[ ! -x "$PYTHON" ]] && PYTHON="python3"

echo "[$(date)] === Brain lint pass starting ===" >> "$LOG"
"$PYTHON" "$SCRIPT_DIR/lint.py" 2>&1 | tee -a "$LOG"
V1_EXIT=$?
echo "[$(date)] === v1 complete (exit=$V1_EXIT) ===" >> "$LOG"
exit "$V1_EXIT"
