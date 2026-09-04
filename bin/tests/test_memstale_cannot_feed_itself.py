#!/usr/bin/env python3
"""The stamp-bumper must never accept its OWN write as proof of an edit.

`Last updated:` is an ATTESTATION. This session found two false facts in `memory/about-me.md`
surviving under a stamp that read *"Nick reverified content current — no changes"* — one of which
core-school had already corrected twelve days earlier. A stamp that outlives the fact it certifies
is the failure mode, and an automated bumper with no proof requirement is a machine for making them.

THE SELF-FEEDING LOOP THIS PREVENTS. The obvious predicate is "there is a commit newer than the
stamp, so the stamp is behind — bump it." That is wrong in the most dangerous direction available:

  close 1: applier bumps the stamp; the close auto-save COMMITS that write
  close 2: the file now has a commit newer than its old stamp -> naive rule calls that proof
  result:  today's date is laundered onto content last edited months ago, and `sys-memstale`
           goes permanently quiet for that file because the stamp is now fresh

So proof requires a commit touching a NON-STAMP line. A stamp-only commit — the applier's own
auto-saved write, or Nick's manual bump committed past midnight — can never be proof of anything.

This test builds a throwaway git repo and demonstrates the property directly, rather than asserting
it from the source.

Run: python3 bin/tests/test_memstale_cannot_feed_itself.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
sys.path.insert(0, str(ROOT / "bin"))
import memstale  # noqa: E402

_passed = 0
_failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def git(repo, *args, env=None):
    e = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if env:
        e.update(env)
    import os
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          env={**os.environ, **e}, timeout=30)


with tempfile.TemporaryDirectory() as td:
    repo = Path(td) / "seat"
    (repo / "memory" / "relationships").mkdir(parents=True)
    git(repo, "init", "-q", ".")
    f = repo / "memory" / "relationships" / "someone.md"
    rel = "memory/relationships/someone.md"

    # A file stamped long ago, with real content.
    f.write_text("---\nLast updated: 2026-01-01\n---\n\nfacts about someone\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial", env={"GIT_COMMITTER_DATE": "2026-01-01T10:00:00"})

    print("=== a STAMP-ONLY commit must NOT count as proof ===")
    # Exactly what the applier's own write + close auto-save produces.
    f.write_text("---\nLast updated: 2026-06-01\n---\n\nfacts about someone\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "stamp bump only",
        env={"GIT_COMMITTER_DATE": "2026-06-01T10:00:00"})
    pd = memstale.proof_date(repo, rel, "2026-01-01")
    check("a commit that changed ONLY the stamp is not proof", pd is None,
          f"got {pd!r} — the loop would feed itself")

    print()
    print("=== a real CONTENT edit IS proof, and dates to that edit ===")
    f.write_text("---\nLast updated: 2026-06-01\n---\n\nfacts about someone, corrected\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "real content edit",
        env={"GIT_COMMITTER_DATE": "2026-07-15T10:00:00"})
    pd2 = memstale.proof_date(repo, rel, "2026-06-01")
    check("a non-stamp change IS proof", pd2 == "2026-07-15", f"got {pd2!r}")
    check("...and the proof date is the EDIT's date, not today's",
          pd2 is not None and not pd2.startswith("2026-08"), f"got {pd2!r}")

    print()
    print("=== stacking stamp-only commits never manufactures proof ===")
    for i, d in enumerate(("2026-07-20", "2026-07-25", "2026-07-28")):
        f.write_text(f"---\nLast updated: {d}\n---\n\nfacts about someone, corrected\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"stamp bump {i}",
            env={"GIT_COMMITTER_DATE": f"{d}T10:00:00"})
    pd3 = memstale.proof_date(repo, rel, "2026-07-15")
    check("three stamp-only commits after the last real edit are still not proof", pd3 is None,
          f"got {pd3!r} — this is the two-close laundering path")

print()
print("=== the residual half must never be auto-bumped ===")
safe = (ROOT / "scheduling" / "core-si" / "auto-safe.txt").read_text()
effective = [ln.strip() for ln in safe.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
check("sys-memstale-proven IS admitted", "sys-memstale-proven" in effective)
check("bare sys-memstale is NOT admitted", "sys-memstale" not in effective,
      "a file with no proven edit has NO non-lying value to write")

print()
print("=== the applier must not re-run the detector under shadow ===")
close_src = (ROOT / "bin" / "core-si-close.py").read_text()
body = close_src.split("def _apply_sys_memstale_proven", 1)[-1].split("\ndef ", 1)[0]
# STRIP THE DOCSTRING AND COMMENTS FIRST. The docstring explicitly warns "must NEVER call
# detect_items()", and a scanner that reads prose about code as code fails on a correct file —
# the fourth checker in this suite to do exactly that today. Scan the CODE.
import re as _re
code = _re.sub(r'"""[\s\S]*?"""', "", body)
code = "\n".join(l.split("#", 1)[0] for l in code.splitlines())
check("applier does not call detect_items()", "detect_items(" not in code,
      "that re-runs detect.sh: writes state files, opens Postgres, shells git ls-remote")
check("applier uses the shared predicate", "memstale.classify(" in body)

print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
