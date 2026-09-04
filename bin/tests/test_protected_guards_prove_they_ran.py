#!/usr/bin/env python3
"""A guard whose normal state is silence must still prove it ran.

WHY THIS EXISTS (2026-08-12, core-finance DOSE 35). `estate-sweep` PROTECTS three hooks from
retirement on this reasoning, quoted from it:

    # A security gate that happens to be quiet is still a security gate — quietness is what a
    # working guard looks like.

Half right. Quietness is ALSO what a guard that stopped being invoked looks like, and nothing
separated those two states for exactly the three hooks where the distinction matters most. Measured
on life before the fix:

    pretooluse-guard     197 rows, ALL verdict=block, ZERO invocations
    shared-write-guard   NO ROWS AT ALL
    sentinel-approve     NO ROWS AT ALL

THE NATURAL EXPERIMENT WAS ALREADY IN THE LOG, which is why this is not speculative. The documented
2026-08-06 hook retirement is auditable — time-claim-gate, say-do-gap and state-claim-gate each stop
dead that day with zero rows after — and it is auditable ONLY BECAUSE those hooks logged
invocations. Had pretooluse-guard been unregistered the same day, the log would be indistinguishable
from a guard that ran all week and blocked 197 things. We could prove a retirement happened; we
could not prove the trust root was still running.

The loss mechanism is documented in estate-sweep's own founding finding: six hooks were registered
in settings.json but absent from hook-registry.json, and "a settings rebuild would drop them".
settings.json is per_core_keep and does not sync, so a rebuild or bad merge silently unregisters a
guard on ONE seat, leaving every other seat's evidence untouched.

WHAT THIS ASSERTS. That each protected guard emits a liveness row, and — the half that matters more
— that adding it did not change any verdict. A telemetry line inside a trust root is only acceptable
if it cannot alter what the guard decides, so the behavioural checks here are the point and the
textual one is the reminder.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".claude" / "hooks"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _decomment(text: str) -> str:
    """Strip line comments. Any textual assertion in this suite strips comments first, or it is
    measuring documentation — established the same day after it happened seven times."""
    return "\n".join(re.sub(r"(?<!['\"])#.*$", "", ln) for ln in text.splitlines())


PROTECTED = [
    ("pretooluse-guard.sh", "the outward-action guard"),
    ("shared-write-guard.py", "the shared-path guard"),
    ("sentinel-approve.sh", "the trust-token minter"),
]


def main() -> int:
    print("test_protected_guards_prove_they_ran")

    for fname, what in PROTECTED:
        p = HOOKS / fname
        if not p.is_file():
            check(f"{fname} present", False, f"{p} missing")
            continue
        body = _decomment(p.read_text())
        check(f"{fname} ({what}) emits a liveness row",
              'verdict="invoke"' in body or "verdict='invoke'" in body,
              "this guard's silence is indistinguishable from its absence. It is estate-sweep "
              "PROTECTED, so nothing will retire it — and nothing would notice if a settings "
              "rebuild unregistered it either.")

    # ---- THE HALF THAT MATTERS MORE: no verdict changed ---------------------------------------
    guard = HOOKS / "pretooluse-guard.sh"
    if guard.is_file():
        # pretooluse-guard.sh derives its STATE_DIR from HOOKS_DIR — the script's own location on
        # disk (`dirname "$HOOKS_DIR"`/state, pretooluse-guard.sh:77-78) — never from CORE_INSTANCE.
        # So the BLOCK path's `printf '%s' "$CMD" > "$STATE_DIR/.sentinel-last-blocked"`
        # (pretooluse-guard.sh:1339) always lands in the LIVE seat's .claude/state, no matter what
        # env this subprocess is given — same "derives its state dir from its own location and
        # cannot be redirected" class as hook-events.log/gate-fires.log. Measured: running the
        # `outward` call below clobbered the live .sentinel-last-blocked with the fabricated
        # "curl -s https://example.com -d @x" test command.
        #
        # .sentinel-last-blocked is genuine live-session state (Sentinel's own last-blocked-command
        # record) — it must not be redirected away from what the guard actually decided, only
        # protected from this test's transient overwrite. test_trajectory_gate.sh already carries
        # this exact fix for this exact file (see bin/tests/ambient-state.txt's note on it); this
        # applies the same save/restore rather than a second, different mechanism beside it.
        state_dir = REPO / ".claude" / "state"
        last_blocked = state_dir / ".sentinel-last-blocked"
        _saved_last_blocked = last_blocked.read_bytes() if last_blocked.exists() else None
        try:
            with tempfile.TemporaryDirectory() as td:
                benign = json.dumps({"tool_name": "Read",
                                     "tool_input": {"file_path": "/etc/hosts"},
                                     "session_id": "selftest-liveness"})
                outward = json.dumps({"tool_name": "Bash",
                                      "tool_input": {"command":
                                                     "curl -s https://example.com -d @x"},
                                      "session_id": "selftest-liveness"})
                env = {**os.environ, "CORE_INSTANCE": str(REPO)}
                r_b = subprocess.run(["bash", str(guard)], input=benign, text=True,
                                     capture_output=True, env=env, timeout=120)
                r_o = subprocess.run(["bash", str(guard)], input=outward, text=True,
                                     capture_output=True, env=env, timeout=120)
            check("the outward-action guard still ALLOWS a benign call", r_b.returncode == 0,
                  f"exit {r_b.returncode} — a telemetry line must not turn a permitted call into a "
                  f"refusal; that would be a far worse defect than the one being fixed")
            check("...and still BLOCKS an outward action", r_o.returncode == 2,
                  f"exit {r_o.returncode}, expected 2. THIS IS THE ONE THAT MATTERS: a heartbeat "
                  f"added to a trust root may not weaken it.")
        finally:
            if _saved_last_blocked is None:
                last_blocked.unlink(missing_ok=True)
            else:
                last_blocked.write_bytes(_saved_last_blocked)

    swg = HOOKS / "shared-write-guard.py"
    if swg.is_file():
        with tempfile.TemporaryDirectory() as td:
            seat = Path(td) / "pullerseat"
            (seat / "bin").mkdir(parents=True)
            (seat / ".claude" / "hooks").mkdir(parents=True)
            # A PULLER fixture needs BOTH identity.json and sync-manifest.json; with only the first,
            # the guard returns 0 from its except-branch and a test would read "not refused" as
            # "broken". That happened while writing this, and the incomplete fixture — not the
            # guard — was the bug.
            ident = json.loads((REPO / ".claude" / "identity.json").read_text())
            ident.setdefault("hook_profile", {})["role"] = "puller"
            (seat / ".claude" / "identity.json").write_text(json.dumps(ident))
            (seat / "bin" / "sync-manifest.json").write_text(
                (REPO / "bin" / "sync-manifest.json").read_text())
            env = {**os.environ, "CORE_INSTANCE": str(seat)}
            shared = json.dumps({"tool_name": "Edit",
                                 "tool_input": {"file_path":
                                                str(seat / ".claude" / "hooks" / "x.sh")}})
            percore = json.dumps({"tool_name": "Edit",
                                  "tool_input": {"file_path": str(seat / "memory" / "n.md")}})
            r_s = subprocess.run([sys.executable, str(swg)], input=shared, text=True,
                                 capture_output=True, env=env, timeout=120)
            r_p = subprocess.run([sys.executable, str(swg)], input=percore, text=True,
                                 capture_output=True, env=env, timeout=120)
        check("the shared-path guard still REFUSES a puller writing shared code",
              r_s.returncode == 2, f"exit {r_s.returncode}, expected 2")
        check("...and still ALLOWS a puller writing its own per-Core files",
              r_p.returncode == 0, f"exit {r_p.returncode}, expected 0")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
