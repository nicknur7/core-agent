#!/usr/bin/env python3
"""Which Core owns a flat vault hub. THE single rule — import it, never re-implement it.

THE BUG THIS REPLACES (2026-08-31)
==================================
`embed.py::pass_hubs` read `hub_org = get_org_id()` with the comment "hubs are global content owned
by the running Core". `discover_hubs()` globs `entities/` + `topics/` with no filter. Together that
means EVERY Core ingests ALL ~7,661 flat hub files into its own partition, so the org column records
which Core happened to run the pass rather than whose knowledge it is.

That was true with one Core and false from the moment there were five. Measured before the fix:
61% of the OPS Core's entities came from the flat pool, so the highest-degree hubs in a business
Core's partition were the operator's PERSONAL entities, not the business's.

WHY A SHARED MODULE AND NOT A SECOND COPY
=========================================
The repair tool (bin/repartition-hubs.py) and the ingester (embed.py) must agree exactly, or the
next hub pass re-creates precisely what the repair just removed — the ingester would keep writing
rows the repair keeps retiring, forever, and the loop would look like data corruption rather than
two implementations of one rule disagreeing. This file is that rule. Both import it.

THE RULE
========
A hub belongs to the Core that DOMINATES the sessions it cites (>= DOMINANCE of citations).
A hub whose citations are genuinely split belongs to the shared tier (org 0), read by every Core
and owned by none. A hub citing nothing resolvable has no derivable owner and is left alone.

Dominance, not "cites exactly one Core". The binary version was tried and was wrong on real data:
it filed a frequently cited personal contact — 298 life citations against 4 school and 2 business,
98% life — as shared infrastructure on the strength of six stray mentions, and did the same to
Sentinel (98% life), Anthropic (93%) and Oregon State (89%). 0.80 is not arbitrary: of the 414
hubs the binary rule sent to shared, 120 have a dominant Core at >=80% and 123 sit below 60%. The
threshold separates two real populations rather than cutting a continuum.

SUBJECT BEATS PROVENANCE. Citation counts say where a hub's TEXT came from, which is not always
whose knowledge it is: a hub can be majority life-cited purely because it was discussed in the
operator's personal Core before its own dedicated domain Core existed. A Core spawned to own a
domain owns that domain's entities regardless of which Core first wrote them down, so DOMAIN_CLAIMS
is checked FIRST and wins. The operator, on being told a domain Core's hub belonged to life by
citation count, pushed back — the domain has its own Core, so it should own its own hub — and he
is right, and citation-counting alone cannot see it.
"""
from __future__ import annotations

import collections
import re
from pathlib import Path

SHARED_ORG = 0
DOMINANCE = 0.80

# The five Cores, plus historical/aux buckets that resolve to one of them. Not guesses — these are
# the mappings export.py's own CWD_PROJECT_RULES already applies. The repo was renamed
# ~/AI Projects/core -> core-life, so `projects/core/` is life's pre-rename path, and career-ops was
# renamed job-hunter (a life PROJECT, not a Core). Without these, 782 hub files resolve to nothing
# purely because they cite a directory that was renamed out from under them.
SLUG_TO_ORG = {
    "life": 1, "business": 2, "school": 3, "finance": 4, "ops": 5,
    "core": 1, "core-life": 1, "core-nick": 1, "core-nick-brain": 1,
    "home": 1, "archive": 1, "relationships": 1, "job-hunter": 1, "career-ops": 1,
}
ORG_NAME = {0: "shared", 1: "life", 2: "business", 3: "school", 4: "finance", 5: "ops"}

# DOMAIN CLAIMS ARE PER-DEPLOYMENT CONFIG, NOT ENGINE — and this file used to ship them.
#
# The first version of this block hardcoded the original operator's entire life as regexes: his
# university, his family's company by name, the vendors and licensing bodies it deals with, and an
# internship employer in the exclude list. Every one shipped to every seat and to a public repo,
# and NEITHER privacy lint could see them — bin/strip-check.py hunts identifiers, bin/prose-
# privacy-lint.py hunts comment prose, and a bare name inside a regex literal is neither. A
# whole-tree review caught a real third party's name, as a bare regex literal in the exclude
# list, sitting two lines under a docstring claiming the identity was "deliberately not written
# here". The comment was fixed; the code was not. That is the failure mode this whole file is
# meant to prevent, one level down — and the FIRST rewrite of this very paragraph then quoted the
# leaked literal in a backtick "as the record of the fix", which re-leaked it, because a name in a
# code span reads as code to a prose scanner. Describe the incident; never quote the identifier.
#
# So the taxonomy now lives OUTSIDE the engine, in a per_core_keep file the sync never ships:
#
#     $CORE_INSTANCE/.claude/hub-domains.json
#     {
#       "claims":  { "<org_id>": ["<regex>", ...], ... },
#       "exclude": { "<org_id>": ["<regex>", ...], ... }
#     }
#
# Absent file = no domain claims beyond the SHARED tier below, and ownership falls through to
# citation dominance, which is correct for a fresh seat that has no domains yet. The one rule
# that ships is the shared-tier rule, because it is genuinely about the ENGINE (a hub named for
# a baseline code path belongs to no seat) and contains nothing about any person.
#
# Keep per-seat patterns tight and unambiguous — a broad pattern silently annexes another Core's
# data, and that is the bug that produced this module in the first place.
import json as _json
import os as _os

SHARED_CLAIMS: list[str] = [
    # Hubs NAMED for a shared-baseline code path. `.claude/hooks/stop-hook.sh` is the same file on
    # every seat — it is not the writer's because the writer happens to publish it, and it is not
    # a business Core's because it sat among that Core's top-degree hubs. A file every Core runs
    # is owned by none of them, which is what the shared tier is for. Deliberately excludes
    # memory/ and sessions/ — those ARE per-Core.
    r"^\.claude/", r"^bin/", r"^scheduling/", r"^tasks/lessons", r"^docs/",
    r"^CLAUDE\.base\.md$", r"^sync-manifest",
]


CITE = re.compile(r"projects/([a-z0-9][a-z0-9_-]*)/", re.I)
# Bare `sessions/YYYY-MM-DD` citations predate the per-Core projects/ layout and are life's.
LEGACY = re.compile(r"(?<!/)\bsessions/\d{4}-\d{2}-\d{2}", re.I)
# A THIRD CITATION SHAPE, found the first time this tool was applied (2026-09-02, org 5). The two
# shapes above are the only ones the rule recognised, so a hub whose ONLY citation was a path under
# a per-Core content directory read as unattributable — even when that directory plainly belongs
# to one Core. Measured on the ownerless bucket after that apply: 182 of 586 ownerless hub FILES
# cite `memory/education/courses/` and nothing else. Coursework is the school Core's by definition;
# the file was stating its origin and the rule could not hear it.
#
# The mapping is a TABLE, not a regex per Core, so a fork adds its own per-Core content dirs here
# (or, preferably, in the same per-seat hub-domains.json that carries DOMAIN_CLAIMS — see below)
# rather than editing engine code. Counted with the same dominance rule as project citations, so a
# hub that cites two Cores' content dirs still resolves to SHARED rather than to whichever wins a
# coin flip.
CONTENT_DIR_ORG: dict[str, int] = {
    "memory/education/": 3,
}
CONTENT_DIR = re.compile("|".join(re.escape(k) for k in CONTENT_DIR_ORG), re.I)


def _load_domains() -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    claims: dict[int, list[str]] = {SHARED_ORG: list(SHARED_CLAIMS)}
    exclude: dict[int, list[str]] = {}
    inst = _os.environ.get("CORE_INSTANCE") or _os.environ.get("CLAUDE_PROJECT_DIR")
    if not inst:
        return claims, exclude
    cfg = Path(inst) / ".claude" / "hub-domains.json"
    try:
        d = _json.loads(cfg.read_text())
    except (OSError, ValueError):
        return claims, exclude
    for k, pats in (d.get("claims") or {}).items():
        try:
            claims.setdefault(int(k), []).extend(str(x) for x in pats)
        except (TypeError, ValueError):
            continue
    for k, org in (d.get("content_dirs") or {}).items():
        try:
            CONTENT_DIR_ORG[str(k)] = int(org)
        except (TypeError, ValueError):
            continue
    for k, pats in (d.get("exclude") or {}).items():
        try:
            exclude.setdefault(int(k), []).extend(str(x) for x in pats)
        except (TypeError, ValueError):
            continue
    return claims, exclude


DOMAIN_CLAIMS, DOMAIN_EXCLUDE = _load_domains()
CONTENT_DIR = re.compile("|".join(re.escape(k) for k in CONTENT_DIR_ORG), re.I)

_CLAIM_RE = {org: [re.compile(p, re.I) for p in pats] for org, pats in DOMAIN_CLAIMS.items()}
_EXCL_RE = {org: [re.compile(p, re.I) for p in pats] for org, pats in DOMAIN_EXCLUDE.items()}


def claimed_by_domain(name: str) -> int | None:
    """The Core whose domain this name belongs to, or None. Subject, not provenance.

    The name is normalised first because this is called with BOTH a hub's real frontmatter name
    ("Acme Construction") and, from owner_for_path, a filename stem
    ("acme-construction"). Matching only the spaced form silently missed every hub
    addressed by path — which is how the first version still resolved OPS to life after the domain
    claim was added.
    """
    norm = re.sub(r"[-_]+", " ", name)
    # Match BOTH the raw name and the normalised one. Found by a test written for the per-seat
    # config: an exclude written as r"\bwidgets-rival\b" could never fire, because the name had
    # already been normalised to "widgets rival" before it was consulted. The shipped exclude
    # happened to be a single word with no hyphen, so the defect was invisible until someone
    # wrote a multi-word pattern — which is exactly what a fork editing hub-domains.json will do.
    forms = (name, norm)
    for org, pats in _CLAIM_RE.items():
        if any(p.search(f) for p in pats for f in forms):
            if any(p.search(f) for p in _EXCL_RE.get(org, []) for f in forms):
                continue
            return org
    return None




def citation_owner(text: str) -> int | None:
    """The dominant Core among this hub's session citations, SHARED_ORG if split, None if unknown."""
    counts: collections.Counter = collections.Counter()
    for m in CITE.finditer(text):
        slug = m.group(1).lower()
        if slug in SLUG_TO_ORG:
            counts[SLUG_TO_ORG[slug]] += 1
    for m in CONTENT_DIR.finditer(text):
        key = m.group(0).lower()
        for k, org in CONTENT_DIR_ORG.items():
            if key == k.lower():
                counts[org] += 1
    if not counts:
        return 1 if LEGACY.search(text) else None
    org, n = counts.most_common(1)[0]
    return org if n / sum(counts.values()) >= DOMINANCE else SHARED_ORG


def owner_for(name: str, text: str) -> int | None:
    """The org that owns this hub. Domain claim wins; else citation dominance."""
    claimed = claimed_by_domain(name)
    return claimed if claimed is not None else citation_owner(text)


def owner_for_path(path: Path, name: str | None = None) -> int | None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    return owner_for(name if name is not None else path.stem, text)
