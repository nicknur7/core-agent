---
description: Show the current session's health — token estimate, tool call count, elapsed time — and a continue / /clear / close recommendation. On-demand version of the staleness-check hook.
---

# /health

Run the session-health readout. Output should be tight (≤10 lines).

## What to do

Run the following bash command to compute the metrics from the current JSONL transcript:

```bash
# SLUG: every non-alphanumeric becomes a dash — the canonical rule, defined by
# bin/core_seat.py::transcripts_dir(). This read `-$(pwd | sed 's|^/||; s|[/ ]|-|g')`, replacing
# only slash and space, so it diverged on any path containing a dot, an underscore or a bracket.
# The two shell copies were corrected on 2026-08-10 and this one was missed because it lives in a
# .md: bin/tests/test_slug_agreement.py pins a HARDCODED LIST of two named .sh files plus
# .claude/hooks/*.py, and the ratchet scans rglob("*.py"). Neither could see a command file.
PROJECTS_DIR="$HOME/.claude/projects/$(pwd | sed 's|[^A-Za-z0-9]|-|g')"
JSONL=$(ls -t "$PROJECTS_DIR"/*.jsonl 2>/dev/null | head -1)
[[ -z "$JSONL" ]] && { echo "No JSONL found at $PROJECTS_DIR — health metrics require an active session transcript."; exit 1; }
CORE_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
BRAIN_RECALL_STATE="$CORE_DIR/.claude/state"
python3 - "$JSONL" "$BRAIN_RECALL_STATE" <<'PY'
import json, sys, os
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
TOKEN_LIMIT = 75_000
TOOL_LIMIT = 80
ELAPSED_LIMIT_MIN = 240
path = sys.argv[1]
brain_recall_state_dir = sys.argv[2] if len(sys.argv) > 2 else None
token_chars = 0; tool_count = 0; first_ts = None; last_ts = None
def add_text(s):
    global token_chars
    if isinstance(s, str): token_chars += len(s)
with open(path) as f:
    for line in f:
        try: d = json.loads(line)
        except: continue
        t = d.get('type'); ts = d.get('timestamp')
        if t not in ('user','assistant'): continue
        msg = d.get('message') or {}
        content = msg.get('content')
        if t == 'user' and ts and first_ts is None:
            txt = ''
            if isinstance(content, str): txt = content
            elif isinstance(content, list):
                if not any(isinstance(p, dict) and p.get('type') == 'tool_result' for p in content):
                    for p in content:
                        if isinstance(p, dict) and p.get('type') == 'text':
                            txt = p.get('text','') or ''; break
            if txt and not txt.startswith('<'): first_ts = ts
        if isinstance(content, str): add_text(content)
        elif isinstance(content, list):
            for p in content:
                if not isinstance(p, dict): continue
                pt = p.get('type')
                if pt == 'tool_use': tool_count += 1
                elif pt == 'text': add_text(p.get('text',''))
                elif pt == 'tool_result':
                    c = p.get('content','')
                    if isinstance(c, str): add_text(c)
                    elif isinstance(c, list):
                        for sub in c:
                            if isinstance(sub, dict) and sub.get('type')=='text':
                                add_text(sub.get('text',''))
        if ts: last_ts = ts
elapsed_min = 0
start_str = "?"
if first_ts:
    try:
        from zoneinfo import ZoneInfo
        pdt = ZoneInfo('America/Los_Angeles')
        f_dt = datetime.strptime(first_ts.replace('Z','+0000'), '%Y-%m-%dT%H:%M:%S.%f%z')
        start_str = f_dt.astimezone(pdt).strftime('%H:%M %Z')
        if last_ts:
            l_dt = datetime.strptime(last_ts.replace('Z','+0000'), '%Y-%m-%dT%H:%M:%S.%f%z')
            elapsed_min = int((l_dt - f_dt).total_seconds() / 60)
    except: pass
token_est = token_chars // 4
def bar(val, lim): 
    pct = min(100, int(100*val/lim)) if lim else 0
    return f"{val:>7,} / {lim:>6,}  ({pct}%)"
crossed = sum([token_est>TOKEN_LIMIT, tool_count>TOOL_LIMIT, elapsed_min>ELAPSED_LIMIT_MIN])
hrs = elapsed_min/60
print(f"Session start:   {start_str}")
print(f"Tokens (est):    {bar(token_est, TOKEN_LIMIT)}")
print(f"Tool calls:      {bar(tool_count, TOOL_LIMIT)}")
print(f"Elapsed:         {hrs:>6.1f}h / {ELAPSED_LIMIT_MIN/60:>4.0f}h    ({int(min(100, 100*elapsed_min/ELAPSED_LIMIT_MIN))}%)")

# Brain-recall hook telemetry — per spec-brain-recall-hook-tightening-2026-05-16 Phase 3
if brain_recall_state_dir:
    session_id = Path(path).stem
    br_path = Path(brain_recall_state_dir) / f".brain-recall-{session_id}.json"
    if br_path.exists():
        try:
            br = json.loads(br_path.read_text())
            fires = br.get('fires', [])
            total = len(fires)
            suppressed = sum(1 for f in fires if f.get('suppressed'))
            if total > 0:
                sup_pct = int(100*suppressed/total)
                # Top 3 most-fired slugs (person + project combined)
                slug_counter = Counter()
                for f in fires:
                    for s in f.get('person_hits', []): slug_counter[s] += 1
                    for s in f.get('project_hits', []): slug_counter[s] += 1
                top = ', '.join(s for s, _ in slug_counter.most_common(3)) or '(none)'
                print(f"Brain-recall:    {total} fires, {suppressed} suppressed ({sup_pct}%) | top hits: {top}")
            else:
                print(f"Brain-recall:    (no fires this session)")
        except Exception:
            print(f"Brain-recall:    (telemetry unreadable)")
    else:
        print(f"Brain-recall:    (no fires this session)")

print(f"Thresholds crossed: {crossed}/3")
if crossed >= 2:
    print("RECOMMEND: /clear (preserves session for /resume) or close + reopen for next thread.")
elif crossed == 1:
    print("RECOMMEND: continue, but watch — one threshold crossed.")
else:
    print("RECOMMEND: continue. Session is fresh.")
PY
```

## Output format

Just paste the python output verbatim inside a fenced code block. No commentary unless the user asked a follow-up question. Don't editorialize the recommendation — let the numbers speak.
