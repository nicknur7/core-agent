#!/usr/bin/env bash
# Compute current session's wall-time duration from JSONL transcript.
# Reads the most-recently-modified JSONL in the project's Claude projects dir,
# finds the first real user-text message timestamp (start) and the last event
# timestamp (end), prints structured stdout:
#
#   START: 2026-05-15 18:01 PDT
#   END:   2026-05-15 19:03 PDT
#   WALL:  1h02m
#   NOTE:  wall-time first-to-last event; includes idle gaps
#
# Used by /close-core to write JSONL-sourced durations into current-state.md
# and session logs instead of Claude's recollection.
# Spec: tasks/specs/spec-time-awareness-fix-2026-05-15.md Phase 2.
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
first_ts = None
last_ts = None

with open(sys.argv[1]) as f:
    for line in f:
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("timestamp")
        if not ts:
            continue
        # First user-text message — same logic as get-session-start-time.sh
        if first_ts is None and d.get("type") == "user":
            msg = d.get("message") or {}
            if msg.get("role") == "user":
                c = msg.get("content")
                text = ""
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c):
                        # Skip tool_result envelopes; they're not user text.
                        last_ts = ts
                        continue
                    for p in c:
                        if isinstance(p, dict) and p.get("type") == "text":
                            text = p.get("text", "")
                            break
                if text and not text.startswith("<"):
                    first_ts = ts
        last_ts = ts

if not first_ts:
    print("ERROR: no user-text message found in transcript", file=sys.stderr)
    sys.exit(1)

def parse(ts):
    return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(pdt)

dt_first = parse(first_ts)
dt_last = parse(last_ts) if last_ts else dt_first
dur = dt_last - dt_first
total_secs = int(dur.total_seconds())
h, rem = divmod(total_secs, 3600)
m, _ = divmod(rem, 60)

if h > 0:
    wall = f"{h}h{m:02d}m"
else:
    wall = f"{m}m"

idle_marker = " (includes idle gaps)" if total_secs > 3 * 3600 else ""

print(f"START: {dt_first.strftime('%Y-%m-%d %H:%M %Z')}")
print(f"END:   {dt_last.strftime('%Y-%m-%d %H:%M %Z')}")
print(f"WALL:  {wall}{idle_marker}")
print(f"NOTE:  wall-time first-to-last event; includes idle gaps when WALL > 3h")
PY
