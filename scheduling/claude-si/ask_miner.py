#!/usr/bin/env python3
"""ask_miner.py — Workstream 2 keystone: mine RECURRING SPECIFIC ASKS ("Nick keeps asking for X").

The breakthrough (2026-07-23): the signal isn't recoverable from raw corrections (frustration tone
dominates the embedding) — it needs an LLM to EXTRACT + CANONICALIZE the underlying ask, after which
identical-text clustering isolates it cleanly. So this is an EXTRACTION problem, not an embedding one.

Pipeline:
  extract_pending(org, n)  -> rows with a correction but no canonical_ask yet (feed to a Sonnet subagent;
                              the parent writes results back via cache_asks — LLM spend, Nick-approved).
  cache_asks(org, pairs)   -> store [{id, ask|None}] into pattern_observations.canonical_ask.
  recurring_asks(org, k)   -> cluster cached asks by canonical text; return asks with support >= k.
  ask_cases(org, k)        -> recurring asks as friction-case-shaped dicts for the router (feeds the spine).

canonical_ask is cached per row, so each correction is extracted ONCE; re-runs only extract new rows.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "brain-pg"))
from _env import connect_corebrain, connect_or_skip, get_org_id  # noqa: E402
sys.path.insert(0, str(HERE.parent.parent / ".claude" / "hooks" / "lib"))
import coreuser as _U  # noqa: E402  — operator name from identity.json, never hardcoded


def extract_pending(org: int, n: int = 200) -> list[dict]:
    # The close-path entry (close-core step 2d). No database -> named skip + empty backlog, not a
    # traceback that kills the close chain. The other functions here run inside a session that
    # already proved the DB is up, and keep connect_corebrain().
    con = connect_or_skip("ASK-MINER")
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, COALESCE(prompt_text,''), correction_text FROM pattern_observations "
            "WHERE org_id=%s AND correction_text IS NOT NULL AND canonical_ask IS NULL AND excluded_reason IS NULL "
            "ORDER BY created_at DESC LIMIT %s", (org, n))
        return [{"id": r[0], "prompt": (r[1] or "")[:400], "correction": (r[2] or "")[:400]}
                for r in cur.fetchall()]
    finally:
        con.close()


_MERGE_STOP = set(
    "the a an and or to of for in on with by is are be do dont don't not that this it its you your "
    "i me my we us so then than when what which how all every each any some more before after into "
    "from at as if but just only also can should would could".split())
# Merge threshold. Chosen from the live distribution rather than picked: pairwise overlap across the
# 30 real asks has a single value at 0.45 and then a gap to 0.18, so the decision sits inside a clear
# cliff instead of on a slope. Loosening to 0.15 starts producing wrong merges — it joins "surface the
# full backlog in one pass" to "sync all cores' shared data in one pass", which share only "one pass"
# and are unrelated asks. Deliberately conservative: a missed merge costs support, a wrong merge
# corrupts the ask itself.
MERGE_JACCARD = 0.20



_BASE_CACHE: tuple | None = None


def _base_rates() -> tuple[dict, int]:
    """How often each term appears across ALL of Nick's prompts — the denominator for contrast.

    Cached per process: the miner scores many asks in one run and this is one query. Returns
    ({}, 0) on any failure, and the caller falls back to raw-frequency ranking rather than minting
    nothing — a DB outage must not silently look like a scoring decision.

    Counts DISTINCT PROMPTS containing the term, not total occurrences. A word repeated five times
    in one prompt is one piece of evidence about that prompt, not five.
    """
    global _BASE_CACHE
    if _BASE_CACHE is not None:
        return _BASE_CACHE
    counts: dict[str, int] = {}
    n = 0
    try:
        con = connect_corebrain()
        try:
            cur = con.cursor()
            cur.execute("SELECT prompt_text FROM pattern_observations "
                        "WHERE org_id = current_setting('app.current_org_id', true)::bigint "
                        "AND prompt_text IS NOT NULL AND prompt_text <> ''")
            for (txt,) in cur.fetchall():
                n += 1
                for t in set(_terms(txt)):
                    counts[t] = counts.get(t, 0) + 1
        finally:
            con.close()
    except Exception:
        _BASE_CACHE = ({}, 0)
        return _BASE_CACHE
    _BASE_CACHE = (counts, n)
    return _BASE_CACHE


def _merge_toks(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9'-]{3,}", (s or "").lower()) if w not in _MERGE_STOP}


def merge_similar(asks: list[dict], threshold: float = MERGE_JACCARD) -> list[dict]:
    """Merge asks that are the same directive worded differently.

    Clusters are keyed on exact canonical_ask text, so one recurring directive expressed several ways
    reads as several weak asks and can fall under the support gate entirely. Measured 2026-07-27: the
    model-collaboration directive held 17 moments spread across 5 phrasings, none dominant, while Nick
    described it as something he asks for constantly.

    Lexical overlap, not embeddings — 30 short normalised imperatives do not need a vector store or an
    external API, and a deterministic rule is auditable. This is a conservative FIRST pass, not general
    semantic clustering: it currently finds one merge. If the corpus grows phrasings that share meaning
    without sharing words, this is the seam where embeddings would go.

    The surviving label is the highest-support phrasing; the rest are recorded in `merged_from` so a
    merge is always visible rather than silently rewriting the ask.
    """
    ordered = sorted(asks, key=lambda a: -a.get("support", 0))
    out: list[dict] = []
    for cand in ordered:
        ct = _merge_toks(cand["ask"])
        placed = False
        for keep in out:
            kt = _merge_toks(keep["ask"])
            if not ct or not kt:
                continue
            if len(ct & kt) / len(ct | kt) >= threshold:
                keep["support"] = keep.get("support", 0) + cand.get("support", 0)
                keep["rows"] = keep.get("rows", 0) + cand.get("rows", 0)
                keep["member_ids"] = list(keep.get("member_ids", [])) + list(cand.get("member_ids", []))
                # LAST_SEEN MUST TAKE THE MAX (2026-08-20). This merged support, rows and
                # member_ids and silently left `last_seen` at the KEEPER's value. `ordered` is
                # sorted by support, so the biggest cluster wins the slot — and if a smaller
                # sibling merged into it carries a NEWER date, the merged ask inherits the older
                # one and reads as stale.
                #
                # Measured on life the day this was found: EVERY high-frustration ask reported
                # `still_recurring=False` against a 14-day cutoff of 2026-08-06, while the corpus
                # itself held rows dated 08-16 and 08-17. The loop was treating live, actively
                # recurring problems as solved.
                #
                # `_still_recurring` gates two things — the rule-coverage bypass (a documented rule
                # only counts as coverage while the ask stops recurring) and the work-moment
                # escalation. Both fail in the SAME direction on a stale date: the loop concludes
                # the existing handling is working and builds nothing. A merged cluster is as recent
                # as its most recent member; anything else discards the evidence that merging it
                # was supposed to add.
                if str(cand.get("last_seen") or "") > str(keep.get("last_seen") or ""):
                    keep["last_seen"] = cand.get("last_seen")
                keep.setdefault("merged_from", []).append(
                    {"ask": cand["ask"], "support": cand.get("support", 0)})
                placed = True
                break
        if not placed:
            out.append(dict(cand))
    return sorted(out, key=lambda a: -a.get("support", 0))


def type_pending(org: int, min_support: int = 3) -> list[dict]:
    """Recurring asks that have a canonical_ask but no cached shape label yet.

    Separate from extract_pending() because canonical_ask was extracted before the type vocabulary
    existed — this backfills without re-extracting text that is already correct. Cluster-level (one
    judgment per distinct ask, applied to every member row) so the LLM sees each ask once, not once
    per observation."""
    con = connect_corebrain()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT canonical_ask, array_agg(id), count(*) FROM pattern_observations "
            "WHERE org_id=%s AND canonical_ask IS NOT NULL AND canonical_ask <> '' AND excluded_reason IS NULL "
            "GROUP BY canonical_ask "
            "HAVING count(*) >= %s AND count(canonical_ask_type) = 0 "
            "ORDER BY count(*) DESC", (org, min_support))
        return [{"ask": r[0], "ids": r[1], "support": r[2]} for r in cur.fetchall()]
    finally:
        con.close()


ASK_TYPES = ("constraint", "procedure", "none")

# T021's explicit marker. Deliberately a sentence, not a code: excluded_reason is read by humans
# reviewing what the corpus dropped, and the one value already in use ("machine-generated: harness
# self-prompt or injected notification, not the user") set that convention. A row carrying this is
# skipped for the same reason canonical_ask='' skipped it, but is now a queryable set — a better
# extractor can re-offer exactly these rows without touching anything else.
NO_ASK_REASON = "miner: no canonical ask derivable from this correction"
"""CLOSED extraction vocabulary describing the SHAPE OF AN ASK — deliberately NOT the artifact
type vocabulary. An ask is *procedural* in shape; the artifact built from it is a `hooked_skill`.
A blanket rename on 2026-07-27 conflated the two and set this to "hooked_skill", which the DB
CHECK (constraint|procedure|none) would have rejected on the next extraction.

`constraint` = a rule about HOW to act ("verify before claiming") →
inject contract. `procedure` = a repeatable thing to DO ("keep the diagram in sync with the system") →
gated procedure artifact. Both are inject-mode with identical blast radius, so a misroute is cheap.
`enforcement` is deliberately absent and must never be added: block-mode stays reachable only through
artifact_typer.ORACLE_CATALOG, which requires code + a locked equivalence test, never a data-only change."""


def cache_asks(org: int, pairs: list[dict]) -> int:
    """Store extraction results. pairs = [{id, ask, type?}] where ask is a canonical string or None,
    and type is one of ASK_TYPES. A None ask is cached as '' so the row is not re-extracted forever.
    An absent/unrecognized type is stored as NULL — the router then falls back to its default rather
    than trusting an out-of-vocabulary label (closed-set validated here AND by a DB CHECK)."""
    con = connect_corebrain()
    try:
        cur = con.cursor()
        n = 0
        for p in pairs:
            ask = p.get("ask")
            # NORMALISE before caching. Clusters are grouped by exact string, so two extractions of
            # the same ask differing only in capitalisation or whitespace split into two weak
            # clusters. Observed live 2026-07-27: "use Codex alongside Core…" and "use codex
            # alongside core…" sat as separate rows of 2 and 5, hiding a single ask of 7.
            if ask:
                ask = " ".join(str(ask).split()).strip().rstrip(".").lower()
            t = p.get("type")
            t = t if t in ASK_TYPES else None
            # ARITY. `type` is the extractor's opinion; `steps` is the evidence for it. Recording
            # both makes the label falsifiable — an ask CLAIMED procedural that decomposes into
            # fewer than two ordered steps is mislabelled, and route_type can say so instead of
            # trusting the word. Bounded and sanitised here as well as by the DB CHECK, because a
            # step list is LLM output and everything else from that source is bounded too.
            steps = p.get("steps")
            if isinstance(steps, list):
                steps = [" ".join(str(s).split())[:200] for s in steps if str(s).strip()][:12]
            else:
                steps = None
            if not ask and t == "none":
                # EXTRACTED, AND THE ANSWER WAS "NO DURABLE INSTRUCTION". THIS IS A SUCCESS.
                #
                # The extractor looked and reported `none` — the correct answer for MOST corrections
                # and the designed majority (587 of life's rows, 52 of business's, 39 of school's).
                # It is NOT a stranding and must never be attributed to a miner failure.
                #
                # I got this wrong in the first version of T021 and rewrote 624 of life's rows,
                # 587 of them typed 'none', as "miner: no canonical ask derivable". core-business
                # refused to run the same migration on its 52 and told me to check my own seat —
                # correctly, and it had my own #1111 verbatim to argue with: "'' does not mean junk.
                # It means EXTRACTED, and the honest answer is: no durable instruction here."
                #
                # Reversible only because canonical_ask_type was never touched, which was luck
                # rather than design.
                cur.execute(
                    "UPDATE pattern_observations SET canonical_ask='', canonical_ask_type=%s "
                    "WHERE id=%s AND org_id=%s",
                    (t, p["id"], org))
            elif not ask:
                # NO ASK AND NO TYPE -> a genuine failure, and the only true stranding (T021).
                # 37 rows on life, 4 on finance, 0 everywhere else — the whole fleet-wide defect is
                # 41 rows, not the 239 I first claimed. The other 198 were the designed state.
                #
                # This wrote canonical_ask='' and the intent was right: stop re-offering a row the
                # extractor has already examined and found nothing in, so the LLM does not pay for
                # it every run. The ENCODING was the defect, in three compounding ways.
                #
                #   1. STRANDED. The work queue at :38 selects `canonical_ask IS NULL`. An
                #      empty-string row is not NULL, so it is never offered again — not by this
                #      miner, and not by a better one later. 239 rows fleet-wide were in that state
                #      (life 133, business 52, school 39, finance 10, ops 5), permanently
                #      unlabellable and permanently unusable.
                #   2. READS AS LABELLED. `canonical_ask IS NOT NULL` is TRUE for ''. The consumers
                #      happen to guard with `<> ''` (:113, :212, measure-contract-fitness:465) — but
                #      any new reader that does the obvious thing is wrong, and one did: my own
                #      corpus-readiness reported "life: 0 unlabelled" hours ago when the real figure
                #      was 133.
                #   3. NO REASON, NO AUDIT. '' records that something was skipped and nothing about
                #      why or when. It cannot be reviewed, counted by cause, or reversed selectively.
                #
                # excluded_reason already exists, is already the exclusion filter every consumer
                # applies, and already carries one explicit human-readable value. Using it means the
                # rows stay skipped, stay cheap, and become a QUERYABLE SET a future extractor can
                # deliberately re-offer.
                cur.execute(
                    "UPDATE pattern_observations SET excluded_reason=%s, excluded_at=now() "
                    "WHERE id=%s AND org_id=%s AND excluded_reason IS NULL",
                    (NO_ASK_REASON, p["id"], org))
            else:
                cur.execute(
                    "UPDATE pattern_observations SET canonical_ask=%s, canonical_ask_type=%s, "
                    "canonical_ask_steps=%s "
                    "WHERE id=%s AND org_id=%s",
                    (ask, t, json.dumps(steps) if steps else None, p["id"], org))
            n += cur.rowcount
        con.commit()
        # WARN WHEN THE WRITE DID NOT LAND. 2026-08-12: life called this for orgs 1, 3, 4 and 5 and
        # it printed "cached 23 / cached 55" for seats whose rows never changed — because WRITES are
        # RLS-scoped to app.current_org_id (policy pattern_observations_update, USING and WITH CHECK
        # both `org_id = current_setting('app.current_org_id')`). A non-writer org silently updates
        # zero rows.
        #
        # This function was HONEST: it returned cur.rowcount and the caller (me) discarded it. The
        # instrument reported correctly and nobody read the report — a distinct failure from an
        # instrument that lies, and easier to repeat, because every future caller has to remember.
        # So the warning lives HERE, once, rather than in each caller's discipline.
        #
        # It is also the correct architecture rather than a limitation to route around: each Core
        # labels its OWN corpus. life cannot mine school's rows and should not be able to.
        if n < len(pairs):
            print(f"[ask_miner] cache_asks(org={org}): {n} of {len(pairs)} row(s) updated. "
                  f"Writes are RLS-scoped to app.current_org_id — a Core can only label its OWN "
                  f"corpus. Run this from the org-{org} seat.", file=sys.stderr)
        return n
    finally:
        con.close()


def recurring_asks(org: int, min_support: int = 3) -> list[dict]:
    con = connect_corebrain()
    try:
        cur = con.cursor()
        # SUPPORT COUNTS DISTINCT MOMENTS, not rows. Re-mining and multi-pass extraction write the
        # same correction text on the same day several times, and counting rows inflated support on
        # 10 of 14 asks (measured 2026-07-27) — worst case 5 rows for 2 real moments. Support drives
        # the min_support gate, so row-counting was minting artifacts for asks that never met the
        # bar. measure-contract-fitness.py already dedupes this way; this aligns the miner to it.
        #
        # mode() = the MAJORITY extracted type across the cluster. One stray extraction can't flip an
        # ask's shape; it takes a majority of independently-extracted rows to say "hooked_skill".
        cur.execute(
            "SELECT canonical_ask, array_agg(id), max(session_date), "
            "       mode() WITHIN GROUP (ORDER BY canonical_ask_type), "
            "       max(coalesce(jsonb_array_length(canonical_ask_steps),0)) AS steps, "
            "       count(DISTINCT (COALESCE(correction_text, prompt_text), "
            "                       COALESCE(session_date, created_at::date))) AS moments "
            "FROM pattern_observations "
            "WHERE org_id=%s AND canonical_ask IS NOT NULL AND canonical_ask <> '' AND excluded_reason IS NULL "
            "GROUP BY canonical_ask "
            "HAVING count(DISTINCT (COALESCE(correction_text, prompt_text), "
            "                       COALESCE(session_date, created_at::date))) >= %s "
            "ORDER BY moments DESC", (org, min_support))
        return [{"ask": r[0], "support": r[5], "rows": len(r[1]), "member_ids": r[1],
                 "last_seen": str(r[2]) if r[2] else None,
                 "ask_type": r[3] if r[3] in ASK_TYPES else None,
                 "steps": r[4] or 0}
                for r in cur.fetchall()]
    finally:
        con.close()


_STOP = {"the", "and", "you", "your", "that", "this", "with", "for", "not", "core", "before",
         "into", "them", "its", "dont", "just", "when", "what", "instead", "than", "over", "across"}
_STOP |= {_U.name().lower()}  # operator's own name is as ubiquitous as "core" — never a distinguishing term


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zA-Z']{4,}", (text or "").lower()) if w not in _STOP]


def _rank_ask_terms(ask: str, member_prompts: list[str] | None = None) -> list[str]:
    """The scoring CORE of _trigger_from_ask, returning <=2 bare WORDS (no regex wrapping).

    EXTRACTED 2026-08-31 so friction_router's fc_-case lane can ground its triggers with the
    exact same validated selection this function earned through two reverts (see the history
    below) instead of re-deriving grounding logic under a different name — the "unify, don't
    add a second parallel mechanism" default. `_trigger_from_ask` below is now a thin wrapper:
    rank, then wrap in `\\b...\\b`. Every line of the actual algorithm is unchanged from before
    the split; only the wrap step moved out.

    Rewritten 2026-07-27. The previous version returned a single OR of the ask's 4 longest words,
    which failed the gate from both directions at once — measured on 5 real asks: three were rejected
    over-broad (9%, 4%, 14% of real prompts, against a 3% bar) and two never fired on their own
    positive example, because the words the extraction LLM chose for the canonical ask did not appear
    in the actual prompts that produced it.

    Both failures have the same root cause: terms drawn from a paraphrase, combined disjunctively.
    So: draw terms from the REAL member prompts (intersected with the ask), and combine them
    CONJUNCTIVELY — the same two-word specificity friction_router already uses. Conjunction is
    expressed as separate prompt_regex ops in the condition's `all` block, never as a lookahead:
    `_validate_regex` rejects lookaheads as ReDoS-prone.
    """
    ask_terms = set(_terms(ask))
    prompts = [p for p in (member_prompts or []) if p]
    if not ask_terms or not prompts:
        return []
    # Terms must satisfy BOTH conditions: present in the ask (semantic relevance) AND present in the
    # real prompts (groundedness, so an honest positive example exists).
    #
    # Requiring only groundedness was tried and REVERTED on 2026-07-27. It raised yield from 1/5 to
    # 4/5 by picking whatever was frequent in the cluster — producing triggers like
    # ("something","working") and ("session","<the user's own username>") that passed the gate purely by
    # being rare, while meaning nothing. That is Goodharting the gate: the metric improved and the
    # artifact got worse. A low yield of well-grounded triggers is the correct outcome; most asks
    # genuinely cannot produce one, and installing a meaningless trigger is worse than installing
    # nothing.
    counts: dict[str, int] = {}
    for p in prompts:
        for t in set(_terms(p)):
            if t in ask_terms:
                counts[t] = counts.get(t, 0) + 1
    # CAPPED AT 3 (2026-08-31, friction-router GAP C fix, judge-required change 4). This used to be
    # `max(1, len(prompts) // 2)` uncapped — fine for THIS function's only caller until now,
    # ask_cases(), which always hands it <=6 prompts (_member_prompts' own limit, ask_miner.py:560),
    # so need topped out at 3 regardless. It stops being fine the moment a caller passes MORE than 6:
    # friction_router's fc_-case lane grounds against every real sibling of an ask, and one live ask
    # ("verify state against the live source before claiming", fc_16358fc0) has 37. Uncapped, need
    # would be 18 — a bar no term not present in essentially every sibling could ever clear, which is
    # a large share of why the no-trigger-terms denial rate measured 92% of the funnel. Capping at 3
    # keeps the bar exactly where ask_cases() already validated it (3 corroborating moments), instead
    # of letting it scale up with cluster size and become stricter than the gate it feeds.
    # NO-OP FOR THE EXISTING CALLER: min(x, 3) == x whenever x <= 3, i.e. whenever len(prompts) <= 6
    # (6 // 2 == 3) — exactly ask_cases()'s own ceiling. Locked by
    # tests/test_trigger_grounding.py::test_need_cap_is_noop_at_or_below_six_prompts.
    need = max(1, min(len(prompts) // 2, 3))
    pool = [t for t, c in counts.items() if c >= need]

    # CONTRAST SCORING (2026-08-12, Phase 4). Rank by DISTINCTIVENESS, not raw frequency.
    #
    # This sorted by `-counts[w]` — how often a term appears in this cluster's own prompts — which
    # cannot tell "this word is about THIS ask" from "this word is common in everything Nick writes".
    # Measured against the 750-prompt base corpus:
    #
    #     session   105/750  (14.0%)     common — says nothing about which ask fired
    #     brain      84/750  (11.2%)     common
    #     clean      52/750  ( 6.9%)     common
    #     loose       7/750  ( 0.9%)     distinctive
    #     fable      12/750  ( 1.6%)     distinctive
    #     across      0/750  ( 0.0%)     NEVER APPEARS
    #
    # `across` is the proof. A live artifact requires `\bacross\b AND \bclean\b`, and Nick has never
    # written "across" in any prompt in the corpus — so that artifact is not merely narrow, it is
    # INCAPABLE OF FIRING, and raw-frequency ranking chose it anyway.
    #
    # Lift = (rate inside this cluster) / (rate across all prompts). A term frequent here AND rare
    # overall is evidence; a term frequent here because it is frequent everywhere is not. This is the
    # "distinctiveness x repetition" signal the 2026-07-27 research named and never built.
    #
    # A ZERO BASE RATE IS A HARD REJECT, not a high score. Dividing by a smoothed zero would rank an
    # unfireable term FIRST — the metric would be maximally confident about a word that can never
    # match. Refusing to install beats installing something provably inert.
    _base, _bn = _base_rates()
    if _base:
        pool = [t for t in pool if _base.get(t, 0) > 0]
        def _lift(w: str) -> float:
            here = counts[w] / max(len(prompts), 1)
            everywhere = _base.get(w, 0) / max(_bn, 1)
            return here / everywhere if everywhere > 0 else 0.0
        pool.sort(key=lambda w: (-_lift(w), -counts[w], -len(w)))
    else:
        # No corpus reachable -> fall back to the prior behaviour rather than mint nothing. Stated
        # so a silent DB outage cannot look like a scoring change.
        pool.sort(key=lambda w: (-counts[w], -len(w)))
    seen, uniq = set(), []
    for w in pool:
        if w not in seen:
            seen.add(w); uniq.append(w)
    top = uniq[:2]
    # A single term is not specific enough to clear the 3%-of-real-corpus bar — measured: the lone
    # surviving term "session" fired on 27/150 (18%). Two co-occurring terms or nothing; failing
    # closed here just means no artifact, which is the correct outcome for an unspecific ask.
    # BYTE-IDENTICAL TO PRE-SPLIT (judge-required change 4): this floor is the same `< 2` check
    # that lived at the end of _trigger_from_ask before the split above; it did not move or loosen.
    if len(top) < 2:
        return []
    return top


def _trigger_from_ask(ask: str, member_prompts: list[str] | None = None) -> list[str]:
    """Return 1-2 regexes that must ALL match for the artifact to fire — `_rank_ask_terms` picks
    the words, this wraps them the way a `prompt_regex` condition op needs them (see that
    function for the full selection history)."""
    top = _rank_ask_terms(ask, member_prompts)
    if len(top) < 2:
        return []
    return [r"\b" + re.escape(w) + r"\b" for w in top]


def _recent_cutoff(days: int = 14) -> str:
    """ISO date N days back. An ask restated inside this window is still live."""
    from datetime import date, timedelta
    return str(date.today() - timedelta(days=days))


# Labels that mean Nick was STOPPING something, not requesting something. Deliberately narrow: the
# instruction-* family is him stating a preference calmly and belongs on the reminder path, while
# these three are the moments he had to interrupt work already in progress.
_FRUSTRATION_LABELS = ("correction-frustration", "correction-stop-execution", "correction-explicit-no")


def _frustration_share(org: int, member_ids: list, support: int = 0) -> float:
    """Fraction of an ask's distinct MOMENTS carrying a stop-the-work label. 0.0 on any failure.

    COUNTED IN MOMENTS, NOT ROWS — and the first version of this counted rows (2026-08-20, caught by
    core-ops within the hour, on the shallow corpus that life's own validation set does not contain).
    `support` is `count(DISTINCT (correction_text, session_date))`; `member_ids` is every raw row.
    Re-mining and multi-pass extraction write the same correction several times, so those two differ
    on **9 of life's 10 top asks**. Measuring the ratio over rows while gating eligibility on moments
    is exactly the defect :295 records having already been fixed for support itself — one angry
    moment extracted three times contributed 3 to both halves of the share but 1 to the gate.

    It moved real verdicts in BOTH directions on life: `consolidate` 70%->78% and `verify state`
    68%->76% CROSS the 0.75 escalation threshold once deduped, while ops's 100% case was four rows
    over three moments. So this was not conservative noise — it was the wrong number, and the seat
    that found it is the one I had told Nick I did not need.

    Zero on any failure is still the safe default: the router only ever uses a HIGH share to
    escalate, so a failed read leaves the ask on the terminal it would have had anyway.
    """
    if not member_ids or support <= 0:
        return 0.0
    try:
        from _env import connect_corebrain
        cur = connect_corebrain().cursor()
        # The numerator dedupes EXACTLY as recurring_asks' support does (:305-306) — same COALESCE
        # pair, same DISTINCT. Two different dedupe rules over one ratio would be a fresh version of
        # this same bug.
        cur.execute(
            "SELECT count(DISTINCT (COALESCE(correction_text, prompt_text), "
            "                       COALESCE(session_date, created_at::date))) "
            "FROM pattern_observations "
            "WHERE org_id = %s AND id = ANY(%s) AND pattern_label = ANY(%s)",
            (org, list(member_ids), list(_FRUSTRATION_LABELS)))
        hits = cur.fetchone()[0] or 0
    except Exception:
        return 0.0
    # Clamped: a label-filtered DISTINCT can only ever be <= support, but a corpus edit between the
    # two reads could in principle invert that, and a share above 1.0 would silently escalate.
    return min(1.0, hits / support)


def ask_cases(org: int, min_support: int = 3, drops: list | None = None) -> list[dict]:
    """Recurring asks as friction-case-shaped dicts the router can consume — cluster_key is the ask
    itself (semantic), so support counts the SAME ask across differently-worded moments.

    `drops` (2026-08-18): pass a list to collect every qualifying ask killed by the trigger gate
    below. It used to be a bare `continue` — no log, no counter, no `detail` row — and because the
    gate runs INSIDE case construction, a dropped ask never reaches `generate_from_asks` and appears
    in no downstream ledger at all. So the single largest loss in the pipeline was also the only one
    with no readable symptom. business found it and measured it on five seats; life reproduced 69% on
    its own corpus, 11 of 16 qualifying asks. The caller logs these; the miner stays free of the
    logger, which is why this is an out-param and not an import."""
    out = []
    # Mine at a LOWER bar, then merge, then apply the real gate. Merging after the gate would be too
    # late: the phrasings that make up one strong directive can each sit under min_support and be
    # dropped before they ever meet each other.
    merged = merge_similar(recurring_asks(org, max(2, min_support - 1)))
    for r in [m for m in merged if m.get("support", 0) >= min_support]:
        # ground the trigger terms in the REAL prompts this ask was mined from, not the paraphrase
        trig = _trigger_from_ask(r["ask"], _member_prompts(org, r["member_ids"]))
        # THE GATE MOVED BELOW THE ROUTER (2026-08-20). This used to `continue` here, killing any ask
        # that could not ground a prompt trigger — and it ran INSIDE case construction, upstream of
        # routing, so the ask died before anything asked what it was going to BECOME.
        #
        # Two of the five terminals do not use a prompt trigger at all:
        #   claude_md_directive  appends prose to CLAUDE.md — there is nothing to match at dispatch
        #   hooked_skill         may install WORK-SHAPED, keyed on a mutating tool call rather than
        #                        on words (artifact_generator._gen_procedure, the work_shape branch)
        # Both were being refused on a requirement neither of them has.
        #
        # MEASURED COST on life's own corpus, the day this was written: 11 of 16 qualifying asks
        # dropped here (69%; business 75%, school 80%). FOUR of the eleven routed to `hooked_skill` —
        #   11x  keep the architecture diagram/documentation in sync
        #    6x  autonomously detect recurring frustrations/workflows and encode them as hooks/skills
        #    4x  execute the plan fully end-to-end with no loose ends
        #    4x  clarify which tools are in use and remove unused systems
        # — which is why `install_hooked_skill` had fired exactly ZERO times in the loop's history
        # while Nick asked for skills, hooks and workflows 67 separate times across three Cores. The
        # system correctly identified "execute the plan fully end-to-end" as a skill and threw it
        # away at a gate.
        #
        # The case is now built either way. `_ask_trigger` may be empty, and `generate_from_asks`
        # applies the requirement AFTER `route_type` — only to the terminals that fire on a match.
        # A drop is still recorded, but now it carries the route it was denied, so the loss is
        # attributable to a terminal instead of vanishing upstream of all of them.
        if not trig and drops is not None:
            drops.append({"ask": r["ask"], "support": r.get("support", 0),
                          "last_seen": str(r.get("last_seen") or ""),
                          "ask_type": r.get("ask_type"), "stage": "no_prompt_trigger"})
        out.append({
            "case_id": "ask_" + re.sub(r"[^a-z0-9]+", "-", r["ask"].lower())[:48].strip("-"),
            "org_id": org, "status": "recurring_ask",
            "support": {"cluster_key": r["ask"], "count": r["support"], "member_ids": r["member_ids"]},
            "user_wanted": r["ask"],
            "trigger_context": {"candidate_event": "UserPromptSubmit", "stage": "prompt"},
            "quality": {"eligible_for_routing": True},
            "_ask_trigger": trig or [],
            "_ask_type": r.get("ask_type"),
            "_ask_steps": r.get("steps", 0),
            # Has this ask been restated recently? A rule that is documented but still being
            # repeated is not coverage — the router uses this to stop suppressing it.
            "_still_recurring": bool(r.get("last_seen") and str(r["last_seen"]) >= _recent_cutoff()),
            # FRUSTRATION SHARE (2026-08-20) — what fraction of this ask's evidence is Nick angry
            # rather than Nick instructing. It is the signal that separates "a thing he wants" from
            # "a thing he has had to STOP me doing", and the second needs to fire at the moment of
            # the work, not as a reminder he reads afterwards. The router uses it to upgrade an
            # inject_contract into a work-moment hook; it never downgrades anything, and it cannot
            # reach a terminal a lower share could not.
            "_frustration_share": _frustration_share(org, r["member_ids"], r.get("support", 0)),
        })
    return out


def _member_prompts(org: int, ids: list, limit: int = 6) -> list[str]:
    con = connect_corebrain()
    try:
        cur = con.cursor()
        # correction_text FIRST. The canonical_ask is extracted from correction_text (see
        # extract_pending), and correction_text is the message Nick actually typed — which is also
        # what a prompt trigger has to match on later. prompt_text holds the PRECEDING turn's prompt,
        # so preferring it grounded every trigger in text unrelated to the ask.
        #
        # This is the root cause of the nonsense triggers seen on 2026-07-27 — ("something",
        # "working") for the architecture-diagram ask, ("fucking", "skill") for the self-improvement
        # ask. Those terms were frequent in the WRONG field. The conjunctive/grounding rules were
        # sound; they were being fed the wrong text.
        cur.execute("SELECT COALESCE(correction_text, prompt_text) FROM pattern_observations "
                    "WHERE org_id=%s AND id = ANY(%s) LIMIT %s", (org, list(ids), limit))
        return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        con.close()


def sibling_moments_for_ask(org: int, canonical_ask: str, limit: int = 30) -> list[str]:
    """Real prompts sharing this EXACT canonical_ask, deduped to distinct MOMENTS — the grounding
    source for friction_router's fc_-case lane (GAP C fix, 2026-08-31).

    WHY THIS EXISTS SEPARATELY FROM `_member_prompts`. That function takes `member_ids` already
    computed by `recurring_asks()`'s org-wide GROUP BY canonical_ask; the fc_-case lane never runs
    that query — a friction_case carries only the ask TEXT (from `build_case`'s `canonical_ask`
    param), not a row-id list. So this re-derives the same sibling set directly from the ask text,
    which is also the ONLY thing friction_router.route() has grounds to group by. It replaces
    grounding fc_ triggers against `support["members"]`, which friction_miner.compute_support
    buckets by `cluster_key` = pattern_label (the correction TYPE, e.g. "correction-frustration")
    — a bucket that mixes every ask ever raised as that kind of correction, so a term could recur
    across the group without ever being about the case's own subject. That mismatch was the
    measured root cause of 33 of 36 all-time denials (org 1, 2026-08-31): the router asked whether
    a word recurred in the WRONG corpus and, for most asks, it did not.

    DEDUPED TO MOMENTS, not rows — the same `DISTINCT (COALESCE(correction_text, prompt_text),
    COALESCE(session_date, created_at::date))` pair `recurring_asks()` uses above, because
    re-extraction and multi-pass mining write the same correction more than once. Row-counting
    inflates support the identical way it did there: measured live on the ask this function was
    built for ("verify state against the live source before claiming", case fc_16358fc0...), 46
    rows share the canonical_ask but only 37 are distinct moments — counting rows would let one
    bad afternoon, logged three times, fake three-of-N term support.

    Correction_text first, prompt_text as fallback — same ordering and same reason as
    `_member_prompts` above: correction_text is what Nick actually TYPED, which is also what the
    resulting `prompt_regex` condition has to match at dispatch time.
    """
    con = connect_corebrain()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT ON (COALESCE(correction_text, prompt_text), "
            "                    COALESCE(session_date, created_at::date)) "
            "       COALESCE(correction_text, prompt_text) "
            "  FROM pattern_observations "
            " WHERE org_id=%s AND canonical_ask=%s AND excluded_reason IS NULL "
            "   AND COALESCE(correction_text, prompt_text) IS NOT NULL "
            " ORDER BY COALESCE(correction_text, prompt_text), "
            "          COALESCE(session_date, created_at::date), created_at DESC "
            " LIMIT %s", (org, canonical_ask, limit))
        return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        con.close()


def _neighbor_prompts(org: int, exclude_ids: list, limit: int = 5) -> list[str]:
    """Real past prompts from OTHER clusters — genuine negatives.

    The negatives used to be siblings from the SAME cluster (members[1:6]), which is a mislabel:
    every member of a cluster is an instance of the same ask, so a correct trigger SHOULD fire on
    them. Labelling them `no_fire` punished exactly the triggers that worked and pushed the
    generator toward arbitrarily narrow rules. Drawing from outside the cluster makes the negative
    mean what it says: a different ask the rule must stay quiet on.
    """
    con = connect_corebrain()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT prompt_text FROM pattern_observations "
            "WHERE org_id=%s AND prompt_text IS NOT NULL AND length(prompt_text) > 20 "
            "AND NOT (id = ANY(%s)) ORDER BY random() LIMIT %s",
            (org, list(exclude_ids), limit))
        return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        con.close()


def route_ask_case(org: int, case: dict) -> tuple[dict, dict] | None:
    """Build a full inject-artifact spec + examples from a recurring-ask case, reusing the friction
    schema so it runs through the SAME corpus-grounded gate (over-broad triggers get rejected there)."""
    import hashlib
    import friction_router as fr
    trig = case.get("_ask_trigger")
    if not trig:
        return None
    # The artifact KIND is part of the identity. It used to be sha256(case_id) alone, so a procedure
    # and a contract mined from the same ask produced the SAME artifact_id — installing one silently
    # overwrote the other, and rolling that one back deleted both. Hit live on 2026-07-27: installing
    # a procedure for "automate baseline sync" clobbered the existing contract for the same ask.
    kind = "hooked_skill" if case.get("_ask_type") == "hooked_skill" else "contract"
    aid = "art_" + hashlib.sha256(f"{kind}|{case['case_id']}".encode()).hexdigest()[:20]
    # CONJUNCTIVE: every trigger term is its own prompt_regex op inside `all`, so they must all match.
    # Expressed as separate ops rather than one lookahead regex — _validate_regex rejects lookaheads.
    cond = {"all": [{"op": "event_is", "value": "UserPromptSubmit"}]
                   + [{"op": "prompt_regex", "value": t} for t in trig]}
    members = _member_prompts(org, case["support"]["member_ids"])
    # The positive must be a REAL prompt this rule actually fires on. It used to be members[0]
    # unconditionally, so a trigger grounded in the cluster majority could fail its own positive
    # (measured: 3 of 5 procedures rejected "positive p1 did NOT fire"). Take every member the  # privacy-ok: generic engineering vocabulary
    # trigger genuinely matches; if none matches, the ask has no honest positive — refuse it.
    def _matches_all(text: str) -> bool:
        return all(re.search(t, (text or "").lower()) for t in trig)
    matching = [m for m in members if _matches_all(m)]
    if not matching:
        return None
    # DEMONSTRATED support, not cluster support. These are two different numbers and the message
    # used to report the wrong one. support.count comes from a SQL count(DISTINCT ...) over the
    # cluster and is genuinely computed — but only ONE member was ever checked against the trigger
    # (`next(...)`), so an artifact could say "Recurring ask (3x)" while shipping a single positive
    # and no evidence that the other two members fire this rule at all. core-school caught it on
    # 2026-08-04: two artifacts claiming 3x with tests.positive_ids == ['p1'], and the "3x" living
    # only in the effect string.
    #
    # This is the same repair compute_support() made for friction_miner on 07-30 — make the claim
    # equal the evidence — applied to the lane that never got it. Every matching member now becomes
    # a positive, and the message reports how many actually fire, so the number in the prose is the
    # number the test set proves. Capped so a large cluster cannot bloat a spec that is read into
    # context on every fire.
    MAX_POSITIVES = 5
    pos = [{"id": f"p{i + 1}", "event": "UserPromptSubmit", "expected": "fire",
            "provenance": "real_positive",
            "hook_input": fr._hook_input("UserPromptSubmit", prompt=m)}
           for i, m in enumerate(matching[:MAX_POSITIVES])]
    demonstrated = len(matching)
    cluster = case["support"]["count"]
    # When the cluster is larger than what the trigger demonstrably fires on, say both rather than
    # silently reporting the flattering one — a reader can then judge the gap instead of trusting it.
    if demonstrated >= cluster:
        msg = f"Recurring ask ({demonstrated}x): {case['user_wanted']}"
    else:
        msg = (f"Recurring ask ({demonstrated}x demonstrated of {cluster} in cluster): "
               f"{case['user_wanted']}")
    neg = [{"id": "n_evt", "event": "Stop", "expected": "no_fire", "provenance": "event_mismatch",
            "hook_input": fr._hook_input("Stop", assistant=case["user_wanted"])},
           {"id": "n_pol", "event": "UserPromptSubmit", "expected": "no_fire", "provenance": "polarity_mutation",
            "hook_input": fr._hook_input("UserPromptSubmit", prompt="a completely unrelated topic entirely")}]
    for i, m in enumerate(_neighbor_prompts(org, case["support"]["member_ids"])):
        neg.append({"id": f"n_nb{i}", "event": "UserPromptSubmit", "expected": "no_fire",
                    "provenance": "real_neighbor", "hook_input": fr._hook_input("UserPromptSubmit", prompt=m[:300])})
    if not any(n["provenance"] == "real_neighbor" for n in neg):
        return None  # need a real corpus neighbor, same bar as friction routing
    spec = {"spec_version": 1, "artifact_id": aid, "case_id": case["case_id"], "org_id": org,
            "type": "contract", "event": "UserPromptSubmit", "condition": cond,
            "effect": {"mode": "inject", "message": msg[:2000], "skill_id": None},
            "tests": {"positive_ids": [p["id"] for p in pos], "negative_ids": [n["id"] for n in neg]},
            "template": {"id": "ask-contract-v1", "sha256": "pending"}, "scope": "org_local",
            "lease": {"max_fires_per_session": 2, "expires_at": None},
            "generator_version": "ask-miner/1"}
    return spec, {"positive": pos, "negative": neg}


def main() -> int:
    org = get_org_id()
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-support", type=int, default=3)
    ap.add_argument("--pending", action="store_true")
    a = ap.parse_args()
    if a.pending:
        p = extract_pending(org)
        print(f"{len(p)} correction(s) awaiting ask-extraction")
        return 0
    rec = recurring_asks(org, a.min_support)
    print(f"{len(rec)} recurring ask(s) at support>={a.min_support} — the keystone signal:")
    for r in rec:
        print(f"  [{r['support']}x] {r['ask']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
