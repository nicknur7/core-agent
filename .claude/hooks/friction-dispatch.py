#!/usr/bin/env python3
"""Thin hook wrapper — delegates to scheduling/claude-si/friction_dispatch.py, the ONE static,
human-reviewed interpreter for friction artifacts. Registered once per event (UserPromptSubmit /
PreToolUse / Stop); the event name is argv[1]. Fail-open: a broken dispatcher must NEVER block."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ.get("CLAUDE_PROJECT_DIR")
                            or Path(__file__).resolve().parents[2]) / "scheduling" / "claude-si"))
ev = sys.argv[1] if len(sys.argv) > 1 else "UserPromptSubmit"

# CANONICAL-LOG VISIBILITY (2026-08-28, found by core-business in its self-audit, bus #5571).
# This dispatcher never called hooklog, so `hook-events.log` held ZERO friction-dispatch rows on all
# five seats while the dispatcher was running constantly and logging to its own
# friction-action-log.jsonl (2,741 rows on business, 5,396 on life). Anyone auditing hook activity
# from the canonical log — which is what that log is FOR — concludes the subsystem is dead.
#
# That is not hypothetical: business grepped hook-events.log during this audit, got 0, and concluded
# the dispatcher had never fired on its seat. It caught the error only by then checking whether the
# file calls hooklog at all. Had it not, it would have reported a dead subsystem to me as fact, and I
# would have relayed it to Nick — the exact shape of the four wrong numbers I gave him tonight.
#
# `invoked` is recorded BEFORE the import, so a dispatcher that fails to import is still visible as
# an invocation rather than vanishing. Fail-open is preserved: hooklog itself is wrapped, because a
# logger must never be able to block the hook it observes.
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
    import hooklog as _hl
    _hl.invoked("friction-dispatch", ev)
except Exception:
    pass

try:
    import friction_dispatch as fd
    sys.exit(fd.run(ev))
except Exception:
    sys.exit(0)  # fail open
