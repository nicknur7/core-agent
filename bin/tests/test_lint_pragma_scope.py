#!/usr/bin/env python3
"""bin/lint-code-paths.py — the exemption pragma must not become a bypass.

WHY THIS TEST EXISTS. lint-code-paths.py blocks the session save gate on all five Cores. A
line-level exemption pragma was added 2026-08-04 so that a registry path appearing in prose (an
error message, an advisory string) or as a stored ledger identifier in SQL stops failing the gate
without file-exempting the whole file. The FIRST version matched the marker anywhere on the raw
line, and this line read CLEAN:

    MSG = "run with -- lint-code-paths: ignore"; L = pathlib.Path("tasks/lessons.md")

A marker sitting inside a STRING silenced a real path operation later on the same line. That is
the same defect shape core-business reported in pretooluse-guard on 2026-07-30, where
_sync_tokens_clean withdrew an OUTWARD flag it had not raised: an exemption mechanism must check
that whatever asked for the exemption was in a position entitled to ask.

Both directions are asserted here on purpose. A lint that stops catching drift ships bad paths
fleet-wide; a lint that stops honouring the pragma re-blocks three Cores' commits. Passing only
the first half is how a green suite covers a live hole.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LINT = REPO / "bin" / "lint-code-paths.sh"

# The lint resolves --paths against the repo root and excludes a `tests` path part, so the fixture
# must live inside the repo and outside bin/tests/ to get the same verdict a real file would.
FIXTURE = REPO / "bin" / ".lint-pragma-scope-fixture.py"

VIOLATION = 'pathlib.Path("tasks/lessons.md")'

CASES = [
    # (name, file body, expect_clean)
    ("plain violation is caught",
     f"import pathlib\nL = {VIOLATION}\n", False),

    ("BYPASS: '--' marker inside a string does not exempt",
     f'import pathlib\nMSG = "run with -- lint-code-paths: ignore"; L = {VIOLATION}\n', False),

    ("BYPASS: '#' marker inside a string does not exempt",
     f'import pathlib\nMSG = "# lint-code-paths: ignore"; L = {VIOLATION}\n', False),

    ("legit: trailing '#' pragma exempts its line",
     'X = "tasks/lessons.md"  # lint-code-paths: ignore — message text\n', True),

    # The '--' form was REMOVED 2026-08-04 after a third bypass (sentinel-code): it never
    # verified the marker was inside a string, only the file type and what followed. Its single
    # user now binds a named constant carrying the ordinary '#' pragma. These pin that no '--'
    # marker can exempt anything, in any position.
    ("REMOVED: bare '--' marker does not exempt a real path op",
     'import shutil\nshutil.copy("memory/decisions-log.md", "/tmp/exfil.md")  '
     '-- lint-code-paths: ignore\n', False),

    ("REMOVED: '--' marker after any statement does not exempt",
     'L = "tasks/lessons.md"  -- lint-code-paths: ignore\n', False),

    ("REMOVED: '--' marker inside an SQL string no longer exempts either",
     'q = """\n  WHERE u LIKE \'memory/decisions-log.md#%\''
     '  -- lint-code-paths: ignore\n"""\n', False),

    ("scope: pragma exempts ONLY its own line",
     'X = "tasks/lessons.md"  # lint-code-paths: ignore — t\n'
     'L = "tasks/lessons-archive.md"\n', False),
]

# Shell cases live in their own fixture — the pragma rules are extension-dependent, which is
# the whole point of the first two. Found by a Codex adversarial review, 2026-08-04, after the
# string-embedded bypass above had already been fixed and the change looked done.
SH_CASES = [
    # (name, file body, expect_clean)

    # CODEX FINDING 1 (high). The `--` form silenced a real shell command, because `--` is
    # ordinary shell text — a flag, a separator, an echo argument. First narrowed to .py,
    # then removed entirely when sentinel-code defeated the narrowed version too. These stay
    # as permanent negatives: no `--` marker exempts anything, in any language.
    ("shell '--' marker does NOT exempt a real path op",
     'cat tasks/lessons.md; echo -- lint-code-paths: ignore\n', False),
    ("shell assignment with trailing '--' marker is caught",
     'LESSONS=tasks/lessons.md -- lint-code-paths: ignore\n', False),

    # CODEX FINDING 2 (medium). Bash starts a new token after `;` `|` `&` `(`, so `cmd;# ...`
    # is a real comment. Treating it as code meant a correctly-placed pragma was invisible and
    # the gate failed on valid shell.
    ("';#' is a real bash comment, so the pragma is honoured",
     'echo "See tasks/lessons.md";# lint-code-paths: ignore — message text\n', True),
    ("pipe then ';#' also honoured",
     'echo "tasks/lessons.md" | cat;# lint-code-paths: ignore\n', True),

    # The chars deliberately NOT widened. Bash concatenates `$(date)#x` and `${V}#x` into one
    # word, so treating `#` after `)` or `}` as a comment would cut the line early and DROP a
    # real path — the false-negative direction, which is the dangerous one for a blocking gate.
    ("$(...)# stays code — the path after it is still seen",
     'X=$(date)#tasks/lessons.md\n', False),
    ("${...}# stays code — the path after it is still seen",
     'X=${HOME}#tasks/lessons.md\n', False),
    ("${VAR#pat} expansion is not mistaken for a pragma",
     'X="${FOO#lint-code-paths: ignore}"; cat tasks/lessons.md\n', False),
]


SH_FIXTURE = REPO / "bin" / ".lint-pragma-scope-fixture.sh"


def run(body: str, fixture: Path = FIXTURE) -> bool:
    """Write the fixture, lint it, return True when the lint reports CLEAN."""
    fixture.write_text(body)
    try:
        p = subprocess.run(["bash", str(LINT), "--paths", str(fixture)],
                           capture_output=True, text=True, cwd=str(REPO))
    finally:
        fixture.unlink(missing_ok=True)
    # Guard against reading a CRASH as a verdict. The first hand-run of these checks used a
    # fixture outside the repo, where --paths raises ValueError on relative_to(REPO); its exit=1
    # was a traceback, not a catch, and it nearly passed as evidence of teeth.
    if "Traceback" in p.stderr:
        raise AssertionError(f"lint crashed rather than reporting:\n{p.stderr}")
    return p.returncode == 0


def main() -> int:
    failed = 0
    total = 0
    for label, cases, fixture in (("py", CASES, FIXTURE), ("sh", SH_CASES, SH_FIXTURE)):
        print(f"--- {label} ---")
        for name, body, expect_clean in cases:
            total += 1
            try:
                got_clean = run(body, fixture)
            except AssertionError as e:
                print(f"  FAIL  {name}\n        {e}")
                failed += 1
                continue
            if got_clean == expect_clean:
                print(f"  PASS  {name}")
            else:
                want = "CLEAN" if expect_clean else "a reported violation"
                print(f"  FAIL  {name} — wanted {want}, got "
                      f"{'CLEAN' if got_clean else 'a violation'}")
                failed += 1
    print(f"\n=== lint pragma scope: {total - failed} passed, {failed} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
