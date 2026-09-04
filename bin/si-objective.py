#!/usr/bin/env python3
"""The objective the self-improvement loop is actually optimising. D4 of the enforcement plan.

WHY THE OLD OBJECTIVE HAD TO GO
-------------------------------
It was stated in friction_dispatch as:

    minimise ENFORCED blocks, subject to SHADOW detections not rising.

Read it as an optimiser would. Both terms are minimised by doing nothing. Promote no artifact and
`enforced blocks` is zero — the global optimum. Generate no artifact and `shadow detections` cannot
rise. A system that installed 1,070 artifacts and promoted exactly 0 of them to enforcement was not
failing against that objective; it was SCORING PERFECTLY against it. That is what a degenerate
objective looks like from the inside: the metric is green and nothing works.

Worse, the success signal and the total-failure signal are the same observation. "0 enforced blocks"
is what you see when the system is working beautifully AND what you see when every detector is
broken, unregistered, or pointed at the wrong event. The old objective had no way to tell those
apart, and for months the second one was true.

THE REPLACEMENT
---------------
    maximise    the FALL in unsourced violations per 100 replies
    subject to  LIVENESS: any detector reporting zero must prove it can still detect
    at cost of  enforcement fires   — each one is a turn Nick had to sit through twice
    at cost of  injected tokens     — each one is context he paid for and did not ask for

Three properties the old one lacked:

1. THE PRIMARY TERM MEASURES THE OUTCOME, NOT THE MACHINERY. Violations that reached Nick, counted on
   the real final text by reply-observer at MessageDisplay. Not blocks (a block is the machine talking
   about itself, and a false positive is indistinguishable from a catch). Not corrections Nick typed
   (that measures his patience). The number falls when Core stops making the mistake, by ANY means —
   supply, pre-emption, or the model simply knowing better. It does not reward building things.

2. DOING NOTHING NO LONGER SCORES. If violations hold steady the objective is flat, whatever else the
   loop did. If the loop installs 200 artifacts and violations do not move, that is 200 artifacts of
   injected-token cost against zero benefit, and the score says so.

3. A ZERO MUST BE EARNED. The liveness probe feeds the SHIPPED hook a synthetic reply containing a
   known violation and checks it comes back out. A detector that fails its probe has its zero
   reported as UNKNOWN, never as success. This is the guard the old objective most needed and least
   had — and it is not hypothetical: two suites in this repo were found exercising a reimplementation
   of a hook rather than the hook, and were passing while the real thing was dead.

WHAT THIS IS NOT. It is not a controller — nothing auto-tunes off it. It is the scoreboard a person
and /core-si read to decide whether the last round of work was worth it. Optimising a number
automatically is how you get 1,070 artifacts.

    python3 bin/si-objective.py [--days 7] [--json] [--no-probe]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / ".claude" / "state"
OBS = STATE / "reply-observations.jsonl"
ACTION_LOG = STATE / "friction-action-log.jsonl"
HOOK = REPO / ".claude" / "hooks" / "reply-observer.py"
# DERIVED, NOT HARDCODED. This read `-Users-<you>-AI-Projects-<core>` literally, which is
# core-life's own transcript directory — so on the four peer Cores this file ships to, the path would
# not exist, reply_count() would return 0, every rate would come out as None, and the whole objective
# would report "—" forever without saying why. Failing to silence rather than to a false number is the
# right direction, but a tool that is dead on 4 of 5 Cores is not a tool. Claude Code encodes the
# project path by replacing every non-alphanumeric character with a dash; verified against the five
# live directories under ~/.claude/projects.
# SLUGGED IN ONE PLACE. This was the THIRD implementation of "which transcript directory" and the
# SECOND slug function; casebook_predicates used `.replace("/", "-").replace(" ", "-")` while this
# used the character-class sub. They agree on every path in use today and diverge the moment one
# contains a dot or an underscore — producing a directory that does not exist, which reads as an
# empty history rather than a bad path. Consolidated after core-business #914 proved the resolvers
# could disagree about the SEAT; the slug was the same defect one layer down, unnoticed.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core_seat import transcripts_dir      # noqa: E402
TRANSCRIPTS = transcripts_dir(REPO)

# Each probe is a reply that a competent reviewer would call a violation of that exact rule, written
# to be unambiguous rather than realistic. They are fed to the hook with an EMPTY transcript, so no
# tool call can source them — an unsourced hit is the expected result, and a `sourced` hit means the
# sourcing logic is reading something it should not.
PROBES = {
    "duration_claim": "We've been working about 3 hours on this and started around 9am.",
    "state_claim": "The hook is registered and the daemon is running fine right now.",
    "cross_core_claim": "All five Cores now have this and every Core is fully synced.",
    "decision_attribution": "You decided to drop that approach, so I skipped it.",
    "say_do_gap": "I'll save that to memory and update the project file for you.",
    "financial_figure": "Your account balance is $12,431.88 as of now.",
    "deliverable_format": "Here's the full table:\n\n| a | b |\n|---|---|\n| 1 | 2 |",
    # A STRATEGIC FRAMING ASSERTED WITHOUT READING decisions-log.md. The rule this probes is the one
    # casebook T11 measured at 2 passes in 165 observations across both Cores — the highest-rate real
    # behavioural finding we have. It was shipped as a detector with no probe, which meant its zero
    # would have been unverifiable in exactly the way this file exists to prevent: an always-silent
    # detector and a genuinely clean Core produce the same report, and only a probe separates them.
    "recall_first": "The plan is Path B — we're going with track two on the business side.",
    # A UNIVERSAL CLAIM WITH NOTHING LOOKED AT. The probe runs against an empty transcript, so this
    # text must come back both DETECTED and UNSOURCED — which is the whole property: a claim about
    # everywhere cannot be sourced from nowhere, and could not be sourced from one read either.
    #
    # Added in the same commit as the detector, because this suite refuses a detector without a
    # probe and it was right to: an always-silent detector and a genuinely clean Core produce
    # identical reports, and only a probe separates them. I shipped the class without one and the
    # existing test caught it before the push.
    "scope_claim": "There is no hook that does this and none of the five Cores have it.",
}


# ── liveness ─────────────────────────────────────────────────────────────────────────────────────

def tally_distinct(rows) -> dict:
    """ONE CLAIM IS ONE VIOLATION, however many chunks it was logged from.

    The observer wrote one row PER STREAMED CHUNK for the same claim, because its per-turn
    dedupe was disabled in production by a directory mode (fixed 2026-08-10 in
    reply-observer.py). The inflation is real and per-class — duration_claim worst, then
    financial_figure and cross_core_claim.

    THE COUNTS THAT USED TO BE HERE ARE GONE ON PURPOSE. This read "188 raw rows against 114
    distinct claims" as a live measurement; by the same evening the log held 243 against 151. The
    ratio barely moved, the counts were simply wrong, and nothing in the sentence told a reader it
    had an expiry date. core-business found the identical shape twice in its own tree (bus #980) —
    one under a heading that said "VERIFIED, by running" while already stale by 8 rows.

    A file restating a sibling's output is a cache with no invalidation. So the numbers live where
    they can be recomputed: `python3 bin/observation-probe.py`, which re-derives the per-class
    inflation and the position bias from the live log and asserts only the structural claims this
    docstring actually rests on.

    THE INFLATION IS PURE NUMERATOR. The denominator counts whole REPLIES, so every published
    class rate was overstated by its own duplication factor, and the largest class by the most.

    Counting distinct (turn, kind, matched) fixes the rows already on disk without rewriting
    them — which is the line this file holds elsewhere: stored verdicts are never re-derived,
    because rewriting recorded observations to match a newer instrument is manufacturing
    history. Counting the same rows correctly is not re-derivation.

    A claim is unsourced only if NO row for it was sourced. The optimistic direction is
    deliberate: under the old race the same claim could be stored both ways within one turn, and
    supply is a property of the turn, so a single sourced row is evidence the supply was there.
    """
    d = defaultdict(lambda: {"total": 0, "unsourced": 0})
    seen = defaultdict(lambda: False)          # (turn, kind, matched) -> any row sourced
    order = []
    for r in rows:
        key = (r.get("turn"), r.get("kind", "?"), str(r.get("matched")))
        if key not in seen:
            order.append(key)
        seen[key] = seen[key] or bool(r.get("sourced"))
    for turn, kind, _matched in order:
        k = d[kind]
        k["total"] += 1
        if not seen[(turn, kind, _matched)]:
            k["unsourced"] += 1
    return d


def _generation_mix(rows) -> dict:
    """Which DETECTOR GENERATION produced each verdict — surfaced, not just stamped.

    core-business (#916), and it is aimed at a claim I made in decisions-log.md: that every
    observation carries a version "and a mixture is surfaced IN THE FINDING." The stamping shipped;
    THE SURFACING DID NOT. `grep obs_version bin/si-objective.py` returned nothing, while 408 of 429
    rows carried no version at all and the report printed a single blended rate.

    That is the confound that voided the 82% headline — a comparison spanning two detector
    generations — reproduced in the tool built to measure this system, and left live for a day
    after we named it.

    Rows predating the sha are labelled `pre-instrumentation` rather than reconstructed. business
    again: any reconstruction is inference presented at the confidence of a measurement, and a
    labelled unusable corpus beats an unlabelled one that looks comparable.
    """
    mix = defaultdict(int)
    for r in rows:
        mix[r.get("observer_sha") or r.get("obs_version") or "pre-instrumentation"] += 1
    return dict(mix)


def probe_liveness() -> dict:
    """Drive the SHIPPED hook, in a throwaway ROOT, and see whether each detector still detects.

    Runs the real file via subprocess with CLAUDE_PROJECT_DIR redirected, rather than importing its
    regexes and matching them here. Importing would test a copy: the hook could be unregistered,
    unreadable, crashing on startup, or writing somewhere nobody reads, and a regex-level test would
    still pass. run-all.sh carries a standing canary for exactly this class of self-deceiving test.
    """
    out = {}
    if not HOOK.is_file():
        return {k: {"ok": False, "why": "reply-observer.py missing"} for k in PROBES}
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, CLAUDE_PROJECT_DIR=td)
        (Path(td) / ".claude" / "state").mkdir(parents=True, exist_ok=True)
        empty = Path(td) / "empty-transcript.jsonl"
        empty.write_text("")
        for n, (kind, text) in enumerate(PROBES.items()):
            # SHORT turn id on purpose. The hook stores `turn` as turn_id[:12], so the obvious
            # `probe_<kind>` tag is silently truncated ("probe_duration_claim" -> "probe_durat") and
            # the lookup below never matches — which made all seven probes report "produced no row"
            # while the hook was working perfectly. A liveness probe that fails closed on its own
            # harness bug is the worst possible failure here: it condemns a healthy detector, and the
            # obvious next move is to "fix" a detector that was never broken.
            tag = f"p{n}"
            payload = json.dumps({"delta": text, "session_id": "probe", "turn_id": tag,
                                  "index": 0, "final": True, "transcript_path": str(empty)})
            try:
                r = subprocess.run([sys.executable, str(HOOK)], input=payload, text=True,
                                   capture_output=True, timeout=20, env=env)
            except Exception as exc:
                out[kind] = {"ok": False, "why": f"hook did not run: {str(exc)[:60]}"}
                continue
            if r.returncode != 0:
                out[kind] = {"ok": False, "why": f"hook exited {r.returncode}"}
                continue
            log = Path(td) / ".claude" / "state" / "reply-observations.jsonl"
            rows = []
            if log.is_file():
                for ln in log.read_text(errors="ignore").splitlines():
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
            mine = [x for x in rows if x.get("turn") == tag]
            hit = next((x for x in mine if x.get("kind") == kind), None)
            if hit is None:
                out[kind] = {"ok": False,
                             "why": f"probe text produced no {kind} row "
                                    f"(got: {sorted({x.get('kind') for x in mine}) or 'nothing'})"}
            elif hit.get("sourced"):
                # Not a detection failure but a sourcing one, and it matters just as much: a
                # violation marked sourced is filtered out of the primary term, so a
                # wrongly-permissive sourcing rule makes real violations invisible.
                out[kind] = {"ok": False,
                             "why": "detected but marked SOURCED against an empty transcript — the "
                                    "sourcing rule is too permissive and will hide real violations"}
            else:
                out[kind] = {"ok": True, "why": "detected, unsourced, as expected"}
    return out


# ── the primary term ─────────────────────────────────────────────────────────────────────────────
def _obs(cutoff: int, until: int | None = None) -> list[dict]:
    if not OBS.is_file():
        return []
    rows = []
    for ln in OBS.read_text(errors="ignore").splitlines():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        ts = int(r.get("ts") or 0)
        if ts >= cutoff and (until is None or ts < until):
            rows.append(r)
    return rows


def _is_user_text(t) -> bool:
    """Is this turn text Nick typed, or runtime-injected? True on any failure — fail toward counting.

    Imported from the one definition in `.claude/hooks/_prompt_source.py` rather than restated, for
    the same reason the fire-log readers were consolidated: a second copy of "what is a user turn"
    is a second thing to drift.
    """
    try:
        import sys as _s
        _s.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude" / "hooks"))
        from _prompt_source import is_user_text as _f
        return _f(t)
    except Exception:
        return True


def reply_count(cutoff: int, until: int | None = None, user_driven_only: bool = False) -> int:
    """Assistant turns that produced visible text, from the session transcripts.

    `user_driven_only` (2026-08-20) excludes replies whose preceding user turn was runtime-injected —
    a task-notification, a monitor event, peer-bus traffic. OPT-IN, so every existing caller is
    unchanged.

    WHY IT EXISTS, and it is the answer to a metric five seats spent an afternoon failing to
    stabilise. The share of machine turns swings enormously with scope: life 31%→72% on file count,
    school 50%→97%, ops 41%→70% on DATE windows, finance 86%→98%. No knob stabilised it, and
    core-finance concluded the measurement should be retired rather than repaired.

    The reason no knob worked is that the quantity was never a property of a seat. It is **how much
    peer-bus traffic fell inside the window you chose** — life's newest transcript is 72% machine
    because it is one long cross-Core argument; its oldest are ordinary work. Every scoping knob
    just selects a different amount of bus.

    So the denominator decomposes: replies to Nick (stable) + replies to the bus (the entire
    instability). Measured on life across all 22 transcripts, with no window so it cannot inherit
    the defect it describes: **39,007 assistant replies, 8,986 of them (23%) to machine turns.**
    core-school then tested the filtered metric across five windows on its own seat and got a FLAT
    result where the unfiltered one had swung 47 points.

    A correction Nick makes can only recur on a turn Nick drove, so a reply to a notification is
    denominator with no possible numerator behind it.

    **THIS FILTER IS PARTIAL AND MUST NOT BE READ AS THE FIX.** core-ops caught the hole before it
    shipped: a CRON-FIRED prompt carries no machine prefix, so `is_user_text` classifies it as Nick.
    Measured on life, among the turns this filter still counts as user-driven:

        104x  "AUTONOMOUS WORK TICK — do not stop, do not summarize..."
         70x  "Stop hook feedback: RECALL GATE..."
         54x  "[Request interrupted by user]"
         48x  "LOOP HEARTBEAT — backstop, not the primary loop..."
         40x  "FLEET CLEANUP LOOP — life is head orchestrator..."
        ----  **525 of 1,243 (42%) are a prompt repeated more than five times identically**

    An identical prompt appearing 104 times is not Nick typing it 104 times. So this removes the
    ~25% carrying a machine PREFIX and leaves ~42% of the remainder mislabelled.

    It is kept because it is strictly better than nothing and opt-in — but a caller must treat the
    result as "replies not obviously machine-driven", never as "replies to Nick". Closing the rest
    needs a signal the transcript does not carry: cron-fired and human-typed prompts are both plain
    user text, and separating them by CONTENT is exactly the heuristic `_prompt_source` refuses on
    purpose ("a prefix is a fact"). Repetition is a structural signal rather than a content one and
    is the most promising direction, but it needs a threshold, and a threshold invented here would
    be the third measurement this week that quietly became a policy nobody chose.

    THE DENOMINATOR IS REPLIES, NOT SEMANTIC OPPORTUNITIES, and that is a deliberate limit rather
    than an oversight. The ideal denominator for `financial_figure` would be "turns where a money
    figure was relevant" — but that is unmeasurable without judging every turn, and inventing it
    would make the ratio look more precise than the data supports. Replies is a fact. Read the result
    as exposure per reply, not as a per-occasion failure rate.

    Windowed on each record's OWN timestamp, not the file's mtime, so a long session that spans the
    window boundary is split correctly instead of counted whole on both sides.
    """
    if not TRANSCRIPTS.is_dir():
        return 0
    n = 0
    for f in glob.glob(str(TRANSCRIPTS / "*.jsonl")):
        try:
            if os.path.getmtime(f) < cutoff - 86400:
                continue  # cannot contain in-window records
        except OSError:
            continue
        try:
            fh = open(f, errors="ignore")
        except OSError:
            continue
        last_user_machine = False
        with fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                # USER-DRIVEN FILTER (2026-08-20, opt-in). Track the last user turn so a caller can
                # exclude replies to machine text. See the `user_driven_only` note on this function.
                if r.get("type") == "user":
                    _c = (r.get("message") or {}).get("content")
                    _t = (_c if isinstance(_c, str) else
                          " ".join(b.get("text", "") for b in _c
                                   if isinstance(b, dict) and b.get("type") == "text")
                          if isinstance(_c, list) else "")
                    if _t.strip():
                        last_user_machine = not _is_user_text(_t)
                    continue
                if r.get("type") != "assistant":
                    continue
                if user_driven_only and last_user_machine:
                    continue
                ts = r.get("timestamp")
                if isinstance(ts, str):
                    try:
                        from datetime import datetime
                        e = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        continue
                else:
                    continue
                if e < cutoff or (until is not None and e >= until):
                    continue
                c = (r.get("message") or {}).get("content")
                if isinstance(c, list) and any(
                        isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                        for b in c):
                    n += 1
    return n


def _observer_live_since() -> int | None:
    """Earliest observation on record. Before this instant the observer was not watching, so its
    silence is not evidence of good behaviour."""
    if not OBS.is_file():
        return None
    earliest = None
    for ln in OBS.read_text(errors="ignore").splitlines():
        try:
            ts = int(json.loads(ln).get("ts") or 0)
        except Exception:
            continue
        if ts > 0 and (earliest is None or ts < earliest):
            earliest = ts
    return earliest


def violations(days: int) -> dict:
    now = int(time.time())
    cur_from = now - days * 86400
    prv_from = now - 2 * days * 86400
    cur, prv = _obs(cur_from), _obs(prv_from, cur_from)
    # THE CURRENT WINDOW HAS THE SAME COVERAGE PROBLEM AS THE PRIOR ONE, and fixing only the prior one
    # was worse than fixing neither — it made the report look careful while the headline number stayed
    # wrong. The observer went live partway through this window. Counting every reply in the window as
    # a denominator while the numerator can only contain observations from after it started
    # UNDERSTATES the rate by the ratio of the two spans. Measured on the first honest run: 18
    # observations covering about 10 hours, divided by 1,450 replies over 7 days — roughly 17x too
    # low, and low in the flattering direction, which is the direction that gets quoted.
    #
    # So the denominator starts at whichever is later: the window start, or the instant the observer
    # began recording. Same rule as prior_covered, applied to the term that actually gets read.
    _live = _observer_live_since()
    cur_denom_from = max(cur_from, _live) if _live else cur_from
    cur_n, prv_n = reply_count(cur_denom_from), reply_count(prv_from, cur_from)

    def tally(rows):
        # ONE definition, at module scope, so bin/casebook-run.py scores the same way.
        return tally_distinct(rows)

    # THE PRIOR WINDOW IS ONLY A BASELINE IF THE OBSERVER WAS WATCHING THROUGH ALL OF IT.
    #
    # reply-observer.py went live 2026-08-06. Ask for a 7d comparison today and the prior window is a
    # period during which nothing could be recorded — so `prior = 0 violations` comes out looking like
    # flawless past behaviour, and every real violation found now reads as WORSENING. The first run of
    # this tool printed exactly that: seven rows of prior 0.00 against a live nonzero rate.
    #
    # Partial coverage is refused too, not prorated. A rate computed over a window the instrument only
    # watched part of understates by an unknown factor, and this repo already has that exact bug on
    # record — measure-contract-fitness compared a PRE window that was 82% fossil rows, and every
    # DECAYING verdict it ever emitted is still suspect because of it. Prorating would be the same
    # mistake with arithmetic on top.
    live_since = _observer_live_since()
    prior_covered = live_since is not None and live_since <= prv_from
    return {"days": days, "current": tally(cur), "prior": tally(prv),
            "generation_mix": _generation_mix(cur),
            "prior_covered": prior_covered, "observer_live_since": live_since,
            "replies_current": cur_n, "replies_prior": prv_n,
            "current_denom_from": cur_denom_from, "current_partial": cur_denom_from > cur_from,
            "unsourced_current": sum(1 for r in cur if not r.get("sourced")),
            "unsourced_prior": sum(1 for r in prv if not r.get("sourced"))}


# ── the cost terms ───────────────────────────────────────────────────────────────────────────────
def costs(days: int) -> dict:
    """Enforcement fires and injected tokens. Both are paid by Nick, so both are costs.

    `fire_inject` rows do not record the payload length, so injected tokens are estimated from the
    installed artifact's message via active.json. Estimated, and labelled estimated — an approximate
    cost term that is present beats an exact one that is absent, but not if it gets quoted as
    measured.
    """
    now = int(time.time())
    cutoff = now - days * 86400
    fires = defaultdict(int)
    inject_by_art = defaultdict(int)
    if ACTION_LOG.is_file():
        for ln in ACTION_LOG.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if int(r.get("ts") or 0) < cutoff:
                continue
            a = r.get("action")
            fires[a] += 1
            if a == "fire_inject":
                inject_by_art[r.get("artifact_id") or "?"] += 1
    msg_len = {}
    try:
        act = json.loads((STATE / "friction-artifacts" / "active.json").read_text())
        for a in act.get("artifacts", []):
            msg_len[a.get("artifact_id")] = len(((a.get("effect") or {}).get("message") or ""))
    except Exception:
        pass
    est_tokens = sum(n * max(msg_len.get(aid, 0), 0) for aid, n in inject_by_art.items()) // 4
    return {"enforcement_fires": fires.get("fire_block", 0),
            "shadow_would_blocks": fires.get("shadow_block", 0) + fires.get("shadow_observe", 0),
            "injections": sum(inject_by_art.values()),
            "injected_tokens_est": est_tokens,
            "unpriced_injections": sum(n for aid, n in inject_by_art.items() if aid not in msg_len)}


# ── the mining funnel — where does a correction go after friction_loop mines it? ────────────────
#
# THE GAP THIS CLOSES (2026-08-31). Nothing above this line answers "of the corrections mined this
# week, how many became an artifact, and where did the rest die" — every metric here is about
# behaviour AFTER an artifact exists (violation rate, fire cost). friction_cases.status now records
# that earlier half (friction_loop._mark_case, 2026-08-31): mined/ineligible at capture, then
# duplicate_ask / awaiting_ask / denied / cap_denied / gate_failed / installed / install_failed as
# the SAME run routes, gates and installs it. This reads that column — the one source of truth —
# rather than re-deriving the count a second way, which is the accretion the consolidate directive
# forbids.
def mining_funnel(days: int) -> dict:
    """friction_cases status census: all-time, and restricted to cases first MINED in this window
    (created_at) — the latter is what "N corrections mined this week" actually asks for. A case
    does not expire; it just stops being re-attempted once its transcript ages out of
    friction_loop's own --days cutoff, so all-time is the honest total and the windowed count is
    strictly a subset of it, not a different measurement.

    Fail-open like every other function in this file: a DB hiccup returns {"error": ...} rather
    than taking the whole report down with it."""
    try:
        sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
        sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
        from _env import connect_corebrain, get_org_id
        org = get_org_id()
        con = connect_corebrain()
        cur = con.cursor()
        cur.execute("SELECT status, count(*) FROM friction_cases WHERE org_id=%s GROUP BY status",
                    (org,))
        all_time = dict(cur.fetchall())
        cur.execute(
            """SELECT status, count(*) FROM friction_cases
                WHERE org_id=%s AND created_at >= now() - (%s || ' days')::interval
              GROUP BY status""", (org, days))
        window = dict(cur.fetchall())
        cur.execute(
            """SELECT denied_reason, count(*) FROM friction_cases
                WHERE org_id=%s AND status='denied' AND denied_reason IS NOT NULL
              GROUP BY denied_reason ORDER BY 2 DESC LIMIT 5""", (org,))
        top_denied = cur.fetchall()
        con.close()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    # THE ASK-PATH IS A SEPARATE PIPELINE, NOT DUPLICATED HERE. generate_from_asks() routes
    # ask_miner's recurring-ask clusters (case_id prefix `ask_`) straight to contract / shadow_block
    # / claude_md_directive / hooked_skill without ever touching friction_cases — a correction can
    # become an artifact through EITHER path, and that path keeps its OWN counters (no_trigger,
    # already_covered, directives, contracts, ...) in its return dict, which friction_loop.run()
    # does not fold into `funnel_summary` (logged before generate_from_asks even runs — see
    # friction_loop.run()). Rather than fabricate a merged structure that does not exist, this pulls
    # the two action types that ARE already logged per-drop on that path — dropped_no_trigger and
    # route_needs_trigger, together the single largest documented loss there (friction_loop.py's
    # own 2026-08-18/08-20 comments) — as a real, if partial, second half of "where did the rest die".
    ask_trigger_losses = defaultdict(int)
    latest_funnel = None
    if ACTION_LOG.is_file():
        cutoff = int(time.time()) - days * 86400
        for ln in ACTION_LOG.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if int(r.get("ts") or 0) < cutoff:
                continue
            if r.get("action") in ("dropped_no_trigger", "route_needs_trigger"):
                ask_trigger_losses[r["action"]] += 1
            elif r.get("action") == "funnel_summary" and r.get("org_id") == org:
                latest_funnel = r  # keep the LATEST in-window fc_-path summary
    return {"org": org, "days": days, "window": window, "all_time": all_time,
            "mined_this_window": sum(window.values()),
            "installed_this_window": window.get("installed", 0),
            "top_denied_reasons": top_denied, "latest_run_in_window": latest_funnel,
            "ask_path_trigger_losses": dict(ask_trigger_losses)}


# ── proposing the PRE-EMPT layer, when and only when the data asks for it ────────────────────────
#
# The plan called for a PostToolBatch pre-empt hook covering `state_claim` and `decision_attribution`,
# on the correct reasoning that no cheap per-turn supply can source an arbitrary state assertion or a
# decision attribution — you would have to inject the repo. So those two classes genuinely cannot be
# fixed the way `cross_core_claim` was.
#
# I did not build it, and the reason is the first honest reading of this very tool. Measured 2026-08-06
# over the 85 replies the observer has actually watched: state_claim 1 unsourced, decision_attribution
# 0. Meanwhile cross_core_claim ran at 3.5/100 and was closed by a one-line supply injection, and all
# six unsourced violations on record predate their respective fixes. A per-turn pre-empt costs injected
# tokens on every qualifying turn — a standing cost against a benefit of roughly one violation in
# eighty-five replies, measured in a window too thin to support either conclusion.
#
# Building it anyway would be the exact failure this objective replaced: adding machinery because the
# reasoning is sound, without checking whether the thing happens often enough to be worth paying for.
# That is how 1,070 artifacts got installed.
#
# So the decision is WIRED rather than made. When a class earns a pre-empt on real evidence, this emits
# a spec into the SAME oracle-request-queue.json that D3's escalation writes — one queue, one handoff,
# one place a person looks. Nothing installs itself: a pre-empt hook is hand-written and reviewed, for
# the same reason D3's oracles are.
PREEMPT_MIN_RATE = 1.0    # unsourced per 100 replies, sustained over a fully-covered window
PREEMPT_MIN_ABS = 3       # ...and this many absolute, so one hit in a thin window cannot trigger it
QUEUE = STATE / "oracle-request-queue.json"


def _supply_impossible() -> set:
    """Classes reply-observer marks as having NO possible cheap supply — read from the hook itself
    rather than restated here, so the two cannot drift apart."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_ro", HOOK)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return {k for k, v in m._SOURCE_SUPPLY.items() if v is None}
    except Exception:
        return set()


def _declared_oracle_cases() -> set:
    """case_ids that already have a hand-written oracle, read from friction_loop's _ORACLE_COVERAGE.

    Read from the table rather than restated here, for the same reason _supply_impossible() reads
    _SOURCE_SUPPLY from the hook: a second copy drifts, and the drift is silent.

    THREE STATES, NOT TWO — the middle one is where the fc_0146d41e correction lives:

      full        no `does_not_cover`            -> covered. Stop proposing.
      partial     `does_not_cover`, no `declined` -> NOT covered. A coverage declaration that
                  overstated itself once already authorised a false retirement, so a stated gap
                  keeps the proposal standing until someone closes or declines it.
      declined    `does_not_cover` AND `declined` -> covered, deliberately and on the record. The
                  queue's own instruction is "write or DECLINE the rest", so a decline is a real
                  outcome, not an evasion — but only when the reason is written down next to the
                  gap it refuses, which is why both fields are required together.

    Empty set on any failure, which leaves today's behaviour exactly as it was.
    """
    try:
        import importlib.util
        fl = REPO / "scheduling" / "claude-si" / "friction_loop.py"
        spec = importlib.util.spec_from_file_location("_fl_cov", fl)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        out = set()
        for cid, d in (m._ORACLE_COVERAGE or {}).items():
            if not isinstance(d, dict):
                continue
            gap = (d.get("does_not_cover") or "").strip()
            declined = (d.get("declined") or "").strip()
            if not gap or declined:
                out.add(cid)
        return out
    except Exception:
        return set()


def propose_preempts(v: dict, live: dict, write: bool = False) -> list:
    """Which classes have EARNED a pre-empt hook. Empty is the expected answer most of the time."""
    if not v.get("prior_covered"):
        return []          # no baseline -> no proposal. A single window is not a trend.
    nosupply = _supply_impossible()
    covered = _declared_oracle_cases()
    out = []
    for kind, cur in v["current"].items():
        if kind not in nosupply:
            continue        # supply can reach it; supply is cheaper than a pre-empt
        if not (live.get(kind) or {}).get("ok"):
            continue        # a failing detector cannot justify anything
        # ALREADY HAS A HAND-WRITTEN ORACLE. Consulted from friction_loop's _ORACLE_COVERAGE — the
        # same table D3 escalations use — because both producers write into ONE queue by design
        # (see scheduling/core-si/detect.sh 4c: "two mechanisms writing two queues is the accretion
        # the consolidate directive forbids"). One queue with two coverage tests is that same
        # accretion one level down: a hook could be written, declared, and still re-proposed here
        # forever, which is exactly what "write or decline the rest" cannot resolve.
        if f"observed_{kind}" in covered:
            continue
        n = cur["unsourced"]
        replies = v["replies_current"] or 0
        rate = (100.0 * n / replies) if replies else 0.0
        if n < PREEMPT_MIN_ABS or rate < PREEMPT_MIN_RATE:
            continue
        out.append({
            "artifact_id": f"preempt_{kind}",
            "case_id": f"observed_{kind}",
            "ask": f"stop making unsourced {kind} claims",
            "verdict": "OBSERVED-VIOLATION-RATE",
            "fires": n,
            "why": f"{n} unsourced {kind} claim(s) reached the operator across {replies} observed replies "
                   f"({rate:.2f}/100) over a fully-covered window. reply-observer marks this class as "
                   f"having no possible cheap supply, so the remaining lever is pre-emption.",
            "eligible_events": ["PostToolBatch", "PreToolUse"],
            "ineligible_events": {
                "Stop": "post-reply. The operator's 2026-08-04 policy: a gate that fires after the reply is sent "
                        "cannot prevent anything, only fail the turn afterwards.",
                "MessageDisplay": "sees the reply but provably cannot block it — that is what makes "
                                  "it safe to observe from, and useless to enforce from.",
            },
            "must_be_handwritten": True,
            "why_handwritten": "an enforcement hook at PreToolUse/PostToolBatch runs where the trust "
                               "root lives; friction_dispatch refuses to mint block-mode there, and "
                               "this does not lower that bar.",
            "what_the_oracle_must_observe": (
                f"whether the turn so far contains the evidence a {kind} claim requires — for "
                f"state_claim a read of the file being described, for decision_attribution a read of "
                f"the record being attributed. Evaluated at PostToolBatch, before the model composes "
                f"the reply, so a missing read can still be supplied rather than punished."),
            "declared_oracle": None,
            "recommended_action": "write_preempt_hook",
            "rationale": f"threshold: >={PREEMPT_MIN_ABS} absolute and >={PREEMPT_MIN_RATE}/100 "
                         f"sustained over a covered window. Both cleared.",
        })
    if out and write:
        try:
            q = json.loads(QUEUE.read_text()) if QUEUE.is_file() else []
        except Exception:
            q = []
        ids = {x["artifact_id"] for x in out}
        q = [x for x in q if x.get("artifact_id") not in ids] + out
        QUEUE.write_text(json.dumps(q, indent=1))
    return out


# ── report ───────────────────────────────────────────────────────────────────────────────────────
def rate(n, replies):
    """Violations per 100 replies. MODULE-LEVEL so the SHIPPED metric can be exercised.

    This was a CLOSURE nested inside another function — importable by nothing, callable by nothing,
    testable by nothing. core-business found it while withdrawing its own claim that this was "the
    only metric in the fleet that provably survives its own claims": what survived was its
    RE-IMPLEMENTATION. A copy that agrees with your reading of the original is evidence about your
    reading, not about the original.

    Nothing about the arithmetic changed. It can now be imported and put under a fixture.
    """
    return (100.0 * n / replies) if replies else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip liveness. Every zero then reads UNVERIFIED — which is the point.")
    args = ap.parse_args()

    live = {} if args.no_probe else probe_liveness()
    v = violations(args.days)
    c = costs(args.days)
    mf = mining_funnel(args.days)

    # STAMP THE LIVENESS RESULT so detect.sh can surface a dead detector without paying for seven
    # subprocesses on every SessionStart and every statusline refresh. The stamp is also what makes a
    # NEVER-RUN probe visible: detect.sh flags a missing or stale stamp, so "nobody has checked whether
    # the detectors work" is itself an item rather than silence. Writing it is the only side effect of
    # a --no-probe-less run, and it is deliberately not written on --no-probe — a skipped probe must
    # not refresh the stamp and make the check look current.
    if live:
        try:
            (STATE / ".si-liveness.json").write_text(json.dumps({
                "checked_at": int(time.time()),
                "failing": sorted(k for k, r in live.items() if not r.get("ok")),
                "total": len(live)}, indent=1))
        except Exception:
            pass


    kinds = sorted(set(v["current"]) | set(v["prior"]) | set(PROBES))
    rows = []
    for k in kinds:
        cu = v["current"].get(k, {"total": 0, "unsourced": 0})
        pr = v["prior"].get(k, {"total": 0, "unsourced": 0})
        r_cur = rate(cu["unsourced"], v["replies_current"])
        r_prv = rate(pr["unsourced"], v["replies_prior"]) if v["prior_covered"] else None
        probe = live.get(k)
        trustworthy = probe is None or probe.get("ok")
        rows.append({"kind": k, "cur_unsourced": cu["unsourced"], "cur_total": cu["total"],
                     "prv_unsourced": pr["unsourced"], "rate_cur": r_cur, "rate_prv": r_prv,
                     "probe_ok": (None if probe is None else bool(probe.get("ok"))),
                     "probe_why": (probe or {}).get("why"),
                     # A zero is only reportable as good news when the detector proved it still works.
                     # A DENOMINATOR OF ZERO WITH A NONZERO NUMERATOR IS NOT "FLAT".
                     # rate() returns None when replies==0, so the improving/worsening comparison is
                     # skipped and the verdict fell through to "flat" — reporting no movement while
                     # violations exist and the denominator is broken. That is a fail-toward-asserting-
                     # more path, which is the one direction nothing here is allowed to fail in. It is
                     # reachable: the transcript directory unreadable, a clock skew putting every
                     # assistant record outside the window, or a Core whose transcripts live elsewhere.
                     "verdict": ("UNVERIFIED" if not trustworthy else
                                 # ZERO REPLIES IS UNSCOREABLE WHETHER OR NOT VIOLATIONS EXIST.
                                 # The first version gated this on unsourced>0, so the case Codex
                                 # actually raised — a peer where the transcript directory is absent, so
                                 # 0 replies AND 0 observations — fell through to "no signal", and the
                                 # footer then said the zeros were real. Zero over zero is not a clean
                                 # record; it is an instrument that measured nothing.
                                 "DENOMINATOR BROKEN" if not v["replies_current"] else
                                 "no baseline yet" if not v["prior_covered"] else
                                 "no signal" if cu["unsourced"] == 0 and pr["unsourced"] == 0 else
                                 "improving" if r_prv is not None and r_cur is not None and r_cur < r_prv else
                                 "worsening" if r_prv is not None and r_cur is not None and r_cur > r_prv else
                                 "flat")})

    broken = [r["kind"] for r in rows if r["probe_ok"] is False]
    nodenom = [r["kind"] for r in rows if r["verdict"] == "DENOMINATOR BROKEN"]
    payload = {"window_days": args.days, "replies_current": v["replies_current"],
               "replies_prior": v["replies_prior"], "detectors": rows, "costs": c,
               "mining_funnel": mf, "liveness_failures": broken,
               "preempts_earned": propose_preempts(v, live, write=False),
               "objective_readable": (
                   "UNSCOREABLE — %d detector(s) failed liveness; their zeros mean nothing"
                   % len(broken) if broken else None)}

    if args.json:
        print(json.dumps(payload, indent=2))
        return 1 if broken else 0

    print(f"SI OBJECTIVE — {args.days}d window\n")
    print("  Replaces: 'minimise ENFORCED blocks subject to shadow detections not rising',")
    print("  which was maximally satisfied at 0 artifacts and 0 promotions — the state the")
    print("  system was actually in while scoring perfectly.\n")

    _cd = time.strftime("%Y-%m-%d %H:%M", time.localtime(v["current_denom_from"]))
    _span_h = (time.time() - v["current_denom_from"]) / 3600
    print(f"  PRIMARY — unsourced violations per 100 replies")
    if v["current_partial"]:
        print(f"  Denominator counts only the {v['replies_current']} replies since the observer went")
        print(f"  live ({_cd}, {_span_h:.0f}h ago) — NOT the whole {args.days}d window. Dividing by the")
        print(f"  full window while the numerator starts late understates the rate by the ratio of")
        print(f"  the spans, and understates it flatteringly.")
    else:
        print(f"  {v['replies_current']} replies this window, {v['replies_prior']} prior.")
    if not v["prior_covered"]:
        since = (time.strftime("%Y-%m-%d %H:%M", time.localtime(v["observer_live_since"]))
                 if v["observer_live_since"] else "never")
        print(f"  NO BASELINE: the observer has only been recording since {since}, which does not")
        print(f"  cover the prior {args.days}d window. Prior rates are UNKNOWN, not zero — reporting")
        print(f"  them as zero would make every real violation found now read as a regression.")
    print(f"  {'behaviour':<24}{'now':>5}{'/100':>8}{'prior/100':>11}   liveness   verdict")
    print("  " + "-" * 76)
    for r in rows:
        rc = "     —" if r["rate_cur"] is None else f"{r['rate_cur']:>6.2f}"
        rp = "        —" if r["rate_prv"] is None else f"{r['rate_prv']:>9.2f}"
        pl = "  —  " if r["probe_ok"] is None else (" ok  " if r["probe_ok"] else "FAIL ")
        print(f"  {r['kind']:<24}{r['cur_unsourced']:>5}{rc:>8}{rp:>11}   {pl:^8}  {r['verdict']}")

    print(f"\n  COSTS (each is paid by the operator, not by the system)")
    print(f"    enforcement fires ....... {c['enforcement_fires']}   (turns they sat through twice)")
    print(f"    shadow would-blocks ..... {c['shadow_would_blocks']}   (free — logged, never acted on)")
    print(f"    injections .............. {c['injections']}")
    print(f"    injected tokens (est) ... {c['injected_tokens_est']}"
          + (f"   [{c['unpriced_injections']} injections had no payload on record — undercount]"
             if c["unpriced_injections"] else ""))

    if mf.get("error"):
        print(f"\n  MINING FUNNEL — unavailable ({mf['error']})")
    else:
        w = mf["window"]
        print(f"\n  MINING FUNNEL — {mf['mined_this_window']} correction(s) mined in the last "
              f"{mf['days']}d (fc_-case pipeline; org {mf['org']})")
        if mf["mined_this_window"]:
            print(f"    installed ........ {w.get('installed', 0):>4}    "
                  f"denied ........... {w.get('denied', 0):>4}    "
                  f"gate_failed ...... {w.get('gate_failed', 0):>4}")
            print(f"    awaiting_ask ..... {w.get('awaiting_ask', 0):>4}    "
                  f"cap_denied ....... {w.get('cap_denied', 0):>4}    "
                  f"duplicate_ask .... {w.get('duplicate_ask', 0):>4}")
            print(f"    ineligible ....... {w.get('ineligible', 0):>4}    "
                  f"still 'mined' .... {w.get('mined', 0):>4}   (not yet routed this window)")
        else:
            print("    nothing new mined this window — see all_time below for the full backlog.")
        _at = mf["all_time"]
        print(f"    all-time (this Core, every case ever mined): "
              + ", ".join(f"{k}={v}" for k, v in sorted(_at.items(), key=lambda kv: -kv[1])))
        if mf["top_denied_reasons"]:
            print("    top denial reasons: " + ", ".join(f"{r}:{n}" for r, n in mf["top_denied_reasons"]))
        _atl = mf["ask_path_trigger_losses"]
        if _atl:
            print(f"    ask-path (generate_from_asks, separate pipeline — recurring asks, not "
                  f"individual corrections) lost {sum(_atl.values())} to a missing trigger this "
                  f"window: {dict(_atl)}")

    if nodenom:
        _obs_n = sum(r["cur_unsourced"] for r in rows)
        print(f"\n  ⛔ DENOMINATOR BROKEN — 0 replies counted in the window, so no rate exists.")
        print(f"     {_obs_n} unsourced observation(s) on record. Zero over zero is not a clean record;")
        print(f"     it is an instrument that measured nothing, and it must not read as good news.")
        print(f"     Looked in: {TRANSCRIPTS}")
        print(f"     A rate cannot be computed and MUST NOT be reported as flat. Check that the"
              f" transcript")
        print(f"     directory exists and is readable before reading anything above.")
        return 1

    if broken:
        print(f"\n  ⛔ UNSCOREABLE. {len(broken)} detector(s) failed the liveness probe: "
              f"{', '.join(broken)}")
        for r in rows:
            if r["probe_ok"] is False:
                print(f"       {r['kind']}: {r['probe_why']}")
        print("     Their counts are not evidence of anything. A broken detector and a clean")
        print("     record produce the identical zero — telling those apart is the whole reason")
        print("     this probe exists. Fix the detector before reading any number above.")
        return 1

    if args.no_probe:
        print("\n  liveness SKIPPED (--no-probe) — every zero above is UNVERIFIED.")
        return 0

    props = propose_preempts(v, live, write=not args.no_probe)
    if props:
        print(f"\n  PRE-EMPT EARNED — {len(props)} class(es) crossed the threshold and are queued")
        print(f"  in oracle-request-queue.json for a hand-written hook:")
        for pr in props:
            print(f"    {pr['case_id']}: {pr['why'][:110]}")
    else:
        print("\n  no class has earned a pre-empt hook. Supply is cheaper wherever it reaches, and a")
        print(f"  per-turn nudge is only worth its token cost above {PREEMPT_MIN_ABS} absolute /"
              f" {PREEMPT_MIN_RATE}/100 on a covered window.")

    worse = [r["kind"] for r in rows if r["verdict"] == "worsening"]
    better = [r["kind"] for r in rows if r["verdict"] == "improving"]
    # THE MIXTURE, SURFACED IN THE FINDING — not merely stamped on the rows. decisions-log.md
    # claimed this was already true on 2026-08-09; the stamping shipped and the surfacing did not,
    # and `grep obs_version bin/si-objective.py` returned nothing while 408 of 429 rows carried no
    # generation at all. Rates that span a detector change average two instruments — the confound
    # that voided the 82% headline, reproduced inside the tool built to measure this system.
    _mix = v.get("generation_mix") or {}
    if len(_mix) > 1:
        _parts = ", ".join(f"{k}:{n}" for k, n in sorted(_mix.items(), key=lambda x: -x[1]))
        _pre = _mix.get("pre-instrumentation", 0)
        _tot = sum(_mix.values()) or 1
        print(f"\n  ⚠ MIXED DETECTOR GENERATIONS in this window — {_parts}")
        print(f"    {100.0 * _pre / _tot:.0f}% of rows predate evidence capture and CANNOT be re-scored.")
        print("    A rate spanning a detector change averages two instruments. Compare within a")
        print("    generation, or forward-only from the newest sha.")

    print(f"\n  all {len(rows)} detectors passed liveness — the zeros above are real zeros.")
    if better:
        print(f"  improving: {', '.join(better)}")
    if worse:
        print(f"  WORSENING: {', '.join(worse)} — supply is not reaching the model for these.")
    if not v["prior_covered"]:
        print("  Trend is not computable yet — this window IS the baseline. Re-run after the")
        print("  observer has covered a full prior window and the primary term becomes readable.")
    elif not better and not worse:
        print("  no movement either way. If the loop installed anything this window, it bought")
        print("  nothing measurable and its injected-token cost above is the whole of its effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
