#!/usr/bin/env python3
"""ONE ANSWER TO "WHICH CORE AM I MEASURING", imported by everything that asks.

core-business, #914: `CORE_INSTANCE=<business> python3 <life's tree>/bin/casebook-run.py` made
T11/T12/T13 report ERROR on its seat. Reproduced here and it is worse than the error it reported —
on the AUTHOR's seat the same command does not fail at all:

    CORE_INSTANCE      = …/core-business
    runner REPO        = …/core-business          <- graded business's files
    predicates TRANSCR = …/projects/…-core-life   <- measured LIFE's behaviour
    SAME CORE?         = False

One run, two Cores, one blended verdict, exit 0. That is the enforcement-audit defect exactly, and
it is the invocation the runner's own docstring calls the preferred one — the form a peer uses to
review this Core's work.

THE CAUSE IS NOT A BUG IN EITHER RESOLVER. Both were individually correct. There were three of them:

    bin/casebook-run.py::_repo_root()          env-first, __file__ fallback
    bin/casebook_predicates.py::_transcripts_dir()  walks up from __file__, never reads the env
    bin/tests/_core.py::core_root()            env-first, refuses when the seats disagree

business's diagnosis is the useful part and it is aimed at me: its ORIGINAL defect was
_transcripts_dir() hardcoded to its own path, which I fixed by DERIVING. But I derived by a second
mechanism instead of using the one already standing beside it — "fixing hardcoded by writing a
second resolver is the two-implementations defect you named yourself in Tier B."

There were two slug functions too, and nobody had noticed because they agree on today's paths:

    casebook_predicates:  s.replace("/", "-").replace(" ", "-")
    si-objective:         re.sub(r"[^A-Za-z0-9]", "-", s)

Identical for `/Users/…/core-life`. Divergent the moment a Core path contains a dot, an underscore
or any other punctuation — and the failure mode is a directory that does not exist, which reads as
an empty history rather than a bad path.

WHAT THIS MODULE DOES NOT DO: collapse two genuinely different contracts into one. The test helper
must REFUSE when the script's Core and the cwd's Core disagree; the trajectory gate must be able to
point deliberately at a materialised checkout where they always disagree. Those stay separate and
call `seat_root()` for the part that is actually common — resolving a root — while the transcript
slug is derived from WHATEVER root the caller settled on, never rediscovered independently.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_MARKER = Path(".claude") / "identity.json"


def walk_up_to_core(start: Path) -> Path | None:
    """Nearest ancestor of `start` holding a Core identity marker, else None."""
    start = Path(start).resolve()
    for cand in [start, *start.parents]:
        if (cand / _MARKER).is_file():
            return cand
    return None


def seat_root(fallback: Path | None = None) -> Path:
    """The Core root this process should act on: explicit env, else the caller's fallback.

    CORE_INSTANCE is honoured first because a caller that sets it KNOWS — the trajectory gate
    pointing at a materialised checkout, a peer reviewing this Core's work. CLAUDE_PROJECT_DIR is
    accepted second and is a weaker signal: the runtime sets it ambiently on every hook and subagent
    invocation, so nobody chose it. Callers needing the stricter contract (refuse when the seats
    disagree) build on walk_up_to_core() instead — see bin/tests/_core.py.
    """
    for var in ("CORE_INSTANCE", "CLAUDE_PROJECT_DIR"):
        val = os.environ.get(var)
        if not val:
            continue
        p = Path(val).expanduser()
        # THE MARKER, AND ONLY THE MARKER. This read `or (p / "bin").is_dir()`, and the right
        # disjunct defeated the left one entirely — core-business demonstrated it rather than
        # arguing it:
        #
        #     CORE_INSTANCE=/usr/local       marker=False  bin/=True   -> ADOPTED
        #     CORE_INSTANCE=/opt/homebrew    marker=False  bin/=True   -> ADOPTED
        #
        # Any directory on the machine with a bin/ subdirectory qualified as a Core. The comment in
        # bin/core_paths.py that delegates here says seat_root "requires the identity marker"; it
        # did not, and the case that FIXED reported clean was rejected only incidentally, because
        # <core>/memory happens to have no bin/.
        #
        # Deleting the disjunct is the whole fix. All five Cores carry the marker — checked — so it
        # was load-bearing for nothing, including peer-review checkouts, which are full copies.
        if (p / _MARKER).is_file():
            return p.resolve()
        # AN EXPLICIT CORE_INSTANCE THAT CANNOT BE ADOPTED MUST REFUSE, NOT FALL THROUGH.
        #
        # Falling through means grading a DIFFERENT tree than the caller named, silently. The
        # trajectory gate materialises a candidate arm and points CORE_INSTANCE at it; if that arm
        # lacks the marker, the loop below falls back to Path(__file__).parents[1] — the FROZEN
        # tree — and the frozen tree gets graded as the candidate. A candidate diff that deletes
        # .claude/identity.json therefore makes its own arm unadoptable and is graded as the thing
        # it was supposed to be compared against. Found by a Fable review, relayed by core-business
        # (bus #1058), verified here: the file is tracked and was in no fence.
        #
        # CLAUDE_PROJECT_DIR is deliberately NOT covered by this refusal: the runtime sets it
        # ambiently on every hook invocation, so nobody chose it, and refusing on it would break
        # every hook that runs outside a Core. CORE_INSTANCE is only ever set on purpose, which is
        # exactly why ignoring it silently is the dangerous case.
        if var == "CORE_INSTANCE":
            raise RuntimeError(
                "CORE_INSTANCE=%s does not contain %s — refusing to fall back to a different tree. "
                "A caller that sets CORE_INSTANCE has named the seat it means; silently grading "
                "another one is how a frozen evaluator ends up scoring itself as the candidate."
                % (val, _MARKER))
    if fallback is not None:
        return Path(fallback).resolve()
    found = walk_up_to_core(Path(__file__))
    if found:
        return found
    raise RuntimeError(
        "cannot identify which Core to act on — refusing to guess. Set CORE_INSTANCE, or run from "
        "inside a Core (a directory tree containing .claude/identity.json)")  # lint-code-paths: ignore — error-message text, not a path op


def core_name(root: Path | None = None) -> str:
    """Short Core name. PREFIX-ANCHORED — `core-` is stripped only from the front.

    `.replace("core-", "")` strips the token from ANYWHERE: `my-core-thing` becomes `mything`, and
    a fork laid out as `riverside-core` comes back unchanged rather than loudly unresolved. Fourth
    instance of SUBSTRING WHERE EXACT IS REQUIRED in one day when business found it.
    """
    name = Path(root or seat_root()).name
    return name[len("core-"):] if name.startswith("core-") else name


def transcripts_dir(root: Path) -> Path:
    """Claude Code's session directory FOR THE GIVEN ROOT — derived, never rediscovered.

    Takes the root as an argument on purpose. Every caller has already decided which Core it is
    measuring; a function that re-derives that decision can disagree with it, which is precisely how
    one run graded business's files while measuring life's behaviour.

    Claude Code slugs a project path by replacing each non-alphanumeric character with a dash.
    Missing the SPACE (paths here live under "AI Projects") yields a directory that does not exist
    and a tidy "no transcripts found" — an empty history rather than a bad path, which is a
    fail-toward-PASS.
    """
    return (Path.home() / ".claude" / "projects"
            / re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve())))


def assert_same_seat(a: Path, b: Path, what_a: str = "root", what_b: str = "root") -> None:
    """REFUSE when two parts of one run resolved to different Cores.

    The backstop for the class this module exists to end. It is not defence in depth for its own
    sake: a single resolver can still be handed two roots by a caller, and a blended verdict at
    exit 0 is worse than any error, because it is plausible.
    """
    if Path(a).resolve() != Path(b).resolve():
        raise RuntimeError(
            "REFUSING TO REPORT A BLENDED RESULT — two parts of this run resolved to different "
            "Cores.\n  %s: %s\n  %s: %s\nOne run must measure one Core." % (what_a, a, what_b, b))
