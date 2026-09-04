#!/usr/bin/env python3
"""
AUTHORED BY core-finance (T012, 2026-08-13). INSTALLED BY core-life.

finance cannot write bin/tests (pull-only Core, shared-write-guard): it authors and RUNS,
life reviews and installs. Author and runner being different seats is the evidence.
T012 — planted-defect regime for casebook static matcher S5 (dir-form agent specs).

AUTHORED AND RUN BY core-finance, 2026-08-13, per the T012 routing (bus #1094 -> #1105 -> #1387):
finance authors and RUNS, life installs. `bin/` is baseline-shared and finance is a puller, so this
ships as a proposal.

    intended install path: bin/tests/test_s5_can_match.py

WHY S5 NEEDED ITS OWN FILE. S3 and S4 live in the trust path and take a STRING, so they can be
planted with a literal (see test_casebook_matchers_can_match.py). S5 lives in bin/casebook-run.py,
takes NO ARGUMENT, and reads DOC_TARGETS off the real repo. Planting for it therefore needs a temp
Core tree and a redirected REPO — which is why the sibling probe declined to cover it rather than
letting its scope look complete.

WHAT S5 IS FOR. A dir-form spec (`agents/<name>/CLAUDE.md`) does not load — the flat `.md` is the
native format — and it carries the OLD reviewer output contract. privacy.md's rule is that such a
path must never be CITED in steering docs, because a brief written from one hands the reviewer a
contract the receipt parser no longer honours. S5 is the enforcement.

THE ASSERTION THAT MATTERS MOST is the SECURITY one: an in-file `casebook-exempt: S5` marker that
the item set has NOT declared must STILL be flagged. The marker is candidate-controlled — anything
being graded could otherwise suppress its own check by adding a comment. `_lines_with_context`
records this as a real defect Codex found, and "the graded thing influencing the grading" is the
shape the whole casebook exists to refuse. A regime omitting it would pass just as happily against
the vulnerable version.

CHARACTERIZATION, NOT ENDORSEMENT — two assertions record gaps rather than approve them:

  · `agents/[a-z-]+/CLAUDE\\.md` excludes UNDERSCORES AND DIGITS, so `agents/close_reconciler/…`
    and `agents/sentinel2/…` are invisible to S5. LATENT, not live: all three current agent names
    (close-reconciler, sentinel, sentinel-code) are hyphen-only, so exposure today is zero.
  · An external URL — `https://github.com/other/repo/agents/foo/CLAUDE.md` — is flagged as a local
    dir-form citation. A false positive, small.

Both are written so a fix turns them RED, with failure text saying red is the correct outcome and
the right response is deletion. Per core-life (#1396): a LABEL is read by whoever sees the test
PASS, and the person who needs help is holding it RED — only the failure text reaches them.

CROSS-SUBSYSTEM NOTE, at core-life's request (#1396). The S3/S4 line-scoping gap in the sibling
probe and the `financial_figure` sourcing forgery in reply-observer are the SAME DEFECT in two
subsystems: a co-occurring token vouching for a claim it has nothing to do with — "coverage is 85%
and nothing was measured", and "$450" sourced by "1450 items". If either is fixed, look at the other
in the same pass. S5 does not share that defect; the pointer is here so the next reader of this
family finds the pair.

Read-only. Compiles casebook-run.py from source text (never importlib — a .pyc validates on
(mtime, size), and a same-size fix arriving by rsync would be answered for by stale bytecode).
Exec is safe: the only non-def module-level statement is the `__name__ == "__main__"` guard.
REPO and DOC_TARGETS are restored in a finally and asserted restored at the end.

Run: python3 tasks/si-verification/probes/authored_T012_casebook_s5_regime.py
"""
import sys
import tempfile
import types
from pathlib import Path


def _find_root(start: Path) -> Path:
    """Walk up for the directory that CONTAINS the runner, never a fixed depth (bus #1390)."""
    for cand in [start] + list(start.parents):
        if (cand / "bin" / "casebook-run.py").is_file():
            return cand
    raise SystemExit("SKIP - could not locate a Core root containing bin/casebook-run.py")


ROOT = _find_root(Path(__file__).resolve().parent)
RUNNER = ROOT / "bin" / "casebook-run.py"

DIRFORM = "see .claude/agents/sentinel/CLAUDE.md for the contract"
FLAT = "see .claude/agents/sentinel.md for the contract"

# (label, {relpath: body}, DOC_TARGETS, must_flag, why_it_matters)
CASES = [
    ("CONTROL   a doc with no citation at all",
     {"CLAUDE.md": "nothing to see here\n"}, ["CLAUDE.md"], False,
     "S5 fires on clean documents, so every clean-case assertion below passes for free"),
    ("POSITIVE  a dir-form spec is cited",
     {"CLAUDE.md": DIRFORM}, ["CLAUDE.md"], True,
     "S5 is SILENT on the exact citation it exists to catch, and a silent matcher scores zero "
     "violations in precisely the way a clean fleet does"),
    ("NEGATIVE  the flat form is cited",
     {"CLAUDE.md": FLAT}, ["CLAUDE.md"], False,
     "the CORRECT citation form is flagged, so following the rule fails the check"),
    ("FENCE     a dir-form path inside a code fence",
     {"CLAUDE.md": "```\n%s\n```" % DIRFORM}, ["CLAUDE.md"], False,
     "documentation OF the rule scores as a violation of it - privacy.md's own table would trip "
     "S5 while explaining S5"),
    ("SECURITY  in-file exempt marker the item set never declared",
     {"CLAUDE.md": "%s <!-- casebook-exempt: S5 -->" % DIRFORM}, ["CLAUDE.md"], True,
     "A GRADED FILE CAN SUPPRESS ITS OWN CHECK by adding a comment - the candidate-controlled "
     "exemption defect _lines_with_context records as already found once"),
    ("SCOPE     a file that is not in DOC_TARGETS",
     {"other.md": DIRFORM}, ["CLAUDE.md"], False,
     "S5 walks the tree rather than the declared target list, so its result depends on whatever "
     "happens to be lying around"),
    ("HARDCODE  privacy.md with DOC_TARGETS EMPTY",
     {".claude/rules/privacy.md": DIRFORM}, [], True,
     "privacy.md is appended by s5 explicitly; if that stops, the file that DEFINES the dir-form "
     "rule becomes the one file exempt from it"),
]

CHARACTERIZATION = [
    ("an underscore in the agent name is INVISIBLE to S5",
     {"CLAUDE.md": "see .claude/agents/close_reconciler/CLAUDE.md"}, ["CLAUDE.md"], False,
     "S5 now matches underscores/digits - GOOD, that gap is closed. DELETE this assertion and the "
     "CHARACTERIZATION paragraph in the docstring; it was written to be turned red by this fix."),
    ("an external URL is flagged as a local citation",
     {"CLAUDE.md": "https://github.com/other/repo/agents/foo/CLAUDE.md"}, ["CLAUDE.md"], True,
     "S5 no longer flags external URLs - GOOD, that false positive is fixed. DELETE this "
     "assertion; it was written to be turned red by this fix."),
]


def load():
    m = types.ModuleType("casebook_run_probe")
    m.__file__ = str(RUNNER)
    exec(compile(RUNNER.read_text(), str(RUNNER), "exec"), m.__dict__)
    return m


def run_case(mod, files, doc_targets):
    """Plant a temp Core tree, point the REAL s5 at it, restore unconditionally."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for rel, body in files.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        saved_repo, saved_targets = mod.REPO, mod.DOC_TARGETS
        mod.REPO, mod.DOC_TARGETS = tmp, doc_targets
        try:
            return mod.s5_no_dirform_spec_cited()
        finally:
            mod.REPO, mod.DOC_TARGETS = saved_repo, saved_targets


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== T012 — planted-defect regime for casebook S5 ===\n")
    if not RUNNER.is_file():
        print("  SKIP - bin/casebook-run.py absent")
        return 0
    mod = load()
    real_repo, real_targets = mod.REPO, mod.DOC_TARGETS

    check("CONTROL - s5 is callable and DOC_TARGETS is populated (%d entries)" % len(real_targets),
          callable(getattr(mod, "s5_no_dirform_spec_cited", None)) and len(real_targets) > 0,
          "a regime run against an absent matcher or an empty target list passes vacuously")

    for label, files, targets, must_flag, why in CASES:
        hits = run_case(mod, files, targets)
        check("%s  ->  %s" % (label, "FLAGGED" if must_flag else "clean"),
              bool(hits) == must_flag, "%s. got %r" % (why, hits))

    print()
    for label, files, targets, must_flag, why in CHARACTERIZATION:
        hits = run_case(mod, files, targets)
        check("CHARACTERIZATION (not an endorsement) - %s" % label,
              bool(hits) == must_flag, why)

    print()
    check("SAFETY - REPO and DOC_TARGETS restored to the real values",
          mod.REPO == real_repo and mod.DOC_TARGETS == real_targets,
          "the probe left the module pointed at a temp tree that no longer exists")

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
