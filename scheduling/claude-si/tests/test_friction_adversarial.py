#!/usr/bin/env python3
"""Adversarial tests locking the Codex-review fixes (2026-07-22). Each maps to a finding.
  python3 tests/test_friction_adversarial.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
# WAS: os.environ.setdefault("CORE_ORG_ID", "1") — at import, unconditionally. Inert wherever
# the variable is exported, and on any seat where it is NOT this ran the adversarial test for
# the friction system AS ORG 1, whatever seat it was on. The comment below records the
# fixtures being de-hardcoded for exactly this reason; the setdefault was left behind. Found
# by core-ops reviewing the fix that missed it.
sys.path.insert(0, str(HERE.parent.parent / "brain-pg"))
from _env import get_org_id  # noqa: E402
os.environ["CORE_ORG_ID"] = str(get_org_id())  # set it to what identity says, never to 1
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
os.environ["CLAUDE_PROJECT_DIR"] = tempfile.mkdtemp()
os.environ["CORE_INSTANCE"] = os.environ["CLAUDE_PROJECT_DIR"]

import friction_dispatch as fd            # noqa: E402
import friction_installer as inst         # noqa: E402
import friction_jsonl as fj               # noqa: E402
import importlib                          # noqa: E402
importlib.reload(fd); importlib.reload(inst)

# The dispatcher filters artifacts by org — friction_dispatch.py:248 keeps only those whose
# org_id equals int(os.environ["CORE_ORG_ID"] or 0). Every fixture below used to hardcode
# org_id 1, so on any Core that is not org 1 they were filtered out before dispatch, nothing
# fired, and three checks failed for a reason unrelated to what they test. core-business
# (org 2) reported exactly that today: "multi-match injects the legacy contract",
# "block artifact downgraded to inject", and "dispatcher log redacts secret session_id" —
# the last of which I initially misdiagnosed as a missing local artifact. All three are this.
# Deriving the org makes the suite test the dispatcher rather than test whether it is running
# on life. This is the "validator hardcoding org 1" class named in tasks/lessons.md 2026-07-30.
_ORG = get_org_id()  # not `env or 1` — identity decides, same resolver install() uses

_fails = []
def check(name, cond, d=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {d}" if d and not cond else ""))
    if not cond: _fails.append(name)


def test_redos():  # #1
    for bad in ["(a+)+$", "(a*)*b", "(x+)+y", "(.*)*z", "a{2,}{3,}"]:
        t = time.time(); r = fd._safe_regex(bad, "a" * 60 + "!"); dt = time.time() - t
        check(f"ReDoS rejected fast: {bad}", r is False and dt < 0.2, f"r={r} dt={dt:.2f}")


def test_trusted_regex_and_multimatch():  # WS1 — one-spine dispatcher changes
    import io, json
    # trusted path: a human-authored optional-group regex is REJECTED structurally but ACCEPTED trusted
    legacy = r"\b(that'?s wrong|you (just )?said|you flip)\b"
    check("legacy regex rejected by structural validator", fd._validate_regex(legacy) is False)
    check("legacy regex accepted on trusted path", fd._safe_regex(legacy, "you just said x", trusted=True))
    check("even trusted still bounds length", fd._safe_regex("a" * 500, "aaa", trusted=True) is False)
    # trusted keys off template.id — a NON-legacy artifact does NOT get the trusted bypass
    fd._atomic_write = getattr(fd, "_atomic_write", None)  # dispatcher has no writer; use installer's file
    inst._atomic_write(inst.ACTIVE, {"artifacts": [
        {"artifact_id": "legacy_x", "org_id": _ORG, "event": "UserPromptSubmit", "trusted_regex": True,
         "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                               {"op": "prompt_regex", "value": r"\b(you (just )?said)\b"}]},
         "effect": {"mode": "inject", "message": "L-X fired"},
         "template": {"id": "legacy-learned-contract", "sha256": "legacy"},
         "lease": {"max_fires_per_session": 2}},
        {"artifact_id": "art_spoof", "org_id": _ORG, "event": "UserPromptSubmit",
         "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                               {"op": "prompt_regex", "value": r"\byou (just )?said\b"}]},
         "effect": {"mode": "inject", "message": "L-Y fired"},
         # spoofs the legacy template.id but has NO projector-set trusted_regex → structural validator
         # still applies (trust is projector-controlled, not artifact-controlled) → rejected
         "template": {"id": "legacy-learned-contract", "sha256": "x"},
         "lease": {"max_fires_per_session": 2}}]})
    importlib.reload(fd)
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps({"prompt": "you just said the opposite", "session_id": "s"})); sys.stdout = io.StringIO()
    fd.run("UserPromptSubmit"); out = sys.stdout.getvalue(); sys.stdin, sys.stdout = old_in, old_out
    ctx = out
    check("multi-match injects the legacy (trusted) contract", "L-X fired" in ctx, ctx[:80])
    check("non-legacy artifact with optional-group regex does NOT fire (structural reject)", "L-Y fired" not in ctx)


def _spec(**over):
    s = {"spec_version": 1, "artifact_id": "art_x", "case_id": "c", "org_id": _ORG, "type": "contract",
         "event": "UserPromptSubmit", "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
         {"op": "prompt_regex", "value": r"\b(migration|schema)\b"}]},
         "effect": {"mode": "inject", "message": "m", "skill_id": None},
         "tests": {"positive_ids": ["p1"], "negative_ids": ["n1", "n2"]},
         "template": {"id": "t", "sha256": "x"}, "scope": "org_local",
         "lease": {"max_fires_per_session": 2, "expires_at": None}, "generator_version": "t"}
    s.update(over); return s


def test_schema_reject():  # #2, inject-only
    ok, _ = inst._validate_spec(_spec(condition={"all": []}), _ORG)
    check("reject vacuous {all:[]}", not ok)
    ok, _ = inst._validate_spec(_spec(effect={"mode": "block", "message": "m", "skill_id": None}), _ORG)
    check("reject mode=block (inject-only)", not ok)
    ok, _ = inst._validate_spec(_spec(condition={"all": [{"op": "run_shell", "value": "x"}]}), _ORG)
    check("reject unknown op", not ok)
    # A literal 2 here stopped being a mismatch the moment the suite ran as org 2 — the test
    # would have silently asserted the opposite of its own name. Derive a different org.
    ok, _ = inst._validate_spec(_spec(org_id=_ORG + 1), _ORG)
    check("reject org mismatch", not ok)
    ok, _ = inst._validate_spec(_spec(lease={"max_fires_per_session": 99, "expires_at": None}), _ORG)
    check("reject bad lease cap", not ok)
    deep = {"all": [{"all": [{"all": [{"all": [{"all": [{"all": [{"all": [{"op": "event_is", "value": "x"}]}]}]}]}]}]}]}
    ok, _ = inst._validate_spec(_spec(condition=deep), _ORG)
    check("reject over-deep condition", not ok)
    ok, _ = inst._validate_spec(_spec(), _ORG)
    check("accept a valid inject spec", ok)
    # --- 4th-review fixes ---
    ok, _ = inst._validate_spec(_spec(condition={"all": [{"op": "event_is", "value": "x"}], "evil": 1}), _ORG)
    check("reject nested unknown key in combinator", not ok)
    ok, _ = inst._validate_spec(_spec(condition={"op": "event_is", "value": "x", "evil": 1}), _ORG)
    check("reject nested unknown key in leaf", not ok)
    ok, _ = inst._validate_spec(_spec(effect={"mode": "inject", "message": "m", "skill_id": None, "x": 1}), _ORG)
    check("reject unknown key in effect", not ok)
    ok, _ = inst._validate_spec(_spec(org_id=True), _ORG)
    check("reject bool org_id (True != int 1)", not ok)
    ok, _ = inst._validate_spec(_spec(artifact_id="sk-live-SECRET"), _ORG)
    check("reject secret-shaped artifact_id", not ok)
    ok, _ = inst._validate_spec(_spec(type="blocker"), _ORG)
    check("reject non-contract type", not ok)
    # --- 5th-review fixes: installer is the boundary, not the router ---
    ok, _ = inst._validate_spec(_spec(event="Stop"), _ORG)
    check("reject non-UserPromptSubmit event at install", not ok)
    ok, _ = inst._validate_spec(_spec(event="UserPromptSubmit", condition={"all": [
        {"op": "event_is", "value": "UserPromptSubmit"}, {"op": "assistant_regex", "value": "x"}]}), _ORG)
    check("reject non-prompt op (assistant_regex) at install", not ok)
    ok, _ = inst._validate_spec(_spec(tests={"positive_ids": ["p1", "p1"], "negative_ids": ["n1", "n2"]}), _ORG)
    check("reject duplicate declared test ids", not ok)
    ok, _ = inst._validate_spec(_spec(tests={"positive_ids": ["p1"], "negative_ids": ["p1", "n2"]}), _ORG)
    check("reject overlapping pos/neg test ids", not ok)


def test_rollback_rejects_bad_id():  # 5th review — rollback validates before logging
    r = inst.rollback("sk-live-SECRET")
    check("rollback rejects secret-shaped id", not r["ok"], str(r))


def test_dispatch_log_redacts_session_id():  # 6th review — dispatcher's OWN logger redacts
    import io, json
    inst._atomic_write(inst.ACTIVE, {"artifacts": [{"artifact_id": "art_d", "org_id": _ORG,
        "event": "UserPromptSubmit", "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
        {"op": "prompt_regex", "value": r"\bzxqw\b"}]},
        "effect": {"mode": "inject", "message": "m"}, "lease": {"max_fires_per_session": 2}}]})
    importlib.reload(fd)
    secret = "sk-live-ABCDEFGH12345678"
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps({"prompt": "zxqw now", "session_id": secret})); sys.stdout = io.StringIO()
    fd.run("UserPromptSubmit")
    sys.stdin, sys.stdout = old_in, old_out
    log_txt = fd.ACTION_LOG.read_text() if fd.ACTION_LOG.exists() else ""
    check("dispatcher log redacts secret session_id", secret not in log_txt and "fire" in log_txt,
          f"log_has_secret={secret in log_txt} dispatcher_fired={'fire' in log_txt}")
    # nested (list) session_id must ALSO not leak (Codex 7th review)
    sys.stdin = io.StringIO(json.dumps({"prompt": "zxqw again", "session_id": [secret]}))
    sys.stdout = io.StringIO()
    fd.run("UserPromptSubmit")
    sys.stdin, sys.stdout = old_in, old_out
    log_txt2 = fd.ACTION_LOG.read_text() if fd.ACTION_LOG.exists() else ""
    # The `"fire" in log_txt2` half is not decoration. Without it this reads "the secret is absent
    # from the log", which is ALSO what an empty log looks like — so it would go green on a Core
    # where the dispatcher never ran, certifying redaction that was never exercised. The flat case
    # above always had this clause; this one did not, and the asymmetry survived seven reviews.
    # Raised by core-business on the bus 2026-08-04 against the flat check; the flat check was
    # already sound and THIS is where the hole actually was.
    check("dispatcher log redacts nested/list session_id",
          secret not in log_txt2 and "fire" in log_txt2,
          f"log_has_secret={secret in log_txt2} dispatcher_fired={'fire' in log_txt2}")


def test_gate_min_corpus_and_exact_ids():  # 4th review — corpus size + exact id binding
    import friction_test_gate as tg
    spec = _spec(event="UserPromptSubmit",
                 condition={"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                                    {"op": "prompt_regex", "value": r"\bzxqw\b"}]},
                 tests={"positive_ids": ["p1"], "negative_ids": ["n_evt", "n_pol"]})
    ex = {"positive": [{"id": "p1", "event": "UserPromptSubmit", "expected": "fire",
                        "provenance": "real_positive", "hook_input": {"event": "UserPromptSubmit", "prompt": "zxqw please"}}],
          "negative": [{"id": "n_evt", "event": "Stop", "expected": "no_fire", "provenance": "event_mismatch",
                        "hook_input": {"event": "Stop", "assistant_text": "zxqw"}},
                       {"id": "n_pol", "event": "UserPromptSubmit", "expected": "no_fire", "provenance": "polarity_mutation",
                        "hook_input": {"event": "UserPromptSubmit", "prompt": "unrelated"}}]}
    ok, why = tg.gate(spec, ex, corpus_prompts=["a", "b"])  # tiny corpus
    check("gate rejects tiny corpus", not ok, why)
    # exact-id binding: declaring an extra positive_id the examples don't cover -> reject
    spec2 = _spec(event="UserPromptSubmit",
                  condition=spec["condition"], tests={"positive_ids": ["p1", "pX"], "negative_ids": ["n_evt", "n_pol"]})
    ok2, why2 = tg.gate(spec2, ex, corpus_prompts=None)
    check("gate rejects id mismatch (declared pX not in examples)", not ok2, why2)


def test_redaction():  # #12
    r = fj.redact("token is sk-live-ABCDEFGH12345678 do not print")
    check("redacts sk- token", "sk-live" not in r and "[REDACTED]" in r, r)
    r2 = fj.redact("aws AKIAABCDEFGH12345678 key")
    check("redacts AKIA key", "AKIA" not in r2)


def test_compaction_boundary():  # #10
    recs = [
        {"uuid": "old_prompt", "type": "user", "userType": "external", "origin": {"kind": "human"}, "sessionId": "S1", "parentUuid": None,
         "message": {"content": "old task"}},
        {"uuid": "old_a", "type": "assistant", "sessionId": "S1", "parentUuid": "old_prompt",
         "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"uuid": "summ", "type": "summary", "sessionId": "S2", "parentUuid": "old_a", "summary": "…"},
        {"uuid": "corr", "type": "user", "userType": "external", "origin": {"kind": "human"}, "sessionId": "S2", "parentUuid": "summ",
         "message": {"content": "no wrong"}},
    ]
    idx = {r["uuid"]: r for r in recs}
    m = fj.reconstruct_moment("corr", idx)
    check("does NOT cross summary boundary", m is not None and not m.complete and "Bash" not in m.tool_uses,
          f"tools={m.tool_uses if m else None}")


def test_block_order():  # #11
    recs = [
        {"uuid": "p", "type": "user", "userType": "external", "origin": {"kind": "human"}, "parentUuid": None, "message": {"content": "go"}},
        {"uuid": "a", "type": "assistant", "parentUuid": "p",
         "message": {"content": [{"type": "text", "text": "first"}, {"type": "tool_use", "name": "Edit"},
                                 {"type": "text", "text": "second"}]}},
        {"uuid": "c", "type": "user", "userType": "external", "origin": {"kind": "human"}, "parentUuid": "a", "message": {"content": "no"}},
    ]
    idx = {r["uuid"]: r for r in recs}
    m = fj.reconstruct_moment("c", idx)
    texts = [b["text"] for b in m.blocks if b["kind"] == "text"]
    check("within-record order preserved (first before second)", texts == ["first", "second"], str(texts))


def test_rollback_local():  # #7
    inst._atomic_write(inst.ACTIVE, {"artifacts": [
        {"artifact_id": "art_a", "org_id": _ORG, "event": "UserPromptSubmit", "effect": {"mode": "inject"}},
        {"artifact_id": "art_b", "org_id": _ORG, "event": "UserPromptSubmit", "effect": {"mode": "inject"}}]})
    inst.rollback("art_a")
    ids = [a["artifact_id"] for a in inst._load_active()["artifacts"]]
    check("rollback removes only target, keeps healthy B", ids == ["art_b"], str(ids))


def test_inject_only_dispatch():  # #1/#15 — a block artifact is downgraded, never blocks
    import io, json
    inst._atomic_write(inst.ACTIVE, {"artifacts": [{"artifact_id": "blk", "org_id": _ORG,
        "event": "UserPromptSubmit", "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"}]},
        "effect": {"mode": "block", "message": "should not block"}, "lease": {"max_fires_per_session": 2}}]})
    importlib.reload(fd)
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps({"prompt": "hi", "session_id": "s"})); sys.stdout = io.StringIO()
    fd.run("UserPromptSubmit"); out = sys.stdout.getvalue()
    sys.stdin, sys.stdout = old_in, old_out
    check("block artifact downgraded to inject (no decision:block)", '"decision"' not in out and "additionalContext" in out, out[:80])


if __name__ == "__main__":
    for fn in [test_redos, test_trusted_regex_and_multimatch, test_schema_reject, test_gate_min_corpus_and_exact_ids,
               test_rollback_rejects_bad_id, test_dispatch_log_redacts_session_id, test_redaction,
               test_compaction_boundary, test_block_order, test_rollback_local, test_inject_only_dispatch]:
        print(fn.__name__); fn()
    print(f"\n{'ALL PASS' if not _fails else 'FAILURES: ' + ', '.join(_fails)}")
    sys.exit(1 if _fails else 0)
