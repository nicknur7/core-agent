#!/usr/bin/env python3
"""THE MINT AND THE CONSUME SITE MUST HASH THE SAME WAY, and the proof must not be a reading.

core-business found this defect TWICE, on two trees, with two different reviewers (#891 ASK 1, then
#909 independently), which is the argument for a test rather than another fix. sentinel-approve.sh
mints `.sentinel-approved-<hash>` and pretooluse-guard.sh looks for that exact filename. They are
two copies of one normalisation living 1100 lines apart in different files, and nothing connected
them — so an edit to either silently unbuilds the approval path.

    bash bin/sync-to-baseline.sh              mint == guard        (the only form ever tested)
    bash "bin/sync-to-baseline.sh"            mint != guard        approval mints, retry blocked
    bash "/Users/…/My Projects/co/bin/sync…sh" mint != guard        the form an autonomous run uses

IT FAILS CLOSED, so it was never a hole. It is worse in a subtler way: the approval reports SUCCESS,
the access log records an approval, and the retry stays blocked with no diagnostic anywhere. A gate
that blocks correct work while claiming to have approved it is one someone switches off — the same
reasoning that ended WebFetch gating after 85 approve-then-rerun blocks with zero real catches.

WHY IT EXTRACTS THE NORMALISERS RATHER THAN REIMPLEMENTING THEM: a reimplementation here would be a
THIRD copy, and it would agree with whichever of the two I read while writing it. That is the
re-implementation-agrees-with-its-own-reading failure this whole layer exists to distrust. The
snippets are pulled from the shipped files and executed, so the test breaks when the shipped code
breaks and cannot drift independently.

Run: python3 bin/tests/test_mint_consume_hash_parity.py
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
MINT = ROOT / ".claude" / "hooks" / "sentinel-approve.sh"
GUARD = ROOT / ".claude" / "hooks" / "pretooluse-guard.sh"

# The shared shape on both sides: HASH=$(printf '%s' "$CMD" | python3 -c '…' | shasum …)
SNIPPET = re.compile(r"HASH=\$\(printf\s+'%s'\s+\"\$CMD\"\s*\|\s*python3\s+-c\s+'(.*?)'\s*\|", re.S)


def normalisers(path: Path) -> list:
    """Every command-normalising snippet embedded in a shell file, in source order."""
    return [m.group(1) for m in SNIPPET.finditer(path.read_text(errors="replace"))]


def run(snippet: str, cmd: str) -> str:
    r = subprocess.run([sys.executable, "-c", snippet], input=cmd, text=True,
                       capture_output=True, timeout=20)
    if r.returncode != 0:
        return "<<error:%s>>" % r.stderr.strip()[:80]
    return r.stdout


# Forms that MUST hash identically on both sides. Each one is a real invocation shape, not a
# synthetic string: an absolute path necessarily carries a quote because "AI Projects" has a space.
FORMS = [
    "bash bin/sync-to-baseline.sh",
    'bash "bin/sync-to-baseline.sh"',
    "bash 'bin/sync-to-baseline.sh'",
    # A REAL CORE NAME IS NOT NEEDED FOR THE PROPERTY, and putting one here failed
    # test_no_cross_core_paths on the next run — a fixture naming another Core is
    # indistinguishable from a hardcoded dependency on it. What this form actually exercises
    # is a path containing a SPACE, which is therefore necessarily quoted.
    'bash "/Users/x/My Projects/checkout/bin/sync-to-baseline.sh"',
    "bash  bin/sync-to-baseline.sh   ",
    "bash bin/sync-to-baseline.sh  # approved by peer",
    "BASH bin/Sync-To-Baseline.sh",
    "gh pr create --title 'Fix A' --body 'B'",
]


def main() -> int:
    p = f = 0
    print("=== mint/consume hash parity ===\n")

    mint_n, guard_n = normalisers(MINT), normalisers(GUARD)
    if not mint_n or not guard_n:
        print("  FAIL  could not extract a normaliser: mint=%d guard=%d — the shape changed, and "
              "this test must be re-pointed rather than deleted" % (len(mint_n), len(guard_n)))
        return 1
    print("  found %d normaliser(s) in sentinel-approve.sh, %d in pretooluse-guard.sh"
          % (len(mint_n), len(guard_n)))

    # EVERY snippet in each file, not just the first — a second mint site with its own copy is
    # exactly how this came back the second time.
    for i, a in enumerate(mint_n):
        for j, b in enumerate(guard_n):
            diffs = [c for c in FORMS if run(a, c) != run(b, c)]
            if diffs:
                print("  FAIL  mint[%d] and guard[%d] disagree on %d form(s):" % (i, j, len(diffs)))
                for c in diffs[:4]:
                    print("          %-52r mint=%r guard=%r" % (c, run(a, c), run(b, c)))
                f += 1
            else:
                print("  PASS  mint[%d] == guard[%d] on all %d invocation forms" % (i, j, len(FORMS)))
                p += 1

    # THE DOSE. A parity test that never fails is indistinguishable from one comparing a string to
    # itself — and this file would report PASS forever if `normalisers()` silently returned the same
    # snippet twice. Perturb one side and require the comparison to FLIP.
    print("\n--- dose: the comparison must depend on the inputs ---")
    perturbed = mint_n[0].replace('.strip().lower()', '.strip()')
    if perturbed == mint_n[0]:
        perturbed = mint_n[0] + "\n"          # fall back to any real change
    if run(perturbed, "BASH bin/X.sh") != run(guard_n[0], "BASH bin/X.sh"):
        print("  PASS  dropping .lower() from one side is DETECTED (the check has teeth)")
        p += 1
    else:
        print("  FAIL  a perturbed normaliser still compared equal — this test proves nothing")
        f += 1

    # And the property that makes the whole thing matter: normalisation must actually COLLAPSE the
    # forms, not merely agree while being an identity function on both sides.
    # SPELLINGS OF ONE COMMAND, which is not the same set as "forms that must agree across sides".
    # The absolute-path form is deliberately excluded: it is a DIFFERENT command string, and a
    # normaliser that resolved it to the relative one would be widening approval scope across the
    # filesystem. Both sides must treat it identically (asserted above) and neither should collapse
    # it (asserted by its absence here).
    spellings = [FORMS[i] for i in (0, 1, 2, 4, 5, 6)]
    canon = {run(mint_n[0], c) for c in spellings}
    if len(canon) == 1:
        print("  PASS  %d spellings of one command collapse to a single canonical form" % len(spellings))
        p += 1
    else:
        print("  FAIL  the normaliser does not collapse equivalent spellings: %d distinct results %r"
              % (len(canon), sorted(canon)[:3]))
        f += 1
    if run(mint_n[0], FORMS[3]) != run(mint_n[0], FORMS[0]):
        print("  PASS  an absolute path stays a DISTINCT command — approval scope is not widened")
        p += 1
    else:
        print("  FAIL  absolute and relative spellings collapsed; one approval now covers both")
        f += 1

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
