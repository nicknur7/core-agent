#!/usr/bin/env bash
# Print the first real user-text message timestamp of the current
# (most-recently-modified) JSONL transcript, in PDT/PST.
# Format: YYYY-MM-DD HH:MM TZ
# Used at session start to write accurate session-file timestamps.
# Fix for 2026-04-29 audit Section 9 (no more "~morning PDT" guesses).
set -uo pipefail

# SLUG: every non-alphanumeric becomes a dash, which is what Claude Code does and what
# bin/core_seat.py::transcripts_dir() is the canonical definition of. This read
# `s|^/||; s|[/ ]|-|g` — only slash and space — so it diverged on any path containing a dot
# or an underscore, producing a directory that does not exist. That reads as an EMPTY
# HISTORY rather than a bad path, which is a fail-toward-PASS. Kept as a local copy rather
# than an import because a shell hook must not depend on bin/ being present; drift is
# prevented by bin/tests/test_slug_agreement.py, which pins every copy to the canonical one.
PROJECTS_DIR="$HOME/.claude/projects/$(pwd | sed 's|[^A-Za-z0-9]|-|g')"
LATEST_JSONL=$(ls -t "$PROJECTS_DIR"/*.jsonl 2>/dev/null | head -1)
if [[ -z "${LATEST_JSONL:-}" || ! -f "$LATEST_JSONL" ]]; then
  echo "ERROR: no JSONL transcript found in $PROJECTS_DIR" >&2
  exit 1
fi

python3 - "$LATEST_JSONL" <<'PY'
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo
pdt = ZoneInfo("America/Los_Angeles")
ts = None
with open(sys.argv[1]) as f:
    for line in f:
        try: d = json.loads(line)
        except Exception: continue
        if d.get("type") != "user": continue
        msg = d.get("message") or {}
        if msg.get("role") != "user": continue
        c = msg.get("content")
        text = ""
        if isinstance(c, str): text = c
        elif isinstance(c, list):
            if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c): continue
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    text = p.get("text", ""); break
        if not text or text.startswith("<"): continue
        ts = d.get("timestamp"); break
if ts:
    dt = datetime.strptime(ts.replace("Z","+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(pdt)
    print(dt.strftime("%Y-%m-%d %H:%M %Z"))
else:
    print("ERROR: no user-text message found in transcript", file=sys.stderr)
    sys.exit(1)
PY
