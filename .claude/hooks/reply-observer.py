#!/usr/bin/env python3
"""MessageDisplay — watch the reply as it streams and RECORD violations. Never blocks. Never injects.

WHY THIS REPLACES THE STOP GATES
--------------------------------
The operator's policy, 2026-08-06: nothing may act after the reply is sent — it all has to happen
before the reply is given; a Stop hook after the reply is useless.

This is right about the experience: a Stop gate fires after the reply is written, so the operator
reads the wrong thing, then reads a footnote correcting it, and the tokens for both are already spent.

But the architecture cannot simply relocate those gates, and this was TESTED rather than assumed on
2026-08-06. MessageDisplay is the only event that sees the reply text. A probe returning exit 2 on a
sentinel chunk was IGNORED and the sentinel reached the terminal. So:

    MessageDisplay sees everything and can stop nothing.
    No surface intercepts model text before it is pasted.

Which means a check that reads the finished reply can never be pre-reply. It has to be REPLACED. The
replacement is three layers, and this file is the third:

    SUPPLY    SessionStart / UserPromptSubmit   make the mistake impossible (clock, duration, digest)
    PRE-EMPT  PreToolUse / PostToolBatch        nudge BEFORE the model writes, when evidence is absent
    OBSERVE   MessageDisplay  <- this file      record what still got through, at zero cost

WHY OBSERVING IS WORTH MORE THAN BLOCKING HERE
----------------------------------------------
The thing this system could not produce, all through 2026-08-05, was an honest count of how often each
mistake ACTUALLY happens. `inject-efficacy.py` had to infer it from corrections Nick bothered to type.
A blocking gate is a terrible instrument for that: it fires, the turn is redone, and the event is
recorded as "prevented" whether or not the model would have made the mistake — and false positives are
indistinguishable from catches. The time-claim gate's 11 blocks in one session turned out to include
at least one false positive found only because it fired on a reply quoting its own trigger words.

An observer has none of that. It sees the real, final, unretried text. It costs no tokens, adds no
latency the user perceives, and produces exactly the denominator the loop needs: violations per reply.
Once supply and pre-empt are in place, this number is how we know whether they worked — rather than
counting blocks, which measures the gate rather than the behaviour.

WHAT IT WATCHES, and why each one is safe to detect without reading intent
-------------------------------------------------------------------------
Only patterns that are mechanically decidable from the text plus the turn's tool calls. Anything
needing judgement is out of scope by construction — this must never become a thing that guesses.

CONTRACT — deliberately the weakest possible, mirroring event-probe.py:
  · reads stdin, appends one line, exits 0
  · never blocks (it structurally cannot on this event), never injects
  · every failure path is a silent exit 0, because it runs on the hot path of every chunk

    events -> .claude/state/reply-observations.jsonl
    read with -> python3 bin/reply-violations.py
"""
from __future__ import annotations

import json
import os
import hashlib
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

import pathlib

_HERE = Path(__file__).resolve()

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CORE_INSTANCE")
            or Path(__file__).resolve().parents[2])
LOG = ROOT / ".claude" / "state" / "reply-observations.jsonl"
MAX_BYTES = 512 * 1024

# ── PER-TURN ACCUMULATION, and why the obvious implementation was a hole ─────────────────────────────
#
# MessageDisplay fires once per streamed CHUNK, not once per reply. The first version ran every detector
# against the individual `delta`, which looks right and is not: a violation split across a chunk
# boundary matches nothing.
#
#     delta 1: "All five "
#     delta 2: "Cores now have this."
#
# Neither chunk matches _CROSS_CORE, the reply still counts toward the denominator, and the objective
# improves while the behaviour is identical. Codex found this 2026-08-06 as a deliberate gaming vector,
# but the worse reading is that it was ALREADY corrupting the numbers: chunk boundaries fall wherever
# the stream happens to break them, so an unknown share of real violations was being silently dropped,
# and every liveness probe still passed because a probe supplies its violation as one delta.
#
# So detection runs on the ACCUMULATED text for the turn. Two consequences to handle:
#   · re-detection — once matched, the phrase stays in the buffer and every later chunk re-matches it.
#     Deduped on (kind, matched) per turn.
#   · unbounded growth — capped at ACCUM_MAX. The cap can in principle split a violation the same way a
#     chunk boundary did, so it is set far above any real reply rather than at a tidy round number.
# IT LIVES OUTSIDE THE REPO, and that is the whole point of this paragraph.
#
# The first version put it in .claude/state/reply-accum/. sentinel-code BLOCKED the push and it was
# right: core-life's .gitignore whitelists .claude/state/ file by file rather than ignoring the
# directory, 94 state files are tracked today, and `git add -A --dry-run` confirms the buffer stages.
# session-lifecycle.sh runs `git add -A` on close, and defensive-save.sh drives that same routine on
# the walk-away path — so the exact scenario defensive-save exists for is the one guaranteed to commit
# a buffer of raw reply text into permanent git history. Each Core pushes its own repo to GitHub. On
# core-finance that is brokerage material; on core-business, employer material.
#
# The proposed minimal fix was a .gitignore line, and I did add one — but .gitignore is NOT in the sync
# manifest, so that fix protects core-life and none of the four Cores that pull this file. A fix has to
# travel with the code, so the correct answer is that this data never enters the repo at all.
#
# It belongs outside regardless of git: the buffer's entire lifetime is one reply. It is not state, it
# is working memory that happens to need to survive between two hook invocations a few hundred
# milliseconds apart. .claude/state/ is for things worth keeping.
# HASHED, NOT TRUNCATED. The first version namespaced on the last 24 characters of the de-punctuated
# root path. That happens to be collision-free across the five real Core paths — they differ in the
# tail — but it only works because the DISTINGUISHING segment sits at the end. Two roots differing in a
# middle segment (/work/core-life vs /other/core-life) collide, and a collision here means two Cores
# sharing an in-flight reply buffer, which is the exact cross-Core leak the namespacing exists to
# prevent. A digest does not care where in the path the difference is.
_TMP = Path(tempfile.gettempdir()) / f"core-reply-accum-{os.getuid()}"
ACCUM_DIR = _TMP / hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
ACCUM_MAX = 64 * 1024
ACCUM_TTL = 3600

# Which detectors' matched text is DATA rather than PHRASING.
#
# `matched` exists to say WHICH phrasing fired, and for six of the seven detectors the match IS the
# phrasing — "fleet-wide", "is live", "you decided", "here's the table". Those are patterns, and storing
# them is what makes the log diagnosable.
#
# financial_figure is different in kind: its pattern is a currency amount, so the match is the VALUE.
# On core-finance that means a brokerage balance written verbatim into a tracked file and pushed to
# GitHub. The log is tracked by design — that is how all 94 state files work, and per_core_keep already
# stops it reaching the shared baseline — so the fix belongs here, at what gets written, not in an
# ignore rule that does not travel. Digits masked, shape kept: "$12,431.88" -> "$##,###.##", which
# preserves everything the measurement needs and none of the figure.
_REDACT_DIGITS = {"financial_figure"}

# ── Detectors. Each is (name, regex, what_would_source_it).
#
# A DURATION or elapsed-time claim. The injected clock supplies NOW; it cannot supply elapsed, so this
# is the class that genuinely needs a tool call — and the class the Stop gate kept catching.
# The COMPACT form `25h35m` is listed first and deliberately does not require a trailing word
# boundary: that is exactly what bin/compute-session-duration.sh prints, and requiring \b after the
# `h` failed on it because the next character is a digit, not a boundary. It was the single most
# important case to catch and the first version missed it — found by unit-testing the detector against
# real sentences from this session rather than invented ones.
_DURATION = re.compile(
    r"(\d+\s*h(?:ou)?r?s?\s*\d+\s*m(?:in)?(?:ute)?s?"          # 25h35m, 2 hr 5 min
    r"|\b\d+\s*(?:h|hr|hrs|hours?|min|mins|minutes?)\b"        # 3h, 45 minutes
    r"|\ban?\s+(?:hour|day|week)\s+ago\b"
    r"|\b(?:this|last)\s+(?:morning|afternoon|evening|night)\b"
    r"|\b(?:tonight|yesterday|today)\b"
    r"|\bearlier\s+(?:today|tonight)\b)", re.I)

# A STATE assertion about the system — "X is broken", "Y is done", "nothing references Z".
_STATE = re.compile(
    r"\b(?:is|are|was|were)\s+(?:now\s+)?(?:broken|failing|dead|gone|missing|absent|fixed|done|"
    r"complete|clean|empty|unused|stale|live|enabled|disabled|registered)\b", re.I)

# A CROSS-CORE completion claim — the class that produced a false fleet-wide statement on 2026-08-05.
_CROSS_CORE = re.compile(
    r"\b(?:all\s+(?:five|5|four|4)\s+cores?|every\s+core|fleet[- ]wide|across\s+all\s+cores?|"
    r"on\s+all\s+cores?)\b", re.I)

# A DECISION attributed to Nick.
_DECISION = re.compile(
    r"\b(?:you|nick)\s+(?:decided|declined|approved|agreed|chose|rejected|said\s+no)\b", re.I)

# ── THREE CLASSES ADDED 2026-08-06 AFTER SENTINEL-CODE BLOCKED THE PUSH.
#
# I retired nine Stop gates with ONE boilerplate reason copied across all nine, and the reason
# asserted coverage that did not exist. sentinel-code checked each gate's guard against the four
# detectors above and found three with no supply, no pre-empt and no observation — not "replaced",
# just gone. It was right, and the boilerplate is what let me not notice: a single reason reused nine
# times is a claim about nine different things verified once.

# SAY-DO GAP: promising a write and not making one. Mechanically decidable — promise language in the
# text, and no mutating tool call in the turn. This is the class CLAUDE.base.md names as one of the
# three original anti-patterns, so retiring it uncovered would have been the worst of the three.
# A FORWARD COMMITMENT ONLY. The tense marker used to be OPTIONAL — `I(?:'ll| will| have)?\s+add`
# — so bare past-tense narration matched: "I added", "written to", "recorded that". Measured on the
# live log, those ARE the unsourced rows: 3 "written to", 2 "I added", 1 each of "written it",
# "recorded that", "recorded it".
#
# THE ITEM IS "A PROMISE TO SAVE MUST BE FOLLOWED BY A WRITE THAT TURN." A past-tense report can
# refer to a write from an EARLIER turn — summarising work already done is the normal shape of a
# report — so demanding same-turn evidence for it marks correct behaviour as a violation. The
# tense marker is now REQUIRED.
#
# WHAT THIS DELIBERATELY STOPS MEASURING, said plainly rather than dropped: "I added X" when no
# write ever happened is a real and worse defect — a claimed completion. It is NOT this class, and
# the observer cannot decide it, because the evidence would be in a different turn than the claim.
# Measuring it needs cross-turn state this hook does not have. Narrowing here is honest only
# because the gap is named.
_SAY_DO = re.compile(
    r"\b(?:I(?:'ll|'m going to| will| am going to| shall)\s+(?:sav|writ|updat|append|add|record|"
    r"log|creat|stor|persist)\w*"
    r"|(?:let me|going to)\s+(?:sav|writ|updat|append|add|record|log)\w*"
    r"|I(?:'ll| will)\s+(?:go ahead and\s+)?(?:do|put|note)\s+that\b)", re.I)

# FINANCIAL FIGURE: a currency amount. Nick's hard rule is that account state is pulled live, never
# recalled — so the sourcing test is an actual account read in the same turn.
# A CURRENCY SHAPE, not a dollar sign followed by digits. Tightened 2026-08-25.
#
# The previous pattern was `\$\s?[\d,]+(?:\.\d{2})?\b`, which matches `$1` — and therefore matches
# every shell positional parameter this Core writes into a reply. `awk '{print $1,$2,$4}'` scored as
# an unsourced financial claim. So did "the parent re-invokes `$1` after cd'ing to the seat", and so
# did "15/15 at $0", a probe score.
#
# That mattered because the number was believed. si-objective's propose_preempts read
# core-life's count as 10 unsourced financial claims across 826 replies (1.21/100), cleared its
# threshold, and filed a work order for a hand-written pre-emption hook. The behaviour it asked
# someone to go prevent was, on this seat, substantially awk.
#
# Found by reading the stored evidence rows instead of the count: the claim snippets are about
# guard defects, probe trajectories and bus messages, and the redacted match tokens are bare `$#`.
# The same shape of error as the always-unsourced recall_first detector documented below — a class
# whose number looks like a behavioural problem and is an instrument problem, which invites you to
# go fix the behaviour. On core-finance, where real balances live, the detector was doing its job;
# a seat-blind pattern is how one seat's correct detector becomes another seat's phantom.
#
# Now requires one of three currency shapes: a cents decimal ($0.18), a thousands comma ($1,200),
# or two-or-more digits ($50). Verified against both directions — every real shape still matches
# ($12,431.88, $1,204, $0.18, $10/$50 per MTok, $2,500.00, 40 USD), every shell positional now
# misses ($1..$9 in awk/cut/loops/prose).
#
# ACCEPTED LOSS, stated rather than hidden: a bare single-digit amount no longer counts, so "$0" as
# a genuine zero-cost claim and "$5 flat" as a price stop being measured. That is the price of
# excluding $1-$9, and it is the right trade only because an ACCOUNT figure — the thing Nick's
# pulled-live-never-recalled rule is actually about — is essentially never a bare single digit.
# If that stops being true, widen it back and find another discriminator for the shell case.
_FINANCIAL = re.compile(
    r"("
    r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?\b"    # $1,200   $12,431.88
    r"|\$\s?\d+\.\d{2}\b"                        # $0.18    $12.50
    r"|\$\s?\d{2,}\b"                            # $50      $100
    r"|\b\d+(?:\.\d+)?\s*(?:USD|dollars)\b"      # 40 USD   3.5 dollars
    r")", re.I)

# DELIVERABLE FORMAT: describing something Nick will VIEW while pasting it as chat text instead of
# producing a file or artifact. Sourced by an actual Write/Artifact/SendUserFile in the turn.
_DELIVERABLE = re.compile(
    # The determiner and the up-to-two intervening words were added 2026-08-06 after the
    # si-objective liveness probe failed on "Here's the FULL table" — the original pattern demanded
    # the noun sit immediately after "the", so the single most natural phrasing of this mistake
    # ("here's the full table", "here's the complete report", "here's a quick diagram") walked
    # straight past it. The detector had never fired, and without the probe that zero would have read
    # as good behaviour indefinitely. Bounded at two words so it stays a phrase match and does not
    # start spanning clauses.
    # The gap may hold modifiers ("full", "complete", "quick") but NOT a determiner or copula. Without
    # that lookahead the two-word gap spans a clause boundary and "here is the reason the chart broke"
    # matches — a sentence about a chart, not a chart pasted into the reply. Checked both directions:
    # five natural phrasings hit, four near-misses stay clean.
    r"\b(?:here(?:'s| is)\s+(?:the|a|an|your|my)\s+"
    r"(?:(?!(?:the|a|an|that|this|which|is|was|were|reason|why|when|because)\b)\w+\s+){0,2}"
    r"(?:table|diagram|chart|report|doc|document|spreadsheet|map)"
    r"|the\s+(?:diagram|chart|architecture\s+map)\s+(?:below|above)"
    r"|as\s+shown\s+below)\b", re.I)

# T10 / recall_first — A STRATEGIC FRAMING ASSERTED WITHOUT GREPPING THE DECISIONS LOG FIRST.
#
# THE ITEM HAD NO DETECTOR AT ALL. `recall_first` was listed in casebook TRANSCRIPT_CLASS and in
# nothing else: no entry in DETECTORS, no source pattern, no observation ever recorded. So T10 could
# only ever report NO-DATA, and it did — for as long as it has existed. Two independent absences
# with one visible symptom, and the symptom looked like "we have no data yet" rather than "nothing
# is looking".
#
# rules/memory.md records why the item exists: this failed TWICE as a discipline rule before being
# promoted to a session-start load. The class is Nick's — strategic direction asserted from memory
# instead of from the record.
#
# Deliberately narrow. It fires on a DIRECTIONAL claim about what was decided or what the strategy
# is, not on the word "decision". A matcher that fires on ordinary prose produces a rate nobody
# trusts, which is the same uselessness as one that never fires.
_RECALL_FIRST = re.compile(
    r"\b(?:"
    r"the\s+(?:plan|strategy|direction|path|track|approach)\s+(?:is|was|has\s+been)\b"
    r"|we(?:'re| are)\s+(?:going\s+with|pursuing|on)\s+(?:path|track|option)\b"
    r"|(?:path|track|option)\s+[A-C]\b"
    r"|the\s+decision\s+(?:was|is)\b"
    r")", re.I)

# ── scope_claim (2026-08-11) ─────────────────────────────────────────────────────────────────────
#
# A UNIVERSAL OR ABSENCE CLAIM MADE FROM ONE READ. "there is no X", "all N are", "none of them
# have", "nothing references it". The rule it observes is already in .claude/rules/memory.md and is
# there because it has cost real money: *"Absence claims need a multi-file grep, not one read. One
# read LOOKS like diligence while verifying nothing when the claim's truth lives in a different
# doc."*
#
# core-business ruled the wider 8-case pattern unenforceable and named this the one decidable slice,
# and it is decidable for a specific reason: the claim's SHAPE is universal, so a single read cannot
# be sufficient evidence no matter what it found. Every other class here asks "did you look?"; this
# one asks "did you look in more than one place?", which is a countable property of the turn.
#
# Measured on 3,975 real assistant replies from this Core: fires on 143 (3.6%), in the same range as
# the artifact gate's 3% ceiling and instruction-directive's 7.4%. The first version was 4.1% and
# caught "and nothing else" — a qualifier, not an absence claim — so `nothing` now requires an
# existence verb after it.
_SCOPE = re.compile(
    r"\b(?:"
    r"(?:there\s+(?:is|are|was|were)\s+no|there'?s\s+no)\s+\w+"
    r"|no\s+(?:such\s+)?\w+s?\s+(?:exist|exists)\b"
    r"|nothing\s+(?:exists|matches|references|calls|reads|writes|uses)\b"
    r"|(?:all|every|none\s+of)\s+(?:the\s+|of\s+)?\w+\s+(?:are|is|have|has|use|uses|carry|carries|were)\b"
    r"|not\s+(?:present|used|referenced|called|defined)\s+anywhere"
    r"|anywhere\s+in\s+the\s+(?:repo|codebase|fleet|tree)"
    r")", re.I)

DETECTORS = (
    ("duration_claim", _DURATION),
    ("state_claim", _STATE),
    ("cross_core_claim", _CROSS_CORE),
    ("decision_attribution", _DECISION),
    ("say_do_gap", _SAY_DO),
    ("financial_figure", _FINANCIAL),
    ("deliverable_format", _DELIVERABLE),
    ("recall_first", _RECALL_FIRST),
    ("scope_claim", _SCOPE),
)


def _self_name() -> str:
    """This Core's own name, from its directory. Never hardcoded — that is the defect being fixed."""
    try:
        for part in [_HERE] + list(_HERE.parents):
            n = getattr(part, "name", "")
            if n.startswith("core-") and n[5:] not in ("bus", "brain", "ui"):
                return n[5:]
            if n == "core":
                return "life"
            # Fork layout: an external fork's Core can be named `riverside-core`, not `core-riverside`. Without this the fork
            # resolves to "" and excludes nothing from its own peer list. sentinel-code flagged it
            # on the push review; it is a correctness gap on a fork we cannot see, which is the
            # category this codebase keeps shipping.
            if n.endswith("-core") and n[:-5] not in ("", "bus", "brain", "ui"):
                return n[:-5]
    except Exception:
        pass
    return ""


def _peer_alternation() -> str:
    """Regex alternation of every Core that is NOT me.

    ROSTER FIRST, directory discovery only as a fallback. My first version discovered
    `core-*` directories and picked up `core-ux` — the UI project, not a Core — which would have
    credited a read of the UI repo as a cross-Core source. Guessing membership from a naming
    convention is how the hardcoded list happened in the first place; core-bus/roster.json is the
    declared answer and it is what `--to all` already routes on.
    """
    me = _self_name()
    names = set()
    try:
        import json as _json
        roster = pathlib.Path.home() / "AI Projects" / "core-bus" / "roster.json"
        if roster.is_file():
            names = {str(n) for n in _json.loads(roster.read_text()).get("cores", []) if n}
    except Exception:
        names = set()
    if not names:
        try:
            for d in _HERE.parents:
                if (d / "core-brain").is_dir():
                    for c in d.iterdir():
                        n = c.name
                        if c.is_dir() and n.startswith("core-") and n[5:] in (
                                "life", "business", "school", "finance", "ops"):
                            names.add(n[5:])
                    break
        except Exception:
            pass
    if not names:
        names = {"life", "business", "school", "finance", "ops"}
    peers = sorted(n for n in names if n and n != me)
    # re.escape EVERY name. Found by sentinel-code on the baseline push review: these names come
    # from an EXTERNAL, UNSYNCED file (core-bus/roster.json) and are joined straight into a regex
    # that is compiled at MODULE IMPORT time inside a dict literal with no try/except. A roster
    # entry containing an unbalanced paren would therefore raise uncaught at import and take the
    # observer down on every Core that pulled it. Today's roster is clean, so this was latent —
    # which is exactly the kind of thing that ships because it works on the writer's machine.
    peers = [re.escape(n) for n in peers]
    return "|".join(peers) if peers else "business|school|finance|ops"



# ── SUPPLY MUST ANSWER THE CLAIM, NOT MERELY BE PRESENT (2026-08-08) ──────────────────────────
# THE DEFECT, found by core-business and reproduced on both Cores. `sourced` was computed from the
# INJECTION ALONE — the claim text was never consulted. Since session-presence injects the clock and
# the PEERS line on EVERY prompt, both supply-backed classes were credited on every turn they fired.
# The case that settles it:
#
#     claim "it is 3pm"   +   supply line reading "19:55 PDT"   ->   sourced=True
#
# The supply CONTRADICTS the claim and credits it anyway. That is not approximate sourcing; the
# instrument was crediting a claim with the evidence that refutes it.
#
# FIX: supply counts only when the CLAIM is of a kind the supply can actually answer. START+WALL
# genuinely answers "this session has run N hours"; it cannot answer "I ran that command 20 minutes
# ago" or "it is 3pm". The PEERS line carries HEADs and baseline SHAs, so it genuinely answers "every
# Core is synced to X"; it cannot answer "school's sentinel.md has 0 VERDICT occurrences".
#
# Anything not matching falls through to requiring a real tool call, which is the safe direction.
# THE INSTRUMENT'S OWN VERSION, stamped on every row it writes.
#
# transcript_score reads the STORED `sourced` verdict — it does not re-derive it, and it should not:
# rewriting recorded observations to match a newer instrument would be manufacturing history. But
# that means a rate computed over a window spanning an instrument change AVERAGES TWO DIFFERENT
# INSTRUMENTS and reports one number.
#
# That is exactly the confound core-business named this morning about model_id: nothing recorded
# which model produced the measured behaviour, so every longitudinal claim was confounded with the
# vendor shipping better weights. Same shape, one layer in — nothing recorded which DETECTOR
# produced the verdict.
#
# Bump this whenever a matcher, source pattern or supply pairing changes.
# DERIVED FROM THIS FILE'S OWN BYTES, not declared. The hand-maintained string below it shipped
# yesterday and was already a lie waiting to happen: it only changes when someone REMEMBERS to
# change it, and yesterday's whole lesson was that disciplines do not hold — the gate caught
# core-business, the rule did not. A detector edit that forgets to bump a constant produces rows
# labelled as the previous generation, which is worse than no label at all.
#
# core-business (#916): "If you ship only one field, ship observer_sha. Twelve bytes turns an
# unlabelled corpus into a labelled one. RIGHT NOW IT IS IMPOSSIBLE TO TELL FROM THE LOG WHICH ROWS
# CAME FROM WHICH DETECTOR GENERATION — precisely the confound that voided the 82% headline claim,
# still live in this file after we spent a day on exactly that class."
def _observer_sha() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except Exception:
        return "unknown"


OBSERVER_SHA = _observer_sha()
OBS_VERSION = "2026-08-09.2"   # RETAINED for the rows that already carry it; superseded by the sha

_SUPPLY_CLAIM_GUARD = {
    # session-scoped elapsed claims only — the thing START+WALL actually answers.
    "duration_claim": re.compile(r"\b(session|so far|we(?:'ve| have) been|this session|since\s+(?:we|start))\b", re.I),
    # sync/state-of-the-fleet claims only — what HEAD and baseline SHAs actually answer.
    "cross_core_claim": re.compile(r"\b(sync|synced|baseline|head|behind|ahead|pulled|commit|sha|up[- ]to[- ]date)\b", re.I),
}

# Sourcing evidence, read from the turn's tool calls in the transcript. Mirrors the Stop gates'
# notion of "sourced this turn" so the numbers are comparable to the old ones.
# A SECOND SUPPLY FOR THE SAME CLASS, and its absence is most of T6's number.
#
# session-presence injects TWO distinct facts on every prompt:
#     ⏰ <now> (live, this turn)          answers WHAT TIME IT IS
#     session START <ts> · WALL <dur>     answers HOW LONG IT HAS BEEN
#
# Only the second was registered, and its guard correctly restricts it to elapsed-session claims
# ("this session", "so far", "we've been"). So a TIME-OF-DAY word — tonight, today, this morning —
# matched no supply at all and scored UNSOURCED even though the clock sat in the same turn's
# context. Measured on the live log: of 175 unsourced duration claims, "tonight" is 90, "today" 43,
# "this morning" 6, "earlier today" 5, "earlier tonight" 4, "this afternoon" 2 — the overwhelming
# majority of the largest class in the suite, and the rate is quoted.
#
# THE PAIRING IS THE POINT, not the leniency: a supply may only vouch for the claims it actually
# answers. The clock cannot source "we have been working 3 hours" and START/WALL cannot source
# "good evening". Keeping them separate is what stops this becoming an always-clean detector, which
# is the failure core-business found in S2 an hour ago.
_SOURCE_SUPPLY_EXTRA = {
    "duration_claim": [
        (re.compile(r"⏰|\blive, this turn\b", re.I),
         re.compile(r"\b(tonight|today|this\s+(?:morning|afternoon|evening)|earlier\s+(?:today|tonight)"
                    r"|yesterday|right\s+now|currently|good\s+(?:morning|afternoon|evening))\b", re.I)),
    ],
}


_SOURCE_TOOLS = {
    "duration_claim": re.compile(r"\b(date|compute-session-duration|\.session-start)\b", re.I),
    "state_claim": re.compile(r"", re.I),          # any read counts; handled below
    # PEER LIST IS DERIVED, NOT HARDCODED (2026-08-08, found by core-business from a non-life seat).
    # This read (core-(business|school|finance|ops)|peer-|...) — "the other Cores" enumerated from
    # LIFE's seat, with core-life itself absent. On every peer, "the other Cores" includes life, so a
    # business claim about core-life sourced by reading life's tree produced a blob containing
    # "core-life", which this pattern did not match. The read happened; the observer scored it
    # unsourced. Correct on the writer's Core, silently wrong on all four pullers — the fifth
    # instance of that shape found in one night. _peer_alternation() builds it from who this Core
    # actually is.
    "cross_core_claim": re.compile(r"(core-(" + _peer_alternation() + r")|peer-|ls-remote|baseline)", re.I),
    "decision_attribution": re.compile(r"(decisions-log|query\.py|recall_|brain)", re.I),
    # RECALL_FIRST HAD NO ENTRY AT ALL, so _SOURCE_TOOLS.get("recall_first") returned None and
    # `bool(srx and ...)` was False on every turn. THE CLASS COULD NEVER RETURN SOURCED — which is
    # why casebook T10 sat at NO-DATA with "NEGATIVE PROBE FAILED: a correctly-sourced turn still
    # yields sourced=False". Not a tuning problem: there was nothing to tune.
    #
    # An always-unsourced detector is the exact twin of the always-clean one core-business found in
    # S2 this hour, and it is the more insidious of the two — a class stuck at 100% violation looks
    # like a real behavioural problem, so it invites you to go fix the behaviour instead of the
    # instrument. T10 has been reporting a failure that could not have been anything else.
    #
    # Sourced by a read of the decisions log, a brain recall, or a session-log read — the three
    # things "grep the decisions log first" actually means in this codebase.
    "recall_first": re.compile(
        r"(decisions-log|recall_similar|get_entity|core-brain|query\.py|sessions/\d{4}-)", re.I),
    # A promise to write is sourced by an actual mutating tool call in the same turn.
    # A promise to write is sourced ONLY by a mutating tool call. Was:
    #     r'"(file_path|old_string|new_string|content)"'
    # which a plain Read satisfies, because Read's input carries file_path too. Now keyed on the
    # tool NAME, which is available since the blob stopped discarding it.
    "say_do_gap": re.compile(r'"name":\s*"(Write|Edit|NotebookEdit|MultiEdit)"', re.I),
    # An account figure is sourced by a live account read — never by recall. Nick's hard rule.
    "financial_figure": re.compile(r"(robinhood|era_|account|balance|positions|brokerage)", re.I),
    # A viewable deliverable is sourced by having actually produced a file or artifact.
    "deliverable_format": re.compile(r'("file_path"|Artifact|SendUserFile|\.html|\.png|\.svg|\.docx)', re.I),
}

# SUPPLY COUNTS AS A SOURCE. This is the whole point of the architecture and the first version of
# this file missed it — which the observer then caught on its own first day of data.
#
# It flagged `35h29m` as an UNSOURCED duration claim. That number was INJECTED by
# session-presence.py's supply on the very same turn. No tool call ran because none needed to; the
# evidence was already in context. Marking it a violation is exactly the false positive the old
# time-claim gate produced, rebuilt by me hours after removing that gate for it.
#
# So sourcing is (tool call) OR (the supply that answers this class was present in the turn's
# injected context). Anything else makes the successful case indistinguishable from the failure and
# guarantees the number never falls, no matter how well supply works — the same "criterion that
# cannot detect success" defect Fable found in the top-line metric.
_SOURCE_SUPPLY = {
    # session-presence injects "⏰ ... · session START <ts> · WALL <d>". START+WALL is what makes an
    # elapsed claim legitimate; the bare clock alone is NOT, since it cannot source elapsed.
    "duration_claim": re.compile(r"session\s+START\b.*\bWALL\b", re.I | re.S),
    # session-presence also injects "PEERS (this Core is life; ...): business@<sha> baseline:<sha> ..."
    # — each peer's HEAD and last-synced baseline. That makes an "all Cores have X" claim checkable
    # from context, which is what the retired cross-core gate was demanding a tool call for. Shipped
    # 2026-08-06 because this was the top REAL violation in the observer's first day of data.
    "cross_core_claim": re.compile(r"\bPEERS\b.*baseline:", re.I | re.S),
    # NO SUPPLY IS POSSIBLE for these two, and that is a fact about the classes rather than a gap.
    # An arbitrary system-state assertion needs the specific file read, and a decision attribution
    # needs the specific record — neither can be pre-supplied cheaply every turn without injecting
    # the whole repo. These are the classes the PRE-EMPT layer (PostToolBatch) has to cover, and
    # pretending otherwise would make the observer credit claims nothing actually sourced.
    "state_claim": None,
    "decision_attribution": None,
    # These three are PRE-EMPT territory, not supply territory: each needs a specific action in the
    # turn (a write, an account pull, a file produced), and no cheap per-turn injection substitutes
    # for having done it. Naming them None keeps the observer honest about that rather than crediting
    # claims nothing sourced.
    "say_do_gap": None,
    "financial_figure": None,
    "deliverable_format": None,
}



def sourced_for(name, matched, blob, injected):
    """THE one place sourcing is decided. Exported so probes exercise the REAL path.

    The casebook probes originally re-derived this from _SOURCE_TOOLS/_SOURCE_SUPPLY, which meant
    two implementations of one rule — the exact defect the Casebook exists to catch, and it went
    stale within minutes: the moment this file was fixed, the probes were still testing the old
    dicts and kept reporting classes as broken after they had been repaired. A probe that can
    disagree with the thing it probes is not a probe.
    """
    srx = _SOURCE_TOOLS.get(name)
    if name == "scope_claim":
        # TWO DISTINCT READS, not two reads. Re-reading one file twice is one place looked, and the
        # claim being observed is about EVERYWHERE. Counting calls rather than targets would score
        # a doubled read as diligence, which is the exact substitution the rule warns about.
        _targets = set(re.findall(r'"(?:file_path|pattern|path)"\s*:\s*"([^"]+)"', blob or ""))
        _cmds = set(re.findall(r'"command"\s*:\s*"([^"]*(?:grep|rg|find|ls)[^"]*)"', blob or ""))
        s = len(_targets | _cmds) >= 2
    elif name == "state_claim":
        s = bool(re.search(r'"(file_path|pattern|command)"', blob or ""))
    elif name == "financial_figure":
        # DIGITS-PROVENANCE, NOW TOLERANT OF ROUNDING. The check was: strip non-digits from the
        # claim and require that string to appear in the blob's digits. So "$2.00" quoted from a
        # tool result reading 1.9950964999999998 scored UNSOURCED — the digits "200" are nowhere in
        # "19950964...". Measured: 14 of 19 unsourced rows are exactly this shape ($#.##, $#, $##).
        #
        # Rounding a measured number is normal and correct. The rule this enforces is Nick's — an
        # account figure is pulled live, never recalled — and a figure that ROUNDS from a number in
        # the same turn's tool output IS pulled live. A literal-substring test cannot express that.
        #
        # Still strict where it matters: the number must be PRESENT in the turn's output at the
        # claimed precision. A recalled figure with no corresponding tool number stays unsourced.
        s = False
        if blob:
            claim = re.sub(r"[^\d.]", "", matched or "")
            try:
                cv = float(claim) if claim and claim.count(".") <= 1 else None
            except ValueError:
                cv = None
            if cv is not None:
                dec = len(claim.split(".")[1]) if "." in claim else 0
                for num in re.findall(r"\d+(?:\.\d+)?", blob):
                    try:
                        if round(float(num), dec) == round(cv, dec):
                            s = True
                            break
                    except ValueError:
                        continue
            if not s:
                # COMPARE AGAINST PARSED NUMBER TOKENS, NOT A DIGIT SOUP (core-finance, 2026-08-13).
                #
                # This was `d[:4] in re.sub(r"[^\d]", "", blob)` — strip every non-digit from the
                # WHOLE tool blob into one string, then substring-test the claim's digits against
                # it. Any number in the turn's output could vouch for any figure whose digits
                # happened to fall inside it. Verified here before applying:
                #
                #     $450       vs  echo processed 1450 items      -> was SOURCED
                #     $45        vs  echo took 3452 ms              -> was SOURCED
                #     $120       vs  ls -la /var/log/1120.txt       -> was SOURCED
                #     $1,240.00  vs  touch -t 1755012400 /tmp/x     -> was SOURCED
                #
                # An item count, a duration, a filename and an epoch stamp. None is a balance.
                #
                # THE DIRECTION IS WHAT MAKES IT WORTH FIXING. :613-615 records that unsourced is
                # the safe failure — over-reporting costs a log line, under-reporting hides the
                # thing being measured. This fallback failed the UNSAFE way: a false SOURCED
                # silently removes a row from the numerator of the very rule it exists to enforce,
                # which on the finance seat is Nick's "an account figure is pulled live, never
                # recalled".
                #
                # Equality against each parsed token instead. A number must be PRESENT in the
                # output, not merely embedded in some longer digit run.
                #
                # NOT DELETED, though the four legitimate cases tested all survive without it —
                # they are caught by the rounding loop above. Four hand-picked cases cannot show a
                # branch is dead, only that it was unnecessary for those four.
                #
                # This paragraph originally justified keeping it with a European-grouping example
                # ("1.240,00" failing the primary and rescued here). **That example is
                # unreachable** — core-finance checked what the DETECTOR can emit, and `_FINANCIAL`
                # produces nothing for the bare form and only "$1" for "$1.240,00", so `matched`
                # can never be that string. A justification resting on an input the detector cannot
                # generate is worth less than no justification, because it reads as evidence.
                #
                # The reachable case is CENTS-DENOMINATED tool output, ordinary for financial APIs:
                # a reply stating $1,240.00 against a tool reporting `balance_cents 124000`. The
                # primary cannot match it (124000 does not round to 1240.00); digits-equal-token
                # does. Producibility verified against `_FINANCIAL`, not assumed. Three such cases
                # measured, asserted in bin/tests/test_scope_claim.py.
                #
                # AND THERE IS NO TRADEOFF HERE, WHICH IS NOT WHAT EITHER OF US EXPECTED. I wrote
                # that this branch "carries the irreducible false-positive rate", reasoning that
                # "$2.00" vouched by a token "200" is a coincidence this rule admits. finance then
                # measured it — same blobs, same 600 claims, only this branch varying:
                #
                #     tool calls    fallback DELETED    digits-token
                #           24            2.3%             2.3%
                #          100            6.3%             6.3%
                #          400           20.2%            20.3%
                #         1500           44.3%            44.3%
                #
                # IDENTICAL. The false sourcing is the PRIMARY ROUNDING LOOP's — `round(float("234"),
                # 0) == 234` sources a $234 balance off `"offset": 234` with this branch deleted
                # entirely. Marginal cost here is ~0.1pp.
                #
                # So the branch has a real benefit and almost no cost, and both of us had argued it
                # as a balance. **Anyone trying to reduce false sourcing should target the primary
                # loop, not this.** That is harder than it sounds: rounding tolerance is what
                # rescues the 14-of-19 $#.## shape, so tightening it trades one failure for the
                # other. Tightened, not removed — and not removable on a cost that is not its own.
                #
                # finance's first hypothesis was the `[:4]` TRUNCATION, with an exposure bound of
                # 1 of 53 rows. They then measured it and withdrew: `$450` vs `1450 items` is still
                # SOURCED with `d[:4] -> d`, because a SHORTER claim is easier to hit by
                # coincidence, not harder. Revised exposure on their corpus: up to 17 of 18 sourced
                # rows. The truncation was never the defect; the substring test underneath it was.
                #
                # WHAT THIS FIX DOES NOT ACHIEVE — read before trusting a sourced=True here.
                #
                # It is a large win and it is not a cure. False-sourcing rate for 3-digit balances,
                # measured by finance across synthetic turns (none of the figures present in the
                # turn at all):
                #
                #     tool calls    shipped    after this fix
                #          5          6.0%        0.6%
                #        100         65.4%        8.6%
                #       1500        100.0%       66.0%
                #
                # The residual is IRREDUCIBLE, and not by this rule's fault: at high tool-call
                # counts a 3-digit number legitimately appears as a token — `"offset": 234`,
                # `"limit": 150` — and token equality cannot distinguish that from a $234 balance.
                # Note it also survives the ROUNDING LOOP above, which matches 234.0 just as
                # readily, so tightening this branch alone could never remove it.
                #
                # The adaptive tail-window at :634-645 makes it worse by construction: a larger
                # blob is a larger digit pool. Both behaviours are individually correct and they
                # compound.
                #
                # So the honest reading of this signal: **sourced=True on a SHORT financial_figure
                # is not evidence.** The class is reliable for figures with enough digits to be
                # improbable and unreliable below that — a property of the question, not of the
                # implementation. Anything that reports a financial-sourcing RATE should say which
                # side of that it is measuring.
                d = re.sub(r"[^\d]", "", matched or "")
                s = bool(d and any(d == re.sub(r"[^\d]", "", n)
                                   for n in re.findall(r"\d+(?:\.\d+)?", blob)))
    else:
        s = bool(srx and blob and srx.search(blob))
    if not s:
        sup = _SOURCE_SUPPLY.get(name)
        if sup is not None and injected and sup.search(injected):
            g = _SUPPLY_CLAIM_GUARD.get(name)
            if g is None or g.search(matched or ""):
                s = True
    if not s and injected:
        # Additional (supply, guard) pairs for classes answered by more than one injected fact.
        for sup, guard in _SOURCE_SUPPLY_EXTRA.get(name, ()):
            if sup.search(injected) and guard.search(matched or ""):
                s = True
                break
    return s



def _has_real_user_record(raw: str) -> bool:
    """Is a genuine user turn inside this window — not just tool results?

    A tool result is delivered as type="user", so the presence of "user" records proves nothing.
    Only a non-tool-result user record carries the hook supply the observer needs.
    """
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content")
        only_tool_result = (isinstance(content, list) and content and all(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content))
        if not only_tool_result:
            return True
    return False


def _turn_tool_blob(payload: dict) -> tuple[str, str]:
    """(tool_blob, injected_context) for the CURRENT turn, bounded.

    Returns TWO things because there are two kinds of evidence. The tool blob is what the turn DID.
    The injected context is what the turn was GIVEN — hook supply arrives inside the user record, and
    an observer that only reads tool calls scores a perfectly-sourced claim as a violation. That
    happened on day one.

    ("", "") on any problem -> unsourced, which is the safe direction for an observer: it over-reports
    rather than under-reports, and an over-report costs a log line while an under-report hides the
    thing we are measuring."""
    try:
        tp = payload.get("transcript_path") or payload.get("transcriptPath")
        if not isinstance(tp, str) or not tp:
            return "", ""   # 2-tuple contract; a bare "" unpacked to ValueError
        p = Path(tp)
        if not p.is_file():
            return "", ""   # 2-tuple contract; a bare "" unpacked to ValueError
        size = p.stat().st_size
        # ADAPTIVE, NOT A FIXED TAIL. This read the last 512KB unconditionally. A long turn — many
        # tool results, large outputs — pushes the turn's OWN user record past that boundary, so the
        # injected clock is invisible and every "tonight" in the reply scores UNSOURCED. The
        # casebook already carried the caveat ("reads high partly as ARTIFACT... long turns where
        # the clock appears once at the top score unsourced") and carrying a caveat is not fixing it:
        # T6 is 175 of 217 observations, the largest class in the suite, and the number is quoted.
        #
        # Now: read a tail, and if no non-tool-result user record is in it, DOUBLE and retry up to a
        # hard cap. Most turns resolve on the first read, so the common path costs what it did
        # before; only the long turns — the ones that were being mismeasured — pay more.
        raw = ""
        window = 512 * 1024
        while True:
            with p.open("rb") as fh:
                if size > window:
                    fh.seek(-window, os.SEEK_END)
                raw = fh.read().decode("utf-8", errors="replace")
            if window >= size or window >= 8 * 1024 * 1024:
                break
            if _has_real_user_record(raw):
                break
            window *= 2
    except Exception:
        return "", ""
    out, inj, saw_user = [], [], False
    for line in reversed(raw.splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "user":
            content = (rec.get("message") or {}).get("content")
            only_tool_result = (isinstance(content, list) and content and all(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content))
            if not only_tool_result:
                # The real user turn. Hook supply (the injected clock, START/WALL, any future digest)
                # rides along in this record, so capture it BEFORE stopping the scan.
                if isinstance(content, str):
                    inj.append(content[:4000])
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and isinstance(c.get("text"), str):
                            inj.append(c["text"][:4000])
                saw_user = True
                break
        if rec.get("type") == "assistant":
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        # THE TOOL NAME WAS BEING DISCARDED. This kept part["input"] and dropped
                        # part["name"], so nothing downstream could tell a Read from a Write —
                        # "file_path" appears in both. That is why say_do_gap ("a promise to write
                        # is sourced by an actual mutating call") was satisfiable by a READ, and why
                        # its 0.73/100 was reported as the one trustworthy transcript number when it
                        # was not. Found by the 2026-08-08 adversarial sweep, which noted correctly
                        # that no regex fix was even possible while the name was thrown away here.
                        out.append(json.dumps({"name": part.get("name") or "",
                                               "input": part.get("input") or {}})[:2200])
    return " ".join(out), " ".join(inj)


def _accum_path(payload: dict) -> Path:
    sid = re.sub(r"[^A-Za-z0-9]", "", str(payload.get("session_id") or "x"))[:16]
    tid = re.sub(r"[^A-Za-z0-9]", "", str(payload.get("turn_id") or "x"))[:16]
    return ACCUM_DIR / f"{sid}-{tid}.json"


def _safe_buffer_read(p: Path) -> tuple:
    """Read the turn buffer WITHOUT following a symlink. Returns (prev_text, seen_list).

    core-business (#924, ASK): the buffer FILE has never had a symlink guard. is_file() FOLLOWS
    symlinks, so with an externally-loosened ACCUM_DIR a planted symlink makes Core
    create-or-truncate an attacker-chosen path and write reply text into it. Reproduced against the
    real module.

    Its framing is the part worth keeping, because it corrects a comfortable reading: the OLD
    bail-on-any-bit code avoided this BY ACCIDENT, and only for directories that were loose AT CALL
    TIME. That check has no memory, so a transient loosen-then-revert would have exposed it
    identically. The class is closed here rather than left contingent on a precondition that
    happens not to occur across five Cores and a fork.

    O_NOFOLLOW is the guarantee, not the lstat — a check followed by an open is a TOCTOU window, so
    the kernel does the refusing.
    """
    try:
        fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return "", []          # missing, or a symlink the kernel refused. Both mean: start fresh.
    try:
        with os.fdopen(fd, "r", errors="ignore") as fh:
            d = json.loads(fh.read() or "{}")
        return d.get("text") or "", d.get("seen") or []
    except Exception:
        return "", []


def _safe_buffer_write(p: Path, payload_obj) -> None:
    """Write the turn buffer, refusing to follow a symlink at the target path."""
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload_obj))
    except Exception:
        pass


def _accumulate(payload: dict, delta: str) -> tuple[str, set]:
    """Append this chunk to the turn's buffer; return (full_text_so_far, already_logged_keys).

    Fails OPEN to the bare delta. If the buffer cannot be read or written the worst outcome is the old
    per-chunk behaviour for that turn — an undercount — which is strictly better than crashing on the
    hot path of every streamed chunk. This hook's contract is that it can never affect a turn.
    """
    p = _accum_path(payload)
    try:
        # Both levels 0700. Path.mkdir applies `mode` to the FINAL directory only — parents get the
        # umask default — so the parent is created explicitly. The child being 0700 is what actually
        # protects the files (whose own mode is the umask default, typically 0644), since another user
        # cannot traverse into a 0700 directory to reach them. The parent is tightened anyway so the
        # protection does not rest on one level alone.
        _TMP.mkdir(parents=True, exist_ok=True, mode=0o700)
        ACCUM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        # sentinel-code's residual, closed: mkdir(mode=, exist_ok=True) does NOT chmod a directory that
        # already exists, and neither call checked what it got. The path is fully deterministic — uid
        # plus a digest of a guessable root — so on a shared host a local attacker could pre-plant a
        # symlink or a mode-0777 directory there before first use, and mkdir would accept it.
        #
        # Not exploitable on Nick's Mac, where $TMPDIR is already an OS-scoped per-user directory. Added
        # anyway because the baseline is published and forkable: a fork on a shared Linux box inherits
        # this file and none of that protection. lstat, not stat, so a symlink is caught rather than
        # followed. Bailing to the bare delta is the correct failure — the caller degrades to per-chunk
        # detection, an undercount, which is always preferable to writing reply text somewhere unknown.
        # BOTH LEVELS, not just the leaf. sentinel-code's follow-up on the first version: checking
        # ACCUM_DIR alone leaves a real TOCTOU window. If _TMP itself were pre-planted as a symlink into
        # an attacker-writable tree, ACCUM_DIR would be created fresh INSIDE that tree by this process,
        # and would then pass every leaf check honestly — os.mkdir assigns ownership to the caller
        # regardless of where the path physically resolves, so it is genuinely ours and genuinely 0700.
        # The attacker, owning the parent, could then swap ACCUM_DIR for a symlink between the check and
        # the write. Verifying the parent closes the entry point rather than racing the window.
        # REPAIR WHAT WE OWN; REFUSE WHAT WE DO NOT. This block used to bail on ANY group/other bit,
        # and that is how the entire accumulation mechanism came to be silently disabled in
        # production for as long as it has existed.
        #
        # MEASURED, on this machine, 2026-08-10:  _TMP mode=755, umask=022.
        #
        # `mkdir(exist_ok=True, mode=0o700)` DOES NOT CHMOD AN EXISTING DIRECTORY — the comment three
        # lines above says exactly that, and the code then relied on it anyway for the parent. So
        # whichever process created $TMPDIR/core-reply-accum-<uid> first, under a 022 umask, left it
        # 0755 permanently, every later run failed this check, and _accumulate returned (delta,
        # set()) on EVERY CHUNK. Two consequences, neither of which announced itself:
        #
        #   · the per-turn dedupe never ran, so one claim logged once per chunk. That is the whole
        #     numerator inflation — duration_claim 188 rows against 114 distinct claims — and I
        #     first read it as a lock race, which it is not.
        #   · detection ran on the BARE DELTA rather than the accumulated text, so a violation split
        #     across a chunk boundary matched nothing. That is precisely the hole documented at the
        #     top of this file as fixed. IT HAS BEEN INERT SINCE THE FIX SHIPPED.
        #
        # A SECURITY CHECK THAT FAILS CLOSED INTO SILENCE IS STILL A FAILURE. The intent was to
        # refuse a pre-planted or attacker-writable directory; what it did was disable the feature on
        # an ordinary Mac with default settings, while every probe kept passing because a probe
        # supplies its violation as a single delta.
        #
        # So: a directory we OWN gets repaired to 0700, which is what the original mkdir intended.
        # A directory owned by someone else, or not a directory at all (symlink), still bails — that
        # is the case the check was actually for, and it is untouched.
        for d in (_TMP, ACCUM_DIR):
            st = os.lstat(d)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
                return delta, set()
            if st.st_mode & 0o077:
                os.chmod(d, 0o700)
                if os.lstat(d).st_mode & 0o077:
                    return delta, set()          # could not repair — refuse rather than proceed
        _prune()
        prev, seen = _safe_buffer_read(p)
        text = (prev + delta)[-ACCUM_MAX:]
        _safe_buffer_write(p, {"text": text, "seen": seen, "ts": int(time.time())})
        # The reply is complete; nothing more can arrive for this turn.
        if payload.get("final"):
            try:
                p.unlink()
            except Exception:
                pass
        return text, set(seen)
    except Exception:
        return delta, set()


class _turn_lock:
    """Serialise the per-turn read-modify-write. THE DEDUPE WAS CORRECT AND STILL LEAKED.

    MEASURED, not suspected. In 429 stored rows, 23 exact (turn, index, kind, matched) tuples were
    written more than once, and one turn logged the SAME WORD `today` at indices 0, 4 and 5 — which
    the `seen` set exists to make impossible. It also logged one claim as sourced=False at index 0
    and sourced=True at indices 4 and 5: the same claim, the same turn, both verdicts.

    The cause is structural, not a logic error. `_accumulate` READ `seen`, main() decided against it,
    and `_remember` WROTE it back — three steps with no lock, while MessageDisplay fires a fresh
    process per streamed chunk. Chunks arrive in bursts, so two processes routinely read the same
    `seen`, both find the claim new, and both log it. That also explains why early chunks sourced
    LOWER than later ones — the first chunks race hardest, and a loser's write clobbers the winner's.

    ONE CORRECTION TO THAT STORY, made 2026-08-10 by measuring rather than re-reasoning. The race is
    real and was fixed, but it is not the only thing producing that gap: detection runs on the
    ACCUMULATED text, so a claim at index 0 is judged against one chunk while a claim at index 5 is
    judged against six. Both mechanisms push the same direction, which is why the race alone looked
    sufficient. `tally_distinct` neutralises what remains by collapsing every chunk of one claim, and
    `bin/tests/test_metric_position_invariance.py` pins that as a property. Current figures:
    `python3 bin/observation-probe.py`.

    THE CONSEQUENCE IS NOT COSMETIC. duration_claim is the largest class on both Cores, and the
    inflation lands entirely in the NUMERATOR of the primary metric, because the denominator counts
    whole replies. Every class was overstated, duration_claim worst.

    The per-class factors that used to be recited here are gone deliberately. They were written as
    live measurements and aged within hours — a file restating a sibling's output is a cache with no
    invalidation (core-business, bus #980, which found the same shape in its tree under a heading
    reading "VERIFIED, by running"). Recompute instead: `python3 bin/observation-probe.py`.

    Fails OPEN. If the lock cannot be taken the worst case is the behaviour that shipped yesterday,
    and this hook's contract is that it can never affect a turn.
    """

    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def __enter__(self):
        try:
            import fcntl
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.fh = open(str(self.path) + ".lock", "a+")
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self.fh = None
        return self

    def __exit__(self, *exc):
        try:
            if self.fh:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()
        except Exception:
            pass
        return False


def _remember(payload: dict, keys: list) -> None:
    """Record what has been logged for this turn so later chunks do not re-log it."""
    p = _accum_path(payload)
    try:
        if not p.is_file():
            return          # already unlinked on the final chunk; nothing left to dedupe against
        prev, seen = _safe_buffer_read(p)
        _safe_buffer_write(p, {"text": prev, "seen": sorted(set(seen) | set(keys)),
                               "ts": int(time.time())})
    except Exception:
        pass


# ── evidence, so a corrected detector can be RE-SCORED against history ───────────────────────────
#
# core-business (#915, #916): it tried to verify yesterday's T6/T8 fixes by replaying business's
# observations through the corrected observer AND COULD NOT. The log stores the VERDICT and nothing
# that produced it, so every detector fix is a permanent, unrepairable discontinuity in the series.
# Six fixes yesterday means six of them. That bounds what the casebook can ever say about trends.
#
# WHY A SEPARATE FILE, OUTSIDE THE REPO — which is where I depart from the spec as sent. The spec
# puts claim/blob/supply in reply-observations.jsonl. That file is per_core_keep so it never reaches
# the shared baseline, but it IS tracked in this Core's own git repo, so reply text and raw tool
# arguments would enter git history permanently and irreversibly. On core-finance that is brokerage
# material; on core-business, employer material.
#
# MY FIRST FIX WAS A .gitignore LINE AND IT WAS WRONG, in a way this file already documents fifty
# lines above about the accumulation buffer: **.gitignore is not in the sync manifest.** The hook
# travels to four peer Cores; the ignore rule does not. So the sidecar would have been created inside
# each peer's tree and committed there, and life — the only Core where I tested it — is the one seat
# that could never see it. That is the author-blindness class, committed while fixing a finding about
# it. bin/tests/test_reply_observer_streaming.py caught it on the next run, which is the whole reason
# that assertion is written as "the hook writes ONLY the observation log" rather than as a path check.
#
# A FIX HAS TO TRAVEL WITH THE CODE. So evidence lives outside every repo, in a per-user directory
# keyed by a digest of the root, 0700, and no Core's .gitignore has to know anything. It is not the
# turn buffer's tmp location either: that is working memory with a one-hour TTL, and evidence has to
# outlive the reply to be worth keeping. The join key is the (session, turn, kind) already stored in
# the verdict log.
EVIDENCE_DIR = (Path.home() / ".claude" / "core-evidence"
                / hashlib.sha256(str(ROOT).encode()).hexdigest()[:16])
EVIDENCE = EVIDENCE_DIR / "reply-evidence.jsonl"
EVIDENCE_MAX_BYTES = 8 * 1024 * 1024
# core-business RAN this against eight real credential shapes and it caught ONE — incidentally, via
# the >=32-char catch-all (#924 BLOCK 1). The keyword branch never fired on `aws_secret_access_key=`
# because `\bsecret\b` cannot match inside an underscored word: underscores are \w, so there is no
# word boundary there. The docstring said "strip credential-shaped runs before anything is written
# down" and the code did not do that — on NEW code that writes reply text to disk for the first time,
# on a shared hook, on a fleet including finance (brokerage) and ops (third-party material).
#
# AND MY TEST COULD NOT HAVE CAUGHT IT: the fixture was `api_key: sk-abc…`, which is itself >=32
# chars, so it passed with the keyword branch DELETED. A checker that cannot fail on the classes that
# actually leak — the third instance today of a fixture satisfying an assertion regardless of the
# code under test.
_SECRETISH = re.compile(
    # keyword = value, with the key matched by SEPARATORS rather than word boundaries so
    # aws_secret_access_key, X-Api-Key and client.secret all hit
    r"(?i)(?:^|[^A-Za-z0-9])(?:[A-Za-z0-9_.\-]*(?:api[_.\-]?key|secret|token|passwo?rd|passwd|"
    r"credential|bearer|authorization|auth)[A-Za-z0-9_.\-]*)\s*[:=]\s*\S+"
    # provider-shaped key ids and prefixed keys
    r"|\bAKIA[0-9A-Z]{16}\b|\bASIA[0-9A-Z]{16}\b"
    r"|\b(?:sk|pk|rk|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}\b"
    # credentials embedded in a URL: scheme://user:pass@host
    r"|\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@"
    # PEM / OpenSSH private key headers
    r"|-----BEGIN[A-Z ]*PRIVATE KEY-----"
    # JWTs, all three segments — the middle one is base64 CLAIMS in cleartext
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?"
    # personal identifiers: email, US SSN
    r"|\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
    r"|\b\d{3}-\d{2}-\d{4}\b"
    # US phone and payment-card numbers, both separator-tolerant
    r"|\b(?:\+?1[ .\-]?)?\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}\b"
    r"|\b(?:\d[ \-]?){13,19}\b"
    # LONG OPAQUE RUNS — the catch-all, kept last AND NARROWED. As `[A-Za-z0-9_-]{32,}` it ate
    # `supercalifragilisticexpialidociousandthensome`, and core-business's conformance harness
    # scores that as a failure on purpose: a row redacted to nothing can never be re-scored, which
    # is the permanent series break this sidecar exists to end. Over-redaction is not the safe
    # direction here, it is a different way to lose the evidence.
    #
    # A real key has MIXED CASE OR DIGITS OR both; an English word of any length has neither. The
    # lookahead requires at least two of the three character classes before the run counts as
    # opaque, which keeps every provider key above (they are all mixed) and spares prose.
    # SCOPED OFF THE IGNORECASE FLAG with (?-i:...), and the first version without it did nothing.
    # `(?i)` at the head applies to the WHOLE pattern, so `[a-z]` and `[A-Z]` become the same class
    # and a MIXED-CASE test cannot distinguish anything — it matched a 45-letter English word. A
    # case-based discriminator under IGNORECASE is the same defect as comparing a captured value by
    # containment: the test is written, runs, and cannot separate the two cases it names.
    r"|\b(?-i:(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*(?:[A-Z]|[0-9]))[A-Za-z0-9_-]{32,})\b"
    r"|\b(?-i:(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{32,})\b")

# ACCOUNT AND CURRENCY FIGURES ARE MASKED, NOT DROPPED. The verdict log already redacts digits for
# financial_figure; the evidence sidecar bypassed that entirely and stored the raw claim (#924
# BLOCK 5). Masking rather than removing keeps the SHAPE, which is what a re-score needs.
_MONEYISH = re.compile(
    r"(?i)(?:\$\s?\d[\d,]*(?:\.\d+)?"
    # THE TOKEN MUST CONTAIN A DIGIT. Without the lookahead, `(?i)[A-Z0-9-]{5,}` matched the word
    # "number" in "account number 5RV12345" — the lazy quantifier takes the EARLIEST match, so the
    # mask landed on a word with no digits in it and the account number itself survived untouched.
    # The redaction reported success and changed nothing, which is this session's dominant defect
    # shape appearing inside the fix for a different instance of it.
    r"|\b(?:account|acct|routing|iban|card)\b[^\n]{0,24}?\b(?=[A-Z0-9-]*\d)[A-Z0-9-]{5,}\b)")


def _redact(s: str, cap: int) -> str:
    """Bound the slice AND strip credential-shaped runs before anything is written down.

    Order matters: credentials first (they may contain digits a money mask would otherwise eat),
    then account/currency figures to `#`, matching what the verdict log already does for
    financial_figure — a rule the sidecar bypassed entirely on its first version.
    """
    if not s:
        return ""
    s = _SECRETISH.sub("<redacted>", s)
    s = _MONEYISH.sub(lambda m: re.sub(r"\d", "#", m.group(0)), s)
    return s[:cap]


def _write_evidence(payload: dict, rows: list, blob: str, injected: str, text: str) -> None:
    """Best-effort. An evidence write must never be able to cost a turn or block the verdict log."""
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Same repair-what-we-own rule as the turn buffer, and for the same reason: mkdir does not
        # chmod an existing directory, so one creation under a loose umask would otherwise leave
        # this readable forever.
        st = os.lstat(EVIDENCE_DIR)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return
        if st.st_mode & 0o077:
            os.chmod(EVIDENCE_DIR, 0o700)
            if os.lstat(EVIDENCE_DIR).st_mode & 0o077:
                return
        if EVIDENCE.exists() and EVIDENCE.stat().st_size > EVIDENCE_MAX_BYTES:
            keep = EVIDENCE.read_text(errors="ignore").splitlines(keepends=True)[-4000:]
            EVIDENCE.write_text("".join(keep))
        # THE FILE MODE, not just the directory's. EVIDENCE is created by open("a") under the
        # process umask, so it lands 0644 while the directory is 0700 — defence-in-depth rather than
        # live exposure, since nobody can traverse a 0700 directory to reach it, but the file holds
        # reply text and should not rely on one level alone. core-business downgraded this from its
        # reviewer's BLOCK to an ASK after reading the directory code, and it was right to.
        _new = not EVIDENCE.exists()
        with EVIDENCE.open("a") as fh:
            if _new:
                try:
                    os.chmod(EVIDENCE, 0o600)
                except Exception:
                    pass
            for r in rows:
                fh.write(json.dumps({
                    "ts": r["ts"], "session": r["session"], "turn": r["turn"], "kind": r["kind"],
                    "observer_sha": OBSERVER_SHA,
                    "claim": _redact(text[-600:], 400),
                    "blob": _redact(blob, 900),
                    "supply": _redact(injected, 400),
                }) + "\n")
    except Exception:
        pass


def _prune() -> None:
    """Drop buffers from turns that never sent a final chunk (interrupted reply, killed session)."""
    try:
        now = time.time()
        for f in ACCUM_DIR.glob("*.json"):
            try:
                if now - f.stat().st_mtime > ACCUM_TTL:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    delta = payload.get("delta")
    if not isinstance(delta, str) or not delta.strip():
        return 0

    # ONE CRITICAL SECTION: read the turn buffer, decide what is new, and record the decision.
    # These three steps were unsynchronised while a FRESH PROCESS runs per streamed chunk, so two
    # chunks routinely read the same `seen`, both judged a claim new, and both logged it. See
    # _turn_lock for the measurement. Released before anything outside the turn buffer is touched.
    with _turn_lock(_accum_path(payload)):
        text, already = _accumulate(payload, delta)
        hits = [(name, m.group(0)[:60])
                for name, rx in DETECTORS
                for m in [rx.search(text)] if m]
        # Anything already recorded for this turn is not a new observation — the buffer keeps the
        # matched phrase for the rest of the reply, so without this every subsequent chunk would log
        # it again and inflate the numerator against a denominator that counts whole turns.
        hits = [(n, m) for n, m in hits if f"{n}\u0000{m}" not in already]
        if hits:
            _remember(payload, [f"{n}\u0000{m}" for n, m in hits])
    if not hits:
        return 0

    blob, injected = _turn_tool_blob(payload)
    rows = []
    for name, matched in hits:
        sourced = sourced_for(name, matched, blob, injected)
        if name in _REDACT_DIGITS:
            matched = re.sub(r"\d", "#", matched)
        rows.append({"ts": int(time.time()), "kind": name, "matched": matched,
                     "sourced": sourced, "index": payload.get("index"),
                     "final": payload.get("final"),
                     "session": str(payload.get("session_id") or "")[:12],
                     "turn": str(payload.get("turn_id") or "")[:12],
                     "observer_sha": OBSERVER_SHA})
    _write_evidence(payload, rows, blob, injected, text)

    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > MAX_BYTES:
            keep = LOG.read_text(errors="ignore").splitlines(keepends=True)[-4000:]
            LOG.write_text("".join(keep))
        with LOG.open("a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
