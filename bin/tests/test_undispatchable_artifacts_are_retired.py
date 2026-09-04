#!/usr/bin/env python3
"""An artifact that CANNOT fire must not stay active.

WHY THIS EXISTS (2026-08-28). The watchdog had two sweeps for broken pairings — a payload with no
artifact, and an artifact with no payload — and none for the third case: an artifact that is whole
and correct and simply has NOWHERE TO FIRE.

Measured on core-life: of 38 active artifacts, 13 had never fired once, and exactly one was
structurally incapable of ever firing — art_331154505c87c73ffffe on event "Stop", whose
friction-dispatch registration was retired 2026-08-06. friction_installer NAMES this artifact in a
comment as "still ACTIVE and can never fire on any turn". It then stayed live for another
seventeen days, because naming a defect in a comment retires nothing.

It counted toward `active` and toward every health readout the whole time — which is the precise
shape of Nick's standing complaint: the loop builds a thing, reports it as working, and the thing
cannot run.

THE DANGEROUS DIRECTION IS OVER-FIRING, and it is what most of this file tests. This sweep decides
to RETIRE based on a config read. Two ways that goes wrong, both of which would wipe the corpus in
a single pass:
  · settings.json unreadable        -> dispatchable_events() returns None
  · settings.json parses but empty  -> returns an empty frozenset (mid-sync, fresh clone before
                                       reconcile-hooks, mid-edit)
Both mean "cannot judge", and the rule the other two sweeps already follow is that what cannot be
judged is not acted on.
"""
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

passes: list = []
failures: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


ARTS = {
    "art_live":  {"artifact_id": "art_live",  "event": "UserPromptSubmit", "type": "contract"},
    "art_live2": {"artifact_id": "art_live2", "event": "PreToolUse",       "type": "hooked_skill"},
    "art_dead":  {"artifact_id": "art_dead",  "event": "Stop",             "type": "contract"},
    "art_noev":  {"artifact_id": "art_noev",  "type": "contract"},
}


def main() -> int:
    import artifact_typer as at
    import friction_watchdog as w

    real_fn = at.dispatchable_events

    def with_events(val):
        """Replace the FUNCTION, not its cache.

        Seeding the cache only works for a value; to simulate the UNREADABLE case the function has
        to actually return None, and clearing the cache just recomputes it from this seat's real
        settings.json — which is readable, so the None branch was never exercised. That made the
        most safety-critical check in this file silently vacuous on its first run.
        """
        at.dispatchable_events = lambda *_a, **_k: val

    # --- the case it exists for -------------------------------------------------------------
    with_events(frozenset({"UserPromptSubmit", "PreToolUse"}))
    got = w._sweep_undispatchable_events(dict(ARTS), dry=True)
    ids = {a for a, _ in got}
    check("an artifact on a dead event is identified", ids == {"art_dead"},
          f"identified {ids!r}; expected exactly {{'art_dead'}}")
    check("the reason names the event and what DOES dispatch",
          bool(got) and "Stop" in got[0][1] and "UserPromptSubmit" in got[0][1],
          f"reason was {got[0][1] if got else None!r}")
    check("an artifact with NO event declared is left alone",
          "art_noev" not in ids,
          "no event is 'cannot judge', not 'dead' — the other sweeps own malformed specs")

    # --- the two ways this could wipe the corpus ---------------------------------------------
    with_events(None)                      # unreadable settings
    check("unreadable settings retire NOTHING",
          w._sweep_undispatchable_events(dict(ARTS), dry=True) == [],
          "a config read failure must never look like 'no event dispatches'")

    with_events(frozenset())               # parses, but no dispatcher registered
    check("an EMPTY live set retires NOTHING",
          w._sweep_undispatchable_events(dict(ARTS), dry=True) == [],
          "mid-sync / fresh-clone settings would otherwise retire every artifact on the seat")

    # --- it must be reversible, like the sweeps beside it -------------------------------------
    src = (REPO / "scheduling" / "claude-si" / "friction_watchdog.py").read_text()
    fn = src.split("def _sweep_undispatchable_events")[1].split("\ndef ")[0]
    check("retires via inst.rollback, the same reversible path the other sweeps use",
          "inst.rollback(aid, reason=reason)" in fn,
          "a hand-rolled removal would lose the reason, which IS the evidence for why it went")
    check("dry=True performs no write",
          "if not dry:" in fn and fn.index("if not dry:") < fn.index("inst.rollback"),
          "rollback must sit under the dry guard")

    # --- and sweep() must actually report it ---------------------------------------------------
    check("sweep() reports the new class in its result",
          '"undispatchable": undispatchable' in src,
          "a sweep whose result is not reported is the dead-counter defect this file is about")
    # NO LIVE sweep() CALL HERE. The first version of this file ended with w.sweep(dry=True) on the
    # real seat to prove the key is reported. run-all.sh flagged it as a LEAK: `dry` suppresses the
    # WRITES this module makes, but a full sweep still touches the action log and the DB through the
    # helpers it calls, so "dry" is not the same as "inert". test_tests_do_not_write_live_state
    # exists for exactly this, and it caught me. The reporting contract is checked against the
    # source above; the behaviour is checked against the stub corpus, which touches nothing.
    with_events(frozenset({"UserPromptSubmit", "PreToolUse"}))
    check("both return paths of sweep() carry the key",
          src.count('"undispatchable": undispatchable') == 2,
          "the early return (no artifacts left) and the main return must BOTH report it, or the "
          "class vanishes from the readout exactly when a sweep emptied the corpus")

    at.dispatchable_events = real_fn
    at.dispatchable_events.__defaults__[0].clear()
    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
