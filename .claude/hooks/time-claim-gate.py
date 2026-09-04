#!/usr/bin/env python3
"""Stop hook — detects time/duration claims made without a same-turn time-source
tool call. Sibling of state-claim-gate.py but scoped to time-of-day, durations,
and session-bound time claims.

Implements CLAUDE.md anti-pattern rule for time-awareness (spec:
tasks/specs/spec-time-awareness-fix-2026-05-15.md Phase 3): every "we worked Nh /
started at X / this morning / 18:54 PDT" must be sourced from a tool call this
turn — not from Claude's recollection.

Time-source tools that satisfy a time-claim:
  - Bash where command contains: date, compute-session-duration,
    get-session-start-time, .session-start, .last-session-start,
    stat, %Y-%m-%d, +%s, timestamp
  - Read of: .claude/state/.session-start, .claude/state/.last-session-start,
    sessions/*.md, ~/.claude/projects/*.jsonl
  - Grep targeting any of the above paths

Honors stop_hook_active to avoid infinite loops.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
import core_paths  # noqa: E402

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

PROJECTS_DIR = os.path.expanduser(
    "~/.claude/projects/-" + os.getcwd().lstrip("/").replace("/", "-").replace(" ", "-")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
try:
    import hooklog  # durable telemetry → .claude/state/hook-events.log
except Exception:
    hooklog = None
_SESSION = ""  # set from stdin payload in main(); used by log_block telemetry

# ── PATTERNS ────────────────────────────────────────────────────────────────
# Common claim-marker context words used by the clock-time pattern to filter
# bare timestamps echoed from tool output. A timestamp without any of these
# nearby is almost certainly a metadata echo (commit timestamp, log line,
# session-file header) — not Claude asserting present time in prose.
_TIME_CONTEXT = (
    r"we|i|us|session|started?|begin|began|begun|ended?|finished?|wrapped?|"
    r"now|currently|elapsed|ago|since|until|by|been|going|kicked\s+off|"
    r"so\s+far|right\s+now|earlier|previously|this\s+session|last\s+session|"
    r"o['']?clock"
)

TIME_PATTERNS = [
    # 1a. Clock claim PRECEDED by a temporal context word within 40 chars.
    #     "we started at 9:00 AM PDT" / "session began at 14:22 PDT" → fires.
    #     Tightened 2026-05-22 (audit response) — original bare pattern fired
    #     on every "21:39 PDT" echoed from a git log / session header / tool
    #     output line (62.5% FP rate per 2026-05-21 audit).
    re.compile(
        rf"\b(?:{_TIME_CONTEXT})\b[^.\n]{{0,40}}?"
        r"\b\d{1,2}:\d{2}\s*(?:AM|PM|A\.M\.|P\.M\.)?\s*(?:PDT|PST|PT|ET|EST|EDT|UTC|GMT)\b",
        re.IGNORECASE,
    ),
    # 1b. Clock claim FOLLOWED by a temporal context word within 40 chars.
    #     "9:00 AM PDT — we kicked off" / "14:22 PDT, started a few minutes ago".
    re.compile(
        r"\b\d{1,2}:\d{2}\s*(?:AM|PM|A\.M\.|P\.M\.)?\s*(?:PDT|PST|PT|ET|EST|EDT|UTC|GMT)\b"
        rf"[^.\n]{{0,40}}?\b(?:{_TIME_CONTEXT})\b",
        re.IGNORECASE,
    ),
    # 2. Duration + session/work reference within 60 chars
    re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:hour|hr|h|minute|min|m)s?\b"
        r"(?:[^.]{0,60}?\b(?:session|we|worked|ran|going|been|elapsed|started|ago)\b)",
        re.IGNORECASE,
    ),
    # 3. Numeric-word durations + session/work reference within 60 chars.
    #    2026-06-23 (A4 fix): added the same context guard pattern 2 uses, so bare
    #    "give me five minutes" / "backoff for five minutes" no longer fire.
    re.compile(
        # context BEFORE the duration ("we've been working this session ... three hours") ...
        r"\b(?:session|we|worked|ran|going|been|elapsed|started|ago)\b[^.]{0,60}?"
        r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty|thirty|forty-five)\s+(?:hour|minute|hr|min)s?\b"
        r"|"
        # ... OR context AFTER ("five minutes into the session"). Bare "five minutes" with
        # no session context on either side stays silent (A4 fix, two-sided 2026-06-23).
        r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty|thirty|forty-five)\s+(?:hour|minute|hr|min)s?\b"
        r"[^.]{0,60}?\b(?:session|we|worked|ran|going|been|elapsed|started|ago)\b",
        re.IGNORECASE,
    ),
    # 4. Session-bound verbs
    re.compile(
        r"\bsession\s+(?:started|began|ended|finished|kicked\s+off|wrapped)\b",
        re.IGNORECASE,
    ),
    # 5. Time-of-day greetings/markers
    re.compile(
        r"\b(?:this|tonight|earlier\s+this)\s+(?:morning|afternoon|evening|night)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bgood\s+(?:morning|afternoon|evening)\b", re.IGNORECASE),
    # 5b. Bare time-of-day adverbs (tonight/today/tomorrow) used self-referentially.
    #     2026-06-04: the compound-only pattern above missed bare "tonight"/"today"
    #     ("I'm living proof tonight", "we shipped it today"). Gated by a temporal/
    #     personal context word within 40 chars + a possessive exclusion so it does
    #     NOT fire on "today's date for when_made" (a field value, not a now-claim).
    re.compile(
        rf"\b(?:{_TIME_CONTEXT})\b[^.\n]{{0,40}}?\b(?:tonight|today|tomorrow)(?!['’]s)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:tonight|today|tomorrow)(?!['’]s)\b[^.\n]{{0,40}}?\b(?:{_TIME_CONTEXT})\b",
        re.IGNORECASE,
    ),
    # 6. Relative time anchors
    re.compile(
        r"\b(?:\d+|an?|a\s+few|several)\s+(?:hour|minute|hr|min)s?\s+ago\b",
        re.IGNORECASE,
    ),
]

# ── EXEMPTION LOGIC ─────────────────────────────────────────────────────────
TIME_SATISFYING_PATH_SUBSTRINGS = (
    ".session-start",
    ".last-session-start",
    "sessions/",
    "/.claude/projects/",
)

TIME_SATISFYING_BASH_SUBSTRINGS = (
    "date",
    "compute-session-duration",
    "get-session-start-time",
    ".session-start",
    ".last-session-start",
    "stat ",
    "%Y-%m-%d",
    "+%s",
    "timestamp",
    "/.claude/projects/",
)


def is_time_source_tool(name: str, tool_input: dict) -> bool:
    if not name:
        return False
    tool_input = tool_input or {}
    if name == "Bash":
        cmd = tool_input.get("command", "") or ""
        return any(sub in cmd for sub in TIME_SATISFYING_BASH_SUBSTRINGS)
    if name == "Read":
        path = tool_input.get("file_path", "") or ""
        return any(sub in path for sub in TIME_SATISFYING_PATH_SUBSTRINGS)
    if name in ("Grep", "Glob"):
        path = (tool_input.get("path") or tool_input.get("pattern") or "")
        return any(sub in path for sub in TIME_SATISFYING_PATH_SUBSTRINGS)
    return False


def log_block(pattern: str, excerpt: str) -> None:
    """Emit one durable telemetry line via the shared hooklog lib (fail-open)."""
    if hooklog is None:
        return
    hooklog.log("time-claim-gate", "Stop", verdict="block",
                trigger=(excerpt or pattern or ""), session=_SESSION)


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


def detect(text, only_idx=None):
    """Time/duration claims in `text`. `only_idx` restricts to those pattern indices.

    PER-SPAN, NOT PER-RESPONSE (2026-08-05). The caller used to build one flat hit list and then ask
    "is anything sourceable AND nothing unsourceable" over the whole response, so a single unsourced
    duration re-armed the gate for every phrase in the turn — including the ones the injected clock
    demonstrably sources. The block was right; the MESSAGE was wrong, telling me to source "this
    morning" when the clock had already answered it and the real offender was "an hour ago"
    elsewhere in the same reply. A gate that names the wrong offender teaches the wrong lesson, and
    it is the reason this one felt like noise while it was in fact catching real errors.

    `only_idx` lets the caller ask specifically for the UNSOURCEABLE hits and report those.
    """
    ct = _claimtext()
    spans = ct.quoted_spans(text) if ct else None
    hits = []
    for i, rx in enumerate(TIME_PATTERNS):
        if only_idx is not None and i not in only_idx:
            continue
        for m in rx.finditer(text or ""):
            if ct and ct.is_mention(text, m.start(), m.end(), spans):
                continue
            hits.append(m.group(0))
            if len(hits) >= 3:
                return hits
    return hits


_CLOCK_RX = re.compile(r"⏰ \d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+ \(live, this turn\)")


def _clock_in_context(jsonl_path) -> bool:
    """Did session-presence inject the live clock into THIS turn?

    Checked against the transcript rather than assumed, for the same reason every other claim
    in this system is: the hook can fail, be unregistered on a peer Core, or be edited. If the
    clock is not actually there, the gate must keep blocking — that is the fail-toward-catching
    direction, and it is the one that cannot silently license a time claim.

    Scans backwards only to the last real user prompt, so a clock from an earlier turn in the
    same session does not exempt this one. It is stamped per-turn on purpose.
    """
    try:
        with open(jsonl_path) as f:
            lines = f.readlines()
    except Exception:
        return False
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if d.get("type") == "user":
            text = content if isinstance(content, str) else json.dumps(content)
            if _CLOCK_RX.search(text or ""):
                return True
            # A real user prompt (not a tool_result echo) is the turn boundary — stop here.
            if not (isinstance(content, list) and content and all(
                    isinstance(p, dict) and p.get("type") == "tool_result" for p in content)):
                return False
    return False


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
        import hooklog as _hl; _hl.invoked("time-claim-gate", "Stop")
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

    last_text = ""
    turn_tools = []  # ALL assistant tool_uses since the last real user prompt
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
                    # A real user prompt (not a tool_result echo) starts a new
                    # turn — reset the per-turn accumulators. Tool-result-only
                    # user messages are part of the SAME turn and must not reset.
                    is_tool_result_only = (
                        isinstance(content, list)
                        and len(content) > 0
                        and all(
                            isinstance(p, dict) and p.get("type") == "tool_result"
                            for p in content
                        )
                    )
                    if not is_tool_result_only:
                        turn_tools = []
                        last_text = ""
                    continue
                if dtype != "assistant":
                    continue
                if not isinstance(content, list):
                    continue
                texts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get("type")
                    if pt == "text":
                        texts.append(p.get("text", "") or "")
                    elif pt == "tool_use":
                        nm = p.get("name") or ""
                        inp = p.get("input") or {}
                        if nm:
                            # Accumulate across the WHOLE turn, not just this
                            # message. The time-source tool (`date`,
                            # compute-session-duration) is normally called in an
                            # EARLIER assistant message than the text that cites
                            # its result — you call it, get the result, THEN
                            # state the value in a new message. Checking only the
                            # final message's tools made every correctly-sourced
                            # time claim fire as a false positive (2026-05-24 fix).
                            turn_tools.append((nm, inp))
                if texts:
                    last_text = " ".join(texts)
    except FileNotFoundError:
        return 0

    if not last_text:
        return 0

    if len(last_text.strip()) < 40:
        return 0

    hits = detect(last_text)

    if not hits:
        return 0

    if any(is_time_source_tool(nm, inp) for nm, inp in turn_tools):
        return 0

    # ── THE INJECTED CLOCK IS A SOURCE (2026-07-30, master plan Phase 1.1) ──────────────
    #
    # session-presence now injects the live wall clock on EVERY UserPromptSubmit. A claim
    # about what time it is NOW is therefore sourced from this turn's context, and blocking
    # it would be the gate policing a fact the system already supplied — pure cost, and the
    # kind of cost that is worse than it looks, because a Stop block is a continuation: it
    # leaves BOTH the flawed sentence and the correction in the transcript for Nick to read.
    #
    # The exemption is deliberately PARTIAL, and the split is the substance of this change:
    #
    #   TIME-OF-DAY  ("this morning", "good evening", "tonight", "14:22 PDT")
    #                the injected clock answers it completely -> exempt.
    #
    #   DURATION / SESSION-BOUND  ("we've been at this 3h", "the session started at 9:00",
    #                "an hour ago")
    #                the clock gives you NOW and says nothing about the start, so the claim
    #                still needs compute-session-duration.sh or .session-start -> still blocked.
    #
    # AN ALLOWLIST, NOT A BLOCKLIST — and that inversion is the fix, not a style choice.
    #
    # The first version listed the DURATION patterns and exempted everything else. Codex's
    # adversarial pass found the hole before this reached the baseline: "We started at 09:14 PDT
    # and the first commit followed shortly after" matches the clock-claim pattern but NO duration
    # pattern (index 4 requires the literal "session started"), so it was exempted — and the
    # current clock cannot possibly source a claimed 09:14 start. Verified reproducing before
    # fixing.
    #
    # The defect is structural, not a missing entry: with a blocklist, anything not enumerated is
    # exempt BY DEFAULT, so every future pattern added to TIME_PATTERNS silently arrives exempt.
    # An allowlist fails the other way — a new pattern is gated until someone deliberately decides
    # the clock can source it. For a gate, defaulting to "still checked" is the only safe default.
    #
    # What the injected clock CAN source is exactly one thing: what time it is NOW. So the exempt
    # set is the present-tense time-of-day patterns and nothing else. A clock time attached to a
    # past event, an elapsed span, or a session boundary is a claim about the PAST, which needs
    # compute-session-duration.sh or .session-start.
    CLOCK_SOURCEABLE_IDX = {5, 6, 7, 8}   # "this morning", "good evening", bare tonight/today
    sourceable = any(TIME_PATTERNS[i].search(last_text)
                     for i in CLOCK_SOURCEABLE_IDX if i < len(TIME_PATTERNS))
    unsourceable = any(TIME_PATTERNS[i].search(last_text)
                       for i in range(len(TIME_PATTERNS)) if i not in CLOCK_SOURCEABLE_IDX)
    if sourceable and not unsourceable and _clock_in_context(path):
        return 0

    # REPORT ONLY WHAT ACTUALLY NEEDS SOURCING. When the clock is present, the sourceable phrases
    # are already answered; naming them in the block message sends the reader to re-source something
    # the system supplied, while the real offender sits further down the reply. Coverage is
    # unchanged — any unsourced duration still blocks — so this is precision, not a weakened gate.
    # It will NOT by itself reduce the block count: that only falls when the agent stops asserting
    # durations from memory, which is exactly what the metric is supposed to measure.
    if unsourceable and _clock_in_context(path):
        _unsourced_idx = {i for i in range(len(TIME_PATTERNS)) if i not in CLOCK_SOURCEABLE_IDX}
        _precise = detect(last_text, only_idx=_unsourced_idx)
        if _precise:
            hits = _precise

    log_block(hits[0][:60], hits[0])
    hits_str = "; ".join(f'"{h}"' for h in hits[:3])
    reason = (
        f"TIME-CLAIM GATE — your last response asserts time/duration ({hits_str}) "
        "without a same-turn tool call sourcing it. Anti-pattern: time-claim-from-memory. "
        "Append a one-line verification footnote with the sourced value — do NOT redo the whole answer. "
        f"Either (a) run `date`, `cat {core_paths.SESSION_START}`, or "
        f"`bash {core_paths.BIN_COMPUTE_SESSION_DURATION}` and cite the result, or "
        "(b) reframe as uncertain (\"I don't have current time loaded — want me to check?\")."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
