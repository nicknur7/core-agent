#!/usr/bin/env python3
"""The model-pin gate has NEVER fired in 493 real spawns. Prove it still can.

WHY THIS EXISTS (2026-08-12, Phase 5). The master plan's Phase 5 states the rule this file enforces:
"Every agent output must be falsifiable by execution. Not 'I checked, it's fine' — 'here is the mutant
I built and here it is failing.'"

Applied to the gate itself. .claude/state/.model-pin-shadow.log carries 493 rows:

    tool=Agent          491        tool=Workflow      2
    pinned=true         463        pinned=false      28
    would_block_live=false  493    <- it has NEVER blocked on the Agent path
    unpinned_in_window=0    491    <- and the counter never reached the limit

A gate that has never fired is indistinguishable from a gate that CANNOT fire — which is the exact
class this repo has now found in four separate instruments tonight (fire_inject that nothing wrote,
steering-retire whose verdicts never intersect its retirable set, the leak counter that printed
before it was computed, a payload check whose log nothing read).

So: this one is DIFFERENT, and the difference is the finding. The spawn ledger persists correctly
(35 session files on disk, all recording pinned:true), the counter accumulates, and the refusal
triggers on the third unpinned spawn inside the window. It has been silent because every real fan-out
was already pinned — "no cause to fire", not "cannot fire".

That distinction is only worth anything if it is re-checked, so this re-checks it every run.

BOTH DIRECTIONS. A gate that refuses everything would also show zero real blocks while looking
healthy, so pinned spawns must pass. And the limit is asserted from the module rather than hardcoded
here: a future loosening of UNPINNED_LIMIT must not silently make this test vacuous.

ISOLATION: runs the gate against a THROWAWAY seat (CORE_INSTANCE redirected to a temp dir), uses a
dedicated session id, and deletes its own ledger.

This paragraph used to read "It writes nothing the live seat reads — the file is keyed by session and
no other reader looks for that id." That was false when written, and false for the same reason the
three agent specs this suite also corrected today were false: it reasoned about the ONE file the
author was thinking of (the session-keyed ledger) and asserted a property of ALL writes. The gate
also appends to `.model-pin-shadow.log` and `hook-events.log`. The leak still matters — a test
must not write into the live seat's state — but the REASON given here was wrong: cross-referenced
2026-08-13, NOTHING reads `.model-pin-shadow.log`. It is not "a corpus the SI loop MEASURES";
it is 559 records accrued over 45 days with no consumer. The claim was asserted in a test written
to defend the file, which is exactly what made it convincing. Caught by run-all.sh's LEAK
counter, added hours earlier for exactly this.
"""
import json
import os
import tempfile
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / ".claude" / "hooks" / "model-pin-gate.py"
SID = "selftest-model-pin-gate"

# THE SEAT THIS TEST RUNS AGAINST IS A DISPOSABLE COPY, NOT THE LIVE ONE (fixed 2026-08-12).
#
# This file previously invoked the gate with the session's own environment inherited, so
# CORE_INSTANCE still pointed at the live seat and the gate wrote `.model-pin-shadow.log` and
# `hook-events.log` into it — caught by run-all.sh's LEAK counter, which is exactly the leak that
# counter was added to detect a few hours earlier.
#
# It is not a tidiness problem. `.model-pin-shadow.log` is one of the corpora the SI loop MEASURES,
# so a test writing into it feeds synthetic rows to the thing that learns from them — the same
# defect as a dose polluting the budget-cap measurement it was taken to inform. The instrument must
# not be able to observe its own test fixtures.
#
# model-pin-gate.py:27 already honours CORE_INSTANCE / CLAUDE_PROJECT_DIR, so redirection needs no
# change to the hook. Any hook that derived its state dir from __file__ instead could NOT be
# redirected this way, which is what run-all.sh's leak note warns about.
_TMP = tempfile.mkdtemp(prefix="mpg-selftest-")
STATE = Path(_TMP) / ".claude" / "state"
STATE.mkdir(parents=True, exist_ok=True)
LEDGER = STATE / f".agent-spawns-{SID}.json"

_ENV = {**os.environ, "CORE_INSTANCE": _TMP, "CLAUDE_PROJECT_DIR": _TMP}

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _spawn(pinned: bool):
    tin = {"subagent_type": "general-purpose", "prompt": "selftest"}
    if pinned:
        tin["model"] = "sonnet"
    payload = {"tool_name": "Agent", "session_id": SID, "tool_input": tin}
    r = subprocess.run([sys.executable, str(GATE)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60, env=_ENV, cwd=_TMP)
    return r.returncode, (r.stderr or "")


def main() -> int:
    print("test_model_pin_gate_fires")
    if not GATE.is_file():
        print(f"  FAIL  {GATE} missing")
        return 1

    # Read the limit from the gate rather than hardcoding it — a future loosening must break this
    # test loudly rather than make it assert nothing.
    src = GATE.read_text()
    import re
    m = re.search(r"^UNPINNED_LIMIT\s*=\s*(\d+)", src, re.M)
    check("UNPINNED_LIMIT is readable from the gate", m is not None,
          "cannot locate the limit; this test would be asserting a number nobody enforces")
    if not m:
        return 1
    limit = int(m.group(1))

    LEDGER.unlink(missing_ok=True)
    try:
        # --- THE DOSE: unpinned spawns must be refused once past the limit ------------------
        codes = []
        for _ in range(limit + 1):
            rc, err = _spawn(pinned=False)
            codes.append(rc)
        check(f"spawns 1..{limit} unpinned are ALLOWED (a single judgment agent may inherit)",
              all(c == 0 for c in codes[:limit]),
              f"exit codes {codes[:limit]} — blocking the first unpinned agent would refuse "
              f"legitimate judgment work and train the operator to work around the gate")
        check(f"unpinned spawn #{limit + 1} in the window is REFUSED",
              codes[-1] == 2,
              f"exit {codes[-1]}, expected 2. The gate has never fired in 493 real spawns; if it "
              f"also cannot fire on demand, it is decoration and every fan-out has been "
              f"inheriting the session model unchecked.")

        # --- and the other direction: pinned spawns must never be refused -------------------
        LEDGER.unlink(missing_ok=True)
        pinned_codes = [_spawn(pinned=True)[0] for _ in range(limit + 1)]
        check("pinned spawns are never refused, however many",
              all(c == 0 for c in pinned_codes),
              f"exit codes {pinned_codes} — a gate that refuses correctly-pinned work is worse "
              f"than none; it gets disabled")
    finally:
        LEDGER.unlink(missing_ok=True)

    check("the dose left no ledger behind", not LEDGER.exists(),
          "this test must not leave state the live seat could read")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
