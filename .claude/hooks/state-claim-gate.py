#!/usr/bin/env python3
"""Stop hook — detects "state-claim from memory" in the just-finished
assistant message. Mirror of say-do-gap.py, but for state-assertions
instead of action-claims.

Implements CLAUDE.md anti-pattern rule #1 structurally: any sentence that
asserts system state ("X is done", "Y didn't fire", "Z is broken", "the
latest is W") must be backed by a tool call result IN THE SAME RESPONSE.
If state-claim language is detected and no read-shaped tool call ran this
turn, blocks the stop and forces re-response.

Read-shaped tools that satisfy a state-claim:
  Read, Bash, Grep, Glob, WebFetch, WebSearch, NotebookRead,
  ListMcpResourcesTool, ReadMcpResourceTool,
  any mcp__*__get_*, mcp__*__list_*, mcp__*__search_*, mcp__*__read_*

Honors stop_hook_active to avoid infinite loops.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
try:
    import hooklog  # durable telemetry → .claude/state/hook-events.log
except Exception:
    hooklog = None

# CORE_PROJECTS_DIR exists so the test suite can point this hook at a fixture transcript
# directory and run THE REAL HOOK. Without it the only way to test was to inline a copy of
# this file into the test — which is what test-state-claim-gate.sh did, and the copy had already
# drifted from the original (it still logged to the abandoned global /tmp path). A test that
# validates a duplicate cannot fail when the original breaks. (core-business, 2026-07-28.)
#
# Reading a different transcript directory is the whole effect; it grants no capability and
# changes no verdict logic. Unset in every non-test invocation.
PROJECTS_DIR = os.environ.get("CORE_PROJECTS_DIR") or os.path.expanduser(
    "~/.claude/projects/-" + os.getcwd().lstrip("/").replace("/", "-").replace(" ", "-"))
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
_SESSION = ""  # set from stdin payload in main(); used by log_block telemetry

# Project-specific keyword list (graphify, brain, lint-pass, close-reconciler, etc.)
# lives in .claude/identity.json so a fresh template clone parameterizes cleanly.
# Falls back to "" (no extra keywords) if the file is missing/malformed — the hook
# still works on the generic system terms below, just less aggressively for project terms.
_IDENTITY_FILE = Path(__file__).resolve().parents[1] / "identity.json"


def _load_extra_keywords():
    try:
        with open(_IDENTITY_FILE) as f:
            data = json.load(f)
        return data.get("state_claim_gate", {}).get("extra_keywords_regex", "") or ""
    except Exception:
        return ""


_EXTRA_KEYWORDS = _load_extra_keywords()


def log_block(pattern: str, excerpt: str) -> None:
    """Emit one durable telemetry line via the shared hooklog lib (fail-open)."""
    if hooklog is None:
        return
    hooklog.log("state-claim-gate", "Stop", verdict="block",
                trigger=(excerpt or pattern or ""), session=_SESSION)

# State-assertion patterns. Word-boundary anchored. Each requires the
# referent to be a system-state keyword to avoid false positives on
# normal English ("the report is detailed", "this approach is clean").
#
# Generic system terms (hook, file, session, memory, etc.) live here.
# Project-specific terms (graphify, brain, lint-pass, close-reconciler,
# weekly-review, job-hunter, core-ui, untrusted-reader) are appended
# from .claude/identity.json so a fresh template clone parameterizes cleanly.
_GENERIC_KEYWORDS = (
    r"hook|hooks|gate|guard|sentinel|"
    r"current-state(?:\.md)?|state\.md|"
    r"lessons(?:\.md)?|"
    r"claude\.md|claude.md|"
    r"session|sessions|session\s+log|"
    r"memory|memory\s+file|"
    r"file|directory|folder|path|"
    r"hook\s+\w+|.*\.sh|.*\.py|.*\.md|.*\.json|"
    r"calendar|reminder|reminders|"
    r"plist|launchd|launchagent|launch\s+agent|"
    r"cron|schedule|"
    r"mcp|mcp\s+server|"
    r"skill|skills|"
    r"agent|agents|subagent|subagents|"
    r"command|commands|slash[-\s]command|"
    r"settings\.json|settings\.local\.json|"
    r"phase[-\s]?\d+|batch[-\s]?\d+|"
    r"tracker|"
    r"plan|"
    r"hook\s+output|"
    r"the\s+latest|"
    r"X|Y|Z"  # placeholder vars in synthetic claims
)

if _EXTRA_KEYWORDS:
    STATE_KEYWORDS = r"(?:the\s+)?(?:" + _GENERIC_KEYWORDS + r"|" + _EXTRA_KEYWORDS + r")"
else:
    STATE_KEYWORDS = r"(?:the\s+)?(?:" + _GENERIC_KEYWORDS + r")"

# State verbs that imply an assertion about that referent's current state.
STATE_VERBS = (
    r"(?:is|are|was|were|isn['']?t|aren['']?t|wasn['']?t|weren['']?t|"
    r"has|have|had|hasn['']?t|haven['']?t|hadn['']?t|"
    r"does|do|did|doesn['']?t|don['']?t|didn['']?t|"
    r"exists?|exist|existed|"
    r"contains?|contains|contain|contained|"
    r"shows?|show|showed|"
    r"says?|say|said|"
    r"reads?|read|reads)"
)

# Adjectives/states that classify the referent.
STATE_ADJECTIVES = (
    r"(?:done|complete|completed|finished|"
    r"broken|failing|failed|"
    r"running|live|active|started|"
    r"empty|missing|absent|gone|deleted|"
    r"present|there|"
    r"installed|wired|configured|set\s*up|enabled|disabled|"
    r"merged|deployed|shipped|"
    r"fixed|resolved|patched|"
    r"stale|fresh|current|outdated|"
    r"pending|queued|waiting|blocked|"
    r"fired|skipped|tested|verified|extracted|processed|"
    r"loaded|unloaded|"
    r"clean|dirty|"
    r"valid|invalid|"
    r"correct|wrong|"
    r"open|closed)"
)

PATTERNS = [
    # "X is done", "the hook is broken", "graphify is running"
    # Allow up to 4 noun-phrase words between keyword and verb (handles
    # "lint v3 dry-run is complete", "subagent Write denials are resolved").
    # Allow modal/aspect words between verb and adjective.
    re.compile(
        rf"\b{STATE_KEYWORDS}(?:\s+(?:[\w\-]+|v\d+)){{0,4}}\s+{STATE_VERBS}\s+(?:not\s+|already\s+|still\s+|now\s+|just\s+|all\s+|fully\s+|partially\s+|currently\s+)?{STATE_ADJECTIVES}\b",
        re.IGNORECASE,
    ),
    # "there is no X", "there's no Y", "there are no Z"
    # Tightened 2026-05-22 (audit response): require X to be a STATE_KEYWORD,
    # otherwise "There's no extra" / "there's no time" / "there's no point"
    # are all benign English that match the original \w+ catchall (FP-class
    # per 2026-05-21 audit).
    re.compile(
        rf"\bthere(?:['']?s|\s+is|\s+are|\s+was|\s+were)?\s+(?:no|not\s+(?:any|a))\s+(?:[\w\-]+\s+){{0,2}}{STATE_KEYWORDS}\b",
        re.IGNORECASE,
    ),
    # "X exists", "no Y exists" — bare existential
    # Tightened 2026-05-22 (audit response): require STATE_KEYWORDS as subject
    # (with up to 3 noun-phrase prefix words). Original \w+ catchall fired on
    # "check exists", "that exist", "plan exist", "those exist" — fragments
    # in narrative prose, not standalone existence claims. Audit FNs like "no
    # /goal slash command exists" still match because "command" IS in
    # STATE_KEYWORDS, so the recall side is preserved.
    re.compile(
        rf"\b(?:no\s+)?(?:[\w\-/]+\s+){{0,3}}{STATE_KEYWORDS}\s+(?:exists?|does\s+not\s+exist|doesn['']?t\s+exist)\b",
        re.IGNORECASE,
    ),
    # "X has fired", "Y didn't fire", "Z fired"
    re.compile(
        r"\b\w+\s+(?:has|had|hasn['']?t|hadn['']?t|did|didn['']?t|already)\s+fired\b",
        re.IGNORECASE,
    ),
    # NOTE: removed pattern "the latest X is Y" / "the most recent Y is Z" — it
    # was firing on "the current listing is at price $5.00" / "the latest report
    # is detailed" (no state adjective). Pattern 1 already catches real claims
    # in this shape because it requires STATE_KEYWORDS + STATE_VERBS +
    # STATE_ADJECTIVES (so "the current listing is broken" still matches there).
    # Removed 2026-05-22 per audit FP class.
    # confident negations: "X is not in <file>", "Y wasn't found"
    re.compile(
        r"\b\w+\s+(?:is|are|was|were)\s+not\s+(?:in|on|at|under|inside)\s+\S",
        re.IGNORECASE,
    ),
]

# Tool names that count as a verification read for a state-claim.
SATISFYING_TOOLS_PREFIXES = (
    "Read", "Bash", "Grep", "Glob", "WebFetch", "WebSearch",
    "NotebookRead", "ListMcpResourcesTool", "ReadMcpResourceTool",
)


def is_read_tool(name):
    if not name:
        return False
    if name in SATISFYING_TOOLS_PREFIXES:
        return True
    # mcp__server__verb_*  — accept get_/list_/search_/read_/fetch_
    if name.startswith("mcp__"):
        # Last underscore-segment is the verb
        m = re.search(r"__(\w+?)(?:_|$)", name.split("__", 2)[-1] if "__" in name[5:] else name[5:])
        # Simpler: check for known read verbs anywhere after the second __
        rest = name[5:]
        if "__" in rest:
            verb_part = rest.split("__", 1)[1]
        else:
            verb_part = rest
        # 2026-06-23 (A1 fix): also match a read verb in ANY underscore-segment,
        # so peer-Core tools (mcp__peer-business__peer_read / peer_list_active_projects)
        # register as reads instead of false-negatives.
        read_verbs = ("get", "list", "search", "read", "fetch", "find", "show", "describe", "view", "recall")
        parts = verb_part.split("_")
        for v in read_verbs:
            if verb_part.startswith(v) or any(p == v for p in parts):
                return True
    return False


def latest_jsonl():
    try:
        files = [
            os.path.join(PROJECTS_DIR, f)
            for f in os.listdir(PROJECTS_DIR)
            if f.endswith(".jsonl")
        ]
    except FileNotFoundError:
        return None
    if not files:
        return None
    return max(files, key=os.path.getmtime)



def _claimtext():
    """Shared mention discriminators (lib/claimtext.py). A phrase inside quotes is being DISCUSSED
    and a phrase after a negation asserts the OPPOSITE — both are mentions, not claims. Two gates hit
    this defect on the same reply on 2026-07-27, so it lives in one place. Fail-open: without the lib
    the gate still works, just less precisely."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import claimtext
        return claimtext
    except Exception:
        return None


# Claims of the form "X isn't there" / "no Y exists" are ABSENCE claims, and per memory.md one read
# cannot prove a negative across documents — main() demands a wider grep for these. detect() carries
# the classification so main() does not need its own copy of the matching loop.
ABSENCE_IDX = {1, 2, 4}
NEG_RX = re.compile(
    r"\b(?:isn['']?t|aren['']?t|wasn['']?t|doesn['']?t|don['']?t|didn['']?t|hasn['']?t|haven['']?t"
    r"|not|no|never|missing|absent|gone)\b",
    re.IGNORECASE,
)


def detect(text, with_absence=False):
    """Assertions of system state in `text`. Returns [] when none are genuinely asserted.

    Extracted 2026-07-27 so bin/grade-gate.py can measure this gate against the real transcript
    corpus — it fires on every Stop and its true rate was unknown. main() calls THIS rather than
    keeping a parallel loop: a second copy would let the live gate drift from the measured one while
    the grader kept reporting reassuring numbers about code that no longer runs (Codex review, same
    day). with_absence=True returns (hits, absence_hit) for main()'s wider-grep demand.
    """
    ct = _claimtext()
    text = text or ""
    spans = ct.quoted_spans(text) if ct else None
    hits, absence = [], False
    for i, rx in enumerate(PATTERNS):
        for m in rx.finditer(text):
            if ct and ct.is_mention(text, m.start(), m.end(), spans):
                continue
            hits.append(m.group(0))
            if i in ABSENCE_IDX or NEG_RX.search(m.group(0)):
                absence = True
            if len(hits) >= 3:
                return (hits, absence) if with_absence else hits
    return (hits, absence) if with_absence else hits


def main():
    # Record that this hook RAN, matched or not. Without it the ledger can count FIRES but not
    # INVOCATIONS, so yield (fires/invocations) is not computable — and a gate that fires 4 times
    # out of 4 looks identical to one that fires 4 times out of 400. Added 2026-07-30 across every
    # instrumented hook that lacked it, so low-yield becomes a measurable verdict rather than a
    # guess. (The ledger could retire EXPENSIVE components but not cheap-and-useless ones; this is
    # the missing term.)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("state-claim-gate", "Stop")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    global _SESSION
    _SESSION = data.get("session_id", "") or ""

    if data.get("stop_hook_active"):
        return 0

    path = latest_jsonl()
    if not path:
        return 0

    # Accumulate text + tool calls across the WHOLE turn (every assistant message
    # since the last real user prompt) — NOT just the final assistant record.
    # 2026-06-04 fix: Opus 4.8 splits a turn into separate assistant messages
    # (state-claim text in one, the verifying read tool in the next), so the final
    # record is usually tool-use-only with empty text. The old "scan only the last
    # message" logic then saw last_text="" and silently passed every state claim.
    # Pre-4.8 the full response was one message; this reconstructs that view.
    # Mirrors the turn-accumulation already in time-claim-gate.py (2026-05-24).
    turn_text_parts = []
    turn_tools = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                dtype = d.get("type")
                msg = d.get("message") or {}
                content = msg.get("content")
                if dtype == "user":
                    # A real user prompt starts a new turn → reset accumulators.
                    # A tool_result-only user message is part of the SAME turn.
                    is_tool_result_only = (
                        isinstance(content, list)
                        and len(content) > 0
                        and all(
                            isinstance(p, dict) and p.get("type") == "tool_result"
                            for p in content
                        )
                    )
                    if not is_tool_result_only:
                        turn_text_parts = []
                        turn_tools = []
                    continue
                if dtype != "assistant":
                    continue
                if not isinstance(content, list):
                    continue
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get("type")
                    if pt == "text":
                        t = p.get("text", "") or ""
                        if t:
                            turn_text_parts.append(t)
                    elif pt == "tool_use":
                        nm = p.get("name") or ""
                        if nm:
                            turn_tools.append(nm)
    except FileNotFoundError:
        return 0

    last_text = " ".join(turn_text_parts)
    last_tools = turn_tools

    if not last_text:
        return 0

    # Skip if response is short and chatty (greetings, acks, simple yes/no).
    if len(last_text.strip()) < 80:
        return 0

    # ONE matching path, shared with the grader. Pattern indices 1 (there-is-no), 2 (exists/
    # doesn't-exist), 4 (is-not-in) are ABSENCE-class claims — per memory.md, a negative across
    # documents cannot be proven by a single read, so those demand a wider grep below.
    hits, absence_hit = detect(last_text, with_absence=True)

    if not hits:
        return 0

    read_count = sum(1 for t in last_tools if is_read_tool(t))

    # Fix 2 (contract-binding proposal, live 2026-06-09 Nick-approved no-shadow):
    # absence-class claims require >=2 read-shaped calls this turn (multi-file
    # verification) — one read of one doc cannot prove "X doesn't exist / X is
    # not in Y" when truth spans more than that doc.
    if absence_hit and read_count < 2:
        log_block("absence-needs-multi-read", hits[0] if hits else "")
        reason = (
            f"STATE-CLAIM GATE (absence-class) — your last response asserts an absence/negative "
            f"(\"{hits[0][:70]}\") backed by {read_count} read(s) this turn. Absence and cross-doc "
            "claims need MULTI-file verification (memory.md rule): one read of one doc cannot prove "
            "a negative. Append a verification footnote: either run a second read/grep over the other "
            "plausibly-authoritative location(s) and cite both, or downgrade the claim to uncertain "
            "(\"not in <file-checked>; haven't checked elsewhere\"). Keep the prior reply intact."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
        return 0

    # Did the response include at least one read-shaped tool call?
    if read_count >= 1:
        return 0

    # Gap detected — log and block.
    log_block(hits[0][:60] if hits else "unknown", hits[0] if hits else "")
    hits_str = "; ".join(f"\"{h}\"" for h in hits[:3])
    reason = (
        f"STATE-CLAIM GATE — your last response asserts system state ({hits_str}) "
        "but no read-shaped tool call (Read / Bash / Grep / Glob / WebFetch / MCP-read) ran this turn. "
        "Anti-pattern rule #1: claim-from-memory is forbidden. "
        "Append a brief verification footnote — do NOT redo the whole answer. "
        "Either (a) run the read that proves the state-claim and add a one-line citation "
        "(\"verified — `<file>:<line>` confirms\"), or "
        "(b) flag the claim as uncertain (\"on second look I'm not sure X is Y — want me to verify?\"). "
        "Keep the prior reply intact; just add the citation/correction as a brief follow-up."
    )
    out = {"decision": "block", "reason": reason}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
