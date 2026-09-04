"""Which Core is this test measuring? Derived, never hardcoded.

WHY THIS FILE EXISTS. core-business reviewed the 2026-08-09 range and found FOUR tests in this
shared directory that hardcode `/Users/<you>/AI Projects/<core>`. bin/tests/ ships to
every Core, and `bin/tests/run-all.sh` is invoked by `.claude/hooks/session-lifecycle.sh` at every
session close. So on business, school, finance and ops, those tests:

  - MEASURED LIFE'S TREE and reported the result as their own, and
  - WROTE FILES INTO LIFE'S LIVE DIRECTORIES to do it.

The worst instance is `test_root_anchors.py`, whose entire job is catching tools that resolve the
wrong Core. Run from business it scanned life and printed "The measured number is 0", exit 0 — so
"this Core has zero vulnerable anchors" and "I audited a different Core and it happened to be zero"
produce the identical line. That zero is what I reported to Nick as measured rather than asserted.

A TEST THAT WRITES INTO ANOTHER CORE'S TREE IS NOT A TEST, IT IS A CROSS-CORE WRITE CHANNEL that
runs on a schedule.
"""
import os
import sys
from pathlib import Path


def core_root() -> Path:
    """This Core's root: DELIBERATE env override, else walk up from this file to the marker.

    core-business ASKed whether the env branch bypassing the refuse-on-mismatch check is deliberate
    (#909): it returned before script_core and cwd_core were ever computed, so the file's entire
    stated guarantee did not cover its own first branch. ANSWER: HALF DELIBERATE, and the half that
    was not is the whole finding.

    THE TWO VARIABLES ARE NOT THE SAME KIND OF THING and treating them as one `or` chain is the bug.

      CORE_INSTANCE     is set BY AN OPERATOR who means it. session-lifecycle.sh exports it from
                        `git rev-parse`. An override that can be overruled is not an override, so
                        this one wins — deliberately, and now documented as winning.

      CLAUDE_PROJECT_DIR is set BY THE RUNTIME, ambiently, on every hook and subagent invocation.
                        Nobody chose it. Letting it win reintroduces precisely the defect this file
                        was written to end: business ran life's test_root_anchors.py, measured LIFE,
                        and got a confident answer at exit 0. Through this door, a subagent or hook
                        running a PEER's copy silently measures whichever Core the runtime happens
                        to be pointed at — a wrong-Core answer wearing the right file's name.

    So the ambient variable is demoted to a HINT: honoured when it agrees with the file's own
    location, and refused when it disagrees, which is the same rule the seats already follow.
    """
    explicit = os.environ.get("CORE_INSTANCE")
    if explicit:
        p = Path(explicit).expanduser()
        if (p / ".claude" / "identity.json").is_file():
            return p.resolve()

    # ONE WALK, imported — not a fourth copy of the same four lines. The STRICT contract below
    # (refuse when the seats disagree) stays here because it is genuinely this file's, and is not
    # shared with the trajectory gate, which must be able to point deliberately at a checkout where
    # the seats always differ. Consolidating the mechanism is not the same as collapsing the
    # contracts, and collapsing them would have been the wrong fix.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core_seat import walk_up_to_core as _up

    script_core = _up(Path(__file__).resolve())
    cwd_core = _up(Path.cwd().resolve())

    # The ambient runtime variable enters HERE, as a third seat subject to the same agreement rule
    # — never as a short-circuit ahead of it.
    ambient = os.environ.get("CLAUDE_PROJECT_DIR")
    if ambient:
        a = Path(ambient).expanduser()
        if (a / ".claude" / "identity.json").is_file():
            a = a.resolve()
            if script_core and a != script_core.resolve():
                raise RuntimeError(
                    "REFUSING TO GUESS WHICH CORE TO MEASURE.\n"
                    "  this test lives in   : %s\n"
                    "  CLAUDE_PROJECT_DIR is: %s\n"
                    "The runtime sets that variable ambiently — it is not a choice to run this "
                    "file against another Core. Set CORE_INSTANCE if you mean it." % (script_core, a))
            if not script_core:
                return a

    # REFUSE WHEN THE SEATS DISAGREE. Deriving from the SCRIPT's location is right for normal
    # operation — every Core runs its own synced copy — and wrong during CROSS-CORE REVIEW, where a
    # peer runs this Core's file from their seat. That is not an edge case: it is exactly how
    # core-business ran test_root_anchors.py, measured LIFE, and got "the measured number is 0" at
    # exit 0. A wrong-Core answer is worse than a refusal, because it is a plausible answer.
    if script_core and cwd_core and script_core != cwd_core:
        raise RuntimeError(
            "REFUSING TO GUESS WHICH CORE TO MEASURE.\n"
            "  this test lives in : %s\n"
            "  you are standing in: %s\n"
            "Run that Core's own copy, or set CORE_INSTANCE explicitly." % (script_core, cwd_core))
    if script_core:
        return script_core
    raise RuntimeError(
        "cannot identify which Core this test belongs to — refusing to guess, because guessing is "
        "how a test measured another Core and reported the answer as its own")


def core_name() -> str:
    """The Core's short name. PREFIX-ANCHORED, not substring-replaced.

    `.replace("core-", "")` strips the token from ANYWHERE in the name — a directory called
    `my-core-thing` becomes `mything`, and any future Core whose name contains the substring is
    silently mangled. core-business flagged it as the FOURTH instance in one day of
    SUBSTRING WHERE EXACT IS REQUIRED, after the direction fence (twice) and the exemption check.

    THE RULE, written where it will be read: on a classification or identity path, compare the
    CAPTURED VALUE by equality or anchor at a boundary — never ask whether the surrounding text
    CONTAINS the thing. Three of the four instances passed their own tests while wrong.
    """
    name = core_root().name
    return name[len("core-"):] if name.startswith("core-") else name
