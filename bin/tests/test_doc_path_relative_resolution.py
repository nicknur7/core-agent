#!/usr/bin/env python3
"""bin/lint-doc-paths.py — citations resolve under BOTH conventions, and neither escapes the repo.

WHY THIS EXISTS. _citation_broken() resolved candidates only against REPO and ENGINE_ROOT, never
against the directory of the file the citation was found in. A markdown link is relative to its own
file, so a correct sibling link was unresolvable BY CONSTRUCTION — not mis-tuned, structurally
impossible to satisfy. Those permanent false failures blocked the session save gate: core-business
reported four of them on 2026-08-04 while sitting on 132 uncommitted files, and school on 241.

Both directions are asserted. A doc lint that stops catching real drift lets citations rot, which
is the whole reason it exists; a doc lint that reports correct links as broken blocks a Core from
saving. The second failure mode is the one that actually cost five days.

The escape cases are here because fixing the first bug nearly created a second: the new
file-relative base was written with a containment guard while the OLD repo-relative check had
none, leaving two standards in one function. `REPO / "../../../../etc/passwd"` resolves through
`..` to a real /etc/passwd, so .exists() was True and the citation was reported as FINE. That is a
false negative rather than a read, but it is the same escape the new code already refused — so
containment now applies to every base, and these cases pin it.
"""
import glob
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
LINT = REPO / "bin" / "lint-doc-paths.py"


def load():
    spec = importlib.util.spec_from_file_location("lint_doc_paths", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # also asserts the module still IMPORTS on this Python
    return mod


def main() -> int:
    m = load()
    rel_dir = pathlib.Path("memory/relationships")

    # A real sibling of a real file, discovered rather than hardcoded: the pair core-business
    # reported does not exist on every Core, and asserting against another Core's filenames is
    # how a test passes for the wrong reason.
    sibs = sorted(glob.glob(str(REPO / "memory" / "relationships" / "*.md")))
    if len(sibs) < 2:
        print("  SKIP  fewer than 2 files in memory/relationships/ — nothing to pair")
        return 0
    sibling = pathlib.Path(sibs[1]).name

    cases = [
        # (name, cited, citing_dir, expect_broken)
        ("real sibling link is NOT broken", sibling, rel_dir, False),
        ("repo-relative citation still resolves", "memory/about-me.md", rel_dir, False),
        ("parent-relative citation resolves", "../about-me.md", rel_dir, False),
        ("missing sibling IS broken", "nope-xyz-123.md", rel_dir, True),
        ("escape 3 levels is broken", "../../../etc/passwd", rel_dir, True),
        ("escape 4 levels is broken", "../../../../etc/passwd", rel_dir, True),
        ("escape via repo base alone is broken", "../../../../etc/passwd", None, True),
    ]

    failed = 0
    for name, cited, citing_dir, expect in cases:
        got = m._citation_broken(cited, "", citing_dir)
        if got == expect:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} — broken={got}, wanted {expect}")
            failed += 1

    print(f"\n=== doc-path relative resolution: {len(cases) - failed} passed, {failed} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
