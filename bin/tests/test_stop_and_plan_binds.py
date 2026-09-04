#!/usr/bin/env python3
"""stop-and-plan is BINDING now — and binds only on a halt, never on frustration.

The SI loop carried this as its red item for 70 days: *"learned contract 'stop-and-plan' fires but
its correction keeps recurring — NOT BINDING"*. The cause was structural: `stop-signal-gate.py`
lives at UserPromptSubmit, which can inject but cannot refuse the tool call that follows it. The
item's remedy names the fix — *"wire the binding gate (recall-first-gate) for this class"*.

The wiring reuses the marker/enforce/satisfy triple already shipped for recall-first:

    stop-signal-gate.py   UserPromptSubmit   writes .stop-plan-required-<sid>  (HALT / EXPLICIT-NO only)
    recall-first-gate.py  PreToolUse         refuses Write|Edit|MultiEdit|NotebookEdit while it exists
    recall-satisfied.py   PostToolUse        clears it on ANY read

WHY THE HALT-ONLY RESTRICTION IS THE LOAD-BEARING PART. This hook's own telemetry — 232 fires
across 48 sessions — is 85% frustration-only, and those run **41 GO-shaped to 1 STOP-shaped**
("I APPROVE BRUH DO IT", "WHYYYYYYYY DO YOU KEEP STOPPPING"). A gate that treated frustration as a
halt would block the operator's own instruction to act, converting a nagging item into a session-wrecker.
So the teeth are granted strictly to halt / explicit-no, and this test's most important assertion
is the negative one.

Run: python3 bin/tests/test_stop_and_plan_binds.py
"""
import atexit
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
HOOKS = ROOT / ".claude" / "hooks"

# TEMP SEAT, NOT THE LIVE ONE. stop-signal-gate.py resolves its STATE_DIR from CORE_INSTANCE
# (falling back to its own repo root, never the caller's cwd), and recall-first-gate.py /
# recall-satisfied.py fall back the same way through CLAUDE_PROJECT_DIR too — so an unredirected
# subprocess call writes real markers AND telemetry (.stop-plan-required-<sid>,
# .stop-signal-<sid>.json) straight into the live .claude/state. Measured: a run of this test with
# no env override left `.claude/state/.stop-signal-test-stop-and-plan-binds.json` behind on the
# live seat. None of the three hooks under test need any other repo file (identity.json is read by
# coreuser.py anchored to ITS OWN __file__, not INSTANCE, so the display name is unaffected) — only
# STATE_DIR, so a bare `.claude/state/` directory is a complete seat for this test's purposes.
SANDBOX = Path(tempfile.mkdtemp(prefix="core-test-stop-and-plan-"))
(SANDBOX / ".claude" / "state").mkdir(parents=True)
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)

STATE = SANDBOX / ".claude" / "state"
SID = "test-stop-and-plan-binds"
MARKER = STATE / f".stop-plan-required-{SID}"
# Per-Core, not /tmp/x. bin/tests/ ships to every seat, so a bare shared path is a cross-Core
# collision waiting to happen — test_no_cross_core_paths.py refuses it, and it is right to:
# two seats running the suite at once would be writing the same name. Never actually written
# here (it is a dummy tool_input), but "it happens not to be written yet" is not isolation.
DUMMY = str(STATE / f".{SID}-dummy-target")

_passed = 0
_failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def run(script, payload):
    import os
    env = {**os.environ, "CORE_INSTANCE": str(SANDBOX), "CLAUDE_PROJECT_DIR": str(SANDBOX)}
    return subprocess.run([sys.executable, str(HOOKS / script)],
                          input=json.dumps(payload), capture_output=True,
                          text=True, cwd=str(ROOT), env=env, timeout=30)


def prompt(text):
    return run("stop-signal-gate.py",
               {"prompt": text, "session_id": SID, "hook_event_name": "UserPromptSubmit"})


def try_write():
    return run("recall-first-gate.py",
               {"session_id": SID, "tool_name": "Write",
                "tool_input": {"file_path": DUMMY, "content": "y"},
                "hook_event_name": "PreToolUse"})


def do_read():
    return run("recall-satisfied.py",
               {"session_id": SID, "tool_name": "Read",
                "tool_input": {"file_path": DUMMY},
                "hook_event_name": "PostToolUse"})


MARKER.unlink(missing_ok=True)

print("=== a HALT makes the next mutation refuse ===")
prompt("hold up what is this")
check("marker written on halt", MARKER.exists())
r = try_write()
check("Write is REFUSED (exit 2)", r.returncode == 2, f"rc={r.returncode}")
check("...and the refusal explains itself", "STOP-AND-PLAN GATE" in r.stderr)
check("...and quotes what Nick actually said", "hold up" in r.stderr)

print()
print("=== a read clears it, and work resumes ===")
do_read()
check("marker cleared by a plain Read", not MARKER.exists())
r = try_write()
check("Write now passes (exit 0)", r.returncode == 0, f"rc={r.returncode}")

print()
print("=== an EXPLICIT NO also binds ===")
MARKER.unlink(missing_ok=True)
prompt("no bruh that's not what i asked")
check("marker written on explicit-no", MARKER.exists())
check("Write refused", try_write().returncode == 2)

print()
print("=== THE CRITICAL NEGATIVE: frustration alone must NEVER bind ===")
for text in ["I APPROVE BRUH DO IT",
             "WHYYYYYYYY DO YOU KEEP STOPPPING I APPROVE EVERYTHING KEEP GOING",
             "GO FIX IT ON ALL CORES NOWWWW",
             "ok why are you not doing anythinggggggggg why do you stop",
             "Bro wtf you should be able to run this"]:
    MARKER.unlink(missing_ok=True)
    prompt(text)
    no_marker = not MARKER.exists()
    passes = try_write().returncode == 0
    check(f"does not block: {text[:44]!r}", no_marker and passes,
          f"marker={MARKER.exists()}")

print()
print("=== the marker cannot outlive its turn ===")
MARKER.unlink(missing_ok=True)
prompt("hold up what is this")
check("armed by the halt", MARKER.exists())
prompt("ok now do the thing")
check("a later ordinary prompt disarms it", not MARKER.exists())

print()
print("=== a machine turn must not disarm it mid-turn ===")
MARKER.unlink(missing_ok=True)
prompt("hold up what is this")
prompt("<task-notification><task-id>abc</task-id><summary>Monitor event</summary></task-notification>")
check("task-notification leaves the halt armed", MARKER.exists())

MARKER.unlink(missing_ok=True)
print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
