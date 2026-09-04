#!/usr/bin/env bash
# log-only-ping.sh — the v1 catalog's ONE shipped action (GAP A-executable-effect, 2026-08-31).
#
# Does exactly one thing: appends a single timestamped line to a receipts file under
# .claude/state/friction-artifacts/ (per-Core, gitignored). No network, no git, no email/SMS, no
# write outside .claude/state, no argv, no stdin read. It exists to prove the run-mode lifecycle
# (enqueue -> drain -> execute -> receipt -> per-session/per-week cap -> one-strike quarantine on
# failure) end to end BEFORE any action with a real side effect gets its own catalog PR — each real
# entry is its own PR (judge requirement 6), and this is the entry that proves the road works.
#
# Invoked ONLY by friction_runner.py's single locked subprocess call site, with a scrubbed env
# (PATH, HOME, FRICTION_ARTIFACT_ID re-validated against ^art_[a-z0-9_]{1,64}$). Never invoked
# with artifact-supplied arguments — the catalog entry names this script; nothing an artifact
# controls ever reaches argv or env beyond that one re-validated id.
set -euo pipefail
STATE_DIR="${CORE_INSTANCE:-${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}/.claude/state/friction-artifacts"
mkdir -p "$STATE_DIR"
printf '%s artifact_id=%s pid=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${FRICTION_ARTIFACT_ID:-unknown}" "$$" \
  >> "$STATE_DIR/run-receipts-script.log"
exit 0
