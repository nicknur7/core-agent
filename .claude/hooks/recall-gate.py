#!/usr/bin/env python3
"""recall-gate — Stop hook (B1 part b, 2026-06-08). THE structural "go look at the brain."

If the just-finished response asserts what WE decided / discussed / built / agreed, or what NICK
decided/declined/told me — i.e. a claim about prior context — it must be backed by a BRAIN query
THIS turn. A local file read (Read/Grep/Bash) does NOT satisfy it: local summaries (current-state.md,
diagnostics, session logs) are exactly the thing that DRIFTS from what was actually said — the 6/04
"Nick declined the hook" claim came from Core's own write-up, not the brain (see memory
feedback_core_summaries_drift_from_what_nick_said). Only the brain holds the real exchange.

Satisfied by any: mcp__core-brain__* (recall_similar / recall_at / get_entity / get_topic /
get_evidence / list_recent_sessions), any tool whose name carries "recall" or "graphify".

Goal (the operator, 2026-06-08): always have the context needed for anything discussed or
previously discussed — that is what the brain provides. This makes that structural, not dependent
on me remembering.

REGISTERED 2026-06-09 in settings.json Stop hooks (Nick-approved, live). The workhorse recall
enforcer — 17 real blocks across 4 sessions as of 2026-06-22 (catches read-local-but-not-brain
decision/history claims). Distinct from learned-recallguard (which catches the no-tool case).
Run self-test:  python3 recall-gate.py --selftest
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # operator name from identity.json, never hardcoded

import json
import os
import re
import sys

PROJECTS_DIR = os.path.expanduser("~/.claude/projects/-" + os.getcwd().lstrip("/").replace("/", "-").replace(" ", "-"))

# High-precision claims about PRIOR context / decision attribution. These are the claims where
# answering from memory/local-summary instead of the brain has burned us. Present-tense plans
# ("let's do X", "I'll build Y") and pure system-state ("the hook is broken" -> state-claim-gate)
# are deliberately NOT here.
HISTORY_CLAIM_PATTERNS = [
    r"\bwe\s+(?:decided|discussed|talked\s+about|agreed|covered|established|concluded|chose|determined)\b",
    r"\bwe\s+(?:already|never|previously)\s+(?:decided|discussed|did|built|addressed|talked|covered|agreed)\b",
    # "asked for" removed 2026-07-27 (measured): it is what Nick does in the CURRENT turn, not a
    # claim about a past decision. It produced 3 of 4 false blocks in the labelled set — including
    # blocking a reply whose only offence was the phrase "you asked for a cleanup". A genuinely-past
    # "you asked for X last week" is still caught by the temporal pattern below, which requires an
    # explicit past marker. The verbs kept here are all real attribution verbs.
    r"\byou\s+(?:decided|declined|approved|rejected|told\s+me|wanted|said\s+to|agreed\s+to)\b",
    # Operator's name, read from identity.json (never hardcoded) — a literal "nick" here meant
    # this pattern silently never fired for any operator whose name isn't Nick.
    rf"\b{re.escape(_U.name())}\s+(?:decided|declined|approved|rejected|wanted|asked\s+for|said)\b",
    # A temporal marker followed by a bare pronoun matches NARRATION as readily as a claim —
    # "earlier, I wrote that assertion myself" is not an assertion about a past decision. Requiring
    # an actual claim verb keeps "earlier we decided" while dropping "earlier I wrote" (measured
    # 2026-07-27: this pattern produced the remaining false block).
    r"\b(?:last\s+time|earlier|previously|in\s+a\s+prior\s+session|that\s+session|back\s+(?:then|on))\b"
    r".{0,40}\b(?:we|you|i)\s+(?:decided|discussed|agreed|approved|declined|rejected|chose|"
    r"concluded|established|covered|talked|said|told)\b",
    r"\bthe\s+(?:decision|plan|approach|call)\s+(?:we|was|you)\b",
    r"\bwe'?ve\s+(?:already\s+)?(?:discussed|decided|talked\s+about|covered|done|addressed|built)\b",
    r"\bhave\s+we\s+(?:discussed|decided|talked|covered|addressed|done|built)\b",
    r"\b(?:was|were)\s+(?:declined|decided|approved|rejected|agreed)\b",
    r"\baccording\s+to\s+(?:our|the\s+prior|the\s+earlier)\b",
]

# 2026-07-27 (governance directive, tune): claims about the CURRENT session are
# structurally NOT in the brain — the close is synchronous, so this session's own
# work only lands at close. Demanding a brain query for "tonight we shipped X" was
# the gate's #1 noise class (it fired on an illustrative quote inside a message
# about gate over-firing). A history-claim match whose ±120-char window carries a
# current-session marker is suppressed (and logged), not blocked. Genuinely-past
# claims — the drift class this gate exists for — are untouched.
CURRENT_SESSION_RX = re.compile(
    r"\b(?:this\s+(?:session|conversation|turn|run)|in\s+this\s+(?:turn|session|reply)|"
    r"above|a\s+moment\s+ago|tonight|today|just\s+(?:now|did|"
    r"shipped|built|ran|fixed|pushed|landed)|we\s+just|earlier\s+(?:tonight|today|"
    r"this\s+session)|moments?\s+ago|mid-session)\b",
    re.I,
)

# Codex review 2026-07-27 (#2): a current-session marker only suppresses when the
# claim's own SENTENCE carries it AND that sentence has no explicit past reference —
# "Nick approved X last week. Tonight I ran tests." must still enforce the first claim.
PAST_OVERRIDE_RX = re.compile(
    r"\b(?:last\s+(?:week|session|time|month|night)|previous(?:ly)?|prior\s+session|"
    r"yesterday|back\s+(?:then|on)|on\s+\d{1,2}/\d{1,2})\b",
    re.I,
)
_SENT_DELIMS = ".!?\n"

GATE_FIRES_LOG = os.path.join(
    os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    or str(__import__("pathlib").Path(__file__).resolve().parents[2]),  # Codex #4: never "."
    ".claude", "state", "gate-fires.log")


def gate_log(action: str, detail: str, snippet: str = ""):
    """Uniform steering-surface telemetry (governance directive 2026-07-27).
    One TSV line per event: ts, hook, action(block|suppress), detail, snippet.
    Size-capped (Codex #5): >512KB → keep the newest 1000 lines."""
    try:
        import datetime as _dt
        os.makedirs(os.path.dirname(GATE_FIRES_LOG), exist_ok=True)
        try:
            if os.path.exists(GATE_FIRES_LOG) and os.path.getsize(GATE_FIRES_LOG) > 512 * 1024:
                with open(GATE_FIRES_LOG) as f:
                    tail = f.readlines()[-1000:]
                with open(GATE_FIRES_LOG, "w") as f:
                    f.writelines(tail)
        except Exception:
            pass
        with open(GATE_FIRES_LOG, "a") as f:
            f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')}\trecall-gate\t"
                    f"{action}\t{detail}\t{snippet[:80]!r}\n")
    except Exception:
        pass  # telemetry must never break the gate


def _sentence_span(text: str, start: int, end: int):
    """Bounds of the sentence containing [start,end) — delimiter-to-delimiter."""
    lo = max((text.rfind(d, 0, start) for d in _SENT_DELIMS), default=-1)
    lo = max(lo + 1, 0)
    his = [i for d in _SENT_DELIMS if (i := text.find(d, end)) != -1]
    hi = min(his) + 1 if his else len(text)
    return lo, hi


def is_brain_tool(name: str) -> bool:
    if not name:
        return False
    n = name.lower()
    if n.startswith("mcp__core-brain__"):
        return True
    if "recall" in n or "graphify" in n:
        return True
    return False


def latest_jsonl():
    try:
        files = [os.path.join(PROJECTS_DIR, f) for f in os.listdir(PROJECTS_DIR) if f.endswith(".jsonl")]
    except FileNotFoundError:
        return None
    return max(files, key=os.path.getmtime) if files else None


def accumulate_turn(path):
    """Return (text, tool_names) accumulated across the whole turn — same approach as
    state-claim-gate (Opus 4.8 splits text and tool calls into separate assistant messages)."""
    text_parts, tools = [], []
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if d.get("type") == "user":
                    is_tool_result_only = (
                        isinstance(content, list) and len(content) > 0
                        and all(isinstance(p, dict) and p.get("type") == "tool_result" for p in content)
                    )
                    if not is_tool_result_only:
                        text_parts, tools = [], []
                    continue
                if d.get("type") != "assistant" or not isinstance(content, list):
                    continue
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text" and p.get("text"):
                        text_parts.append(p["text"])
                    elif p.get("type") == "tool_use" and p.get("name"):
                        tools.append(p["name"])
    except FileNotFoundError:
        return "", []
    return " ".join(text_parts), tools









# Discriminators are SHARED (lib/claimtext.py). recall-gate and decision-attribution-gate hit the
# identical quoted-mention defect on the same reply, minutes apart; fixing it twice would have been
# the "another patch beside the old one" pattern Nick has corrected 9 times. One implementation,
# imported by both.
try:
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
    from claimtext import is_mention as _is_mention, quoted_spans as _quoted_spans
except Exception:            # fail-open: without the lib the gate still works, just less precise
    _is_mention = None
    def _quoted_spans(_t):
        return []


def detect(text):
    """Return (hits, suppressed): history-claim matches split into enforceable
    hits vs current-session-context matches (suppressed, logged, never blocked).
    Codex review 2026-07-27 (#1): EVERY occurrence of every pattern is classified
    independently (finditer) — a suppressed current-session match must not shadow
    a later genuine past claim on the same pattern."""
    hits, suppressed = [], []
    qspans = _quoted_spans(text)
    for rx in HISTORY_CLAIM_PATTERNS:
        for m in re.finditer(rx, text, re.I):
            if _is_mention and _is_mention(text, m.start(), m.end(), qspans):
                suppressed.append(m.group(0))   # quoted or negated — a mention, not a claim
                continue
            lo, hi = _sentence_span(text, m.start(), m.end())
            sent = text[lo:hi]
            if CURRENT_SESSION_RX.search(sent) and not PAST_OVERRIDE_RX.search(sent):
                suppressed.append(m.group(0))
            else:
                hits.append(m.group(0))
            if len(hits) >= 3:
                return hits, suppressed
    return hits, suppressed


def main():
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("recall-gate", "Stop")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("stop_hook_active"):
        return 0
    path = latest_jsonl()
    if not path:
        return 0
    text, tools = accumulate_turn(path)
    if not text or len(text.strip()) < 80:
        return 0
    hits, suppressed = detect(text)
    if suppressed:
        gate_log("suppress", "current-session-context", "; ".join(suppressed[:3]))
    if not hits:
        return 0
    if any(is_brain_tool(t) for t in tools):
        return 0  # grounded in the brain this turn — good
    gate_log("block", "history-claim-no-brain-query", "; ".join(hits[:3]))
    hits_str = "; ".join(f'"{h}"' for h in hits[:3])
    reason = (
        f"RECALL GATE — your response asserts prior context ({hits_str}) but no brain query ran this turn. "
        f"Claims about what we decided / discussed / built — or what {_U.name()} decided/declined — must come from the "
        f"BRAIN, not memory or a local summary (those drift; the 6/04 '{_U.name()} declined' claim was Core's own write-up). "
        "Run a brain query now (mcp__core-brain__recall_similar / recall_at / get_entity) and add a one-line "
        "citation, OR soften the claim to 'I should check the brain before stating that.' Keep the rest of the reply."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def _selftest():
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name); ok = ok and cond
    # claims that SHOULD trip the gate
    for c in ["we decided to go with Haiku last session",
              "you declined that hook on 6/04",
              "we already discussed the org partitions",
              "have we talked about this before",
              f"{_U.name()} approved the baseline push earlier"]:
        check(f"detects: {c[:40]}", bool(detect(c)[0]))
    # 2026-07-27 precision lock, from MEASURED false blocks in .claude/state/gate-fires.log.
    # The gate blocked two replies whose only offence was describing the CURRENT turn — "you asked
    # for a cleanup" and "earlier, I wrote that assertion". "asked for" is what Nick does now, not a
    # past decision; a temporal word plus a bare pronoun is narration, not a claim. These assertions
    # exist so a future edit cannot quietly reintroduce either.
    for c in ["You asked for a cleanup, so that gets fixed before the push.",
              "an hour before Codex flagged it — earlier, I wrote that assertion myself",
              "You asked me for all phases, so I did all phases.",
              "Earlier in this turn I said the corpus was corrections-only.",
              "As you asked for above, I ran Codex over the whole subsystem.",
              "you asked for; earlier — I"]:
        check(f"no false block: {c[:38]}", not detect(c)[0])
    # QUOTED MENTIONS are not claims — writing ABOUT the gate must not trip it. Measured live:
    # the reply explaining this very tuning was blocked by its own example string.
    for c in ["It requires a claim verb, so `earlier we decided` trips and `earlier I wrote` does not.",
              'The pattern keeps "we decided last week" while dropping narration.',
              "The gate blocked on 'you asked for' which was never a decision."]:
        check(f"quoted mention not a claim: {c[:34]}", not detect(c)[0])
    check("a quote elsewhere does not shield a real claim",
          bool(detect('He said "hello" and then we decided last week to ship it.')[0]))
    # ...while genuine past-decision attribution still trips it
    for c in ["Last time we decided to keep it shadow-only.",
              "You told me to skip the migration.",
              "you asked for that diagram last time we talked"]:
        check(f"still detects: {c[:38]}", bool(detect(c)[0]))
    # benign present/future text that should NOT trip it
    for c in ["let's build the gate and test it",
              "I'll pin the extraction to Haiku now",
              "the hook is broken and needs a fix",
              "change the button color to blue"]:
        check(f"ignores: {c[:40]}", not detect(c)[0])
    # current-session claims: suppressed (logged), never blocked (2026-07-27 tune)
    for c in ["tonight we decided to purge and re-extract the ops data",
              "we discussed the purge plan earlier this session and ran it",
              "you approved the fleet push earlier tonight",
              "we agreed just now that workflow transcripts stay invisible"]:
        h, s = detect(c)
        check(f"suppresses current-session: {c[:36]}", not h and s)
    # a past claim NEXT TO current-session text still enforces if outside the window
    far = "we decided last session to pin Haiku for extraction." + (" filler" * 40) + " tonight I ran the tests."
    check("past claim beyond window still detects", bool(detect(far)[0]))
    # Codex #1: suppressed match must NOT shadow a later past claim on the same pattern
    shadow = "Tonight we decided to ship the gate fix. Last month we decided to keep the old design."
    h, s = detect(shadow)
    check("suppressed match doesn't shadow later past claim", bool(h) and bool(s))
    # Codex #2: past claim stays enforced when an ADJACENT sentence is current-session
    adj = f"{_U.name()} approved the baseline push last week. Tonight I ran the deployment tests."
    check("adjacent current-session sentence doesn't suppress past claim", bool(detect(adj)[0]))
    # Codex #2 override: past marker in the SAME sentence beats a current marker
    both = "Tonight I confirmed that we agreed last month to keep the manifest excludes."
    check("past override within sentence still enforces", bool(detect(both)[0]))
    # satisfier
    check("brain tool recognized: recall_similar", is_brain_tool("mcp__core-brain__recall_similar"))
    check("brain tool recognized: get_entity", is_brain_tool("mcp__core-brain__get_entity"))
    check("local read NOT a brain tool: Grep", not is_brain_tool("Grep"))
    check("local read NOT a brain tool: Read", not is_brain_tool("Read"))
    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    sys.exit(main())
