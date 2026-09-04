#!/usr/bin/env python3
"""A generated SKILL.md must carry an activation condition that can actually match.

WHY THIS EXISTS (2026-08-28). The operator's point: a skill that needs to be used means Core
knowing to use it, not the operator having to type it. Every skill the loop had ever promoted ended with

    Use when the task at hand is exactly this; generated from 6 observed uses.

which is circular. A skill is selected by matching its DESCRIPTION against the situation, so
"use this when the task is this" gives the matcher nothing to work with. All four skills promoted
before this date were unfireable.

Nothing noticed, for the same reason the tier looked healthy: a graduated skill is a SECOND,
EARLIER surface, and the hook it graduates from keeps firing (promote() never deactivates the
artifact). The capability still worked at Edit-time; only the planning-time surface was dead. So
the tier's own evidence — fires, sessions, span — stayed green while its output was inert, and no
test read the description at all.

Second, related defect pinned here: `_BROAD_DESC` refuses the word "proactively", which is the
vocabulary a description needs in order to fire. Generator and guard must not contradict, or
promotion can only ever emit something the guard would reject.

The procedure body already renders a correct condition under "## When this fires", derived from
the artifact's `condition` block. These checks pin that it reaches the front matter.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

passes: list = []
failures: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


BODY = """# execute the plan fully

## When this fires

You are about to change a file (Edit / Write / MultiEdit / NotebookEdit).

## Do

- execute the plan fully
"""
EV = {"fires": 6, "sessions": 4, "span_days": 9.0}


def main() -> int:
    import skill_graduate as sg

    desc, text = sg._render("x", "execute the plan fully", BODY, "art_x", EV)
    print(f"\n  rendered description:\n    {desc}\n")

    check("description is not circular",
          "task at hand is exactly this" not in desc.lower(),
          "a skill cannot be selected on a description that only points back at itself")

    check("description carries the body's real activation condition",
          "about to change a file" in desc.lower(),
          f"the '## When this fires' section did not reach the front matter: {desc!r}")

    check("generator and broadness guard do not contradict",
          not sg._BROAD_DESC.search(desc),
          f"the generator emits a description its own guard refuses, so nothing can promote: {desc!r}")

    check("description still leads with the ask",
          desc.lower().startswith("execute the plan fully"),
          "the ask is what a reader matches on first")

    d2, _ = sg._render("x", "do the thing", "# t\n\n## Do\n\n- x\n", "art_x", EV)
    check("falls back cleanly when the body has no '## When this fires'",
          bool(d2) and "do the thing" in d2,
          f"got {d2!r}")

    # --- names ------------------------------------------------------------------------------
    # 'keep-architecture-diagram-documentatio' and 'autonomously-detect-recurring-frustrat' are
    # both live skill directories whose names end mid-word, because the old slug sliced at byte
    # 38 without regard to where the words were.
    for ask in ("keep the architecture diagram/documentation in sync with the actual system",
                "autonomously detect recurring frustrations and encode them as hooks"):
        name = sg._slug(ask)
        ok_name = bool(sg._NAME_RE.match(name or ""))
        check(f"name is usable: {name!r}", ok_name, "failed _NAME_RE")
        words = set(w for w in re.split(r"[^a-z0-9]+", ask.lower()) if w)
        check(f"name is cut on a word boundary: {name!r}",
              ok_name and name.split("-")[-1] in words,
              f"last segment {name.split('-')[-1]!r} is not a whole word from the ask")

    # --- promotion identity is the ARTIFACT, not the filename -------------------------------
    # I CAUSED THIS ON 2026-08-28 AND THE FIX ABOVE IS WHAT CAUSED IT. promote() skipped an
    # artifact whose NAME was already claimed, which held only while _slug was stable. Fixing
    # _slug to cut on word boundaries changed two names, both sailed past the name check, and the
    # same artifact promoted a SECOND time:
    #     art_hs695c70b4eec100ce -> autonomously-detect-recurring-frustrat  AND
    #                            -> autonomously-detect-recurring
    # Two directories, one procedure, identical descriptions — the model sees one capability twice
    # and demotion evidence splits across both, each with half the usage. The duplicates were
    # archived; this pins the rule that let them exist.
    src = (REPO / "scheduling" / "claude-si" / "skill_graduate.py").read_text()
    check("promote() dedupes on artifact_id, not just on name",
          "already = promoted_artifact_ids()" in src and "if aid in already:" in src,
          "a name-only check breaks the moment the slug function changes")
    check("promoted_artifact_ids reads the GEN_MARKER, the same identity demote() uses",
          "GEN_MARKER not in head" in src and "artifact=(art_" in src,
          "promotion and demotion must agree on what identifies a generated skill")

    ids = sg.promoted_artifact_ids()
    check("every generated skill on THIS seat maps to a distinct artifact",
          len(ids) == len([d for d in (REPO / ".claude" / "skills").iterdir()
                           if (d / "SKILL.md").is_file()
                           and sg.GEN_MARKER in (d / "SKILL.md").read_text(errors="replace")[:2000]]),
          f"{len(ids)} distinct artifacts across the generated skill dirs — a mismatch means one "
          f"artifact owns two directories again")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
