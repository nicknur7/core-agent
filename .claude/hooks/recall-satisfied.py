#!/usr/bin/env python3
"""PostToolUse — clears the recall-required marker once a BRAIN read ran.

Counterpart of recall-first-gate.py (contract-binding fix 1, 2026-06-09).
**2026-06-26 (memory-system overhaul Phase 3):** the marker now clears ONLY when
the satisfying tool was an actual brain query — not any local read. The original
"any read-shaped tool clears it" let a `Read("memory/current-state.md")` satisfy
recall-first exactly like a brain query, so the gate never actually required the
brain (root cause of leaning on stale local files). Brain reads =
`mcp__core-brain__*`, the `claude-brain` / `recall-similar` skills, or a Bash brain
query (query.py / graphify / claude-brain). Local Read/Grep/Glob no longer clear it.

Fail-open: any error → exit 0.
"""
import json
import os
import sys
from pathlib import Path

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STATE_DIR = INSTANCE / ".claude" / "state"


def is_brain_read(tool_name: str, tool_input: dict) -> bool:
    """True only if this tool call actually consulted the brain."""
    if tool_name.startswith("mcp__core-brain__"):
        return True
    if tool_name == "Skill":
        skill = str(tool_input.get("skill", "")).strip().lower()
        if skill in ("claude-brain", "recall-similar"):
            return True
        # /recall-similar may arrive as the command/name field
        if "claude-brain" in skill or "recall-similar" in skill:
            return True
    if tool_name in ("Bash", "BashOutput"):
        cmd = str(tool_input.get("command", "")).lower()
        if any(k in cmd for k in ("query.py", "graphify", "claude-brain", "recall_similar", "brain-pg/", "corebrain")):
            return True
    return False



def _query_text(tool_name: str, tool_input: dict) -> str:
    """Everything the caller actually ASKED for, flattened. Content, not namespace."""
    parts = []
    for k in ("query", "name", "entity", "term", "q", "text", "pattern", "args", "skill", "command"):
        v = tool_input.get(k)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts).lower()


def _addresses_demand(qtext: str, subjects: list) -> bool:
    """Does this query plausibly answer the demand that raised the marker?

    THE DEFECT THIS CLOSES. is_brain_read() decided on the tool NAMESPACE alone — so a brain query
    about ANYTHING cleared a recall demand about SOMETHING ELSE, and because this hook never
    receives the tool RESULT, a zero-hit query cleared it identically to a real hit. The rule Nick
    has corrected us on more than any other was satisfiable by querying the brain for nothing.

    Binding is by SUBJECT, and only when the marker recorded one. A marker-only fire (past-tense
    phrasing, no named person or project) has no subject, so any brain read still clears it — the
    old behaviour, kept deliberately rather than inventing a constraint the demand never carried.
    Same asymmetry as the approval-token binding: refuse when there IS something to check and it
    does not match; never refuse for want of data.
    """
    if not subjects:
        return True
    ql = qtext or ""
    for s in subjects:
        s = str(s).strip().lower()
        if not s:
            continue
        if s in ql:
            return True
        # a multi-word subject counts if its distinctive head word is asked for
        head = max(s.split(), key=len, default="")
        if len(head) >= 5 and head in ql:
            return True
        # A FILENAME SUBJECT ALSO MATCHES ITS STEM. Primitive-change demands record the filename
        # (pretooluse-guard.sh), and the natural way to ask the brain about it is by name without
        # the extension. Requiring the extension refused "history of pretooluse-guard" — a query
        # plainly about the right thing.
        #
        # core-business's warning when it proposed widening this gate: the version that gets muted
        # is the one that blocks correct work, and it cited the WebFetch precedent — 85
        # approve-then-rerun blocks with 0 real catches. A false refusal here costs more than the
        # miss it prevents, because it teaches you to stop reading the demand.
        if "." in s:
            stem = s.rsplit(".", 1)[0]
            if len(stem) >= 5 and stem in ql:
                return True
    return False


def main() -> int:
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("recall-satisfied", "PostToolUse")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    session_id = data.get("session_id") or data.get("sessionId") or ""
    if not session_id:
        return 0
    tool_name = data.get("tool_name") or data.get("toolName") or ""
    tool_input = data.get("tool_input") or data.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    # ── stop-and-plan: ANY read clears it (2026-08-25). ─────────────────────────────────────────
    #
    # Cleared before the brain-read test below, and on purpose. The stop-and-plan contract asks for
    # RE-GROUNDING — "re-read what Nick ACTUALLY just asked, in his words" — which a local
    # Read/Grep/Glob satisfies completely. Demanding a brain query to recover from "no, stop" would
    # add exactly the friction two of that contract's three forbidden_moves prohibit, and this hook's
    # own matcher already restricts it to read-shaped tools, so reaching here IS the evidence.
    #
    # The asymmetry with recall-first below is intentional, not an oversight: that class is about
    # stale local files, so only the brain clears it; this class is about having misread Nick, so
    # reading him again clears it.
    try:
        plan_marker = STATE_DIR / f".stop-plan-required-{session_id}"
        if plan_marker.exists():
            plan_marker.unlink()
    except Exception:
        pass

    # Only a genuine brain query clears the recall-required marker.
    if not is_brain_read(tool_name, tool_input):
        return 0
    marker = STATE_DIR / f".recall-required-{session_id}"
    try:
        if marker.exists():
            subjects = []
            try:
                _raw = marker.read_text().strip()
                if _raw.startswith("{"):
                    subjects = json.loads(_raw).get("subjects") or []
                # a legacy bare-timestamp marker yields no subjects -> unchanged behaviour,
                # so this ships without wedging any in-flight session
            except Exception:
                subjects = []
            if _addresses_demand(_query_text(tool_name, tool_input), subjects):
                marker.unlink()
            else:
                # The demand stands. Record the near-miss so the gap is measurable rather than
                # silent — a refusal nobody can count is the same defect one level up.
                try:
                    with (STATE_DIR / ".recall-mismatch.log").open("a") as fh:
                        fh.write(json.dumps({"session": session_id, "tool": tool_name,
                                             "wanted": subjects,
                                             "asked": _query_text(tool_name, tool_input)[:200]}) + "\n")
                except Exception:
                    pass
                return 0
    except Exception:
        pass
    # Breadcrumb for recall-before-grep.py: a genuine brain query ran this session,
    # so session-history grep is now unlocked as the sanctioned fallback (Phase 2).
    try:
        (STATE_DIR / f".brain-queried-{session_id}").touch()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
