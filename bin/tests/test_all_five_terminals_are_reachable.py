#!/usr/bin/env python3
"""Every promotion path the loop claims to have must be REACHABLE and WIRED, not merely written.

WHY THIS EXISTS (2026-08-20).

Nick asked for autonomous self-improvement across every path — skills, hooks, workflows, CLAUDE.md
edits, contracts — in **67 separate messages** across three Cores, starting 2026-07-16 with a message
that already told us *"none of them are working."* Five weeks later the ledger read:

    inject contracts      700     (predates the ask)
    CLAUDE.md directives    2     (one day in July)
    skills                  0
    hooks                   0
    workflows               0

Four independent breaks, every one of them "built but never connected":

  A. `ask_miner.ask_cases` refused any ask that could not ground a PROMPT trigger, and it ran inside
     case construction — UPSTREAM of routing. So an ask bound for a terminal that needs no trigger
     died on a requirement that does not apply to it. 11 of life's 16 qualifying asks dropped there;
     FOUR of them routed to `hooked_skill`, including "execute the plan fully end-to-end with no
     loose ends" — the system correctly identified Nick's own complaint as a skill and binned it.
  B. `skill_graduate.promote()` — turns a proven hooked_skill into a real skill. Referenced in three
     comments. **Called by nothing.**
  C. `generate_from_workflows()` — complete, with its own renderer, trigger derivation, work-shape
     fallback and test file. **Zero callers. Never executed once.**
  D. No hook-authoring path existed at all.

WHAT THIS ASSERTS — the wiring, not the output. A terminal can legitimately produce nothing on a
given corpus; it may never be unreachable or unreferenced.

  1. Every terminal `artifact_typer` can emit has a branch in `artifact_generator.generate`.
  2. The trigger requirement is applied AFTER routing and only to terminals that fire by matching.
  3. `skill_graduate.promote` and `generate_from_workflows` are called from the loop's own run path.
  4. The work-hook terminal fires at a mutation and stays silent on a read and on a prompt.
  5. It is escalation-only — it can never take an ask from a stronger terminal.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SI = REPO / "scheduling" / "claude-si"
sys.path.insert(0, str(SI))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

import artifact_typer as T  # noqa: E402

checks = 0
typer_src = (SI / "artifact_typer.py").read_text()
gen_src = (SI / "artifact_generator.py").read_text()
loop_src = (SI / "friction_loop.py").read_text()
miner_src = (SI / "ask_miner.py").read_text()

# --- 1) every emitted terminal has a generator branch -------------------------------------------
terminals = set(re.findall(r'"type":\s*"([a-z_]+)"', typer_src))
# Two terminate in the router without generating anything, and `inject_contract` is the DOCUMENTED
# fall-through at the end of generate() rather than a named branch — asserting a branch for it would
# be a test demanding the code be restructured to match the test, which is backwards.
terminals -= {"already_covered", "scheduled_job_proposal", "inject_contract"}
assert terminals, "no terminals found — retarget this test"
assert "route_ask_case" in gen_src.split("def generate(")[1], \
    "generate() no longer falls through to route_ask_case — inject_contract has lost its terminal"
for t in sorted(terminals):
    assert f'"{t}"' in gen_src, \
        "terminal %r is emitted by the router and has no branch in artifact_generator" % t
checks += 1

# --- 2) BREAK A: the trigger requirement is below the router, not in case construction ------------
gate = miner_src[miner_src.index("def ask_cases("):]
gate = gate[:gate.index("return out")]
assert "if not trig:\n            if drops" not in gate and "\n            continue" not in gate.split("_frustration_share")[0][-400:], \
    "ask_cases still drops on a missing prompt trigger — the gate is back above the router"
assert "route_needs_trigger" in loop_src, \
    "the loop no longer applies the trigger requirement per-terminal after routing"
m = re.search(r'if not case\.get\("_ask_trigger"\) and t in \(([^)]*)\)', loop_src)
assert m, "the per-terminal trigger requirement is missing from the loop"
needs = set(re.findall(r'"([a-z_]+)"', m.group(1)))
assert needs == {"inject_contract", "enforcement_block"}, \
    "trigger required for %s — a directive has nothing to match and a work-shaped skill keys on a " \
    "tool, so requiring one of either rebuilds the defect" % (needs,)
checks += 1

# --- 3) BREAKS B and C: the orphans are called from the loop's run path ---------------------------
run = loop_src[loop_src.index("\ndef run("):]
run = run[:run.index("\ndef ", 10)]
for fn, why in (("generate_from_workflows", "workflows never install"),
                ("promote(", "a proven skill never graduates")):
    assert fn in run, "run() does not call %s — %s" % (fn, why)
checks += 1

# --- 4) BREAK D: the work hook fires at the work moment, and only there ---------------------------
import artifact_generator as G  # noqa: E402
import friction_dispatch as fd  # noqa: E402

ask = "zz stop renaming my files without telling me"
route = T.route_type(ask, frustration_share=0.9, still_recurring=True)
assert route["type"] == "work_hook", "high-frustration recurring ask no longer escalates: %r" % route
# A work_hook ROUTE installs as a hooked_skill ARTIFACT, and that is not a compromise — the
# installer fences PreToolUse to `hooked_skill` on purpose (friction_installer.py:493: a contract's
# specificity is proven against a corpus of PROMPTS, and a tool-shaped condition has no grounding
# there). The first build of this terminal invented a parallel PreToolUse `contract` type and the
# validator refused it outright. A work-moment artifact IS a work-shaped skill; the frustration
# share selects that form rather than minting one of its own.
#
# Generation writes a payload file, so it runs against a SANDBOX. The first version of this test
# did not, and installed real artifacts into the live seat — caught by run-all's leak detector,
# which is the second time today a test of mine wrote where it was measuring.
import os as _os, shutil as _shutil, tempfile as _tf
import friction_installer as _fi  # noqa: E402

# THIS SEAT'S ORG AS THE ARGUMENT. A literal here is the same defect as a literal in a spec:
# on any seat whose org is not 1 the call operates on — and in promote/generate paths WRITES
# to — life's partition. Found by sweeping the ARGUMENT side after core-school showed the
# spec-side sweep had missed it.
import sys as _sys2
from pathlib import Path as _PP2
_sys2.path.insert(0, str(_PP2(__file__).resolve().parents[2] / "scheduling" / "brain-pg"))
from _env import get_org_id as _goi2  # noqa: E402
ORG = _goi2(_PP2(__file__).resolve().parents[2])
_sandbox = Path(_tf.mkdtemp(prefix="terminals-"))
_saved_procdir = _fi.PROCDIR
_fi.PROCDIR = _sandbox / "procedures"
try:
    g = G.generate(ORG, {"case_id": "zz", "user_wanted": ask, "support": {"count": 9}}, route)
finally:
    _fi.PROCDIR = _saved_procdir
    _shutil.rmtree(_sandbox, ignore_errors=True)
assert g["action"] == "install_hooked_skill", \
    "a work_hook must generate a hooked_skill artifact — PreToolUse is fenced to that type: %s" % g["action"]
spec = g["spec"]
assert spec["type"] == "hooked_skill", spec["type"]
assert spec["event"] == "PreToolUse", "a work hook must fire before the work, not after"
assert spec["effect"]["mode"] == "inject", "a work hook speaks; it must not block"
for ev, tool, want in (("PreToolUse", "Edit", True), ("PreToolUse", "Read", False),
                       ("UserPromptSubmit", None, False)):
    payload = {"tool_name": tool} if tool else {"prompt": ask}
    got = fd.evaluate(spec["condition"], fd.normalize_for_test(payload, ev))
    assert got is want, "work hook on %s/%s fired=%s, expected %s" % (ev, tool, got, want)
checks += 1

# --- 5) escalation-only: it can never take an ask from a stronger terminal ------------------------
assert T.route_type(ask, frustration_share=0.9, still_recurring=False)["type"] == "inject_contract", \
    "escalated an ask that stopped recurring — a solved problem must not be escalated"
assert T.route_type(ask, frustration_share=0.5, still_recurring=True)["type"] == "inject_contract", \
    "escalated below the threshold"
# a directive-bound ask stays a directive even at maximum frustration
d = T.route_type("explain the plan/system in simple, clear terms nick can understand",
                 frustration_share=1.0, still_recurring=True)
assert d["type"] != "work_hook", \
    "frustration outranked a stronger terminal (%s) — the work hook must only ever replace the " \
    "default reminder" % d["type"]
checks += 1

# --- 6) the promoter's budget gate refuses when the ceiling is unknown ----------------------------
# Structure, not prose — a message can be reworded, a missing `return` cannot be talked around.
_gate = gen_src[gen_src.index("def promote_proven_contract("):]
_gate = _gate[:_gate.index("\ndef ")]
_m = re.search(r"if not _bl\.is_file\(\):\s*\n\s*return \{[^}]*promote_skipped", _gate)
assert _m, ("the steering-budget gate does not RETURN on a missing baseline — an absent ceiling is "
            "not 'no limit', it is 'the limit is unknown', and a ratchet must stop on unknown. "
            "Without the return, _ceiling stays None, the whole check is skipped, and it writes.")
checks += 1

# --- 7) every counter the loop publishes is actually incremented somewhere ------------------------
#
# `no_trigger` was initialised and incremented NOWHERE — it always read 0 while the branch folded
# into `skipped`. core-finance found it on a 4-case corpus by noticing `ask_cases` reported 3 asks
# without a trigger while the loop reported `no_trigger: 0`, and asked rather than asserted because
# it had not read the function. It was a defect: the ninth "built, named, never wired" of the day.
#
# A dead counter is worse than a missing one. It publishes a zero that reads as a measurement, and on
# a small seat that zero is exactly the number someone would trust.
gen = loop_src[loop_src.index("def generate_from_asks("):]
gen = gen[:gen.index("\ndef ", 10)]
init = re.search(r"out = \{([^}]*)\}", gen)
assert init, "generate_from_asks no longer initialises an out dict — retarget this test"
counters = [k for k in re.findall(r'"([a-z_]+)":\s*0\b', init.group(1))]
assert counters, "no zero-initialised counters found — retarget"
# A counter may be incremented through a VARIABLE key — `_key = "work_hooks" if ... else
# "procedures"` then `out[_key] += 1`. The first version of this check missed that and reported both
# as dead, which would have been a test demanding the code be rewritten to suit the test. Resolve the
# literals assigned to any variable that is later used as an `out[var] +=` subscript.
dyn = set()
for var in set(re.findall(r"out\[([a-z_]+)\]\s*\+=", gen)):
    for m in re.finditer(r"%s\s*=\s*(.+)" % re.escape(var), gen):
        dyn.update(re.findall(r'"([a-z_]+)"', m.group(1)))
dead = [c for c in counters
        if c not in dyn
        and not re.search(r'out\["%s"\]\s*(\+=|=\s*out\.get|=\s*len\()' % re.escape(c), gen)]
assert not dead, (
    "counter(s) initialised and never incremented: %s — each publishes a zero that reads as a "
    "measurement while the branch it names folds silently into another bucket" % dead)
checks += 1

print("ok — %d checks: %d terminals all generable, gate below the router, both orphans wired, "
      "work hook fires only at a mutation" % (checks, len(terminals)))
