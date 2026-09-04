#!/usr/bin/env python3
"""A ready oracle whose EVENT no longer dispatches must defer, not route into a dead end.

WHY THIS EXISTS (2026-08-28). `enforcement_block` had produced ZERO artifacts in the lifetime of
the loop, on every seat. Not because no ask reached it — eight of life's asks did, including the
HIGHEST-SUPPORT ask in the whole corpus, "use codex alongside core for substantial system/code
work", restated by Nick ten times.

Both entries in ORACLE_CATALOG declare event "Stop". friction-dispatch's Stop registration was
retired 2026-08-06 (nothing drives the agent after the reply is sent).
friction_installer._event_is_dispatchable correctly refuses to install a block whose event does
not dispatch — so every one of those asks died at install, silently.

The perverse part, and the reason this is a bug rather than a policy outcome: one branch ABOVE,
an oracle that is NOT ready falls back to a working inject_contract. So being oracle_ready was
STRICTLY WORSE than not being ready. The readier the oracle, the worse the outcome for the ask.

Pinned here:
  1. the deferral fires while Stop is dead, and names the event so the reason is auditable
  2. it does NOT fire when the event is live — this must re-arm itself if a Stop dispatcher ever
     returns, with no edit
  3. dispatchable_events() returns None (unknown), never an empty set, when settings are
     unreadable — an empty set is indistinguishable from "nothing dispatches" and would silently
     downgrade every enforcement ask forever on a transient config error
"""
import json
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

passes: list = []
failures: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    import artifact_typer as T

    ask = "use codex alongside core for substantial system/code work"

    live = T.dispatchable_events()
    check("dispatchable_events reads a real registration",
          live is None or isinstance(live, frozenset),
          f"got {live!r}")
    print(f"\n  this seat dispatches friction-dispatch on: {sorted(live) if live else live}\n")

    # --- 1. deferral while the oracle's event is dead ---------------------------------------
    T.dispatchable_events.__defaults__[0].clear()
    T.dispatchable_events.__defaults__[0]["v"] = frozenset({"UserPromptSubmit", "PreToolUse"})
    r = T.route_type(ask, ask_type="constraint", still_recurring=True)
    check("a ready oracle on a DEAD event defers instead of dying at install",
          r["type"] == "inject_contract",
          f"routed to {r['type']!r} — that terminal cannot install, so the ask is served by nothing")
    check("the deferral names the dead event, so the reason is auditable",
          "Stop" in r.get("reason", ""),
          f"reason was {r.get('reason')!r}")

    # --- 2. it must re-arm on its own if Stop ever dispatches again --------------------------
    T.dispatchable_events.__defaults__[0]["v"] = frozenset({"UserPromptSubmit", "PreToolUse", "Stop"})
    r2 = T.route_type(ask, ask_type="constraint", still_recurring=True)
    check("with the event LIVE, enforcement is reached again (no edit required)",
          r2["type"] == "enforcement_block",
          f"routed to {r2['type']!r}; the fix must be a deferral, not a permanent disable")

    # --- 3. unknown must not read as "nothing dispatches" ------------------------------------
    T.dispatchable_events.__defaults__[0].clear()
    with tempfile.TemporaryDirectory() as td:
        broken = pathlib.Path(td) / ".claude"
        broken.mkdir()
        (broken / "settings.json").write_text("{ this is not json")
        real = T._Path
        try:
            class _Fake:
                def __init__(self, *_a, **_k): pass
                def resolve(self): return self
                @property
                def parents(self): return {2: pathlib.Path(td)}
            T._Path = lambda *a, **k: _Fake()
            got = T.dispatchable_events()
        finally:
            T._Path = real
            T.dispatchable_events.__defaults__[0].clear()
    check("unreadable settings return None (unknown), not an empty set",
          got is None,
          f"got {got!r} — an empty set would defer EVERY enforcement ask on a transient config error")

    # --- 4. the install gate still fails CLOSED on the same question -------------------------
    src = (REPO / "scheduling" / "claude-si" / "friction_installer.py").read_text()
    check("the install gate still refuses a block it cannot prove dispatches",
          "refusing a block install rather than assuming" in src,
          "this change must not have relaxed the install-side gate; the two directions are "
          "deliberately asymmetric")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
