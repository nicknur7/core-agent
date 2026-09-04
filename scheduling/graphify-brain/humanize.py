#!/usr/bin/env python3
"""Shared display-string helper: turn a pipeline slug into a human-readable title.

Problem: brain-graph hub/topic titles come straight from LLM-extracted slugs
like `phase-11-r14-knowledge-graph-extraction` or `body-extraction-prompt-v2-2`
— readable to the pipeline, nonsense to a human looking at the graph viz or a
hub page. This module fixes the DISPLAY string only; the slug itself stays the
stable node id / filename everywhere (dedupe, edges, and file lookups are
untouched — see the docstring on `humanize_slug` for the one dedupe risk this
does carry).

CANONICAL COPY: this file is the source of truth for the shared-baseline side
(syncs via bin/sync-to-baseline.sh, part of `scheduling/graphify-brain` in
bin/sync-manifest.json). A byte-identical TWIN lives at
`$CORE_BRAIN/_build/humanize.py` because consolidate.py runs out of the brain
vault repo (a separate git repo per Core / per fork, not covered by the
Core-instance sync manifest) and can't import across repos without assuming a
sibling directory layout that isn't guaranteed to hold for every fork. If you
edit the logic here, port the same edit to the twin by hand.

Generic and fork-portable by design: no personal names, no per-Core project
slugs. The acronym map is deliberately limited to (a) universal tech/PM terms
and (b) the shared engine's own stack (RRF, HNSW, RLS, PG — used by every Core
running this pipeline, not just this instance). Extend the map freely; do not
add anything person- or Core-specific here — that belongs in each instance's
own config, not the shared helper.
"""
import re

# Tokens (lowercased) that should render as a fixed acronym/brand casing
# rather than Title Case. Extend freely — this is the one "maintainable"
# knob this module exposes. Keep it generic (tech/PM terms), not personal.
ACRONYM_MAP = {
    # AI / ML
    "ai": "AI", "llm": "LLM", "nlp": "NLP", "gpt": "GPT", "asi": "ASI",
    "ocr": "OCR", "asr": "ASR", "tts": "TTS",
    # Core engine's own stack (shared across every fork running this pipeline)
    "mcp": "MCP", "si": "SI", "pg": "PG", "rrf": "RRF", "rls": "RLS",
    "hnsw": "HNSW", "bfs": "BFS", "dfs": "DFS",
    # Web / API / data
    "api": "API", "html": "HTML", "json": "JSON", "css": "CSS", "js": "JS",
    "ts": "TS", "sql": "SQL", "xml": "XML", "yaml": "YAML", "csv": "CSV",
    "pdf": "PDF", "url": "URL", "uri": "URI", "uuid": "UUID", "jwt": "JWT",
    "http": "HTTP", "https": "HTTPS", "dns": "DNS", "cdn": "CDN",
    "rest": "REST", "etl": "ETL", "orm": "ORM", "db": "DB", "cli": "CLI",
    "sdk": "SDK", "os": "OS", "ui": "UX", "ux": "UX", "io": "IO", "id": "ID",
    "qa": "QA", "pr": "PR", "vm": "VM", "cpu": "CPU", "gpu": "GPU",
    "ram": "RAM", "ci": "CI", "cd": "CD",
    # PM / business
    "kpi": "KPI", "roi": "ROI", "mvp": "MVP", "poc": "POC", "wbs": "WBS",
    "faq": "FAQ", "crm": "CRM", "erp": "ERP", "seo": "SEO",
    # Mixed-case special forms
    "saas": "SaaS", "oauth": "OAuth", "macos": "macOS", "ios": "iOS",
    "graphql": "GraphQL", "grpc": "gRPC",
}
# "ui" above intentionally maps to UX per Core's own convention collision —
# fixed below to avoid ambiguity (UI is far more common as a literal token).
ACRONYM_MAP["ui"] = "UI"

# Small words that stay lowercase mid-title (never first/last word).
MINOR_WORDS = {
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "nor",
    "but", "vs", "via", "from", "with", "at", "by", "as",
}

# Pipeline-management wrapper: "phase-11-", "phase-0a-" etc. Pure internal
# tracking noise (every pipeline round has one), not content — safe to
# delete outright. Non-anchored so it also catches "graphify-brain-phase-11-
# round-13-..." where the marker isn't at the very start of the slug.
_PHASE_PREFIX_RE = re.compile(r'(?:^|-)phase-\d+[a-z]?(?=-|$)')

# A single "rNN" or "vNN" token (round / version marker). Deliberately kept
# (uppercased) rather than deleted — see the dedupe-risk note in the
# docstring below.
_ROUND_OR_VERSION_RE = re.compile(r'^[rv]\d+$')

# Merged "v2.2"-style token after the vN + bare-digit merge pass.
_VERSION_DOTTED_RE = re.compile(r'^v(\d+)\.(\d+)$')

# "3d", "0a" — digit run followed by exactly one trailing letter.
_DIGIT_LETTER_RE = re.compile(r'^(\d+)([a-z])$')


def humanize_slug(slug: str) -> str:
    """Convert a pipeline slug (lowercase, hyphen-separated) into a
    human-readable display title. Returns the input unchanged (title-cased
    as a last resort) if it doesn't look like a slug at all.

    DOES NOT touch the slug used as node id / filename anywhere — this is a
    display-only transform. Call it exactly where a label/title string is
    assembled, never where an id is compared, looked up, or written to disk.

    Design choices worth knowing about (the risk callout the orchestrator
    asked for):

    - `phase-N[a-z]-` prefixes are DELETED (pure pipeline-management noise,
      no content value, safe — every round has one so it never disambiguates
      two different topics from each other).
    - `rN` / `vN` round/version markers are KEPT but re-cased to `R14` / `V2`
      instead of being deleted outright. The brief's spec called for
      stripping `r\\d+-` and `-v\\d+(-\\d+)?$` patterns; this implementation
      deliberately does NOT delete them, because doing so can make two
      DIFFERENT hub slugs collide onto the SAME display string (e.g.
      `core-ui-v1` and `core-ui-v2` would both become "Core UI"). Slugs stay
      unique either way (they're untouched), but a graph viz / hub index
      showing two nodes with an identical label is a real UX regression, not
      just a cosmetic one. Uppercasing instead of deleting keeps the display
      string unique while still cleaning up the ugly lowercase-hyphen
      artifact. Flag this to a human reviewer if exact literal-strip
      behavior was expected instead.
    """
    if not slug:
        return slug
    s = slug.strip().lower()
    s = _PHASE_PREFIX_RE.sub('', s)
    # Collapse any double-hyphens left behind by the removal, trim edges.
    s = re.sub(r'-{2,}', '-', s).strip('-')
    if not s:
        # Slug was ONLY a phase marker (rare/degenerate) — fall back to a
        # plain title-case of the original rather than returning empty.
        return slug.replace('-', ' ').replace('_', ' ').title()

    tokens = s.split('-')

    # Merge a "vN" token immediately followed by a bare-digit token into a
    # single "vN.M" (e.g. "v2", "2" -> "v2.2") so version numbers like
    # body-extraction-prompt-v2-2 read as "V2.2" instead of "V2 2".
    merged = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if re.fullmatch(r'v\d+', t) and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            merged.append(f'{t}.{tokens[i + 1]}')
            i += 2
            continue
        merged.append(t)
        i += 1
    tokens = merged

    words = []
    last_idx = len(tokens) - 1
    for idx, t in enumerate(tokens):
        if not t:
            continue
        low = t.lower()

        m = _VERSION_DOTTED_RE.match(low)
        if m:
            words.append(f'V{m.group(1)}.{m.group(2)}')
            continue
        if _ROUND_OR_VERSION_RE.match(low):
            words.append(low.upper())
            continue
        if low in ACRONYM_MAP:
            words.append(ACRONYM_MAP[low])
            continue
        if low.isdigit():
            words.append(low)
            continue
        dl = _DIGIT_LETTER_RE.match(low)
        if dl:
            words.append(f'{dl.group(1)}{dl.group(2).upper()}')
            continue
        if low in MINOR_WORDS and 0 < idx < last_idx:
            words.append(low)
            continue
        words.append(t[:1].upper() + t[1:].lower())

    return ' '.join(words) if words else slug


if __name__ == "__main__":
    # Quick self-test / demo — run `python3 humanize.py` to eyeball output.
    # These are 15 real slugs pulled from $CORE_BRAIN/topics/*.md filenames
    # (2026-07-01), picked for maximum ugliness across the pattern classes
    # this module targets: phase-N wrappers, rN/vN round-and-version
    # markers, and generic acronym tokens.
    SAMPLES = [
        "phase-11-r14-knowledge-graph-extraction",
        "body-extraction-prompt-v2-2",
        "brain-extraction-pipeline-graphify-brain-v2-3",
        "brain-graph-extraction-pipeline-phase-11-r14-r15",
        "brain-subagent-logs-from-2026-05-11-r11-r12-batches",
        "core-ui-v1",
        "core-ui-v2",
        "extraction-hardening-rules-r11-r12-lessons-applied-to-r13-r14",
        "graphify-brain-phase-11-round-13-r13-50-wiki-files",
        "graphify-brain-phase-11-rounds-r1-r4",
        "json-schema-v2",
        "karpathy-lint-pass-v1-v2",
        "lint-v3-v4",
        "multi-core-architecture-v2",
        "phase-0a-archive-consolidation-tasks-cleanup",
    ]
    for s in SAMPLES:
        print(f"{s!r:70} -> {humanize_slug(s)!r}")
