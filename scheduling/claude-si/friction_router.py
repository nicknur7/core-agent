#!/usr/bin/env python3
"""friction_router.py — P2 router (v3, post-2nd-review). friction_case -> artifact spec + tests.

v1 is INJECT-ONLY and targets UserPromptSubmit ONLY — the one event whose payload carries the user
prompt, which is also the field the specificity corpus (pattern_observations.prompt_text) grounds
against. Stop/PreToolUse are deferred: Stop would need an assistant-output corpus we don't have
cleanly (Codex 4th review); PreToolUse's payload lacks the user's intent.

Specificity (Codex re-review #1): the condition is CONJUNCTIVE — it requires the TWO most
distinctive words to CO-OCCUR (two separate regex predicates under `all`), not a single-keyword OR
match. So a "database migration" rule does NOT fire on "database vacation."

Negatives (Codex re-review #2): REAL corpus-neighbor negatives are MANDATORY — overlapping neighbors
(sharing one key word) are KEPT and tested (they're the hardest negatives; with conjunctive matching
they correctly don't fire). Routing FAILS if no real neighbor is available. All stored/injected text
is redacted (fj.redact).
"""
from __future__ import annotations

import hashlib
import re

import friction_jsonl as fj

# v4 (2026-08-31, GAP C fix): trigger derivation for the fc_-case lane now grounds against the
# ask's own real siblings (ask_miner.sibling_moments_for_ask + _rank_ask_terms) instead of
# support["members"] (pattern_label-grain — the wrong corpus, see route()'s TRIGGER DERIVATION
# comment). Output changes for cases that previously denied "no_trigger_terms", so the version
# bumps: _artifact_id hashes it, and a stale artifact_id from a v3 refusal must not collide with
# a v4 mint of the same case_id once this ships.
GENERATOR_VERSION = "friction-router/4"


def _artifact_id(case_id: str) -> str:
    return "art_" + hashlib.sha256(f"{case_id}|{GENERATOR_VERSION}".encode()).hexdigest()[:20]


def _word_re(w: str) -> str:
    return r"\b" + re.escape(w) + r"\b"


def _occurs(word: str, text: str) -> bool:
    """Word-boundary containment — the SAME test the generated condition will apply.

    Grounding used plain substring containment (`w in text`) while the emitted condition uses
    _word_re -> r"\bw\b". So "systems" grounded a trigger on \bsystem\b that could never match
    it, and the test gate reported "positive p1 did NOT fire" — an impossible contract rather
    than a wrong one. Any check that decides whether a trigger is grounded has to use the trigger's
    own matching semantics, or it is measuring a different question.
    """
    return re.search(_word_re(word), text or "", re.I) is not None


def _hook_input(event, *, prompt="", assistant="", tool=None):
    return {"event": event, "tool_name": tool, "prompt": prompt, "assistant_text": assistant,
            "session_id": "test"}


def route(case: dict, neighbors: list[str] | None = None) -> dict | None:
    if not case.get("quality", {}).get("eligible_for_routing"):
        case["_drop_reason"] = "not_eligible"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None
    moment = case.get("moment", {})
    correction = case.get("user_wanted") or moment.get("correction", "")

    # THE TRIGGER COMES FROM THE ASK, NOT THE FRUSTRATION (2026-07-30). Drawing key terms from
    # the raw correction is how a contract ends up firing on \bnothing\b: "nothing" is a long,
    # rare word in an angry sentence, and _distinctive ranks by length as a proxy for
    # distinctiveness, so incidental venting outranks the actual subject. The first artifact this
    # generator produced after distillation was minted with exactly that trigger — a correct
    # message ("fix session start/close reliability across all cores") wired to a word that has
    # nothing to do with it.
    #
    # A trigger should fire on the SUBJECT of the recurring ask. canonical_ask is that subject
    # with the frustration stripped out, so both halves of the artifact derive from the same
    # distilled text and can no longer disagree about what the contract is for.
    #
    # But a trigger has to satisfy BOTH constraints at once, and taking either alone fails:
    #
    #   on-subject   drawn from the ask, or you get \bnothing\b
    #   occurring    present in what Nick actually TYPES, or the contract never fires
    #
    # Deriving from the ask alone produced 0 mintable artifacts, because distilled vocabulary
    # ("reliability") is not always the vocabulary of the prompt. Deriving from the correction
    # alone produced on-topic-looking nonsense. So: take a wider candidate set FROM THE ASK, then
    # keep only the candidates that actually recur across sibling prompts. What survives is both
    # about the right subject and grounded in real usage; if fewer than two survive, refuse.
    # REQUIRED HERE, not 120 lines below. The distilled-form check used to live only at the `want`
    # gate further down, so this block ran first with `_canon_for_key or correction` — deriving the
    # trigger from Nick's raw complaint whenever no distilled form existed. Harmless in practice,
    # because the later check refused the mint anyway, but harmless for a reason that is 120 lines away
    # and easy to move: relax or reorder that gate and this silently becomes an off-subject trigger
    # source again, which is the exact defect removed below. The invariant belongs where it is relied on.
    _canon_for_key = (case.get("canonical_ask") or "").strip()
    if not _canon_for_key:
        case["_drop_reason"] = "no_canonical_ask"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None                      # no distilled form yet — wait for ask_miner

    # ROUTE-TYPE PRE-CHECK (2026-08-31, judge-required change 3 — the SAME defect the ask lane
    # already fixed, in the OTHER lane). This function only ever builds `type: "contract"`
    # (event=UserPromptSubmit, op=prompt_regex) — so it demanded a lexical trigger from every case
    # and called it "no_trigger_terms" whenever grounding failed. But not every ask was ever going
    # to GET a prompt trigger: a diffuse standing preference has no subject word to key on
    # (claude_md_directive) and a cadence ask needs a clock, not a regex (scheduled_job_proposal) —
    # artifact_typer.route_type() already knows this, deterministically, from the ask text alone.
    # ask_cases() hit the identical shape of defect and fixed it 2026-08-20 by checking route_type()
    # BEFORE demanding a trigger (see its own comment block, "THE GATE MOVED BELOW THE ROUTER").
    # Those two ask types are ALREADY served — generate_from_asks() builds them from the SAME
    # canonical_ask via the ask lane, independently of this function — so a "no_trigger_terms"
    # verdict here for one of them was never a recoverable loss: this lane was trying to build a
    # kind of artifact it structurally cannot build, then blaming the trigger for the refusal.
    import artifact_typer as at  # local: matches ask_miner.route_ask_case's own import-at-use style
    import ask_miner as am
    _rt_type = (at.route_type(_canon_for_key) or {}).get("type")
    if _rt_type in ("claude_md_directive", "scheduled_job_proposal"):
        case["_drop_reason"] = f"served_by_ask_lane:{_rt_type}"  # OBSERVABILITY (2026-08-31)
        return None

    # TRIGGER DERIVATION, GROUNDED AGAINST THE ASK'S OWN SIBLINGS (2026-08-31, GAP C fix).
    #
    # This used to read `support["members"]`, populated by friction_miner.compute_support, which
    # buckets by `cluster_key` = pattern_label — the correction's TYPE ("correction-frustration",
    # "correction-should-have", ...), not its subject. That bucket mixes every ask Nick has ever
    # raised as that kind of correction, so a candidate word had to recur across a group that, for
    # most asks, is simply unrelated text. Measured live, org 1, 2026-08-31: 33 of 36 all-time
    # denials were exactly this — `no_trigger_terms` on a case whose ask DOES recur, just not in
    # the corpus this was grounding against (fc_16358fc0's ask has 46 sibling ROWS sharing its
    # exact canonical_ask — 37 distinct moments after dedup — and none of that evidence was ever
    # consulted, because compute_support grouped it by correction-type instead).
    #
    # ask_miner already built and validated the fix for the ask lane: `_rank_ask_terms` grounds
    # candidate words in the ask's own REAL sibling prompts — lift-ranked against the whole
    # corpus's base rate, zero-base-rate hard-rejected, two-term floor (see that function for the
    # full history). Reusing it here — rather than this file's own `_distinctive`/`_occurs` pair,
    # a second mechanism solving the identical problem — is the consolidation: one validated
    # grounding algorithm with two callers, not two mechanisms that happen to agree most of the
    # time. `sibling_moments_for_ask` is the fc_-lane equivalent of `_member_prompts`: it re-derives
    # the sibling set from the ask TEXT (all this lane has — no `member_ids` from `recurring_asks()`
    # exist here) and dedupes rows to distinct MOMENTS, the same repair `recurring_asks()` and
    # `compute_support` already made for their own counts, so one bad afternoon logged three times
    # cannot fake three-of-N term support.
    _sib_prompts = am.sibling_moments_for_ask(case["org_id"], _canon_for_key)
    key = am._rank_ask_terms(_canon_for_key, _sib_prompts)
    if len(key) < 2:
        case["_drop_reason"] = "no_trigger_terms"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None
    # NO SEPARATE CO-OCCURRENCE SEARCH, unlike the code this replaces. `_rank_ask_terms` already
    # returns its best two terms by lift; it does not promise they co-occur in one real sibling. If
    # they never do, `_make_examples` below cannot find a genuine positive among the siblings and
    # falls back to the correction text — and the UNCHANGED test gate (friction_test_gate.gate)
    # bounces that downstream as "positive did NOT fire", a `gate_failed`, not a silent install.
    # That is a real verdict about a real spec (judge's review: "watched the unchanged gate bounce
    # its own worst pair"), which is strictly more information than refusing to try at all.

    # v1 targets UserPromptSubmit ONLY: the gate's specificity corpus is prompt_text, so prompt
    # conditions are grounded against real data. Stop would need an assistant-output corpus we don't
    # have cleanly — deferred (Codex 4th review).
    event, op = "UserPromptSubmit", "prompt_regex"
    cond = {"all": [{"op": "event_is", "value": event}] + [{"op": op, "value": _word_re(w)} for w in key]}

    want = fj.redact((case.get("user_wanted") or "").strip().replace("\n", " "))[:400]

    # ── MINT QUALITY GATE (2026-07-30) — the taste gate this generator never had ──────────────
    #
    # `want` is the RAW user text, truncated. There is no distillation step, so whatever Nick
    # typed became the injected "guidance" verbatim. Measured consequence: of 30 active artifacts,
    # NINETEEN injected undistilled quotes — including verbatim profanity, a pasted terminal
    # banner from a ops transcript (box-drawing characters and all), subagent task-ids, and one
    # firing on \bwork\b + \bcode\b, i.e. on essentially every coding prompt.
    #
    # The Northstar is "molding the agent around the person." Re-injecting his month-old anger at
    # him on the word "work" is context pollution wearing the uniform of learning. The whole loop
    # had been judged on whether it RUNS — mint counts, proof windows, promote paths — and never
    # once on whether its output is any good. One read of active.json falsified it.
    #
    # A generator that cannot distill must REFUSE rather than quote. Returning None here sends the
    # case back to the pool; it is not lost, it simply does not become live guidance until it can
    # be expressed as an instruction. Fail-toward-silence is correct for a steering surface: a
    # missing nudge costs nothing, a bad one costs every turn it fires on.
    # RECURRENCE, first — because the message calls itself "Recurring expectation" and until
    # 2026-07-30 nothing checked that it recurred. friction_miner wrote a hardcoded
    # distinct_sessions=1 / positive_case_ids=[] and left "populated at clustering/routing (P2)"
    # as a comment; P2 did not exist. All 44 cases in the pool carried that placeholder, and
    # every artifact ever minted here was therefore a single prompt from a single session
    # presented to the model as an established pattern. compute_support() now computes it.
    #
    # Two sessions is the floor, not two occurrences: ten corrections inside one bad afternoon
    # are one event. A case that has not recurred goes back to the pool and costs nothing there.
    _sup = case.get("support", {}) or {}
    if int(_sup.get("distinct_sessions") or 1) < 2:
        case["_drop_reason"] = "not_recurring"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None                      # not recurring — do not call it "Recurring expectation"
    if not (_sup.get("positive_case_ids") or []):
        case["_drop_reason"] = "no_sibling_support"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None                      # no sibling occurrences to ground it in

    # ── DISTILLED OR SILENT (2026-07-30) ───────────────────────────────────────────────────
    #
    # `want` is fj.redact(user_wanted) — the RAW correction, truncated to 400 chars. Every filter
    # below it was built to catch what happens when raw text becomes steering: profanity, pasted
    # terminal output, 446-character quotes, typos, first-person. All of that is downstream of
    # one decision — that the generator QUOTES instead of DISTILLING.
    #
    # It did not have to. ask_miner already writes canonical_ask onto pattern_observations, 287
    # of 443 live rows carry one, and they read as real instructions ("act autonomously and
    # execute without pausing to ask permission"). Two channels over the same corpus, one
    # distilling and one quoting, and the injected artifacts came from the quoting one.
    #
    # So: mint from the distilled ask, and REFUSE when there is none. A case with no canonical
    # form is not lost — it returns to the pool and becomes mintable as soon as ask_miner
    # canonicalises it. Fail-toward-silence is right for a steering surface: a missing nudge
    # costs nothing, a bad one costs every turn it fires on.
    #
    # The text gates below now run against the DISTILLED text as belt-and-braces. They should
    # essentially never fire. If they start firing, canonicalisation has regressed, and that is
    # worth knowing loudly rather than papering over.
    _canon = (case.get("canonical_ask") or "").strip()
    if not _canon:
        case["_drop_reason"] = "no_canonical_ask"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None                      # no distilled form yet — wait for ask_miner
    want = _canon[:400]

    _m = want.lower()
    if len(want) > 160:
        case["_drop_reason"] = "undistilled_quote"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None                      # undistilled quote, not an instruction
    # First person is the tell that this is a QUOTE rather than an instruction. A distilled
    # directive reads "use codex alongside core for substantial system/code work"; the artifacts
    # minted this morning read "i want you to fix it end to end and test it" and "Okay, before
    # that, I want you to tell me exactly...". The second is not even an expectation — it is a
    # one-off question. Cheap, mechanical, and it fails toward silence.
    if re.search(r"\b(?:i|i'?m|i'?ll|i'?ve|my|me)\b", _m):
        case["_drop_reason"] = "first_person_quote"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None                      # Nick's words, not a distilled instruction
    if re.search(r"fuck|shit|damn", _m):
        case["_drop_reason"] = "profanity"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None                      # never inject the user's profanity back at him
    if re.search(r"[▗▘▙▚▛▜▝▞▟│─╭╮╰╯]|task-id|tool_use|<function_", want):
        case["_drop_reason"] = "pasted_artifact"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None                      # pasted terminal / transcript / tool output
    # A SECOND TRIGGER-GROUNDING CHECK USED TO LIVE HERE AND WAS REMOVED (2026-08-31). It re-read
    # `support["members"]` — the same pattern_label-grain bucket the derivation step above stopped
    # using — and re-tested the already-chosen `key` against it with a substring match. Once the
    # derivation step above grounds against `sibling_moments_for_ask` instead, this second check
    # would silently REINTRODUCE the exact defect GAP C names: a correctly-derived, ask-grounded
    # trigger term checked against an unrelated correction-type bucket, and dropped as
    # "trigger_not_recurring" whenever that unrelated bucket happens not to contain it — which is
    # most of the time, by construction. Grounding happens once, upfront, against the right corpus;
    # a second pass against the wrong one is not belt-and-braces, it is a second parallel mechanism
    # that disagrees with the first. `_make_examples` below still requires a genuine example to
    # exist (falling through to the corpus-grounded test gate otherwise), so recurrence is still
    # provably checked — just not twice, and not against the wrong siblings.

    effect = {"mode": "inject",
              "message": f"Recurring expectation ({case.get('support', {}).get('cluster_key')}): {want}",
              "skill_id": None}
    # `members` is the SAME ask-grounded sibling set the trigger was derived from above — not
    # `support["members"]` (pattern_label-grain). _make_examples wants dict-likes with
    # `prompt_text`; sibling_moments_for_ask returns bare strings, so wrap them.
    pos, neg = _make_examples(key, correction, neighbors,
                              members=[{"prompt_text": t} for t in _sib_prompts])
    if not pos or not neg:
        case["_drop_reason"] = "no_examples"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None
    # MANDATORY: at least one REAL corpus-neighbor negative (Codex re-review #2)
    if not any(n["provenance"] == "real_neighbor" for n in neg):
        case["_drop_reason"] = "no_real_neighbor_negative"  # OBSERVABILITY (2026-08-31): read back by friction_loop.run() to persist WHY this case never became a spec
        return None
    case.pop("_drop_reason", None)  # cleared on mint — a case dict reused across calls should not carry a stale refusal
    return {
        "spec_version": 1, "artifact_id": _artifact_id(case["case_id"]), "case_id": case["case_id"],
        "org_id": case["org_id"], "type": "contract", "event": event, "condition": cond, "effect": effect,
        "tests": {"positive_ids": [p["id"] for p in pos], "negative_ids": [n["id"] for n in neg]},
        "template": {"id": "hook-rule-v1", "sha256": "pending"}, "scope": "org_local",
        "lease": {"max_fires_per_session": 2, "expires_at": None},
        "generator_version": GENERATOR_VERSION,
        "_examples": {"positive": pos, "negative": neg},
    }


def _make_examples(key, correction, neighbors, members=None):
    """Positive/negative fixtures for the test gate.

    THE POSITIVE MUST CONTAIN THE TRIGGER, and after 2026-07-30 the correction often does not.
    Triggers now derive from canonical_ask (the distilled subject) rather than from the raw
    correction, so a positive built blindly from `correction` fails its own gate — "positive p1
    did NOT fire" on 5 of 6 routed artifacts, which reads like a bad contract and is actually a
    bad fixture.

    The right positive is a REAL prompt of Nick's that contains the key terms, and one is
    guaranteed to exist: key terms are only chosen if they appear in at least two sibling
    prompts. So search the members for one and use it; fall back to the correction only when
    there are no members, which is the pre-clustering case the recurrence gate already rejects.
    Using a real prompt is also strictly better evidence than a synthesised one.
    """
    def ex(eid, prov, expected, ev, **kw):
        return {"id": eid, "event": ev, "expected": expected, "provenance": prov,
                "hook_input": _hook_input(ev, **kw)}

    _p = correction
    for m in (members or []):
        t = str(m.get("prompt_text") or m.get("correction") or "")
        if t and all(_occurs(k, t) for k in key):
            _p = t[:300]
            break
    pos = [ex("p1", "real_positive", "fire", "UserPromptSubmit", prompt=_p)]
    neg = [ex("n_evt", "event_mismatch", "no_fire", "Stop", assistant=" ".join(key)),
           ex("n_pol", "polarity_mutation", "no_fire", "UserPromptSubmit", prompt="a completely unrelated topic entirely")]

    # REAL corpus-neighbor negatives — KEEP overlapping ones (with conjunctive matching they don't
    # fire unless they contain BOTH key words, which unrelated prompts almost never do).
    for i, nb in enumerate((neighbors or [])[:8]):
        if not nb or nb == correction:
            continue
        neg.append(ex(f"n_nb{i}", "real_neighbor", "no_fire", "UserPromptSubmit", prompt=nb[:300]))
    return pos, neg
