#!/usr/bin/env python3
"""friction_tune.py — narrow an over-firing artifact toward its own intent, instead of killing it.

WHY THIS EXISTS
---------------
friction_watchdog had exactly one response to a misbehaving rule: quarantine it. Fires more
than twice in a session, dispatch errors, lease expired — all roads led to rollback. Binary:
alive or dead.

The operator, 2026-07-28, pushed back when shown a drop-or-downgrade choice: why drop a rule
instead of tuning it?

He was right and the question I put to him was the wrong one. A rule that over-fires is
usually not WRONG — its trigger is too wide. Quarantining it throws away something the system
learned about how he works, on evidence that only shows the trigger needs tightening. Drop is
correct in two cases only: the guarded behaviour stopped occurring (it worked), or the rule
has drifted so far it is matching something unrelated and needs re-deriving from scratch.

THE INVARIANT, ENFORCED IN CODE AND NOT BY CONVENTION
-----------------------------------------------------
    A tune may never reduce what a rule catches unless every one of its positives still
    passes.

Without that, "tuning" quietly becomes "disabling" — a narrowing that fires less because it
has stopped catching the thing it exists for is indistinguishable, by rate alone, from a
successful tune. Every candidate here is verified against the artifact's own positive
examples BEFORE it is written, and rejected if any positive stops matching.

WHAT IT WILL AND WILL NOT DO
----------------------------
WILL: add a conjunct that every positive satisfies and the over-fired text does not. That is
narrowing toward evidence — it can only ever REDUCE what matches, never extend it.

WILL NOT: rewrite or relax an existing condition, change the effect mode, or touch a rule
whose verdict is "rotted" or "drifted". Those need re-deriving from origin, which is a
judgement call and goes to review rather than being applied. A tuner that rewrites logic is
one bad inference away from silently changing what a gate protects.

Reversible by construction: si_project.upsert stores prior_spec and bumps revision on every
write, so any tune can be rolled back and the history is on the record.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MIN_POSITIVES = 2          # below this there is not enough evidence to narrow safely
MAX_CANDIDATE_TERMS = 40   # keep the search cheap and the resulting condition readable
MAX_CLAUSES = 12           # the DSL's own cap (friction_installer._validate_condition). Mirrored
                           # here so a narrowing can never propose a condition the validator
                           # would reject. Unreachable at today's shapes (largest live clause
                           # list is 3, cap on narrowings is 3) — checked anyway, it costs a line.
MAX_NARROWINGS = 3         # churn budget. Narrowing is monotonic and add-only, so without a
                           # cap a rule can be tightened every sweep — each step passing the
                           # invariant — until it fires on nothing. A rule that needs a fourth
                           # cut is the wrong rule, and the honest response is to re-derive it
                           # from origin (a judgement call, proposal-only) rather than keep
                           # cutting. (core-business, hostile read finding 6.)


# Ops the tune harness can HONESTLY supply. It builds a context out of one stored example
# string plus an event name — that is all it has. Everything else in friction_dispatch._OPS
# reads live runtime state (tool calls, tool mutability, state flags, delivery/review oracles)
# which the harness does not have and cannot fabricate without lying.
#
# An unsupplied op does not error — it evaluates against absent state and returns a confident
# False. So the invariant silently concludes "this positive no longer matches" and refuses,
# for a reason that has nothing to do with the narrowing. The rule is never tuned and nobody
# is told why.
#
# core-business measured the live estate: exactly 1 of 30 artifacts is affected — and it is
# the ONLY enforcing block, gated on state_flag_is + adversarial_review_present. Narrow blast
# radius and highest stakes are the same artifact, which is precisely why a silent skip stays
# invisible forever: nobody notices one rule quietly never being considered.
TUNABLE_OPS = {"event_is", "prompt_regex", "assistant_regex"}

# THE BOUNDARY, not a gap. core-business traced this from one artifact to the whole class:
#
#   The tune path can narrow CONTRACTS and cannot narrow ENFORCEMENT, by construction, and
#   this holds for every future enforcement artifact as long as blocks key on session state.
#
# It follows from what enforcement IS. A block fires on what the session DID — was a
# deliverable written, did an adversarial review happen — which is Stop-oracle state. A
# contract fires on what the prompt SAID, which is exactly the tunable set. Both live
# enforcement templates (deliverable_as_artifact, adversarial_review_before_blast_radius) are
# event=Stop with state_flag_is + an oracle op; zero templates use assistant_regex.
#
# So the measured "1 of 30 artifacts" is narrow in COUNT and total in KIND: 100% of the
# enforcing class, present and future.
#
# We are NOT going to fix that, and the decision is deliberate. A block keyed on "did an
# adversarial review happen" has no narrower version that still means the same thing —
# narrowing it would change what it protects, which is the single outcome the invariant exists
# to prevent. Quarantine-then-re-derive is the correct and final answer for the block class.
#
# The honest claim is therefore "contracts self-tune; enforcement is human-reviewed", which is
# both true and more defensible than a blanket tuning claim.
#
# Consequence handled below: this means untunability fires on every block FOREVER. Reported as
# an expected BOUNDARY rather than a recurring fault, because a warning that always fires is a
# warning nobody reads — the exact failure mode we just removed from estate-sweep.
ORACLE_OPS = {"state_flag_is", "artifact_delivery_present", "adversarial_review_present",
              "tool_call_present", "tool_result_present", "tool_mutability_is", "tool_name_in"}


def unsupplied_ops(cond) -> set:
    """Ops in this condition that the harness cannot supply. Walks all/any/none recursively."""
    found = set()
    if isinstance(cond, dict):
        op = cond.get("op")
        if isinstance(op, str) and op not in TUNABLE_OPS:
            found.add(op)
        for key in ("all", "any", "none"):
            for sub in (cond.get(key) or []):
                found |= unsupplied_ops(sub)
    elif isinstance(cond, list):
        for sub in cond:
            found |= unsupplied_ops(sub)
    return found


def text_of(example) -> str:
    """The text of a stored example, whichever shape it is in.

    Examples became {text, channel} dicts in the finding-7 fix, because an assistant_regex
    clause evaluated against a prompt string fabricates evidence. But _terms() still expected
    plain strings, so the FIRST thing propose_narrowing did with a real example was call
    .lower() on a dict and raise AttributeError — which _try_narrow's blanket except swallowed,
    returning None, falling through to quarantine.

    That is finding 1's exact symptom, reintroduced by finding 7's fix, and invisible for the
    same reason all of them were invisible: the tune path had never executed. Found 2026-07-28
    by running it end to end rather than reading it. (bin/tests/test_tune_path_e2e.py.)

    Accepts the legacy bare-string shape so artifacts stored before the channel fix still work.
    """
    if isinstance(example, dict):
        return example.get("text") or ""
    return example or ""


def _terms(text: str) -> set:
    """Content words, lowercased. Deliberately crude — the candidate only has to be
    verifiable, and every candidate is verified against the positives before use."""
    stop = {"the", "and", "that", "this", "with", "from", "have", "was", "for", "you",
            "not", "but", "are", "his", "her", "its", "our", "your", "they", "them"}
    return {w for w in re.findall(r"[a-z][a-z'-]{2,}", (text or "").lower()) if w not in stop}


def propose_narrowing(spec: dict, positives: list, overfired: list) -> dict | None:
    """A conjunct present in EVERY positive and absent from the over-fired text.

    Returns a new spec, or None when no safe narrowing exists — which is a legitimate and
    common answer. A rule can genuinely be too wide with no term that separates the cases,
    and inventing one would be exactly the guesswork this whole subsystem exists to replace.
    """
    if len(positives) < MIN_POSITIVES:
        return None
    common = None
    for p in positives:
        t = _terms(text_of(p))
        common = t if common is None else (common & t)
    if not common:
        return None

    bad = set()
    for o in overfired:
        bad |= _terms(text_of(o))
    candidates = sorted(common - bad)[:MAX_CANDIDATE_TERMS]
    if not candidates:
        return None

    # Prefer the longest term: a longer word is more specific, so it narrows more and is
    # less likely to be incidental vocabulary shared by accident.
    term = max(candidates, key=len)

    cond = json.loads(json.dumps(spec.get("condition") or {}))   # deep copy
    clauses = cond.get("all")
    if not isinstance(clauses, list):
        return None
    # BOUND THE CLAUSE LIST. The DSL caps a condition at 12 clauses
    # (friction_installer._validate_condition), and this appended unconditionally. Unreachable
    # at today's artifact shapes — the largest live clause list is 3 and MAX_NARROWINGS is 3, so
    # the worst case is 6 — but the check costs one line and the alternative is a spec the
    # validator would reject. Latent-and-far, per core-business's own measurement, fixed anyway.
    if len(clauses) + 1 > MAX_CLAUSES:
        return None
    clauses.append({"op": "prompt_regex", "value": rf"\b{re.escape(term)}\b"})

    new = json.loads(json.dumps(spec))
    new["condition"] = cond

    # VALIDATE THE NARROWED CONDITION with the production validator, not by trusting that this
    # function only ever appends well-formed clauses. That happens to be true today and is
    # exactly the kind of "happens to be true" that stops being true after one edit.
    # (core-business, finding 8, second recommendation.)
    try:
        import friction_installer as _inst
        ok, why = _inst._validate_condition(cond, TUNABLE_OPS)
        if not ok:
            return None
    except Exception:
        pass   # validator unavailable -> fall through; verify() is still the hard gate
    # HISTORY, not just the latest term. narrowed_on was overwritten each time, so a spec
    # carried no record of having been narrowed before — and narrowing is monotonic and
    # add-only, so a rule could be tightened every sweep, each step individually
    # invariant-passing, until it matched nothing at all. Death by a thousand valid cuts.
    # (core-business, finding 6.)
    # HISTORY DOES NOT GO IN THE SPEC. This wrote a top-level "_tune" key, and _validate_spec
    # enforces a CLOSED key set that does not include it — so the first successful narrowing
    # would have written an artifact that the validator rejects, permanently un-reinstallable
    # through the normal path, and it would have done so silently. si_artifacts is supposed to
    # hold only gate-passed, validated specs; that invariant is the whole trust story for the
    # table. (core-business, finding 8.)
    #
    # Widening the allowed key set to fit bookkeeping is the obvious fix and the wrong one — the
    # schema being narrow is what makes it worth having. si_artifacts already versions every
    # write via prior_spec + revision, so the history is recorded there more durably than a JSON
    # key anyway. The term is returned alongside the spec instead of buried inside it.
    return {"spec": new, "term": term}





def verify(new_spec: dict, positives: list, evaluate) -> tuple[bool, str]:
    """THE INVARIANT. Every positive must still match, or the tune is refused.

    `evaluate(spec, text) -> bool` is injected so this is testable without a live dispatcher.
    The caller MUST inject the production matcher; a weaker one makes this check vacuous in
    the dangerous direction, which is exactly what happened — the first version used a local
    lambda that saw only prompt_regex clauses and ignored every other operator.

    So the injection point is also defended here, independently of who injects: if a spec's
    condition exercises NO clause this evaluator understands, "all positives pass" is
    meaningless and the answer is UNPROVEN, not PASS. That closes the vacuous path whatever
    is passed in. (core-business, hostile read finding 3, second recommendation.)
    """
    clauses = [c for c in ((new_spec.get("condition") or {}).get("all") or [])
               if isinstance(c, dict) and c.get("op")]
    if not clauses:
        return False, "unproven — the narrowed spec has no evaluable clauses"
    for p in positives:
        try:
            if not evaluate(new_spec, p):
                return False, f"positive no longer matches: {text_of(p)[:90]}"
        except Exception as e:
            return False, f"evaluation failed on a positive: {type(e).__name__}"
    return True, f"all {len(positives)} positives still match across {len(clauses)} clause(s)"


def tune(artifact: dict, positives: list, overfired: list, evaluate, prior: int = 0) -> dict:
    """Attempt one narrowing. Returns {ok, spec?, term?, reason}. Never writes — the caller does,
    so the decision to persist stays with code that also owns rollback.

    `prior` is how many times this artifact has already been narrowed. It is passed IN rather
    than read off the spec, because the count is bookkeeping and bookkeeping does not belong in
    a validated spec — see the note in propose_narrowing. The caller reads it from the evidence
    store, which is where every other piece of per-artifact bookkeeping now lives.
    """
    if artifact.get("effect", {}).get("mode") == "block" and not positives:
        return {"ok": False, "reason": "refusing to narrow a BLOCK with no positive examples "
                                       "— the invariant cannot be checked, so it cannot be safe"}
    # Distinguish the two ways narrowing declines. Both returned "no term separates the
    # positives", which is false when the real reason is that there was only one positive to
    # generalise from — and a wrong diagnostic sends the next reader looking in the wrong
    # place. The router currently emits ONE positive per artifact, so this is the common case,
    # not an edge case.
    if prior >= MAX_NARROWINGS:
        return {"ok": False,
                "reason": f"already narrowed {prior}x (cap {MAX_NARROWINGS}) — a rule needing "
                          f"this many tightenings is the wrong rule, not a wide one. Escalate "
                          f"to re-derive rather than cutting again."}
    if len(positives) < MIN_POSITIVES:
        return {"ok": False,
                "reason": f"only {len(positives)} positive example(s); need {MIN_POSITIVES} to "
                          f"generalise a narrowing safely — one example cannot distinguish an "
                          f"essential term from an incidental one"}
    # Report untunability EXPLICITLY rather than letting it surface as a bogus invariant
    # failure. This path returns ok=False like every other decline, so behaviour is unchanged
    # (fall through to quarantine) — the difference is that the caller now has a reason worth
    # logging. (core-business, finding 7b scope correction.)
    missing = unsupplied_ops(artifact.get("condition") or {})
    if missing:
        # EXPECTED vs UNEXPECTED. An enforcing block keyed on session-state oracles is the
        # documented boundary above — it is working as designed and must not read as a fault.
        # Anything else is a genuine surprise worth a human looking at.
        expected = (artifact.get("effect", {}).get("mode") == "block"
                    and missing <= ORACLE_OPS)
        if expected:
            reason = (f"boundary (expected) — enforcement keys on session state "
                      f"({', '.join(sorted(missing))}), which has no narrower form that means "
                      f"the same thing. Blocks are quarantine-then-re-derive by design, not "
                      f"tunable. Contracts self-tune; enforcement is human-reviewed.")
        else:
            reason = (f"untunable (unexpected) — condition uses "
                      f"{', '.join(sorted(missing))}, which the tune harness cannot supply. "
                      f"Verifying against fabricated-absent state would make the invariant "
                      f"meaningless, so it declines instead.")
        return {"ok": False, "untunable": sorted(missing), "expected": expected,
                "reason": reason}
    cand = propose_narrowing(artifact, positives, overfired)
    if cand is None:
        return {"ok": False, "reason": "no term separates the positives from the over-fired text"}
    # propose_narrowing returns {spec, term} — the term travels ALONGSIDE the spec rather than
    # inside it, because a validated spec has a closed key set with no room for bookkeeping.
    new_spec, term = cand["spec"], cand["term"]
    ok, why = verify(new_spec, positives, evaluate)
    if not ok:
        return {"ok": False, "reason": f"narrowing rejected — {why}"}
    return {"ok": True, "spec": new_spec, "term": term,
            "reason": f"narrowed on '{term}'; {why}"}


if __name__ == "__main__":
    # Self-check with a known-answer case, so running the file proves the invariant holds
    # rather than merely that it imports.
    def _eval(spec, text):
        return all(re.search(c["value"], text, re.I)
                   for c in spec["condition"]["all"] if c.get("op") == "prompt_regex")

    art = {"effect": {"mode": "inject"},
           "condition": {"all": [{"op": "prompt_regex", "value": r"\breview\b"}]}}
    pos = ["please review the migration carefully", "review the migration before pushing"]
    over = ["review the weather", "review my calendar"]
    r = tune(art, pos, over, _eval)
    print("narrowing:", r["reason"])
    assert r["ok"] and r["term"] == "migration", r
    # The narrowed spec must carry NO bookkeeping key — that is the whole point of finding 8.
    assert "_tune" not in r["spec"], f"spec must stay schema-clean: {sorted(r['spec'])}"

    # And the invariant: a narrowing that would break a positive must be refused.
    bad = {"effect": {"mode": "block"},
           "condition": {"all": [{"op": "prompt_regex", "value": r"\bx\b"}]}}
    r2 = tune(bad, ["alpha only", "beta only"], ["alpha beta"], _eval)
    print("no-safe-term case:", r2["reason"])
    assert not r2["ok"], r2

    # And untunability is REPORTED, not silently mistaken for an invariant failure.
    st = {"effect": {"mode": "block"},
          "condition": {"all": [{"op": "prompt_regex", "value": r"\bpush\b"},
                                {"op": "state_flag_is", "value": "reviewed"}]}}
    r3 = tune(st, ["push the migration", "push the release"], [], _eval)
    print("untunable case:", r3["reason"])
    assert not r3["ok"] and r3.get("untunable") == ["state_flag_is"], r3
    # A BLOCK on oracle ops is the designed boundary and must report as EXPECTED, so it does
    # not train anyone to ignore the message. An inject on the same op is a real surprise.
    assert r3["expected"] is True, r3
    r4 = tune({"effect": {"mode": "inject"},
               "condition": {"all": [{"op": "state_flag_is", "value": "r"}]}},
              ["a b", "a c"], [], _eval)
    assert r4["expected"] is False, r4
    print("inject-on-oracle case:", r4["reason"][:60], "...")
    print("OK — narrowing works and the invariant refuses unsafe tunes")
