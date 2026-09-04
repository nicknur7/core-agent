#!/usr/bin/env python3
"""Prompt-stage hooks must not treat runtime-injected text as something the user said.

WHY THIS EXISTS (2026-08-18, measured by core-finance, reproduced on life).

UserPromptSubmit fires for far more than the user's own messages. finance walked its session:

    real user prompts                                 8
    task-notifications (peer bus events)            366
    directive injections                             36
    fired when the triggering turn was a NOTIFICATION 35   -> 97% FALSE-FIRE

Life's live transcript: **217 of 301 user-type records (72%) begin `<task-notification>`.**

Two of the injections are not suggestions. `brain-recall-trigger` emits *"Your FIRST tool call MUST
be a grep across those for the topic he's referencing"* — a MANDATE on a premise that is false by
construction on a notification turn. The wasted call is not the hazard; a MUST-directive with no
valid referent creates pressure to invent one, which is the shape of every failure this fleet
corrected that day.

THE GUARD EXISTED AND MISSED THE TAG THAT DOMINATES THE TRAFFIC. `HOOK_PREFIXES` was declared FOUR
times, byte-identical (sha a69e7ba31ca1), and none of the four listed `<task-notification>`. Four
copies of one constant, all equally out of date, is what that duplication costs — so this is one
definition in `_prompt_source.py`, not a fifth copy.

Verified against pre-patch behaviour: `verification-trigger` and `brain-recall-trigger` both FIRED on
a bus notification before the guard and are silent after. `stop-signal-gate` did not fire on that
particular text — its guard is preventive, not corrective, and this test does not claim otherwise.

WHAT THIS ASSERTS.
  1. The helper classifies machine prefixes as not-user and ordinary text as user.
  2. It fails toward FIRING — anything unrecognised is treated as the user speaking, because
     silently suppressing a real turn is the unrecoverable direction.
  3. No hook re-declares the prefix list. A second copy is how the four drifted out of date together.
  4. Every prompt-stage hook that injects consults it.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".claude" / "hooks"
sys.path.insert(0, str(HOOKS))

from _prompt_source import MACHINE_PREFIXES, is_user_text  # noqa: E402

checks = 0

assert "<task-notification>" in MACHINE_PREFIXES, \
    "the tag that is 72% of prompt-stage traffic is not in the list"
checks += 1

for machine in ("<task-notification>\n<task-id>x</task-id>",
                "[SYSTEM NOTIFICATION - NOT USER INPUT]\nbackground event",
                "  \n\t<system-reminder>x</system-reminder>",
                "<local-command-stdout>ok"):
    assert not is_user_text(machine), "classified as user text: %r" % machine[:40]
checks += 1

for human in ("fix the retention setting",
              "I told you about this yesterday",
              "he said <task-notification> was firing",  # quoting a tag is not being one
              "no stop, that is not what I asked for"):
    assert is_user_text(human), "real user text classified as machine: %r" % human
checks += 1

# Fails toward firing on anything unrecognised.
assert is_user_text("...unrecognised leading punctuation"), "unrecognised text must default to user"
checks += 1
# ...but empty / non-string is not a user turn either way, and no hook should act on it.
assert not is_user_text("") and not is_user_text(None) and not is_user_text(b"x")
checks += 1

# No hook re-declares the list.
copies = [p.name for p in HOOKS.glob("*.py")
          if p.name != "_prompt_source.py" and re.search(r"^\s*(HOOK_PREFIXES|MACHINE_PREFIXES)\s*=", p.read_text(), re.M)]
assert not copies, "prefix list re-declared in: %s — one definition, or they drift apart again" % copies
checks += 1

# Every injecting prompt-stage hook consults it.
REQUIRED = ["verification-trigger", "brain-recall-trigger", "stop-signal-gate",
            "learned-classifier", "learned-stopguard", "learned-recallguard", "learned-validator"]
missing = [h for h in REQUIRED if "_prompt_source" not in (HOOKS / (h + ".py")).read_text()]
assert not missing, "prompt-stage hook(s) not consulting the guard: %s" % missing
checks += 1

# End to end: the two that demonstrably fired must now be silent on a notification and live on a prompt.
#
# RUN AGAINST A SANDBOX INSTANCE, NOT THIS SEAT. The first version of this test pointed the hooks at
# the live repo, `brain-recall-trigger` fired on the REAL prompt exactly as intended, and wrote
# `.claude/state/.recall-required-unknown` into the running Core — a blocking marker, in live state,
# from a test. run-all.sh's leak detector caught it. A test that must exercise a hook's firing path
# has to give that hook somewhere else to write, and `CORE_INSTANCE` is the documented knob
# (brain-recall-trigger.py:44).
import os
import shutil
import tempfile

SANDBOX = Path(tempfile.mkdtemp(prefix="prompt-hook-sandbox-"))
(SANDBOX / ".claude" / "state").mkdir(parents=True)
(SANDBOX / ".claude" / "hooks").mkdir(parents=True)
for aux in ("verification-triggers.json",):
    if (HOOKS / aux).exists():
        shutil.copy(HOOKS / aux, SANDBOX / ".claude" / "hooks" / aux)
for d in ("memory/relationships", "memory/projects"):
    (SANDBOX / d).mkdir(parents=True, exist_ok=True)
env = dict(os.environ, CORE_INSTANCE=str(SANDBOX), CLAUDE_PROJECT_DIR=str(SANDBOX))

NOTIF = ('{"prompt":"<task-notification>\\n<event>BUS: I told you the plan is wrong, verify the file</event>"}')
REAL = '{"prompt":"I told you about the retention setting yesterday — is that file still right?"}'
try:
    for h in ("verification-trigger", "brain-recall-trigger"):
        quiet = subprocess.run([sys.executable, str(HOOKS / (h + ".py"))], input=NOTIF,
                               capture_output=True, text=True, cwd=str(SANDBOX), env=env)
        assert not quiet.stdout.strip(), "%s still injects on a notification turn: %s" % (h, quiet.stdout[:120])
        loud = subprocess.run([sys.executable, str(HOOKS / (h + ".py"))], input=REAL,
                              capture_output=True, text=True, cwd=str(SANDBOX), env=env)
        assert loud.stdout.strip(), "%s stopped firing on a REAL user prompt — the unrecoverable direction" % h
    # ...and prove the sandbox was doing real work: the firing path wrote there, not here.
    assert any(SANDBOX.joinpath(".claude", "state").iterdir()), (
        "no state written to the sandbox — CORE_INSTANCE was ignored, so this test would have "
        "written into the live seat again and its silence proves nothing")
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)
checks += 1

print("ok — %d checks: %d machine prefixes, one definition, %d hooks wired"
      % (checks, len(MACHINE_PREFIXES), len(REQUIRED)))
