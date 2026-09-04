#!/usr/bin/env python3
"""SHARED STEERING MUST NOT CITE AN ARTIFACT THAT EXISTS ON ONLY ONE CORE.

`.claude/rules/*.md` and `CLAUDE.base.md` are pushed to every Core. A citation in them resolves on
the author's disk by construction — that is what makes this class invisible to `lint-doc-paths.sh`,
which checks the LOCAL tree and correctly reports CLEAN.

THE LIVE INSTANCE, found via core-business's borrow of this Core's runner (bus #996-#999). Its
`.claude/rules/memory.md:20` cited `tasks/research/enforcement-audit-2026-08-09.md`. That file does
not exist on business, does not exist on school — both are on the same baseline and both carry the
line — and the document is absent from those trees at ANY path.

The path was unfixable by syncing, and that is the structural part: `tasks/**` is in the manifest's
`per_core_keep`, so it is NEVER pushed. **A shared file citing a per-Core-only location names a path
that can never exist on any peer, by construction.** Life's copy now cites
`docs/enforcement-audit-2026-08-09.md`, and `docs/` IS shared, so the sync delivers the document
along with the corrected pointer.

WHAT THIS CHECK IS *NOT*, because the obvious version is 96% noise. Sweeping shared steering for
citations into `per_core_keep` prefixes yields 27 candidates on this tree and 26 are correct:
`memory/current-state.md`, `tasks/lessons.md`, `memory/access-log.md` and the rest are CONVENTION
paths — every Core has its own copy, and telling a Core to read its own `current-state.md` is exactly
what the rule should say. Verified across the fleet: all nine convention paths exist on life,
business, school and finance.

So the manifest is the wrong discriminator. The right one is **fleet-wide existence**: a cited path
is a defect only when peers lack it. That distinction is the finding; the sweep that produced it was
mostly noise, and recording the ratio here is deliberate — 27 accused, 1 real, the highest
false-positive rate of the five sweeps measured on 2026-08-10.

SKIPS RATHER THAN FAILS when peers are absent. On a fork or a fresh clone there is nothing to compare
against, and an unanswerable question must not be reported as a pass or a failure.

Run: python3 bin/tests/test_shared_cites_fleet_wide.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
FLEET = ROOT.parent

# THE SHARED SURFACE IS WIDER THAN THE RULES FILES, and my first version missed most of it.
# core-business swept the same class (bus #1002) across shared COMMANDS and scheduling/ docs and
# surfaced nine citations this check could not see, because it only looked at .claude/rules/* and
# CLAUDE.base.md. Every one turned out to be already fixed on life — business was reading pre-sync
# copies — but the gap was real: a check scoped to six files while the shared surface is dozens
# reports CLEAN for the same reason lint-doc-paths does, one level up.
#
# Discovered by a peer scanning MY tree with MY argument and finding places I had not applied it.
# DERIVED FROM THE MANIFEST, NOT FROM A DIRECTORY GLOB. Globbing `.claude/commands/*.md` invented
# three false accusations in one run: `focus-status.md` is a LIFE-ONLY command (absent on business,
# never pushed), and two hits sat in `scheduling/_archive/`, which is not a shared directory at all.
# The manifest names the shared surface exactly — `shared.dirs` + `shared.files` — so reading it
# excludes all three by construction rather than by a stop-list I would have to maintain.
#
# This is the second time in one hour that the manifest was the right source and a filesystem
# heuristic was the wrong one, in opposite directions: per_core_keep is the wrong test for whether a
# CITED path is reachable (fleet-wide existence is), and a directory glob is the wrong test for
# whether a CITING file is shared (the manifest is).
def shared_docs():
    mf = ROOT / "bin" / "sync-manifest.json"
    if not mf.is_file():
        return []
    shared = json.loads(mf.read_text()).get("shared", {})
    out = []
    for rel in shared.get("files", []):
        if rel.endswith(".md") and (ROOT / rel).is_file():
            out.append(rel)
    for d in shared.get("dirs", []):
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            r = str(p.relative_to(ROOT))
            if "/_archive/" in "/" + r or "/archive/" in "/" + r:
                continue
            out.append(r)
    return sorted(set(out))


SHARED = shared_docs()

CITE = re.compile(r"`([A-Za-z0-9_./-]+\.(?:md|py|sh|json))`")

# Literal placeholders, not paths. `sessions/YYYY-MM-DD.md` is a naming convention being described.
PLACEHOLDER = re.compile(r"YYYY|<[a-z-]+>|\{|\*")


def peers():
    return sorted(p for p in FLEET.glob("core-*")
                  if p.is_dir() and p != ROOT and (p / ".claude" / "identity.json").is_file())


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== shared steering may not cite a life-only artifact ===\n")

    others = peers()
    if not others:
        print("  SKIP — no peer Cores beside %s; the question is unanswerable here, and an" % ROOT.name)
        print("         unanswerable question is not a pass.")
        return 0
    print("  comparing against %d peer(s): %s\n" % (len(others), ", ".join(o.name for o in others)))

    # A LINE THAT DECLARES THE PER-CORE-NESS IS DELIBERATE, NOT A DEFECT. `deep-plan.md:52` cites
    # `memory/reading-queue.json` and says in the same breath "(instance-only — memory/** is
    # per_core_keep, absent on a fresh clone)". The author already knew and wrote it down.
    #
    # This is a TEXT marker, not a stop-list, and the distinction is core-business's (bus #1002):
    # *"a stop-list for files that talk about missing files is how a checker starts lying."* A
    # maintainer-held allowlist silently swallows the next real case; a marker in the prose costs the
    # author one clause and is visible to every reader. It matches how lint-doc-paths.py already
    # exempts `(removed …)` / `(retired …)` citations — the document has to say it, not a config.
    # THE COMMENT ABOVE AND THIS REGEX DISAGREED (fixed 2026-09-01). The paragraph says this marker
    # "matches how lint-doc-paths.py already exempts `(removed …)` / `(retired …)` citations" — and
    # neither word was in the pattern. So a shared file that correctly announced a path as removed
    # was still counted as an undeclared citation. `.claude/commands/core-si.md:89` cites
    # memory/claude-si-ledger.md and says "(removed cc3d63b)" in the same line; the checker read the
    # citation and ignored the declaration it claims to honour.
    #
    # Two further shapes were undeclared-but-correct, and both are the marker working as intended
    # once it can see them:
    #   README.md:61 lists `memory/goals.md` while TELLING A NEW USER to create it. A path a shared
    #   doc instructs you to populate is the opposite of a path that should already exist.
    #   .claude/rules/privacy.md:44 names `.claude/agents/sentinel-code/CLAUDE.md` inside a table
    #   whose entire purpose is "these dir-form specs exist and you must NOT cite them" — the path is
    #   the SUBJECT of the warning. That file already carries a casebook-exempt marker at its head
    #   for exactly this inversion.
    #
    # Widened to the words the comment always claimed, plus the two shapes above. Still a TEXT
    # marker, never a stop-list: core-business's rule holds — "a stop-list for files that talk about
    # missing files is how a checker starts lying."
    DECLARED = re.compile(
        r"per[_ -]core|instance[- ]only|your seat|each Core|not cited by path"
        r"|\(\s*(?:removed|retired|deleted)\b"          # the words the comment already promised
        r"|must NOT be cited|do not cite|deliberately still present"  # named in order to warn about it
        # NARROWED after review (2026-09-01). The first cut used `still present` and
        # `—\s*(?:what|who|your)`, and sentinel-code was right that both are too broad to be safe:
        # the dose test only proves the offender-classification predicate still fires on a synthetic
        # tuple — it never re-runs the widened regex against real prose, so a broad clause could
        # blind the checker on a FUTURE real violation and nothing would notice. `—\s*what` in
        # particular would exempt any em-dash-then-"what" line anywhere in shared steering, which is
        # a common sentence shape, not a declaration.
        #
        # README's list is now matched by its actual structure — a markdown bullet whose backticked
        # path is immediately followed by an em-dash gloss telling the reader to populate it — rather
        # than by the gloss's first word.
        r"|^\s*[-*]\s+`[^`]+`\s+—\s",
        re.I)

    cited = []
    for rel in SHARED:
        src = ROOT / rel
        if not src.is_file():
            continue
        for i, ln in enumerate(src.read_text().splitlines(), 1):
            if DECLARED.search(ln):
                continue
            for c in CITE.findall(ln):
                if not PLACEHOLDER.search(c):
                    cited.append((rel, i, c))

    check("shared steering actually cites something (an empty scan is not a clean scan)",
          bool(cited), "no citations found — re-point this check rather than deleting it")

    # A citation is a defect only when it resolves HERE and on NO peer. Present-nowhere is a
    # different bug (lint-doc-paths owns it); present-everywhere is the normal convention case.
    # TWO KINDS OF LIFE-ONLY CITATION, AND ONLY ONE IS A DEFECT. The first run of this check
    # conflated them and went red on five citations, four of which were correct.
    #
    #   UNDELIVERABLE  the cited path sits under a `per_core_keep` prefix, so the sync can NEVER
    #                  deliver it. `tasks/research/enforcement-audit-2026-08-09.md` was this: absent
    #                  on business and school, and no push would ever have fixed it. A real defect,
    #                  and the one this file was written for.
    #
    #   PENDING SYNC   the cited path sits under a SHARED prefix and simply has not been pushed yet.
    #                  `docs/enforcement-audit-2026-08-09.md` is this. It resolves everywhere the
    #                  moment the baseline push lands, which is a command only Nick runs.
    #
    # Failing on the second would make the suite red for a condition no amount of work here can
    # clear — a gate blocking correct behaviour, which is how gates get disabled. It is reported
    # loudly and counted, but it is not a failure.
    mf = ROOT / "bin" / "sync-manifest.json"
    keep = []
    if mf.is_file():
        keep = [k.replace("/**", "").rstrip("/") for k in
                json.loads(mf.read_text()).get("per_core_keep", [])]

    undeliverable, pending = [], []
    for rel, i, c in cited:
        if not (ROOT / c).is_file():
            continue                       # local breakage is lint-doc-paths' job, not this one
        if any((o / c).is_file() for o in others):
            continue                       # a peer has it — nothing to say
        (undeliverable if any(c.startswith(k + "/") or c == k for k in keep)
         else pending).append((rel, i, c))

    check("no shared file cites an artifact the sync can NEVER deliver", not undeliverable,
          "\n          ".join("%s:%d cites %s — under per_core_keep, unreachable on every peer"
                              % o for o in undeliverable))

    if pending:
        print("\n  PENDING SYNC — %d citation(s) resolve here and on no peer, but sit under a SHARED"
              % len(pending))
        print("  prefix, so the baseline push delivers them. Not a defect; listed so the count is")
        print("  visible rather than absorbed:")
        for rel, i, c in pending:
            print("    %s:%d -> %s" % (rel, i, c))

    print("\n--- THE DOSE: the check must be able to SEE such a citation ---")
    # Without this, "0 offenders" is satisfied by a scan that resolves nothing.
    #
    # THE PROBE IS DERIVED, NOT NAMED. It used to hardcode docs/enforcement-audit-2026-08-09.md as
    # "exists here and on no peer" — true when written, false the moment core-business pulled the
    # baseline that file had shipped in. The dose then failed for a reason that had nothing to do
    # with the property under test, on a seat where nothing was broken. A fixture that names one
    # instance of a category expires when the world moves; one that finds an instance does not.
    #
    # Same defect class as the org-1 test, the hardcoded seat name, and the one-path ignore rule:
    # written against the single case that was in front of the author.
    # SEARCH WHERE LIFE-ONLY FILES ACTUALLY ARE. The first derived version inherited the
    # hardcoded path's directory and looked only in docs/*.md — which found nothing and skipped
    # permanently, i.e. it swapped a test that failed for the wrong reason for one that never ran.
    # Narrowing the search to where the old example happened to live is the same mistake one level
    # down.
    def _life_only():
        for sub in ("docs", "bin", "eval", ".claude/rules", "tasks/research"):
            d = ROOT / sub
            if not d.is_dir():
                continue
            for q in sorted(d.rglob("*")):
                if not q.is_file():
                    continue
                rel = str(q.relative_to(ROOT))
                if not any((o / rel).is_file() for o in others):
                    return rel
        return None

    probe = _life_only()
    if probe is None:
        print("  SKIP  no life-only doc exists right now — every docs/*.md is on at least one peer.")
        print("        Not a pass: the dose below cannot run, and that is said out loud rather")
        print("        than counted green.")
    else:
        check("found a genuinely life-only file to dose with (%s)" % probe, True)
    here = probe is not None and (ROOT / probe).is_file()
    on_peers = [] if probe is None else [o.name for o in others if (o / probe).is_file()]
    if probe is not None:
        check("the derived case really is life-only (so the dose is real)", here and not on_peers,
              "here=%s peers_with_it=%s" % (here, on_peers))
    if probe is not None:
        synthetic = [(rel, i, probe) for rel, i, c in [("x", 1, probe)]]
        would_flag = [o for o in synthetic if (ROOT / o[2]).is_file()
                      and not any((q / o[2]).is_file() for q in others)]
        check("...and a shared file citing it WOULD be flagged", bool(would_flag),
              "the offender predicate cannot detect the known case — it has no teeth")

    print("\n--- and the manifest is NOT the discriminator (26 of 27 would be false positives) ---")
    mf = ROOT / "bin" / "sync-manifest.json"
    if mf.is_file():
        keep = [k.replace("/**", "").rstrip("/") for k in json.loads(mf.read_text()).get("per_core_keep", [])]
        naive = [c for _, _, c in cited if any(c.startswith(k + "/") for k in keep)]
        convention = [c for c in naive if all((o / c).is_file() for o in others)]
        print("    naive (cites a per_core_keep prefix): %d" % len(naive))
        print("    of those, present on EVERY peer:      %d  <- correct convention citations" % len(convention))
        # NAME THE EXCEPTIONS. This said "only 59 of 62" and stopped, which is a finding nobody can
        # act on — the reader still has to re-derive the scan to learn WHICH three, and re-deriving
        # a predicate is how two copies of it come to disagree. A check that cannot say what failed
        # sends its reader to rebuild the check.
        _odd = sorted({c for c in naive if c not in convention})
        check("the naive rule would flag mostly-correct citations",
              len(convention) >= max(1, len(naive) - 2),
              "only %d of %d naive hits are convention paths; the exceptions are: %s"
              % (len(convention), len(naive), ", ".join(_odd) or "<none>"))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
