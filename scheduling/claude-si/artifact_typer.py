#!/usr/bin/env python3
"""artifact_typer.py — the SELF-BUILDING type router (WS4). Given a recurring-ask / friction case,
DECIDE which artifact to build. Deterministic decision table (no LLM) — ambiguous always falls to a
non-blocking inject_contract, never a blocker (governing plan 2026-07-20).

Types:
  already_covered     — an existing hook/contract already handles this concept → build nothing
  enforcement_block   — the ask maps to a VALIDATED STATIC ORACLE (checkable satisfying-condition in the
                        DSL); becomes a block artifact installed SHADOW (enforced=false) until proven
  hooked_skill        — a repeatable thing to DO. Installs a gated trigger plus a markdown payload
                        under .claude/state/friction-artifacts/procedures/. Deliberately NOT written
                        straight into .claude/skills/: a skill activates on DESCRIPTION match, which
                        this gate cannot test in advance. It may GRADUATE into a real skill later,
                        once it has earned that surface by firing (skill_graduate.py, 2026-07-27).
                        When the ask is about a moment of WORK it triggers on PreToolUse over a
                        closed tool vocabulary instead of on the prompt.
  claude_md_directive — a diffuse standing preference with no trigger and no oracle — a PROPOSAL
  scheduled_job_proposal — the ask has its own CLOCK, so no prompt or tool trigger can serve it.
                        PROPOSAL ONLY, and that ceiling is policy rather than immaturity: a
                        scheduled job runs unattended and can spend money or take an outward
                        action, both of which are Nick's hard rules. Typed before `procedure`
                        because a cadence ask is often also procedural, and the schedule is the
                        part a hooked_skill cannot express — a skill still waits to be triggered.
  work_hook           — a recurring correction whose EVIDENCE is dominated by Nick stopping the work
                        (>= FRUSTRATION_HOOK_SHARE of its observations are correction-frustration /
                        -stop-execution / -explicit-no) and which is still recurring. Installs a
                        PreToolUse INJECT artifact over the same closed tool vocabulary
                        `hooked_skill`'s work-shape branch uses, so it speaks at the moment of the
                        mutation rather than as prose read beforehand — the reminder already did not
                        hold, which is what the frustration evidence means. Deliberately NOT a new
                        .py registered in settings.json: that would be a second mechanism beside a
                        working dispatcher, and it would bypass the test gate, rollback and the kill
                        switch. ESCALATION-ONLY — decided after every stronger terminal, so it can
                        only ever replace the default reminder, never take an ask from a better home.
  inject_contract     — DEFAULT: everything context-triggerable → a reminder through the live gate
  slash_command       — a procedure ask that names DELIBERATE, on-demand invocation ("run this
                        myself", "as a command") rather than an ambient trigger. Installs a REAL
                        `.claude/commands/<slug>.md` file — the same rails as hooked_skill (test
                        gate, payload-hash integrity, quarantine, rollback), except the payload
                        goes to the real command surface instead of the hidden procedures/ store.
                        Safe to do directly: a hooked_skill hides because a SKILL activates on
                        description match, which this gate cannot test in advance (see above); a
                        slash command activates ONLY when Nick types `/<slug>` himself, which IS
                        the invocation this terminal exists for, so there is no hidden-activation
                        risk to avoid. Checked BEFORE the plain hooked_skill branch so it can only
                        WIN an ask away from the weaker default, never from an already-grounded
                        hooked_skill trigger.
  workflow            — a procedure ask that names MULTIPLE COORDINATING AGENTS, not a single
                        trigger-and-remind action. The Workflow tool orchestrates multi-agent
                        scripts; auto-generating and auto-running one is a bigger claim than this
                        loop can honestly make (its own DSL is INJECT-ONLY — there is no "run a
                        tool" effect mode, and test_static_no_codegen locks every module here
                        against eval/exec/child-process calls on artifact data). So this terminal produces a
                        PROPOSAL ONLY: a markdown brief at
                        .claude/state/friction-artifacts/workflow-proposals/<id>.workflow.md,
                        pointed to by the same inject-mode gated trigger every other terminal uses.
                        It can never auto-run — not by policy, by the shape of the schema. This is
                        the terminal a multi-agent ask still gets when it does NOT match a
                        workflow_run catalog entry (below) — the fallback, not a lesser cousin.
  workflow_run        — a `workflow`-shaped ask (multiple coordinating agents) that ALSO matches a
                        pre-authored, hash-pinned entry in workflow_catalog.py's manifest (Gap B,
                        2026-08-31 — judge-selected Candidate 1). Installs the SAME real
                        `.claude/commands/<slug>.md` file slash_command uses — Nick still has to
                        type `/slug` himself, so nothing here fires unattended, but nothing is
                        DRAFTED at invocation time either: the command's own body names the exact
                        catalog script, its pinned hash, and closed catalog-default params. Falls
                        back to `workflow` (the proposal) when no catalog entry matches, or when
                        the catalog itself fails to load (tampered/missing script) — a routing
                        decision must never trust an unverifiable catalog as a green light.

The ORACLE_CATALOG is the safety boundary: enforcement is only EVER produced for a concept with an
entry here, and each entry names a static oracle op the dispatcher can actually evaluate. Adding an
oracle requires code + a locked equivalence test — never a data-only change.
"""
from __future__ import annotations

import re
from pathlib import Path as _Path

# --- coverage: concepts already enforced by a hook or a floor rule (don't duplicate) ---
#
# A `hook:` value is a CLAIM ABOUT THE RUNNING SYSTEM and is resolved against live registration
# by `live_hook_names()` below — it is not taken on faith. It used to be. Four of this map's five
# hook: targets were retired on 2026-08-06 with the nine Stop gates, and the map went on reporting
# them as coverage for twelve days on every seat. business measured the bill on its own corpus:
# its single most-recurrent correction, "recall from the brain before answering about past work"
# at 19x, and "verify state against the live source before claiming" at 8x, both routed
# `already_covered` and produced nothing — deferred to enforcement that is registered nowhere.
# Both are literally two of the three anti-patterns CLAUDE.md says nothing enforces.
#
# The names below are the LIVE successors, verified registered in .claude/settings.json on all
# five seats, not the retired predecessors:
#     state-claim-gate (Stop, retired) -> verification-trigger  (UserPromptSubmit, fires BEFORE the
#                                         reply; its docstring: "implements CLAUDE.md anti-pattern
#                                         rule #2 structurally", and CLAUDE.base.md names it the
#                                         survivor of the 08-06 retirement)
#     recall-gate      (Stop, retired) -> recall-first-gate     (PreToolUse; its own docstring says
#                                         it MOVES recall-first enforcement off the Stop gate —
#                                         "force the right first move" instead of catching the bad
#                                         answer at Stop. Nick-approved, no-shadow, live 06-09)
# The remaining two have NO live enforcer and are demoted to `rule:` — prose-grade coverage, which
# this file already treats as conditional on the ask going quiet:
#     approval-gate    retired 07-27 for over-blocking (24 blocks in 58 invocations), not superseded
#     say-do-gap       retired 08-06; reply-observer RECORDS the gap after the fact, never prevents
#                      it, so it is not enforcement. (That entry was mis-keyed besides: ghostwriting
#                      is not a say/do gap. It is a CLAUDE.md rule, and it is now labelled as one.)
COVERED_CONCEPTS = {
    "verify state against the live source before claiming": "hook:verification-trigger",
    "recall from the brain before answering about past work": "hook:recall-first-gate",
    "recall from the actual brain": "hook:recall-first-gate",
    "present a plan and get explicit approval before executing": "rule:CLAUDE.base.md plan-first",
    "warn nick before operations that consume significant usage": "rule:subagents.md",
    "warn before heavy subagent fan-out spend": "rule:subagents.md",
    "grep decisions-log": "rule:memory.md",
    "use codex": "rule:codex-routing.md",
    "pin sonnet for mechanical subagent": "rule:subagents.md",
    "stay scoped to the current core": "hook:stay-scoped",
    "surface the full backlog": "rule:claude-si show-all",
    "don't ghostwrite": "rule:CLAUDE.md never-ghostwrite-as-nick",
}


def dispatchable_events(_cache: dict = {}):
    """Events on which friction-dispatch is ACTUALLY registered in THIS Core's settings.json.

    Sibling of live_hook_names, same source and same reasoning: settings.json is what the runtime
    executes, so a claim about which events dispatch is read from it rather than asserted here. A
    hardcoded event list would be a THIRD place recording that — after settings.json and
    bin/hook-registry.json — and would rot the same way the oracle templates did.

    RETURNS None WHEN UNKNOWN — never an empty set. That distinction is the whole point and is
    the opposite choice from
    friction_installer._event_is_dispatchable, which fails CLOSED on the same question. The
    asymmetry is the point: that one decides whether to INSTALL something that can stop work, where
    an unreadable config must never look like a live dispatcher. This one only decides whether to
    DOWNGRADE a block to a reminder. Failing closed here would silently reroute every enforcement
    ask into a reminder the moment settings.json became unreadable — turning a config error into a
    quiet, permanent capability loss. The install gate still refuses on its own read, so a failure
    here cannot produce an installed block; it can only produce a proposal that gate then rejects.
    An empty set would be indistinguishable from "no event dispatches" and would silently
    downgrade EVERY enforcement ask to a reminder the moment settings.json became unreadable —
    a config error turning into a permanent, quiet capability loss. None means the caller keeps
    the block and lets the install gate, which fails closed on its own read, make the call.
    """
    if "v" in _cache:
        return _cache["v"]
    import json as _json
    events: set = set()
    try:
        root = _Path(__file__).resolve().parents[2]
        seen_any = False
        for fn in ("settings.json", "settings.local.json"):
            f = root / ".claude" / fn
            if not f.is_file():
                continue
            seen_any = True
            for ev, groups in (_json.loads(f.read_text()).get("hooks") or {}).items():
                for g in groups or []:
                    for hk in g.get("hooks") or []:
                        if "friction-dispatch" in (hk.get("command", "") or ""):
                            events.add(ev)
        if not seen_any:
            return None
    except Exception:
        return None          # unreadable → UNKNOWN, not "nothing dispatches". Not cached; retried.
    _cache["v"] = frozenset(events)
    return _cache["v"]


def live_hook_names(_cache: dict = {}) -> frozenset:
    """Hook script basenames actually registered in THIS Core's settings.json.

    This exists so a `hook:` coverage claim cannot outlive the hook. The map above was a hardcoded
    assertion about the running system, and hardcoded assertions about the running system rot in
    exactly one direction: silently, toward suppression. Retire a hook now and every concept that
    deferred to it falls through on the very next loop run, with no edit to this file.

    Reads registered COMMANDS rather than the hooks directory, because the retired gates are all
    still ON DISK — a file-presence check calls every one of them live. bin/hook-registry.json is
    not authoritative either: it is a catalog, and its entries carry `retired: true` precisely
    because the registry outlives the registration. settings.json is what the runtime executes.

    FAILS TOWARD BUILDING. Any error returns the empty set, so NO hook: claim is honoured and every
    such ask routes onward. That direction is deliberate and it is the whole lesson of this defect:
    an unwarranted inject_contract is one more line of prose in a prompt, while an unwarranted
    suppression is a repeated correction disappearing in silence for twelve days with no log line.
    """
    if "v" in _cache:
        return _cache["v"]
    import json as _json
    names: set[str] = set()
    try:
        root = _Path(__file__).resolve().parents[2]
        for fn in ("settings.json", "settings.local.json"):
            f = root / ".claude" / fn
            if not f.is_file():
                continue
            for _ev, groups in (_json.loads(f.read_text()).get("hooks") or {}).items():
                for g in groups or []:
                    for hk in g.get("hooks") or []:
                        names.update(re.findall(r"([A-Za-z0-9_.-]+)\.(?:py|sh)\b",
                                                hk.get("command", "") or ""))
    except Exception:
        names = set()
    _cache["v"] = frozenset(names)
    return _cache["v"]

# --- ENFORCEMENT ORACLE CATALOG (the safety boundary) ---
# Each entry: signals that identify the ask, the event it checks at, and the static oracle op + polarity.
# `oracle_ready` gates SHADOW generation itself: False = the oracle is a stub, so NO block is generated
# even in shadow (Codex: a wrong/unavailable oracle is the single riskiest failure).
ORACLE_CATALOG = {
    "deliverable_as_artifact": {
        "signals": ["artifact not terminal", "clickable artifact", "surface content nick will view",
                    "deliver output as a clickable artifact", "render diagram as image", "not buried in chat"],
        "event": "Stop",
        # block iff the turn CLAIMED a deliverable but NO artifact was delivered (none-of artifact_delivery_present)
        "condition_kind": "artifact_delivery",
        "equivalence_oracle": "hook:deliverable-format-gate",
        "oracle_ready": True,   # oracle_adapter provides a real artifact_delivery signal + locked equiv test
        # NEVER AUTO-PROMOTE — and unlike the entry below, the reason is a CATEGORY ERROR, not a
        # pending redesign (2026-08-12, found by core-finance verifying the author->enforce path on
        # all five disks).
        #
        # This entry declares event="Stop". friction-dispatch.py is registered ONLY on
        # UserPromptSubmit and PreToolUse (.claude/settings.json) — never Stop. So it has never been
        # invoked on any seat, and finance was right that the path is dead fleet-wide.
        #
        # But registering the dispatcher on Stop is the ONE fix that is off the table. The
        # operator's policy, 2026-08-06, decisions-log:3752: nothing may act after the reply is
        # sent — it all has to happen before the reply is given; a Stop hook after the reply is
        # useless — nine Stop gates were retired fleet-wide on that directive. Re-registering one
        # to revive this oracle would undo it.
        #
        # And the question this oracle asks — "did the turn deliver an artifact?" — CANNOT be
        # answered before the turn produces one. MessageDisplay is the only event that sees the
        # reply text, and reply-observer.py:17 states its own limit: "MessageDisplay sees everything
        # and can stop nothing." There is no pre-reply vantage point from which this is decidable.
        #
        # So it is structurally unenforceable in the same sense as frustration-deescalate: not
        # unwired, but unanswerable at any point where an answer could still change the outcome. It
        # stays useful as SHADOW telemetry — "you claimed a deliverable and shipped none" is worth
        # recording — and it must never flip to an enforced block that fails a turn after the fact.
        "never_promote": True,
    },
    "adversarial_review_before_blast_radius": {
        # The triad directive — Nick's most-repeated ask (17 recorded moments, 5 of them AFTER the
        # rule documenting it shipped). Documented-but-still-recurring is the definition of an
        # unenforced directive, which is exactly what an oracle is for.
        "signals": ["use codex alongside core for substantial system/code work",
                    "orchestrate codex and fable alongside core by default for substantial work",
                    "use fable for adversarial review of plans and designs before shipping",
                    "use codex", "adversarial review before shipping"],
        "event": "Stop",
        # block iff the turn took a blast-radius action AND no adversarial review ran in it
        "condition_kind": "adversarial_review",
        "equivalence_oracle": "oracle_adapter.review_signals",
        "oracle_ready": True,   # both halves are mechanical: a command shape and a subagent call
        # NEVER AUTO-PROMOTE (Codex review, 2026-07-27). This oracle observes at Stop, by which point
        # the push or migration has ALREADY RUN. As a shadow signal that is fine and useful — it
        # records "you shipped without review". As an ENFORCED block it would be actively harmful:
        # it cannot prevent the action, only fail the turn afterwards, inviting a retry of a
        # possibly non-idempotent operation.
        #
        # Prevention belongs at PreToolUse, on the command itself, before it executes. Until that
        # redesign exists this entry stays shadow-only, enforced by friction_promote rather than by
        # my intention: `never_promote` is checked there, so no proof window can flip it.
        "never_promote": True,
    },
}

# --- PROCEDURE detection is NOT a needle list ---
# It used to be `SKILL_SIGNALS`, 7 hardcoded substrings. Measured 2026-07-27: matched 0 of 27 real
# recurring asks at support>=2, while the corpus was full of procedure-shaped demand. Needle-matching
# over LLM-CANONICALIZED text is a category error — canonicalization already removed the surface
# features the needles keyed on. The shape now comes from the extraction step that produces
# canonical_ask (ask_miner.ASK_TYPES, closed vocabulary, majority-voted across the cluster, DB
# CHECK-constrained). This function stays deterministic: it READS a cached label, it never calls an LLM.


def _existing_capability_names() -> set[str]:
    """Every name already invocable in this Core, across ALL THREE namespaces.

    Dedupe used to consult only COVERED_CONCEPTS (hook:/rule: labels), so it could not see that a
    capability already existed. The live corpus had the failure preloaded: "capture a handoff doc
    before clearing context" recurs 3x and `handoff` already ships — as .claude/commands/handoff.md,
    a slash COMMAND, not a skill. Checking only .claude/skills/ would have missed it and the engine's
    first generated procedure would have duplicated a hand-authored one (Fable review 2026-07-27).
    Fail-open: an unreadable directory yields fewer names, never a crash.
    """
    import os
    from pathlib import Path
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
    names: set[str] = set()
    try:  # skills: .claude/skills/<name>/SKILL.md
        for d in (root / ".claude" / "skills").iterdir():
            if (d / "SKILL.md").is_file():
                names.add(d.name.lower())
    except Exception:
        pass
    try:  # slash commands: .claude/commands/<name>.md
        for f in (root / ".claude" / "commands").glob("*.md"):
            names.add(f.stem.lower())
    except Exception:
        pass
    try:  # user-scope + plugin skills (codex:*, vercel:*, …) share the invocation namespace
        for base in (Path.home() / ".claude" / "skills", Path.home() / ".claude" / "plugins"):
            for p in base.rglob("SKILL.md"):
                names.add(p.parent.name.lower())
    except Exception:
        pass
    return names


# Capability names that are also ordinary English. A bare occurrence of one of these in an ask is
# NOT evidence the ask is about that capability. Found empirically: "keep the architecture diagram
# in sync with the actual system" matched the `sync` command and was wrongly ruled already-covered.
_AMBIGUOUS_NAMES = {"sync", "ship", "run", "init", "review", "health", "access", "loop", "schedule",
                    "simplify", "update-config", "auth", "deploy", "status", "env"}


def _duplicates_existing(ask: str, names: set[str]) -> str | None:
    """Return the name of an already-existing capability this ask is asking for, else None.

    Deliberately conservative in BOTH directions, because both errors are real: a false negative
    duplicates a hand-authored capability (the `handoff` case), while a false positive silently
    suppresses a legitimate procedure. So a match requires a DISTINCTIVE name — a multi-word /
    hyphenated name matched as a phrase, or a single token of 6+ chars that is not ordinary English.
    """
    low = (ask or "").lower()
    toks = set(re.findall(r"[a-z0-9-]{3,}", low))
    best = None
    for n in names:
        if n in _AMBIGUOUS_NAMES:
            continue
        if "-" in n or ":" in n:  # compound names are distinctive; match as a phrase
            phrase = n.replace("-", " ").replace(":", " ")
            if phrase in low or n in toks:
                if best is None or len(n) > len(best):
                    best = n
        elif len(n) >= 6 and n in toks:
            if best is None or len(n) > len(best):
                best = n
    return best

# A procedure about a moment of WORK rather than a moment of conversation. Kept narrow and
# hardcoded here on purpose — corpus data may decide an ask is procedural, but only code may
# decide an artifact keys on tool events.
_WORK_MOMENT = re.compile(
    r"\b(before|when|prior to)\b.{0,40}\b(edit|editing|writ|push|commit|migrat|deploy|ship|chang|touch|modif)\w*", re.I)

# --- slash_command / workflow: two more procedure SHAPES, both narrowing what hooked_skill already
# claims (2026-08-31) --------------------------------------------------------------------------
#
# Checked INSIDE the existing `ask_type == "procedure"` branch, before the plain hooked_skill
# fallback, so neither can ever take an ask away from an already-grounded hooked_skill trigger —
# only the shape of a procedure ask that would otherwise become the default changes.
#
# MEASURED (2026-08-31), not guessed: the three live support>=3 procedure asks on life —
#   "keep the architecture diagram/documentation in sync with the actual current system, not stale"
#   "autonomously detect recurring frustrations/workflows and encode them as hooks/contracts..."
#   "execute the plan fully end-to-end with no loose ends or open questions"
# — match NEITHER signal below. All three describe something that should happen AS PART OF
# ordinary work; none names deliberate on-demand invocation or multiple coordinating agents. Both
# terminals therefore report a genuine ZERO from today's ask corpus — the correct result for this
# corpus, not evidence the signal is broken. Kept as small hardcoded patterns rather than a data-
# driven inference, same discipline as ORACLE_CATALOG and _WORK_MOMENT: the corpus can decide an
# ask is procedural, but it cannot decide it names deliberate invocation or a multi-agent shape.
_DELIBERATE_INVOCATION = re.compile(
    r"\b(as a (slash )?command|give (me|us) a command|make (this|it) a (slash )?command"
    r"|a /\w|slash command|run (this|it) (myself|manually|on demand)"
    r"|invoke (this|it) (myself|manually|on demand)|on demand|manually (run|trigger|invoke))\b",
    re.I)

_MULTI_AGENT_SIGNALS = re.compile(
    r"\b(orchestrat\w*|multi-?agent|sub-?agents?|in parallel|fan out|spawn (multiple|several)"
    r"|coordinate (across|between)|hand off between|pipeline of agents)\b", re.I)

# --- CADENCE: the ask names WHEN, recurrently, independent of any prompt (Phase 3.5) ---
#
# The third trigger shape. prompt-shaped -> hooked_skill; work-moment -> PreToolUse; cadence ->
# a scheduled job. The distinction that matters: a cadence ask does not wait to be triggered by
# anything Nick types or any tool he reaches for. It has its own clock.
#
# MEASURED BEFORE BUILDING, and the measurement decided the design. A loose pattern
# ("every|always|automatically") matched 16 of 287 distilled asks — but reading them, almost all
# were "every CORE", "every element", "every time": `every` as a SCOPE quantifier, not a
# schedule. Requiring an actual time or lifecycle anchor leaves exactly ONE:
#
#     "always run the full close-reconciler at session close"
#
# One consumer in 287. The work-moment surface next to this was justified at 3 of 29 and its
# comment says the surface has "real consumers rather than being widened speculatively" — 1 of
# 287 does not clear that bar. So this DETECTS and PROPOSES; it does not generate a scheduled
# job, and no scheduler is built. If cadence asks accumulate, they become visible and countable
# and the surface can earn its implementation then.
#
# It would also be the wrong thing to auto-install even with consumers: a scheduled job runs
# unattended and can spend money or take outward action, both of which are Nick's hard rules.
# Proposal-only is the ceiling here by policy, not by immaturity.
_CADENCE = re.compile(
    r"\b(at (the )?(session )?(close|start|end)"
    r"|every (morning|day|night|week|session)"
    r"|daily|nightly|weekly|hourly"
    r"|each (morning|day|session)"
    r"|after (each|every|the) (work|session|close)"
    r"|on a schedule|periodically)\b", re.I)


# --- diffuse standing preferences with no trigger + no oracle → CLAUDE.md directive proposal ---
DIRECTIVE_SIGNALS = ["consolidate patched", "clean, efficient design", "not more patches",
                     "explain the plan/system in simple", "explain technical/jargon", "reason independently",
                     "don't replace an existing useful tool", "add alongside"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _hit(ask: str, needles: list[str]) -> str | None:
    a = _norm(ask)
    for n in needles:
        if n in a:
            return n
    return None


# A 'procedure' with fewer ordered steps than this is a rule wearing the wrong label. Two is the
# floor: one step is a constraint, and the distinction between them is the whole point of
# separating contracts from hooked_skills.
MIN_PROCEDURE_STEPS = 2


# A recurring ask whose evidence is at least this fraction "Nick stopping the work" rather than
# "Nick asking for something" is escalated from a passive reminder to a WORK-MOMENT hook. Set from
# the measured distribution on life's own corpus, where the share runs 0% to 100% across 16 asks and
# genuinely discriminates: the ones above this line are the ones he had to interrupt repeatedly
# (stay-scoped 88%, warn-before-spend 100%, consolidate-don't-patch 70%), the ones below are calm
# standing requests (use-codex 20%, ground-truth-first 0%). It is a floor for ESCALATION only —
# nothing is ever routed to a weaker terminal by it, so a wrong value costs specificity, not safety.
FRUSTRATION_HOOK_SHARE = 0.75


def route_type(ask: str, active_cluster_keys: set[str] | None = None,
               ask_type: str | None = None, still_recurring: bool = False,
               steps: int = 0, frustration_share: float = 0.0) -> dict:
    """Return {type, reason, oracle?} for one recurring ask. Deterministic; ambiguous → inject_contract.

    `ask_type` is the cached, closed-vocabulary label from the extraction step (ask_miner.ASK_TYPES).
    It is advisory: it can only select between two inject-mode shapes. It can never reach block-mode —
    that stays gated behind ORACLE_CATALOG below, which is checked FIRST and requires code + a locked
    equivalence test to extend."""
    a = _norm(ask)
    active_cluster_keys = active_cluster_keys or set()

    # 1) already generated as a live contract?
    if a in {_norm(k) for k in active_cluster_keys}:
        return {"type": "already_covered", "reason": "already an active contract"}
    # 2) already handled? A HOOK is enforcement and settles it. A RULE is prose, and prose only
    #    counts as coverage while the ask stops recurring.
    #
    #    Measured 2026-07-27: "orchestrate with Codex and Fable" was suppressed as covered by
    #    rule:codex-routing.md, yet 5 of its 17 recorded moments landed AFTER that rule shipped —
    #    two of them the same day Nick asked why it still wasn't happening. Treating "a rule exists"
    #    as "handled" is how the loudest recurring directive in the corpus generated nothing for
    #    three days. A rule that is still being restated is evidence of failure, not coverage.
    rule_bypass = False
    for concept, by in COVERED_CONCEPTS.items():
        if concept in a:
            if by.startswith("hook:"):
                # A hook settles it ONLY while the hook is actually registered. Unregistered, the
                # claim is documentation of a hook that used to run, which is prose — so it drops to
                # the same conditional treatment as a rule rather than terminating the route.
                if by.split(":", 1)[1] in live_hook_names():
                    return {"type": "already_covered", "reason": f"covered by {by}"}
                if still_recurring:
                    rule_bypass = True
                    break
                return {"type": "already_covered",
                        "reason": f"{by} is not registered, but the ask went quiet"}
            if by.startswith("rule:") and still_recurring:
                rule_bypass = True  # documented but not binding — build something that fires
                break
            return {"type": "already_covered", "reason": f"covered by {by}"}
    # 3) enforcement — ONLY if it maps to a catalog oracle that is oracle_ready.
    #
    #    ORDERING (2026-07-27), stated precisely because the first version of this comment claimed
    #    an ordering the code did not implement:
    #      · hook coverage           → already_covered, always. A hook enforces; never duplicate it.
    #      · rule coverage, quiet    → already_covered. The ask stopped recurring, so the rule IS
    #                                  working, and enforcing something that already stopped is
    #                                  strictly worse than leaving it alone.
    #      · rule coverage, recurring→ falls through HERE. An ask that keeps being restated despite
    #                                  its rule, and that maps to a code-defined oracle, should get
    #                                  the enforcement rather than a second reminder.
    #      · no coverage             → falls through here too.
    #
    #    This does NOT reopen what sentinel-code closed. The invariant it asked for was that a
    #    DATA-DRIVEN bypass cannot mint blocking power. Reaching enforcement still requires matching
    #    a signal list defined in THIS FILE, against an oracle with `oracle_ready`, whose condition
    #    comes verbatim from a template hash-pinned in artifact_generator, and which installs SHADOW
    #    and can only become enforced through friction_promote's proof window. Nothing in the
    #    corpus can add an oracle; that is still code plus a locked equivalence test.
    for okey, ent in ORACLE_CATALOG.items():
        if _hit(a, ent["signals"]):
            if not ent.get("oracle_ready"):
                # oracle not real yet → do NOT fake a block; fall through to a reminder instead
                return {"type": "inject_contract", "reason": f"enforcement deferred (oracle {okey} not ready)"}
            # BEING oracle_ready USED TO BE STRICTLY WORSE THAN NOT BEING READY (2026-08-28).
            #
            # Both catalog oracles declare event "Stop". friction-dispatch's Stop registration was
            # retired 2026-08-06 under Nick's policy that nothing drives the agent after the reply
            # is sent. friction_installer._event_is_dispatchable correctly REFUSES a block whose
            # event does not dispatch — so every ask reaching here died at install, silently,
            # while the identical ask with a NOT-ready oracle got a working inject_contract one
            # branch above. The readier the oracle, the worse the outcome.
            #
            # Measured on life the day this was written: `enforcement_block` had produced ZERO
            # artifacts in the lifetime of the loop, and the ask routed into it was the
            # HIGHEST-SUPPORT ask in the entire corpus — "use codex alongside core for substantial
            # system/code work", support 10, restated by Nick ten times and served by nothing.
            #
            # This does not grant blocking power anywhere new, loosen the oracle bar, or re-enable
            # a Stop dispatcher. It reuses the deferral that already existed one branch above, for
            # the one case it failed to cover: the oracle is real, and its EVENT is dead. If a Stop
            # dispatcher ever returns, this re-arms itself with no edit — same reasoning as
            # live_hook_names, and read from the same file the runtime actually executes.
            _ev, _live = ent.get("event"), dispatchable_events()
            if _ev and _live is not None and _ev not in _live:
                return {"type": "inject_contract",
                        "reason": f"enforcement deferred (oracle {okey} targets {_ev}, which no "
                                  f"longer dispatches) — serving the ask as a reminder"}
            return {"type": "enforcement_block", "reason": f"maps to static oracle {okey}",
                    "oracle": okey, "event": ent["event"], "condition_kind": ent["condition_kind"]}
    # A rule-covered ask that matched NO oracle escalates only as far as a live reminder.
    if rule_bypass:
        return {"type": "inject_contract",
                "reason": "documented by a rule that is not binding — escalating to a live reminder"}
    # 3.5) CADENCE — the ask has its own clock, so no prompt or tool trigger can serve it.
    #      PROPOSAL ONLY, and that ceiling is policy rather than immaturity: a scheduled job runs
    #      unattended and can spend money or take an outward action, and both are Nick's hard
    #      rules. Checked before `procedure` because a cadence ask is often ALSO procedural
    #      ("run the full close-reconciler at session close" is both), and the schedule is the
    #      part a hooked_skill cannot express — a skill still waits to be triggered.
    if _CADENCE.search(a):
        return {"type": "scheduled_job_proposal", "cadence": _CADENCE.search(a).group(0),
                "reason": "ask names a recurring time/lifecycle anchor — needs a schedule, "
                          "not a trigger; proposal only (spend + outward action are hard rules)"}
    # 4) diffuse standing preference → CLAUDE.md directive (checked before `procedure`: a diffuse
    #    preference has no trigger, so a procedure file for it would never be reachable)
    if _hit(a, DIRECTIVE_SIGNALS):
        return {"type": "claude_md_directive", "reason": "diffuse standing preference, no trigger/oracle"}
    # 5) procedure — the extraction step judged this a repeatable thing to DO, not a rule about HOW
    #    to act. Refuse if the capability already exists in ANY namespace (skills/commands/plugins).
    if ask_type == "procedure":   # ask SHAPE (DB vocabulary) → hooked_skill ARTIFACT
        # WORK-MOMENT detection. Some procedures are about a moment of WORK rather than a moment of
        # conversation — "ground truth against past history before making changes" wants to fire when
        # a change is about to happen, not when Nick says the word "changes". Those get a PreToolUse
        # trigger keyed on the tool instead of a prompt regex.
        #
        # Deliberately a small hardcoded pattern in THIS file, same discipline as ORACLE_CATALOG's
        # signal lists: the corpus can decide an ask is procedural, but it cannot decide that an
        # artifact gets to key on tool events. Measured 2026-07-27: 3 of 29 recurring asks match,
        # so the surface has real consumers rather than being widened speculatively.
        work_shape = bool(_WORK_MOMENT.search(a))
        # ARITY CHECK. `ask_type` is the extractor's opinion; `steps` is the evidence for it. An ask
        # claimed procedural that decomposes into fewer than two ordered steps is mislabelled — there
        # is no procedure to write down, only a rule — so it falls back to a contract rather than
        # producing a one-line payload file with a trigger attached. steps==0 means the extraction
        # predates the arity column and is treated as unknown, not as a failure.
        if 0 < steps < MIN_PROCEDURE_STEPS:
            return {"type": "inject_contract",
                    "reason": f"labelled procedure but decomposes into {steps} step — not a procedure"}
        dup = _duplicates_existing(a, _existing_capability_names())
        if dup:
            return {"type": "already_covered", "reason": f"capability '{dup}' already exists"}
        # WORKFLOW checked before slash_command: it is the "bigger claim" (multiple coordinating
        # agents), so an ask that happens to name both invocation AND orchestration gets the more
        # specific classification rather than the lighter one.
        if _MULTI_AGENT_SIGNALS.search(a):
            # CATALOG CHECK (Gap B, 2026-08-31). A multi-agent ask that ALSO names a concept this
            # Core already has a reviewed, hash-pinned Workflow script for gets the REAL terminal;
            # everything else still gets the honest proposal below, unchanged. Import is local and
            # wrapped: a routing decision must degrade to the proposal, never raise, if the catalog
            # module itself is absent (a fork without workflow_catalog.py) or its manifest fails
            # the trust-anchor check (workflow_catalog.match already fails closed to None on that;
            # the try/except here is belt-and-suspenders against an import-time error instead).
            try:
                import workflow_catalog as _wc
                cid = _wc.match(a)
            except Exception:
                cid = None
            if cid:
                return {"type": "workflow_run", "work_shape": work_shape, "catalog_id": cid,
                        "reason": f"procedure ask names multiple coordinating agents AND matches "
                                  f"catalog entry {cid!r} — a real, catalog-pinned workflow "
                                  f"command, not a proposal"}
            return {"type": "workflow", "work_shape": work_shape,
                    "reason": "procedure ask names multiple coordinating agents — a workflow-tool "
                              "proposal, not a single gated trigger"}
        if _DELIBERATE_INVOCATION.search(a):
            return {"type": "slash_command", "work_shape": work_shape,
                    "reason": "procedure ask names deliberate, on-demand invocation — a command "
                              "the operator runs by name, not an ambient trigger"}
        return {"type": "hooked_skill", "work_shape": work_shape,
                "reason": ("extraction typed this a repeatable procedure"
                           + (" at a work moment" if work_shape else ""))}
    # 6a) WORK-MOMENT HOOK (2026-08-20) — the frustration terminal Nick asked for 67 times.
    #
    # THIS IS AN UPGRADE OF THE WEAKEST TERMINAL, NOT A SIXTH COMPETITOR. Everything above still
    # wins: already_covered, enforcement_block, claude_md_directive and hooked_skill are all decided
    # before this line. Only an ask that would otherwise become a passive reminder is eligible, so
    # this can never take an ask away from a better home — the failure mode of adding a route.
    #
    # WHY A HOOK RATHER THAN A REMINDER. A reminder is prose that arrives with the prompt and is read
    # before the work. These asks are the ones Nick had to INTERRUPT — the evidence is dominated by
    # stop-execution and explicit-no, meaning the reminder either was not there or did not hold. A
    # work-moment artifact fires at the mutating tool call itself, which is the moment the thing he
    # is stopping actually happens.
    #
    # NO NEW ENGINE. This is a PreToolUse artifact on the existing dispatcher, keyed on the same
    # closed tool vocabulary `_gen_procedure`'s work_shape branch already uses. It inherits the test
    # gate, the corpus specificity rule, rollback and the kill switch. Writing a new .py into
    # settings.json would have been a second mechanism beside a working one — the exact accretion
    # Nick's standing directive forbids, and it would have bypassed every gate listed above.
    #
    # INJECT ONLY, AND NEVER Stop. `effect.mode` stays inject: this speaks at the moment of work, it
    # does not block. Blocking requires ORACLE_CATALOG, a hash-pinned template and a human, and the
    # Stop event is off the table entirely by Nick's 2026-08-06 directive.
    #
    # `still_recurring` is required: an ask that stopped recurring is one whose current handling is
    # working, and escalating a solved problem is strictly worse than leaving it alone.
    if frustration_share >= FRUSTRATION_HOOK_SHARE and still_recurring:
        return {"type": "work_hook", "frustration_share": round(frustration_share, 2),
                "reason": (f"{frustration_share:.0%} of this ask's evidence is the operator stopping the "
                           f"work (>= {FRUSTRATION_HOOK_SHARE:.0%}) and it is still recurring — "
                           f"a reminder has not held, so it fires at the work moment instead")}
    # 6) DEFAULT — context-triggerable reminder
    return {"type": "inject_contract", "reason": "default: context-triggered reminder"}


def route_all(recurring: list[dict], active_cluster_keys: set[str] | None = None) -> list[dict]:
    out = []
    for r in recurring:
        d = route_type(r.get("ask", ""), active_cluster_keys)
        out.append({**r, "route": d})
    return out


def main() -> int:
    import json, os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    # `_Path`, not `Path` — this module imports pathlib.Path UNDER AN ALIAS, so the bare name
    # was never bound and main() raised NameError before it reached anything. Not caught by
    # the suite because main() is a __main__-only CLI entrypoint no test imports; caught by
    # sentinel-code executing it during review of the baseline push.
    sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "scheduling" / "brain-pg"))
    import ask_miner
    from _env import get_org_id  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
    org = get_org_id()
    rec = ask_miner.recurring_asks(org, 3)
    routed = route_all(rec)
    from collections import Counter
    tally = Counter(r["route"]["type"] for r in routed)
    print(f"routed {len(routed)} recurring asks: {dict(tally)}\n")
    for r in routed:
        print(f"  [{r['support']:>2}x] {r['route']['type']:<20} {r['ask'][:52]}  · {r['route']['reason']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
