#!/usr/bin/env python3
"""friction_miner.py — P1 miner. Turns already-detected corrections (pattern_observations,
which carry source_file + source_uuid) into rich friction_case objects, by reconstructing
the MOMENT (friction_jsonl) and deriving a first-pass trigger_context from the corrected
turn's tool activity. The router (P2) refines type/event/condition + full negatives; this
layer's job is faithful CAPTURE of the moment + a candidate event, failing closed when the
transcript is gone (rotation) or the chain is incomplete.

Org-scoped (CORE_ORG_ID). Idempotent: case_id = sha256(source_file|source_uuid|detector).
Output: JSONL of friction_case objects (schema: friction-case.schema.json) for inspection;
the DB table lands in a follow-up migration.

  CORE_ORG_ID=1 python3 friction_miner.py --days 14 --out friction-cases.jsonl
  CORE_ORG_ID=1 python3 friction_miner.py --days 14 --summary   # counts + samples, no write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "brain-pg"))
from _env import connect_corebrain, get_org_id  # noqa: E402
import friction_installer as fi  # noqa: E402
import friction_jsonl as fj  # noqa: E402

DETECTOR_VERSION = "friction-miner/1"

# Tools whose use means the agent was MUTATING state (→ a PreToolUse-blockable moment).
MUTATING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}
# mcp write-ish tools are mutating too; read tools are not.
_READONLY_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "TodoWrite"}

# pattern_label → first-pass candidate event (router refines).
LABEL_EVENT = {
    "correction-stop-execution": ("before_tool", "PreToolUse"),
    "correction-this-is-wrong": ("after_claim", "Stop"),
    "correction-already-told-you": ("task_start", "UserPromptSubmit"),
    "recall-miss": ("task_start", "UserPromptSubmit"),
    "correction-should-have": ("task_start", "UserPromptSubmit"),
    "hallucination-state-claim": ("after_claim", "Stop"),
    "hallucination-say-do-gap": ("after_claim", "Stop"),
}


def _tool_mutability(tool_uses: list[str]) -> str | None:
    if not tool_uses:
        return None
    return "mutating" if any(t in MUTATING_TOOLS for t in tool_uses) else "readonly"


def _derive_trigger_context(label: str, moment) -> dict:
    """First-pass stage + candidate_event. Uses the label, then refines with tool activity:
    a mutating-tool turn corrected with a stop/redirect → PreToolUse regardless of label."""
    mut = _tool_mutability(moment.tool_uses)
    stage, event = LABEL_EVENT.get(label, ("ambiguous", "UserPromptSubmit"))
    # refine: if the corrected turn was actively mutating and the correction is a halt/redirect,
    # the friction lives BEFORE the tool call → PreToolUse.
    if mut == "mutating" and label in ("correction-stop-execution", "correction-frustration",
                                       "correction-explicit-no", "correction-not-what-i-want"):
        stage, event = "before_tool", "PreToolUse"
    elif label == "correction-frustration" and not moment.tool_uses:
        stage, event = "after_claim", "Stop"
    return {
        "stage": stage,
        "candidate_event": event,
        "tool_name": (moment.tool_uses[-1] if moment.tool_uses else None),
        "tool_mutability": mut,
        "claim_kind": None,
        "evidence_block_uuid": None,
    }


def _case_id(source_file: str, source_uuid: str) -> str:
    return "fc_" + hashlib.sha256(f"{source_file}|{source_uuid}|{DETECTOR_VERSION}".encode()).hexdigest()[:24]


def build_case(org: int, source_file: str, source_uuid: str, label: str, moment,
               canonical_ask: str | None = None) -> dict:
    """One friction case. `canonical_ask` is the DISTILLED form of what Nick wanted.

    WHY IT IS THREADED THROUGH (2026-07-30). This system already had a distillation channel and
    was not using it in the channel that needed it most. ask_miner writes canonical_ask onto
    pattern_observations — 287 of 443 live rows carry one, and they read as instructions:

        "act autonomously and execute without pausing to ask permission"
        "always run the full close-reconciler at session close"
        "apply fixes to all cores immediately, not deferred to next pull"

    Meanwhile the friction router built its injected message from `user_wanted`, which is the RAW
    correction text. Same corpus, two channels, one distilling and one quoting — and the quoting
    one is the half that produced every artifact purged today, profanity and typos included.

    So this is not a new mechanism, it is the consolidation Nick names as his most recurring
    directive: unify onto the clean path rather than adding another filter beside the old one.
    The length/profanity/first-person gates in friction_router stay as belt-and-braces, but they
    stop being the primary defence — because a distilled ask is not a quote in the first place.
    """
    blocks = [{"role": b["role"], "kind": b["kind"], "uuid": b.get("uuid"),
               "text": (fj.redact(b["text"])[:2000] if b.get("text") else None), "tool_name": b.get("tool_name")}
              for b in moment.blocks[-30:]]  # cap the window
    content_sha = hashlib.sha256((moment.prompt_text + "|" + moment.correction_text).encode()).hexdigest()
    corr_red = fj.redact(moment.correction_text)  # redact secrets before storing/injecting (Codex #12)
    tc = _derive_trigger_context(label, moment)
    return {
        "schema_version": 1,
        "case_id": _case_id(source_file, source_uuid),
        "org_id": org,
        "status": "mined" if moment.complete else "ineligible",
        "source": {"transcript_path": source_file, "session_id": Path(source_file).stem,
                   "correction_uuid": source_uuid, "correction_timestamp": None,
                   "detector_version": DETECTOR_VERSION, "content_sha256": content_sha},
        "moment": {"blocks": blocks, "correction": corr_red[:4000],
                   "window_before": None, "window_after": None},
        "trigger_context": tc,
        "agent_did_wrong": (fj.redact(moment.blocks[-1]["text"])[:400] if (moment.blocks and moment.blocks[-1].get("text")) else
                            f"turn used tools: {moment.tool_uses[:8]}"),
        "user_wanted": corr_red[:400],
        # The distilled form, when ask_miner has produced one. friction_router mints from THIS
        # and refuses when it is absent — see its DISTILLED-OR-SILENT block.
        "canonical_ask": (canonical_ask or "").strip() or None,
        # distinct_sessions/positive_case_ids are placeholders here and get their real values in
        # compute_support() below, once every case in this run is known. Before 2026-07-30 they
        # were placeholders that nothing ever replaced — see that function's header.
        "support": {"cluster_key": label, "distinct_sessions": 1, "positive_case_ids": []},
        "positive_examples": [],   # populated at clustering/routing (P2)
        "negative_examples": [],
        "quality": {"parser_complete": moment.complete, "confidence": 0.9 if moment.complete else 0.2,
                    "ambiguities": ([] if moment.complete else ["chain_incomplete"]),
                    "eligible_for_routing": bool(moment.complete)},
    }


_SUPPORT_STOP = {
    "that", "this", "with", "have", "want", "just", "like", "what", "when", "your", "from",
    "then", "them", "they", "there", "here", "will", "would", "should", "could", "make",
    "need", "give", "take", "look", "know", "think", "into", "over", "back", "also", "been",
    "does", "done", "than", "some", "more", "very", "much", "even", "only", "before", "after",
    "about", "because", "which", "where", "these", "those", "still", "again", "thing", "things",
}


def compute_support(cases: dict) -> None:
    """Fill in each case's real cluster support, in place.

    THE DEFECT THIS EXISTS TO FIX (found 2026-07-30, master plan Phase 0.6).

    build_case() wrote a hardcoded {"distinct_sessions": 1, "positive_case_ids": []} with a
    comment saying "populated at clustering/routing (P2)". P2 was never built. So every case in
    the pool — all 44 of them — carried "this happened once, in one session, with no siblings",
    and friction_router minted from them anyway and labelled the result:

        "Recurring expectation (instruction-directive): ..."

    Nothing in that sentence was true. Every artifact the friction lane has ever produced was a
    ONE-OFF quote of a single prompt, presented to the model as an established pattern. That is
    the complete explanation for the 19 undistilled artifacts purged earlier today AND for the
    three minted after the purge with typos still in them (colloablorate, reced, codext) — a
    typo is the signature of a single occurrence, and there was no recurrence test to catch it.

    It also explains why the mint gate's TRIGGER GROUNDING check never fired: it reads
    support["members"], which no case has ever had, so `if _members:` was False and the whole
    check was skipped. A gate reading a field nobody writes is indistinguishable from no gate.

    WHAT SUPPORT NOW MEANS. Cases group by cluster_key (the correction label). Within a group,
    distinct_sessions counts how many DIFFERENT sessions contributed — the real question, since
    ten corrections inside one bad afternoon are one event, not ten. positive_case_ids lists the
    siblings, and members carries their text so the router's grounding check has something to
    read. The router additionally requires the trigger terms to recur across siblings, so a
    coarse label grouping cannot on its own manufacture a pattern.
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for c in cases.values():
        groups[c["support"]["cluster_key"]].append(c)
    for key, members in groups.items():
        sessions = {m["source"].get("session_id") for m in members}
        sessions.discard(None)
        ids = [m["case_id"] for m in members]
        texts = [{"case_id": m["case_id"], "prompt_text": m.get("user_wanted") or ""}
                 for m in members]
        for m in members:
            m["support"]["distinct_sessions"] = len(sessions)
            m["support"]["positive_case_ids"] = [i for i in ids if i != m["case_id"]]
            m["support"]["members"] = [t for t in texts if t["case_id"] != m["case_id"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    # Defaults to the per-Core state path so no Core has to know where cases live (and so
    # nobody re-parks personal data in the shared scheduling/claude-si/ dir). --summary
    # still suppresses the write.
    ap.add_argument("--out", default=str(fi.CASES))
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    org = get_org_id()
    con = connect_corebrain(); cur = con.cursor()
    cur.execute(
        """SELECT source_file, source_uuid, pattern_label, canonical_ask
             FROM pattern_observations
            WHERE org_id = %s AND source_file LIKE '%%.jsonl' AND source_uuid IS NOT NULL
              AND COALESCE(session_date, created_at::date) >= (CURRENT_DATE - %s)
            ORDER BY created_at DESC""",
        (org, args.days))
    rows = cur.fetchall()
    con.close()

    cache: dict = {}
    cases: dict = {}   # case_id -> case (dedup)
    stats = {"rows": len(rows), "file_gone": 0, "file_bad": 0, "uuid_missing": 0,
             "mined": 0, "ineligible": 0, "dup": 0}
    from collections import Counter
    by_event = Counter()
    for sf, su, label, canon in rows:
        p = Path(sf)
        if not p.exists():
            stats["file_gone"] += 1
            continue
        if sf not in cache:
            cache[sf] = fj.parse_file(sf)
        r = cache[sf]
        if not r.ok:
            stats["file_bad"] += 1
            continue
        m = fj.reconstruct_moment(su, r.by_uuid)
        if m is None:
            stats["uuid_missing"] += 1
            continue
        case = build_case(org, sf, su, label, m, canonical_ask=canon)
        cid = case["case_id"]
        if cid in cases:
            stats["dup"] += 1
            continue
        cases[cid] = case
        if m.complete:
            stats["mined"] += 1
            by_event[case["trigger_context"]["candidate_event"]] += 1
        else:
            stats["ineligible"] += 1

    compute_support(cases)

    print(f"friction-miner (org {org}, {args.days}d): {stats}")
    print(f"candidate events (eligible cases): {dict(by_event)}")
    _rec = sum(1 for c in cases.values() if c["support"]["distinct_sessions"] >= 2)
    print(f"cluster support: {_rec}/{len(cases)} cases recur across >=2 sessions "
          f"(only these are mintable)")
    eligible = [c for c in cases.values() if c["quality"]["eligible_for_routing"]]
    for c in eligible[:3]:
        tc = c["trigger_context"]
        print(f"  · [{c['support']['cluster_key']}] event={tc['candidate_event']} "
              f"mut={tc['tool_mutability']} tools={[b['tool_name'] for b in c['moment']['blocks'] if b['kind']=='tool_use'][:5]}")
        print(f"      wanted: {c['user_wanted'][:70]!r}")

    if args.out and not args.summary:
        outp = Path(args.out)
        with outp.open("w") as f:
            for c in cases.values():
                f.write(json.dumps(c) + "\n")
        print(f"wrote {len(cases)} friction cases -> {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
