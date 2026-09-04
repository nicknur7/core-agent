#!/usr/bin/env python3
"""PreToolUse gate — recall-first proactive enforcement (contract-binding
proposal fix 1, live 2026-06-09, Nick-approved no-shadow).

When brain-recall-trigger.py matched the current prompt, it wrote
.claude/state/.recall-required-<session_id>. While that marker exists, mutating
tools (Write/Edit/MultiEdit/NotebookEdit) are refused: the turn's FIRST move
after a recall trigger must be a read/recall, not a mutation. The marker is
cleared by recall-satisfied.py (PostToolUse on read-shaped tools) or by the
next non-trigger prompt.

This moves recall-first enforcement from "catch the bad answer at Stop"
(recall-gate.py) to "force the right first move" — the proposal's escalation.

Fail-open: any error → exit 0. Kill-switch: LEARNED_LAYER=0.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # user name from identity.json, never hardcoded
import json
import os
import sys
from pathlib import Path

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STATE_DIR = INSTANCE / ".claude" / "state"


def main() -> int:
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("recall-first-gate", "PreToolUse")
    except Exception:
        pass
    if os.environ.get("LEARNED_LAYER") == "0":
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    session_id = data.get("session_id") or data.get("sessionId") or ""
    if not session_id:
        return 0
    tool = data.get("tool_name") or ""

    # ── SECOND CLASS ON THE SAME GATE: stop-and-plan (2026-08-25). ──────────────────────────────
    #
    # The SI loop carried "learned contract 'stop-and-plan' fires but its correction keeps
    # recurring — NOT BINDING" as a red item, and its remedy names this hook: "wire the binding
    # gate (recall-first-gate) for this class". stop-signal-gate.py detects the halt at
    # UserPromptSubmit but can only inject there — it cannot refuse the Write that follows.
    #
    # It now writes .stop-plan-required-<sid> on HALT or EXPLICIT-NO only, and this gate — already
    # scoped by its registry matcher to Write|Edit|MultiEdit|NotebookEdit — refuses the mutation.
    # recall-satisfied.py clears the marker on any read, because what this contract asks for is
    # re-grounding ("re-read what Nick ACTUALLY just asked"), which a local read satisfies. That is
    # deliberately a weaker clear than the brain-query one recall-first demands below: the two
    # classes want different evidence, and requiring a brain query to recover from "no, stop" would
    # be friction the contract itself forbids.
    #
    # NOT ON FRUSTRATION. 85% of stop-signal fires are frustration-only and run 41:1 toward "go";
    # binding those would refuse Nick's own instruction to act.
    plan_marker = STATE_DIR / f".stop-plan-required-{session_id}"
    if plan_marker.exists():
        try:
            snippet = json.loads(plan_marker.read_text()).get("snippet", "")
        except Exception:
            snippet = ""
        sys.stderr.write(
            f"STOP-AND-PLAN GATE — {_U.name()} issued a halt or an explicit no this turn, and {tool} is "
            "the first thing you tried to do about it. Refused.\n"
            + (f'  He said: "{snippet.strip()[:140]}"\n' if snippet.strip() else "")
            + "  Before mutating anything: re-read what he ACTUALLY asked, in his words, not your "
              "prior framing — then say what you are about to do.\n"
            "  ANY read (Read/Grep/Glob/Bash) clears this. It fires once per halt, and it does NOT "
            "fire on frustration alone — if he is telling you to GO, this will not be in your way.\n"
            "  (Marker: .claude/state/" + plan_marker.name + " — written by stop-signal-gate.py.)\n"
        )
        return 2

    marker = STATE_DIR / f".recall-required-{session_id}"
    if not marker.exists():
        return 0
    sys.stderr.write(
        "RECALL-FIRST GATE — this prompt matched a brain-recall trigger, and no BRAIN query "
        f"has run yet this turn, so {tool} is refused. First move must be an actual BRAIN query: "
        "mcp__core-brain__recall_similar / recall_at / get_entity, the `claude-brain` or "
        "`recall-similar` skill, or a Bash brain query (query.py / graphify / corebrain). "
        "A local Read/Grep/Glob does NOT clear this — only a genuine brain query does "
        "(recall-satisfied.py, 2026-06-26: the brain is ground truth; local files drift). "
        "(Marker: .claude/state/" + marker.name + " — written by brain-recall-trigger.py.)\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
