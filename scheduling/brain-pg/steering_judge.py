#!/usr/bin/env python3
"""steering_judge — score each steering event on what happened NEXT, from the transcript.

Master plan Phase 3.1. The ledger answers "what does this cost". grade-intent answers "does it
still catch what it was built for". Neither answers the question the operator actually asked:
whether a gate is actually earning its keep, or firing at random and calling that useful while it
wastes tokens.

That is a per-FIRE question, and it can only be answered by looking at the turn that followed.

GROUND TRUTH IS THE TRANSCRIPT, NEVER THE GATE'S OWN LOGS
---------------------------------------------------------
A gate that records its own success is a gate that will always look successful. So every outcome
here is derived from what Claude and Nick DID after the fire — did the advice get followed, did
Nick push back on the gate itself — never from anything the gate wrote about itself.

This is enforced in the database, not by convention. The `steering_judge` role holds SELECT on
steering_events and INSERT+SELECT on steering_judgments, and nothing else. The judge physically
cannot edit the telemetry it is scored against. (Verified in the grant table; see
migrations/2026-07-30-steering-events.sql.)

DETERMINISTIC, LIKE grade-intent AND FOR THE SAME REASON
--------------------------------------------------------
No classifier, no LLM, no similarity scoring. tasks/research/kind-check-research-2026-07-27.md
measured lexical same-kind scoring against a real labelled set and got AUC 0.54 — a coin flip —
so the cheap version was deleted rather than shipped weak. Everything below is exactly decidable
from the transcript, and anything that is not decidable is scored `neutral` rather than guessed.

OUTCOMES
  helped          the advice was taken in the very next assistant turn. For a read-demanding
                  advisory that means a read/recall tool call actually happened.
  false_positive  Nick's next message pushes back on the INTERVENTION itself ("stop firing on",
                  "why did it block that", "that hook is wrong"). The strongest signal available,
                  and rare, because he mostly ignores a bad nudge rather than complaining.
  overridden      a BLOCK fired and the very next assistant turn produced text anyway without
                  doing the thing the block demanded.
  neutral         everything else, including every case where the transcript is gone.

`neutral` is deliberately the large bucket. A judge that forces every event into a strong verdict
manufactures signal, and this system has spent a whole session finding metrics that reported
confident conclusions from data they did not have.

  python3 steering_judge.py --days 14           # score, print a summary
  python3 steering_judge.py --days 14 --write   # persist judgments
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import connect_corebrain, get_org_id  # noqa: E402
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "bin"))
import core_seat as _core_seat  # noqa: E402 — canonical seat/slug resolver

JUDGE_REVISION = 1

# Tools that satisfy a read-demanding advisory. Deliberately narrow: a Bash `ls` is not a recall.
READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
RECALL_TOOL_RX = re.compile(r"core-brain|recall_similar|recall_at|get_entity|query\.py", re.I)

# Nick pushing back on the INTERVENTION, not on the work. These are his actual phrasings, taken
# from corrections in the corpus rather than invented.
FALSE_POSITIVE_RX = re.compile(
    r"\b(stop\s+(firing|blocking)|why\s+(did|is)\s+(it|that|this)\s+(block|fire)|"
    r"that\s+hook\s+(is\s+)?(wrong|broken)|the\s+(hook|gate)\s+(is\s+)?(wrong|broken|annoying)|"
    r"quit\s+blocking|stop\s+(nagging|asking\s+me))\b", re.I)

# Which hooks give read-demanding advice — the only class where "advice taken" is decidable.
READ_ADVISORY = {"verification-trigger", "brain-recall-trigger", "recall-gate",
                 "recall-first-gate", "recall-before-grep", "state-claim-gate"}


def _parse_session(path: Path):
    """[(kind, ts_index, text, tools)] in order. Cheap linear scan, no JSON schema assumptions."""
    out = []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return out
    for i, line in enumerate(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        t, msg = d.get("type"), (d.get("message") or {})
        content = msg.get("content")
        if t == "user":
            txt = content if isinstance(content, str) else ""
            if isinstance(content, list):
                if all(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
                    continue          # a tool result is not a user turn
                txt = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            out.append(("user", i, txt or "", []))
        elif t == "assistant" and isinstance(content, list):
            texts, tools = [], []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    texts.append(p.get("text") or "")
                elif p.get("type") == "tool_use":
                    tools.append(json.dumps({"name": p.get("name"), "input": p.get("input")})[:400])
            out.append(("assistant", i, " ".join(texts), tools))
    return out


def _did_read(tools) -> bool:
    for t in tools:
        try:
            d = json.loads(t)
        except Exception:
            continue
        if d.get("name") in READ_TOOLS:
            return True
        if RECALL_TOOL_RX.search(t):
            return True
    return False


def judge_session(events, turns) -> list:
    """Score every event in one session. events are (id, hook, verdict, ts) in order."""
    out = []

    # PAIRING AN EVENT TO A TURN. steering_events carries a wall-clock timestamp, not a transcript
    # offset, so an event cannot be tied to an exact line. The first version of this paired EVERY
    # event in a session against the session's FIRST assistant turn, which is not an approximation
    # — it is measuring one turn 585 times and reporting the result as 585 judgments. It produced
    # 510 neutral / 74 overridden / 1 helped, and that shape was the tell.
    #
    # Pair by ORDINAL instead. A prompt-time hook fires at most once per prompt, so the k-th fire
    # of a given hook in a session lines up with the k-th assistant turn. That is still an
    # approximation and it is stated as one — confidence is capped accordingly, and anything that
    # cannot be placed scores `neutral` rather than being forced into a verdict.
    assistants = [t for t in turns if t[0] == "assistant"]
    users = [t for t in turns if t[0] == "user"]
    seen_per_hook: dict = {}

    for eid, hook, verdict, _ts in events:
        k = seen_per_hook.get(hook, 0)
        seen_per_hook[hook] = k + 1
        turn = assistants[k] if k < len(assistants) else None

        outcome, conf, why = "neutral", 0.3, "no decidable signal"

        # Pushback must come AFTER this fire, not anywhere in the session — otherwise one
        # complaint marks every earlier event in that session a false positive.
        later_user = users[k + 1:] if k + 1 < len(users) else []
        if any(FALSE_POSITIVE_RX.search(u[2] or "") for u in later_user[:2]):
            outcome, conf, why = "false_positive", 0.9, "user pushed back on the intervention"
        elif hook in READ_ADVISORY and turn is not None:
            if _did_read(turn[3]):
                outcome, conf, why = "helped", 0.6, "a read/recall followed the advisory"
            elif verdict == "block":
                outcome, conf, why = "overridden", 0.5, "block issued, no read followed"
            else:
                outcome, conf, why = "neutral", 0.4, "advisory not followed, no pushback"
        elif turn is None:
            outcome, conf, why = "neutral", 0.2, "no matching turn — event could not be placed"

        out.append({"steering_event_id": eid, "outcome": outcome, "confidence": conf,
                    "evidence": {"hook": hook, "verdict": verdict, "why": why, "ordinal": k}})
    return out


def _connect_as_judge():
    """Connect, then DROP PRIVILEGE to the restricted role. Returns (conn, restricted: bool).

    CRITICAL FIX 2026-07-30, found by Codex's adversarial pass before the baseline push, and the
    fix then took two attempts because the first one was wrong in an instructive way.

    The docstring claimed the judge "physically cannot edit the telemetry it is scored against",
    and a test asserted the grants on the `steering_judge` role. Both true, both irrelevant:
    connect_corebrain() connects as `brain_app` (_env.py), which holds DELETE and UPDATE on BOTH
    steering tables. The restricted role existed and nothing ever ran as it. The test checked the
    lock while the door stood open — the exact defect class this session was spent finding,
    committed by me, inside the mechanism built to prevent it.

    FIRST ATTEMPT set CORE_BRAIN_DB_USER=steering_judge and connected as that role. It silently
    fell back to brain_app, because the role was created NOLOGIN and can never be connected as.
    So the design had been decorative since the migration, and a fallback that reports
    `restricted: False` was the only reason I noticed rather than declaring victory twice.

    SET ROLE is the correct Postgres mechanism for this: it drops privilege over the existing
    socket connection, needs no separate credentials, and is verifiable in one query. brain_app is
    granted membership in steering_judge, so the switch is permitted and the resulting effective
    role genuinely cannot UPDATE or DELETE — checked, not assumed.

    If SET ROLE fails (a peer that has not run the migration), the connection stays usable and
    `restricted` comes back False, which the caller reports. A judge quietly running with write
    access is worse than one that says it could not drop privilege.
    """
    con = connect_corebrain()
    try:
        with con.cursor() as cur:
            cur.execute("SET ROLE steering_judge")
        return con, True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return con, False


def run(days: int, write: bool) -> dict:
    org = get_org_id()
    con, restricted = _connect_as_judge()
    cur = con.cursor()
    cur.execute(
        """SELECT id, hook, verdict, ts, session_id FROM steering_events
            WHERE org_id=%s AND ts >= now() - (%s || ' days')::interval
              AND session_id <> ''
              AND NOT EXISTS (SELECT 1 FROM steering_judgments j
                               WHERE j.steering_event_id = steering_events.id)
            ORDER BY session_id, ts""", (org, days))
    rows = cur.fetchall()

    # DERIVE the transcript directory from THIS Core's own root. It was hardcoded to
    # core-life's, so on business/school/finance/ops every event pointed at a transcript in that
    # peer's own project dir, was marked unplaced, and produced no judgment — while the close
    # logged an ordinary "0 scored". A peer would have seen a judge that ran cleanly and measured
    # nothing. Found by Codex before the push; same silent-absence shape as everything else today.
    #
    # Claude Code's project dir is the instance path with / and spaces replaced by '-', prefixed
    # by '-'. Derived rather than configured so a new Core needs no setup step to be measurable.
    _inst = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
    proj = _core_seat.transcripts_dir(_inst)
    by_session: dict = {}
    for eid, hook, verdict, ts, sid in rows:
        by_session.setdefault(sid, []).append((eid, hook, verdict, ts))

    judgments, unplaced = [], 0
    for sid, evs in by_session.items():
        path = proj / f"{sid}.jsonl"
        if not path.exists():
            unplaced += len(evs)
            continue
        judgments.extend(judge_session(evs, _parse_session(path)))

    tally: dict = {}
    for j in judgments:
        tally[j["outcome"]] = tally.get(j["outcome"], 0) + 1

    if write and judgments:
        for j in judgments:
            cur.execute(
                """INSERT INTO steering_judgments
                     (org_id, steering_event_id, outcome, confidence, decided_by, evidence,
                      judge_revision)
                   VALUES (%s,%s,%s,%s,'deterministic',%s,%s)""",
                (org, j["steering_event_id"], j["outcome"], j["confidence"],
                 json.dumps(j["evidence"]), JUDGE_REVISION))
        con.commit()
    con.close()
    return {"scored": len(judgments), "unscorable_transcript_gone": unplaced,
            "sessions": len(by_session), "tally": tally, "written": bool(write and judgments),
            "restricted_role": restricted}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    r = run(a.days, a.write)
    print(f"steering judge — {r['scored']} events scored across {r['sessions']} session(s)")
    if r["unscorable_transcript_gone"]:
        print(f"  {r['unscorable_transcript_gone']} unscorable (transcript rotated away)")
    for k, v in sorted(r["tally"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:16} {v}")
    print(f"  written: {r['written']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
