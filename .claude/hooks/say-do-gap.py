#!/usr/bin/env python3
"""Stop hook — detects "say/do gap" in the just-finished assistant message.

Scans the last assistant message in the session JSONL for future-tense action
language ("I'll save", "saving X now", "let me log") OR past-tense save claims
("saved to X"). If found AND no satisfying tool call (Write/Edit/MultiEdit/
NotebookEdit/TaskCreate/TaskUpdate) ran in the same message, blocks the stop
with a reason that forces Claude to either complete the action or rewrite
the claim.

Implements CLAUDE.md anti-pattern rule #3 structurally.

Reads JSON via stdin (Stop hook input). Honors `stop_hook_active` to avoid
infinite loops — if a prior Stop hook already blocked, exit silently.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
try:
    import hooklog  # durable telemetry → .claude/state/hook-events.log
except Exception:
    hooklog = None

# CORE_PROJECTS_DIR exists so the test suite can point this hook at a fixture transcript
# directory and run THE REAL HOOK. Without it the only way to test was to inline a copy of
# this file into the test — which is what test-say-do-gap.sh did, and the copy had already
# drifted from the original (it still logged to the abandoned global /tmp path). A test that
# validates a duplicate cannot fail when the original breaks. (core-business, 2026-07-28.)
#
# Reading a different transcript directory is the whole effect; it grants no capability and
# changes no verdict logic. Unset in every non-test invocation.
PROJECTS_DIR = os.environ.get("CORE_PROJECTS_DIR") or os.path.expanduser(
    "~/.claude/projects/-" + os.getcwd().lstrip("/").replace("/", "-").replace(" ", "-"))
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
_SESSION = ""  # set from stdin payload in main(); used by log_block telemetry


def log_block(pattern: str, excerpt: str) -> None:
    """Emit one durable telemetry line via the shared hooklog lib (fail-open)."""
    if hooklog is None:
        return
    hooklog.log("say-do-gap", "Stop", verdict="block",
                trigger=(excerpt or pattern or ""), session=_SESSION)

# Verbs that imply a state-changing action that should accompany a tool call
FUTURE_TENSE = re.compile(
    r"\b(?:i['']?ll|let me|i (?:will|am going to|gonna))\s+"
    r"(?:save|log|write|update|add|edit|create|append|flag|note|record|capture|store|persist|track|append|append it|drop in|put in|put it in|stick in)\b",
    re.IGNORECASE,
)
# 2026-06-23 (A2 fix): bare 'this'/'that' objects fired on innocent prose
# ("Adding this constraint...", "Capturing this pattern..."). Now a demonstrative
# object must be followed by a destination preposition (to/in/into), OR the object
# is a direct destination ("to memory", "into the file"). Preserves "saving this
# to memory" / "logging it into the log"; drops "adding this constraint".
PRESENT_PROGRESSIVE = re.compile(
    r"\b(?:saving|logging|writing|updating|adding|editing|creating|appending|flagging|noting|recording|capturing|storing|persisting|tracking)\s+"
    r"(?:(?:this|that|it)\s+(?:to|in|into)\b|to\s+(?:memory|the|lessons|project|current|canvas|disk|file|tasks)|into?\s+the\b|down\s+now\b)",
    re.IGNORECASE,
)
# Past-tense save claim: "I saved X to Y" or "we just logged X" or "now added X to Y".
# Tightened 2026-05-22 (audit response): require first-person or recency marker
# ("i/we/just/already/now") as the subject. Original bare past-tense was 88% FP per
# 2026-05-21 hook audit — fired on every "persisted at" / "appended to" / "logged in"
# fragment in retrospective prose, even when the claim was about prior turns or
# was a relative clause embedded in narrative. First-person/recency anchor narrows
# to direct self-claims about THIS turn.
# 2026-06-23 (A2 fix): object now requires a destination preposition (optionally
# after a this/that/it pronoun), so "I noted that the approach was clean" and
# "we already added that feature" no longer fire, while "already logged it to the
# file" / "saved to memory" still do.
PAST_TENSE = re.compile(
    r"\b(?:(?:i|we)(?:['’]ve)?|just|already|now)\s+"  # 2026-06-23: handle contractions (I've/we've saved)
    r"(?:saved|logged|wrote|written|updated|added|edited|appended|flagged|noted|recorded|captured|stored|persisted|tracked)\s+"
    # Allow a short object phrase between the verb and the destination. Requiring the
    # preposition to follow the verb IMMEDIATELY meant the gate missed the most natural way
    # to make the claim — "I saved THE DECISION to memory", "we just logged THAT OUTCOME in
    # the session file" — while still catching the terser "I saved to memory". The
    # destination-noun requirement after the preposition is what kills the false positives
    # (T05b-d), so it is kept; only the gap before it is widened, lazily and to at most three
    # words so the match cannot run across a sentence.
    #
    # Found 2026-07-28 when the test stopped exercising an inlined COPY of this hook and
    # started running the real one: 2 of 20 cases failed immediately. The copy was broader
    # here, so the suite had been green about behaviour the shipped gate did not have.
    r"(?:\S+\s+){0,3}?(?:to|in|at|under|into)\s+(?:the|this|that|it|memory|lessons|current|sessions|tasks|canvas|project|disk|file)\b",
    re.IGNORECASE,
)

SATISFYING_TOOLS = {
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "TaskCreate", "TaskUpdate",
}


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


def detect(text):
    """Action-claim language in `text` (future / present-progressive / past)."""
    ct = _claimtext()
    spans = ct.quoted_spans(text) if ct else None
    hits = []
    for label, rx in (("future", FUTURE_TENSE), ("present", PRESENT_PROGRESSIVE), ("past", PAST_TENSE)):
        for m in rx.finditer(text or ""):
            if ct and ct.is_mention(text, m.start(), m.end(), spans):
                continue
            hits.append(f"[{label}] {m.group(0)}")
    return hits


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
        import hooklog as _hl; _hl.invoked("say-do-gap", "Stop")
    except Exception:
        pass
    # Read Stop hook input
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    global _SESSION
    _SESSION = data.get("session_id", "") or ""

    # Honor stop_hook_active to avoid infinite loop — if a previous Stop
    # already blocked, just let this one through.
    if data.get("stop_hook_active"):
        return 0

    path = latest_jsonl()
    if not path:
        return 0

    # Accumulate text + tool calls across the WHOLE turn (every assistant message
    # since the last real user prompt) — NOT just the final assistant record.
    # 2026-06-04 fix: Opus 4.8 splits a turn into separate assistant messages
    # (announcement text in one, tool_use in the next), so the final record is
    # usually tool-use-only with empty text. The old "scan only the last message"
    # logic then saw last_text="" and silently passed every say/do violation.
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

    hits = detect(last_text)

    if not hits:
        return 0

    # Did the message satisfy with at least one Write/Edit/etc.?
    if set(last_tools) & SATISFYING_TOOLS:
        return 0

    # Gap detected — log and block
    log_block(hits[0] if hits else "unknown", hits[0] if hits else "")
    hits_str = "; ".join(h for h in hits[:3])
    reason = (
        f"SAY/DO GAP — your last response contains action-claim language ({hits_str}) "
        "but no Write/Edit/MultiEdit/NotebookEdit/TaskCreate/TaskUpdate tool call ran this turn. "
        "Anti-pattern rule #3: complete the action with a tool call NOW, OR rewrite the claim "
        "as proposed (\"I would do X — confirm?\") rather than as already-doing. "
        "Re-respond: either run the tool call(s) you implied, or remove/reframe the claim. "
        "Past-tense save claims (\"saved X to Y\") need a Write/Edit this turn that proves it."
    )
    out = {
        "decision": "block",
        "reason": reason,
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
