#!/usr/bin/env python3
"""THE stale-memory predicate — one copy, read by detect.sh AND by the core-si applier.

`detect.sh` emits its items from `--json`; `bin/core-si-close.py`'s applier acts from the same
call. Two copies of a resolution rule is how the doc-path bug survived for a month, and this file
exists so that cannot happen here.

────────────────────────────────────────────────────────────────────────────────────────────────
THE DEFECT THIS FILE IS SHAPED AROUND: A LOOP THAT FEEDS ITSELF
────────────────────────────────────────────────────────────────────────────────────────────────
The obvious predicate — "the file has a commit newer than its `Last updated:` stamp, so the stamp
is behind; bump it" — is WRONG, and wrong in the most dangerous available direction.

The applier's own stamp write is committed by the close auto-save. So on the NEXT close that file
has a commit newer than its (old) stamp, which the naive rule reads as proof of an unbumped edit.
In exactly two closes the loop launders TODAY'S DATE onto a file whose CONTENT was last edited
months ago — and then permanently silences `sys-memstale` for it, because the stamp is now fresh.
The detector goes quiet, the file looks verified, and nothing is.

That is worse than doing nothing, because `Last updated` is an ATTESTATION. This session already
found two false facts in `about-me.md` that survived under a stamp reading *"Nick reverified content
current — no changes"*, and core-school had corrected one of them twelve days earlier. A stamp that
outlives the fact it certifies is the exact failure mode; an automated stamp-bumper with no proof
requirement is a machine for manufacturing them.

SO: proof must be a commit that touched a NON-STAMP line. A stamp-only commit — the applier's own
auto-saved write, or Nick's manual bump committed past midnight — can never be proof of anything.
That single rule is what stops the loop feeding itself, and it is why `proof_date()` reads the diff
rather than just the commit date.

────────────────────────────────────────────────────────────────────────────────────────────────
THE NAMED RESIDUAL, stated rather than buried
────────────────────────────────────────────────────────────────────────────────────────────────
Any committed non-stamp change counts as proof, INCLUDING A TYPO FIX. So the meaning of
`Last updated` drifts from *facts were verified* toward *file was really edited*. That is bounded —
it is the date of a real commit that really touched content, and it cannot compound, because the
next bump needs another real edit. It is still a weakening, and Nick is told in one line when this
lands rather than discovering it later.

Usage:
    python3 bin/memstale.py --json     # {"proven": [...], "residual": [...]}
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# ENV OVERRIDE FIRST, __file__ only as the fallback. bin/ ships to every Core, and a tool that
# anchors solely on __file__ resolves whichever Core's copy happens to be executing — which is how
# a tool run from one seat measures (and here, WRITES TO) another seat's memory files. That is the
# defect bin/tests/_core.py was written about and test_root_anchors.py exists to catch; it caught
# this file on its first suite run.
REPO = Path(os.environ.get("CORE_INSTANCE")
            or os.environ.get("CLAUDE_PROJECT_DIR")
            or Path(__file__).resolve().parents[1])

STAMP_RE = re.compile(r"^last updated:", re.I)
STATUS_RE = re.compile(r"^Status:\s*(LOCKED|COMPLETE|EXTRACTED|DRAFT|SKELETON|SUPERSEDED)\b", re.I)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
STALE_DAYS = 30


def scan_set(repo: Path):
    """The files this predicate governs. Mirrors detect.sh §12's find, including its exemptions:
    archive/ is out, and memory/projects/business/* are Status-marked planning docs in whichever
    Core holds them (the org==1 guard was removed 2026-06-24 because business false-flagged its own
    docs every session)."""
    mem = repo / "memory"
    out = [mem / n for n in ("about-me.md", "preferences.md", "skills-interests.md", "goals.md")]
    for sub in ("relationships", "projects"):
        root = mem / sub
        if root.is_dir():
            out += [p for p in sorted(root.rglob("*.md"))
                    if "archive" not in p.parts and "business" not in p.parts]
    return [p for p in out if p.is_file()]


def read_stamp(path: Path):
    """(line_index, stamp_date_str) for the first `Last updated:` line, or (None, None)."""
    try:
        for i, line in enumerate(path.read_text(errors="replace").splitlines()):
            if STAMP_RE.match(line.strip()):
                m = DATE_RE.search(line)
                return (i, m.group(0)) if m else (i, None)
    except Exception:
        pass
    return (None, None)


def is_status_exempt(path: Path) -> bool:
    try:
        for line in path.read_text(errors="replace").splitlines()[:40]:
            if STATUS_RE.match(line.strip()):
                return True
    except Exception:
        pass
    return False


def proof_date(repo: Path, rel: str, stamp: str):
    """Date of the newest commit strictly AFTER `stamp` whose diff for `rel` touches a NON-STAMP
    line, else None.

    THE NON-STAMP REQUIREMENT IS THE WHOLE POINT — see the module docstring. Without it the
    applier's own auto-committed stamp write becomes the next cycle's proof and the loop launders
    a fresh date onto stale content.
    """
    try:
        r = subprocess.run(["git", "log", "-n", "40", "--no-merges", "--format=%H %cI", "--", rel],
                           cwd=str(repo), capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    for row in (r.stdout or "").splitlines():
        bits = row.split()
        if len(bits) != 2 or len(bits[1]) < 10:
            continue
        sha, cdate = bits[0], bits[1][:10]
        if cdate <= stamp:
            continue
        try:
            d = subprocess.run(["git", "show", sha, "--format=", "--unified=0", "--", rel],
                               cwd=str(repo), capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        for ln in (d.stdout or "").splitlines():
            if ln.startswith(("+++", "---")):
                continue
            if ln[:1] in "+-" and not STAMP_RE.match(ln[1:].lstrip()):
                return cdate
    return None


def classify(repo: Path = REPO, today=None) -> dict:
    """-> {"proven": [{path, rel, line_idx, old, new}], "residual": [{rel, why}]}

    PROVEN is the mechanizable slice: the file is >30d stale AND a real content edit exists after
    the stamp, so there is a DEFENSIBLE date to write — the date of that edit, never today's.

    RESIDUAL is everything else, and it stays Nick's. It is not a judgment call being deferred; for
    a no-stamp or no-proven-edit file there is NO NON-LYING VALUE TO WRITE. Writing any date there
    asserts a freshness nothing in the repo supports and destroys the only staleness signal the
    file has.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=STALE_DAYS)
    proven, residual = [], []
    for p in scan_set(repo):
        # Re-proved here every time, never inherited from a resolved basename.
        if is_status_exempt(p):
            continue
        rel = p.relative_to(repo).as_posix()
        idx, stamp = read_stamp(p)
        if idx is None or not stamp:
            residual.append({"rel": rel, "why": "no Last-updated stamp"})
            continue
        try:
            stamp_d = datetime.strptime(stamp, "%Y-%m-%d").date()
        except Exception:
            residual.append({"rel": rel, "why": f"unparseable stamp {stamp!r}"})
            continue
        if stamp_d > cutoff:
            continue                      # fresh enough; not an item at all
        pd = proof_date(repo, rel, stamp)
        if pd:
            proven.append({"rel": rel, "line_idx": idx, "old": stamp, "new": pd})
        else:
            residual.append({"rel": rel, "why": f"stale {(today - stamp_d).days}d, no proven edit"})
    return {"proven": proven, "residual": residual}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = classify(REPO)
    if a.json:
        print(json.dumps(out))
        return 0
    print(f"proven (mechanically bumpable): {len(out['proven'])}")
    for x in out["proven"]:
        print(f"  {x['rel']}: {x['old']} -> {x['new']}")
    print(f"residual (the operator's — no non-lying value to write): {len(out['residual'])}")
    for x in out["residual"]:
        print(f"  {x['rel']}: {x['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
