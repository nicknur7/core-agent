#!/usr/bin/env python3
"""The friction dispatcher must not match a prompt regex against runtime-injected text.

WHY THIS EXISTS (2026-08-18) — AND IT IS THE GAP THE FIRST FIX LEFT OPEN.

`_prompt_source.is_user_text` was wired into `learned-classifier` and six sibling hooks. **The same
contracts also reach the prompt through a second path**: `si_project._snapshot_to_spec` translates
each snapshot contract into a `legacy_<key>` inject artifact, and `friction_dispatch.run` fires it on
its own `prompt_regex` leg — which reads `payload["prompt"]` with no user/machine distinction.

Found the way these things should be found: **`verify-dont-claim` injected into life's own context on
a `<task-notification>` turn AFTER the first fix shipped.** Observed in the running seat, not
inferred, and the lowercase message body identified the emitter as `si_project`'s artifact rather
than the classifier.

Scale, from the classifier's log across the fleet: ops 90% of fires on machine text (427/473),
school 76%, business 68%, finance 53%, life 7% — life being the seat with no over-broad induced
triggers. The dispatcher path is additional to all of those numbers, not included in them.

WHAT THIS ASSERTS.
  1. A real prompt still fires its contract — the direction that must never silently break.
  2. The identical words wrapped in a `<task-notification>` fire nothing.
  3. Only prompt-matching is disarmed: `prompt_text` is blanked rather than the run aborted, so
     `event_is` / `tool_name_in` legs and the Stop-side oracles are untouched.
  4. The guard is on the LIVE path, not in `_normalize` — `normalize_for_test` and
     `friction_test_gate`'s specificity measurement both call `_normalize`, and moving the guard
     there would silently rewrite locked test fixtures and the measured fire-rate.

SANDBOXED, BECAUSE THE FIRST RUN OF THIS CHECK WAS NOT. Exercising the dispatcher by hand wrote two
synthetic rows into the live seat's `friction-action-log.jsonl` — a `fire_inject` and a
`dispatch_nofire` — in a ledger four Cores were parsing for fire-rate that same hour. The module's own
comment at :51 records the identical mistake being made once before. Disclosed rather than deleted:
an append-only ledger is not made more honest by editing it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".claude" / "hooks" / "friction-dispatch.py"
checks = 0

SANDBOX = Path(tempfile.mkdtemp(prefix="dispatch-sandbox-"))
try:
    art_dir = SANDBOX / ".claude" / "state" / "friction-artifacts"
    art_dir.mkdir(parents=True)
    # THE SANDBOX MUST CARRY THE IDENTITY MARKER OR IT IS NOT A CORE. `core_seat.seat_root` accepts
    # CORE_INSTANCE only for a directory holding `.claude/identity.json` — deliberately, so that a
    # stray env var cannot point Core at an arbitrary path. Without it the sandbox is silently
    # rejected, INSTANCE falls back to the REPO, and this test exercises the LIVE seat's artifacts
    # while appearing to pass. That is how the first hand-run of this check wrote two synthetic rows
    # into the live action log.
    (SANDBOX / ".claude" / "identity.json").write_text(json.dumps(
        {"core": "sandbox", "org_id": 1, "hook_profile": {"role": "test"}}))
    shutil.copytree(REPO / ".claude" / "hooks", SANDBOX / ".claude" / "hooks",
                    ignore=shutil.ignore_patterns("archive", "__pycache__"))
    (art_dir / "active.json").write_text(json.dumps({"artifacts": [{
        "artifact_id": "test_machine_guard", "spec_version": 1, "case_id": "test",
        "org_id": 1, "type": "contract", "event": "UserPromptSubmit",
        "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                              {"op": "prompt_regex", "value": r"\bpurple armadillo\b"}]},
        "effect": {"mode": "inject", "message": "SENTINEL-STRING-FIRED", "skill_id": None},
        "enforced": False, "active": True,
    }]}))
    # CLAUDE_PROJECT_DIR stays on the REPO so the thin hook wrapper can import the dispatcher
    # module; CORE_INSTANCE points at the sandbox so every state write lands there. That split is
    # the whole point of `_seat_root()` honouring CORE_INSTANCE first (friction_dispatch.py:47-61),
    # and pointing both at the sandbox makes the import fail and the wrapper fail open — which reads
    # as "the guard works" while proving nothing.
    env = dict(os.environ, CORE_INSTANCE=str(SANDBOX), CLAUDE_PROJECT_DIR=str(REPO),
               CORE_ORG_ID="1")

    def dispatch(prompt, sid):
        r = subprocess.run([sys.executable, str(HOOK), "UserPromptSubmit"],
                           input=json.dumps({"prompt": prompt, "session_id": sid}),
                           capture_output=True, text=True, cwd=str(SANDBOX), env=env)
        return r.stdout

    real = dispatch("we need a purple armadillo for this", "s-real")
    assert "SENTINEL-STRING-FIRED" in real, \
        "a REAL prompt no longer fires its contract — the unrecoverable direction: %r" % real[:200]
    checks += 1

    for machine in ("<task-notification>\n<event>BUS: we need a purple armadillo for this</event>",
                    "[SYSTEM NOTIFICATION - NOT USER INPUT]\nwe need a purple armadillo for this",
                    "<system-reminder>we need a purple armadillo for this</system-reminder>"):
        out = dispatch(machine, "s-machine")
        assert "SENTINEL-STRING-FIRED" not in out, \
            "dispatcher fired on runtime-injected text: %r" % machine[:48]
    checks += 1

    # 3) a non-prompt leg is untouched — blanking prompt_text must not disable the rest of the DSL.
    (art_dir / "active.json").write_text(json.dumps({"artifacts": [{
        "artifact_id": "test_event_only", "spec_version": 1, "case_id": "test",
        "org_id": 1, "type": "contract", "event": "UserPromptSubmit",
        "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"}]},
        "effect": {"mode": "inject", "message": "EVENT-ONLY-FIRED", "skill_id": None},
        "enforced": False, "active": True,
    }]}))
    out = dispatch("<task-notification>\n<event>anything</event>", "s-event")
    assert "EVENT-ONLY-FIRED" in out, \
        "an artifact with no prompt_regex leg stopped firing — the guard aborted the run instead of " \
        "blanking prompt_text: %r" % out[:200]
    checks += 1

    # 4) every fire row carries the aim classification, so the guard is measured rather than trusted
    log = SANDBOX / ".claude" / "state" / "friction-action-log.jsonl"
    assert log.is_file(), "no action log written to the sandbox — the run never reached the logger"
    fires = [json.loads(l) for l in log.read_text().splitlines()
             if l.strip() and json.loads(l).get("action") == "fire_inject"]
    assert fires, "no fire_inject rows in the sandbox log"
    assert all("user_turn" in f for f in fires), \
        "a fire row has no `user_turn` — the aim question becomes unanswerable on this path again, " \
        "which is the blind spot that made life only 72% measured"
    # The alarm is scoped to artifacts that MATCH ON THE PROMPT. `test_event_only` has no
    # `prompt_regex` leg, so it fires on every turn by design and legitimately reads
    # `user_turn: false` — the first draft of this assertion did not make that distinction and
    # flagged correct behaviour, which is the false alarm the module comment now warns about.
    prompt_fires = [f for f in fires if f.get("artifact_id") == "test_machine_guard"]
    assert prompt_fires, "no fires recorded for the prompt-matching artifact"
    assert all(f["user_turn"] is True for f in prompt_fires), \
        "a PROMPT-MATCHING artifact fired on a turn classified as not-user — after the guard that " \
        "is an alarm, not a statistic: %r" % [f for f in prompt_fires if f.get("user_turn") is not True][:2]
    event_only = [f for f in fires if f.get("artifact_id") == "test_event_only"]
    assert event_only and all(f.get("user_turn") is False for f in event_only), \
        "the event-only artifact should have fired on a machine turn and recorded user_turn=false"
    checks += 1

    # 5) the guard is on the live path, not in _normalize (which tests and the gate also call)
    src = (REPO / "scheduling" / "claude-si" / "friction_dispatch.py").read_text()
    norm = src[src.index("def _normalize("):src.index("def _stop_oracles(")]
    assert "_prompt_source" not in norm, \
        "the guard moved into _normalize — normalize_for_test and friction_test_gate call it, and it " \
        "would silently rewrite locked fixtures and the measured fire-rate"
    checks += 1
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("ok — %d checks: real prompt fires, machine turns do not, non-prompt legs unaffected" % checks)
