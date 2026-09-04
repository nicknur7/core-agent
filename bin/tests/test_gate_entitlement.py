#!/usr/bin/env python3
"""THE TRUST ROOT MUST BE A STRICT ANCESTOR OF THE CANDIDATE — a commit cannot be its own grader.

core-business, bus #1015, and the distinction it drew is the whole finding:

    AUTHENTICITY   `.gate-frozen-at` proves the child is running the tree the PARENT materialised.
                   That was the #1008 fix and it holds.
    ENTITLEMENT    nothing proved that tree should have been trusted AT ALL. A candidate that points
                   the trust root at ITSELF is still materialised as its own frozen evaluator, and
                   the stamp agrees with it perfectly.

It found a third route that needs no tampering whatsoever: `--promote <candidate>` and then grade it.
Promotion is the SANCTIONED way to move the trust root, it skips the re-exec by design (it must, or
the tool could never bootstrap), and nothing in pretooluse-guard.sh matches it. One normal-sounding
maintenance command and the candidate is its own grader — nothing tampered, nothing looking wrong,
the gate still printing verdicts.

ONE CHECK COVERS BOTH, which is why this is small:

    trusted == candidate                     refuse   self-grading, however it was reached
    promoted to the candidate, then graded   refuse   same state, sanctioned route
    trusted is an older commit               ALLOW    the honest case
    promote to HEAD, then grade HEAD+1       ALLOW    the legitimate promote workflow
    no shared history                        refuse   undecidable, and undecidable reverts

The two ALLOW rows are why this is a fix and not a wall — a check that also breaks the legitimate
workflow is not a fix, and business verified those rows before proposing it.

WHY THIS TESTS `decide()` IN-PROCESS RATHER THAN THE CLI. The gate re-executes from the FROZEN
checkout before grading, so the running evaluator is the copy at the trust root — which does not
contain this check and cannot until the root is promoted. That is the trusting-trust architecture
working, not a flaw, and it means the CLI cannot demonstrate a gate fix until a reviewed promotion
lands. Calling decide() directly exercises the decision on a controlled history today; the end-to-end
path is covered by bin/tests/test_trajectory_gate.sh once promoted.

Run: python3 bin/tests/test_gate_entitlement.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()


def load_gate():
    spec = importlib.util.spec_from_file_location("tg_ent", str(ROOT / "bin" / "trajectory-gate.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["tg_ent"] = m
    spec.loader.exec_module(m)
    return m


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, timeout=60)


def build_repo(d):
    """Three commits on a line, plus one unrelated root — enough for every row."""
    git(d, "init", "-q", ".")
    git(d, "config", "user.email", "t@t"); git(d, "config", "user.name", "t")
    shas = []
    for i in range(3):
        (d / ("f%d.txt" % i)).write_text("v%d\n" % i)
        git(d, "add", "-A")
        git(d, "commit", "-qm", "c%d\n\nCasebook-Target: S1\n" % i)
        shas.append(git(d, "rev-parse", "HEAD").stdout.strip())
    # an unrelated root: no shared history with the line above
    git(d, "checkout", "-q", "--orphan", "lonely")
    (d / "other.txt").write_text("x\n")
    git(d, "add", "-A"); git(d, "commit", "-qm", "unrelated\n\nCasebook-Target: S1\n")
    lonely = git(d, "rev-parse", "HEAD").stdout.strip()
    git(d, "checkout", "-q", "-")
    return shas, lonely


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== entitlement: the trust root must be a STRICT ancestor ===\n")
    m = load_gate()

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        shas, lonely = build_repo(d)
        old, mid, new = shas

        def reasons(trusted, candidate):
            """Run decide()'s entitlement gate with a chosen trust root, in a controlled repo."""
            orig = m.trusted_sha
            m.trusted_sha = lambda: trusted
            os.environ["CORE_GATE_SOURCE_REPO"] = str(d)
            try:
                v = m.decide(candidate, d)
            finally:
                m.trusted_sha = orig
                os.environ.pop("CORE_GATE_SOURCE_REPO", None)
            return " | ".join(v.get("reasons") or [])

        print("--- the two REFUSE rows business named ---")
        r_self = reasons(new, new)
        check("trusted == candidate is refused", "cannot be its own grader" in r_self, r_self[:180])
        check("...and the reason names BOTH routes that reach it",
              "--promote" in r_self, r_self[:180])

        r_unrel = reasons(lonely, new)
        check("no shared history is refused as UNDECIDABLE",
              "not an ancestor" in r_unrel, r_unrel[:180])

        print("\n--- the two ALLOW rows, which are why this is a fix and not a wall ---")
        # The honest case and the legitimate promote workflow must both get PAST entitlement.
        # They may still revert for other reasons; what must not appear is an entitlement refusal.
        r_honest = reasons(old, new)
        check("an older trust root passes entitlement (the honest case)",
              "its own grader" not in r_honest and "not an ancestor" not in r_honest,
              r_honest[:180])

        r_promote = reasons(mid, new)
        check("promote-to-HEAD then grade HEAD+1 passes entitlement",
              "its own grader" not in r_promote and "not an ancestor" not in r_promote,
              r_promote[:180])

        print("\n--- and it FAILS CLOSED when a SHA cannot be resolved ---")
        # The first version of this check used `repo` instead of source_repo(). After the re-exec an
        # arm is a `git archive` extraction, NOT a git repo, so rev-parse returned empty and an
        # `if _same and _cand` guard skipped the entire check — a guard that failed OPEN, inside the
        # fix for a trust-root bypass. Caught only because the refusal I expected never printed.
        r_bogus = reasons("0" * 40, new)
        check("an unresolvable trust root REFUSES rather than skipping the check",
              "UNDECIDABLE" in r_bogus or "cannot resolve" in r_bogus, r_bogus[:180])

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    print("\nNOTE: this exercises decide() directly. The CLI re-execs from the FROZEN checkout, so")
    print("the running evaluator is the copy at the trust root and does NOT contain this check")
    print("until a reviewed promotion lands. That is the architecture, not a gap in the test.")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
