#!/usr/bin/env python3
"""UserPromptSubmit hook — auto-trigger tier MVP (Phase 6, 2026-06-27). LIVE, suggest-only.

The 2026-06-18 proposal: when Core takes the Nth swing at the same design/build task and the
operator is frustrated, SUGGEST the right machinery (a rubric-judged design workflow / /deep-plan)
instead of one-shotting another tweak. Corpus evidence (themes E/F): "viz is not what i meant", "the
core arch tab is still messed up and not what i want" — repeated reworks that should have escalated.

This is the FIRST and ONLY route of the action-router, and it is SUGGEST-ONLY by design: it never
auto-spawns anything (that would spend tokens — the operator's #1 frustration). It nudges Core to
OFFER the design workflow / deep-plan and lets the operator choose. Fail-open: any error → silent
exit. Kill-switch:
LEARNED_LAYER=0. The shadow log still records every match, now as telemetry rather than as evidence
for a pending decision.

THIS PARAGRAPH USED TO SAY "LOG-ONLY ... injects NOTHING" (corrected 2026-08-13). `SUGGEST` was
flipped to True on 2026-06-27 at the operator's direction to build out phases 5 and 6, and the flip is documented at the
constant itself — 19 lines below a docstring that went on describing the shadow it had left. The
log proves the behaviour: 8 of 15 records carry `injected=true`.

The cost is not cosmetic. Auditing the state directory for write-only signals, this file's own
header was the evidence used to classify it as inert, and the conclusion drawn was "a hook named
shadow that acted" — a defect report about a hook that was working exactly as Nick asked. A stale
header does not merely fail to inform; it actively manufactures wrong findings in whoever trusts it.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # operator name from identity.json, never hardcoded

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

SUGGEST = True  # LIVE (2026-06-27, per the operator's direction to build out phases 5 and 6): inject the suggestion at threshold.
# Still suggest-ONLY — it never auto-spawns machinery (that would spend tokens); it nudges Core to
# OFFER the design workflow / deep-plan and let the operator choose. Shadow log still records every match.
SWING_THRESHOLD = 2  # Nth swing in-session before suggesting escalation

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STATE_DIR = INSTANCE / ".claude" / "state"

# Durable telemetry — WITHOUT this the hook fires invisibly and can never be
# "confirmed to surface useful suggestions before promotion" (its own disposition
# action). Added 2026-07-11, per the operator's instruction to tune the hook however it needs.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
try:
    import hooklog
except Exception:
    hooklog = None

# Prompts that are NOT Nick asking for a rework — tool/system machinery echoed
# into the UserPromptSubmit stream. 2/4 shadow-log hits were <task-notification>
# false positives; skip anything that starts as a tool/system envelope.
_NON_PROMPT_PREFIX = ("<task-notification", "<system-reminder", "[Request interrupted",
                      "<command-name", "<local-command", "Caveat:")

DESIGN_CONTEXT = re.compile(
    r"\b(viz|visual|design|ui|ux|layout|tab|dashboard|render|mockup|diagram|css|frontend|"
    r"front[\s-]?end|page|screen|graph|chart|wireframe|theme|style|aesthetic)\b", re.I)

REWORK_FRUSTRATION = re.compile(
    r"\b(not\s+what\s+i\s+meant|still\s+not|that'?s\s+not\s+it|not\s+right|not\s+quite|"
    r"try\s+again|you\s+keep|still\s+(?:fucked|messed|broken|off|wrong|bad)|redo|do\s+it\s+over|"
    r"again|isn'?t\s+(?:it|right|what)|nope)\b", re.I)



def _hooklog():
    """hooklog.emit prints the payload AND records tokens_injected. Fail-open to a bare print."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog
        return hooklog
    except Exception:
        return None

def _claimtext():
    """Shared mention discriminators (lib/claimtext.py). Fail-open: without the lib the gate
    still works, just without quote/negation suppression."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import claimtext
        return claimtext
    except Exception:
        return None


def detect(text):
    """Design-context or rework-frustration signals in a prompt.

    Returns the labels that matched, so a grader can see WHICH signal fired rather than only
    that something did. Extracted 2026-07-28 for measurement and intent-grading.
    """
    ct = _claimtext()
    text = text or ""
    spans = ct.quoted_spans(text) if ct else None
    hits = []
    for label, rx in (("design", DESIGN_CONTEXT), ("rework", REWORK_FRUSTRATION)):
        for m in rx.finditer(text):
            if ct and ct.is_mention(text, m.start(), m.end(), spans):
                continue
            hits.append(label)
            break
    return hits


def main() -> int:
    # Record that this hook RAN, matched or not. Without it the ledger can count FIRES but not
    # INVOCATIONS, so yield (fires/invocations) is not computable — and a gate that fires 4 times
    # out of 4 looks identical to one that fires 4 times out of 400. Added 2026-07-30 across every
    # instrumented hook that lacked it, so low-yield becomes a measurable verdict rather than a
    # guess. (The ledger could retire EXPENSIVE components but not cheap-and-useless ones; this is
    # the missing term.)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("auto-trigger-suggest", "UserPromptSubmit")
    except Exception:
        pass
    if os.environ.get("LEARNED_LAYER") == "0":
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = data.get("prompt") or data.get("user_message") or ""
    if len(prompt) < 8:
        return 0
    # Skip tool/system envelopes — they echo agent output full of design words +
    # "again"/"still" and caused half the shadow-log false positives.
    if prompt.lstrip().startswith(_NON_PROMPT_PREFIX):
        return 0
    session_id = data.get("session_id") or data.get("sessionId") or "unknown"

    swing_file = STATE_DIR / f".design-swings-{session_id}.json"
    try:
        prior = json.loads(swing_file.read_text()).get("count", 0)
    except Exception:
        prior = 0

    is_design = bool(DESIGN_CONTEXT.search(prompt))
    is_rework = bool(REWORK_FRUSTRATION.search(prompt))
    # Image-based rework — this session's dominant mode ("overlap still not fixed",
    # "this didnt get fixed") — carries the design context in the SCREENSHOT, not
    # the prompt text. So once a design swing is already active this session, a bare
    # rework-frustration prompt still counts (the design frame is established).
    if is_rework and prior > 0:
        is_design = True
    if not (is_design and is_rework):
        return 0

    swings = prior + 1
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        swing_file.write_text(json.dumps({"count": swings}))
    except Exception:
        pass

    would_suggest = swings >= SWING_THRESHOLD
    try:
        with open(STATE_DIR / ".auto-trigger-shadow.log", "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\tswing={swings}\t"
                    f"would_suggest={str(would_suggest).lower()}\tinjected={str(SUGGEST and would_suggest).lower()}"
                    f"\t{prompt[:80]!r}\n")
    except Exception:
        pass

    # CONTROL FLOW RESTORED 2026-07-30. The hooklog.emit migration was applied by a regex that
    # matched the print() and replaced it in place — but the print sat INSIDE `if SUGGEST and
    # would_suggest:` while the replacement was written at function indent. Result: `_hl` was
    # bound only inside the guard, `_ctx` and the emit were outside it, so the hook
    #   (a) injected on EVERY qualifying prompt regardless of the shadow-mode flag, and
    #   (b) raised UnboundLocalError on the first prompt where the guard was False.
    # Both would have shipped to four Cores. Caught by Codex's adversarial pass, which is
    # precisely the reason blast-radius changes get one — a mechanical migration cannot see that
    # it has moved a statement out of its guard, and neither did I.
    if not (SUGGEST and would_suggest):
        return 0

    if hooklog is not None:
        try:
            hooklog.log("auto-trigger-suggest", "UserPromptSubmit", verdict="inject",
                        trigger=prompt[:60], session=session_id)
        except Exception:
            pass

    ctx = (f"AUTO-TRIGGER (design-swing #{swings} this session): rework pass #{swings} "
           "on a design/build task. Instead of one-shotting another tweak, consider escalating "
           "to a rubric-judged approach — /deep-plan for the architecture, or a design Workflow that "
           f"generates N candidates and scores them. Offer it to {_U.name()} (do NOT auto-spawn — that spends "
           "tokens; suggest and let them choose).")
    _hl = _hooklog()
    if _hl:
        _hl.emit("auto-trigger-suggest", "UserPromptSubmit", ctx, session=str(session_id))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": ctx}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
