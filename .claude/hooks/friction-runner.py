#!/usr/bin/env python3
"""Thin hook wrapper — delegates to scheduling/claude-si/friction_runner.py, the ONE place a
run_action's script is ever spawned. Registered on UserPromptSubmit ONLY (see that module's
docstring for why PreToolUse is out of scope in v1). Fail-open: a broken runner must NEVER block,
and this file always exits 0 regardless of what friction_runner.run() itself returns, mirroring
friction-dispatch.py's own wrapper."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ.get("CLAUDE_PROJECT_DIR")
                            or Path(__file__).resolve().parents[2]) / "scheduling" / "claude-si"))

# CANONICAL-LOG VISIBILITY — same reasoning as friction-dispatch.py's own wrapper (2026-08-28,
# core-business bus #5571): record the invocation BEFORE the import, so a runner that fails to
# import is still visible in hook-events.log rather than vanishing indistinguishably from "never
# registered at all".
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
    import hooklog as _hl
    _hl.invoked("friction-runner", "UserPromptSubmit")
except Exception:
    pass

try:
    import friction_runner as fr
    fr.run()
except Exception:
    pass
sys.exit(0)  # ALWAYS 0 — an action's own failure is handled (and quarantined) inside fr.run()
