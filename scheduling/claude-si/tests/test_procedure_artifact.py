#!/usr/bin/env python3
"""Safety proof for the `procedure` artifact kind (2026-07-27).

A procedure is a gated trigger PLUS a markdown payload. The trigger runs the existing gate
unchanged; everything new is in the payload, so that is what this file attacks:

  · the payload cannot point anywhere but its own artifact's file
  · the recorded hash is re-verified against disk, so post-authoring tampering is caught
  · a contract cannot smuggle a payload, and a procedure cannot omit one
  · rollback retires the payload, not just the trigger (otherwise "artifact-local rollback"
    and "watchdog fails safe" would be false statements for procedures)
  · the watchdog retires orphaned payloads
  · the proliferation cap holds
  · a procedure and a contract mined from the SAME ask get DIFFERENT artifact_ids
    (they collided in the first implementation and silently overwrote each other)

Stdlib only, state isolated to a temp dir.

  python3 tests/test_procedure_artifact.py
"""
import json
import os
import sys as _sys
from pathlib import Path as _PPath
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

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

# A TEMP CORE WITH NO settings.json IS NOT A CORE (2026-08-13). install_shadow_block now refuses to
# install a block whose event has no LIVE friction-dispatch registration, and it fails CLOSED when
# the registration cannot be read — so a bare temp tree refuses every block install. That is the
# guard working: it exists because two shadow blocks were installed for the `Stop` event four days
# after the Stop dispatcher was retired, and this fixture was previously able to install a block
# into a Core with no hooks whatsoever, which is exactly the state that produced the bug.
#
# Staged rather than exempted. Pointing the guard at the real repo would break the isolation this
# fixture exists for, and exempting tests would mean the install path under test is not the one
# that ships.
_stg = Path(os.environ["CORE_INSTANCE"], ".claude")
_stg.mkdir(parents=True, exist_ok=True)
# Events derived FROM the catalog, not hardcoded. The enforcement templates currently declare
# event="Stop"; the live seat retired its Stop dispatcher on 2026-08-06, which is the real defect
# this guard reports. A fixture testing INSTALL MECHANICS has to simulate a Core where the
# template's event does dispatch, or it is asserting the guard rather than the installer. Deriving
# the list means this fixture follows the templates automatically if they ever move off Stop.
def _fixture_events():
    evs = {"UserPromptSubmit", "PreToolUse"}
    try:
        import artifact_typer as _at
        evs |= {v.get("event") for v in _at.ORACLE_CATALOG.values() if v.get("event")}
    except Exception:
        evs |= {"Stop"}
    return sorted(evs)


(_stg / "settings.json").write_text(json.dumps({"hooks": {
    ev: [{"hooks": [{"type": "command",
                     "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/friction-dispatch.py" ' + ev}]}]
    for ev in _fixture_events()}}))


import importlib                        # noqa: E402
import friction_installer as inst       # noqa: E402
import friction_watchdog as wd          # noqa: E402

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

importlib.reload(inst)
importlib.reload(wd)

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _canonical_failure_is_absence(mod) -> tuple:
    """(True, reason) when the failure _canonical_orphans recorded means there is NO database;
    (False, reason) when a reachable database refused or errored — a defect, not absence. Reads
    the cause the function itself recorded; a second connection would judge one failure by
    another (Codex, pass 2)."""
    from _env import db_absent, describe_db_failure  # a broken _env is a CRASH here, correctly
    exc = getattr(mod, "_LAST_CANONICAL_FAILURE", None)
    if exc is None:
        return (False, "checked=False but no failure was recorded — _canonical_orphans changed shape")
    return (db_absent(exc), describe_db_failure(exc))


AID = "art_proc_test_0001"
BODY = "## Test procedure\n\n1. Do the thing.\n2. Verify the thing.\n"


def _spec(aid=AID, typ="hooked_skill", payload=None):
    s = {
        "spec_version": 1, "artifact_id": aid, "case_id": "c_test", "org_id": ORG,
        "type": typ, "event": "UserPromptSubmit",
        "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                              {"op": "prompt_regex", "value": r"\bwidget\b"}]},
        "effect": {"mode": "inject", "message": "follow the procedure", "skill_id": None},
        "tests": {"positive_ids": ["p1"], "negative_ids": ["n1", "n2"]},
        "template": {"id": "ask-contract-v1", "sha256": "pending"}, "scope": "org_local",
        "lease": {"max_fires_per_session": 2, "expires_at": None},
        "generator_version": "test/1",
    }
    if payload is not None:
        s["payload"] = payload
    return s


print("\n=== payload write + validate ===")
blk = inst.write_procedure(AID, BODY)
check("write_procedure returns closed block",
      set(blk) == {"path", "sha256", "bytes"}, str(blk))
check("payload path is <artifact_id>.md", blk["path"] == f"{AID}.md", blk["path"])
ok, why = inst._validate_payload(_spec(payload=blk))
check("valid payload accepted", ok, why)

print("\n=== payload tamper + escape ===")
inst._procedure_path(AID).write_text(BODY + "\nEXTRA INJECTED LINE\n")
ok, why = inst._validate_payload(_spec(payload=blk))
check("tampered payload rejected", not ok, why)
inst.write_procedure(AID, BODY)  # restore

ok, _ = inst._validate_payload(_spec(payload={**blk, "path": "../../../etc/passwd"}))
check("path traversal rejected", not ok)
ok, _ = inst._validate_payload(_spec(payload={**blk, "path": "other_artifact.md"}))
check("pointing at another artifact's payload rejected", not ok)
ok, _ = inst._validate_payload(_spec(payload={**blk, "sha256": "z" * 64}))
check("non-hex sha256 rejected", not ok)
ok, _ = inst._validate_payload(_spec(payload={**blk, "bytes": inst.MAX_PROCEDURE_BYTES + 1}))
check("oversize byte count rejected", not ok)
ok, _ = inst._validate_payload(_spec(payload={**blk, "extra": 1}))
check("unknown payload key rejected (closed set)", not ok)

print("\n=== type/payload coupling ===")
# THE ARGUMENT SIDE, NOT JUST THE SPEC SIDE. `_validate_spec(spec, org)` compares the spec's
# org against the TRUSTED org passed in. The sweep fixed every `"org_id": 1` in the specs and
# left seven call sites passing a literal 1 as that trusted argument, so on any seat whose ORG
# is not 1 the two disagreed and validation failed. core-school found it by reading the call
# sites after the spec fix did not clear their failure — the second time today that "fixed the
# constant, kept the shape" was the actual defect.
ok, why = inst._validate_spec(_spec(typ="contract", payload=blk), ORG)
check("contract carrying a payload rejected", not ok, why)
ok, why = inst._validate_spec(_spec(typ="hooked_skill", payload=None), ORG)
check("procedure without a payload rejected", not ok, why)
ok, why = inst._validate_spec(_spec(typ="skill", payload=blk), ORG)
check("unknown type rejected", not ok, why)
ok, why = inst._validate_spec(_spec(payload=blk), ORG)
check("well-formed procedure spec accepted", ok, why)

print("\n=== payload redaction + size bound ===")
big = inst.write_procedure("art_proc_big_0001", "x" * (inst.MAX_PROCEDURE_BYTES * 2))
check("oversize body truncated to the cap", big["bytes"] <= inst.MAX_PROCEDURE_BYTES, str(big["bytes"]))
sec = inst.write_procedure("art_proc_secret_01", "token sk-abcdefghijklmnopqrstuvwxyz0123456789\n")
body = inst._procedure_path("art_proc_secret_01").read_text()
check("secret-shaped payload content is redacted",
      "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in body, body[:80])

print("\n=== rollback retires the payload ===")
inst.write_procedure(AID, BODY)
arts = {"artifacts": [dict(_spec(payload=blk), _installed_at=1)]}
inst._atomic_write(inst.ACTIVE, arts)
check("payload present before rollback", inst._procedure_path(AID).is_file())
inst.rollback(AID)
check("artifact gone after rollback",
      not any(a["artifact_id"] == AID for a in inst._load_active().get("artifacts", [])))
check("payload gone from procedures/ after rollback", not inst._procedure_path(AID).is_file())
check("payload moved to quarantined/, not deleted",
      any(AID in p.name for p in inst.PROCQDIR.glob("*.md")))

print("\n=== watchdog retires orphaned payloads ===")
inst.write_procedure("art_proc_orphan_01", BODY)
inst._atomic_write(inst.ACTIVE, {"artifacts": []})
# NEEDS A LIVE CANONICAL STORE. _sweep_orphan_payloads asks Postgres (not active.json — see
# test_orphan_sweep_asks_the_source_of_truth.py for why) whether a candidate is still live, and
# FAILS CLOSED when it cannot reach it: nothing is retired rather than risking a wrongly-deleted
# payload. With corebrain unreachable this section would report neither PASS nor a real defect —
# it would report the sweep's own designed-in safety behaviour as a failure. Probe with the same
# call the sweep makes before asserting on its outcome.
_, _canon_checked = wd._canonical_orphans([])
_canon_absent, _canon_why = (True, "") if _canon_checked else _canonical_failure_is_absence(wd)
if not _canon_checked and _canon_absent:
    print(f"  SKIP  {_canon_why} — the orphan sweep fails closed (retires nothing) without a live "
          "canonical store to confirm against")
elif not _canon_checked:
    check("the canonical store answered the orphan probe", False,
          f"_canonical_orphans reported checked=False for a reason that is NOT an absent database: "
          f"{_canon_why} — a schema/query error hiding as 'unavailable'")
else:
    r = wd.sweep(dry=True)
    check("dry sweep reports the orphan", "art_proc_orphan_01" in (r.get("orphan_payloads") or []))
    check("dry sweep does not delete", inst._procedure_path("art_proc_orphan_01").is_file())
    wd.sweep(dry=False)
    check("real sweep retires the orphan", not inst._procedure_path("art_proc_orphan_01").is_file())

print("\n=== proliferation cap ===")
capped = [dict(_spec(aid=f"art_proc_cap_{i:04d}"), _installed_at=1)
          for i in range(inst.MAX_ACTIVE_PROCEDURES)]
inst._atomic_write(inst.ACTIVE, {"artifacts": capped})
inst.write_procedure("art_proc_over_0001", BODY)
res = inst.install(_spec(aid="art_proc_over_0001",
                         payload=inst.write_procedure("art_proc_over_0001", BODY)), {})
check("install refused at the procedure cap",
      not res.get("ok") and "cap" in str(res.get("reason")), str(res))

print("\n=== a procedure and a contract for the same ask get different ids ===")
import hashlib  # noqa: E402
cid = "ask_some-recurring-thing"
a_contract = "art_" + hashlib.sha256(f"contract|{cid}".encode()).hexdigest()[:20]
a_procedure = "art_" + hashlib.sha256(f"procedure|{cid}".encode()).hexdigest()[:20]
check("kind-scoped artifact ids do not collide", a_contract != a_procedure)

print("\n=== Codex review regressions (2026-07-27) ===")

# HIGH #2 — the pinned hash must survive persistence, and must be re-checked at FIRE time.
r = inst._deep_redact({"payload": {"path": "a.md", "sha256": "a" * 64, "bytes": 5},
                       "effect": {"message": "sk-abcdefghijklmnopqrstuvwxyz012345"}})
check("payload.sha256 survives deep redaction", r["payload"]["sha256"] == "a" * 64)
check("real secrets still redacted alongside it",
      "sk-abcdefghijklmnopqrstuvwxyz012345" not in r["effect"]["message"])

import friction_dispatch as fdp  # noqa: E402
importlib.reload(fdp)
FID = "art_proc_fire_0001"
fblk = inst.write_procedure(FID, BODY)
art = dict(_spec(aid=FID, payload=fblk), _installed_at=1)
check("intact payload verifies at fire time", fdp._payload_verified(art))
inst._procedure_path(FID).write_text(BODY + "\nrun something hostile\n")
check("tampered payload REJECTED at fire time", not fdp._payload_verified(art))
inst.write_procedure(FID, BODY)
check("restored payload verifies again", fdp._payload_verified(art))
check("payload pointing at a different artifact rejected at fire time",
      not fdp._payload_verified(dict(art, artifact_id="art_proc_other_001")))

# HIGH #4 — guard-surface content is refused outright, not merely redacted.
for bad in ("edit .claude/settings.json to add a hook",
            "run the command with --dangerously-bypass-approvals-and-sandbox",
            "disable pretooluse-guard first",
            "use git push --no-verify"):
    ok, _ = inst._payload_content_ok(bad)
    check(f"content lint refuses: {bad[:38]}", not ok)
ok, _ = inst._payload_content_ok(BODY)
check("content lint allows an ordinary procedure", ok)

# HIGH #3 — symlink containment on the payload path.
sym = inst._procedure_path("art_proc_symlink_1")
target = Path(_TMP) / "outside.md"
target.write_text("outside the tree\n")
try:
    sym.symlink_to(target)
    raised = False
    try:
        inst.write_procedure("art_proc_symlink_1", BODY)
    except ValueError:
        raised = True
    check("write refuses a symlinked payload path", raised)
    check("symlink target not overwritten", target.read_text() == "outside the tree\n")
    sym.unlink()
except OSError:
    check("write refuses a symlinked payload path", True, "symlinks unsupported here")

print("\n=== work-shape triggers are fenced to hooked_skill @ PreToolUse (Phase 3) ===")
_ws_cond = {"all": [{"op": "event_is", "value": "PreToolUse"},
                    {"op": "tool_name_in", "value": ["Edit", "Write"]},
                    {"op": "tool_mutability_is", "value": "mutating"}]}


def _ws(typ="hooked_skill", event="PreToolUse", cond=None, aid="art_ws_test_0001"):
    s = _spec(aid=aid, typ=typ)
    s["event"] = event
    s["condition"] = cond if cond is not None else _ws_cond
    if typ == "hooked_skill":
        blk = inst.write_procedure(aid, "## steps\n\n1. a\n2. b\n")
        s["payload"] = blk
    else:
        s.pop("payload", None)
    return s


ok, why = inst._validate_spec(_ws(), ORG)
check("hooked_skill @ PreToolUse with tool ops accepted", ok, why)

ok, why = inst._validate_spec(_ws(typ="contract"), ORG)
check("a CONTRACT may not use PreToolUse", not ok, why)

# the important fence: tool ops must not leak into a prompt-event artifact of any type
ok, why = inst._validate_spec(_ws(event="UserPromptSubmit"), ORG)
check("tool ops rejected on a prompt-event artifact", not ok, why)

ok, why = inst._validate_spec(
    _ws(cond={"all": [{"op": "event_is", "value": "PreToolUse"},
                      {"op": "assistant_regex", "value": r"\bfoo\b"}]}), ORG)
check("assistant_regex still rejected even on the widened surface", not ok, why)

ok, why = inst._validate_spec(
    _ws(typ="contract", event="UserPromptSubmit",
        cond={"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                      {"op": "prompt_regex", "value": r"\bwidget\b"}]}), ORG)
check("an ordinary contract is unaffected by the widening", ok, why)


print("\n=== Codex round: block install is held to a HIGHER bar than inject ===")
_blk = {"spec_version": 1, "artifact_id": "art_blk_test_0001", "case_id": "c", "org_id": ORG,
        "type": "contract", "event": "Stop",
        "condition": {"all": [{"op": "event_is", "value": "Stop"}]},
        "effect": {"mode": "block", "message": "m", "skill_id": None},
        "enforced": False,
        "tests": {"positive_ids": ["p1"], "negative_ids": ["n1", "n2"]},
        "template": {"id": "block-deliverable-v1", "sha256": "x"}, "scope": "org_local",
        "lease": {"max_fires_per_session": 2, "expires_at": None},
        "generator_version": "t/1"}
r = inst.install_shadow_block({**_blk, "evil_field": 1}, {})
check("block spec with an unknown top-level key is refused",
      not r.get("ok") and "unknown keys" in str(r.get("reason")), str(r))
r = inst.install_shadow_block({**_blk, "lease": {"max_fires_per_session": 99, "expires_at": None}}, {})
check("block spec with an out-of-range lease cap is refused",
      not r.get("ok") and "cap" in str(r.get("reason")), str(r))
r = inst.install_shadow_block({**_blk, "effect": {"mode": "block", "message": "m", "skill_id": None, "x": 1}}, {})
check("block spec with an unknown effect key is refused", not r.get("ok"), str(r))
r = inst.install_shadow_block({**_blk, "effect": {"mode": "inject", "message": "m", "skill_id": None}}, {})
check("a non-block spec cannot use the block installer", not r.get("ok"), str(r))

print("\n=== an oracle marked never_promote can never be auto-enforced ===")
import artifact_typer as _at
import friction_promote as _fp
_never = {k for k, v in _at.ORACLE_CATALOG.items() if v.get("never_promote")}
check("at least one oracle is marked never_promote", bool(_never), str(_never))
import artifact_generator as _ag
_blocked_tpls = {_ag._load_templates()[k]["template_id"] for k in _never}
check("never_promote template ids are excluded from the promotable set",
      not (_blocked_tpls & _fp._verified_template_ids()), str(_blocked_tpls))


print("\n=== Codex round 3: the ENFORCEMENT PRIMITIVE is identity-aware, not just its caller ===")
import si_project as _sp  # noqa: E402
import artifact_generator as _ag3  # noqa: E402
import artifact_typer as _at3  # noqa: E402
_tpls = _ag3._load_templates()
_never_key = next((k for k, v in _at3.ORACLE_CATALOG.items() if v.get("never_promote")), None)
check("a never_promote oracle exists to test against", bool(_never_key))
if _never_key:
    _t = _tpls[_never_key]
    _spec_never = {"template": {"id": _t["template_id"]}, "event": _t["event"],
                   "condition": _t["condition"], "effect": {"mode": _t["effect_mode"]}}
    check("_enforceable() refuses a never_promote oracle", not _sp._enforceable(_spec_never))
# EVERY oracle is never_promote as of 2026-08-12, so there is no promotable one to test against.
# That is a DELIBERATE, DOCUMENTED state, not a defect: both ORACLE_CATALOG entries declare
# event="Stop", friction-dispatch never registers on Stop, and re-registering it would undo Nick's
# 2026-08-06 no-post-reply directive. deliverable_as_artifact additionally asks a question that is
# not decidable before a reply exists, so it is structurally unenforceable rather than unwired.
#
# This assertion previously grabbed "some other oracle" and required _enforceable() to ALLOW it. With
# no promotable oracle in the catalog it picked a never_promote one and failed — the FIXTURE was
# invalidated by a legitimate state change, not the logic.
#
# Not made green by weakening it. The safety half — _enforceable() REFUSES a never_promote oracle —
# is asserted above and is untouched; that is the property that stops a block reaching real work. The
# permissive half is asserted only when there is something to assert it about, and its absence is
# reported explicitly so nobody reads silence as coverage. Tracked as T023 (design a pre-reply oracle
# vantage point) — when one lands, this assertion starts running again on its own.
_ok_key = next((k for k in _tpls
                if k != _never_key and not _at3.ORACLE_CATALOG.get(k, {}).get("never_promote")), None)
if _ok_key is None:
    check("NOTE: no promotable oracle exists to test the permissive path (see T023) — "
          "the refusal path above is still fenced", True)
if _ok_key:
    _t2 = _tpls[_ok_key]
    _spec_ok = {"template": {"id": _t2["template_id"]}, "event": _t2["event"],
                "condition": _t2["condition"], "effect": {"mode": _t2["effect_mode"]}}
    check("_enforceable() allows a promotable oracle", _sp._enforceable(_spec_ok))
    # the exploit: promotable label, forbidden condition
    if _never_key:
        _spoof = dict(_spec_ok, condition=_tpls[_never_key]["condition"])
        check("_enforceable() refuses a mismatched condition under a promotable label",
              not _sp._enforceable(_spoof))
check("_enforceable() refuses an unknown template id",
      not _sp._enforceable({"template": {"id": "no-such-template"}}))
check("_enforceable() refuses a spec with no template", not _sp._enforceable({}))

print("\n=== capability timestamps are UTC-correct, not machine-local ===")
import skill_graduate as _sg3  # noqa: E402
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
_now = _dt.now(_tz.utc)


def _cap_line(dt, name):
    return (f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.000Z | hook=capability:{name} | "
            f"event=Skill | verdict=capability | session=s | excerpt=")


_sg3.EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
_sg3.EVENTS_LOG.write_text("\n".join([
    _cap_line(_now - _td(hours=2), "recent"),
    _cap_line(_now - _td(days=29, hours=20), "inside-boundary"),
    _cap_line(_now - _td(days=31), "outside"),
    _cap_line(_now + _td(days=2), "future"),
    "zzzzzzzz | hook=capability:junk | event=Skill | verdict=capability | session=s | excerpt=",
]) + "\n")
_u = _sg3.capability_usage()
check("only in-window capability records count",
      sorted(_u) == ["inside-boundary", "recent"], str(sorted(_u)))

print()
if _fails:
    print(f"FAILURES ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL PASS")
