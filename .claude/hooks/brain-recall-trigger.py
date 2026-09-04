#!/usr/bin/env python3
"""UserPromptSubmit hook — detects references to known people, projects,
or past-conversation markers in the user's prompt and injects a reminder to
proactively invoke the claude-brain recall skill before responding.

Pairs with verification-trigger.py:
  - verification-trigger enforces read-before-respond on state-claim questions
  - brain-recall-trigger enforces brain-recall on references to prior context
    that lives in the brain vault

Person/project slugs are filesystem-driven from memory/relationships/ and
memory/projects/ so they stay current as the user's life evolves. Conversation
markers are hardcoded.

Output via hookSpecificOutput.additionalContext, never blocks the prompt.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # operator name from identity.json, never hardcoded

import json
import os
import re
import sys
from pathlib import Path

# Prompt-stage hooks fire for runtime-injected text too — task-notifications, monitor events, command
# stdout. Guard imported rather than re-declared: HOOK_PREFIXES already existed in FOUR byte-identical
# copies and none of them listed `<task-notification>`, which is 72% of this seat's prompt-stage
# traffic. See .claude/hooks/_prompt_source.py. Import failure leaves today's behaviour unchanged.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _prompt_source import is_user_text as _is_user_text
except Exception:  # pragma: no cover - fail toward firing, never toward silent suppression
    def _is_user_text(_p):
        return True



# Resolve the instance root. Prefer $CORE_INSTANCE, but a UserPromptSubmit hook
# does NOT reliably get it in its env. The previous "fail loud if unset" guard
# printed to stderr (INVISIBLE for a hook) and exited — which silently killed this
# hook from 2026-05-16 to 2026-05-31 (recall never nudged; see post-mortem
# tasks/research/brain-recall-dead-hook-postmortem-2026-05-31.md). So fall back to
# deriving the root from this file's location (.claude/hooks/ -> repo root), exactly
# like rot-check.py (line 343). NEVER hard-exit a hook on missing env again — for a
# hook, "fail loud" to stderr is fail SILENT.
_instance_env = os.environ.get("CORE_INSTANCE")
INSTANCE_ROOT = Path(_instance_env) if _instance_env else Path(__file__).resolve().parents[2]
RELATIONSHIPS = INSTANCE_ROOT / "memory" / "relationships"
PROJECTS = INSTANCE_ROOT / "memory" / "projects"


PAST_CONVERSATION_MARKERS = [
    r"\bwe\s+talked\s+about\b",
    r"\bwe\s+discussed\b",
    r"\bwe\s+decided\b",
    r"\bwe\s+covered\b",
    r"\byesterday\b",
    r"\blast\s+time\b",
    r"\blast\s+(?:call|meeting|session|conversation)\b",
    r"\bremember\s+when\b",
    r"\bafter\s+the\s+(?:call|meeting)\b",
    r"\bthe\s+meeting\s+(?:went|was|with)\b",
    # Added 2026-05-16 per spec-cascade-fix Phase 3 — missed a real "talked to my [relation]
    # about [a tool]" prompt on 5/15.
    r"\b(?:talked|spoke|chatted)\s+(?:with|to)\b",
    r"\bmy\s+(?:sister|brother|mom|dad|stepdad|stepmom|girlfriend|partner|friend|cousin|uncle|aunt|wife|husband|kid|son|daughter)\b",
    r"\b(?:he|she|they)\s+(?:said|told\s+me|asked\s+me|mentioned|wanted)\b",
    # Added 2026-05-17 per tasks/research/brain-recall-pattern-audit-2026-05-17.md.
    # Replaces narrow `\bi\s+(?:told|called|asked|texted)\s+\w+` (one-word-after only) with broader form.
    r"\bi\s+(?:told|said|mentioned)\s+(?:you|him|her|them)\b",                              # 67% precision in audit
    r"\bi\s+have\s+(?:told|said|been|talked|mentioned)\b",                                  # 100% precision
    r"\bi\s+was\s+getting\s+at\b",                                                          # 100% precision
    r"\bit\s+(?:missed|seemed|was)\s+(?:the\s+)?(?:moment|point|right|correct|wrong)\b",    # 100% precision
    r"\bthat\s+(?:we\s+)?(?:built|did|made|wrote|shipped|tested)\b",                        # 100% precision
    r"\bwe\s+(?:built|did|wrote|shipped|tested|fixed|added|made)\b",                        # 67% precision
    r"\bwe\s+(?:are|were)\s+(?:having|doing|building|talking)\b",                           # 67% precision
    r"\bwe\s+(?:have|had|keep)\s+been\s+(?:discussing|talking\s+about|going\s+over)\b",  # R2 fix 2026-06-23: narrowed from bare 'we have been' (57% precision; 'working on X' is operational not recall)
    r"\bin\s+(?:the\s+)?(?:last|prior)\s+session\b",                                        # 100% precision
    r"\bwhat\s+was\s+the\s+last\s+(?:thing|message|prompt)\b",                              # 50% precision (acceptable)
    # Added 2026-06-08 (C1): the EXACT phrases Nick uses to demand grounding, which the trigger
    # was missing. Telemetry proof: this session fired 6x and MISSED the turns where he said
    # "go check the history" / "verify it in the brain" / "look at the brain" — the highest-value
    # recall demands went silent. These are high-precision (an explicit instruction to consult history).
    r"\b(?:look\s+at|check|query|search|go\s+to|consult|read)\s+(?:the\s+)?brain\b",         # "look at the brain", "check the brain"
    r"\bin\s+the\s+brain\b",                                                                  # "verify it in the brain"
    r"\bcheck\s+the\s+history\b",                                                             # "go check the history"
    r"\bground\s+(?:everything|it|this|that|your|the|in)\b",                                  # "ground everything", "ground it in history"
    r"\bgo\s+(?:look|check|verify|see)\b.{0,40}\b(?:brain|history|memory|what\s+we|last\s+session|prior\s+session|we\s+(?:did|built|decided|discussed))\b",  # R1 fix 2026-06-23: require recall context (was bare 'go look', fired on 'go look at my contacts')
    r"\b(?:addressed|talked\s+about|discussed|covered|done)\s+(?:it|this|that)\s+before\b",   # "have we addressed this before"
    r"\bhave\s+we\s+(?:talked|discussed|addressed|done|covered|built)\b",                     # "have we talked about / addressed X"
    r"\bwhat\s+(?:and\s+the\s+)?why\b",                                                       # "the what and the why" — Nick's grounding phrase
]


# Added 2026-06-27 (Phase 4, theme A — the #1 frustration class, ~22 corpus fires). The EXACT
# recall-demand phrases Nick uses that matched NONE of the patterns above — e.g.
# "pull it up in core os like i said", "you have a full history on her", "from the begining".
# SHADOW MODE: these INJECT the recall reminder (pure upside) and get logged to
# .claude/state/.recall-demand-shadow.log, but do NOT set the blocking .recall-required marker
# yet — so they can't false-block. Promote into PAST_CONVERSATION_MARKERS (which sets the marker)
# once the shadow log shows clean, high-precision firing.
RECALL_DEMAND_MARKERS = [
    r"\bpull\s+(?:it|that|this|them)\s+up\b",            # "pull it up [in core os]"
    r"\blike\s+i\s+(?:said|told\s+you|mentioned)\b",     # "like i said", "like i told you"
    r"\bas\s+i\s+(?:said|told\s+you|mentioned)\b",       # "as i said"
    r"\bi\s+already\s+(?:told|said|mentioned|gave)\b",   # "i already told you"
    r"\bfrom\s+the\s+(?:beginning|start)\b",             # "from the beginning"
    r"\byou\s+(?:should|already)\s+know\b",              # "you should/already know"
    r"\byou\s+know\s+(?:this|that|the|about)\b",         # "you know this"
    r"\byou\s+have\s+(?:a\s+)?(?:full\s+)?history\b",    # "you have a full history on her"
]


STRATEGIC_CONTEXT_MARKERS = [
    r"\bco[\s-]?founders?\b",
    r"\bteam[\s-]?building\b",
    r"\baccelerator\b",
    r"\bintros?\b",
]


# Added 2026-05-24: assessment / why / state-class prompts. These ask Core to
# judge state, history, or "why we built X" — exactly the prompts where answering
# from memory instead of the brain produced confident-but-wrong assessments
# (see honest-system-assessment-2026-05-24.md: a whole assessment was written
# from file metrics, then reversed once the brain was consulted). Firing recall
# on these makes brain-first the default for the highest-risk question class.
ASSESSMENT_MARKERS = [
    r"\bwhere\s+(?:are\s+we|do\s+we\s+stand|we'?re\s+at|are\s+we\s+at)\b",
    r"\bwhy\s+(?:did|do|does|are|is|have|would)\s+we\b",
    r"\bwhy\s+is\s+(?:this|that|it|the|everything)\b",
    r"\btell\s+me\s+(?:where|what|why|how|the\s+state|honestly)\b",
    r"\b(?:current\s+state|state\s+of\s+(?:the\s+)?(?:system|things|core))\b",
    r"\b(?:assess|assessment|audit|evaluate|evaluation|re-?assess)\b",
    r"\bhow\s+(?:are\s+we|is\s+(?:it|this|that)\s+(?:doing|going|working))\b",
    r"\b(?:go|dive)\s+deep\b",
    r"\breally\s+tell\s+me\b",
    r"\bwhat\s+(?:did|have)\s+we\s+(?:build|built|do|done|ship|shipped)\b",
]


# Stems we never treat as person/project triggers (false-positive guard)
SKIP_SLUGS = {"other-people", "readme", "archive", "shared", "_template"}


# ---------------------------------------------------------------------------
# 2026-07-30, master-plan Phase 0.1 — non-referent name matches
#
# The docstring on detect() said "a name is a referent wherever it appears, quoted or not",
# and that is right about QUOTING and wrong about everything else. Graded against 353 real
# prompts (compaction summaries and /context output excluded — neither is a prompt), 11 of
# 34 person-slug matches were not references to the person at all:
#
#   file path      "briar.md (x2) -> a life-Core example used to illustrate"    5   discussing the FILE
#   pasted UI      "Opus 4.8 with high effort - Claude Max   ~/AI Projects/"    4   a statusline banner
#   product tier   "i just upgraded to the 20X max plan"                        1   a subscription
#   idiom          "take efficiency and quality of wirk to the max"             1   an adverb
#
# 32% of the leg, and each fire costs a full recall advisory injected into that turn plus a
# .recall-required marker that then gates the response. Pure cost, no possible benefit —
# the strictly-dominated class the plan spends first.
#
# The discriminator is the span's IMMEDIATE neighbours, never the slug, which keeps this a
# superset test (plan 3.3): every true positive in the corpus survives, because none of them
# sits next to an extension, a brand word, a product noun, or a lowercase determiner.
# "Maintain the Dana Whitfield relationship" is exactly why the determiner rule also requires
# the match to be lowercase — a capitalised name after "the" is still a name.
# ---------------------------------------------------------------------------

# Immediately BEFORE the match. Lowercase-only cases carry the `lower_only` flag.
_NOT_REFERENT_BEFORE = [
    (re.compile(r"\b(?:the|a|an|this|that)\s+$", re.I), True,  "determiner"),
    (re.compile(r"\b(?:claude|chatgpt|openai|gpt|anthropic|codex)\s+$", re.I), False, "brand"),
    (re.compile(r"[/\\]$"), False, "path-segment"),
]

# Immediately AFTER the match. Same lower_only convention as above: "max out" is a verb,
# "Jordan out-ran everyone" is not something anyone writes, and requiring lowercase means a
# capitalised name is never dropped by this rule.
_NOT_REFERENT_AFTER = [
    (re.compile(r"^\.(?:md|py|sh|json|ya?ml|txt|log|sql|ts|js)\b"), False, "file-extension"),
    (re.compile(r"^\s+(?:plan|tier|mode|subscription|quota|window)\b", re.I), False, "product-noun"),
    (re.compile(r"^\s+(?:out|ed)\b"), True, "verb-particle"),
]



def _hooklog():
    """hooklog.emit prints the payload AND records tokens_injected. Fail-open to a bare print."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog
        return hooklog
    except Exception:
        return None

def _non_referent(text, start, end):
    """Why this name match is not a reference to the named thing — or None if it is one."""
    before, after = text[max(0, start - 24):start], text[end:end + 24]
    matched_lower = text[start:end].islower()
    for rx, lower_only, why in _NOT_REFERENT_BEFORE:
        if rx.search(before) and (matched_lower or not lower_only):
            return why
    for rx, lower_only, why in _NOT_REFERENT_AFTER:
        if rx.search(after) and (matched_lower or not lower_only):
            return why
    return None


def _referent_hit(rx, text, path_only=False):
    """True when `rx` matches `text` somewhere that is a real reference.

    path_only restricts the check to the file-path shapes, which are wrong for any slug;
    the determiner/product shapes are person-specific and would eat legitimate project
    mentions ("the core repo", "core mode").
    """
    for m in rx.finditer(text):
        why = _non_referent(text, m.start(), m.end())
        if why is None or (path_only and why not in ("file-extension", "path-segment")):
            return True
    return False


def _person_patterns():
    patterns = []
    if not RELATIONSHIPS.is_dir():
        return patterns
    for entry in RELATIONSHIPS.iterdir():
        slug = entry.stem if entry.is_file() else entry.name
        if slug.lower() in SKIP_SLUGS or slug.startswith("."):
            continue
        patterns.append((slug, re.compile(rf"\b{re.escape(slug)}\b", re.I)))
    return patterns


def _project_patterns():
    patterns = []
    if not PROJECTS.is_dir():
        return patterns
    for entry in PROJECTS.iterdir():
        slug = entry.stem if entry.is_file() else entry.name
        if slug.lower() in SKIP_SLUGS or slug.startswith("."):
            continue
        if slug == "core":
            # Per tasks/research/brain-recall-pattern-audit-2026-05-17.md: bare `\bcore\b`
            # had 59% FP rate against operational prompts (Nick uses "core" as the system name).
            # Require a qualifier word — keeps the high-value "core repo/engine/etc" mentions,
            # drops the low-value "core is broken / ship core" operational uses.
            patterns.append((slug, re.compile(
                # 'brain' deliberately excluded — "core brain" is the core-brain slug, not core.
                r"\bcore\s+(?:repo|engine|instance|nick|memory|system|directory|template|architecture|pipeline|primitive)s?\b",
                re.I,
            )))
        elif slug == "core-brain":
            # Same logic as `core` — bare "core brain" / "core-brain" fires on operational
            # prompts ("core brain isnt a repo", "what's in core-brain"). Require qualifier.
            # 2026-05-17 audit measured 83% FP rate on bare match.
            patterns.append((slug, re.compile(
                r"\bcore[\s-]+brain\s+(?:repo|pipeline|extraction|graph|database|vault|primitive|architecture|index|merge|cluster|community|node|edge)s?\b",
                re.I,
            )))
        else:
            pattern = re.escape(slug).replace(r"\-", r"[\s-]+")
            patterns.append((slug, re.compile(rf"\b{pattern}\b", re.I)))
    return patterns


# Brain-sourced names (2026-07-15): the local memory/{relationships,projects} folders are
# THIN on lightly-used peer Cores, so the trigger under-fired there (recall was ~dead on
# business/school/finance/ops while firing 212x on life). Pull this Core's high-signal
# entity names (kind Project + Entity = its projects + people/orgs) straight from corebrain
# so recall fires on peer-relevant references regardless of local-memory sparsity. Cached
# daily → the per-prompt hot path only reads a small JSON file; the brain query runs once/day.
# Fail-open at every step: any error → fall back to the local-only patterns (prior behavior).
_BRAIN_NAME_STOP = {"core", "core-brain", "nick", "brain", "the brain", "core os"}


def _org_id():
    # IDENTITY FIRST — this had the precedence backwards. Everywhere else in the tree identity wins
    # over CORE_ORG_ID (2026-08-05, _env.get_org_id), because a session env var leaked across a `cd`
    # into another Core is the common failure and the env is the thing that lies. This hook read the
    # env first, so a leaked value silently pointed recall at another seat's partition.
    # Stays fail-OPEN (returns None -> local-only patterns), unlike the dispatcher which fails closed.
    try:
        import json as _j
        oid = _j.loads((INSTANCE_ROOT / ".claude" / "identity.json").read_text()).get("org_id")
        if oid:
            return str(oid)
    except Exception:
        pass
    v = os.environ.get("CORE_ORG_ID")
    return v if (v and v.isdigit()) else None


def _brain_names():
    """This Core's Project+Entity names from corebrain, cached 24h. Fail-open → []."""
    cache = INSTANCE_ROOT / ".claude" / "state" / ".brain-recall-names.json"
    import json as _j
    import time as _t
    try:
        c = _j.loads(cache.read_text())
        if _t.time() - c.get("generated", 0) < 86400:
            return c.get("names", [])
    except Exception:
        pass
    org = _org_id()
    if not org:
        return []
    names = []
    # STRICT clean filter (2026-07-15): the brain's raw Project/Entity names are noisy
    # (file paths, code fragments extracted from sessions). Only human-readable multi-word
    # proper-noun names survive: alphanumeric+space, contains a space, 5-39 chars. For
    # business this cuts 1633 noisy rows → ~11 clean project names (Career Hunter, Quote
    # Scanner, …). High-signal + bounded → no per-prompt FP explosion.
    # kind='Project' ONLY — the clean, high-signal set (6-49/org, all real project names).
    # kind='Entity' was tested and REJECTED: it's noisy (extracted Incident/Rule/Code  # privacy-ok: generic engineering vocabulary
    # phrases like "Rule Honest Uncertainty In Ai Prompts") that would false-fire the
    # trigger — the over-firing we're deliberately avoiding. People are already covered by
    # _person_patterns (local memory/relationships).
    sql = (f"SELECT DISTINCT name FROM entities WHERE org_id={int(org)} "
           + "AND kind='Project' "
           + "AND name ~ '^[A-Za-z][A-Za-z0-9 ]{4,38}$' AND name LIKE '% %' "
           + "ORDER BY name LIMIT 100")
    try:
        import subprocess as _sp
        out = _sp.run(["psql", "-d", os.environ.get("COREBRAIN_DB", "corebrain"), "-tA", "-c", sql],
                      capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            for n in out.stdout.splitlines():
                n = n.strip()
                if n and n.lower() not in _BRAIN_NAME_STOP and re.search(r"[A-Za-z]", n):
                    names.append(n)
    except Exception:
        return []
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(_j.dumps({"generated": _t.time(), "names": names}))
    except Exception:
        pass
    return names


def _brain_patterns():
    pats = []
    for n in _brain_names():
        # multi-word/hyphenated OR length>=5 to keep signal high (single short words = FP risk)
        if len(n) < 5 and not re.search(r"[\s-]", n):
            continue
        esc = re.escape(n).replace(r"\ ", r"\s+").replace(r"\-", r"[\s-]+")
        try:
            pats.append((n, re.compile(rf"\b{esc}\b", re.I)))
        except re.error:
            continue
    return pats


# ── PERFORM THE RECALL (2026-07-30, master plan Phase 1.2) ────────────────────────────────
#
# This hook told me to recall. bin/grade-gate.py measures its lift at +1% over a 58% baseline —
# i.e. it barely changes what happens next, because an instruction to go look something up is
# only as good as the follow-through. The plan's third shape applies: make the good outcome the
# default rather than requesting it. The hook has a database connection available and I do not,
# not without spending a turn on it.
#
# WHY A THIN QUERY RATHER THAN scheduling/brain-pg/query.py. Measured on this machine:
#
#     query.py, full hybrid (vector+fts+graph+rerank)   5.5 - 8.6 s
#     query.py, use_vector=False, everything else off   2.19 s
#     this function                                     0.05 s
#
# The 2.2s floor is not query time — leg_fts run inside an open session is instant. It is
# `import query` pulling numpy / voyage / flashrank at module scope no matter which legs are
# disabled. The registered hook timeout is 5s, so query.py would have been a coin flip on
# timing out AND would have added seconds of dead latency to 35% of Nick's prompts. A hook on
# the prompt hot path has to be cheap or it is not worth having.
#
# What is lost: the vector leg (semantic neighbours) and the rerank. What is kept is exactly the
# high-precision case this hook fires on — a literal named person or project. FTS on a proper
# noun is the right tool for that, and it is what the recall skill already documents as the
# fallback path.
#
# A NOTE ON --baseline: query.py's --baseline flag is advertised as "FTS+graph only" and is
# nothing of the kind. It is a benchmark path that shells out to grep over $CORE_BRAIN markdown
# and returns [] when that variable is unset. It looked like a fast FTS path for exactly as long
# as it took to read it.
_RECALL_TIMEOUT_S = 2
_RECALL_CAP_CHARS = 1000   # ~250 tok, inside the plan's 300-tok budget


def _perform_recall(query_text, org_id):
    """Top-3 brain entities for `query_text`, or None. Never raises, never blocks."""
    if not query_text or not org_id:
        return None
    try:
        import psycopg2
        # COREBRAIN_DB resolver (2026-08-31 fix) — was hardcoded "corebrain"; same resolver as
        # _env.connect_corebrain(). Fails safe either way (return None on connect error), but a
        # non-default install silently got NO recall here instead of degrading to grep-fallback
        # for the right reason.
        con = psycopg2.connect(dbname=os.environ.get("COREBRAIN_DB", "corebrain"),
                               connect_timeout=_RECALL_TIMEOUT_S)
    except Exception:
        return None
    try:
        with con:
            cur = con.cursor()
            cur.execute("SET LOCAL statement_timeout = %s", (_RECALL_TIMEOUT_S * 1000,))
            cur.execute("SELECT set_config('app.current_org_id', %s, true)", (str(org_id),))
            # OR, not AND. plainto_tsquery ANDs every term, so a bag of words from a prompt
            # ("lets work friction spine decide clustering") requires ALL of them and matches
            # nothing — which is exactly what it did on the first test. to_tsquery with `|`
            # ORs them and lets ts_rank do the work of preferring documents that hit more terms.
            # Terms are stripped to [A-Za-z0-9-] before interpolation so no tsquery operator can
            # come from the prompt.
            terms = [t for t in re.split(r"[^A-Za-z0-9-]+", query_text) if len(t) > 2][:12]
            if not terms:
                return None

            # NAME MATCH FIRST. FTS over compiled_truth_md has the same word-versus-name
            # ambiguity that Phase 0.1 fixed at the trigger: querying "max" returned "Job H
            # per-job max ($51.91) exceeds Grade 5 Final max" — the English word, ranked above
            # the person. Entities whose NAME contains the term are unambiguously about the
            # referent, so they lead; FTS fills any remaining slots.
            like = [f"%{t}%" for t in terms[:4]]
            cur.execute(
                """SELECT name, left(COALESCE(compiled_truth_md, ''), 220), 1.0
                     FROM entities
                    WHERE org_id = %s AND valid_until IS NULL
                      AND name ILIKE ANY(%s)
                    ORDER BY length(name) LIMIT 3""",
                (int(org_id), like))
            named = cur.fetchall() or []
            if len(named) >= 3:
                return named[:3]

            tsq = " | ".join(terms)
            cur.execute(
                """SELECT name, left(COALESCE(compiled_truth_md, ''), 220),
                          ts_rank(to_tsvector('english', COALESCE(compiled_truth_md, '')),
                                  to_tsquery('english', %s)) AS r
                     FROM entities
                    WHERE org_id = %s AND valid_until IS NULL
                      AND to_tsvector('english', COALESCE(compiled_truth_md, ''))
                          @@ to_tsquery('english', %s)
                    ORDER BY r DESC LIMIT 3""",
                (tsq, int(org_id), tsq))
            seen = {n for n, _t, _r in named}
            merged = named + [r for r in (cur.fetchall() or []) if r[0] not in seen]
            return merged[:3] or None
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _recall_query(person_hits, project_hits, prompt):
    """What to ask the brain — or None when there is nothing precise enough to ask.

    NAMED HITS ONLY, and that restriction is the finding rather than a shortcut. The first
    version fell back to a bag of content words from the prompt on marker-only fires. Tried
    against "lets work on the friction spine — what did we decided about clustering last time"
    it returned the claude-brain skill's own entity page and an Etsy listing note about Baby
    Bingo: 254 tokens of loosely-related material, injected on every marker fire.

    That is the fitness function answering directly. An OR query over a vague prompt ranks by
    how many common words a document happens to contain, which is close to ranking by document
    length. A named person or project is a proper noun, and FTS on a proper noun is precise —
    which is exactly the case this leg is good for and the case the recall skill documents.

    Marker-only fires keep the advisory instead. Telling me to recall is weak, but it is
    honestly weak; injecting confident-looking irrelevant context is worse than weak.
    """
    names = list(dict.fromkeys(list(person_hits) + list(project_hits)))
    return " ".join(names)[:120] if names else None

STATE_DIR = INSTANCE_ROOT / ".claude" / "state"
COOLDOWN_FIRES = 10  # suppress repeat fires within last N entries for same project_hits
TELEMETRY_CAP = 50   # keep most recent N entries in state file


def _telemetry_path(session_id: str) -> Path:
    return STATE_DIR / f".brain-recall-{session_id}.json"


def _load_telemetry(path: Path) -> dict:
    if not path.exists():
        return {"fires": []}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"fires": []}


def _save_telemetry(path: Path, data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass  # telemetry must never break the hook


def _should_suppress(state: dict, project_hits: list, person_hits: list) -> bool:
    """Suppress this fire's injection if same project+person fired in last
    COOLDOWN_FIRES entries AND no new person/project signal."""
    if not project_hits and not person_hits:
        return False
    recent = state.get("fires", [])[-COOLDOWN_FIRES:]
    for entry in recent:
        prior_projects = set(entry.get("project_hits", []))
        prior_persons = set(entry.get("person_hits", []))
        if set(project_hits).issubset(prior_projects) and set(person_hits).issubset(prior_persons):
            return True
    return False



# What this hook's injection ASKS FOR. bin/grade-gate.py scores a read-demanding advisory by
# REDUNDANCY rather than frequency: its injected advice is *recall the brain before answering*, so a fire whose next
# turn already recalled was advice Core did not need.
ADVICE = "read"


def _claimtext():
    """Shared mention discriminators (lib/claimtext.py) — see that file's header. Fail-open."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import claimtext
        return claimtext
    except Exception:
        return None


def detect(text):
    """(fired, detail) — would this prompt trigger a brain-recall reminder, before any
    session-level suppression? Suppression is stateful (a brain query already ran this session,
    cooldown on repeat project hits) and deliberately NOT modelled here: this is the RAW trigger
    surface, which is the thing bin/grade-gate.py needs to measure against the real prompt corpus.

    Marker matches route through the shared mention check — Nick writing *stop firing on
    "what did we decide"* is discussing the phrase, not asking for recall. Named person/project
    hits do NOT: a name is a referent wherever it appears, quoted or not.
    """
    text = text or ""
    if len(text) < 10:
        return False, {}
    ct = _claimtext()
    spans = ct.quoted_spans(text) if ct else None

    def _markers(markers):
        out = []
        for m in markers:
            for hit in re.finditer(m, text, re.I):
                if ct and ct.is_mention(text, hit.start(), hit.end(), spans):
                    continue
                out.append(m)
                break
        return out

    detail = {
        # Name hits route through _referent_hit (Phase 0.1) — see the block above
        # _person_patterns for the measured false-positive shapes it drops.
        "person": [s for s, p in _person_patterns() if _referent_hit(p, text)],
        "project": [s for s, p in _project_patterns() if _referent_hit(p, text, path_only=True)],
        "brain": [n for n, p in _brain_patterns() if _referent_hit(p, text, path_only=True)],
        "past": _markers(PAST_CONVERSATION_MARKERS),
        "strategic": _markers(STRATEGIC_CONTEXT_MARKERS),
        "assessment": _markers(ASSESSMENT_MARKERS),
    }
    return any(detail.values()), detail


# ── T11 COVERAGE: a proposal to change a primitive is a recall demand too ─────────────────────
#
# core-business measured the gap on its own corpus: of 284 turns that trigger casebook T11
# (primitive + propose), only 43 name a person or project — so 241, or 85%, were unreachable by
# this gate. T11 measures life at 2 brain queries in 165 such turns and business at 0 in 269. The
# mechanism was never weak; it was aimed at a different population than the item counting the
# failure.
#
# It also caught business this afternoon and it checked the marker afterwards: the recorded subject
# was a PROJECT while it was changing a probe fixture. THE GATE FIRED BY COINCIDENCE, and it had
# already written that up as coverage before verifying. That is why the subject below is the
# PRIMITIVE FILENAME and not a generic flag — the demanded query has to be about the thing being
# changed, or this becomes satisfiable by any brain read and gets muted.
_PRIM_PROPOSE = re.compile(
    r"\b(should|propose|I would|let's|we need to|recommend|fix|change|rewrite|add|remove)\b", re.I)
_PRIM_NAMED = re.compile(
    r"\b([a-z0-9][a-z0-9_-]{2,40}\.(?:py|sh|md|json))\b"
    r"|\b(pretooluse[- ]guard|sentinel[- ]approve|sentinel[- ]receipt|reply[- ]observer|"
    r"session[- ]presence|casebook[- ]run|trajectory[- ]gate|sync-(?:from|to)-baseline)\b", re.I)


# Text that is NOT a request from Nick: system notifications, injected hook context, skill files,
# and the autonomous loop's own prompts. Measured before shipping this widening: of 124 prompts in
# life's newest transcript the raw trigger fired on 26 (21%), and the hits were dominated by the
# loop's own tick text and by <task-notification> blocks — MACHINE TEXT NAMING PRIMITIVES, not a
# person asking for a change.
#
# core-business's warning was precisely this: widening the trigger is either the right price for
# the highest-rate real failure we have, or it is the WebFetch-gating mistake again — 85
# approve-then-rerun blocks with 0 real catches. A gate that fires on its own loop prompt is the
# muted version by construction, so the rate was measured and decomposed before shipping rather
# than after.
_NOT_A_REQUEST = (
    "AUTONOMOUS WORK TICK", "KEEP WORKING. Do not stop", "PROCEDURE THIS TICK",
    "Procedure each wake", "<task-notification>", "<system-reminder>",
    "UserPromptSubmit hook additional context", "SYSTEM NOTIFICATION - NOT USER INPUT",
    # SLASH-COMMAND BODIES AND HOOK FEEDBACK arrive as user turns and are the dominant false-fire
    # class once primitives enter the trigger. MEASURED on 330 real prompts: every one of the 12
    # hits was a command body (/core-si, /close-core, /deep-plan) or Stop-hook feedback — machine
    # text that ENUMERATES primitives rather than proposing a change to one. /close-core alone
    # yielded 35 filenames, and a demand naming 35 files is unsatisfiable by construction, which is
    # the muted version of this gate rather than a working one.
    "Stop hook feedback:", "Session Close", "Execute the full session close",
)


def _looks_like_command_body(text: str) -> bool:
    """A slash-command definition pasted in as a turn is documentation, not a request.

    They open with `# /name` and read like a spec — the /deep-plan body alone names five primitives
    while asking for nothing. Keyed on that opening rather than on a keyword list so a new command
    does not have to be added here to be recognised.
    """
    head = text.lstrip()[:400]
    return head.startswith("# /") or "\n# /" in text[:200]


def _primitive_subjects(text: str) -> list:
    """Named primitives in a turn that PROPOSES changing one. Empty unless both halves hold.

    Requires the proposal half so ordinary mentions of a filename do not raise a demand, and
    requires a NAMED primitive so the recorded subject is specific enough to bind a query to.
    """
    if not text or not _PRIM_PROPOSE.search(text):
        return []
    if any(k in text for k in _NOT_A_REQUEST) or _looks_like_command_body(text):
        return []          # machine text naming a primitive is not a proposal to change one
    # A TURN NAMING MANY PRIMITIVES IS AN INVENTORY, NOT A PROPOSAL. Measured: real proposals name
    # one or two files; the false fires named 5, 17 and 35. A demand carrying 35 subjects also
    # cannot be satisfied by any realistic query, so it would block work rather than redirect it.
    _MAX_SUBJECTS = 3
    out = set()
    for m in _PRIM_NAMED.finditer(text):
        hit = (m.group(1) or m.group(2) or "").strip().lower()
        if hit:
            out.add(hit.rsplit("/", 1)[-1])
    return sorted(out) if len(out) <= _MAX_SUBJECTS else []


def main():
    # Record that this hook RAN, matched or not. Without it the ledger can count FIRES but not
    # INVOCATIONS, so yield (fires/invocations) is not computable — and a gate that fires 4 times
    # out of 4 looks identical to one that fires 4 times out of 400. Added 2026-07-30 across every
    # instrumented hook that lacked it, so low-yield becomes a measurable verdict rather than a
    # guess. (The ledger could retire EXPENSIVE components but not cheap-and-useless ones; this is
    # the missing term.)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("brain-recall-trigger", "UserPromptSubmit")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    prompt = data.get("prompt") or data.get("user_message") or ""
    if not _is_user_text(prompt):
        return  # runtime-injected turn, not the user speaking

    if not prompt or len(prompt) < 10:
        return

    session_id = data.get("session_id") or data.get("sessionId") or "unknown"

    _fired, _d = detect(prompt)
    person_hits = _d.get("person", [])
    project_hits = _d.get("project", [])
    # Brain-sourced project/entity names (2026-07-15) — fires recall on peer-relevant
    # references even when THIS Core's local memory/ is thin. Merged into project_hits so
    # downstream telemetry/cooldown treat them identically.
    brain_hits = _d.get("brain", [])
    if brain_hits:
        project_hits = list(dict.fromkeys(project_hits + brain_hits))
    past_markers = _d.get("past", [])
    strategic_markers = _d.get("strategic", [])
    assessment_markers = _d.get("assessment", [])
    demand_markers = [m for m in RECALL_DEMAND_MARKERS if re.search(m, prompt, re.I)]  # SHADOW (Phase 4)

    # 2026-05-17: strategic_markers promoted to primary trigger per pattern audit
    # (cofounder/accelerator/team-building references almost always need recall).
    # 2026-05-24: assessment_markers added — see ASSESSMENT_MARKERS comment above.
    # PRIMITIVE PROPOSALS ARE A TRIGGER, NOT JUST A SUBJECT. Shipped 2026-08-10 morning as an
    # enrichment INSIDE `if triggered:` — so it decorated the subject list of a marker some OTHER
    # trigger had already caused, and a turn saying "I should fix this guard", naming no person and
    # no project, still did not fire. core-business read the line and refused the push over it
    # (#924): "The number moved; the coverage did not."
    #
    # THE MEASUREMENT I REPORTED WAS OF THE WRONG POPULATION. 21.0% -> 1.6% was the exclusion list's
    # effect on prompts that were ALREADY firing, and I presented it as a coverage result for T11's
    # 85% unreachable population. It was never a coverage measurement at all. That is worse than the
    # missing clause, because a wrong fix announces itself on the next run and a wrong measurement
    # does not.
    primitive_hits = _primitive_subjects(prompt)
    triggered = bool(person_hits or project_hits or past_markers or strategic_markers
                     or assessment_markers or primitive_hits)

    # 2026-07-27 (governance directive, tune): a MARKER-ONLY fire (past/strategic/
    # assessment phrasing, NO named person/project) exists to get the brain consulted
    # in a session where prior context is in play. Once a genuine brain query HAS run
    # this session (breadcrumb from recall-satisfied.py), re-firing on every past-tense
    # phrase is nagging — mid-session referents are structurally not in the brain until
    # close. Named hits and explicit recall-DEMANDS ("you should know") fire regardless.
    # Codex review 2026-07-27 (#3): explicit past-reference requests ("remember what we
    # discussed", "in a prior session", "last time") are ALSO exempt from suppression —
    # those point at genuinely-past content the brain does hold.
    _EXPLICIT_PAST_RX = (
        r"\b(?:remember|what\s+did\s+we|did\s+we\s+(?:ever|already)|"
        r"(?:in\s+a\s+|the\s+)?(?:prior|previous|past|last)\s+(?:session|conversation)|"
        r"last\s+(?:week|time|month)|go\s+look|check\s+(?:my\s+|the\s+)?(?:brain|memory))\b"
    )
    _breadcrumb_present = False
    try:  # Codex #6: breadcrumb check must fail open (bad session_id → treat as absent)
        _breadcrumb_present = (STATE_DIR / f".brain-queried-{session_id}").exists()
    except Exception:
        pass
    if (triggered and not person_hits and not project_hits and not demand_markers
            and not re.search(_EXPLICIT_PAST_RX, prompt, re.I)
            and _breadcrumb_present):
        triggered = False
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(INSTANCE_ROOT / ".claude" / "state" / "gate-fires.log", "a") as _gf:
                _ts = __import__("datetime").datetime.now().isoformat(timespec="seconds")
                _gf.write(f"{_ts}\tbrain-recall-trigger\tsuppress\tmarker-only+recall-already-ran\t{prompt[:80]!r}\n")
        except Exception:
            pass

    # 2026-06-27 Phase 4 SHADOW: demand_markers cause the INJECT (helpful reminder) but are
    # deliberately EXCLUDED from `triggered` so they do NOT set the blocking .recall-required
    # marker. Log each match so the shadow firing can be reviewed before promotion.
    if demand_markers:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(STATE_DIR / ".recall-demand-shadow.log", "a") as _sf:
                _ts = __import__("datetime").datetime.now().isoformat(timespec="seconds")
                _sf.write(f"{_ts}\twould_block={not triggered}\thits={len(demand_markers)}\t{prompt[:80]!r}\n")
        except Exception:
            pass
    inject = triggered or bool(demand_markers)

    # Proactive enforcement marker (contract-binding fix 1, live 2026-06-09):
    # recall-first-gate.py (PreToolUse) refuses mutating tools while this marker
    # exists — the turn's first move must be a read/recall. Per-prompt semantics:
    # set on trigger, cleared on a non-trigger prompt (and by any read via the
    # PostToolUse clearer recall-satisfied.py).
    required_marker = STATE_DIR / f".recall-required-{session_id}"
    try:
        if triggered:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            # RECORD WHAT RECALL WAS DEMANDED, not merely that it was.
            #
            # The marker held a bare timestamp, so recall-satisfied.py had nothing to bind against
            # and cleared it on ANY brain-namespace call. Querying the brain about ANYTHING
            # satisfied a demand about SOMETHING ELSE — and since the clearer never receives the
            # tool RESULT either, a zero-hit query cleared it identically to a real one. The
            # recall-first rule is the one Nick has corrected us on most, and it was satisfiable
            # by querying the brain for nothing.
            #
            # Subjects are the NAMED hits only (people, projects). A marker-only fire — past-tense
            # phrasing with no named subject — has no subject to bind to, and writing an empty list
            # is the honest record of that: the clearer then keeps its old any-read behaviour rather
            # than inventing a constraint.
            _subjects = sorted({str(x) for x in (list(person_hits or []) + list(project_hits or [])) if x})
            # A named primitive under proposal is a subject too — see _primitive_subjects above.
            # Recorded as the FILENAME so the satisfier's binding forces a query about that file.
            _subjects = sorted(set(_subjects) | set(primitive_hits))
            required_marker.write_text(__import__("json").dumps({
                "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                "subjects": _subjects,
            }))
        elif required_marker.exists():
            required_marker.unlink()
    except Exception:
        pass  # enforcement marker must never break the hook

    if not inject:
        return

    # ── Telemetry: always record the fire, even if we suppress the injection ──
    telemetry_path = _telemetry_path(session_id)
    state = _load_telemetry(telemetry_path)
    fire_entry = {
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "person_hits": person_hits,
        "project_hits": project_hits,
        "past_marker_count": len(past_markers),
        "strategic_marker_count": len(strategic_markers),
        "assessment_marker_count": len(assessment_markers),
        "prompt_snippet": prompt[:80],
        "suppressed": False,  # filled in below
    }

    suppressed = _should_suppress(state, project_hits, person_hits)
    fire_entry["suppressed"] = suppressed

    state.setdefault("fires", []).append(fire_entry)
    # Cap the state file size so it doesn't grow unbounded.
    state["fires"] = state["fires"][-TELEMETRY_CAP:]
    _save_telemetry(telemetry_path, state)

    # Uniform steering-surface telemetry (governance directive 2026-07-27).
    # Size-capped (Codex #5): >512KB → keep the newest 1000 lines.
    try:
        _gflog = INSTANCE_ROOT / ".claude" / "state" / "gate-fires.log"
        try:
            if _gflog.exists() and _gflog.stat().st_size > 512 * 1024:
                _tail = _gflog.read_text().splitlines(keepends=True)[-1000:]
                _gflog.write_text("".join(_tail))
        except Exception:
            pass
        with open(_gflog, "a") as _gf:
            _ts = __import__("datetime").datetime.now().isoformat(timespec="seconds")
            _act = "suppress" if suppressed else "fire"
            _det = f"persons={len(person_hits)},projects={len(project_hits)},markers={len(past_markers)+len(strategic_markers)+len(assessment_markers)}"
            _gf.write(f"{_ts}\tbrain-recall-trigger\t{_act}\t{_det}\t{prompt[:80]!r}\n")
    except Exception:
        pass

    if suppressed:
        return  # cooldown — don't emit additionalContext, but did record the fire

    parts = ["BRAIN RECALL TRIGGER detected:"]
    if person_hits:
        parts.append(f"  - Named person(s): {', '.join(person_hits)}")
    if project_hits:
        parts.append(f"  - Named project(s): {', '.join(project_hits)}")
    if past_markers:
        parts.append(f"  - Past-conversation marker(s) present")
    if strategic_markers:
        parts.append(f"  - Strategic context marker(s): {len(strategic_markers)}")
    if assessment_markers:
        parts.append(f"  - Assessment/why/state-class marker(s): {len(assessment_markers)} — recall the WHY/history before judging")
    if demand_markers:
        parts.append(f"  - Recall-DEMAND phrase(s): {len(demand_markers)} (e.g. 'pull it up', 'like i said', 'you should know') — {_U.name()} is telling you it's already in memory; recall NOW, don't answer from context")

    # Do the recall rather than asking for it. On success the results go in and the breadcrumb
    # is set, because recall genuinely HAS run this turn — that is what recall-satisfied.py
    # touches and what recall-gate reads, so performing it here closes the loop instead of
    # adding a second demand on top of the first.
    hits = _perform_recall(_recall_query(person_hits, project_hits, prompt), _org_id())
    if hits:
        parts.append("")
        parts.append("BRAIN (top 3, FTS over this Core's entities — already run, no need to re-query):")
        budget = _RECALL_CAP_CHARS
        for name, truth, _rank in hits:
            line = f"  · {name}: {' '.join((truth or '').split())}"
            if len(line) > budget:
                line = line[:max(0, budget - 1)] + "…"
            if not line.strip("  ·"):
                continue
            parts.append(line)
            budget -= len(line)
            if budget <= 0:
                break
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / f".brain-queried-{session_id}").touch()
        except Exception:
            pass
    else:
        # No named hit to query precisely on, or Postgres unavailable. The advisory stands, so
        # coverage never drops below what it was before this leg became active.
        parts.append("")
        parts.append(
            "Recall the brain before answering (`claude-brain` skill) — don't synthesize from "
            "grep/Read alone when prior sessions are in play."
        )

    _hl = _hooklog()
    _ctx = "\n".join(parts)
    if _hl:
        _hl.emit("brain-recall-trigger", "UserPromptSubmit", _ctx, session=str(session_id))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": _ctx}}))


if __name__ == "__main__":
    main()
