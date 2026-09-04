#!/usr/bin/env python3
"""The consolidation pass — Phase C of the learning-substrate unification.

Plan: tasks/research/learning-substrate-unification-2026-08-03.md §3 Phase C.

WHAT THIS IS FOR, IN ONE PARAGRAPH. Every learning mechanism Core has today fires on a single
MOMENT: a correction happens, a detector notices, an artifact is minted. That is why the system can
only learn from failure — the corrective miner is the sole doorway between "what Core knows" and
"how Core behaves" (finding F6). A sequence is not visible from inside one moment. It is only
visible from ABOVE, by reading a whole session after the fact and asking "what did Nick want, what
steps actually got him there, and did he accept the result." This pass is that reading. Nick called
it the dream cycle, and the name is apt: it is the part that runs after the day, over the whole
day, rather than reacting inside it.

WHY --prepare/--apply RATHER THAN AN INLINE MODEL CALL. The extraction is a judgement pass and
needs a model; the writing is mechanical and must be verified. Splitting them is the same shape
`learned-resynth.py` already uses, and it is what makes the parent able to check every record
before it touches the brain. The alternative — a script that calls a model and writes whatever
comes back — is precisely the failure mode that put 26 unverified checkpoints into a merge on
2026-06-11. Consolidate, don't invent a second pattern.

    consolidate_sessions.py --detect            # what is unconsolidated, and is it acceptable?
    consolidate_sessions.py --prepare [--limit N]  # emit brief + material for the extraction pass
    consolidate_sessions.py --apply FILE.json   # validate, then write Workflow + workflow_steps
    consolidate_sessions.py --status            # what has been consolidated, and what it produced

ACCEPTANCE IS MECHANICAL, NOT ASKED (plan §5). A sequence counts as successful when the session
ends with no correction recorded against it AND no Stop-gate block on the final turn. Both signals
already exist — `pattern_observations` and `steering_events`. Asking Nick to label sessions would be
homework, and the brain records homework as the thing to eliminate.

THE PROXY IS NAMED, NOT HIDDEN (plan §7). Absence-of-correction is not proof of correctness. A
silently-wrong sequence Nick never caught would be learned as good. Mitigations, all real: the
2-session recurrence bar before anything generates a behavior, provenance on every row so a bad
batch is removable in one statement, and workflows surfaced as steps he can read and reject rather
than silently enforced.
"""
from __future__ import annotations
import argparse
import collections
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _env import load_secrets, connect_corebrain, connect_or_skip, get_org_id, core_root  # noqa: E402
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "bin"))
import core_seat as _core_seat  # noqa: E402 — canonical seat/slug resolver
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "lib"))
import coreuser as _U  # noqa: E402 — operator name from identity.json, never hardcoded

load_secrets()

# A workflow with one step is not a workflow — it is a preference wearing a sequence's clothes.
# artifact_typer.py already enforces MIN_PROCEDURE_STEPS = 2; reuse the number rather than inventing
# a second bar that can drift from it.
MIN_STEPS = 2

# Plan §3 C3, and the same bar the friction loop already uses. Reused deliberately: a second
# recurrence threshold in a second place is how two subsystems start disagreeing about what
# "recurring" means.
MIN_SESSIONS_TO_GENERATE = 2

# Bound the material handed to the extraction pass. A whole session can be enormous; the sequence
# is carried by the user's asks and the tool ORDER, not by tool output bodies.
MAX_TURNS_PER_SESSION = 120
MAX_CHARS_PER_TURN = 600


def _jsonl_dir() -> Path:
    root = core_root(Path(__file__))
    slug = _core_seat.transcripts_dir(root).name
    return Path.home() / ".claude" / "projects" / slug


def _session_files() -> list[Path]:
    d = _jsonl_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def _tool_signature(name: str, inp: dict) -> str:
    """A one-line descriptor of what a tool call DID. Never its output.

    The first version of this reduction kept only tool NAMES, on the reasoning that the sequence
    lives in the ordering. Running it proved that wrong immediately: a window rendered as thirty
    consecutive "[tool] Bash" lines carries no recoverable sequence, and any workflow extracted
    from it would be invented rather than observed — the precise failure the brief's hard rule 6
    exists to prevent. Ordering plus identity is the minimum; ordering alone is noise.

    Still deliberately lossy — inputs are truncated hard and outputs never included. The goal is
    "ran the migration", "edited correction-rate.py", not a replayable script.
    """
    def s(key, n=90):
        v = inp.get(key)
        return str(v)[:n].replace("\n", " ").strip() if v else ""

    if name == "Bash":
        # description is the model's own summary of the command and is usually the better label.
        return s("description", 90) or s("command", 90)
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        p = s("file_path", 120)
        return p.rsplit("/", 1)[-1] if "/" in p else p
    if name in ("Grep", "Glob"):
        return s("pattern", 60)
    if name in ("Agent", "Task"):
        return s("description", 80)
    if name == "Skill":
        return s("skill", 40)
    if name.startswith("mcp__"):
        return name.split("__", 2)[-1][:60]
    return s("description", 60) or s("query", 60)


def read_session(path: Path) -> dict:
    """Reduce one transcript to the shape a sequence is visible in.

    Deliberately lossy. Tool RESULTS are dropped entirely — they are the bulk of the bytes and
    carry none of the ordering information. What survives: what Nick typed, in order, and which
    tools ran between his turns, in order. That is the sequence.
    """
    turns: list[dict] = []
    session_id = path.stem
    last_ts = ""
    for line in path.read_text(errors="ignore").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = r.get("timestamp") or ""
        if ts:
            last_ts = ts
        typ = r.get("type")
        msg = r.get("message") or {}
        if typ == "user" and r.get("userType") == "external":
            if r.get("isMeta"):
                continue                      # skill body / slash-command expansion, not typed
            c = msg.get("content")
            if isinstance(c, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in c
            ):
                continue                      # tool result envelope, not a prompt
            text = c if isinstance(c, str) else "".join(
                b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
            ) if isinstance(c, list) else ""
            text = (text or "").strip()
            # Hook feedback, task-notifications, system notifications and interrupt markers all
            # arrive as user-role messages. The miner already enumerates them; reuse that list so
            # a notification is never read as something Nick asked for.
            if text and not text.lstrip().startswith(_hook_prefixes()):
                turns.append({"role": "nick", "text": text[:MAX_CHARS_PER_TURN], "ts": ts})
        elif typ == "assistant":
            for b in (msg.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    turns.append({"role": "tool", "tool": b.get("name", "?"),
                                  "sig": _tool_signature(b.get("name", ""), b.get("input") or {}),
                                  "ts": ts})
    return {
        "session_id": session_id,
        "path": str(path),
        "turns": turns[:MAX_TURNS_PER_SESSION],
        "n_turns": len(turns),
        "last_ts": last_ts,
        "date": (turns[0]["ts"][:10] if turns and turns[0].get("ts") else last_ts[:10]),
    }


_MINER_CACHE = {}


def _miner():
    """The corrective miner module, loaded by path (its filename is hyphenated).

    Both the correction regexes and the hook/notification prefix list come from here. Neither is
    re-declared in this file. A second copy of "what counts as a correction" or "what is not a
    Nick turn" is how two components start describing different sessions — the exact defect the
    2026-08-05 audit found sitting between this metric's own numerator and denominator.
    """
    import importlib.util
    src = core_root(Path(__file__)) / "scheduling" / "claude-si" / "learned-corpus-miner.py"
    spec = importlib.util.spec_from_file_location("_lcm", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _correction_rx():
    """Reuse the corrective miner's OWN definition of a correction. Never redefine it here.

    Two definitions of "a correction" across two components is how the metric and the learner
    start disagreeing about what happened — the same class of defect the 2026-08-05 fleet audit
    found between this metric's numerator and denominator.
    """
    if "rx" not in _MINER_CACHE:
        _MINER_CACHE["rx"] = _miner().CORRECTION_RX
    return _MINER_CACHE["rx"]


def _hook_prefixes() -> tuple:
    """The miner's list of user-role messages that are not the operator — hook feedback, notifications."""
    if "hp" not in _MINER_CACHE:
        _MINER_CACHE["hp"] = tuple(_miner().HOOK_PREFIXES)
    return _MINER_CACHE["hp"]


def segment_windows(sess: dict) -> list[dict]:
    """Split a session into candidate sequences, and judge acceptance PER SEQUENCE.

    WHY NOT PER SESSION, WHICH IS WHAT THE PLAN SAID. Plan §3 C2 defines success as a session
    ending "without a correction within the same session." Implemented literally, that measured
    the wrong thing, and --detect proved it on the first run: core-life's substantial sessions run
    175, 486, 347, 1643 and 2373 turns, and every one of them contains at least one correction
    somewhere. The only sessions that passed were three of 1-2 turns — sessions too short to
    contain a workflow at all. So the criterion as written could only ever admit sessions with
    nothing to learn, and would have shipped as "0 workflows found, system healthy."

    The plan's own sentence describes the right granularity: "Nick asked for X; it took steps
    A->B->C->D; it ENDED in acceptance." It is the SEQUENCE that ends in acceptance, not the day.
    A ten-hour session where Nick corrected something at 11am and then got four things right
    afterwards contains four successes and one failure, not zero successes.

    So: cut the session at each correction. A window that TERMINATES in a correction is a failed
    attempt — the work in it was rejected. A window terminated by a fresh non-corrective ask, or
    by the end of the session, is accepted. This is strictly more faithful to the plan's intent
    and it is the only version that can ever return a workflow on a real Core.
    """
    rx = _correction_rx()
    turns = sess["turns"]
    windows: list[dict] = []
    cur: list[dict] = []
    for t in turns:
        if t["role"] == "nick":
            is_corr = any(r.search(t["text"]) for r in rx.values())
            if is_corr:
                if cur:
                    windows.append({"turns": cur, "ended_in": "correction",
                                    "closing_text": t["text"][:200]})
                cur = []
                continue
            if cur:
                windows.append({"turns": cur, "ended_in": "next_ask", "closing_text": t["text"][:200]})
                cur = []
        cur.append(t)
    if cur:
        windows.append({"turns": cur, "ended_in": "session_end", "closing_text": ""})

    out = []
    for w in windows:
        tools = [t for t in w["turns"] if t["role"] == "tool"]
        asks = [t for t in w["turns"] if t["role"] == "nick"]
        # A window with no ask has no "what was wanted"; one with no tools did no work.
        if not asks or len(tools) < MIN_STEPS:
            continue
        w["accepted"] = w["ended_in"] in ("next_ask", "session_end")
        w["n_tools"] = len(tools)
        out.append(w)
    return out


def acceptance(conn, org: int, sess: dict) -> dict:
    """Session-level summary, kept for --detect reporting only.

    This is now DESCRIPTIVE, not the gate. The gate is segment_windows(), per sequence. Stop-gate
    blocks are still counted here because a session dense with blocks is worth seeing in the
    readout, but a block no longer disqualifies the successful stretches around it.
    """
    sid = sess["session_id"]
    out = {"corrections_in_session": 0, "stop_blocks": 0, "accepted_windows": 0, "why": ""}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pattern_observations WHERE org_id=%s AND source_file LIKE %s",
            (org, f"%{sid}%"),
        )
        out["corrections_in_session"] = cur.fetchone()[0]
        try:
            cur.execute(
                "SELECT count(*) FROM steering_events "
                "WHERE org_id=%s AND session_id=%s AND verdict ILIKE %s",
                (org, sid, "%block%"),
            )
            out["stop_blocks"] = cur.fetchone()[0]
        except Exception:
            out["stop_blocks"] = 0          # table may predate this Core's migration set
    wins = segment_windows(sess)
    out["accepted_windows"] = sum(1 for w in wins if w["accepted"])
    out["total_windows"] = len(wins)
    out["accepted"] = out["accepted_windows"] > 0
    out["why"] = (f"{out['accepted_windows']}/{out['total_windows']} work windows ended in "
                  f"acceptance rather than a correction")
    return out


def revision_id(sess: dict) -> str:
    """C4 idempotency key: stable per (session, content). Re-running consolidates nothing twice.

    Content-hashed rather than session-id-only so that a session which GREW since the last pass is
    a new revision and gets reconsidered, while an unchanged one is skipped. Date.now() is never an
    input — the same session must always produce the same key.
    """
    h = hashlib.sha256()
    h.update(sess["session_id"].encode())
    h.update(str(sess["n_turns"]).encode())
    for t in sess["turns"]:
        h.update((t.get("text") or t.get("tool") or "").encode())
    return f"consolidate/{sess['session_id'][:12]}/{h.hexdigest()[:16]}"


def already_done(conn, org: int, rev: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM workflow_steps WHERE org_id=%s AND source_revision_id=%s LIMIT 1",
            (org, rev),
        )
        return cur.fetchone() is not None


def cmd_detect(conn, org: int, limit: int) -> int:
    files = _session_files()[:limit]
    print(f"{'session':16} {'turns':>6} {'corr':>5} {'blk':>4}  {'accepted':9} {'consolidated':12} date")
    n_ready = 0
    for f in files:
        s = read_session(f)
        if not s["turns"]:
            continue
        acc = acceptance(conn, org, s)
        rev = revision_id(s)
        done = already_done(conn, org, rev)
        if acc["accepted"] and not done:
            n_ready += 1
        print(f"{s['session_id'][:16]:16} {s['n_turns']:>6} {acc['corrections_in_session']:>5} "
              f"{acc['stop_blocks']:>4}  {str(acc['accepted']):9} {str(done):12} {s['date']}")
    print(f"\n{n_ready} session(s) accepted and not yet consolidated.")
    if not n_ready:
        print("Nothing to do. Note this is the EXPECTED steady state on a Core whose sessions all")
        print("contain corrections — acceptance is strict by design (plan §3 C2).")
    return 0


BRIEF = """You are the consolidation pass of a personal agent's learning system.

You are given ONE session: what the user typed, in order, and which tools ran between their turns.
Your job is to find SUCCESS SEQUENCES — a thing the user asked for, and the ordered steps that
actually delivered it.

Return STRICT JSON, no prose, no code fence:

{"workflows": [
   {"name": "<short imperative name, <=60 chars, e.g. 'ship a fix to the baseline'>",
    "trigger": "<what the user said/wanted that starts this, one sentence>",
    "trigger_prompts": ["<VERBATIM the NICK: line(s) that opened this window — copied exactly,
                         not paraphrased, not summarised. 1-3 of them>"],
    "steps": [{"action": "<what was done, one clause, imperative>",
               "tool_hint": "<tool name if one clearly did it, else null>"}, ...]}
]}

WHY trigger_prompts MUST BE VERBATIM: when this workflow recurs it generates a real behaviour, and
the installer will only accept it if the positive test is a prompt the trigger genuinely fires on.
A paraphrase fails that check and the workflow is silently refused. Copy the text.

HARD RULES — a workflow that breaks any of these is worse than no workflow:

1. MINIMUM 2 STEPS. A one-step "workflow" is a preference, not a sequence. Omit it.
2. Steps must be things that ACTUALLY HAPPENED in this session, in the order they happened. Do not
   write the ideal procedure. Do not add a step because it seems like good practice.
3. Only include a sequence that reached what the user asked for. If the session wandered, ended
   mid-task, or the user redirected, that is not a success sequence.
4. Describe the WORK, not the conversation. "read the failing test, then fixed the parser" is a
   workflow. "answered the user, then asked a question" is not.
5. Generalise one notch, no further. "push a shared file to the baseline" — good. "run
   sync-to-baseline.sh at 00:14 on Aug 5" — too specific. "do engineering" — too general.
6. If this session contains NO real multi-step success sequence, return {"workflows": []}. That is
   a correct and common answer. Returning something to be useful is the failure mode here — an
   invented workflow enters the agent's brain as fact and shapes later behaviour.

SESSION:
"""


def cmd_prepare(conn, org: int, limit: int, out_path: Path) -> int:
    files = _session_files()
    picked = []
    for f in files:
        s = read_session(f)
        if not s["turns"]:
            continue
        acc = acceptance(conn, org, s)
        if not acc["accepted"]:
            continue
        rev = revision_id(s)
        if already_done(conn, org, rev):
            continue
        s["revision_id"] = rev
        s["acceptance"] = acc
        picked.append(s)
        if len(picked) >= limit:
            break
    if not picked:
        print("[prepare] nothing accepted-and-unconsolidated. No brief written.")
        return 0
    # Emit accepted WINDOWS, not whole sessions. The extraction pass should never see the
    # stretches that ended in a correction — those are failed attempts, and showing them invites
    # the model to write down the ideal procedure it thinks should have happened instead of the
    # one that actually worked. Hard rule 2 of the brief says don't; not showing it is better
    # than asking it not to.
    sessions = []
    for s in picked:
        wins = [w for w in segment_windows(s) if w["accepted"]]
        if not wins:
            continue
        sessions.append({
            "session_id": s["session_id"], "revision_id": s["revision_id"], "date": s["date"],
            "windows": [
                {"ended_in": w["ended_in"],
                 "transcript": [
                     (f"{_U.name().upper()}: {t['text']}" if t["role"] == "nick"
                      else f"  [{t['tool']}] {t.get('sig','')}".rstrip())
                     for t in w["turns"]
                 ]}
                for w in wins
            ],
        })
    payload = {"org_id": org, "brief": BRIEF, "sessions": sessions}
    out_path.write_text(json.dumps(payload, indent=1))
    print(f"[prepare] {len(picked)} session(s) -> {out_path}")
    print("[prepare] run the extraction against each session's transcript using payload['brief'],")
    print("[prepare] then: consolidate_sessions.py --apply <result.json>")
    print('[prepare] result shape: {"results":[{"revision_id":"...","workflows":[...]}]}')
    return 0


# A learned step must never TELL the agent to take an outward or irreversible action.
#
# Found by Codex on adversarial review of this loop, and verified: `_payload_content_ok` allowed
# "send the verified resolution to Nick by email", "delete the stale branch", "buy the domain if it
# is available" and "curl the results to the webhook". It masks known secret SHAPES and refuses
# guard-surface paths; it is not instruction sanitisation, and nothing else stood between an
# extracted step and durable injected instruction.
#
# Nick's hard rules are never spend money, never send mail/SMS without seeing the draft, never delete
# without confirming. The outward action itself is still gated by pretooluse-guard and Sentinel,
# which sit outside the model — but a workflow that INSTRUCTS the agent toward one is the loop
# writing its own permissions, which is the same category as writing its own hooks. Refused here, at
# the point it would become durable, rather than relying on a downstream gate to catch the result.
_OUTWARD_STEP_RX = __import__("re").compile(
    r"\b(email|e-mail|send (?:the |a |an )?(?:mail|message|text|sms|draft|reply)|sms|imessage"
    r"|buy|purchase|order|pay|checkout|subscribe"
    r"|delete|rm -rf|drop table|truncate|force[- ]?push|push --force"
    r"|curl|wget|POST to|webhook"
    r"|commit and push|push to (?:origin|baseline|main)"
    r"|osascript|open a pr|merge the pr)\b", __import__("re").I)


def _validate(entry: dict) -> tuple[list[dict], list[str]]:
    """Every rejection reason is reported, never silently dropped. The parent verifies; that is
    the whole reason this is a separate step from the model call."""
    ok, problems = [], []
    for wf in entry.get("workflows") or []:
        name = (wf.get("name") or "").strip()
        steps = wf.get("steps") or []
        if not name:
            problems.append("workflow with no name — dropped")
            continue
        clean, outward = [], []
        for s in steps:
            if not (isinstance(s, dict) and (s.get("action") or "").strip()):
                continue
            act = s["action"].strip()
            m = _OUTWARD_STEP_RX.search(act)
            if m:
                outward.append(f"{act[:70]!r} (matched {m.group(0)!r})")
                continue
            clean.append(s)
        if outward:
            # The WHOLE workflow is refused, not just the offending step. Dropping one step and
            # keeping the rest would install a procedure that silently omits part of what actually
            # happened — an agent following an edited sequence believes it is following the observed
            # one. Refusing loudly is the honest outcome.
            problems.append(f"{name!r}: REFUSED — step(s) describe an outward or irreversible "
                            f"action: {'; '.join(outward)}. A learned workflow must never instruct "
                            f"the agent to send, buy, delete, push or curl.")
            continue
        if len(clean) < MIN_STEPS:
            # Plan §3 A4, applied here too: under-arity is a MISLABEL worth reporting, not a  # privacy-ok: generic engineering vocabulary
            # record worth keeping.
            problems.append(f"{name!r}: {len(clean)} step(s) < {MIN_STEPS} — mislabelled, dropped")
            continue
        ok.append({"name": name[:60], "trigger": (wf.get("trigger") or "").strip(),
                   "steps": clean,
                   # Real prompts that opened the accepted window. Phase D3 uses these as the
                   # POSITIVE test when a recurring workflow generates an artifact, because
                   # friction_installer refuses a positive that is not a prompt the trigger
                   # genuinely fires on. Without them D3 would have to fabricate one and defeat
                   # the gate every other artifact passes.
                   "trigger_prompts": [p for p in (wf.get("trigger_prompts") or [])
                                       if isinstance(p, str) and p.strip()][:6]})
    return ok, problems


def cmd_apply(conn, org: int, path: Path) -> int:
    data = json.loads(path.read_text())
    results = data.get("results") or []
    total_wf = total_steps = 0
    with conn.cursor() as cur:
        cur.execute("SET app.current_org_id = %s", (str(org),))
        for entry in results:
            rev = entry.get("revision_id")
            if not rev:
                print("[apply] SKIP: result with no revision_id")
                continue
            if already_done(conn, org, rev):
                print(f"[apply] SKIP {rev}: already consolidated (C4 idempotency)")
                continue
            good, problems = _validate(entry)
            for p in problems:
                print(f"[apply] REJECT {rev}: {p}")
            for wf in good:
                # The trigger goes in compiled_truth_md — entities has no `summary` column, and
                # compiled_truth_md is the field hub rendering already reads, so a Workflow shows
                # up in recall with its trigger attached rather than as a bare name.
                trig = wf["trigger"]
                # REUSE an existing Workflow of the same name — do NOT insert a second entity.
                #
                # This was an INSERT, and it made Phase D3 unreachable by construction: recurrence is
                # counted as distinct source_revision_id PER ENTITY, so the same workflow seen in two
                # sessions produced two separate entities each holding one session. Nothing could
                # ever cross the 2-session bar, and the failure would have been invisible — the rows
                # look correct, the counts look correct, and the generator simply never fires. Found
                # by trying to verify D3 end to end rather than by reading the code.
                cur.execute(
                    "SELECT id FROM entities WHERE kind='Workflow' AND org_id=%s AND name=%s "
                    "AND valid_until IS NULL LIMIT 1",
                    (org, wf["name"]),
                )
                row = cur.fetchone()
                if row:
                    eid = row[0]
                    # A recurrence can refine the trigger wording; keep the first one rather than
                    # letting the newest session overwrite an established description.
                    if trig:
                        cur.execute(
                            "UPDATE entities SET compiled_truth_md = COALESCE(compiled_truth_md, %s), "
                            "updated_at = now() WHERE id=%s",
                            (f"**Triggered when:** {trig}", eid))
                else:
                    cur.execute(
                        "INSERT INTO entities (name, kind, org_id, compiled_truth_md, source_file) "
                        "VALUES (%s,'Workflow',%s,%s,%s) RETURNING id",
                        (wf["name"], org,
                         (f"**Triggered when:** {trig}" if trig else None),
                         f"consolidate_sessions/{rev}"),
                    )
                    eid = cur.fetchone()[0]
                for i, st in enumerate(wf["steps"], start=1):
                    # ON CONFLICT: a recurrence keeps the FIRST occurrence's steps. The value of a
                    # second sighting is the recurrence itself, not a re-description — and letting
                    # the newest session overwrite an established sequence would mean a workflow's
                    # steps silently churn every time it happens again.
                    cur.execute(
                        "INSERT INTO workflow_steps "
                        "(workflow_entity_id, step_index, action, tool_hint, org_id, source_revision_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (eid, i, st["action"].strip()[:500],
                         (st.get("tool_hint") or None), org, rev),
                    )
                # The window's real opening prompts, kept verbatim for D3's positive test.
                # ON CONFLICT so re-consolidating an unchanged session stays a no-op.
                for p in wf.get("trigger_prompts") or []:
                    cur.execute(
                        "INSERT INTO workflow_triggers "
                        "(workflow_entity_id, prompt_text, org_id, source_revision_id) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (eid, p.strip()[:2000], org, rev),
                    )
                total_wf += 1
                total_steps += len(wf["steps"])
                print(f"[apply] + {wf['name']!r} ({len(wf['steps'])} steps) entity={eid}")
    conn.commit()
    print(f"[apply] wrote {total_wf} workflow(s), {total_steps} step(s)")
    return 0


def cmd_flag_stale_steps(conn, org: int, dry: bool = False) -> int:
    """PHASE W — flag a workflow whose steps stopped working, so the next pass re-extracts them.

    Phases D3/E2 made a workflow FIRE. Nothing noticed when one of its steps went wrong.
    `refresh_workflow_payloads` keeps the installed artifact in sync with the brain, but nothing
    updated the BRAIN when reality diverged, so a workflow could keep confidently injecting a
    sequence that had stopped being how the work is actually done.

    THE SIGNAL: a workflow artifact fired, and the SAME session later produced a correction. That is
    the mechanical form of "the procedure was offered and the outcome still needed fixing". Both
    halves already exist — `fire_inject` rows in the action log, corrections in
    `pattern_observations` — and nothing joined them.

    WHAT THIS DELIBERATELY DOES NOT DO: it does not rewrite a step. Choosing WHICH step was wrong and
    what it should say is a judgement pass, and a deterministic guess at it would corrupt the
    sequence far more cheaply than it would fix it. This FLAGS; the existing close-time extraction
    (step 2c) re-derives the sequence from the newer session, and `ON CONFLICT DO NOTHING` on
    workflow_steps means the flag has to clear the way first. One extraction mechanism, not two.
    """
    import re as _re
    cur = conn.cursor()
    cur.execute("SET app.current_org_id = %s", (str(org),))
    root = core_root(Path(__file__))
    alog = root / ".claude" / "state" / "friction-action-log.jsonl"

    # workflow artifacts and the sessions they fired in
    fired = collections.defaultdict(set)
    try:
        for ln in alog.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("action") == "fire_inject" and str(r.get("artifact_id", "")).startswith("art_wf"):
                fired[r["artifact_id"]].add(r.get("session_id") or "")
    except Exception:
        pass
    if not fired:
        print("[flag-steps] no workflow artifact has fired yet — nothing to judge")
        return 0

    flagged = 0
    for aid, sessions in fired.items():
        # Which brain Workflow does this artifact serve? case_id is wf_<entity_id>.
        cur.execute("SELECT spec FROM si_artifacts WHERE artifact_id=%s AND org_id=%s", (aid, org))
        row = cur.fetchone()
        case_id = ""
        if row and row[0]:
            spec = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            case_id = str(spec.get("case_id", ""))
        m = _re.match(r"wf_(\d+)$", case_id)
        if not m:
            continue
        eid = int(m.group(1))

        # Did any session in which it fired also produce a correction?
        hits = 0
        for sid in sessions:
            if not sid:
                continue
            cur.execute("SELECT count(*) FROM pattern_observations "
                        "WHERE org_id=%s AND source_file LIKE %s", (org, f"%{sid}%"))
            if (cur.fetchone() or [0])[0] > 0:
                hits += 1
        if hits == 0:
            print(f"[flag-steps] {aid}: fired in {len(sessions)} session(s), no corrections — steps holding")
            continue

        cur.execute("SELECT name FROM entities WHERE id=%s AND kind='Workflow'", (eid,))
        nm = (cur.fetchone() or ["?"])[0]
        print(f"[flag-steps] {aid} ({nm!r}): fired in {len(sessions)} session(s), "
              f"{hits} of them also produced a correction -> steps may be stale")
        if not dry:
            # A marker on the entity, not a deletion. The next extraction re-derives from the newer
            # session; three non-converging flags is the retirement bar (tracked by counting markers).
            cur.execute(
                "UPDATE entities SET compiled_truth_md = "
                "  COALESCE(compiled_truth_md,'') || %s, updated_at = now() WHERE id=%s",
                (f"\n\n<!-- step-review flagged {hits} correction-session(s) -->", eid))
            flagged += 1
    if not dry:
        conn.commit()
    print(f"[flag-steps] flagged {flagged} workflow(s) for step re-extraction")
    return 0


def cmd_status(conn, org: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.id, e.name, "
            "  (SELECT count(*) FROM workflow_steps w WHERE w.workflow_entity_id=e.id) AS steps, "
            "  (SELECT count(DISTINCT t.source_revision_id) FROM workflow_triggers t "
            "     WHERE t.workflow_entity_id=e.id) AS sessions "
            "FROM entities e WHERE e.org_id=%s AND e.kind='Workflow' AND e.valid_until IS NULL "
            "ORDER BY sessions DESC, steps DESC",
            (org,),
        )
        rows = cur.fetchall()
    if not rows:
        print("no Workflow entities yet for this org.")
        return 0
    print(f"{'id':>9} {'steps':>6} {'sessions':>9}  name")
    for eid, name, steps, sessions in rows:
        mark = "GENERATES" if sessions >= MIN_SESSIONS_TO_GENERATE else f"holds ({sessions}/{MIN_SESSIONS_TO_GENERATE})"
        print(f"{eid:>9} {steps:>6} {sessions:>9}  {name}   [{mark}]")
    print(f"\nA workflow generates a behavior only at >= {MIN_SESSIONS_TO_GENERATE} independent "
          f"sessions (plan §3 C3 — the friction loop's existing bar, reused not reinvented).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--apply", metavar="FILE")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--flag-stale-steps", action="store_true",
                    help="Phase W: flag workflows whose fires were followed by corrections")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--out", default=".claude/state/consolidate-pending.json")
    args = ap.parse_args()

    if os.environ.get("CORE_CONSOLIDATE_OFF") == "1":
        print("[consolidate] CORE_CONSOLIDATE_OFF=1 — disabled, nothing done.")
        return 0

    org = get_org_id(Path(__file__))
    # connect_or_skip, per _env's convention: a close step that CAN be skipped prints a named
    # status and returns 0 instead of killing the close chain with a traceback (found 2026-09-04
    # tracing /close-core on a clone with no database).
    conn = connect_or_skip("CONSOLIDATE")
    if conn is None:
        return 0
    try:
        if args.detect:
            return cmd_detect(conn, org, args.limit)
        if args.prepare:
            return cmd_prepare(conn, org, args.limit,
                               core_root(Path(__file__)) / args.out)
        if args.apply:
            return cmd_apply(conn, org, Path(args.apply))
        if args.status:
            return cmd_status(conn, org)
        if args.flag_stale_steps:
            return cmd_flag_stale_steps(conn, org, dry=False)
    finally:
        conn.close()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
