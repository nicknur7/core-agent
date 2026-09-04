#!/usr/bin/env python3
"""Thin hook wrapper — runs the friction watchdog sweep at SessionStart (quarantines any
misbehaving artifact + restores the prior snapshot). Fail-safe: any error is swallowed."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ.get("CLAUDE_PROJECT_DIR")
                            or Path(__file__).resolve().parents[2]) / "scheduling" / "claude-si"))
try:
    import friction_watchdog as wd
    wd.sweep(dry=False)
except Exception:
    pass
sys.exit(0)
