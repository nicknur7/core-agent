#!/usr/bin/env python3
"""Every file the capabilities inventory names must exist here, or say where it does.

WHY THIS EXISTS (2026-08-13). CLAUDE.base.md calls `memory/capabilities.md` "the live per-Core
inventory" — it is what Core reads to answer "can I do X?". Cross-checking every file path it names
against the filesystem:

    74 file references · 68 resolve · 6 did not

Five of the six were one section: the Playwright browser harnesses (`login.py`, `session.py`,
`auth-state.json`, `download-file.py`, `submit-dsgn210.py`) and the directories holding them.

    memory/education/                     life: ABSENT   core-school: present
    memory/education/canvas-automation/   life: ABSENT   core-school: present
    memory/education/outlook-automation/  life: ABSENT   core-school: present

The whole section described core-school's harnesses as life's capabilities. Reading it,
Core-on-life would tell Nick it can pull a Canvas assignment page, then discover at the tool call
that the harness is on a different seat — the failure arriving AFTER the promise, which is the order
that costs him something.

The sixth was `staleness-check.sh`, which is correctly archived on disk as
`staleness-check.sh.archived-2026-05-15` and accurately described. A false positive of the matcher,
not of the document — recorded because a 6 that is really a 5 is exactly the kind of number this
suite has spent the week refusing to report.

WHAT THIS ASSERTS. Not "every named file must exist" — a per-Core inventory legitimately references
a peer's tooling, and deleting that knowledge would be worse than mislabelling it. The assertion is
that an unresolvable reference must sit under a heading that says WHOSE it is. The document may
describe another seat; it may not describe another seat's capability as its own.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "memory" / "capabilities.md"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_capabilities_inventory_is_true")
    if not DOC.is_file():
        print(f"  SKIP  {DOC} absent — capabilities.md is per_core_keep and may not exist here")
        return 0
    text = DOC.read_text()

    # Index the real tree once. Match by relative path AND by basename, because the document names
    # some files bare (`recall-gate.py`) and others by path. Also tolerate the archive convention
    # `<name>.archived-<date>`, which is a live, accurate way to say "retired on this date".
    real_paths, real_names = set(), set()
    for p in REPO.rglob("*"):
        if not p.is_file():
            continue
        if any(x in p.parts for x in (".git", "node_modules", "__pycache__")):
            continue
        real_paths.add(str(p.relative_to(REPO)))
        real_names.add(p.name)
        if ".archived-" in p.name:
            real_names.add(p.name.split(".archived-")[0])

    def resolves(ref: str) -> bool:
        # `lstrip("./")` strips ANY leading '.' or '/' CHARACTERS, not the prefix "./" — so
        # ".mcp.json" became "mcp.json" and a real file reported as absent. Caught because the
        # only survivor of the narrowed check was a file I could see on disk. Use removeprefix,
        # which strips the string.
        r = ref[2:] if ref.startswith("./") else ref
        return (r in real_paths or Path(r).name in real_names
                or (REPO / r).exists())

    # Section a reference sits under: the nearest preceding markdown heading.
    lines = text.splitlines()
    heading_at = {}
    current = ""
    for i, ln in enumerate(lines):
        if ln.startswith("#"):
            current = ln
        heading_at[i] = current

    # EXECUTABLES AND CONFIG ONLY — .md is deliberately excluded.
    #
    # A named .py/.sh/.json is a TOOLING claim: "this seat has this and can run it." A named .md is
    # often something else entirely, and widening to it produced three immediate false positives:
    # `pricing.md` and `capital.md` appear as TARGETS a skill would be applied to ("use for pricing
    # unit econ"), not as files claimed to exist, and `memory/brain-lint-reports/YYYY-MM-DD.md` is a
    # template path with a placeholder date.
    #
    # That is the same scope problem that sank the header-vs-behaviour sweep hours earlier: telling
    # a claim-of-existence from a mention requires reading what the sentence is about. Rather than
    # attempt it, the check stays where the distinction is structural — an executable either exists
    # or the capability is broken.
    refs = []
    for i, ln in enumerate(lines):
        for m in re.finditer(r"`([\w./-]+\.(?:py|sh|json))`", ln):
            refs.append((m.group(1), heading_at[i]))

    check(f"the inventory names file paths at all ({len(refs)} references)", bool(refs),
          "no backticked file references found — either the format changed or this test is now "
          "measuring nothing")

    # An unresolvable reference is only acceptable when its heading says whose it is.
    ELSEWHERE = re.compile(r"NOT HERE|core-(school|business|finance|brain|bus)|peer-", re.I)
    orphans = [(r, h) for r, h in refs if not resolves(r) and not ELSEWHERE.search(h or "")]

    check("every unresolvable reference sits under a heading naming the seat it lives on",
          not orphans,
          "these name files that are not on this seat, under a heading that claims them as this "
          "Core's capability:\n          "
          + "\n          ".join(f"{r}   (under: {h.strip() or '<no heading>'})" for r, h in orphans))

    # And the other direction — the check must be capable of failing.
    fake = "definitely-not-a-real-file-xyz.py"
    check("the resolver would notice a genuinely absent file",
          not resolves(fake),
          "resolves() returns True for a file that does not exist, so the assertion above passes "
          "regardless of the document's accuracy")

    # ---- THE ALWAYS-LOADED STEERING SURFACE ---------------------------------------------------
    # capabilities.md is read on demand. CLAUDE.md, CLAUDE.base.md and .claude/rules/*.md are
    # loaded EVERY TURN (CLAUDE.base.md's own "Rules files" section says so and corrects an earlier
    # claim that they were on-demand). A path claim that is wrong there is wrong in every turn's
    # context, so it is the surface where this check matters most.
    #
    # Measured 2026-08-13: 18 executable references across 8 files, ZERO unresolved. Asserted rather
    # than assumed, because these files are edited often — tonight alone touched several — and the
    # cheapest moment to catch a stale path is the edit that introduces it.
    #
    # No exemption for "lives elsewhere" here, unlike capabilities.md above: a rules file naming a
    # peer's script would be steering this Core by another seat's tooling, which is a different and
    # worse problem than an inventory describing it.
    steering = [REPO / "CLAUDE.md", REPO / ".claude" / "CLAUDE.base.md",
                REPO / "tasks" / "lessons.md"]
    steering += sorted((REPO / ".claude" / "rules").glob("*.md"))
    checked = unresolved = 0
    broken: list[str] = []
    for f in steering:
        if not f.is_file():
            continue
        for m in re.finditer(r"`([\w./-]+\.(?:py|sh|json))`", f.read_text()):
            checked += 1
            if not resolves(m.group(1)):
                unresolved += 1
                broken.append(f"{f.relative_to(REPO)} -> {m.group(1)}")

    check(f"every executable path in the always-loaded steering files resolves "
          f"({checked} references)",
          not broken,
          "these are injected into EVERY turn's context, so a stale path is a standing instruction "
          "to use something that is not here:\n          " + "\n          ".join(broken))

    check("the steering surface was actually scanned (else the check above is vacuous)",
          checked > 0,
          "no executable references found in CLAUDE.md / CLAUDE.base.md / rules/*.md — either the "
          "citation style changed or the file list is wrong, and this assertion is measuring an "
          "empty set")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
