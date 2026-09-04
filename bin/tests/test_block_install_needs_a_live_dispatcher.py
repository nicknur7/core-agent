#!/usr/bin/env python3
"""A block artifact may not be installed for an event nothing dispatches.

WHY THIS EXISTS (2026-08-13).

`artifact_typer.ORACLE_CATALOG` holds both enforcement templates, and `install_shadow_block` is
documented as "the ONLY path that installs a block artifact" — so the catalog is the only route to
an enforceable block anywhere in the system. **Both templates declare `event: "Stop"`.**

The Stop registration of `friction-dispatch` was RETIRED 2026-08-06, under Nick's policy that
nothing may drive the agent after the reply is sent. `bin/hook-registry.json` tombstones it properly
and the reasoning is sound. `.claude/settings.json` registers the dispatcher on `PreToolUse` and
`UserPromptSubmit` only, and `stop-hook.sh` does not invoke it.

Four days later, on 2026-08-10, two shadow blocks were installed from those templates anyway:

    shadow_block_install events : 6      (2 artifacts x 3 revisions)
    shadow_block events         : 0      — and zero is the only reachable value
    friction_promote input      : empty by construction

`art_331154505` is still ACTIVE and cannot fire on any turn of any session.

**This was not a validation bypass.** `install_shadow_block` checks the event against a hash-pinned
template and did so correctly. The template was stale. That distinction is the whole lesson: a
check that compares against a *stored copy* of the truth cannot notice the truth moved, which is the
same defect as grading an artifact against its own generated prose, one layer down.

WHAT THIS ASSERTS. That the installer consults the LIVE hook registration rather than any stored
list — including this test's. A hardcoded set of allowed events here would be a fourth place
recording which events dispatch (after settings.json, hook-registry.json, and the templates), and
would go stale exactly as the template did.
"""
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_block_install_needs_a_live_dispatcher")
    try:
        import friction_installer as fi
    except Exception as exc:
        check("friction_installer imports", False, str(exc))
        print(f"\n{len(passes)} passed, {len(failures)} failed")
        return 1

    check("the installer exposes a dispatchability check",
          hasattr(fi, "_event_is_dispatchable"),
          "no _event_is_dispatchable — the guard is gone, and a block can again be installed for "
          "an event with no dispatcher")
    if not hasattr(fi, "_event_is_dispatchable"):
        print(f"\n{len(passes)} passed, {len(failures)} failed")
        return 1

    # ---- AGAINST THE LIVE REGISTRATION ---------------------------------------------------------
    settings = REPO / ".claude" / "settings.json"
    live = set()
    if settings.is_file():
        conf = json.loads(settings.read_text())
        for ev, groups in (conf.get("hooks") or {}).items():
            for g in groups or []:
                for hk in (g.get("hooks") or []):
                    if "friction-dispatch" in str(hk.get("command", "")):
                        live.add(ev)

    check(f"this seat registers the dispatcher somewhere ({sorted(live)})",
          bool(live),
          "no friction-dispatch registration found in settings.json — either the dispatcher was "
          "removed entirely or the command string changed, and every assertion below is vacuous")

    for ev in sorted(live):
        ok, why = fi._event_is_dispatchable(ev)
        if not ok:
            check(f"a LIVE event ({ev}) is accepted", False,
                  f"refused with: {why}\n          the guard rejects an event this seat actually "
                  "dispatches on, which would block every legitimate block install")
            break
    else:
        check(f"every live event is accepted ({len(live)} checked)", bool(live))

    ok, why = fi._event_is_dispatchable("Stop")
    check("Stop is refused — the case this exists for",
          (not ok) if "Stop" not in live else True,
          f"Stop was accepted while absent from the live registration {sorted(live)}. Both "
          "ORACLE_CATALOG templates declare event=Stop, so accepting it re-opens the exact hole: "
          "artifacts installed into an event with no dispatcher, and a promote lifecycle whose "
          "input is empty by construction")
    if not ok:
        check("the refusal explains itself to whoever hits it",
              "dispatch" in why.lower() or "registration" in why.lower(),
              f"reason is not actionable: {why!r}")

    ok, _ = fi._event_is_dispatchable("SessionStart")
    check("an event with a real hook but no DISPATCHER is refused",
          not ok,
          "SessionStart runs hooks on this seat, but friction-dispatch is not one of them. "
          "Accepting it would mean the check is testing 'is this a known event' rather than "
          "'does the dispatcher run here'")

    for bad in (None, "", 123, []):
        ok, _ = fi._event_is_dispatchable(bad)
        if ok:
            check(f"a malformed event ({bad!r}) is refused", False,
                  "a block spec with no usable event was accepted")
            break
    else:
        check("malformed events are refused", True)

    # ---- FAIL-CLOSED, which is the half that matters for a BLOCK -------------------------------
    # If the registration cannot be read, refusing is required: a config read failure must not be
    # indistinguishable from a live dispatcher. Same rule install_shadow_block already applies to
    # specificity ("unprovable specificity must fail closed").
    real_state = fi.STATE
    try:
        fi.STATE = Path("/nonexistent-path-for-this-test-xyz") / ".claude" / "state"
        ok, why = fi._event_is_dispatchable("UserPromptSubmit")
        check("an UNREADABLE registration refuses the install (fails CLOSED)",
              not ok,
              "the guard returned OK when it could not read settings.json — so a missing or "
              "corrupt config silently re-enables every event, which is the failure mode a block "
              "guard must never have")
        if not ok:
            check("the fail-closed reason says it could not verify, not that the event is wrong",
                  "cannot read" in why.lower() or "refus" in why.lower(),
                  f"reason misattributes the cause: {why!r} — an operator would go looking at the "
                  "event instead of at the config")
    finally:
        fi.STATE = real_state

    # and prove the restore worked, so a later assertion is not reading a broken module
    ok, _ = fi._event_is_dispatchable("UserPromptSubmit")
    check("the module still works after the fail-closed probe",
          ok or "UserPromptSubmit" not in live,
          "STATE was not restored — this test corrupted the module it is checking")

    # ---- AND THE GUARD MUST BE WIRED, not merely present ---------------------------------------
    # Everything above tests the helper in isolation. A helper that is never CALLED passes all of
    # it while the hole stays open — the inert-instrument shape this suite exists to refuse. Read
    # by AST over the source, not by regex: a mention in a docstring or a comment is not a call.
    import ast
    src = (REPO / "scheduling" / "claude-si" / "friction_installer.py").read_text()
    tree = ast.parse(src)
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "install_shadow_block"), None)
    check("install_shadow_block exists to be checked", target is not None,
          "no install_shadow_block in friction_installer.py — it was renamed or removed, and this "
          "test is now guarding nothing")
    if target is not None:
        called = {n.func.id for n in ast.walk(target)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        check("install_shadow_block CALLS the dispatchability check",
              "_event_is_dispatchable" in called,
              "the guard is defined but never invoked from the install path. Every assertion above "
              "would still pass, and a block artifact could again be installed for a dead event — "
              "which is precisely how the Stop templates got in.")

        # and it must run BEFORE the expensive work, or a dead-event install still does the gating
        lines = [n.lineno for n in ast.walk(target)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_event_is_dispatchable"]
        gate_lines = [n.lineno for n in ast.walk(target)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr in ("gate", "upsert")]
        check("the check runs before the gate/persist work",
              bool(lines) and (not gate_lines or min(lines) < min(gate_lines)),
              f"dispatchability checked at line(s) {lines}, gate/persist at {gate_lines} — a dead "
              "event would pay for corpus fetching and oracle gating before being refused")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
