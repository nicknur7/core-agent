#!/usr/bin/env python3
"""Shared code must not read or write a path every Core shares.

WHY THIS IS A GATE AND NOT THREE MORE FIXES
-------------------------------------------
This defect class was found three separate times, by accident, in three different files:

  1. backfill-hook-events.py hardcoded core-life's path — every Core would have scanned
     core-life's transcripts.
  2. core-doctor.sh read /tmp/core-hook-events.log — machine-global. A freshly spawned Core
     reported "24 events, hook=say-do-gap-test", all of them core-life's.
  3. run-brain-update.sh WROTE /tmp/brain-stop-hook.log and core-si/detect.sh READ it to
     decide whether THIS Core's brain pipeline had failed. A write/read pair across every
     Core on the machine: core-life could report a failure that was core-business's embed.

session-lifecycle.sh had already been fixed for exactly this in June 2026 and left a comment
saying so. The comment did not stop it recurring, because a comment cannot. Everything in
this repo is SHARED — it syncs to every Core and to an external fork — so a bare /tmp path in
shared code is a cross-tenant channel by default, and the person adding one is never thinking
about the other Cores.

WHAT COUNTS AS SAFE
-------------------
A /tmp path is fine when it is namespaced by something per-Core ($$, CORE_INSTANCE, the repo
basename, a mktemp), or when sharing is the POINT. The brain lock is the clearest example of
the latter: one brain, one lock, all Cores queue on it deliberately. So this checks for a
discriminator rather than banning /tmp, and carries an explicit allowlist for the paths whose
sharing is intentional — each with a reason, so the next reader can tell design from decay.

    python3 bin/tests/test_no_cross_core_paths.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# Directories that sync to every Core. Anything here is shared by definition.
SHARED = ["bin", ".claude/hooks", ".claude/rules", ".claude/agents",
          "scheduling/brain-pg", "scheduling/graphify-brain", "scheduling/brain-lint",
          "scheduling/claude-si", "scheduling/core-si", "scheduling/system-health"]

# "tests" WAS IN THIS SET AND THAT IS WHERE THE INCIDENTS LIVED. core-business found four shared
# tests hardcoding another Core's absolute path — two of them WRITING into that Core's live tree
# from every Core at every session close — and this gate could not see any of them, because it
# skipped the directory they were in. A gate that excludes the place the class recurs is not a
# gate. bin/tests/ ships to five Cores; it is the LAST directory that should be exempt.
SKIP_DIRS = {"archive", "__pycache__", "node_modules", "migrations"}


def _per_core_keep() -> set:
    """Files that live in a shared DIR but never sync — they are per-Core already.

    Read from bin/sync-manifest.json rather than listed here, so this cannot drift from
    what the push script actually excludes. A file the sync excludes is not shared code and
    holding it to a shared-code rule produces a false failure, which is how a useful test
    becomes one people disable.
    """
    import json
    try:
        m = json.loads((REPO / "bin" / "sync-manifest.json").read_text())
    except Exception:
        return set()
    out = set()
    for pat in m.get("per_core_keep", []):
        out.add(pat.rstrip("/*").rstrip("/"))
    return out

# Tokens that make a /tmp path per-Core (or per-run). Any one is enough.
DISCRIMINATORS = ["$$", "CORE_INSTANCE", "CORE_BRAIN", "basename", "mktemp", "getpid",
                  "session", "SESSION", "session_key", "${key}", "digest", "uuid",
                  # any hash-ish variable, in either case, however prefixed (_bhash, BRAIN_HASH)
                  "hash", "HASH",
                  # the repo's own name is per-Core by construction
                  "REPO.name", "REPO_NAME", "repo_name"]

# Paths whose sharing across Cores is DELIBERATE. Reason required.
#
# TWO MAPS, ON PURPOSE. ALLOWED (below) is matched against the CAPTURED PATH by equality for the
# /tmp check. ALLOWED_PATHS does the same for Core paths. ALLOWED_SITES is for fixture prose that
# is not a path at all — and it is FILE-SCOPED, because an exemption that applies everywhere is how
# one verified fixture silently exempts an unrelated real defect in another file.
ALLOWED = {
    "/tmp/core-hook-events.log":
        "legacy fallback only, read when a Core has no durable log of its own yet",
    "/tmp/brain-stop-hook.log":
        "same — legacy fallback in detect.sh, used only when the per-Core log is absent",
    "/tmp/exfil.md":
        "fixture: the exfil path IS the thing the pragma-scope test detects. SELF-MATCHING IS "
        "UNAVOIDABLE — this dict must contain the literal and the checker scans its own source. "
        "core-business confirmed it benign by isolation: comment out the only genuine usage site "
        "and this key line is the sole remaining occurrence, and the gate still passes. The /tmp "
        "check compares the CAPTURED PATH by equality, so an entry cannot over-exempt anything "
        "but itself.",
}

# Exact CAPTURED Core paths that are deliberate.
#
# THE SELF-MATCH IS MECHANICALLY UNAVOIDABLE AND BENIGN, confirmed in isolation by core-business
# (#909): the checker scans its own source, and an exemption dict must contain the literal string it
# exempts, so every key here is also an occurrence of the pattern. Commenting out the only genuine
# usage site left this dict's own definition line as the sole match and the gate still passed.
#
# It is not a coverage hole, and the reason is worth stating rather than trusting: the comparison at
# the check site is EXACT AGAINST THE CAPTURED PATH, so a self-matching key can only ever exempt the
# identical string — it cannot widen to a neighbouring path on the same line. That is the difference
# between this and BLOCK 1, where the same dict was consulted by asking whether the surrounding LINE
# contained a key, and one comment mentioning an allowed path exempted every Core path beside it.
ALLOWED_PATHS = {
    "/Users/n/AI Projects/core-life":
        "fixture: a sample push command fed to the review-gate and blast-radius detectors",
    "https://github.com/nicknur7/my-own-core.git":
        "fixture: push-isolation asserts against a SYNTHETIC own-repo remote. It used to name the author's real private repo 'on purpose'; on a public repo that is a signpost to private infrastructure, and the normalisation logic never needed a real value",
    "nicknur7/my-own-core":
        "same fixture, short form — and this entry's own line is why ALLOWED_SITES exists: an "
        "exemption's definition can itself match the pattern it exempts",
}

# (file, line-fragment) — scoped, because prose is not a path and must not exempt globally.
ALLOWED_SITES = [
    ("bin/tests/_core.py", "shared directory that hardcode"),
    ("bin/tests/test_no_cross_core_paths.py", "fixture: a sample push command"),
    ("bin/tests/test_no_cross_core_paths.py", "fixture: push-isolation asserts"),
    ("bin/tests/test_push_isolation.sh", "nicknur7/my-own-core"),
    ("bin/casebook-run.py", "cross_core_claim"),
    ("bin/peer-mcp-server.py", "CORE_DOMAIN_LABEL"),
    (".claude/hooks/tests/test-brain-recall-referent.py", "pasted statusline"),
]

PAT = re.compile(r'["\'=(]\s*(/tmp/[A-Za-z0-9_.\-]+)')

# AND IT ONLY EVER MATCHED /tmp/. A hardcoded `/Users/<someone>/AI Projects/core-<name>` is a
# different pattern shape entirely, so the four real incidents would have been invisible even with
# bin/tests/ scanned. This is the shape that actually hurts: it names ANOTHER CORE, so the file
# reads that Core's state — or writes to it — while reporting the answer as its own.
# CAPTURE THE PATH, NOT THE LINE. The first cut captured everything between the quotes, so an
# ALLOWED entry could never match exactly and a verified fixture stayed red — a gate whose
# exemption mechanism does not work is a gate someone turns off.
CORE_PATH_PAT = re.compile(
    # A PATH HAS core-<name> DIRECTLY AFTER A SLASH. Prose says "yes core-life" or "leaked into
    # core-ops" with a SPACE before it, and the previous cut flagged all of it — a gate that
    # cries wolf on English is one someone switches off, which is the same failure mode as one
    # that misses real defects, arrived at from the other side.
    r'["\']([^"\']*[/~]core-(?:life|business|school|finance|ops)[^"\']*)["\']')

FAIL: list[str] = []


def main() -> int:
    print("cross-Core path isolation")
    PCK = _per_core_keep()
    checked = 0
    for d in SHARED:
        root = REPO / d
        if not root.exists():
            continue
        for f in list(root.rglob("*.py")) + list(root.rglob("*.sh")):
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            rel = str(f.relative_to(REPO))
            if rel in PCK or any(rel.startswith(x + "/") for x in PCK):
                continue
            checked += 1
            try:
                lines = f.read_text(errors="replace").splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines, 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                for m in PAT.finditer(line):
                    path = m.group(1)
                    if path in ALLOWED:
                        continue
                    # The discriminator may sit anywhere on the line (a variable interpolated
                    # into the path, a suffix appended after it).
                    if any(tok in line for tok in DISCRIMINATORS):
                        continue
                    FAIL.append(f"{f.relative_to(REPO)}:{i}  {path}  — {stripped[:70]}")
                # A HARDCODED CORE PATH is the shape that actually hurts: it names ANOTHER Core,
                # so the file reads that Core's state — or writes into it — while reporting the
                # answer as its own. Checked in the same pass, same exemptions.
                for m in CORE_PATH_PAT.finditer(line):
                    path = m.group(1)
                    if any(tok in line for tok in DISCRIMINATORS):
                        continue
                    # EQUALITY ON THE CAPTURED PATH, plus a FILE-SCOPED site exemption. The
                    # previous version tested `if any(k in line for k in ALLOWED)` — it captured
                    # `path` and never used it, so ANY line containing an allowed key anywhere (in
                    # a comment, in an unrelated string, in a different path on the same line)
                    # exempted EVERY Core path on that line. core-business proved it by planting a
                    # synthetic file naming core-finance; the gate passed it.
                    #
                    # I INTRODUCED THAT AN HOUR EARLIER, deliberately, reasoning that line-keys
                    # were "more stable" than exact captures. They are more stable and they do not
                    # test the thing. FOURTH INSTANCE TODAY of substring-where-exact-is-required,
                    # and the second where the comment asserted a property the code lacked — this
                    # time inside the file built to prevent exactly this class.
                    if path in ALLOWED_PATHS:
                        continue
                    site = f"{f.relative_to(REPO)}"
                    if any(site == s and frag in line for s, frag in ALLOWED_SITES):
                        continue
                    FAIL.append(f"{f.relative_to(REPO)}:{i}  {path}  — hardcoded Core path")

    # The class is subtle enough that a passing run should still say what it checked.
    print(f"  scanned {checked} shared files across {len(SHARED)} synced dirs "
          f"({len(PCK)} per-core-keep patterns skipped)")
    if FAIL:
        print(f"  FAIL  {len(FAIL)} shared path(s) with no per-Core discriminator:")
        for x in FAIL:
            print(f"        {x}")
        print("\n  Namespace it per-Core, or add it to ALLOWED with a reason if the")
        print("  sharing is deliberate (like the brain lock: one brain, one lock).")
        return 1
    print("  PASS  every /tmp path in shared code is per-Core or explicitly allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
