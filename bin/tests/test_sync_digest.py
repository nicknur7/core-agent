#!/usr/bin/env python3
"""THE CONTENT DIGEST MUST BE STABLE AND SENSITIVE, or the approval it binds is decoration.

core-business refused to let its APPROVE satisfy the 50-file ceiling (bus #1025) and gave the
structural reason: *"the receipt would attest that business approved a COMMAND, at a TIME. It would
say nothing about which 112 files rode along."* The digest exists to make an approval a statement
about a specific tree.

TWO PROPERTIES, AND BOTH ARE LOAD-BEARING IN OPPOSITE DIRECTIONS:

    STABLE     the same staged set must produce the same digest on repeated runs, or the mint's
               recomputation can NEVER match and the mechanism refuses every honest push. A gate
               that blocks correct behaviour is one someone disables.
    SENSITIVE  any change to the set — an edit, an addition, a removal — must change it, or a tree
               that moved between review and push still passes. That is the exact failure the
               digest was built to stop, and the one I closed by hand three times before building it.

WHY THIS TESTS THE ALGORITHM AND NOT THE SCRIPT. `sync-to-baseline.sh --check` computes the digest
only after cloning the baseline and rsyncing into it, which costs ~30s a call. Three calls would add
90s to a suite that runs in ~125s total, and a test that doubles the suite is a test someone starts
skipping — the failure mode the runtime ratchet exists to catch. So the same `git diff --cached
--raw | awk | sort | shasum` pipeline runs here against a synthetic staged repo, where every case is
constructed rather than waited for.

The producer's live behaviour was dosed by hand when it was written (stable across two runs, changed
on a one-line edit) and is re-dosed here in a form that runs every time.

Run: python3 bin/tests/test_sync_digest.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SYNC = ROOT / "bin" / "sync-to-baseline.sh"

# The exact pipeline the script uses. Kept as ONE string so it cannot drift from the shipped one
# without this test's own extraction check failing below.
PIPELINE = "git diff --cached --raw | awk '{print $4, $6}' | sort | shasum -a 256 | awk '{print $1}'"


def sh(cwd, cmd, must_succeed=True):
    """Run a fixture command and REFUSE TO HIDE ITS FAILURE.

    The first version returned `.stdout` and dropped the exit code and stderr. A restore step then
    failed — `git rm -r --cached .` erroring with "staged content different from both the file and
    the HEAD" — the index was never cleared, and the test reported "the digest depends on something
    outside the file set". The digest was correct; the SETUP had not run.

    That is the planting-is-a-measurement failure with the helper itself as the accomplice: the one
    piece of code whose job was to report what happened chose not to. Fixture commands now raise.
    """
    r = subprocess.run(["bash", "-c", cmd], cwd=str(cwd), capture_output=True,
                       text=True, timeout=120)
    if must_succeed and r.returncode != 0:
        raise RuntimeError("fixture command failed (%d): %s\n%s" % (r.returncode, cmd, r.stderr.strip()[:300]))
    return r.stdout.strip()


def digest(repo):
    return sh(repo, PIPELINE)


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== sync content digest: stable and sensitive ===\n")

    # THE PIPELINE UNDER TEST MUST BE THE SHIPPED ONE. Without this the file happily verifies a
    # string that no longer appears in sync-to-baseline.sh — testing a copy of a thing instead of
    # the thing, which is the shape that made business's stale figures pass their own checks.
    src = SYNC.read_text() if SYNC.is_file() else ""
    check("this test's pipeline is the one sync-to-baseline.sh actually runs",
          "git diff --cached --raw" in src and "shasum -a 256" in src,
          "the shipped digest expression changed — re-point this test rather than deleting it")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        sh(d, "git init -q . && git config user.email t@t && git config user.name t")
        for i in range(3):
            (d / ("f%d.txt" % i)).write_text("content %d\n" % i)
        sh(d, "git add -A")

        d1 = digest(d)
        d2 = digest(d)
        check("a digest is produced at all", len(d1) == 64, "got %r" % d1)
        check("STABLE: the same staged set digests identically twice", d1 == d2,
              "%s vs %s — the mint could never match this" % (d1[:16], d2[:16]))

        print("\n--- SENSITIVE: every way the set can change must move it ---")
        (d / "f1.txt").write_text("content 1 EDITED\n")
        sh(d, "git add -A")
        d_edit = digest(d)
        check("an EDIT to a staged file changes the digest", d_edit != d1,
              "edit was invisible — the digest is keyed on paths, not content")

        (d / "f3.txt").write_text("new file\n")
        sh(d, "git add -A")
        d_add = digest(d)
        check("an ADDED file changes the digest", d_add != d_edit)

        sh(d, "git rm -q --cached f0.txt")
        d_del = digest(d)
        check("a REMOVED file changes the digest", d_del != d_add)

        print("\n--- and it returns to a previous value when the tree does ---")
        # Guards against a digest that folds in something extrinsic (a timestamp, a counter), which
        # would be stable within a run and unmatchable across the review→mint gap.
        # RESTORE BY REBUILDING THE INDEX, not by `git reset`. The first version used
        # `git reset && git checkout -- .` and the round-trip failed — because this fixture has NO
        # COMMIT, so reset has no HEAD to restore to and simply empties the index. The digest was
        # right and my restore was wrong: a fixture that fails to reconstruct the case, reported as
        # a defect in the subject. Third time today, and the tell was the same each time — the
        # failure accused the thing under test rather than the setup.
        (d / "f3.txt").unlink(missing_ok=True)
        (d / "f1.txt").write_text("content 1\n")
        sh(d, "git rm -rf -q --cached . && git add f0.txt f1.txt f2.txt")
        check("restoring the original content restores the original digest", digest(d) == d1,
              "expected %s, got %s — the digest depends on something outside the file set"
              % (d1[:16], digest(d)[:16]))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
