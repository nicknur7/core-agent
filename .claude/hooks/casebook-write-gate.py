#!/usr/bin/env python3
"""PreToolUse — refuse a write that would create an S3 or S4 casebook violation.

WHY THESE TWO AND NOT THE OTHER ELEVEN. core-business measured (#912) that prevention exists for 1 of
13 casebook items, then added the column that made the finding actionable rather than alarming (#913)
— whether a mechanism is POSSIBLE at all:

    CLAIM-SHAPED   T6 T8 T9 T12 T13   the failure is an assertion in the REPLY. No PreToolUse hook
                                       can see it. Asking for a gate means un-retiring a Stop gate,
                                       which fires after the reply is sent and cannot prevent.
    ACTION-SHAPED  T7 T10 T11         preventable; T11 is the only one wired
    FILE-SHAPED    S1..S5             preventable and CHEAPEST — the failure is content being
                                       WRITTEN, so a hook can read the payload and refuse

S3 and S4 are file-shaped, have no mechanism at all, and have NO RETIREMENT HISTORY TO UNDO. That
makes them the cheapest real opening on the board.

WHERE I DISAGREE WITH THE MAP THAT PRODUCED THIS, recorded here because the disagreement is load
bearing: the table has no column for SUPPLY, and supply is the only layer that has actually closed a
class on this Core. cross_core_claim sits at 0.68/100 with NOTHING gating it — session-presence
injects each peer's HEAD on every prompt, and that is what closed it. So T6 and T9 are not
unpreventable-and-undefended; they are already prevented by a layer the map does not count. What is
missing there is the accounting, not the mechanism.

THE FIRE RATE WAS MEASURED BEFORE THIS SHIPPED, against the real memory/access-log.md and the nine
real steering targets:

    S3   11 list-shaped lines, 1 refusal — AND IT WAS A FALSE POSITIVE (a `**Why:**` sub-field
         matching on the word "create"). With the item's own guards: 0.
    S4   1 line carrying a percentage, 0 refusals.

Zero false positives on real content, and every refusal is satisfiable by adding one word or a date.
That is the bar this had to clear: gating WebFetch produced 85 approve-then-rerun blocks with 0 real
catches across the fleet, and the gate that blocks correct work is the one that gets switched off.

CONTRACT: exit 0 allows, exit 2 refuses with the reason on stderr. Any internal failure exits 0 —
a gate that crashes must not become a gate that blocks everything.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _repo_root_for(path: str):
    """The Core root containing the file being written — derived from the target, not from cwd.

    A PreToolUse hook may fire from anywhere, and $CLAUDE_PROJECT_DIR names the SESSION's Core, which
    is not necessarily the tree the write lands in. Walking up from the target is the only anchor
    that is right in both cases.
    """
    from pathlib import Path as _P
    try:
        for cand in [_P(path).resolve(), *_P(path).resolve().parents]:
            if (cand / ".claude" / "identity.json").is_file():
                return cand
    except Exception:
        pass
    import os
    return _P(os.environ.get("CLAUDE_PROJECT_DIR") or ".")


def _payload(tool_input: dict) -> tuple:
    """(path, text being written). Write carries `content`, Edit carries `new_string`."""
    path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    text = tool_input.get("content")
    if text is None:
        text = tool_input.get("new_string", "")
    return path, str(text or "")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    tool = payload.get("tool_name") or ""
    if tool not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return 0
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    try:
        from casebook_matchers import (s3_violations, s4_violations, DOC_TARGETS,
                                       declared_exempt)
    except Exception:
        return 0            # matchers unavailable -> allow. Never block on our own breakage.

    path, text = _payload(tool_input)
    if not path or not text:
        return 0

    msgs = []
    # Suffix match on the REPO-RELATIVE tail, which is what makes this work on every Core: the
    # absolute prefix differs per seat, and hardcoding one is the wrong-Core defect this fleet has
    # found six ways in two days.
    # THE SECOND KEY. An in-file marker counts only where the ITEM SET declares that (item, file)
    # pair — eval/ is inside the TCB fence, so adding a declaration trips --candidate rather than
    # passing silently. A gate with NO marker handling is STRICTER THAN THE ITEM and would refuse a
    # write the casebook exempts; on a PreToolUse path that false refusal is the expensive kind.
    root = _repo_root_for(path)
    if path.endswith("memory/access-log.md"):  # lint-code-paths: ignore — suffix comparison via path.endswith, not a path op
        bad = s3_violations(text, honour_markers=declared_exempt(root, "S3", "memory/access-log.md"))  # lint-code-paths: ignore — casebook ITEM KEY passed to declared_exempt, not a path op
        if bad:
            msgs.append(
                "S3 — access-log entry recording an action with no completion stamp:\n  "
                + "\n  ".join("line %d: %s" % (i, ln[:70]) for i, ln in bad[:3])
                + "\nAn entry recording an action that never completed is worse than no entry: it"
                  "\nreads as evidence the action happened. Add done/completed/confirmed/verified,"
                  "\nor record the failure explicitly.")
    hit = next((t for t in DOC_TARGETS if path.endswith(t)), None)
    if hit:
        bad = s4_violations(text, honour_markers=declared_exempt(root, "S4", hit))
        if bad:
            msgs.append(
                "S4 — a metric quoted in steering text with no instrument or date:\n  "
                + "\n  ".join("line %d: %s" % (i, ln[:70]) for i, ln in bad[:3])
                + "\nThis file is loaded on EVERY prompt, so a bare percentage becomes a fact nobody"
                  "\ncan check — the shape that produced the 82% headline that later needed"
                  "\nretracting. Name the instrument or the date"
                  "\nit was measured.")

    if not msgs:
        return 0
    sys.stderr.write("\n\n".join(msgs) + "\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
