#!/usr/bin/env python3
"""The baseline digest must bind the DESTINATION path of a rename, not just the source.

THE ATTACK (found by core-business 2026-08-12, reproduced here before the fix):

    git mv docs/note.md .claude/hooks/evil.sh
    git diff --cached --raw
      :100644 100644 a4fc220 a4fc220 R100   docs/note.md   .claude/hooks/evil.sh

Rename detection collapses a move into ONE row whose $6 is the SOURCE path. The old formula
`awk '{print $4, $6}'` therefore digested `a4fc220 docs/note.md` — so a reviewer approves a digest
naming a DOCS file while the content lands in `.claude/hooks/`, the trust root.

At the old 1800s TTL the exposure window was short. The change this guards was extending the TTL to
24h once approvals bind to content; without this fix, a stale approval for an innocuous docs edit
would authorise a file arriving in the trust root a day later. Step 1 (bind the digest) had to be
provably correct before step 2 (extend the TTL), and it was not.

WHY `--no-renames` AND NOT `-M0`: business proposed `-M0`. Running it showed `-M0` is rename
detection at a 0 PERCENT similarity threshold — more aggressive, not disabled — and it still emitted
the single R100 row carrying only the source path. `--no-renames` splits a move into
`D <source>` + `A <destination>`, so both paths reach the reviewer. Right diagnosis, wrong flag, and
the difference was visible only by executing it. That is why this file tests the FORMULA'S BEHAVIOUR
against a planted attack rather than grepping sync-to-baseline.sh for a flag name.

Scratch repos only. Never touches the real repo or the baseline.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SYNC = REPO / "bin" / "sync-to-baseline.sh"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else chr(10) + '          ' + detail}")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True,
    ).stdout


def live_formula() -> str:
    """The digest formula as it exists in the shipped script, extracted verbatim.

    Reading it from source rather than hardcoding it means this test cannot silently pass against a
    formula that has since been weakened — the thing under test is what actually ships.
    """
    src = SYNC.read_text()
    m = re.search(r"SYNC_DIGEST_INPUT=\$\((.*?)\)\s*$", src, re.S | re.M)
    if not m:
        return ""
    # Drop shell line-continuations FIRST. Collapsing whitespace alone leaves a bare `\` token
    # sitting mid-command, which then escapes the following space and silently produces an empty
    # result — the test fails while the code under test is fine. Cost me a false red here.
    return " ".join(m.group(1).replace("\\\n", " ").split())


def digest_input(repo: Path, formula: str) -> str:
    return subprocess.run(["bash", "-c", formula], cwd=repo,
                          capture_output=True, text=True).stdout


def scratch() -> Path:
    d = Path(tempfile.mkdtemp(prefix="digest-rename-"))
    git(d, "init", "-q")
    (d / "docs").mkdir()
    (d / ".claude" / "hooks").mkdir(parents=True)
    (d / "docs" / "note.md").write_text("harmless note\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "init")
    return d


def main() -> int:
    print("test_digest_binds_rename")
    formula = live_formula()
    if not formula:
        print("  FAIL  could not extract SYNC_DIGEST_INPUT formula from bin/sync-to-baseline.sh")
        return 1

    # --- 1. THE ATTACK: a move into the trust root must expose the destination -------------
    d = scratch()
    try:
        git(d, "mv", "docs/note.md", ".claude/hooks/evil.sh")
        git(d, "add", "-A")
        out = digest_input(d, formula)
        check("a rename into .claude/hooks/ exposes the DESTINATION path",
              ".claude/hooks/evil.sh" in out,
              f"digest input was {out.strip()!r} — a reviewer would approve the source path while "
              f"the content lands in the trust root")
        check("the rename still names the source path too (delete half is visible)",
              "docs/note.md" in out,
              f"digest input was {out.strip()!r}")

        # The regression this replaces: prove the OLD formula was genuinely vulnerable, so this
        # test is known to discriminate rather than passing for an unrelated reason.
        old = digest_input(d, "git diff --cached --raw | awk '{print $4, $6}' | sort")
        check("CONTROL — the pre-fix formula really was blind to the destination",
              ".claude/hooks/evil.sh" not in old,
              "the old formula already exposed the destination, so this test proves nothing")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # --- 2. A chmod with identical content must change the digest input --------------------
    d = scratch()
    try:
        # STAGE THE FILE FIRST, at 100644. core-business caught that the earlier version took
        # `before` on a CLEAN tree, so it was the empty string and `after != before` was trivially
        # true — the assertion read as though it proved mode-sensitivity while the whole weight sat
        # on `"100755" in after`. A comparison against nothing is not a comparison. Same shape as
        # every other instrument this week: it reported a property it was not measuring.
        (d / "docs" / "note.md").write_text("changed content\n")
        git(d, "add", "-A")
        before = digest_input(d, formula)
        assert "100644" in before, "fixture precondition: the file must be staged at 100644 first"

        (d / "docs" / "note.md").chmod(0o755)
        git(d, "add", "-A")
        after = digest_input(d, formula)
        check("a chmod +x with identical content changes the digest input",
              after.strip() != before.strip() and "100755" in after and "100644" in before,
              f"before={before.strip()!r} after={after.strip()!r} — two pushes differing only in "
              f"whether a script is executable would be indistinguishable to the reviewer")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # --- 3. A delete must not read as a modify --------------------------------------------
    d = scratch()
    try:
        git(d, "rm", "-q", "docs/note.md")
        out = digest_input(d, formula)
        check("a delete carries the D status letter", " D " in f" {out} ",
              f"digest input was {out.strip()!r} — a delete indistinguishable from a modify")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
