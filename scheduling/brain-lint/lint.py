#!/usr/bin/env python3
"""
Brain lint pass v1 — Karpathy-style gap detection.

Walks the Core Brain vault and compares against Core's canonical memory.
Surfaces three kinds of gaps:
  (a) brain knows about a topic, memory doesn't
  (b) memory tracks a project/person, brain hasn't mentioned it recently
  (d) brain has orphan pages with no inbound wikilinks

(c) contradiction detection deferred to v2 (needs LLM pass).

Read-only on both brain and memory. Writes report to
`memory/brain-lint-reports/YYYY-MM-DD.md`.
"""

import os
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

import os
# Path resolution. Post the self-hosted-Cores refactor (2026-06) this script
# lives INSIDE each Core (e.g. core-life/scheduling/brain-lint/lint.py), so the
# Core root IS parents[2] — there is no separate `instance/` repo anymore. The
# prior fallback `parents[2].parent / "instance"` pointed at the dead pre-refactor
# `~/AI Projects/instance` path: whenever CORE_INSTANCE was unset (lint-pass.sh
# sets no env), MEMORY resolved to a nonexistent dir, section (b) scanned zero
# entries and returned a FAKE 0, and reports misfiled to the dead path. Caught +
# fixed 2026-07-16 (stray reports were accumulating under ~/AI Projects/instance/).
_INSTANCE = Path(os.environ.get("CORE_INSTANCE", str(Path(__file__).resolve().parents[2])))
# Default MUST match the real vault dir name `core-brain` — NOT `brain`. The old
# `_INSTANCE.parent / "brain"` default pointed at a nonexistent folder whenever
# CORE_BRAIN was unset, so all_brain_md() silently returned [] and section (b)
# reported EVERY memory entry as "never mentioned in brain" — pure garbage
# (caught 2026-07-11 when a manual run without CORE_BRAIN flagged person/project slugs).
BRAIN = Path(os.environ.get("CORE_BRAIN", str(_INSTANCE.parent / "core-brain")))
MEMORY = _INSTANCE / "memory"
REPORTS = MEMORY / "brain-lint-reports"

SKIP_DIRS = {"_build", ".git", ".obsidian", "node_modules"}
RECENT_DAYS = 7  # v2: tightened from 30; brain history is ~3 weeks, 30d = "ever"
TOP_N = 30

# v2: orphan check is meaningful only on session/subagent/project files.
# tools/ and entities/ hubs are referenced via plain-text + path refs, not
# wikilinks — so they're nearly always orphans by the wikilink-only metric.
ORPHAN_SKIP_DIRS = {"tools", "entities"}

# Known memory ↔ brain topic-slug aliases. Without these, gap-topics
# floods with false positives where the topic slug differs from the memory
# filename (e.g., topic `core-ui-development` vs memory `core-ui.md`), or
# where the topic is a generic concept covered across many memory files
# rather than as a single named slug (e.g., `memory-management`).
#
# Lookup logic: if KEY appears anywhere in concatenated memory text, all
# brain topic slugs in VALUES are marked as covered (suppressed from gap-topics).
#
# CANONICAL SOURCE: `.claude/identity.json` -> `brain_lint_topic_aliases`.
# That key holds project-specific aliases (loaded into TOPIC_ALIASES below).
# The fallback dict here covers the case where identity.json is missing or
# malformed — keeps the lint useful in a freshly-cloned engine where the
# instance hasn't populated identity.json yet. Engine ships with this minimal
# fallback; instance overrides via identity.json.
import json as _json

_FALLBACK_TOPIC_ALIASES = {
    # Generic concept-level aliases. These map broadly-present substrings
    # in memory to brain topic slugs they cover. Safe defaults for any
    # instance. Project-specific aliases (core-ui, job-tracker, etc.)
    # live in identity.json and are merged in below.
    "memory": {"memory-management"},
    "calendar": {"calendar-management"},
    "launchd": {"launchd-scheduling"},
    "sentinel": {"git-push-approval", "outward-action-approval"},
    "token": {"token-optimization"},
    "audit": {"session-auditing"},
    "close-reconciler": {"close-reconciliation", "session-close-protocol"},
}


def _load_topic_aliases() -> dict:
    """Merge identity.json's `brain_lint_topic_aliases` (instance-specific)
    with the generic fallback. Returns dict[str, set[str]].
    """
    merged = {k: set(v) for k, v in _FALLBACK_TOPIC_ALIASES.items()}
    identity_path = _INSTANCE / ".claude" / "identity.json"
    try:
        with open(identity_path) as f:
            data = _json.load(f)
        aliases = data.get("brain_lint_topic_aliases", {})
        for key, slugs in aliases.items():
            if key.startswith("_"):
                continue
            if not isinstance(slugs, list):
                continue
            merged.setdefault(key, set()).update(slugs)
    except (FileNotFoundError, PermissionError, _json.JSONDecodeError, OSError):
        pass
    return merged


TOPIC_ALIASES = _load_topic_aliases()


def all_brain_md():
    out = []
    for root, dirs, files in os.walk(BRAIN):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".md") and not f.startswith("."):
                out.append(Path(root) / f)
    return out


def all_memory_md():
    out = []
    for root, dirs, files in os.walk(MEMORY):
        dirs[:] = [d for d in dirs if d not in {"archive", "brain-lint-reports"} and not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                out.append(Path(root) / f)
    return out


def slug_variants(name):
    n = name.lower()
    return {n, n.replace("-", " "), n.replace("-", "_"), n.replace("_", " ")}


_WORD_RE = re.compile(r"[a-z0-9]+")


def _iter_md(roots, exclude_dirs):
    """Yield every *.md file Path under `roots`, skipping `exclude_dirs` by name."""
    ex = set(exclude_dirs)
    for r in roots:
        r = Path(r)
        if not r.exists():
            continue
        for dirpath, dirs, files in os.walk(r):
            dirs[:] = [d for d in dirs if d not in ex and not d.startswith(".")]
            for fn in files:
                if fn.endswith(".md") and not fn.startswith("."):
                    yield Path(dirpath) / fn


def _grep_present(patterns, roots, whole_word=False,
                  exclude_dirs=("_build", ".git", ".obsidian", "node_modules")):
    """Return the subset of `patterns` that occur (case-insensitively) in any *.md
    file under `roots`, matched as a WHOLE-WORD token SEQUENCE.

    PURE PYTHON, single pass per file, O(total_tokens) — deliberately NO external
    binary. History (2026-07-11): the original did 7,540 slugs × a 180MB regex/`in`
    scan = ~1.4 TB/run and hung as the brain grew; a `grep -F -f` rewrite looked fast
    in the shell but the shell's `grep` is really `ugrep`, while Python's subprocess
    gets BSD `/usr/bin/grep`, which took >120s on 44 patterns (and `rg`/`ugrep`/GNU
    `grep` are shell shims / not installed as callable binaries). So depending on any
    grep is unreliable. This version can't hang: it tokenizes each file once and
    matches slug token-tuples via a first-token index, pruning patterns as they're
    found. Hyphens/underscores/spaces in a slug are all treated as token separators,
    so `docker-container`, `docker_container`, and `docker container` all match.

    `whole_word` is accepted for signature compatibility; matching is always
    token-boundary (so "core" never matches inside "scoreboard").
    """
    # Map token-tuple -> original pattern strings; index tuples by their first token.
    tuple_to_pats, by_first = {}, {}
    for p in patterns:
        if not p or not p.strip():
            continue
        toks = tuple(_WORD_RE.findall(p.lower()))
        if not toks:
            continue
        tuple_to_pats.setdefault(toks, set()).add(p.strip().lower())
        by_first.setdefault(toks[0], []).append(toks)
    if not by_first:
        return set()

    present = set()
    for f in _iter_md(roots, exclude_dirs):
        if not by_first:
            break  # every pattern already found — stop reading
        try:
            toks = _WORD_RE.findall(f.read_text(errors="ignore").lower())
        except Exception:
            continue
        n = len(toks)
        for i, t in enumerate(toks):
            cands = by_first.get(t)
            if not cands:
                continue
            still = []
            for cand in cands:
                L = len(cand)
                if i + L <= n and tuple(toks[i:i + L]) == cand:
                    present |= tuple_to_pats.get(cand, set())
                else:
                    still.append(cand)  # keep searching this one
            if len(still) != len(cands):
                if still:
                    by_first[t] = still
                else:
                    del by_first[t]
    return present


# ─── Section (a): Gap Topics ─────────────────────────────────────────────────
def section_a():
    """Topics in 3+ brain sessions not mentioned in any memory/ file."""
    topics = []
    for tf in (BRAIN / "topics").glob("*.md"):
        text = tf.read_text(errors="ignore")
        m = re.search(r"^sessions:\s*(\d+)", text, re.M)
        if m:
            topics.append((tf.stem, int(m.group(1)), tf))

    candidates = sorted(
        [(n, c, p) for n, c, p in topics if c >= 3],
        key=lambda x: x[1],
        reverse=True,
    )

    memory_concat = "\n".join(
        mf.read_text(errors="ignore") for mf in all_memory_md()
    ).lower()

    # v2: build reverse alias lookup — for each brain topic slug, find which
    # memory entries cover it (so we don't flag aliased topics as gaps).
    aliased_topics = set()
    for memory_stem, brain_slugs in TOPIC_ALIASES.items():
        if memory_stem in memory_concat or memory_stem.replace("-", " ") in memory_concat:
            aliased_topics.update(brain_slugs)

    gaps = []
    for name, count, path in candidates:
        if name in aliased_topics:
            continue
        if not any(v in memory_concat for v in slug_variants(name)):
            gaps.append((name, count, path))
    return gaps[:TOP_N]


# ─── Section (b): Gap Memory ─────────────────────────────────────────────────
def section_b():
    """Memory entries with no brain mentions or no recent mentions."""
    entries = []
    for mf in (MEMORY / "projects").glob("*.md"):
        if mf.stem == "shared":
            continue
        entries.append(("project", mf.stem, mf))
    rel_dir = MEMORY / "relationships"
    if rel_dir.exists():
        for mf in rel_dir.rglob("*.md"):
            if "archive" in mf.parts:
                continue
            entries.append(("relationship", mf.stem, mf))

    brain_files = all_brain_md()
    brain_stems = {bf.stem.lower() for bf in brain_files}

    # in_brain: ONE grep pass over the whole vault for every entry's name-variants,
    # instead of loading a 180MB concat and substring-scanning per entry every run
    # (the old O(entries × brain) that read the entire vault into RAM).
    all_variants = set()
    for _, _name, _ in entries:
        all_variants |= slug_variants(_name)
    # whole_word: a memory name counts as "mentioned" when it appears as a whole token,
    # NOT as an incidental substring. This is both more correct (no "core" inside
    # "scoreboard") and what makes it FAST — substring `-o` on a common token like
    # "core"/"max" matched millions of times and took 120s; whole-word bounds it to <2s.
    present = _grep_present(all_variants, [BRAIN], whole_word=True)

    # Recent window (last RECENT_DAYS) is a small subset — read those in Python.
    recent_threshold = datetime.now() - timedelta(days=RECENT_DAYS)
    recent_text_parts = []
    for sf in brain_files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", sf.name)
        if m:
            try:
                if datetime.strptime(m.group(1), "%Y-%m-%d") >= recent_threshold:
                    recent_text_parts.append(sf.read_text(errors="ignore"))
            except ValueError:
                pass
    recent_concat = "\n".join(recent_text_parts).lower()

    gaps = []
    for kind, name, path in entries:
        variants = slug_variants(name)
        in_hub = any(v in brain_stems for v in variants)
        in_brain = any(v in present for v in variants)
        in_recent = any(v in recent_concat for v in variants)
        if not in_brain:
            gaps.append((kind, name, "never mentioned in brain", path))
        elif not in_recent and not in_hub:
            gaps.append((kind, name, f"no mention in last {RECENT_DAYS} days", path))
    return gaps[:TOP_N]


# ─── Section (d): Orphan Topics ───────────────────────────────────────────────
def section_d():
    """Topic-hub pages whose slug is referenced nowhere else in the brain.

    v4: the brain does NOT use [[wikilink]] syntax — verified zero of 461
    topic pages contain wikilinks. The original v1/v2 check produced ~all
    topics as orphans (false positives capped at TOP_N).

    Replaced with: a topic is orphan only if its slug name does not appear
    as plain text anywhere in the rest of the brain (other topic pages,
    session files, subagent files, project hubs). This matches how the
    brain actually cross-references — by name and path, not wikilinks.

    Real orphans surface topics that have no inbound mentions at all —
    candidates for merging into a parent topic or removal.
    """
    topics_dir = BRAIN / "topics"
    if not topics_dir.exists():
        return []

    topic_files = list(topics_dir.glob("*.md"))

    # A topic counts as referenced if its slug (or space-variant) appears as a WHOLE
    # WORD in any NON-topic brain file. This was the quadratic killer: 3,770+ topic
    # slugs × a regex scan over a ~100MB concat = ~1.4 TB/run, single-threaded — it
    # hung every time once the brain grew. Now: ONE whole-word `grep` pass over the
    # brain minus topics/. Word-boundary (`grep -w`) mirrors the old `\bslug\b`, so
    # "audit" still won't match "auditor". (2026-07-11 rewrite.)
    variants = {}
    all_pats = set()
    for f in topic_files:
        slug = f.stem.lower()
        vs = {slug, slug.replace("-", " ")}
        variants[f] = vs
        all_pats |= vs
    present = _grep_present(
        all_pats, [BRAIN], whole_word=True,
        exclude_dirs=("_build", ".git", ".obsidian", "node_modules", "topics"),
    )
    orphans = [f for f in topic_files if not (variants[f] & present)]
    return orphans[:TOP_N]


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Fail LOUD, never silently emit garbage. An empty/missing brain makes every
    # section flag every entry as absent — a report worse than none, because it
    # reads as authoritative. Abort with a clear message and non-zero exit BEFORE
    # writing anything, so callers (SI detect, session-start) see failure not noise.
    import sys as _sys
    _brain_md = all_brain_md() if BRAIN.exists() else []
    if not BRAIN.exists() or len(_brain_md) == 0:
        _sys.stderr.write(
            f"brain-lint ABORTED: BRAIN={BRAIN} "
            f"({'does not exist' if not BRAIN.exists() else '0 markdown files'}). "
            "Set CORE_BRAIN to the vault path (…/core-brain). Not writing a report — "
            "an empty brain would flag every memory entry as a false 'never mentioned'.\n"
        )
        return 2

    REPORTS.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    now_full = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    out_path = REPORTS / f"{today}.md"

    print(f"Running brain lint v1 → {out_path}")
    a = section_a()
    print(f"  (a) gap-topics:    {len(a)}")
    b = section_b()
    print(f"  (b) gap-memory:    {len(b)}")
    d = section_d()
    print(f"  (d) orphan-pages:  {len(d)}")

    lines = []
    lines.append("---")
    lines.append(f"generated: {now_full}")
    lines.append(f"brain_path: {BRAIN}")
    lines.append(f"memory_path: {MEMORY}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Brain Lint Report — {today}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"{len(a)} gap-topics · {len(b)} gap-memory · {len(d)} orphan-pages")
    lines.append("")

    lines.append("## (a) Gap Topics — brain knows, memory doesn't")
    lines.append("")
    if a:
        for name, count, path in a:
            lines.append(f"- `{name}` — {count} sessions · `{path}`")
    else:
        lines.append("_No gap topics found._")
    lines.append("")

    lines.append("## (b) Gap Memory — memory tracks, brain doesn't recently")
    lines.append("")
    if b:
        for kind, name, reason, path in b:
            lines.append(f"- `{name}` ({kind}) — {reason} · `{path}`")
    else:
        lines.append("_No stale memory entries found._")
    lines.append("")

    lines.append("## (d) Orphan Pages — no inbound wikilinks")
    lines.append("")
    if d:
        for f in d:
            lines.append(f"- `{f}`")
    else:
        lines.append("_No orphan pages found._")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("- **(a)** flags brain topics with ≥3 sessions whose slug isn't mentioned anywhere in `memory/`. Real gaps if Core is supposed to track them.")
    lines.append(f"- **(b)** flags memory project/relationship entries with no brain mention in the last {RECENT_DAYS} days. Possibly stale.")
    lines.append("- **(d)** flags topic-hub pages whose slug appears nowhere else in the brain (other topics, sessions, subagents, project hubs). Real orphans surface candidates for merging into a parent topic or removal — typical cause is extraction over-fragmentation (e.g., 3 separate `claude-md-*` topics from one CLAUDE.md session).")
    lines.append("- **Aliases:** v3 uses `TOPIC_ALIASES` in `lint.py` to suppress (a) false positives where the brain topic slug differs from a memory file (e.g., `core-ui-development` ↔ `core-ui.md`) or the concept is spread across multiple memory files (e.g., `memory-management` covered by all of `memory/`). Add new aliases as they surface.")
    lines.append("- v3 contradiction-detection (was: `lint-v3.py`) OBSOLETED 2026-05-18 — superseded by compile-truth (brain-pg Step 3) + hybrid RRF query layer (`scheduling/brain-pg/query.py`).")

    out_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
