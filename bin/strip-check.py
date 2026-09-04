#!/usr/bin/env python3
"""Does anything that SHIPS to the baseline carry personal data?

WHY A SCRIPT AND NOT A GREP (2026-08-29). The Makefile target scanned the whole working tree, so
on the baseline-WRITER Core it always failed — on sessions/, memory/ and CLAUDE.md, every one of
which is per_core_keep and never reaches the baseline. A check that cannot pass where it runs is a
check people learn to ignore, and this repo has a documented history of exactly that: instruments
that report red forever, get muted, and stop being read.

It also disagreed with .github/workflows/strip-check.yml, which searched a DIFFERENT pattern set —
so "clean locally" did not imply "clean in CI", and neither result meant much.

This reads bin/sync-manifest.json — the same authority sync-to-baseline.sh uses to decide what
actually travels — and scans exactly that set. Exit 1 on any hit.

Usage:  python3 bin/strip-check.py [--list]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parent.parent))
MANIFEST = REPO / "bin" / "sync-manifest.json"

# PATTERNS ARE DERIVED FROM *YOUR* IDENTITY, NEVER HARDCODED.
#
# This file used to carry the original author's surname, home-directory username and both real
# email addresses as literal regexes — and the ALLOW rule below exempts `strip-check` from its own
# scan, so it reported the tree CLEAN while being the single largest personal-data leak in it. A
# scrubber that publishes the identifiers it scrubs for is worse than no scrubber, because it also
# tells a reader exactly what to grep for.
#
# It was also useless to anyone else: scanning a stranger's repo for a name that is not theirs
# finds nothing and passes vacuously.
#
# So the list is built at runtime from three sources, none of which is committed:
#   1. .claude/identity.json — user.full_name, user.name, user.email (you fill this in on setup)
#   2. the current OS user name, which is what appears in an absolute home-directory path
#   3. bin/.strip-patterns — optional, gitignored, one extra regex per line, for anything else
#      (a second email, a partner's name, an employer) you never want to ship  # privacy-ok: illustrative category list, no real person named
# Plus one generic rule that needs no configuration: any absolute /Users/<someone>/ path.
GENERIC_PATTERNS = [
    # An absolute home-dir path leaks whoever's machine it came from. The exemptions are the
    # placeholder names this repo's own docs and fixtures use — measured, not guessed: the only
    # seven values present across the whole tree are you, USER, someone, other, x, u and n.
    # One-and-two-character names are exempt as a class because no real account is named "x",
    # and the alternative is an enumeration that silently rots the first time a fixture adds an
    # eighth placeholder. A real username (the one this file used to hardcode was 13 characters)
    # is still caught, and $USER is separately covered by the identity-derived patterns below.
    r"/Users/(?!you\b|USER\b|someone\b|other\b|[a-z]{1,2}\b)[a-z][a-z0-9_-]+/",
]


def _identity_patterns() -> list[str]:
    """Your own identifiers, from your own config. Absent config yields an empty list, which is
    correct for a fresh clone that has no identity yet — the generic rules still apply."""
    pats: list[str] = []
    try:
        idj = json.loads((REPO / ".claude" / "identity.json").read_text())
        user = idj.get("user") or {}
        for key in ("full_name", "name", "email", "contact_lookup_name"):
            v = (user.get(key) or "").strip()
            # Skip the shipped placeholders, and skip anything too short to be distinctive —
            # a two-character name as a regex would match half the repo.
            if len(v) < 4 or v.startswith("YOUR") or v.endswith("example.com"):
                continue
            pats.append(re.escape(v))
            # An email also leaks as its local part alone.
            if "@" in v:
                pats.append(re.escape(v.split("@", 1)[0]))
    except Exception:
        pass
    try:
        me = os.environ.get("USER") or ""
        if len(me) >= 4 and me not in ("root", "user", "runner"):
            pats.append(re.escape(me))
    except Exception:
        pass
    try:
        extra = (REPO / "bin" / ".strip-patterns").read_text().splitlines()
        pats += [ln.strip() for ln in extra if ln.strip() and not ln.strip().startswith("#")]
    except Exception:
        pass
    # De-duplicate while preserving order, so the reported pattern set is stable run to run.
    return list(dict.fromkeys(pats))


_IDENTITY_PATTERNS = _identity_patterns()
PATTERNS = GENERIC_PATTERNS + _IDENTITY_PATTERNS

# A CHECK MUST SAY WHAT IT CANNOT SEE. Deriving patterns from identity.json is correct — a forker's
# scrubber should hunt THEIR identifiers, not the original author's — but it has a blind spot with
# a precise shape: on the published TEMPLATE, identity.json is a placeholder, so this derives ZERO
# identity patterns and the only rules left are the generic ones. It then reports "Clean".
#
# Measured 2026-09-02: the same file, byte-identical in the author's private Core and in the public
# tree, carrying the author's first name four times. Private Core: 775 hits. Public tree: "Clean".
# Same bytes, opposite verdicts, and the difference was that one seat knew who to look for. A tool
# that reports clean because it could not look is the exact failure that shipped a personal-data
# file to a public repo earlier that day — "checked the shape, not the content" — one level down.
#
# So when no identity pattern could be derived, this now SAYS SO, on stderr, every run, with the
# explicit consequence, instead of letting "Clean" stand unqualified. It does not fail the run:
# a fresh fork with no identity yet is a legitimate state, and the generic rules still ran.
# The first version of this note fired only when the pattern list was EMPTY. That missed the
# sharper case, found immediately on testing: on the author's own machine the public tree derived
# exactly one pattern — the OS username from $USER — and reported "Clean" while scanning for a
# username and not a name. Coverage was silently a function of WHO RAN THE CHECK. So the note now
# fires whenever identity.json contributed nothing, and says what the run was actually able to see.
def _identity_json_contributed() -> bool:
    try:
        user = (json.loads((REPO / ".claude" / "identity.json").read_text()).get("user") or {})
        return any(len((user.get(k) or "").strip()) >= 4
                   and not str(user.get(k)).startswith("YOUR")
                   and not str(user.get(k)).endswith("example.com")
                   for k in ("full_name", "name", "email", "contact_lookup_name"))
    except Exception:
        return False


_IDENTITY_SOURCE_NOTE = None
if not _identity_json_contributed():
    _seen = ", ".join(_IDENTITY_PATTERNS) if _IDENTITY_PATTERNS else "nothing"
    _IDENTITY_SOURCE_NOTE = (
        f"strip-check: .claude/identity.json is the shipped placeholder (or absent), so NO name or "
        f"email was derived from it. Identity-shaped patterns this run could see: {_seen} "
        f"(from $USER / bin/.strip-patterns only). A 'Clean' below means the GENERIC rules and those "
        f"patterns found nothing — it does NOT mean the operator's name is absent from the tree. "
        f"Fill identity.json, or add names to bin/.strip-patterns, to scan for a specific person.")

# Legitimate appearances: the repo's own identity, packaging metadata, and the checks whose whole
# job is to search for these strings. Without this the checker flags itself.
ALLOW = re.compile(
    r"nicknur7/core-agent"
    r"|pyproject\.toml|CODEOWNERS|SECURITY\.md|CODE_OF_CONDUCT\.md|CONTRIBUTING\.md"
    r"|strip-check"
    r"|noreply@"
)

SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gz", ".dump", ".pyc", ".ico", ".woff", ".woff2"}


def _excluded(rel: str, patterns: list[str]) -> bool:
    """per_core_keep wins over shared.dirs — that is how the real sync behaves.

    Several per_core_keep entries sit INSIDE a shared dir: scheduling/brain-pg is shared, but
    compile-truth-work/, assert-work/, assertion-work/ and the eval sets under it are not. Those
    hold real working data. A first cut of this script ignored the exclusions and reported 16,145
    hits — every one a file that never ships. Subtracting them is the difference between a check
    that means something and a wall of noise.
    """
    import fnmatch
    for pat in patterns:
        if pat.endswith("/**"):
            if rel == pat[:-3] or rel.startswith(pat[:-2]):
                return True
        elif fnmatch.fnmatch(rel, pat) or rel == pat:
            return True
    return False


def shipped_files() -> list[Path]:
    """Exactly the files sync-to-baseline.sh would send: shared.dirs + shared.files, MINUS
    per_core_keep."""
    man = json.loads(MANIFEST.read_text())
    keep = man.get("per_core_keep", [])
    out: list[Path] = []
    for d in man["shared"]["dirs"]:
        base = REPO / d
        if base.is_dir():
            out += [p for p in base.rglob("*")
                    if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    for f in man["shared"]["files"]:
        p = REPO / f
        if p.is_file():
            out.append(p)
    return sorted({p for p in set(out)
                   if not _excluded(str(p.relative_to(REPO)), keep)})


def main() -> int:
    if not MANIFEST.exists():
        print(f"strip-check: {MANIFEST} missing — cannot determine what ships")
        return 1

    files = shipped_files()
    if "--list" in sys.argv:
        for p in files:
            print(p.relative_to(REPO))
        return 0

    rx = re.compile("|".join(PATTERNS))
    hits: list[str] = []
    archived_skipped = 0
    for p in files:
        if p.suffix in SKIP_SUFFIX:
            continue
        # The checker holds the patterns it searches for; flagging itself is noise.
        if p.name in {"strip-check.py"}:
            continue
        # Archived hooks are kept as history, not shipped behaviour. They still travel, so this
        # is a NOTE not an exemption — see the archive line in the report footer.
        if "/hooks/archive/" in str(p):
            archived_skipped += 1
            continue
        try:
            txt = p.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            if rx.search(line) and not ALLOW.search(line):
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")

    print(f"strip-check: scanned {len(files)} files that actually ship to the baseline")
    if archived_skipped:
        print(f"  ({archived_skipped} archived hook file(s) under hooks/archive/ ship but were NOT scanned — "
              f"kept as history, not shipped behaviour)")
    if hits:
        print(f"FAIL — personal data on {len(hits)} shipped line(s):")
        for h in hits[:30]:
            print("  " + h)
        if len(hits) > 30:
            print(f"  … and {len(hits) - 30} more")
        return 1
    if _IDENTITY_SOURCE_NOTE:
        print(_IDENTITY_SOURCE_NOTE, file=sys.stderr)
    print("Clean — nothing that ships to the baseline carries personal data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
