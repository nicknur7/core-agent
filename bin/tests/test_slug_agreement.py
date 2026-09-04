#!/usr/bin/env python3
"""EVERY COPY OF THE TRANSCRIPT SLUG MUST AGREE WITH THE CANONICAL ONE.

Consolidating resolvers into bin/core_seat.py on 2026-08-10 fixed three call sites and core-business
found a fourth still live an hour later (#924 BLOCK 3): "the fix consolidated three and left a
fourth." Sweeping properly turned up ELEVEN implementations of the same two-line computation, in
Python and in bash, across hooks and bin.

They all agree on today's paths, which is why nobody noticed. They diverge on any path containing a
dot or an underscore:

    /Users/x/AI Projects/core_life.bak
      two-char version   -Users-x-AI-Projects-core_life.bak      <- directory does not exist
      canonical          -Users-x-AI-Projects-core-life-bak

And the failure mode is the dangerous direction: a slug pointing at a non-existent directory yields
"no transcripts found", which every caller reads as an EMPTY HISTORY rather than as a bad path. That
is a fail-toward-PASS, and this repo has shipped it before — correction-rate-clean.py reported an
implausible zero and was caught only because the number was too clean to believe.

WHY THIS IS A TEST AND NOT AN IMPORT. A shell hook cannot import a Python module, and a PreToolUse
hook must not depend on bin/ existing at the moment it fires — that is the same reasoning that put
casebook_matchers.py beside the hooks rather than in bin/. So the copies stay, and DRIFT IS PREVENTED
BY MEASUREMENT INSTEAD: every implementation is executed against the same adversarial paths and
required to produce the identical answer.

Run: python3 bin/tests/test_slug_agreement.py
"""
import pathlib
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
sys.path.insert(0, str(ROOT / "bin"))
from core_seat import transcripts_dir  # noqa: E402

# Paths chosen to separate the implementations, not to be realistic. Every one is a legal directory
# name, and the last three are the shapes a backup, a fork, or a dated checkout actually produces.
# NO REAL CORE NAME IN A FIXTURE. The property under test is about PUNCTUATION, not identity, and a
# test file naming a Core is indistinguishable from a hardcoded dependency on it —
# test_no_cross_core_paths caught this file on its first run, as it caught my hash-parity fixture
# this morning. The real Core's own path is exercised separately, via ROOT.
PATHS = [
    "/Users/u/AI Projects/checkout",
    "/Users/u/AI Projects/checkout_two.bak",
    "/Users/u/AI Projects/checkout.2026-08-10",
    "/work/some-tree",
    "/Users/u/My Docs/a+b",
]


def canonical(p: str) -> str:
    return transcripts_dir(p).name


def bash_slug(script: Path, p: str):
    """Extract the sed program a shell script uses and run it on the given path."""
    m = re.search(r"pwd \| sed '([^']+)'", script.read_text())
    if not m:
        return None
    r = subprocess.run(["sed", m.group(1)], input=p, text=True, capture_output=True, timeout=30)
    # scripts that prepend a literal dash are handled by comparing the tail
    return r.stdout.strip()


def main() -> int:
    p = f = 0
    abstain = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== transcript slug agreement ===\n")

    print("--- the canonical implementation resolves THIS Core to a directory that exists ---")
    # NOT A CODE PROPERTY — A FIXTURE. `~/.claude/projects/<slug>/` is created by the Claude Code
    # CLI itself, the first time an interactive session actually runs with this exact path as its
    # cwd. Nothing in this repo provisions it, so a standalone clone exercised via subprocess/import
    # (a fork, a CI checkout, this suite's own audit tooling — never a real `claude` session against
    # ROOT) legitimately has no such directory yet. Every OTHER check in this file proves the slug
    # FORMULA is correct by comparing implementations against each other; only this one line depends
    # on a directory the formula's correctness cannot conjure into existing. Abstain, don't fail.
    real_dir = transcripts_dir(ROOT)
    if real_dir.is_dir():
        check("core_seat.transcripts_dir points at a real directory", True)
    else:
        print("  UNDECIDABLE  core_seat.transcripts_dir points at a real directory\n"
              "          no dir at %s — no live claude session has run against this exact path "
              "yet; the slug FORMULA is proven correct by every check below, only its live "
              "existence is unverifiable here" % real_dir)
        abstain += 1

    print("\n--- shell copies must produce the identical slug ---")
    for rel in (".claude/hooks/get-session-start-time.sh", "bin/compute-session-duration.sh"):
        script = ROOT / rel
        if not script.is_file():
            check("%s exists" % rel, False, "missing")
            continue
        bad = []
        for path in PATHS:
            got, want = bash_slug(script, path), canonical(path)
            if got is None:
                bad.append("%s: could not extract a sed program" % rel)
                break
            if got.lstrip("-") != want.lstrip("-"):
                bad.append("%s\n            got  %s\n            want %s" % (path, got, want))
        check("%s agrees with core_seat on all %d paths" % (rel, len(PATHS)),
              not bad, "\n          ".join(bad[:3]))

    # ── DISCOVER NEW COPIES, do not enumerate the known ones ───────────────────────────────────
    #
    # Everything above pins a HARDCODED LIST — two named .sh files, and .claude/hooks/*.py below.
    # That cannot find a copy nobody has added to the list, and on 2026-08-13 one had survived
    # since before the 08-10 consolidation: `.claude/commands/health.md` still built the transcript
    # path with `sed 's|^/||; s|[/ ]|-|g'`, the exact divergent form whose correction is recorded in
    # the two .sh files' own comments. It was invisible to BOTH slug tests — this one enumerates,
    # and the ratchet scans rglob("*.py"). A command file is neither.
    #
    # This file's docstring says "DRIFT IS PREVENTED". It was prevented for the copies known when it
    # was written, and described as if it covered the class.
    #
    # So: a TEXT scan for the divergent program across every file type. It cannot verify a slug is
    # correct — that is what the behavioural checks above are for — but it can prove the known-wrong
    # one is absent, which is the half a list cannot do.
    print("\n--- no file anywhere still carries the DIVERGENT sed ---")
    DIVERGENT = "s|[/ ]|-|g"
    offenders = []
    # `fp`, not `p` — `p` is this file's module-level PASS COUNTER, which check() increments.
    # Shadowing it made the next check() raise TypeError on PosixPath + int. A loop variable that
    # collides with a counter fails loudly here; in a script that only reported, it would have
    # silently reset the count.
    for fp in sorted(ROOT.rglob("*")):
        if not fp.is_file() or fp.suffix not in (".sh", ".md", ".py"):
            continue
        if any(x in fp.parts for x in (".git", "archive", "_archive", "node_modules")):
            continue
        # THIS FILE IS EXCLUDED, and it has to be: it DEFINES the divergent literal in order to
        # search for it, so scanning itself is a guaranteed self-match. Caught on the first run.
        # Narrow and honest — an exclusion by resolved path, not a pattern carve-out that could
        # also hide a real copy elsewhere. The cost is that a genuine divergent slug written inside
        # this one file would be missed; it is 190 lines whose subject is that exact string.
        if fp.resolve() == pathlib.Path(__file__).resolve():
            continue
        try:
            body = fp.read_text(errors="ignore")
        except Exception:
            continue
        for i, ln in enumerate(body.splitlines(), 1):
            # A COMMENT QUOTING THE OLD FORM IS NOT A USE OF IT. Both corrected .sh files quote the
            # divergent program in the comment explaining why it was replaced, and matching those
            # would make this assertion permanently red for documenting its own fix — the
            # mention-is-not-an-assertion defect, which this suite has now hit six times.
            stripped = ln.lstrip()
            if stripped.startswith("#") or stripped.startswith("--"):
                continue
            if DIVERGENT in ln:
                offenders.append(f"{fp.relative_to(ROOT)}:{i}  {ln.strip()[:88]}")
    check(f"no live line uses the divergent slug program ({DIVERGENT})",
          not offenders,
          "these build a transcript path with slash-and-space only, so they diverge on any path "
          "containing a dot, underscore or bracket — and a wrong directory reads as an EMPTY "
          "HISTORY, which is a fail-toward-PASS:\n          " + "\n          ".join(offenders[:5])
          + "\n          Use `sed 's|[^A-Za-z0-9]|-|g'`, the canonical rule in "
            "bin/core_seat.py::transcripts_dir().")

    print("\n--- python copies in LIVE hooks must produce the identical slug ---")
    # Only registered hooks are checked. The retired Stop gates carry the old two-character form and
    # are tombstoned, not maintained — asserting on them would be asserting on dead code, and the
    # failure would be indistinguishable from a real one.
    settings = (ROOT / ".claude" / "settings.json").read_text()
    live_offenders = []
    for hook in sorted((ROOT / ".claude" / "hooks").glob("*.py")):
        if hook.name not in settings:
            continue
        src = hook.read_text()
        if '.replace("/", "-").replace(" ", "-")' in src:
            live_offenders.append(hook.name)
    check("no REGISTERED hook still uses the two-character slug",
          not live_offenders, "offenders: %s" % live_offenders)

    print("\n--- the dose: these paths must actually SEPARATE the implementations ---")
    # If the canonical and two-character forms agreed everywhere, every assertion above would pass
    # against a broken copy. At least one path must distinguish them, or this file proves nothing.
    def two_char(x):
        return x.lstrip("/").replace("/", "-").replace(" ", "-")
    separating = [x for x in PATHS if two_char(x) != canonical(x).lstrip("-")]
    check("at least one probe path distinguishes canonical from the old form",
          len(separating) >= 2,
          "only %d of %d paths separate them — the corpus is too weak" % (len(separating), len(PATHS)))
    print("        separating paths: %d of %d" % (len(separating), len(PATHS)))

    print("\n=== Results: %d passed, %d failed%s ===" % (
          p, f, ", %d undecidable" % abstain if abstain else ""))
    if f:
        return 1
    if abstain:
        # rc=2 + UNDECIDABLE, the run-all.sh ABSTAIN contract (test_wilson_ci_known_answers.py is
        # the precedent this copies): every other check in this file ran and proved the slug
        # formula agrees across every copy; only the live-session-directory fixture is missing.
        print("\n  UNDECIDABLE  %d check(s) could not run on this seat (no live claude session has "
              "run against this exact path). Not a pass." % abstain)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
