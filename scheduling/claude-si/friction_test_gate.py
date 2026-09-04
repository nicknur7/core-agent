#!/usr/bin/env python3
"""friction_test_gate.py — P3 test-gate (v1). An artifact is installable ONLY if it fires on
every positive example and on ZERO negatives, evaluated through the SAME static dispatcher that
runs in production. This is the "gated by your own history, not by a human" safety mechanism.

  CORE_ORG_ID=1 python3 friction_test_gate.py            # route+gate all, report
  CORE_ORG_ID=1 python3 friction_test_gate.py --cases <path>   # override the default

Default cases path is friction_installer.CASES (.claude/state/friction-cases.jsonl) — per-Core,
outside the shared scheduling/claude-si/ dir so it is neither pushed to the baseline nor
clobbered by a pull.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import friction_dispatch as fd
import friction_installer as fi
import friction_router as fr


OVERBROAD_RATE = 0.03  # a rule may fire on at most this fraction of REAL corpus prompts
MIN_CORPUS = 40        # a fire-rate check on a tiny sample is meaningless — need a real sample

# "NO CONCLUSION EXISTS" IS NOT "FAILED" (2026-08-12, found by core-finance).
#
# gate() returns (False, reason) for three materially different things:
#
#   DECIDED-BAD      "over-broad: fires on 83/150 real prompts (55%)"   the artifact is unsafe
#   MALFORMED SPEC   "duplicate example ids"                           the submission is broken
#   CANNOT YET TELL  "corpus too small (31<40)"                        NOTHING was determined
#
# The caller logged all three as `test_fail` with no class field, and nothing read the reason. On
# core-finance that produced 234 `test_fail` rows of which **234 were undecidable** — two artifacts
# retried 117 times each between 08-04 and 08-12, 100 of them on the last day alone, against a
# corpus that grew 27 -> 31 versus a threshold of 40. An unsatisfiable precondition retried as
# though it were transient, with the retry rate RISING.
#
# The wasted work is the smaller cost. Anyone reading that log sees 234 specificity failures against
# two artifacts and concludes the seat has two badly-broken rules; the truth is the opposite — those
# two have never been tested at all. A correct refusal, recorded as a verdict it never reached.
#
# THE MARKER IS A PREFIX RATHER THAN A THIRD RETURN VALUE because seven call sites unpack
# `ok, why = gate(...)`, several of them tests on peer seats that are mid-flight. Widening the tuple
# would break every one of them at once for a classification only the installer needs. The prefix is
# authored HERE, beside the messages it marks, and `is_undecidable` reads the same constant — so the
# message and the predicate that classifies it cannot drift apart the way a matcher kept at a
# distance from its strings always eventually does.
#
# It stays a refusal in every case. An untested artifact must never install; the fix is to record
# WHY it did not, not to let it through.
UNDECIDABLE = "UNDECIDABLE: "


def is_undecidable(why: str) -> bool:
    """True when a gate refusal means 'no conclusion exists yet', not 'this failed'."""
    return str(why or "").startswith(UNDECIDABLE)


def gate(spec: dict, examples: dict, corpus_prompts: list | None = None) -> tuple[bool, str]:
    """True iff all positives fire, no negative fires, AND the rule is SPECIFIC against the real
    corpus (Codex 3rd review): it must fire on <=3% of a sample of actual past prompts pulled from
    the trusted corpus. This is grounded in real data, not caller-labeled 'real_neighbor' strings or
    a keyword-order heuristic — an over-broad rule is caught even if its synthesized examples pass."""
    pos = examples.get("positive", [])
    neg = examples.get("negative", [])
    if len(pos) < 1 or len(neg) < 2:
        return False, "need >=1 positive and >=2 negatives"
    provs = {e.get("provenance") for e in neg}
    if len(provs) < 2:
        return False, "negatives lack diversity (>=2 distinct provenances)"
    tests = spec.get("tests", {})  # EXACT-bind example ids to declared test ids (Codex 4th/5th review)
    pos_ids, neg_ids = [e.get("id") for e in pos], [e.get("id") for e in neg]
    dpos, dneg = tests.get("positive_ids", []), tests.get("negative_ids", [])
    if len(set(pos_ids)) != len(pos_ids) or len(set(neg_ids)) != len(neg_ids):
        return False, "duplicate example ids"
    if len(set(dpos)) != len(dpos) or len(set(dneg)) != len(dneg):
        return False, "duplicate declared test ids"  # ["p1","p1"] must not satisfy set-equality
    if set(pos_ids) != set(dpos):
        return False, "positive example ids must EXACTLY equal spec.tests.positive_ids"
    if set(neg_ids) != set(dneg):
        return False, "negative example ids must EXACTLY equal spec.tests.negative_ids"
    cond = spec.get("condition", {})
    # CORPUS-GROUNDED specificity: fire-rate against a REAL, sufficiently-large sample must be low.
    if corpus_prompts:
        if len(corpus_prompts) < MIN_CORPUS:
            return False, f"{UNDECIDABLE}corpus too small ({len(corpus_prompts)}<{MIN_CORPUS}) to prove specificity"
        event = spec.get("event")
        field = "prompt" if event == "UserPromptSubmit" else "assistant_text"
        fires = sum(1 for p in corpus_prompts
                    if fd.evaluate(cond, fd._normalize({field: p, "event": event}, event)))
        rate = fires / max(1, len(corpus_prompts))
        if rate > OVERBROAD_RATE:
            return False, f"over-broad: fires on {fires}/{len(corpus_prompts)} real prompts ({rate:.0%})"
    elif corpus_prompts is not None:
        return False, f"{UNDECIDABLE}no corpus sample available to prove specificity"
    for ex in pos:
        if ex.get("expected") != "fire":
            return False, f"positive {ex.get('id')} not labeled 'fire'"
        if not fd.evaluate(cond, fd.normalize_for_test(ex["hook_input"], ex["event"])):
            return False, f"positive {ex.get('id')} did NOT fire"
    for ex in neg:
        if ex.get("expected") != "no_fire":
            return False, f"negative {ex.get('id')} not labeled 'no_fire'"
        if fd.evaluate(cond, fd.normalize_for_test(ex["hook_input"], ex["event"])):
            return False, f"negative {ex.get('id')} ({ex.get('provenance')}) WRONGLY fired"
    if spec["effect"]["mode"] == "block" and not ({"event_mismatch", "polarity_mutation"} <= provs):
        return False, "blocker lacks required negative diversity"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(fi.CASES))
    args = ap.parse_args()
    p = Path(args.cases)
    if not p.exists():
        print(f"no cases file {p}")
        return 1
    from collections import Counter
    stats = Counter()
    by_type = Counter()
    samples = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        spec = fr.route(case)
        if spec is None:
            stats["not_routable"] += 1
            continue
        examples = spec.pop("_examples")
        ok, why = gate(spec, examples)
        stats["gated_pass" if ok else "gated_fail"] += 1
        by_type[(spec["type"], spec["event"], "PASS" if ok else "FAIL")] += 1
        if ok and len(samples) < 3:
            samples.append((spec, examples))
        elif not ok and stats["gated_fail"] <= 2:
            samples.append((spec, examples, why))
    print(f"test-gate over {p.name}: {dict(stats)}")
    print("by (type, event, verdict):")
    for k, v in sorted(by_type.items()):
        print(f"    {k}: {v}")
    for s in samples[:4]:
        spec = s[0]
        note = f" FAIL: {s[2]}" if len(s) == 3 else " PASS"
        print(f"  {spec['type']}/{spec['event']} mode={spec['effect']['mode']}{note}")
        print(f"    cond={json.dumps(spec['condition'])[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
