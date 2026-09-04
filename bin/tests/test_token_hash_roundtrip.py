"""Do the MINT hash and the GUARD hash now agree for the forms an autonomous run uses?

They agreed only on the already-canonical form. The quoted-absolute-path form — which is what an
unattended run produces, because the path contains a space — minted a token the guard then could
not find: approval reports success, access log records it, retry stays blocked, no diagnostic.
"""
import re
import subprocess

SYNC = "bin/" + "sync-to" + "-baseline.sh"
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _core import core_root as _core_root  # noqa: E402

# DERIVED. I hardcoded this an hour ago, in a test written to verify a fix for hardcoded paths,
# and the widened gate caught it on its first real run. That is the gate working — and it is the
# fifth instance today of the habit business named.
HOME = str(_core_root())

NORMALISER = r'''
import re, sys
s = re.sub(r"\s+#.*$", "", sys.stdin.read().strip()).replace(chr(34), "").replace(chr(39), "")
sys.stdout.write(re.sub(r"\s+", " ", s).strip().lower())
'''


def sha(text, normalise):
    if normalise:
        text = subprocess.run(["python3", "-c", NORMALISER], input=text,
                              capture_output=True, text=True).stdout
    out = subprocess.run(["shasum", "-a", "256"], input=text, capture_output=True, text=True)
    return out.stdout.split()[0][:8]


CASES = [
    ("canonical", "bash %s" % SYNC),
    ("quoted", 'bash "%s"' % SYNC),
    ("absolute quoted", 'bash "%s/%s"' % (HOME, SYNC)),
    ("extra whitespace", "bash   %s" % SYNC),
    ("trailing comment", "bash %s   # nightly" % SYNC),
]

# Read the guard's CURRENT hash line to confirm it normalises, rather than assuming the patch took.
guard = open("%s/.claude/hooks/pretooluse-guard.sh" % HOME).read()
m = re.search(r"^HASH=\$\(printf '%s' \"\$CMD\" \| (python3|shasum)", guard, re.M)
guard_normalises = bool(m and m.group(1) == "python3")
print("\n  guard normalises before hashing: %s\n" % guard_normalises)

print("  %-18s %-10s %-10s %s" % ("form", "mint", "guard", ""))
bad = 0
for label, cmd in CASES:
    mint = sha(cmd, True)                       # sentinel-approve.sh always normalises
    grd = sha(cmd, guard_normalises)
    ok = mint == grd
    bad += not ok
    print("  %-18s %-10s %-10s %s" % (label, mint, grd, "MATCH" if ok else "DIVERGE"))

print("\n  %s\n" % ("every form round-trips — an unattended push can redeem its own token"
                    if not bad else "%d form(s) still diverge" % bad))

# EXIT ON THE RESULT. Until 2026-08-10 this file contained ZERO exit calls: it computed `bad`,
# printed "%d form(s) still diverge", and returned 0 regardless. A divergence between how
# sentinel-approve.sh hashes a command and how the guard hashes it would have been printed and
# then counted as a passing test, forever.
#
# That matters more here than in the sibling case found the same hour (test_org_scoping_lint.py,
# four failure verdicts and no exit code): this file guards the APPROVAL TOKEN path. A silent
# divergence means a minted token cannot be redeemed by the command it was minted for — the
# unattended-push property this test exists to protect — and the suite would have kept saying green.
#
# Second instance of the same class in one hour, and the class is this session's own lesson:
# reporting a value is not checking it. `bad` was computed, printed, and never branched on.
import sys
sys.exit(1 if bad else 0)
