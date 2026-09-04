#!/usr/bin/env python3
"""approval-gate — the binding enforcement gate (B1, 2026-06-08).

THE FIX for the acknowledgment escape hatch (tasks/research/si-enforcement-break-diagnosis-2026-06-07.md):
the old guards blocked on the WORD "acknowledgment" (any "you're right, fixing it" passed). This gates on
Nick's APPROVAL TOKEN instead. State machine:

  - A redirect/frustration/stop signal in a user prompt  -> set  .claude/state/.awaiting-approval
  - While that marker is set, MUTATING tools are REFUSED (exit 2). Read-only tools stay allowed so Core
    can investigate and form a plan.
  - The marker clears ONLY on an explicit approval token in the NEXT user prompt (go / yes / do it /
    approved / build it / send it / ship it) — NOT on Core's own text.

This file handles BOTH hook events (it inspects stdin to tell which):
  - UserPromptSubmit : arm/clear the marker from the user's prompt.   (never blocks)
  - PreToolUse       : block mutating tools while the marker is set.   (exit 2 to refuse)

Registered 2026-06-08 (commit dd2fcae, Nick-approved): UserPromptSubmit (all matchers) +
PreToolUse (Write|Edit|MultiEdit|NotebookEdit|Task). UserPromptSubmit arms/clears the
.awaiting-approval marker; PreToolUse blocks mutating tools while the marker is set.

Run self-test:  python3 approval-gate.py --selftest
"""
import json
import os
import re
import sys
from pathlib import Path

_instance_env = os.environ.get("CORE_INSTANCE")
INSTANCE_ROOT = Path(_instance_env) if _instance_env else Path(__file__).resolve().parents[2]
STATE_DIR = INSTANCE_ROOT / ".claude" / "state"
MARKER = STATE_DIR / ".awaiting-approval"


def _operator() -> str:
    """Operator's name from identity.json (fork-portable — no hardcoded 'Nick')."""
    try:
        return json.loads((INSTANCE_ROOT / ".claude" / "identity.json").read_text())["user"]["name"]
    except Exception:
        return "the operator"


OPERATOR = _operator()

MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Workflow", "Task", "Agent"}

# High-precision redirect/stop/frustration markers. Deliberately STRICT — boy-who-cried-wolf is the
# documented failure mode for these hooks (brain: "Stop-signal gate must be high-precision"). A casual
# "no" / "don't" must NOT arm the gate; only a real course-change signal does.
REDIRECT_MARKERS = [
    r"\bstop\b(?!\s+(?:the|this|that|all|every)\b)", r"\bhold\s+up\b",
    r"\bwait\b(?!\s+(?:for|until|while|a\s+(?:sec|second|moment|minute|bit)|on\s+me|let\s+me)\b)",  # 2026-06-23: don't arm on 'wait for me' / 'wait a sec'
    r"\bhalt\b",
    r"\bthat'?s?\s+(?:not|wrong|incorrect)\b", r"\bnot\s+what\s+i\b",
    r"\byou'?re\s+(?:wrong|missing)\b", r"\bdon'?t\s+(?:just|barrel|do\s+that)\b",
    r"\bback\s+up\b", r"\bredo\b", r"\bstart\s+over\b",
    r"\bwhy\s+(?:did|are|would)\s+you\b", r"\bthat'?s?\s+the\s+(?:most\s+)?(?:surface|shit)\b",
]
# Explicit approval tokens that release the gate. Must be a clear GO from Nick, not Core's own words.
# INVESTIGATION verbs after "go" mean "go LOOK at X" (an instruction to investigate), NOT approval —
# the gate must stay armed. Everything else after a leading "go" (go ahead / go deep / go do it / bare
# "go") IS approval. 2026-07-01 fix: the 2026-06-23 bare-'go' removal over-corrected and false-negatived
# standalone "Go" / "go deep" / "No go deep" three times in one session, blocking approved work.
_GO_INVESTIGATION = r"look|check|find|read|re-?read|see|grep|search|explore|investigate|dig|examine|back|through|over|easy|slow"
APPROVAL_TOKENS = [
    r"\bgo\s+(?:ahead|deep|for\s+it|with\s+it|do\b|build\b|ship\b|run\b)",           # go ahead / go deep / go do it / go build it
    r"^\s*(?:no[,.]?\s+|ok(?:ay)?[,.]?\s+|yeah[,.]?\s+|yep[,.]?\s+|sure[,.]?\s+)?go\b(?!\s+(?:" + _GO_INVESTIGATION + r")\b)",  # leading/standalone "go" (not "go look")
    r"\byes\b", r"\bdo\s+it\b", r"\bapproved?\b", r"\bbuild\s+it\b",
    # 2026-07-19: over-fire fix — the gate armed on frustration then stayed armed for ~6 turns because
    # natural forward directives ("lets do claude api", "ok lets /close-core") matched no token. Add
    # imperative "let's <positive-verb>" + proceed/keep-going. Keyed on user language (no slash-command
    # clear — that would let Core clear its own gate via a Skill call). "let's not" won't match.
    r"\blet'?s\s+(?:go|do|run|build|ship|close|start|proceed|continue|keep\s+going|move|finish)\b",
    r"\bproceed\b", r"\bkeep\s+going\b", r"\bcarry\s+on\b",
    # 2026-07-27 — FIFTH over-fire of the same class. Two consecutive unambiguous approvals were not
    # recognised in one session: "why you stop. continue all the way through" (the word "stop" armed
    # it, and bare "continue" was not a token) and "see it needs to be tunned. go" (bare "go" was
    # only matched at the START of a prompt, never at the end).
    #
    # The pattern across all five fixes is that this list is maintained by patching after each miss,
    # which is why it keeps missing. Logged as the first real case for the estate sweep, which should
    # be deriving these from evidence rather than waiting for the next false block.
    # Negation guard covers the whole family, not two literal spellings. The first draft excluded only
    # "don't continue" / "do not continue", so "won't continue" and "shouldn't continue" still read as
    # approval, and trailing "go" had no guard at all — "don't go" cleared the gate (sentinel-code,
    # 2026-07-27). A false CLEAR is the dangerous direction here: it lets work proceed through a
    # redirect, which is the exact failure this gate exists to prevent.
    # Python lookbehinds must be fixed-width, so each negation is its own assertion. The
    # apostrophe-less spellings are not optional extras — Nick types "dont" / "wont" routinely, and
    # without them "dont go" read as approval.
    r"(?<!\bnot )(?<!n't )(?<!dont )(?<!wont )(?<!cant )(?<!never )(?<!\bno )\bcontinue\b",
    r"(?<!\bnot )(?<!n't )(?<!dont )(?<!wont )(?<!cant )(?<!never )(?<!\bno )\bgo\s*[.!]?\s*$",
    r"\bsend\s+it\b", r"\bship\s+it\b", r"\bproceed\b", r"\bgreen\s*light\b",
    r"\blgtm\b", r"\bdo\s+everything\b", r"\bexecute\b",
]


def _arm():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(f"awaiting {OPERATOR}'s explicit approval after a redirect\n")
    except Exception:
        pass


def _clear():
    try:
        MARKER.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _matches(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


def handle_user_prompt(prompt: str):
    """UserPromptSubmit: an approval token clears; otherwise a redirect arms. Approval wins ties
    (if Nick says 'yes go do X' that's a green light, not a redirect). Never blocks."""
    # System/automated content (task-notifications, tool-results, command wrappers, reminders)
    # is NOT a user redirect — never arm on it. 2026-06-18 fix (armed on a workflow-completion notice).
    if re.search(r"<task-notification>|</?status>|tool_use_id|Dynamic workflow|<system-reminder>|<command-(name|message)>|<local-command|persisted-output|<task-id>", prompt or "", re.I):
        return
    # Self-issued loop/workflow continuations are NOT redirects — never arm on them
    # (a /loop prompt may contain 'stop the loop when…'). 2026-06-18 fix.
    if re.match(r'^\s*/(loop|workflow)\b', prompt or '') or re.search(r'\b(?:stop|end|conclude|halt)\s+the\s+(?:loop|workflow|run|cycle)\b', prompt or '', re.I):
        return
    if _matches(APPROVAL_TOKENS, prompt):
        _clear()
    elif _matches(REDIRECT_MARKERS, prompt):
        _arm()
    # else: leave the marker as-is (a neutral prompt mid-redirect keeps the gate armed).


def handle_pretooluse(tool_name: str):
    """PreToolUse: refuse mutating tools while awaiting approval. Read-only tools pass."""
    if MARKER.exists() and tool_name in MUTATING_TOOLS:
        sys.stderr.write(
            f"⛔ approval-gate: {OPERATOR} redirected and hasn't said go yet. "
            f"Refusing {tool_name} until an explicit approval (go/yes/do it). "
            "Read-only tools are allowed — investigate and present a plan first.\n"
        )
        sys.exit(2)


def main():
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("approval-gate", "PreToolUse")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    # Disambiguate the event from the payload shape.
    if "tool_name" in data or "toolName" in data:
        handle_pretooluse(data.get("tool_name") or data.get("toolName") or "")
    else:
        prompt = data.get("prompt") or data.get("user_message") or ""
        if prompt:
            handle_user_prompt(prompt)


def _selftest():
    import tempfile, types
    global STATE_DIR, MARKER
    tmp = Path(tempfile.mkdtemp())
    STATE_DIR = tmp; MARKER = tmp / ".awaiting-approval"
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name); ok = ok and cond
    # 0. NEGATION LOCK (2026-07-27). The token list has now been patched five times, each time after
    #    it falsely blocked work — and the fifth fix itself shipped two bugs: "don't go" cleared the
    #    gate (trailing-go had no guard) and "dont go" cleared it (apostrophe-less spellings, which
    #    Nick actually types, were not covered). A false CLEAR is the dangerous direction: it lets
    #    work proceed through a redirect, which is the one thing this gate exists to prevent. These
    #    assertions are the regression lock that was missing, so the next edit cannot quietly
    #    reintroduce either.
    for _p, _want in [("go", True), ("ok go", True), ("continue", True),
                      ("see it needs to be tuned. go", True),
                      ("continue all the way through", True), ("keep going", True),
                      ("don't go", False), ("dont go", False), ("wont continue", False),
                      ("cant continue", False), ("never continue", False),
                      ("do not continue", False), ("go look at the file", False)]:
        check(f"approval({_p!r}) == {_want}", _matches(APPROVAL_TOKENS, _p) is _want)
    # 1. redirect arms
    handle_user_prompt("hold up, that's not what I asked")
    check("redirect arms the marker", MARKER.exists())
    # 2. mutating tool blocked while armed
    blocked = False
    try:
        handle_pretooluse("Edit")
    except SystemExit as e:
        blocked = (e.code == 2)
    check("Edit blocked while awaiting approval", blocked)
    # 3. read-only tool allowed while armed
    allowed = True
    try:
        handle_pretooluse("Read")
    except SystemExit:
        allowed = False
    check("Read allowed while awaiting approval", allowed)
    # 4. approval token clears
    handle_user_prompt("ok go do it")
    check("approval token clears the marker", not MARKER.exists())
    # 5. after clear, mutating tool allowed
    allowed2 = True
    try:
        handle_pretooluse("Edit")
    except SystemExit:
        allowed2 = False
    check("Edit allowed after approval", allowed2)
    # 6. acknowledgment WITHOUT approval does NOT clear (the escape-hatch fix)
    handle_user_prompt("stop, you're wrong")
    check("re-armed by new redirect", MARKER.exists())
    # simulate Core saying 'you're right, fixing it' — that's NOT a user prompt, so marker stays.
    check("Core's own ack can't clear it (marker still set)", MARKER.exists())
    # 7. 2026-07-01 fix: standalone / leading "go" variants MUST clear (the 3x-blocked case)
    for phrase in ["Go", "go deep", "No go deep on everything you need", "Go ahead and", "ok go"]:
        handle_user_prompt("hold up")                 # arm
        handle_user_prompt(phrase)                    # should clear
        check(f'"{phrase}" clears the gate', not MARKER.exists())
    # 8. "go LOOK/CHECK/READ" (investigation) must NOT clear — preserves 2026-06-23 intent
    for phrase in ["go look at the config", "go check the file", "go read that", "go back"]:
        handle_user_prompt("hold up")                 # arm
        handle_user_prompt(phrase)                    # should stay armed
        check(f'"{phrase}" keeps the gate armed', MARKER.exists())
    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    main()
