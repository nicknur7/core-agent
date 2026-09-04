#!/usr/bin/env python3
"""Lint markdown files for broken in-repo path references.

For every .md file in the Core instance repo, find path-shaped strings under
known repo-internal prefixes and verify each cited path actually exists on
disk. Reports broken references with file:line for fixing.

USAGE:
  bash bin/lint-doc-paths.sh            # human report, exit 1 if broken
  bash bin/lint-doc-paths.sh --count    # just count (for SessionStart)
  bash bin/lint-doc-paths.sh --json     # machine output

COVERAGE — LIVE DOCS ONLY (the ones we actually re-read for current state):
  CLAUDE.md, CLAUDE.local.md
  .claude/rules/*.md
  .claude/agents/**/CLAUDE.md
  .claude/commands/*.md
  memory/projects/*.md (excluding memory/projects/archive/)
  memory/relationships/*.md
  memory/about-me.md, preferences.md, goals.md, skills-interests.md
  memory/capabilities.md
  bin/*.md, README.md (if present)
  tasks/*.md, tasks/research/*.md, tasks/specs/*.md — UNDATED (standing) docs only

EXCLUDED (immutable snapshots — historical paths are expected to be stale):
  sessions/*.md (write-once session logs)
  memory/brain-lint-reports/*.md (frozen reports)
  memory/access-log.md (append-only log)
  memory/decisions-log.md (append-only history)
  tasks/**/*-YYYY-MM-DD*.md (date-stamped = timeboxed snapshot; citations record
    what was true when written — rewriting them falsifies the record)
  tasks/lessons-archive.md (append-only history, same class as decisions-log)
  memory/projects/archive/*
  Anything under archive/ or _archive/

NOT COVERED (known gaps):
  - $CORE_INSTANCE / $CORE_BRAIN env-var paths (engine paths resolve via fallback)
  - Paths in .sh / .py / .json (those are source-of-truth)
  - Paths with placeholders (YYYY-MM-DD, <slug>, gmail-<localpart>, *)
  - External system paths (~/Library, /tmp/, /usr/local/...)
  - Paths reconstructed from string concat or sed expressions

ENGINE FALLBACK:
  If `$CORE_ENGINE` is set and points to an existing dir, paths that don't
  resolve under the instance repo are checked under the engine repo before
  being reported broken. Lets instance docs cite engine-resident scripts
  (`scheduling/brain-pg/query.py`, etc.) without false positives.

WHEN THE LINT FAILS:
  The cited path moved or was deleted; the doc still names the old location.
  Fix the doc to match reality, or fix the code to match the doc — but NEVER
  ignore the diff. Drift compounds.
"""
# Required for the `Path | None` annotation below: the Cores run Python 3.9, where PEP 604  # privacy-ok: PEP 604 is a Python Enhancement Proposal, not a course
# unions raise TypeError at def time, not at type-check time. Without this the module fails to
# import at all — and this lint gates the save on every Core, so that is a fleet-wide hard stop,
# not a style issue. Caught by running it; the same pattern is already used in
# scheduling/claude-si/friction_installer.py.
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)

# Engine-repo fallback root. Many live docs in the instance cite paths under
# `scheduling/brain-pg/...` / `scheduling/graphify-brain/...` — those files
# live in `$CORE_ENGINE`, not the instance. Treat the cite as resolved if the
# path exists under either root.
_ENGINE_ENV = os.environ.get("CORE_ENGINE", "").strip()
ENGINE_ROOT = Path(_ENGINE_ENV).expanduser() if _ENGINE_ENV else None
if ENGINE_ROOT is not None and not ENGINE_ROOT.exists():
    ENGINE_ROOT = None

# Engine template detection — the engine/baseline repo's docs legitimately cite
# instance-only paths (`memory/current-state.md`, `.claude/identity.json`,
# `memory/relationships/<name>.md`) that don't exist UNTIL an instance is spawned
# from the template. Live-doc enforcement is an INSTANCE concern; on the template
# we exit clean. Two independent signals (either suffices):
#   1. `bin/init-instance.sh` present — exists only in the engine, not instances.
#   2. CORE_INSTANCE == CORE_ENGINE — the baseline CI (ci.yml) points both env
#      vars at the same checkout; a real instance always has distinct roots. This
#      backstops the case where the baseline lacks init-instance.sh (the marker
#      that previously left baseline CI red — the template was linted as if it
#      were an instance).
_CI_INSTANCE = os.environ.get("CORE_INSTANCE", "").strip()
_CI_ENGINE = os.environ.get("CORE_ENGINE", "").strip()
IS_ENGINE_TEMPLATE = (
    (REPO / "bin" / "init-instance.sh").exists()
    or bool(_CI_INSTANCE) and _CI_INSTANCE == _CI_ENGINE
)

PREFIXES = (
    r"\.claude/",
    r"memory/",
    r"tasks/",
    r"bin/",
    r"sessions/",
    r"scheduling/",
    r"agents/",
    r"hooks/",
    r"commands/",
)

# Match path-shaped strings; allow letters, digits, dot, slash, dash, underscore.
# Anchored on a non-word-non-slash boundary to avoid matching mid-token.
PATH_RX = re.compile(
    r"(?<![\w/.\-])(?:" + "|".join(PREFIXES) + r")[\w./\-]+",
)

# Skip these as templates, examples, or non-paths.
PLACEHOLDER_RX = re.compile(
    # X{2,} (2026-07-26, was XXXX): backlog items cite planned research docs as
    # `…-2026-05-XX.md` — a deliberate to-be-written date placeholder, not drift.
    r"(YYYY[-_](?:MM[-_]DD|QN|WW|MM)|X{2,}|<[\w\-]+>|\{\{|/\*|---|\.\.\.|<NAME>|<topic>|<slug>|N{2,})"
)

# Trailing partial patterns that signal a placeholder, e.g.:
#   memory/automations/gmail-     ← "gmail-<localpart>"
#   tasks/handoff-                ← "handoff-<slug>-<date>"
#   .claude/state/.rot-score-     ← ".rot-score-<sessionid>"
#   sessions/2026-05-             ← "2026-05-<DD>"
TRAILING_PARTIAL_RX = re.compile(r"[-/]$|[-/]\d{0,3}$")

# Planned-path exception: a citation IMMEDIATELY followed by a planned/future
# marker — e.g. `bin/etsy-bot.py` (planned) — is a deliberate forward-reference
# in a plan/hub doc, NOT drift. Same rationale as the `.claude/state/*` exemption
# in scan_file: the citation is architecturally correct even though the file
# isn't on disk yet. Keyed tightly to a marker that immediately follows the path
# (allowing intervening markdown ``*_`` and whitespace) so it can't blanket-
# suppress every path on a line that merely mentions "planned" somewhere.
# 2026-07-29 — added instance-only / per-core / local-only. SHARED code legitimately cites
# paths that live only inside a running Core: `memory/**`, `tasks/**`, `.claude/rules-life/**`
# and the archive dirs are all per_core_keep, so they are absent from the baseline BY DESIGN and
# absent from any fresh clone until a Core is populated. Before this, every such citation was
# either a permanent false positive on a cold clone, or had to be deleted from the doc — which
# loses genuinely useful information ("the queue lives at memory/reading-queue.json"). The
# marker lets the doc keep the reference and state why it is not on disk yet.
# 2026-07-30 — added baseline-only, the exact MIRROR of instance-only above. A shared doc can
# also cite a path that exists on the BASELINE and legitimately does not exist on an instance:
# `.claude/agents/sentinel/CLAUDE.md` is the case that forced this (privacy.md documents the
# dir-form specs' baseline status precisely so nobody cites them as live, and that documentation
# was itself linted as drift). Verified against the baseline repo before use — the marker asserts a
# fact about another repo, so it must be checked there, never inferred from the working tree.
PLANNED_MARKER_RX = re.compile(
    r"^[\s`*_]*\((?:planned|to[ -]build|not[ -]yet[ -](?:built|created)|future|tbd|wip|coming|removed|retired|deleted|archived"
    r"|instance[ -]only|per[ -]core|local[ -]only|baseline[ -]only)[^)]*\)",
    re.IGNORECASE,
)

# Live-doc allowlist — paths that should always have correct citations.
# Anything else is treated as snapshot-immutable.
LIVE_DOC_RX = re.compile(
    r"^("
    r"CLAUDE\.md|CLAUDE\.local\.md|README\.md|"
    r"\.claude/rules/[\w\-]+\.md|"
    # 2026-07-29: the FLAT native-format spec was missing from this allowlist, so the agent
    # definitions that ACTUALLY LOAD were never linted — only the pre-migration dir-form
    # below, which stopped loading in May 2026 and is now being deleted. The migration moved
    # the specs and did not move the lint's view of them, so every citation in a live
    # reviewer charter went unchecked for two months. Both forms are listed until the
    # dir-form is gone from every Core.
    r"\.claude/agents/[\w\-]+\.md|"
    r"\.claude/agents/[\w\-]+/CLAUDE\.md|"
    r"\.claude/commands/[\w\-]+\.md|"
    r"\.claude/skills/[\w\-]+/SKILL\.md|"
    r"memory/projects/[\w\-]+\.md|"
    r"memory/projects/[\w\-]+/[\w\-]+\.md|"
    r"memory/relationships/[\w\-]+\.md|"
    r"memory/about-me\.md|memory/preferences\.md|"
    r"memory/goals\.md|memory/skills-interests\.md|"
    r"memory/capabilities\.md|memory/current-state\.md|"
    r"memory/education/courses/[\w\-]+/overview\.md|"
    r"bin/[\w\-]+\.md|"
    # tasks/ (2026-07-26, per the operator's call to hold tasks docs to the standard): STANDING docs only.
    # A date-stamped filename (…-2026-07-24.md) is a timeboxed snapshot — an audit, research
    # artifact, or spec that records what was true WHEN WRITTEN; rewriting its citations
    # falsifies the record (same principle as the sessions/ exclusion, and the same line the
    # memory rules draw for freshness). Undated tasks docs (backlog.md, lessons.md,
    # system-rundown.md, …) are living state and must cite reality.
    # lessons-archive.md is append-only history (retired lessons keep their at-the-time
    # citations) — same class as decisions-log.md / access-log.md, which were always excluded.
    r"tasks/(?!lessons-archive\.md$)(?!.*\d{4}-\d{2}-\d{2})[\w\-\.]+\.md|"
    r"tasks/research/(?!.*\d{4}-\d{2}-\d{2})[\w\-\.]+\.md|"
    r"tasks/specs/(?!.*\d{4}-\d{2}-\d{2})[\w\-\.]+\.md"
    r")$"
)

# Directories to skip entirely.
EXCLUDE_DIR_PARTS = {".git", "node_modules", "archive", "_archive", "venv", ".venv"}


def is_placeholder(s: str) -> bool:
    if PLACEHOLDER_RX.search(s):
        return True
    if TRAILING_PARTIAL_RX.search(s):
        return True
    return False


def looks_like_real_path(s: str) -> bool:
    """Filter out English prose that happens to start with `hooks/` or `memory/`.
    A real path has at least one specificity marker:
      - file extension (`.md`, `.sh`, `.py`, ...)
      - trailing slash (explicit directory reference)
      - a digit (date, version, line number)
      - a dash (component-style filename like `untrusted-fetch`)
    Pure all-lowercase-word/word/word/word strings are prose, not paths.
    """
    if "/" not in s:
        return False
    if s.endswith("/"):
        return True
    if re.search(r"\.[A-Za-z0-9]+$", s):
        return True
    if re.search(r"\d", s):
        return True
    if "-" in s.split("/", 1)[1]:  # dash in any segment after the prefix
        return True
    return False


def trim_trailing(s: str) -> str:
    """Strip trailing punctuation that's not part of the path."""
    return re.sub(r"[.,;:!?)\]\}'\"`]+$", "", s)


def strip_lineref(s: str) -> str:
    """Strip ':NN' line-number suffix if present (e.g., file.md:42)."""
    return re.sub(r":\d+$", "", s)


# Markdown link targets: `](target)`. Checked SEPARATELY from PATH_RX because PATH_RX only
# matches strings starting with a known directory PREFIX, so a ROOT-LEVEL filename is
# structurally invisible to it — no prefix to anchor on.
#
# 2026-07-29: that blind spot shipped five dead links in README.md, the file a new user or a
# fork owner reads first, including `INSTALL.md` — which README calls "the documented four-step
# install" and which has never existed in any Core. The lint reported README.md as
# "Broken in-repo path references: 0 — CLEAN" the whole time, while README itself advertises
# this lint as one that "refuses to commit markdown that cites a path that doesn't exist on
# disk." The guard was not mis-tuned; the shape was unexpressable in its pattern.
#
# Only local relative targets are considered: http(s)/mailto/protocol-relative and pure `#`
# anchors are skipped, and a leading `./` is normalised away. An anchor suffix (`file.md#sec`)
# is trimmed to the file part — the file must exist; the anchor is not resolved.
MDLINK_RX = re.compile(r"\]\(\s*(?!https?:|mailto:|//|#)([^)\s]+?)\s*\)")


def _citation_broken(cited: str, rest_of_line: str, citing_dir: Path | None = None,
                     full_line: str = "") -> bool:
    """Shared resolver for BOTH matchers. One copy on purpose.

    Two copies of a resolution rule is how the `^### `-vs-`^#{2,3} ` heading bug survived —
    one copy got fixed, the other silently kept matching nothing. If a new exemption is
    needed, it goes here and both matchers inherit it.

    citing_dir is the directory of the file the citation was found IN, and omitting it was a
    defect this function had from the start: a markdown link is relative to its own file, but
    candidates were only ever resolved against REPO and ENGINE_ROOT. So a correct sibling link
    was unresolvable BY CONSTRUCTION — not mis-tuned, structurally impossible to satisfy —
    and the failures it produced blocked the save gate on every Core carrying such a link.
    Reported by core-business 2026-08-04 with four instances, all genuinely correct on disk:
    memory/relationships/{acme-overview,jordan-lee}.md citing each other as siblings, and
    two child links under memory/projects/.

    Repo-relative resolution is kept and tried FIRST, because much of this corpus cites that
    way ("memory/decisions-log.md" from anywhere). The file-relative base is added, not
    substituted — a citation is correct if it resolves under EITHER convention, and treating
    one as the only truth is what created the bug.
    """
    if not cited or is_placeholder(cited):
        return False
    if "*" in cited:                      # globs aren't checkable
        return False
    if PLANNED_MARKER_RX.match(rest_of_line):
        return False
    candidates = [cited]
    stripped_ref = strip_lineref(cited)
    if stripped_ref != cited:
        candidates.append(stripped_ref)
    # Runtime-files allowlist: .claude/state/* is gitignored and hook-written at runtime,
    # so the reference is architecturally correct on a first-run Core.
    if any(c.startswith(".claude/state/") for c in candidates):
        return False
    # Absolute paths are off-scope for an in-repo lint.
    if any(c.startswith(("~/", "/")) for c in candidates):
        return False
    # A CITATION THAT NAMES ANOTHER CORE IS NOT AN IN-REPO PATH. The corpus writes cross-Core
    # references as prose plus a path — core-business `memory/projects/career.md` — and resolving
    # that against THIS tree reports a broken link for a file that exists, on the Core named right
    # next to it. Verified: core-business/memory/projects/career.md is present.
    #
    # Found only because casebook S2 was fixed today and could report a failure for the first time.
    # The lint had been emitting this correctly all along; nothing could read its answer.
    # THE CORE NAME PRECEDES THE PATH, so rest_of_line (text AFTER the citation) never contains
    # it — my first cut checked the wrong half of the line and silently changed nothing. The
    # producer's count stayed at 1 and I would have called it fixed. Needs the WHOLE line.
    if re.search(r"\bcore-(?:life|business|school|finance|ops)\b", full_line or rest_of_line or ""):
        return False
    # Resolution bases, each paired with the tree the result must stay inside. A citation is
    # correct if it resolves under ANY of them — repo-relative and file-relative are both real
    # conventions in this corpus, and treating either as the only truth is what caused the bug.
    #
    # Containment applies to ALL bases, not just the new one. Adding the file-relative base with
    # a relative_to() guard while the repo-relative check had none would have left two different
    # standards in one function, and the older one is genuinely looser: `REPO / "../../../../
    # etc/passwd"` resolves through `..` to a real /etc/passwd, so .exists() returned True and
    # the citation was reported as FINE. Low severity in a doc lint — a false negative, not a
    # read — but it is the same escape the new code already refused, so both get the same rule.
    bases = [(REPO, REPO)]
    if ENGINE_ROOT is not None:
        bases.append((ENGINE_ROOT, ENGINE_ROOT))
    if citing_dir is not None:
        bases.append((citing_dir, REPO))
    for base, root in bases:
        for c in candidates:
            try:
                resolved = (base / c).resolve()
                resolved.relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            if resolved.exists():
                return False
    return True


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (line_no, broken_path) tuples."""
    try:
        text = path.read_text()
    except Exception:
        return []

    broken = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        # Track fenced code blocks. We still scan them — paths in code blocks
        # are often the real ones — but we skip lines that look like shell
        # examples with placeholders.
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue

        # Matcher 1 — prefix-anchored path-shaped strings anywhere in the line.
        for m in PATH_RX.finditer(line):
            cited = trim_trailing(m.group(0))
            # looks_like_real_path guards against English prose that happens to open with
            # `hooks/` or `memory/`. It applies ONLY here: a markdown link target is already
            # unambiguously a path by syntax, so requiring a specificity marker there would
            # silently drop bare targets like `LICENSE`.
            if not cited or not looks_like_real_path(cited):
                continue
            if _citation_broken(cited, line[m.end():], path.parent, line):
                broken.append((line_no, cited))

        # Matcher 2 — markdown link targets, prefix or not. This is what catches a
        # ROOT-LEVEL file (`INSTALL.md`, `CONTRIBUTING.md`) that matcher 1 cannot see.
        for m in MDLINK_RX.finditer(line):
            target = m.group(1).strip()
            target = target.split("#", 1)[0]          # `file.md#section` -> `file.md`
            if target.startswith("./"):
                target = target[2:]
            if not target:                            # pure `#anchor` link
                continue
            if _citation_broken(target, line[m.end():], path.parent, line):
                broken.append((line_no, target))
    return broken


# ── THE archival predicate. ONE copy, called by detect.sh (--split) AND by the core-si applier. ──
#
# Two copies of a resolution rule is how the last doc-path bug survived; this file's own header
# says so. The core-si applier must never re-derive "is this ref archival-fixable" — it shells out
# to --split and reads this.
ARCHIVE_GLOBS = ("scheduling/_archive/**/*", "tasks/archive/**/*",
                 ".claude/hooks/archive/**/*", "memory/archive/**/*")

# REVIEWER CHARTERS ARE REPORT-ONLY, NEVER REWRITTEN. .claude/agents/sentinel*.md are the live
# contracts the outward gate is reviewed against. A basename-collision rewrite inside one of those
# re-aims the security gate's cited contract at an ARCHIVED file — the gate would then be reviewed
# against a retired document while still reporting clean. The --fix-archival path has run at every
# full close since 2026-07-26 with no such guard.
REPORT_ONLY_PREFIX = ".claude/agents/"


def archive_index():
    idx = {}
    for pat in ARCHIVE_GLOBS:
        for q in REPO.glob(pat):
            if q.is_file():
                idx.setdefault(q.name, []).append(q.relative_to(REPO).as_posix())
    return idx


def archival_target(cited, idx):
    """The archive path for `cited`, or None when 0 or >1 candidates exist.

    >1 is NOT a judgment call deferred to a human — it is MISSING INFORMATION. Nothing in the repo
    determines which of several same-named archived files a citation meant, so there is no correct
    value to write and guessing would manufacture a confident wrong ref.
    """
    # `Path`, not `pathlib.Path` — this module does `from pathlib import Path`, so the bare
    # module name was never bound and this line raised NameError every time archival_target
    # was reached. Found by test_no_undefined_names on its first run.
    c = idx.get(Path(cited).name, [])
    return c[0] if len(c) == 1 else None


def split_counts(results):
    """(archival_fixable, other). The admission unit is the KEY, so these two populations must be
    two keys: an applier that fixes the first and returns True would flip the SECOND — the
    judgment half auto-safe.txt's HARD FLOOR reserves — to a status reading `auto-applied`."""
    idx = archive_index()
    fixable = other = 0
    for rel, items in results.items():
        report_only = rel.startswith(REPORT_ONLY_PREFIX)
        for _ln, cited in items:
            if not report_only and archival_target(cited, idx):
                fixable += 1
            else:
                other += 1
    return fixable, other


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", action="store_true", help="print broken count only")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON")
    ap.add_argument("--paths", nargs="*", help="limit scan to these paths")
    ap.add_argument("--split", action="store_true",
                    help="print {archival_fixable, other} — the two-key population split")
    ap.add_argument("--fix-archival", action="store_true",
                    help="rewrite a broken ref ONLY when its exact basename exists in ONE "
                         "archive location (retire-legacy moved it); never guesses")
    args = ap.parse_args()

    # Engine template: docs cite future-instance paths. Exit clean.
    if IS_ENGINE_TEMPLATE:
        if args.count:
            print(0)
        elif args.json:
            print(json.dumps({"engine_template": True, "scanned": 0, "broken_total": 0}, indent=2))
        else:
            print(f"Engine template detected at {REPO} — live-doc lint is an instance concern, skipping.")
            print("CLEAN — engine template citations refer to future-instance paths (expected).")
        sys.exit(0)

    def is_live_doc(p: Path) -> bool:
        try:
            rel = str(p.resolve().relative_to(REPO))
        except ValueError:
            return False
        if any(part in EXCLUDE_DIR_PARTS for part in p.parts):
            return False
        return bool(LIVE_DOC_RX.match(rel))

    if args.paths:
        # Resolve FIRST (2026-07-26 fix). A relative --paths argument used to survive intake
        # (is_live_doc resolves internally, so the filter passed) and then crash at
        # `path.relative_to(REPO)` below — relative-vs-absolute ValueError. Every caller had
        # been working around it by passing absolute paths. Relative, absolute, and ./-prefixed
        # now all behave identically.
        md_files = [
            Path(p).resolve() for p in args.paths
            if p.endswith(".md") and Path(p).is_file() and is_live_doc(Path(p))
        ]
    else:
        md_files = [p for p in REPO.rglob("*.md") if is_live_doc(p)]

    results = {}
    for path in md_files:
        broken = scan_file(path)
        if broken:
            results[str(path.relative_to(REPO))] = broken

    # ── --fix-archival (2026-07-26, at the operator's request to make this automatic too) ─────────────────
    # A broken ref whose EXACT basename exists in exactly ONE archive location is a file
    # retire-legacy (or a manual archive) moved; rewrite the citation to the new home.
    # Ambiguous (0 or >1 archive matches) refs are left alone and still reported — this
    # repairs only the provable case, never guesses. Runs at full close via session-lifecycle.
    if args.fix_archival and results:
        archived = archive_index()
        fixed = 0
        still = {}
        for rel, items in results.items():
            doc = REPO / rel
            text = doc.read_text()
            original = text
            report_only = rel.startswith(REPORT_ONLY_PREFIX)
            remaining = []
            for line_no, cited in items:
                target = None if report_only else archival_target(cited, archived)
                if target and cited in text:
                    # WORD BOUNDARY, not str.replace(). A plain global replace rewrote inside
                    # LONGER citations: `tasks/foo.md` turned `tasks/foo.md.bak` into
                    # `tasks/archive/foo.md.bak`, a path that never existed — manufacturing a fresh
                    # broken ref that this very detector then nags about forever. This path has run
                    # at every full close since 2026-07-26.
                    text = re.sub(rf"{re.escape(cited)}(?![\w./\-])", lambda _m: target, text)
                    fixed += 1
                    print(f"  fixed: {rel} :: {cited} -> {target}")
                else:
                    remaining.append((line_no, cited))
            if remaining:
                still[rel] = remaining
            # Was unconditional: rewrote every scanned doc even when nothing changed, bumping mtimes
            # and dirtying the tree for no reason (and defensive-save then committed the noise).
            if text != original:
                doc.write_text(text)
        results = still
        print(f"fix-archival: {fixed} ref(s) rewritten to archive locations")

    if args.split:
        f, o = split_counts(results)
        print(json.dumps({"archival_fixable": f, "other": o}))
        return 0

    total = sum(len(v) for v in results.values())

    if args.count:
        print(total)
        # --count is read by the SessionStart hook; exit 0 so command-substitution
        # captures just the number without the `|| fallback` firing.
        sys.exit(0)

    if args.json:
        print(json.dumps(
            {"scanned": len(md_files), "broken_total": total, "files": results},
            indent=2,
        ))
        sys.exit(1 if total else 0)

    # Human report
    print(f"Scanned {len(md_files)} .md files under {REPO}")
    print(f"Broken in-repo path references: {total}")
    if not total:
        print("CLEAN — every cited path resolves.")
        sys.exit(0)
    print()
    for rel, items in sorted(results.items()):
        print(f"  {rel}:")
        for line_no, cited in items:
            print(f"    L{line_no}  -->  {cited}")
    print()
    print("To fix: open each file:line and either update the path to match reality")
    print("or move the file to match the doc. Drift unchecked = drift compounds.")
    sys.exit(1)


if __name__ == "__main__":
    main()
