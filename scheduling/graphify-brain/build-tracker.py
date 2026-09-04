#!/usr/bin/env python3
"""
Phase 11 tracker generator.

Walks the Core Brain vault, cross-references against extracted chunk-body-*.json
files in checkpoints/, and emits phase-11-tracker.md with [x]/[ ] markers per file.

Idempotent — safe to call from the Stop hook on every session close.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parent
_ENGINE = REPO.parent.parent  # Engine repo (where this script lives — graphify-brain pipeline code is engine-side)
import os
import sys
_INSTANCE_ENV = os.environ.get("CORE_INSTANCE")
_BRAIN_ENV = os.environ.get("CORE_BRAIN")
if not _INSTANCE_ENV or not _BRAIN_ENV:
    print(f"{Path(__file__).name}: $CORE_INSTANCE and $CORE_BRAIN both required.", file=sys.stderr)
    sys.exit(1)
_INSTANCE = Path(_INSTANCE_ENV)
BRAIN = Path(_BRAIN_ENV)
# Per spec-graphify-out-relocation-2026-05-16.md — checkpoints live in brain repo.
CHECKPOINTS = BRAIN / "_build" / "output" / "checkpoints"
CHECKPOINTS.mkdir(parents=True, exist_ok=True)
# Tracker output: instance-side personal data. Engine ships clean; tracker
# lives in $CORE_INSTANCE/tasks/graphify-brain/ so the engine repo stays
# free of generated personal artifacts.
# WIKI_GLOBS below ALSO walks instance content (memory/, sessions/, tasks/) —
# the prior `CORE_REPO = REPO.parent.parent` name confusingly pointed at engine
# but was used as if it pointed at instance. Renamed _ENGINE for engine-side
# refs; introduced _INSTANCE for the wiki source. Per spec-cascade-fix-2026-05-16.md Phase 1.
TRACKER = _INSTANCE / "tasks" / "graphify-brain" / "phase-11-tracker.md"
TRACKER.parent.mkdir(parents=True, exist_ok=True)

# Brain vault: conversation-body files. Path keys are relative to BRAIN root.
# Walks every projects/<slug>/{sessions,subagents} dynamically so newly-routed
# project dirs (e.g. core-school after the export.py:74 routing fix 2026-05-12)
# are picked up automatically without an explicit list edit.
BRAIN_DIRS = sorted({
    str(p.relative_to(BRAIN))
    for p in (BRAIN / "projects").glob("*/sessions")
    if p.is_dir()
} | {
    str(p.relative_to(BRAIN))
    for p in (BRAIN / "projects").glob("*/subagents")
    if p.is_dir()
})

# Instance wiki content. Each entry is (label, glob_pattern_relative_to_INSTANCE).
# Globs use ** for recursion. All matches must be .md files.
WIKI_GLOBS: list[tuple[str, str]] = [
    ("sessions", "sessions/*.md"),
    ("memory (top-level)", "memory/*.md"),
    ("memory/projects", "memory/projects/*.md"),
    ("memory/relationships", "memory/relationships/**/*.md"),
    ("memory/education/courses", "memory/education/courses/**/*.md"),
    ("memory/security", "memory/security/**/*.md"),
    ("memory/brain-lint-reports", "memory/brain-lint-reports/*.md"),
    ("memory/weekly-reviews", "memory/weekly-reviews/*.md"),
    ("memory/automations", "memory/automations/*.md"),
    ("tasks", "tasks/*.md"),
    (".claude/agents", ".claude/agents/**/CLAUDE.md"),
    (".claude/skills", ".claude/skills/**/SKILL.md"),
    ("scheduling READMEs", "scheduling/*/README.md"),
]

# Wiki files explicitly excluded from extraction (credentials / ephemeral state).
# cost-log.md removed 2026-05-12 (file deleted + gitignored); entry kept until
# 2026-05-13 cleanup pass dropped it. (Audit brain-vault punch list #12.)
WIKI_EXCLUDE = {
    "memory/secrets.md",
    "memory/pending.md",  # mirrors core_paths.MEM_PENDING (rel path, not Path object)
}


def load_processed() -> set[str]:
    """Return the set of source_file values from completed chunk-body-*.json files."""
    processed: set[str] = set()
    for p in CHECKPOINTS.glob("chunk-body-*.json"):
        try:
            data = json.loads(p.read_text())
            sf = data.get("metadata", {}).get("source_file")
            if sf:
                processed.add(sf)
        except Exception:
            pass
    return processed


def list_brain_files() -> dict[str, list[str]]:
    """Return {relative_dir: [paths_relative_to_brain]} for each brain dir."""
    out: dict[str, list[str]] = {}
    for d in BRAIN_DIRS:
        full = BRAIN / d
        if full.is_dir():
            out[d] = sorted(f"{d}/{p.name}" for p in full.glob("*.md"))
        else:
            out[d] = []
    return out


def list_wiki_files() -> dict[str, list[str]]:
    """Return {label: [paths_relative_to_INSTANCE]} for each wiki glob."""
    out: dict[str, list[str]] = {}
    for label, pattern in WIKI_GLOBS:
        matches = sorted(
            str(p.relative_to(_INSTANCE))
            for p in _INSTANCE.glob(pattern)
            if p.is_file() and str(p.relative_to(_INSTANCE)) not in WIKI_EXCLUDE
        )
        out[label] = matches
    return out


def render_section(
    title: str, universe: dict[str, list[str]], processed: set[str]
) -> tuple[list[str], int, int]:
    """Render one section (brain or wiki). Returns (lines, done, total)."""
    total = sum(len(files) for files in universe.values())
    done_count = sum(1 for files in universe.values() for f in files if f in processed)
    pct = (done_count / total * 100) if total else 0.0

    lines: list[str] = []
    lines.append(f"## {title} — {done_count}/{total} ({pct:.1f}%)")
    lines.append("")
    lines.append("| Source | Done | Total | % |")
    lines.append("|---|---|---|---|")
    for label, files in universe.items():
        n = len(files)
        if n == 0:
            continue
        d_done = sum(1 for f in files if f in processed)
        d_pct = (d_done / n * 100) if n else 0.0
        lines.append(f"| `{label}` | {d_done} | {n} | {d_pct:.0f}% |")
    lines.append("")
    for label, files in universe.items():
        if not files:
            continue
        d_done = sum(1 for f in files if f in processed)
        lines.append(f"### `{label}` — {d_done}/{len(files)}")
        lines.append("")
        for f in files:
            mark = "x" if f in processed else " "
            lines.append(f"- [{mark}] `{f}`")
        lines.append("")
    return lines, done_count, total


def render(
    processed: set[str],
    brain: dict[str, list[str]],
    wiki: dict[str, list[str]],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    brain_lines, brain_done, brain_total = render_section(
        "Brain vault (historical, auto-grown)", brain, processed
    )
    wiki_lines, wiki_done, wiki_total = render_section(
        "Core repo (canonical, curated)", wiki, processed
    )
    grand_total = brain_total + wiki_total
    grand_done = brain_done + wiki_done
    grand_pct = (grand_done / grand_total * 100) if grand_total else 0.0

    out: list[str] = []
    out.append("# Phase 11 — Brain extraction tracker")
    out.append("")
    out.append(f"_Last generated: {now} (auto-generated by `build-tracker.py`)_")
    out.append("")
    out.append(f"**Overall:** {grand_done} / {grand_total} files extracted ({grand_pct:.1f}%)")
    out.append("")
    out.append(f"- Brain vault (historical): {brain_done} / {brain_total}")
    out.append(f"- Core repo (canonical): {wiki_done} / {wiki_total}")
    out.append("")
    out.append("---")
    out.append("")
    out.extend(brain_lines)
    out.append("---")
    out.append("")
    out.extend(wiki_lines)
    return "\n".join(out) + "\n"


def main() -> None:
    processed = load_processed()
    brain = list_brain_files()
    wiki = list_wiki_files()
    TRACKER.write_text(render(processed, brain, wiki))
    brain_total = sum(len(f) for f in brain.values())
    wiki_total = sum(len(f) for f in wiki.values())
    brain_done = sum(1 for files in brain.values() for f in files if f in processed)
    wiki_done = sum(1 for files in wiki.values() for f in files if f in processed)
    total = brain_total + wiki_total
    done = brain_done + wiki_done
    print(f"Tracker written: {TRACKER}")
    print(f"  Overall: {done} / {total} ({done/total*100:.1f}%)")
    print(f"  Brain:   {brain_done} / {brain_total}")
    print(f"  Wiki:    {wiki_done} / {wiki_total}")


if __name__ == "__main__":
    main()
