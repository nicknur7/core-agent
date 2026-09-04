"""Liveness + negative probes for bin/lint-org-scoping.py.

WHY THIS FILE EXISTS. On 2026-08-09 a fleet sweep found the pre-commit ORG-SCOPE block had never
fired: session-lifecycle.sh:92 read `--count` through `$(... || echo 0)`, the linter exited 1 on
violations, so the caller captured "2\n0" and its own `^[0-9]+$` guard rejected it.  # privacy-ok: generic engineering vocabulary
Silent pass, every commit, with two flagged lines in the tree.

AND THE DEAD GATE WAS HIDING A SECOND DEFECT. Both flagged lines were FALSE POSITIVES — f-strings
inside a multi-line print(), not SQL. Nobody had ever seen them because the gate never fired.
Fixing only the deadness would have converted a silent no-op into a gate that blocks every commit
touching _env.py, and a gate that blocks correct work is one someone disables.

THEN THE FIX ITSELF WENT BLIND MID-REPAIR. Exempting emitter calls by AST line-span over-exempted
a line carrying BOTH log.info(...) AND cur.execute(...), dropping detection from 5 planted
violations to 4 — while the baseline count and the false-positive count both still looked perfect.
Only the liveness probe caught it.

That is the whole lesson in one file: SILENCING NOISE AND SILENCING THE DETECTOR PRODUCE THE SAME
CLEAN NUMBER. A negative probe alone cannot tell them apart. Both directions run here, always.

Run: python3 bin/tests/test_org_scoping_lint.py
"""
import subprocess
import sys
import tempfile
import os
import shutil

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _core import core_root as _core_root  # noqa: E402

# DERIVED. This planted a violation file inside LIFE's live scheduling/brain-pg/ and ran a baseline
# count against life's tree at MODULE IMPORT — from every Core, at every session close, racing
# whatever life was doing in that directory.
REPO = str(_core_root())
LINT = os.path.join(REPO, "bin", "lint-org-scoping.py")
TARGET = os.path.join(REPO, "scheduling", "brain-pg", "_probe_orglint.py")

REAL = '''"""Planted violations — every one an ACTUAL unsafe org_id reaching a DB call."""
def bad_fstring(cur, org):
    cur.execute(f"SELECT * FROM entities WHERE org_id = {org}")

def bad_percent(cur, org):
    cur.execute("SELECT * FROM entities WHERE org_id = %d" % (org,))

def bad_format(cur, org):
    cur.execute("SELECT * FROM entities WHERE org_id = {}".format(org))

def bad_concat(cur, org):
    cur.execute("SELECT * FROM entities WHERE org_id = " + str(org))

def emits_AND_executes(cur, org, log):
    log.info(f"scoping to org_id = {org}"); cur.execute(f"SELECT 1 WHERE org_id = {org}")
'''

SAFE = '''"""Planted NON-violations — diagnostics and correctly-bound SQL."""
import sys
def diag(ident, env):
    print(f"CORE_ORG_ID={env} but identity says org_id={ident} — using {ident}", file=sys.stderr)

def raises(ident):
    raise RuntimeError(f"could not resolve org_id={ident}")

def safe_self(cur):
    cur.execute("SELECT * FROM entities WHERE org_id = current_setting('app.current_org_id')::bigint")

def safe_bound(cur, orgs):
    placeholders = ",".join(["%s"] * len(orgs))
    cur.execute(f"SELECT * FROM entities WHERE org_id IN ({placeholders})", orgs)
'''


def count():
    """Read the violation total from --json.

    UPDATED 2026-08-12 with the linter's output shape. `--json` used to emit a bare
    {path: [violations]} mapping, so this summed len() over its values. That mapping had nowhere to
    put a denominator: `{}` meant BOTH "28 files scanned, all clean" AND "0 files scanned, nothing
    looked at", and session-lifecycle.sh consumes --count for the ORG-SCOPE RISK block — so a
    renamed directory silently disabled the multi-tenant guard while reporting a clean bill of
    health. Found by core-finance. The output is now {scanned, violation_total, findings}.

    Reads `violation_total` rather than re-summing `findings`: the linter already computes the
    number it blocks on, and a test that recomputes it would be asserting its own arithmetic
    instead of the value the consumer actually sees.
    """
    r = subprocess.run([sys.executable, LINT, "--json"], capture_output=True, text=True, cwd=REPO)
    import json
    try:
        d = json.loads(r.stdout or "{}")
    except Exception:
        return None, r.stdout
    if "violation_total" not in d:
        # Do NOT fall back to the old shape. A silent fallback would let this test keep passing
        # against a linter that had lost its denominator again, which is the defect itself.
        return None, f"--json lacks 'violation_total' (keys={sorted(d)}) — shape regressed"
    return d["violation_total"], d


def _findings(d):
    """The path->violations mapping, wherever the linter currently puts it.

    ADDED 2026-08-12. `--json` moved from a bare {path: [...]} mapping to
    {scanned, violation_total, findings} so that a clean scan and an unrun scan stop producing the
    same bytes. Three sites in this file iterated the top level directly and silently found nothing
    once the shape changed — they would have reported "detector is blind" about a linter that was
    working fine. Reading through one accessor keeps the next shape change to one edit.
    """
    if not isinstance(d, dict):
        return {}
    return d.get("findings", {})


# THIS FILE HAD NO EXIT CODE AT ALL UNTIL 2026-08-10.
#
# It printed STILL FLAGGING / DETECTOR IS BLIND / STILL CRIES WOLF / MANGLED and then fell off the
# end of the module, so every one of those verdicts exited 0. Four failure modes, carefully worded,
# none of them enforced — the file could detect its own subject going blind and report success.
#
# That is precisely the lesson this session put in tasks/lessons.md hours earlier, found in the
# test that was supposed to be doing the checking: **reporting a value is not checking it.** Ask
# whether anything BRANCHES on the number, not whether the number is printed. Here nothing did.
#
# The new suite runner would not have caught it either: the file prints "ok" on its passing lines,
# so it satisfies the evidence check and exits 0. A test that lies in the passing direction is
# invisible to a runner that trusts exit codes, which is why the exit code has to be real.
FAILED = []

base, _ = count()
print("\n  baseline (real tree): %s violation(s)  %s"
      % (base, "ok — the two false positives are gone" if base == 0 else "STILL FLAGGING"))
if base != 0:
    FAILED.append("baseline: %s violation(s) in the real tree, expected 0" % base)

print("\n  LIVENESS — plant 5 real violations, all must fire")
open(TARGET, "w").write(REAL)
try:
    n, d = count()
    hits = sum(len(v) for k, v in _findings(d).items() if "_probe_orglint" in k)
    # EXACTLY 5, NOT AT LEAST 5. `hits >= 5` passed at 6 and would pass at 60, so it could not
    # distinguish a sixth real defect from one line counted twice — and it WAS counting twice:
    # `emits_AND_executes` carries two f-string interpolations, so the same lineno and code were
    # appended as two findings. Fixed in lint-org-scoping.py by deduping per (line, reason); the
    # assertion is exact now so the next inflation cannot hide under a `>=`.
    lines = {v["line"] for k, v_ in _findings(d).items() if "_probe_orglint" in k for v in v_}
    print("     detected %s finding(s) across %s violating line(s), expected 5/5  %s"
          % (hits, len(lines), "ok" if hits == 5 and len(lines) == 5 else "WRONG COUNT"))
    if hits != 5 or len(lines) != 5:
        FAILED.append("liveness: %d findings on %d lines, expected 5 and 5" % (hits, len(lines)))
finally:
    os.unlink(TARGET)

print("\n  NEGATIVE — plant diagnostics + correctly-bound SQL, none may fire")
open(TARGET, "w").write(SAFE)
try:
    n, d = count()
    hits = sum(len(v) for k, v in _findings(d).items() if "_probe_orglint" in k)
    print("     flagged  %s/0  %s" % (hits, "ok" if hits == 0 else "STILL CRIES WOLF"))
    if hits != 0:
        FAILED.append("negative: %d false positive(s) on diagnostics and bound SQL" % hits)
finally:
    os.unlink(TARGET)

print("\n  GATE — does the pre-commit idiom now actually block?")
r = subprocess.run(
    'V=$(python3 bin/lint-org-scoping.py --count 2>/dev/null || echo 0); '
    'printf "%q " "$V"; if [[ "$V" =~ ^[0-9]+$ ]]; then echo "parses"; else echo "MANGLED"; fi',
    shell=True, capture_output=True, text=True, cwd=REPO, executable="/bin/bash")
print("     %s" % r.stdout.strip())
if "parses" not in r.stdout:
    FAILED.append("gate: the pre-commit idiom mangles --count (%s)" % r.stdout.strip()[:80])

print()
if FAILED:
    print("=== FAILED ===")
    for x in FAILED:
        print("  " + x)
    sys.exit(1)
print("=== ALL PASS — 4 checks, each with a real exit code for the first time ===")
sys.exit(0)
