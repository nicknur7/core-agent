"""A SHARED FILE MAY ONLY CITE SHARED PATHS.

core-business found four steering files in `.claude/rules/` — a SHARED dir — citing
`tasks/research/enforcement-audit-2026-08-09.md`, and `tasks/**` is per_core_keep. The citation
resolved on life and dangled on business, school, finance and ops.

THE AUTHOR IS THE ONE SEAT THAT CANNOT SEE IT. On the Core that wrote the file, the target exists;
the reference looks correct forever. That is the same structural blindness as the PEERS line
hardcoding life's peer set, and as the auditor that measured whichever Core it lived in — three
instances in one day of a defect visible from every seat except the one that creates it.

Found only because the casebook's S2 check started working an hour earlier. S2 had been an
always-pass check, so nothing had ever read the lint's answer.

Run: python3 bin/tests/test_shared_cites_shared.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
MANIFEST = json.loads((ROOT / "bin" / "sync-manifest.json").read_text())
SHARED_DIRS = MANIFEST["shared"]["dirs"]
SHARED_FILES = set(MANIFEST["shared"]["files"])
PER_CORE = MANIFEST.get("per_core_keep", [])

PLACEHOLDER = re.compile(r"(YYYY|MM-DD|<[^>]+>|\bname\b)")
CITE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:md|py|sh|json|jsonl))`")


def is_shared(rel: str) -> bool:
    # PER_CORE IS SUBTRACTED FIRST, and until 2026-08-13 it was not subtracted at all here.
    # core-finance, bus #1428, on their own seat: this classified a file as shared by DIRECTORY
    # membership alone, so `.claude/agents/sentinel.md` counted as shared because it sits under
    # `.claude/agents` — while the manifest lists that exact path in per_core_keep, beside
    # sentinel-code.md and the sentinel/** glob.
    #
    # The consequence is the inversion of what a per-Core file is for: a per-Core file citing a
    # per-Core path was reported as a shared file citing one. All 4 findings on finance were that,
    # 4 of 4 false.
    #
    # PER_CORE was already loaded, and is_per_core already existed — applied to the citation TARGET
    # below and never to the citing FILE. The list was in the room the whole time.
    #
    # Third instance tonight of one shape: a classifier that enumerates what is IN without
    # subtracting what is OUT. The tombstone guard compared exact strings against a half-glob list;
    # this one never consulted the list. Reusing is_per_core rather than adding a second matcher is
    # deliberate — a parallel implementation is how the two would drift apart.
    if is_per_core(rel):
        return False
    if rel in SHARED_FILES:
        return True
    return any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in SHARED_DIRS)


def is_per_core(rel: str) -> bool:
    for pat in PER_CORE:
        base = pat.replace("/**", "").rstrip("/")
        if rel == base or rel.startswith(base + "/"):
            return True
    return False


# ── THE CLASSIFIER IS CHECKED IN BOTH DIRECTIONS BEFORE IT IS TRUSTED ────────────────────────
#
# core-finance, sending the fix above: "the second row is the half I would have skipped if I were
# only killing the false positives: a `_per_core` that over-matched would silently stop scanning
# genuinely shared files, and the test would go green by checking nothing."
#
# That was live here. `scanned` was PRINTED and never ASSERTED, so an exclusion that swallowed
# every shared file would fall straight through to "PASS every citation resolves on every Core"
# over an empty sweep — the vacuous pass this suite refuses everywhere else.
#
# A POSITIVE CONTROL RATHER THAN A COUNT FLOOR. A number like `scanned >= 20` drifts with the file
# census and means nothing on a Core with a different shape. These six paths pin the property
# itself: three that per_core_keep names explicitly and three that no reading of the manifest makes
# per-Core. If either column moves, the classifier changed and every verdict below is about a
# different set of files.
_MUST_NOT_BE_SHARED = [".claude/agents/sentinel.md",
                       ".claude/agents/sentinel/CLAUDE.md",
                       ".claude/agents/sentinel-code.md"]
_MUST_BE_SHARED = [".claude/agents/close-reconciler.md",
                   ".claude/rules/privacy.md",
                   ".claude/hooks/stop-hook.sh"]
_ctl = []
for _r in _MUST_NOT_BE_SHARED:
    if is_shared(_r):
        _ctl.append("%s is per_core_keep but classified SHARED — the defect finance found: a "
                    "per-Core file citing a per-Core path gets reported as a shared file citing "
                    "one, which is what a per-Core file is FOR" % _r)
for _r in _MUST_BE_SHARED:
    if not is_shared(_r):
        _ctl.append("%s is genuinely shared but classified per-Core — the exclusion over-matches, "
                    "and an over-matching exclusion makes this whole file pass by scanning "
                    "nothing" % _r)
if _ctl:
    print("test_shared_cites_shared")
    for _c in _ctl:
        print("  FAIL  %s" % _c)
    print("\n  the shared/per-Core classifier is wrong; not reporting citations measured with it\n")
    sys.exit(1)

bad = []
scanned = 0
for d in SHARED_DIRS + [""]:
    root = ROOT / d if d else ROOT
    if not root.is_dir():
        continue
    for f in root.rglob("*.md"):
        rel = str(f.relative_to(ROOT))
        if not is_shared(rel) or "/archive/" in rel or "__pycache__" in rel:
            continue
        scanned += 1
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith(("#", ">")):
                continue           # prose and block quotes describing the problem, not citing it
            for m in CITE.finditer(line):
                target = m.group(1)
                if not is_per_core(target) or is_shared(target):
                    continue
                # A CONVENTIONAL per-Core path is FINE. memory/current-state.md is per_core_keep
                # and EVERY Core has one — a shared rule citing it resolves everywhere, to that
                # Core's own copy. The defect is a per-Core path that exists on ONE Core only.
                #
                # The first cut flagged all 126, including every reference to current-state.md and
                # sessions/. A gate that cries wolf at 126 is one someone switches off, which is
                # the same uselessness as one that misses the real thing — I wrote that two hours
                # ago about a different gate and then built this one the same way.
                if PLACEHOLDER.search(target):
                    continue
                # A PATTERN LIST IS NOT A CITATION. sentinel-code.md enumerates the per_core_keep
                # globs it must never approve — `memory/**`, `sessions/**`, `CLAUDE.local.md` — and
                # the whole point is that those are per-Core. Flagging them inverts the file's
                # meaning.
                if line.count("`") >= 6 or "**`" in line or "/**" in line:
                    continue
                # AND A CITATION THAT ALREADY DECLARES ITSELF instance-only is doing the right
                # thing: it tells the reader the path is theirs, not the author's.
                if re.search(r"instance-only|per[- ]Core|your own|each Core"
                             r"|optional|gitignored|if present|may not exist", line, re.I):
                    continue
                peers = [d for d in ROOT.parent.glob("core-*")
                         if d.is_dir() and d.resolve() != ROOT.resolve()
                         and (d / ".claude" / "identity.json").is_file()]
                if not peers:
                    continue          # cannot judge without a peer to compare against
                elsewhere = any((d / target).exists() for d in peers)
                if not elsewhere:
                    bad.append("%s:%d cites %s — exists on %s ONLY, dangles on %s"
                               % (rel, i, target, ROOT.name,
                                  ", ".join(d.name for d in peers)))

print("\n  scanned %d shared markdown file(s)\n" % scanned)
if bad:
    for b in bad[:20]:
        print("  FAIL  %s" % b)
    print("\n  %d shared file(s) cite a per-Core path.\n" % len(bad))
    sys.exit(1)
print("  PASS  every citation in a shared file resolves on every Core\n")
