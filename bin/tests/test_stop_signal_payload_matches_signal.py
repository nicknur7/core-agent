#!/usr/bin/env python3
"""THE GATE WAS BINDING BACKWARDS, AND ITS OWN TELEMETRY SAID SO FOR 70 DAYS.

`stop-signal-gate.py` fires at UserPromptSubmit on three signal classes — halt, explicit-no,
frustration — and until 2026-08-25 it emitted ONE payload for all three:

    "STOP the current approach. Do NOT continue prior actions, tool calls, or a plan you were
     mid-way through ... If you were about to push/build/edit on momentum, halt it ... ask ONE
     specific question."

The SI queue carried it as a red item: *"learned contract 'stop-and-plan' fires but its correction
keeps recurring — NOT BINDING ... a new PreToolUse mechanism has to be designed."* That diagnosis
was wrong in an instructive way. The gate binds fine. It binds the wrong direction.

MEASURED, from the hook's own `.claude/state/.stop-signal-*.json`, 232 fires across 48 sessions:

    frustration-only (no halt, no explicit-no) .............. 197 / 232   (85%)
    of those carrying a directional marker:
        GO-shaped   ("do it", "keep going", "why are you not") ..... 41
        STOP-shaped ("hold up", "stop doing", "wait") ............... 1

Forty-one to one. Verbatim fires include (profanity redacted here) "I APPROVE BRUH DO IT", "WHYYYYYYYY
DO YOU KEEP STOPPPING I APPROVE EVERYTHING KEEP GOING", and "ok why are you not doing
anythinggggggggg why do you stop".
On every one of those the hook told Core to halt mid-plan and ask a question.

That is not a missing gate. It is a gate MANUFACTURING two of the three forbidden_moves on the very
contract it enforces (learned_contracts id 7, org 1):

    · "Don't hedge, re-check, or re-litigate after Nick has explicitly reauthorized ... — act"
    · "don't insert an unrequested check-in/ping step when the instruction implied autonomous
       completion"

— which is a closed loop: the hook fires on frustration, tells Core to stop and ask, Core stops and
asks, Nick gets more frustrated, the hook fires again. Seventy days of non-graduation is what that
looks like from outside.

WHY NOT PreToolUse, WHICH IS WHAT THE ITEM ASKED FOR. All three forbidden_moves are properties of
the REPLY, which PreToolUse cannot see. Worse, a blocking gate there adds exactly the friction the
last two forbidden_moves prohibit, so it would raise the measured violation rate. The event was
never the problem; the payload was.

This test pins the split so it cannot silently regress back to one payload.

Run: python3 bin/tests/test_stop_signal_payload_matches_signal.py
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
HOOK = ROOT / ".claude" / "hooks" / "stop-signal-gate.py"
SID = "test-payload-split"
# THIS TEST WRITES LIVE STATE AS A SIDE EFFECT AND MUST CLEAN IT UP.
# stop-signal-gate.py now drops `.claude/state/.stop-plan-required-<sid>` on a halt/explicit-no,
# so merely exercising the hook arms the real gate on the real seat. bin/tests/ ships to every Core
# and runs at every close via session-lifecycle.sh, so a leaked marker here would arm stop-and-plan
# on five seats from a test run. test_tests_do_not_write_live_state.py exists for exactly this.
# SANDBOXED, NOT CLEANED UP AFTER. The comment above was right about the danger, and the cleanup it
# describes was incomplete: stop-signal-gate.py writes TWO files — `.stop-plan-required-<sid>`
# (line 139) and `.stop-signal-<sid>.json` (line 155). _clean() knew only about the first, so every
# run left `.claude/state/.stop-signal-test-payload-split.json` behind on the live seat. The brain
# records the original incident ("leaves stray marker file on host"); the fix for it covered one of
# the two outputs.
#
# Measured 2026-09-01: that file was sitting in life's state, written 04:45 during a suite run. It
# survived an audit of five hook-invoking tests, because that audit compared state hashes before and
# after — and a test that REWRITES an existing file with identical content moves no hash. The only
# check that finds it is removing the file and seeing whether it comes back. It did.
#
# Enumerating the files a hook writes is the same losing game as a denylist: the hook grows a third
# output and the cleanup silently stops covering it. Redirecting the hook's whole state dir at a
# temp seat covers whatever it writes, now and later. Its sibling test_stop_and_plan_binds.py was
# fixed the same way in the same pass.
SANDBOX = Path(tempfile.mkdtemp(prefix="core-test-payload-split-"))
(SANDBOX / ".claude" / "state").mkdir(parents=True)
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)
MARKER = SANDBOX / ".claude" / "state" / f".stop-plan-required-{SID}"

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


def fire(prompt):
    """Run the REAL hook and return its injected context, or None if it stayed silent."""
    payload = {"prompt": prompt, "session_id": SID,
               "hook_event_name": "UserPromptSubmit"}
    try:
        # cwd stays ROOT so the hook reads the real repo; only its STATE dir is redirected.
        # stop-signal-gate.py:45 resolves INSTANCE from CORE_INSTANCE and falls back to its own
        # repo root, so passing it is what actually contains the writes. CLAUDE_PROJECT_DIR is set
        # too because other hooks prefer that one and this brief may grow to cover them.
        import os
        _env = dict(os.environ, CORE_INSTANCE=str(SANDBOX), CLAUDE_PROJECT_DIR=str(SANDBOX))
        r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                           capture_output=True, text=True, cwd=str(ROOT), timeout=30,
                           env=_env)
    except Exception as e:
        return f"__ERROR__{e}"
    if not (r.stdout or "").strip():
        return None
    try:
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return None


# Every prompt below has the SHAPE of a recorded fire from the reference seat — all-caps, dragged
# letters, a go-ahead — rewritten as synthetic strings for the public template. Two keep a profane
# token on purpose: it is the sole trigger FRUSTRATION_RX fires on for those rows, and a detector
# cannot be tested without the thing it detects. Nothing here is a verbatim quote of anyone.
FRUSTRATION_ONLY = [
    "I APPROVE BRUH DO IT",
    "WHYYYYYYYY DO YOU KEEP STOPPPING I APPROVE EVERYTHING KEEP GOING AND ADDRESS",
    "GO YES DO IT BRUH THIS IS ANNOYING JUST GO I ALREADY APPROVED",
    "ok why are you not doing anythinggggggggg why do you stop",
    "Bro why are you not running this",
    "oh for fucks sake dont give up just keep going and work with the other seat",
    "GO FIX IT EVERYWHERE NOWWWW AND MAKE SURE YOU DONT FUCK IT UP",
]
HALT_OR_NO = [
    "hold up what is this",
    "no bruh that's not what i asked",
    "wait wait that's wrong",
    "okkkk hold up. so we do want to extract sub agents? why",
]
QUIET = [
    "walk the 15 and correct whats wrong",
    "can you address and clear all si notifications",
    "check in with all the cores tell me how they are doing",
]

print("=== FRUSTRATION-ONLY (85% of fires) gets the ACT payload, never the halt payload ===")
for p in FRUSTRATION_ONLY:
    ctx = fire(p) or ""
    check(f"acts, does not halt: {p[:46]!r}",
          "FRUSTRATION SIGNAL" in ctx and "STOP the current approach" not in ctx,
          f"fired={bool(ctx)}")

print()
print("=== HALT / EXPLICIT-NO still gets the stop payload — the 15% the original text was right for ===")
for p in HALT_OR_NO:
    ctx = fire(p) or ""
    check(f"halts: {p[:46]!r}", "STOP the current approach" in ctx, f"fired={bool(ctx)}")

print()
print("=== high precision preserved: ordinary prompts do not fire at all ===")
for p in QUIET:
    check(f"silent: {p[:46]!r}", fire(p) is None)

print()
print("=== the ACT payload must not reintroduce the moves it used to cause ===")
act = fire("I APPROVE BRUH DO IT") or ""
check("no unrequested check-in step ('ask ONE specific question')",
      "ask ONE specific question" not in act)
check("does not tell Core to halt work Nick did not halt ('halt it')",
      "halt it" not in act)
check("does tell Core to answer the question first",
      "Answer the actual question FIRST" in act)

print()
print("=== both payloads keep the one forbidden_move that applies to every signal ===")
stop = fire("no bruh that's not what i asked") or ""
check("ACT payload names the flip rule", "NAME the flip" in act)
check("STOP payload names the flip rule", "NAME the flip" in stop)

# No _clean() here any more: the sandbox above is removed wholesale by its atexit handler, which
# covers every file the hook writes rather than the one this call used to know about.

print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
