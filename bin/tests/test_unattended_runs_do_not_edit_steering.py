#!/usr/bin/env python3
"""A run with nobody present must not rewrite CLAUDE.md.

WHY THIS EXISTS (2026-08-28). Found by core-school (bus #5707), independently confirmed within
minutes by ops, business and finance. The chain, all of it real:

    bin/si-drain.sh turn_the_crank      (03:10 LaunchAgent, no human)
      -> friction_loop.run()
        -> artifact_generator.generate()
          -> route "claude_md_directive"
            -> auto_apply_directive()  -> os.replace onto REPO/CLAUDE.md

Nick approved auto_apply_directive on 2026-08-17 for SESSION CLOSE — a session running, him
reachable, the result in front of him. On 2026-08-28 I added turn_the_crank(), which made the same
path reachable unattended on five seats.

THE PART THAT IS MINE. I asked Nick whether the Cores could "install and retire their own artifacts
nightly, with nobody watching." He said yes. He was never told it also rewrites CLAUDE.md — the
steering file that loads on EVERY turn. school's framing is exact: "Nick approved the mechanism does
not carry over to Nick approved it firing at 03:10 while he is asleep." The consent was real; the
description I obtained it on was incomplete.

The capability is not removed — it is withheld from the context Nick did not authorise and kept in
the one he did, and the proposal is RETURNED rather than dropped so nothing is silently lost.
"""
import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

passes: list = []
failures: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


CASE = {"canonical_ask": "a probe directive that must never reach CLAUDE.md",
        "case_id": "probe_unattended", "support": {"count": 9}}

# SEAT-RESOLVED, NOT org 1. bin/tests/ is shared and ships to every Core and to forks, so a literal
# 1 here aims a peer's test at life's partition — the same reason test_quarantine_is_durable.py
# carries this note. Caught by test_org_is_single_sourced.py on the very run that added this file.
# auto_apply_directive does not use org for the write (it targets REPO/CLAUDE.md), but a literal
# that happens to be harmless today is still the pattern that stops being harmless later.
def _org() -> int:
    from _env import get_org_id
    return get_org_id()


def main() -> int:
    import artifact_generator as ag

    claude_md = REPO / "CLAUDE.md"
    before = claude_md.read_bytes() if claude_md.is_file() else b""

    prior = os.environ.get("CORE_SI_UNATTENDED")
    try:
        os.environ["CORE_SI_UNATTENDED"] = "1"
        r = ag.auto_apply_directive(_org(), CASE)
    finally:
        os.environ.pop("CORE_SI_UNATTENDED", None)
        if prior is not None:
            os.environ["CORE_SI_UNATTENDED"] = prior

    check("an unattended run WITHHOLDS the CLAUDE.md write",
          r.get("action") == "directive_withheld_unattended",
          f"got action={r.get('action')!r} — the 03:10 job would edit steering with nobody present")

    check("CLAUDE.md is byte-identical after the attempt",
          (claude_md.read_bytes() if claude_md.is_file() else b"") == before,
          "the withhold returned the right action but something still wrote the file")

    check("the withheld proposal is SURFACED, not dropped",
          bool(r.get("proposed")),
          "withholding must not lose the finding — an unattended run still has to say what it "
          "would have written, or the capability degrades into silence")

    # --- the guard must sit ABOVE every write, not beside one caller ------------------------
    src = (REPO / "scheduling" / "claude-si" / "artifact_generator.py").read_text()
    fn = src[src.index("def auto_apply_directive("):]
    fn = fn[:fn.index("\ndef ")] if "\ndef " in fn else fn
    gpos = fn.find('CORE_SI_UNATTENDED')
    wpos = fn.find('_os.replace')
    check("the guard precedes the write inside auto_apply_directive",
          gpos != -1 and wpos != -1 and gpos < wpos,
          "a gate placed after the write is not a gate — the 2026-08-20 lesson from the budget "
          "check that guarded a door nobody used")

    # --- and the drain must actually set it ---------------------------------------------------
    drain = (REPO / "bin" / "si-drain.sh").read_text()
    check("bin/si-drain.sh marks its crank as unattended",
          "CORE_SI_UNATTENDED=1" in drain,
          "the guard is inert unless the unattended caller declares itself")
    m = re.search(r"CORE_SI_UNATTENDED=1[^\n]*\"\$PY_BIN\"", drain)
    check("it is set on the SAME invocation that runs friction_loop",
          m is not None,
          "exporting it elsewhere in the script would not reach the python subprocess that calls "
          "friction_loop.run()")

    # --- a session close must be UNAFFECTED ---------------------------------------------------
    close = (REPO / "bin" / "core-si-close.py").read_text()
    check("a session close does NOT set the flag (Nick's approved context is preserved)",
          "CORE_SI_UNATTENDED" not in close,
          "this must withhold from the unattended context only; close is what Nick approved on "
          "2026-08-17 and must keep working")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
