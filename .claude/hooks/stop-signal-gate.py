#!/usr/bin/env python3
"""stop-signal-gate.py — UserPromptSubmit hook.

Structural enforcement for the correction patterns that measure-rule-fitness.py
flagged as NOT graduating under CLAUDE.md rules alone (correction-stop-execution,
correction-explicit-no, correction-frustration). The loop's verdict (2026-05-31):
pure behavioral rules recur at the same rate post-promotion; only HOOK-enforced
patterns graduate (state-claim, say-do = 0 recurrence). So this is the structural
escalation the loop itself prescribes.

When Nick issues a halt / explicit-no / frustration signal, inject a hard halt
directive: STOP the current approach, do NOT continue prior actions, re-ground in
exactly what he asked. A CLAUDE.md rule is only sometimes in context; this fires
every time the signal appears.

HIGH-PRECISION by design — calibrated to Nick's actual stop/frustration signals
(boy-who-cried-wolf is the failure mode for these hooks, per the 2026-05-17 recall
pattern audit). Casual "no"/"don't"/"stop" in a normal sentence must NOT fire; only
strong, unambiguous halt/frustration markers do. Telemetry written so the loop can
measure whether this graduates the patterns.

Output via hookSpecificOutput.additionalContext — never blocks the prompt.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # user name from identity.json, never hardcoded
import json
import os
import re
import sys
from pathlib import Path

# Prompt-stage hooks fire for runtime-injected text too — task-notifications, monitor events, command
# stdout. Guard imported rather than re-declared: HOOK_PREFIXES already existed in FOUR byte-identical
# copies and none of them listed `<task-notification>`, which is 72% of this seat's prompt-stage
# traffic. See .claude/hooks/_prompt_source.py. Import failure leaves today's behaviour unchanged.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _prompt_source import is_user_text as _is_user_text
except Exception:  # pragma: no cover - fail toward firing, never toward silent suppression
    def _is_user_text(_p):
        return True


INSTANCE = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
STATE_DIR = INSTANCE / ".claude" / "state"

# High-precision signal sets. Each is a strong, unambiguous halt/redirect/frustration
# marker — NOT casual usage. Calibrated against 2026-05-31 session prompts:
# "holddddd up", "wtf why did this happen", "stop with all of this freeze already",
# "no bruh", "figure it tf out", "come on".
HALT_RX = re.compile(
    r"\b(hol+d+\s*up|hol+dup|wait\s+wait|stop\s+(with|doing|wasting|right now)|"
    r"cut\s+it\s+out|quit\s+it|that'?s\s+enough|knock\s+it\s+off|pump\s+the\s+brakes)\b",
    re.I,
)
EXPLICIT_NO_RX = re.compile(
    r"\b(no\s+bruh|no+\s+(stop|dont|don'?t)|that'?s\s+not\s+what|not\s+what\s+i\s+(want|asked|said)|"
    r"i\s+didn'?t\s+(ask|say|want)|that'?s\s+wrong|completely\s+(wrong|off))\b",
    re.I,
)
FRUSTRATION_RX = re.compile(
    r"(\bwtf\b|\btf\b|fuck|ffs|bruh\b|are\s+you\s+(kidding|serious)|"
    r"why\s+(the\s+hell|are\s+you|did\s+this\s+happen)|what\s+is\s+going\s+on|"
    r"figure\s+it\s+(tf\s+)?out|slipping|you\s+keep)",
    re.I,
)



def _claimtext():
    """Shared mention discriminators (lib/claimtext.py) — see that file's header. Fail-open."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import claimtext
        return claimtext
    except Exception:
        return None


def detect(text):
    """(fired, {halt, no, frustration}) for a user prompt. A halt/no phrase inside quotes is being
    DISCUSSED — Nick writing *don't say "stop"* is not a stop signal."""
    ct = _claimtext()
    text = text or ""
    spans = ct.quoted_spans(text) if ct else None

    def _hit(rx):
        for m in rx.finditer(text):
            if ct and ct.is_mention(text, m.start(), m.end(), spans):
                continue
            return True
        return False

    flags = {"halt": _hit(HALT_RX), "no": _hit(EXPLICIT_NO_RX), "frustration": _hit(FRUSTRATION_RX)}
    return any(flags.values()), flags


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    prompt = data.get("prompt") or data.get("user_message") or ""
    if not _is_user_text(prompt):
        return  # runtime-injected turn, not the user speaking

    if not prompt or len(prompt) < 2:
        return

    fired, _flags = detect(prompt)
    halt, no, frustration = _flags["halt"], _flags["no"], _flags["frustration"]

    # ── BIND THE CLASS, don't just advise it (2026-08-25). ──────────────────────────────────────
    #
    # This hook only ever INJECTED, and the SI loop measured the result for 70 days:
    # "stop-and-plan fires but its correction keeps recurring — NOT BINDING". UserPromptSubmit
    # cannot refuse the tool call that follows it, so the halt was a suggestion.
    #
    # It now drops a marker that `recall-first-gate.py` (PreToolUse, matcher
    # Write|Edit|MultiEdit|NotebookEdit) refuses on, and `recall-satisfied.py` (PostToolUse, read
    # tools) clears. That is the SAME marker/enforce/satisfy triple already shipped for recall-first
    # — the SI item's own remedy is "wire the binding gate (recall-first-gate) for this class", and
    # wiring into it beats standing up a second gate beside it.
    #
    # ONLY ON halt / explicit-no. NEVER on frustration alone. That restriction is the whole reason
    # this is safe to make binding: 85% of this hook's fires are frustration-only and they run 41:1
    # toward "go", so a gate that treated frustration as a halt would block Nick's own instructions
    # to act — turning the measured problem into a much worse one. The split above is what earns
    # the teeth here.
    #
    # Cleared and rewritten on EVERY genuine user prompt, so the marker can never outlive its turn.
    # Machine turns (task-notifications) returned above at the _is_user_text guard and therefore
    # cannot wipe it mid-turn.
    try:
        _sid = data.get("session_id") or "unknown"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _plan_marker = STATE_DIR / f".stop-plan-required-{_sid}"
        if _plan_marker.exists():
            _plan_marker.unlink()
        if halt or no:
            _plan_marker.write_text(json.dumps({"snippet": prompt[:160],
                                                "halt": halt, "no": no}))
    except Exception:
        pass

    if not fired:
        return

    # Telemetry so measure-rule-fitness can track whether this graduates the patterns.
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        sid = data.get("session_id") or "unknown"
        path = STATE_DIR / f".stop-signal-{sid}.json"
        prev = json.loads(path.read_text()) if path.exists() else {"fires": []}
        prev["fires"] = (prev.get("fires", []) + [{
            "halt": halt, "no": no, "frustration": frustration, "snippet": prompt[:80]
        }])[-50:]
        path.write_text(json.dumps(prev, indent=2))
    except Exception:
        pass

    kinds = []
    if halt:
        kinds.append("HALT")
    if no:
        kinds.append("EXPLICIT-NO / redirect")
    if frustration:
        kinds.append("FRUSTRATION")

    # ── TWO PAYLOADS, SPLIT ON THE SIGNAL. Rewritten 2026-08-25. ────────────────────────────────
    #
    # This hook shipped ONE payload — "STOP the current approach... halt it... ask ONE specific
    # question" — for all three signals. Its own telemetry says that was wrong for most of what it
    # catches. Measured across 232 recorded fires in 48 sessions on this seat:
    #
    #     frustration-only, no halt and no explicit-no ......... 197 of 232  (85%)
    #     of those, carrying a directional marker:
    #         GO-shaped   ("do it", "why are you not", "keep going") .... 41
    #         STOP-shaped ("hold up", "stop doing", "wait") .............. 1
    #
    # Forty-one to one. The frustration fires include (profanity redacted here): "I APPROVE BRUH
    # DO IT", "WHYYYYYYYY DO YOU KEEP STOPPPING I APPROVE EVERYTHING KEEP GOING", "GO FIX IT ON ALL
    # CORES NOWWWW", "why are you not doing anythinggggggggg why do you stop". On every one of
    # those this hook injected an instruction to halt, stop mid-plan, and ask a question.
    #
    # So the SI verdict "fires but its correction keeps recurring — NOT BINDING" was reading the
    # symptom. The hook binds fine. It binds BACKWARDS on 85% of its fires, and the behaviour it
    # produces there — halting, re-checking, inserting an unrequested approval step — is *literally*
    # two of the three forbidden_moves on the contract it enforces:
    #   · "Don't hedge, re-check, or re-litigate after Nick has explicitly reauthorized ... — act"
    #   · "don't insert an unrequested check-in/ping step when the instruction implied autonomous
    #      completion"
    # A gate that manufactures the correction it fires on is a feedback loop, not an enforcement
    # layer, and 70 days of non-graduation is what that looks like from the outside.
    #
    # THE FIX IS THE PAYLOAD, NOT THE EVENT. The SI item asked for "a new PreToolUse mechanism".
    # PreToolUse is the wrong place: all three of this contract's forbidden_moves are about the
    # shape of the REPLY, which PreToolUse cannot see, and a blocking gate there would add exactly
    # the friction the last two forbidden_moves prohibit — making the measured problem worse.
    # UserPromptSubmit already fires at the right instant with the right information. What was wrong
    # was what it said.
    if halt or no:
        # The genuine stop case — 35 of 232 fires. The original text is correct HERE and kept.
        msg = [
            f"⛔ STOP SIGNAL ({', '.join(kinds)}) — {_U.name()} is halting or redirecting you.",
            "",
            "Before doing ANYTHING else this turn:",
            "  1. STOP the current approach. Do NOT continue prior actions, tool calls, or a plan you were mid-way through.",
            f"  2. Re-read what {_U.name()} ACTUALLY just asked — in his words, not your prior framing.",
            "  3. If you were about to push/build/edit on momentum, halt it. Acknowledge the redirect explicitly.",
            "  4. If you reversed a prior recommendation, NAME the flip.",
            "",
            "If his message ALSO contains the new instruction, that IS the redirect — carry it out. "
            "Do not ask him to re-confirm what he just told you. Ask ONE specific question only if "
            "you genuinely cannot tell what he wants.",
        ]
    else:
        # Frustration with no halt and no explicit-no — 85% of fires, 41:1 GO over STOP.
        msg = [
            f"🔥 FRUSTRATION SIGNAL — {_U.name()} is annoyed. Read this before you reply.",
            "",
            "On this seat, frustration without an explicit halt has meant *act*, not *stop*, by 41 "
            "to 1 across 232 measured fires. He is almost always angry that Core STOPPED, hedged, "
            "or asked again — not that it moved.",
            "",
            "  1. Do NOT halt work he did not ask you to halt, and do NOT insert an approval or "
            "check-in step he did not request. If he has already approved something, it is approved.",
            "  2. Do NOT re-litigate, re-verify, or re-explain a decision he has already made or "
            "overridden. Act on it.",
            "  3. Answer the actual question FIRST, in one sentence, before any context or "
            "play-by-play. If he asked what is going on, say what is going on.",
            "  4. If he is frustrated because something is broken or was missed, own it plainly in "
            "one line — no apology spiral, no re-derivation — then go fix it.",
            "  5. If you reversed a prior recommendation, NAME the flip.",
            "",
            "Only stop if he actually told you to stop. Treating frustration as a halt signal is "
            "what this hook used to do, and it was a measured cause of this exact frustration.",
        ]
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(msg),
        }
    }))


if __name__ == "__main__":
    main()
