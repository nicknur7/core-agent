#!/usr/bin/env python3
"""Who-tests-the-tester + install/dispatch/rollback safety proof (P3).

Proves: (1) the gate REJECTS intentionally-broken artifacts (positive-that-doesnt-fire,
negative-that-fires); (2) a valid artifact installs, the STATIC DISPATCHER actually fires it
on the real payload, and rollback restores the prior state. Stdlib only.

  python3 tests/test_friction_gate_safety.py
"""
import io
import json
import os
import sys as _sys
from pathlib import Path as _PPath
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# isolate state into a temp dir so we never touch real active artifacts
_TMP = tempfile.mkdtemp()
# CORE_INSTANCE MUST BE REDIRECTED TOO, NOT JUST CLAUDE_PROJECT_DIR.
# These tests isolate by overriding CLAUDE_PROJECT_DIR. The modules under test resolve their root
# from CORE_INSTANCE, falling back to their OWN repo root when it is unset — so the isolation
# covered the variable the test controls and missed the one the code follows, and every run wrote
# its fixtures into the LIVE seat. core-life's .claude/state/friction-artifacts/active.json was
# found holding a single artifact `blk` / "should not block" stamped org 2, and core-business's held
# three stamped org 1: each seat carrying the other's test fixtures. Found by core-business,
# bus #1039/#1040.
#
# active.json is a projection of Postgres, so nothing was lost — `si_project.project(org)` rebuilt
# 22 artifacts. That is luck about this particular file, not a property of the isolation.
os.environ["CLAUDE_PROJECT_DIR"] = _TMP
os.environ["CORE_INSTANCE"] = _TMP
# RESOLVED, NOT PINNED. This was `= "1"` — a hard assignment, which reads as a fixture
# constructing a sandbox and is actually the org pin in disguise. It survived the sweep
# that removed the `setdefault` pins, and core-school proved it live: they pulled the
# "fix", two of three tests went green, and this one still failed on org mismatch.
# Resolved from the REAL repo root, not from CORE_INSTANCE, because the lines above
# have already redirected that to a temp tree with no identity.json.
_sys.path.insert(0, str(_PPath(__file__).resolve().parents[3] / "scheduling" / "brain-pg"))
from _env import get_org_id as _goi
os.environ["CORE_ORG_ID"] = str(_goi(_PPath(__file__).resolve().parents[3]))

import friction_dispatch as fd          # noqa: E402
import friction_test_gate as tg         # noqa: E402
import friction_installer as inst       # noqa: E402
import importlib

# ─── ABSTAIN RATHER THAN CRASH WHEN THE CORPUS IS ABSENT ───────────────────────────────────────
# The install gate proves an artifact is SPECIFIC by scoring it against a real corpus sample, which
# lives in Postgres. On an interpreter without psycopg2 there is no corpus, the gate returns
# `UNDECIDABLE: no corpus sample available to prove specificity`, install writes nothing, and the
# next line reads active.json and dies with FileNotFoundError — a missing-file error standing in
# for a missing database.
#
# core-school hit exactly this after pulling the org fix: PASS under 3.9 (psycopg2 present), FAIL
# under 3.14 (absent), and their run-all.sh grades with 3.14. The org fix did not cause it — it
# UNMASKED it. Before the fix, install refused earlier on the org mismatch, so the DB dependency
# never got a chance to surface. A defect can only be found once the one in front of it is gone.
#
# run-all.sh already has the right verdict for this: exit 2 + an UNDECIDABLE line is ABSTAIN,
# "declined to certify here; no fixture, not a defect". Using the runner's existing convention
# rather than inventing a skip.
def _abstain_if_no_corpus(res):
    why = (res or {}).get("reason", "") or ""
    if "UNDECIDABLE" in why or "no corpus sample" in why:
        print(f"UNDECIDABLE: {why}")
        print("  This fixture needs a Postgres-backed corpus sample to exercise the install gate.")
        print("  Run it with an interpreter that has psycopg2 (compare `which -a python3`).")
        sys.exit(2)


# ─── THIS SEAT'S ORG, RESOLVED THE WAY THE INSTALLER RESOLVES IT ───────────────────────────────
# Never stamp a literal. Every spec below used `"org_id": 1`, and `install()` does NOT trust the
# spec's own org claim — it calls `_env.get_org_id()`, where **identity wins over the environment**
# (2026-08-05). The fixture redirects CORE_INSTANCE to a temp tree with no identity.json, so that
# resolution falls through to the SEAT. On life it returns 1, the literal matches, and the install
# succeeds. On core-school it returns 3, `_validate_spec(spec, org)` rejects the org mismatch,
# `install()` returns ok=False having written nothing, and the next line — `json.loads(
# inst.ACTIVE.read_text())` — dies with FileNotFoundError on a file that was never created.
#
# So this suite passed on the baseline WRITER and failed on every other seat, and the writer is the
# seat that decides whether the baseline is green. core-school reported it BROKEN at c7d0e37 under
# both interpreters while run-all.sh here reported ALL GREEN; both readings were correct.
#
# This is the second time the same literal has bitten. The header above records the first: three
# artifacts "stamped org 1: each seat carrying the other's test fixtures", found by core-business
# (#1039/#1040). That was fixed by isolating the STATE PATH; the org itself stayed hardcoded, so
# the identity-wins change turned a latent fixture bug into a cross-seat red suite.
#
# Deliberately fails LOUD rather than defaulting to 1: a seat where this cannot resolve is a seat
# where `install()` returns "no trusted org", and a default would restore exactly the silent
# writer-only pass this removes.
try:
    from _env import get_org_id  # noqa: E402
    ORG = get_org_id()
except Exception as _e:  # pragma: no cover - a seat this broken cannot run the installer either
    raise SystemExit(f"cannot resolve this seat's org id the way friction_installer does: {_e}")

# rebind module state paths to the temp dir
for m in (fd, inst):
    importlib.reload(m)

_fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond: _fails.append(name)


def _spec(cond, event="PreToolUse", mode="inject", aid="art_test"):
    return {"spec_version": 1, "artifact_id": aid, "case_id": "c", "org_id": ORG,
            "type": "contract", "event": event, "condition": cond,
            "effect": {"mode": mode, "message": "test msg", "skill_id": None},
            "tests": {"positive_ids": ["p1"], "negative_ids": ["n1", "n2", "n3"]},
            "template": {"id": "hook-rule-v1", "sha256": "x"}, "scope": "org_local",
            "lease": {"max_fires_per_session": 2, "expires_at": None},
            "generator_version": "test"}


def _ex(event, expected, provenance, **kw):
    return {"id": kw.pop("id", "e"), "event": event, "expected": expected,
            "provenance": provenance, "hook_input": {"event": event, "session_id": "t", **kw}}


def test_gate_rejects_positive_that_doesnt_fire():
    # condition needs a mutating tool; positive supplies a Read -> must NOT pass the gate
    spec = _spec({"all": [{"op": "event_is", "value": "PreToolUse"}, {"op": "tool_mutability_is", "value": "mutating"}]})
    ex = {"positive": [_ex("PreToolUse", "fire", "real_positive", id="p1", tool_name="Read")],
          "negative": [_ex("PreToolUse", "no_fire", "tool_mismatch", id="n1", tool_name="Grep")]}
    ok, why = tg.gate(spec, ex)
    check("gate rejects positive-that-doesnt-fire", not ok, f"ok={ok}")


def test_gate_rejects_negative_that_fires():
    # over-broad condition (any PreToolUse) fires on the negative too -> reject
    spec = _spec({"all": [{"op": "event_is", "value": "PreToolUse"}]})
    ex = {"positive": [_ex("PreToolUse", "fire", "real_positive", id="p1", tool_name="Edit")],
          "negative": [_ex("PreToolUse", "no_fire", "tool_mismatch", id="n1", tool_name="Edit")]}
    ok, why = tg.gate(spec, ex)
    check("gate rejects negative-that-fires (over-broad)", not ok, f"ok={ok}")


def test_gate_passes_valid():
    # v1 is UserPromptSubmit-only + prompt-grounded; two rare tokens must CO-OCCUR (conjunctive).
    spec = _spec({"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                          {"op": "prompt_regex", "value": r"\bzxqw\b"},
                          {"op": "prompt_regex", "value": r"\bpldk\b"}]}, event="UserPromptSubmit")
    ex = {"positive": [_ex("UserPromptSubmit", "fire", "real_positive", id="p1", prompt="zxqw pldk please")],
          "negative": [_ex("Stop", "no_fire", "event_mismatch", id="n1", assistant_text="zxqw pldk"),
                       _ex("UserPromptSubmit", "no_fire", "polarity_mutation", id="n2", prompt="all good thanks"),
                       _ex("UserPromptSubmit", "no_fire", "real_neighbor", id="n3", prompt="only zxqw here")]}
    ok, why = tg.gate(spec, ex)
    check("gate passes a valid artifact", ok, why)
    return spec, ex


def test_install_dispatch_rollback():
    spec, ex = test_gate_passes_valid()
    res = inst.install(spec, ex)
    _abstain_if_no_corpus(res)
    check("install ok", res["ok"], res["reason"])
    check("active.json written", inst.ACTIVE.exists())
    # the STATIC DISPATCHER must fire on the real positive payload
    payload = {"prompt": "zxqw pldk please", "session_id": "t"}
    old_stdin, old_stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload)); sys.stdout = io.StringIO()
    importlib.reload(fd)  # pick up temp ACTIVE path
    fd.run("UserPromptSubmit")
    out = sys.stdout.getvalue(); sys.stdin, sys.stdout = old_stdin, old_stdout
    check("dispatcher fired on real positive", "additionalContext" in out or "decision" in out, f"out={out[:80]!r}")
    # and NOT on a non-matching payload (only one of the two required tokens present)
    sys.stdin = io.StringIO(json.dumps({"prompt": "only zxqw here", "session_id": "t2"}))
    sys.stdout = io.StringIO()
    fd.run("UserPromptSubmit")
    out2 = sys.stdout.getvalue(); sys.stdin, sys.stdout = old_stdin, old_stdout
    check("dispatcher silent on non-match (one token missing)", out2.strip() == "", f"out={out2[:80]!r}")
    # rollback removes it
    rb = inst.rollback(spec["artifact_id"])
    check("rollback ok", rb["ok"], rb["reason"])
    active = json.loads(inst.ACTIVE.read_text())
    check("artifact gone after rollback", all(a["artifact_id"] != spec["artifact_id"] for a in active["artifacts"]))


if __name__ == "__main__":
    for fn in [test_gate_rejects_positive_that_doesnt_fire, test_gate_rejects_negative_that_fires,
               test_gate_passes_valid, test_install_dispatch_rollback]:
        print(fn.__name__)
        fn()
    print(f"\n{'ALL PASS' if not _fails else 'FAILURES: ' + ', '.join(_fails)}")
    sys.exit(1 if _fails else 0)
