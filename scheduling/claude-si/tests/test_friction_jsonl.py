#!/usr/bin/env python3
"""Tests for friction_jsonl.py — the P1 parser. Real-shape fixtures (incl. the
system-hook-injection parent chain that broke the first cut) + fail-closed cases +
a read-only smoke against a real transcript. Stdlib only.

  python3 tests/test_friction_jsonl.py
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import friction_jsonl as fj

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _write(records):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for r in records:
        f.write(json.dumps(r) + "\n")
    f.close()
    return Path(f.name)


# --- Fixture: a real-shape turn: prompt -> assistant(text+tool_use) -> tool_result(user) ->
#     system(hook injection) -> user correction. The correction's parent is the SYSTEM record. ---
def turn_fixture():
    return [
        {"uuid": "u_prompt", "type": "user", "userType": "external", "origin": {"kind": "human"}, "parentUuid": None,
         "message": {"content": "build the thing"}},
        {"uuid": "a_turn", "type": "assistant", "parentUuid": "u_prompt",
         "message": {"content": [{"type": "text", "text": "on it"},
                                 {"type": "tool_use", "name": "Edit"}]}},
        {"uuid": "u_toolresult", "type": "user", "parentUuid": "a_turn",
         "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"uuid": "sys_hook", "type": "system", "parentUuid": "u_toolresult",
         "content": "<system-reminder>verification trigger</system-reminder>"},
        {"uuid": "u_correction", "type": "user", "userType": "external", "origin": {"kind": "human"}, "parentUuid": "sys_hook",
         "message": {"content": "no stop, that's wrong"}},
        # non-turn noise the real stream contains
        {"type": "file-history-snapshot", "snapshot": {}},
        {"uuid": "m1", "type": "mode", "mode": "default"},
        {"type": "queue-operation", "op": "x"},
    ]


def test_reconstruct_through_system():
    p = _write(turn_fixture())
    r = fj.parse_file(p)
    check("parse ok", r.ok)
    check("non-turn counted not rejected", r.non_turn >= 3, f"non_turn={r.non_turn}")
    m = fj.reconstruct_moment("u_correction", r.by_uuid)
    check("moment reconstructed", m is not None)
    check("chain complete THROUGH system record", m and m.complete,
          "system-record parent must be traversed, not dead-end")
    check("prompt captured", m and m.prompt_text == "build the thing")
    check("correction captured", m and m.correction_text == "no stop, that's wrong")
    check("tool_use captured in moment", m and "Edit" in m.tool_uses)
    p.unlink()


def test_external_user_filter():
    # hook-text and tool_result user messages are NOT external prompts.
    #
    # NOTE ON THE FIXTURES: "a" carries NO origin, because that is what a hook-injected turn
    # actually looks like — Claude Code does not stamp machine text as human. Exclusion is now
    # decided by that absence rather than by the "<system-reminder>" prefix, which is the whole
    # point of the change: the prefix list was a blocklist that missed the scheduler prompts, and
    # a filter keyed on content prefers well-formed machine text over Nick's real messages.
    #
    # "b" DOES carry a human stamp on purpose: a tool_result must be excluded regardless of what
    # any origin field says, so this pins the precedence.
    recs = [
        {"uuid": "a", "type": "user", "userType": "external", "parentUuid": None,
         "message": {"content": "<system-reminder>hi</system-reminder>"}},
        {"uuid": "b", "type": "user", "userType": "external", "origin": {"kind": "human"}, "parentUuid": None,
         "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        {"uuid": "c", "type": "user", "userType": "external", "origin": {"kind": "human"}, "parentUuid": None,
         "message": {"content": "real prompt"}},
    ]
    idx = {r["uuid"]: r for r in recs}
    check("hook-text excluded", not fj.is_external_user(idx["a"]))
    check("tool_result-list excluded", not fj.is_external_user(idx["b"]))
    check("real prompt included", fj.is_external_user(idx["c"]))


def test_fail_closed_malformed():
    # >0.1% malformed lines -> reject
    good = [{"uuid": f"u{i}", "type": "assistant", "message": {"content": "x"}} for i in range(50)]
    p = _write(good)
    with p.open("a") as f:
        f.write("{ this is not json\n")  # 1 bad / 51 ~= 2% > 0.1%
    r = fj.parse_file(p)
    check("malformed file fails closed", not r.ok, f"ok={r.ok}")
    p.unlink()


def test_incomplete_chain():
    # correction whose parent chain never reaches a prompt -> complete=False
    recs = [
        {"uuid": "sys", "type": "system", "parentUuid": None, "content": "boot"},
        {"uuid": "u_corr", "type": "user", "userType": "external", "origin": {"kind": "human"}, "parentUuid": "sys",
         "message": {"content": "you never did that"}},
    ]
    p = _write(recs)
    r = fj.parse_file(p)
    m = fj.reconstruct_moment("u_corr", r.by_uuid)
    check("incomplete chain -> complete False (never fabricates prompt)",
          m is not None and not m.complete and m.prompt_text == "")
    p.unlink()


def test_determinism():
    p = _write(turn_fixture())
    a = fj.parse_file(p); b = fj.parse_file(p)
    ma = fj.reconstruct_moment("u_correction", a.by_uuid)
    mb = fj.reconstruct_moment("u_correction", b.by_uuid)
    check("deterministic across 2 runs", ma.tool_uses == mb.tool_uses and ma.prompt_text == mb.prompt_text)
    p.unlink()


def test_real_corpus_smoke():
    files = fj.session_jsonls()
    if not files:
        print("  SKIP  real-corpus smoke (no transcripts)")
        return
    biggest = max(files, key=lambda x: x.stat().st_size)
    r = fj.parse_file(biggest)
    check("real transcript parses", r.ok, r.error or "")
    check("real transcript has turns", len(r.by_uuid) > 0)


if __name__ == "__main__":
    for fn in [test_reconstruct_through_system, test_external_user_filter, test_fail_closed_malformed,
               test_incomplete_chain, test_determinism, test_real_corpus_smoke]:
        print(fn.__name__)
        fn()
    print(f"\n{'ALL PASS' if not _fails else 'FAILURES: ' + ', '.join(_fails)}")
    sys.exit(1 if _fails else 0)
