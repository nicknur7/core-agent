#!/usr/bin/env python3
"""
consolidate.py — Phase 3 Step 3
Reads all agent JSON reports, normalizes topics/tools/entities,
generates hub pages and TOP-TOPICS.md leaderboard.
Does NOT modify any session/subagent files.
"""

import json
import glob
import os
import re
import sys
from collections import defaultdict, Counter
from datetime import date, timedelta
from pathlib import Path

# Twin of $CORE_INSTANCE/scheduling/graphify-brain/humanize.py — see that
# file's docstring for why this is a duplicate rather than a cross-repo
# import. Load from this same directory (_build/), no path assumptions
# beyond "next to this script".
sys.path.insert(0, str(Path(__file__).resolve().parent))
from humanize import humanize_slug

# Fail loud if $CORE_BRAIN unset. Hardcoded fallback masked the 2026-05-14→15
# read/write divergence for ~24h. Per spec-cascade-fix-2026-05-16.md Phase 1.
_brain_env = os.environ.get("CORE_BRAIN")
if not _brain_env:
    raise RuntimeError(
        "consolidate.py: $CORE_BRAIN env var is required. "
        "Set it before invoking (e.g., export CORE_BRAIN=~/AI\\ Projects/core-brain)."
    )
VAULT = Path(_brain_env)
REPORTS_DIR = VAULT / "_build" / "reports"

# ── TOP-TOPICS ranking weights ──────────────────────────────────────────────
# weight = RECENCY_ALPHA * count_in_window + (1 - RECENCY_ALPHA) * total_count
# Tune to bias toward recent activity. 1.0 = pure recency; 0.0 = pure lifetime.
RECENCY_ALPHA = 0.6
RECENCY_WINDOW_DAYS = 30

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def extract_date(fpath: str):
    """Pull YYYY-MM-DD from anywhere in fpath. Returns datetime.date or None."""
    if not fpath:
        return None
    m = _DATE_RE.search(fpath)
    if not m:
        return None
    try:
        y, mo, d = m.group(1).split("-")
        return date(int(y), int(mo), int(d))
    except (ValueError, TypeError):
        return None

# ── Normalization maps ──────────────────────────────────────────────────────

TOOL_ALIASES = {
    "next-js": "nextjs",
    "next.js": "nextjs",
    "python": "python3",
    "osascript": "applescript",
    "node.js": "node",
    "nodejs": "node",
    "node-js": "node",
    "bash-script": "bash",
    "shell": "bash",
    "typescript": "typescript",  # keep as-is (already lowercase)
}

ENTITY_ALIASES = {
    # PER-VAULT ENTITY ALIASES — people and private orgs REMOVED for the template.
    #
    # This maps nicknames to canonical names so the graph does not split one entity across
    # several nodes. That makes it, in the author's own vault, a list of real people and
    # private organisations — and it shipped here verbatim in a repo that is explicitly a
    # publishable template with an external fork. Same leak class as commit 5ab58a6.
    #
    # The generic technology names below are kept because they are useful to everyone and
    # identify nobody. Add your own people and orgs locally; they are your vault's data.
    #   "nickname": "Canonical Name",
    "anthropic": "Anthropic",
    "claude code": "Claude Code",
    "github": "GitHub",
    "github actions": "GitHub Actions",
    "gmail": "Gmail",
    "linkedin": "LinkedIn",
    "obsidian": "Obsidian",
    "typescript": "TypeScript",
}

# Topics to merge (secondary → canonical)
TOPIC_ALIASES = {
    # Per-vault: merge secondary topic slugs into a canonical one.
    # Emptied for the template — the author's entries were their own project names.
    #   "secondary-slug": "canonical-slug",
}


def normalize_tool(t: str) -> str:
    t = t.strip().lower()
    return TOOL_ALIASES.get(t, t)


def normalize_entity(e: str) -> str:
    key = e.strip().lower()
    return ENTITY_ALIASES.get(key, e.strip())


def normalize_topic(t: str) -> str:
    t = t.strip().lower().replace(" ", "-")
    return TOPIC_ALIASES.get(t, t)


def slug(name: str) -> str:
    """Convert any name to a safe filename slug."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ── Load all reports ────────────────────────────────────────────────────────

def load_reports():
    reports = sorted(REPORTS_DIR.glob("*.json"))
    all_results = {}  # keyed by file path to deduplicate agent_9 vs agent_9_retry

    for rp in reports:
        data = json.loads(rp.read_text())
        if isinstance(data, list):
            results = data
        elif isinstance(data, dict):
            results = data.get("results", [])
        else:
            results = []

        for r in results:
            fpath = r.get("file", "")
            # retry takes precedence over original agent_9 for same file
            if fpath not in all_results or "retry" in rp.name:
                all_results[fpath] = r

    return list(all_results.values())


# ── Build inverted indexes ──────────────────────────────────────────────────

def build_indexes(results):
    # Each index maps normalized-name → list of (file_path, summary, session_id)
    topics_idx = defaultdict(list)
    tools_idx = defaultdict(list)
    entities_idx = defaultdict(list)

    for r in results:
        # Reports split between "file" and "path" depending on which agent version
        # produced them — accept either. Without a path, recency falls back to total.
        fpath = r.get("file") or r.get("path", "")
        summary = r.get("summary", "")
        session_id = r.get("session_id", "")
        entry_date = extract_date(fpath)

        for t in r.get("topics", []):
            norm = normalize_topic(t)
            entry = {"file": fpath, "summary": summary, "session_id": session_id, "date": entry_date}
            # avoid dupes within same topic
            if not any(e["file"] == fpath for e in topics_idx[norm]):
                topics_idx[norm].append(entry)

        for t in r.get("tools", []):
            norm = normalize_tool(t)
            entry = {"file": fpath, "summary": summary, "session_id": session_id, "date": entry_date}
            if not any(e["file"] == fpath for e in tools_idx[norm]):
                tools_idx[norm].append(entry)

        for e in r.get("entities", []):
            norm = normalize_entity(e)
            entry = {"file": fpath, "summary": summary, "session_id": session_id, "date": entry_date}
            if not any(e2["file"] == fpath for e2 in entities_idx[norm]):
                entities_idx[norm].append(entry)

    return topics_idx, tools_idx, entities_idx


# ── File path → wikilink ────────────────────────────────────────────────────

def _rewrite_stale_brain_path(fpath: str) -> str:
    """Map historical vault paths to current vault paths so to_wikilink() can resolve them.
    Brain moved 2026-05-01 (~/core brain/ → ~/AI Projects/core brain/) and renamed 2026-05-14
    (~/AI Projects/core brain/ → ~/AI Projects/core-brain/). Legacy 'you'
    project slug renamed to 'home'. Old report JSONs still carry stale paths, but the
    underlying files exist at the new locations. Rewrite here so hub pages link cleanly.
    """
    rewrites = [
        # noqa: hygiene-lint — legacy-path mapping data (rewrite rules), not hardcoded behavior.
        ("/Users/you/AI Projects/core brain/", "/Users/you/AI Projects/core-brain/"),  # noqa: hygiene-lint
        ("/Users/you/core brain/", "/Users/you/AI Projects/core-brain/"),  # noqa: hygiene-lint
        ("/projects/you/sessions/", "/projects/home/sessions/"),
        ("/projects/you/subagents/", "/projects/home/subagents/"),
        # Relative-form variants (no leading slash) that appear in some report JSONs.
        ("projects/you/sessions/", "projects/home/sessions/"),
        ("projects/you/subagents/", "projects/home/subagents/"),
    ]
    for old, new in rewrites:
        if old in fpath:
            fpath = fpath.replace(old, new)
    return fpath


def to_wikilink(fpath: str, summary: str) -> str:
    """Convert absolute file path to a vault-relative Obsidian wikilink."""
    fpath = _rewrite_stale_brain_path(fpath)
    try:
        rel = Path(fpath).relative_to(VAULT)
        # strip .md for wikilink
        rel_no_ext = str(rel).removesuffix(".md")
        display = Path(fpath).stem  # filename without ext
        if summary:
            return f"- [[{rel_no_ext}|{display}]] — {summary}"
        return f"- [[{rel_no_ext}|{display}]]"
    except ValueError:
        return f"- {fpath} — {summary}"


# ── Write hub pages ─────────────────────────────────────────────────────────

def is_manually_edited(fpath: Path) -> bool:
    """Check if an existing page has `manually_edited: true` in its YAML frontmatter.
    Returns False if file doesn't exist, has no frontmatter, or flag is absent/false.

    The flag is the preservation contract: pages with this flag are skipped on regen
    so manual lint corrections don't get clobbered. Remove the flag (or set to false)
    to allow consolidate.py to regenerate the page from extraction reports.
    """
    if not fpath.exists():
        return False
    try:
        text = fpath.read_text()
    except Exception:
        return False
    if not text.startswith("---"):
        return False
    # Find the closing --- of the frontmatter
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False
    fm = parts[1]
    # Match `manually_edited: true` (case-insensitive on the value, exact key)
    for line in fm.splitlines():
        line_stripped = line.strip()
        if line_stripped.lower().startswith("manually_edited:"):
            value = line_stripped.split(":", 1)[1].strip().lower().strip("\"'")
            return value in ("true", "yes", "1")
    return False


def write_hub(directory: Path, name: str, hub_type: str, entries: list):
    directory.mkdir(parents=True, exist_ok=True)
    fname = slug(name) + ".md"
    fpath = directory / fname

    if is_manually_edited(fpath):
        # Preserve manual edits — skip overwrite. Caller logs aggregate count.
        return "skipped"

    # `name` doubles as an ID field: extract-topics-and-tools.py reads this
    # frontmatter value back to build the topic/tool graph node id
    # (normalize_token(name) -> "topic_<...>"). It must stay the raw slug —
    # humanizing it here would silently change node ids on the next graph
    # rebuild. The H1 is purely a DISPLAY string, so only it gets humanized.
    # Entities are excluded: entity `name` values already go through
    # ENTITY_ALIASES proper-noun casing above (e.g. "GitHub Actions",
    # "GitHub") — they aren't slug-shaped, so running them through
    # humanize_slug (which assumes lowercase-hyphenated input) would
    # mangle multi-word proper nouns instead of improving them.
    display_title = humanize_slug(name) if hub_type in ("topic", "tool") else name

    lines = [
        f"---",
        f"type: {hub_type}",
        f"name: {name}",
        f"sessions: {len(entries)}",
        f"---",
        f"",
        f"# {display_title}",
        f"",
        f"**{len(entries)} session{'s' if len(entries) != 1 else ''}**",
        f"",
    ]

    for e in sorted(entries, key=lambda x: x["file"], reverse=True):
        lines.append(to_wikilink(e["file"], e["summary"]))

    fpath.write_text("\n".join(lines) + "\n")
    return "written"


# ── TOP-TOPICS.md ───────────────────────────────────────────────────────────

def _recent_count(entries, window_start):
    return sum(1 for e in entries if e.get("date") and e["date"] >= window_start)


def _weighted_score(entries, window_start):
    return RECENCY_ALPHA * _recent_count(entries, window_start) + (1 - RECENCY_ALPHA) * len(entries)


def write_top_topics(topics_idx, tools_idx, entities_idx):
    out = VAULT / "TOP-TOPICS.md"
    today = date.today()
    window_start = today - timedelta(days=RECENCY_WINDOW_DAYS)

    def render(idx, header, item_label, hub_dir, limit, humanize=False):
        rows = sorted(idx.items(), key=lambda x: -_weighted_score(x[1], window_start))[:limit]
        block = [
            "",
            f"## {header}",
            "",
            f"| Rank | {item_label} | Total | Recent {RECENCY_WINDOW_DAYS}d | Weight |",
            "|---|---|---|---|---|",
        ]
        for i, (name, entries) in enumerate(rows, 1):
            r = _recent_count(entries, window_start)
            w = _weighted_score(entries, window_start)
            # Wikilink TARGET (slug(name)) always points at the raw-slug
            # filename write_hub() actually wrote — only the DISPLAY text
            # (after the \|) is humanized, and only for topics/tools (see
            # write_hub's comment on why entities are excluded).
            display = humanize_slug(name) if humanize else name
            block.append(
                f"| {i} | [[{hub_dir}/{slug(name)}\\|{display}]] | {len(entries)} | {r} | {w:.1f} |"
            )
        return block

    lines = [
        "# TOP TOPICS",
        "",
        f"Auto-generated by consolidate.py. Rerun to refresh.",
        f"Ranking: weight = {RECENCY_ALPHA} × count_last_{RECENCY_WINDOW_DAYS}d + {1 - RECENCY_ALPHA:.1f} × total_count.",
    ]
    lines += render(topics_idx, "Topics", "Topic", "topics", 50, humanize=True)
    lines += render(tools_idx, "Tools & Frameworks", "Tool", "tools", 30, humanize=True)
    lines += render(entities_idx, "Entities", "Entity", "entities", 30, humanize=False)

    out.write_text("\n".join(lines) + "\n")
    print(f"  Written: TOP-TOPICS.md")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("Loading reports...")
    results = load_reports()
    print(f"  {len(results)} unique session entries loaded")

    print("Building indexes...")
    topics_idx, tools_idx, entities_idx = build_indexes(results)
    print(f"  {len(topics_idx)} topics | {len(tools_idx)} tools | {len(entities_idx)} entities")

    print("Writing topic hub pages...")
    topics_dir = VAULT / "topics"
    t_written = t_skipped = 0
    for name, entries in topics_idx.items():
        result = write_hub(topics_dir, name, "topic", entries)
        if result == "skipped": t_skipped += 1
        else: t_written += 1
    print(f"  {t_written} topic pages written, {t_skipped} preserved (manually_edited: true)")

    print("Writing tool hub pages...")
    tools_dir = VAULT / "tools"
    tool_written = tool_skipped = 0
    for name, entries in tools_idx.items():
        result = write_hub(tools_dir, name, "tool", entries)
        if result == "skipped": tool_skipped += 1
        else: tool_written += 1
    print(f"  {tool_written} tool pages written, {tool_skipped} preserved")

    print("Writing entity hub pages...")
    entities_dir = VAULT / "entities"
    e_written = e_skipped = 0
    for name, entries in entities_idx.items():
        result = write_hub(entities_dir, name, "entity", entries)
        if result == "skipped": e_skipped += 1
        else: e_written += 1
    print(f"  {e_written} entity pages written, {e_skipped} preserved")

    print("Writing TOP-TOPICS.md...")
    write_top_topics(topics_idx, tools_idx, entities_idx)

    print("\nDone.")
    print(f"  Topics: {len(topics_idx)}")
    print(f"  Tools:  {len(tools_idx)}")
    print(f"  Entities: {len(entities_idx)}")


if __name__ == "__main__":
    main()
