#!/usr/bin/env bash
# core-notify.sh — fire a LOCAL macOS notification. No network, no outward send.
#
#   core-notify.sh "<title>" "<message>" [subtitle]
#
# Used by the close-time core-si pass (bin/core-si-close.sh) to surface critical
# improvements, trust-streak admissions, and first auto-fires. This is the present-day
# stand-in for the future Core OS one-click approve/deny card — same intent, simpler surface.
#
# Local-only: uses osascript `display notification` (built-in; terminal-notifier not required).
# pretooluse-guard allowlists this script by name (it pattern-matches notification strings as
# outward, but a local desktop notification leaves no device). Fail-open: never errors a caller.
set -uo pipefail

TITLE="${1:-Core}"
MESSAGE="${2:-}"
SUBTITLE="${3:-}"

[[ -z "$MESSAGE" ]] && exit 0

# Escape double quotes for the AppleScript string literals.
_esc() { printf '%s' "$1" | sed 's/"/\\"/g'; }
TITLE_E=$(_esc "$TITLE")
MESSAGE_E=$(_esc "$MESSAGE")
SUBTITLE_E=$(_esc "$SUBTITLE")

if [[ -n "$SUBTITLE_E" ]]; then
  osascript -e "display notification \"${MESSAGE_E}\" with title \"${TITLE_E}\" subtitle \"${SUBTITLE_E}\"" >/dev/null 2>&1 || true
else
  osascript -e "display notification \"${MESSAGE_E}\" with title \"${TITLE_E}\"" >/dev/null 2>&1 || true
fi
exit 0
