#!/usr/bin/env python3
"""measure-contract-fitness.py — the MEASURE organ, restored for the LEARNED layer (B2, 2026-06-08).

History: the measure->retire half of the SI loop was built 2026-05-26 (measure-rule-fitness.py) for the
OLD rule loop, then archived 2026-06-05 in the learned-layer pivot — so the live learned layer has no
feedback half. This ports that logic to the learned layer: for each synthesized contract, does the
correction class it addresses keep RECURRING after the contract shipped? (si-enforcement-break-diagnosis
Fix 5: "flag any contract whose fire-count is high while its correction class still recurs" — the
escape-hatch / not-binding signal.)

Join: learned_contracts.trigger_labels  <->  pattern_observations.pattern_label.
Window split: pattern_observations.created_at  <  vs  >=  contract.created_at.
Rate-normalized (per-week) because the pre/post windows differ in length.
Fire-count: .claude/state/learned-fires.log (TSV: hook \\t contracts-csv \\t prompt).

Verdict:
  post_days < MIN_POST_DAYS      -> INSUFFICIENT (too soon)
  post_count == 0                -> GRADUATED    (recurrence stopped)
  post_rate < pre_rate * DECAY   -> DECAYING     (working)
  else                           -> NOT-BINDING  (fires but the correction keeps coming -> structural escalation)

Persistence: writes .claude/state/contract-fitness.json (read by /core-si). A state file, not a DB
table, because brain_app lacks CREATE on schema public (DDL is owner-only) — and this mirrors how
brain-health persists (.brain-health-status). /core-si consumes the JSON; no SQL queryability needed.

Usage:
  python3 measure-contract-fitness.py            # compute + print scorecard + write the JSON
  python3 measure-contract-fitness.py --dry-run  # print only, no file write
"""
import os
import sys
import json
import bisect as _bisect
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain-pg"))
# WRITES are RLS-scoped to CORE_ORG_ID. READS ARE NOT — every query here must scope itself.
#
# This line used to read "(brain_app, RLS-scoped to CORE_ORG_ID)" without qualification, and that is
# WHY _confounded() below shipped without an org predicate: the author, and every later reader, took
# scoping as ambient. core-business checked the policies instead of the comment:
#
#     pattern_observations   relrowsecurity=True   relforcerowsecurity=True   3 policies
#         _select   PERMISSIVE   qual: true                       <- reads UNSCOPED
#         _update   PERMISSIVE   qual: org_id = current_setting(…)
#
# Verified through this exact connect_corebrain() path, as brain_app (rolbypassrls=False) with
# app.current_org_id=1: `SELECT org_id, count(*) FROM entities GROUP BY org_id` returns ALL FIVE
# ORGS. 30 of 31 org-partitioned tables share the shape.
#
# WHETHER THAT IS A DEFECT IS NICK'S CALL, not this file's: several policies are named `read_all`,
# and CLAUDE.base.md documents the brain as a SHARED vault with org PARTITIONING — partitioning is
# not isolation, and a shared recall vault may be meant to be readable across Cores. Until that is
# decided, every SELECT's explicit predicate is LOAD-BEARING rather than belt-and-braces, and a
# comment promising ambient scoping is the thing most likely to remove one.
from _env import connect_corebrain, get_org_id  # noqa: E402

DECAY = 0.5
MIN_POST_DAYS = 2

# THE CORPUS-VOLUME CONFOUND (core-business, T009, 2026-08-12). Every DECAYING verdict this module
# has emitted compared a contract's pre/post rate against DECAY alone — against nothing.
#
# The WHOLE CORPUS fell across the same split. Measured independently on life, org 1, GEN-filtered,
# machine rows excluded: 184 rows / 21 days pre = 61.3/wk, 363 rows / 68 days post = 37.4/wk.
# A baseline ratio of 0.61. So a contract whose correction merely tracked the corpus would post a
# ratio near 0.61 and, against a fixed DECAY of 0.5, could read as nearly-decaying while nothing
# about that behaviour had changed at all.
#
# It cuts both ways and business worked both: three of life's four DECAYING verdicts SURVIVE
# because their ratios (0.40, 0.39, 0.29) sit well below the baseline — that is decay beyond what
# the corpus explains, and it is a stronger claim than the raw verdict was making. One verdict at
# ratio 0.69 is ABOVE baseline: its correction fell LESS than the corpus did, so it is not decaying
# in any meaningful sense and had the right label for the wrong reason.
#
# Reporting the baseline alongside every verdict rather than silently re-scoring: the verdict rule
# stays auditable and the confound becomes visible instead of being folded invisibly into a number.
# A ratio the reader cannot see is a ratio the reader cannot argue with.
# MIN_PRE_N = 5, and the number is NOT tuned to make a particular verdict flip.
#
# core-business refuted model-routing-and-defaults as computed on pre n=2. Measured here it is
# pre n=3 / post n=1 — so at MIN_PRE_N=3 the guard missed it by exactly one row, and business's
# substance held even though its count did not: a ratio built on three events against one is noise
# whichever of us has the count right, and dividing by a 21-day window does not make it less so.
#
# 5 comes from the external evaluation lane's own criterion — size the window so there are at least
# ~5 expected events per side under the null — not from wanting this case to change. Picking a
# threshold because it produces the answer you already believe is the Goodhart failure this whole
# instrument exists to avoid, and it would be indistinguishable from good judgement afterwards.
#
# It also catches the other end: plan-not-execute now has pre n=0 (the machine-row exclusion emptied
# its pre window) and was still emitting a verdict. A rate comparison against zero observations is
# not a weak signal, it is no signal.
MIN_PRE_N = 5

# Verdicts that make NO comparative claim, so the power floor cannot apply to them: they already
# refuse (the INSUFFICIENT* family), or they assert a category rather than a rate (UNENFORCEABLE is
# a statement about what a gate could ever detect; GATED-WATCH is "too early, look again").
_NON_COMPARATIVE = ("INSUFFICIENT", "UNENFORCEABLE", "GATED-WATCH", "QUARANTINED")


def _apply_power_floor(verdict: str, why: str, pre: int):
    """Downgrade any comparative verdict whose pre-window is too thin to support one.

    WHY THIS IS A VERDICT AND NOT A FILTER (2026-08-12, the third venue of one defect).

    The floor was first written as the string "[pre n=%d, TOO FEW to interpret]" appended to the
    RATIONALE — prose a human reads, while every consumer branches on `verdict`, a label a machine
    reads. It was then added a second time as a `pre_count >= MIN_PRE_N` filter on the two lists
    this module EMITS (`not_binding`, `not_binding_artifacts`). Both were real fixes and both missed,
    because `friction_loop` does not only read those two lists — at :1149 and :1156 it RE-DERIVES two
    more sets straight from the raw rows:

        not_binding_fired       = {r for r in si_artifacts if r["verdict"] == "NOT-BINDING-FIRED"}
        not_binding_fired_slugs = {r for r in contracts    if r["verdict"] == "NOT-BINDING" and fires}

    and then (:1160) SUBTRACTS the second from the floored list, so the floor was not merely bypassed
    but inverted. Those two sets are what reach `flag_needs_oracle`. Measured after the list-level
    fix shipped: `plan-not-execute` (pre n=0) and art_97b6…/art_3c7e… (pre n=1, n=0) were all still
    in the actionable sets. The 111 repeated flags were never touched by either earlier fix.

    A guard that must be repeated at each consumer will be missed at the next consumer. So the floor
    is applied ONCE, here, to the verdict itself — the field every consumer already branches on,
    including consumers not yet written and the three peer Cores running their own copy of the
    reader. No consumer needs to know MIN_PRE_N exists.

    `INSUFFICIENT-UNDERPOWERED` is deliberately NOT a new mechanism: this module already refuses with
    `INSUFFICIENT` (too few post-days) and `INSUFFICIENT-CONFOUNDED` (windows not comparable by
    activity). "Too few pre-observations" is the third way a comparison is invalid, so it joins that
    family and inherits its handling for free. Nothing downstream acts on INSUFFICIENT*.

    APPLIES TO SUCCESS VERDICTS TOO, not only to NOT-BINDING. Carving the floor down to "the verdicts
    that currently drive actions" would rebuild the exact split that caused this bug — a guard on one
    branch and not its sibling — and the next consumer of GRADUATED would inherit the hole. Verified
    before widening: nothing outside this module acts on GRADUATED (export-si-trajectory computes its
    own from CLEAN_MIN_D), so for that family this is a reporting change, and it moves in the
    direction that COSTS us claimed successes rather than the one that flatters.

    The superseded verdict is carried in the rationale rather than discarded. A row that silently
    changes label is how the fire_count:0 -> GRADUATED defect read as health.
    """
    # THE LABEL STAYS; THE REASON GETS MORE HONEST (2026-08-18, found by core-business).
    #
    # business measured its live fitness file: **5 of 11 contracts have a pre-window under MIN_PRE_N
    # and all five report as CONFOUNDED**, because `_NON_COMPARATIVE` contains "INSUFFICIENT" and
    # "INSUFFICIENT-CONFOUNDED" startswith it, so this floor never runs once the confound test fires.
    # The clean case is `plan-not-execute` at **pre 0, post 0**, told "the windows differ so much in
    # ACTIVITY that a per-calendar-day rate cannot be compared" — a statement about a comparison with
    # no operands on either side.
    #
    # I FIRST RE-LABELLED THESE TO UNDERPOWERED AND THE SUITE REFUSED IT.
    # `test_underpowered_verdict_not_actionable.py:92-93` asserts, by name and with a reason:
    #     ("INSUFFICIENT",            0, survives, "already refuses — must not be double-downgraded")
    #     ("INSUFFICIENT-CONFOUNDED", 0, survives, "already refuses, for the other invalid comparison")
    # That is a deliberate prior decision with a test guarding it, not an oversight, and a drive-by
    # relabel would have overwritten someone's stated reasoning on the strength of a peer's message.
    #
    # Both positions are right about different things, so neither has to lose: the prior decision is
    # about the VERDICT (an already-refusing verdict must not be re-refused), business's finding is
    # about the REASON A HUMAN READS. The remedies differ and that is why the reason matters —
    #     CONFOUNDED    windows not comparable by activity  -> a better denominator would help
    #     UNDERPOWERED  nothing in the pre-window at all    -> a denominator does nothing; needs data
    # so reporting the first alone invites the conclusion that fixing the denominator unblocks these
    # contracts. On business it would unblock six of eleven, not all.
    #
    # Therefore: keep the label, append the count. No verdict changes, no downstream risk, and the
    # human reads both facts instead of one.
    _v = str(verdict)
    if _v.startswith(_NON_COMPARATIVE):
        if pre < MIN_PRE_N and _v.startswith("INSUFFICIENT") and "MIN_PRE_N" not in str(why):
            why = (f"{why} ALSO under-powered: the pre-window holds {pre} observation(s), under "
                   f"MIN_PRE_N={MIN_PRE_N} — a better denominator would not make this comparable, "
                   f"because there is nothing on the pre side to compare against.")
        return verdict, why
    if pre >= MIN_PRE_N:
        return verdict, why
    return "INSUFFICIENT-UNDERPOWERED", (
        f"pre-window holds {pre} observation(s), under MIN_PRE_N={MIN_PRE_N} — there is nothing for "
        f"the post rate to be measured AGAINST, so no comparative verdict is supportable. "
        f"Would otherwise have read: {verdict} — {why}")


# A zero is only evidence of absence when a non-zero was likely. Above this probability of seeing
# post==0 by chance ALONE — the ask recurring at exactly its old rate, nothing prevented — the zero
# carries no information and cannot support "the ask stopped".
#
# RAISING THIS NUMBER IS NOT A TUNING TWEAK. Read the required-exposure table in _apply_zero_floor
# first. Its only possible effect is to make flattering verdicts reachable again on evidence that has
# not changed — which is the defect this constant exists to close. core-finance flagged the pressure
# before it could be acted on (bus #1360): "the obvious way to get successes back is to raise
# POST_ZERO_ALPHA, and that reintroduces exactly the defect." If a weaker claim is genuinely wanted,
# say so in the verdict's own name rather than by loosening the threshold behind it.
POST_ZERO_ALPHA = 0.05


def _zero_is_uninformative(pre: int, pre_n: int, post_n: int):
    """P(post == 0 | the ask still recurs at its pre-window rate). Returns (uninformative, p).

    Exposure is counted in OBSERVATIONS, not in days, and that choice is the whole point. A
    per-week rate over post_days assumes corrections arrive at a steady wall-clock rate; Nick's
    session density varies by an order of magnitude between a sprint and a quiet week, which is the
    same confound `_confounded()` exists to refuse. Observation-count exposure is invariant to it.

    Bernoulli per observation, so P(zero) = (1 - rate)^post_n. Deliberately the crudest defensible
    model: it needs no distributional assumption beyond "each observation is a chance for the ask
    to appear", and it is being used to REFUSE a claim, so erring toward refusal is the safe error.
    """
    if pre_n <= 0 or post_n <= 0:
        return True, 1.0            # no exposure measured -> a zero cannot mean anything
    rate = pre / pre_n
    if rate <= 0:
        return True, 1.0            # never seen before either; its absence after is not news
    if rate >= 1:
        return False, 0.0
    p = (1.0 - rate) ** post_n
    return p > POST_ZERO_ALPHA, p


def _apply_zero_floor(verdict: str, why: str, pre: int, post: int, pre_n: int, post_n: int):
    """Refuse a GRADUATED whose post-window was too small for the ask to have shown up.

    WHY THIS EXISTS (2026-08-13). The pre-side floor above and this one are the SAME defect on
    opposite sides of the split, and only one side had a guard. `MIN_POST_DAYS = 2` is a floor in
    the wrong unit: days are not chances. An artifact installed 2026-08-08 clears it with 5 days
    while the corpus holds 17 observations after it — and a trigger matching ~1.5% of rows expects
    0.26 hits in that window. Zero is then not a result, it is the default.

    The asymmetry is what makes it worth a verdict rather than a caveat. An underpowered PRE window
    already produces INSUFFICIENT-UNDERPOWERED, which is honest and costs the loop a claim. An
    underpowered POST window produced GRADUATED — the flattering direction. A measurement whose
    two failure modes are "admit nothing is known" and "declare success" is not a measurement.

    Measured across life's 17 trigger-carrying artifacts before this shipped: E[post] under
    "nothing changed" was below 0.7 for EVERY ONE of them, highest 0.65, and 15 of 17 read
    GRADUATED. The single GRADUATED the shipped matcher reported — art_cf3349715, "ask stopped
    recurring after firing 4x" — had a 57% chance of showing zero with the ask recurring at its
    full old rate. That was the loop's ONLY success verdict.

    Applies to GRADUATED-UNPROVEN too. That verdict already declines to credit the artifact, but it
    still asserts the ask STOPPED; the same zero fails to support the same half of the sentence.

    WHAT THIS MAKES UNREACHABLE, STATED SO NOBODY HAS TO REDISCOVER IT (core-finance, bus #1360,
    verified independently here). A significant zero needs post_n >= ln(alpha)/ln(1-rate). At life's
    own measured rates that is:

        artifact         pre rate    post_n needed for p <= 0.05
        art_bc7cea       14/585                    124
        art_cedafa       12/585                    145
        art_cf3349715     5/301                    179
        art_a12282        9/595                    197
        art_110d7e        5/558                    333

    **The largest post window on this seat is 49 observations.** So GRADUATED is not merely rarer
    after this change — it is currently UNREACHABLE, and the fitness report will show zero successes.

    That is the correct outcome: the successes were never established, and this floor revealed that
    rather than caused it. It is written down because two wrong reactions follow from the empty
    column, and the second is the dangerous one:

      (a) a reader sees GRADUATED go 15 -> 0 and concludes the fix broke the measurement;
      (b) the obvious way to "get successes back" is to raise POST_ZERO_ALPHA — which restores the
          flattering verdict on evidence that has not changed. **The pressure this fix creates points
          straight at undoing it.** Naming the required exposure makes that visibly a choice to
          accept a weaker claim rather than a threshold nudge.

    The real way to reach GRADUATED is more post-window exposure — i.e. time, or a corpus that grows
    — not a smaller alpha.
    """
    if post != 0 or not str(verdict).startswith("GRADUATED"):
        return verdict, why
    uninformative, p = _zero_is_uninformative(pre, pre_n, post_n)
    if not uninformative:
        return verdict, f"{why} [post-window zero is significant: p={p:.3f}]"
    return "GRADUATED-UNDERPOWERED", (
        f"post-window holds {post_n} observation(s); at the pre-window rate ({pre}/{pre_n}) a count "
        f"of zero would occur by chance with p={p:.2f} — above POST_ZERO_ALPHA={POST_ZERO_ALPHA}, so "
        f"the zero is not evidence the ask stopped. Would otherwise have read: {verdict} — {why}")


GATE_WATCH_DAYS = 7  # a freshly-gated contract gets this long to bend the curve before re-alarming
INSTANCE = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parents[1].parent))
FIRES_LOG = INSTANCE / ".claude" / "state" / "learned-fires.log"

# learned-fires.log's contract column also carries RECORD KINDS, not only contract names.
# learned-stopguard.py:117 and learned-recallguard.py:122 write "shadow" for a near-fire (the
# denominator those guards exist to produce); "block" appears as a verdict. Reserved here so
# fire_counts() can separate them instead of tallying them as contracts. Populated by
# fire_counts(); read it for the near-fire telemetry rather than re-deriving it.
#
# `shadow` and `block` ARE NOT THE SAME THING — corrected 2026-08-12 by a probe core-finance
# authored and ran on its own seat, after my first fix filed both as telemetry.
#
#   shadow  the contract nearly fired and did not. Near-fire. The denominator those guards exist
#           to produce. NOT a fire.
#   block   the contract ENFORCED. learned-recallguard.py:139 emits
#           {"decision":"block","reason":"LEARNED CONTRACT — recall-first: ..."} — that is the
#           contract doing its job, and it is a REAL FIRE of the contract the hook names.
#
# Filing `block` as telemetry was worse than the bug it replaced: a contract that enforces ONLY via
# the block path reads fires==0 and grades NOT-BINDING-NO-FIRE — "the trigger never matched" —
# while it had blocked every single time. That is a confident wrong answer about the exact question
# the instrument exists to answer, and it would have argued for retiring a contract that works.
#
# The map is DERIVED, not assumed: each hook names its own contract in its own LEARNED CONTRACT
# string. `stopguard` names none, so it is deliberately absent — an unmapped hook is counted into
# FIRE_UNATTRIBUTED and printed, never silently dropped and never credited to a guess. Inventing an
# attribution here would move a fitness verdict on no evidence, which is the whole failure mode.
_FIRE_RECORD_KINDS = frozenset({"shadow"})
_BLOCK_HOOK_CONTRACT = {
    "recallguard": "recall-first",     # learned-recallguard.py:136 "LEARNED CONTRACT — recall-first:"
    "validator": "stop-and-plan",      # learned-validator.py       "LEARNED CONTRACT — stop-and-plan:"
}
FIRE_RECORDS = Counter()
FIRE_UNATTRIBUTED = Counter()
OUT_FILE = INSTANCE / ".claude" / "state" / "contract-fitness.json"
IDENTITY = INSTANCE / ".claude" / "identity.json"


_BASELINE_CACHE = {}


def _corpus_baseline(cur, split):
    """post/pre rate ratio for the WHOLE corpus across the same split. See MIN_PRE_N above.

    Returned so a verdict can be read against it: a contract whose ratio merely tracks this number
    changed nothing that the corpus does not already explain. Cached — it is identical for every
    contract sharing a split and this runs once per contract otherwise.
    """
    key = str(split)
    if key in _BASELINE_CACHE:
        return _BASELINE_CACHE[key]
    try:
        cur.execute(
            "SELECT count(*) FILTER (WHERE COALESCE(session_date, created_at::date) <  %s),"
            "       count(*) FILTER (WHERE COALESCE(session_date, created_at::date) >= %s),"
            "       min(COALESCE(session_date, created_at::date)),"
            "       max(COALESCE(session_date, created_at::date))"
            "  FROM pattern_observations"
            " WHERE org_id = current_setting('app.current_org_id', true)::bigint"
            "   AND correction_text IS NOT NULL AND excluded_reason IS NULL AND " + GEN,
            (split, split))
        pre_n, post_n, lo, hi = cur.fetchone()
        pre_days = max((split - lo).days, 1) if lo else 1
        post_days = max((hi - split).days, 1) if hi else 1
        pre_rate = pre_n / pre_days
        post_rate = post_n / post_days
        val = (post_rate / pre_rate) if pre_rate > 0 else None
    except Exception:
        val = None          # never let the annotation break a verdict
    _BASELINE_CACHE[key] = val
    return val


def _reply_rate_note(pre_rr, post_rr) -> str:
    """The per-reply half of a verdict sentence, or an explicit statement that it is unavailable.

    Written because `per_reply` shipped fully built, fully tested, and CALLED BY NOTHING — found by
    core-business within an hour. Computing it and dropping it on the floor would have been the same
    defect one step later, so it lands in the rationale a human actually reads.
    """
    if pre_rr is None or post_rr is None:
        return " · per-reply unavailable (transcripts do not cover the pre-window)"
    direction = "agrees" if (post_rr < pre_rr) == True else "DISAGREES"
    return (f" · per-reply {pre_rr:.2f} -> {post_rr:.2f} per 1k replies ({direction} with the "
            f"per-week direction)")


def _vs_baseline(cur, split, pre_rate, post_rate):
    """Suffix naming how a contract's ratio compares to the corpus-wide one."""
    base = _corpus_baseline(cur, split)
    if base is None or pre_rate <= 0:
        return ""
    ratio = post_rate / pre_rate
    if ratio <= base * 0.75:
        return f" [ratio {ratio:.2f} vs corpus baseline {base:.2f} — decay beyond corpus volume]"
    return (f" [ratio {ratio:.2f} vs corpus baseline {base:.2f} — NOT distinguishable from the "
            f"corpus falling; the whole corpus fell too]")


def _gates() -> dict:
    """Contracts with a LIVE structural gate (identity.json `learned_contract_gates`).
    NOT-BINDING + gated + within GATE_WATCH_DAYS of the gate going live ->
    GATED-WATCH (act -> watch -> re-alarm loop) instead of re-proposing the
    already-completed 'wire the gate' action every session."""
    try:
        g = json.loads(IDENTITY.read_text()).get("learned_contract_gates", {})
        return {k: v for k, v in g.items() if isinstance(v, dict) and v.get("since")}
    except Exception:
        return {}


def _unenforceable() -> set:
    """Contracts that are structurally unenforceable (linguistic-signal category
    errors, e.g. 'frustration') — marked UNENFORCEABLE not NOT-BINDING, so core-si
    stops proposing an impossible gate. From identity.json."""
    try:
        return set(json.loads(IDENTITY.read_text()).get("structurally_unenforceable_contracts", {}).get("contracts", []))
    except Exception:
        return set()


# --- ENFORCEMENT HONESTY (2026-08-31) --------------------------------------------------------
#
# Measured the day this shipped: learned_contracts held 42 rows fleet-wide, 41 with
# checkable='[]'. That number reads as "97% of what the loop installs is unenforced prose", and
# it is wrong in BOTH directions, which is why the tier below is DERIVED and never read from the
# column:
#
#   · `checkable` is read by NO code. learned-validator.py — the Stop hook the seed docstring
#     says "BLOCKS on" these clauses — hardcodes its predicate (HALT_RX/MUTATING_TOOLS/ACK_RX)
#     and never queries the DB. The snapshot the classifier reads carries only required_shape/
#     forbidden_moves. So the one populated row overclaims mechanism, and the four seeded
#     stop-and-plan copies on peer orgs — enforced by the SAME fleet-wide hook — carry [].
#   · Every automated writer is FORBIDDEN from populating it (si_seed_base inserts '[]',
#     si_induct inserts [], learned-resynth's --apply excludes it by design). Hand-gated with
#     no hand: the column can never grow, so its emptiness measures policy, not honesty.
#
# What Nick actually needs to know per row is: does anything MECHANICALLY act when this is
# violated, or is it prose asking the model nicely? Three tiers, each derived from what runs:
#
#   enforces     a registered hook blocks/refuses on this contract's predicate
#   advises      inject-only guidance (the honest majority — and honestly labelled as such)
#   placeholder  an si_induct parked ask still carrying the hardcoded boilerplate shape —
#                not a contract at all, an unprocessed backlog row wearing a contract's name
#
# Same discipline as live_hook_names in artifact_typer (imported, not reimplemented): the tier
# is checked against settings.json registration, so retiring a hook demotes its contracts to
# `advises` on the next measure with no edit here. FAILS TOWARD `advises`: an import or read
# error can only UNDERSTATE enforcement, never claim a net that is not there — the direction
# CLAUDE.base.md's "a rule promising a gate that no longer runs" lesson requires.
#
# The map is a claim about the running system, so each entry names its evidence:
#   stop-and-plan  -> learned-validator   (Stop; blocks mutating-tool-after-halt, hardcoded
#                                          port of the ONE hand-written checkable clause)
#   recall-first   -> recall-first-gate   (PreToolUse; refuses mutating tools while the
#                                          .recall-required marker is set — live 2026-06-09)
# verification-trigger is deliberately ABSENT: it injects and "never blocks" (its own
# docstring), so verify-dont-claim stays `advises` — the retired state-claim-gate is exactly
# the overclaim this tier exists to prevent.
_CONTRACT_ENFORCERS = {
    "stop-and-plan": "learned-validator",
    "recall-first": "recall-first-gate",
}

# si_induct.induce_inject_only's hardcoded body, verbatim. A row still carrying it has had no
# learned content synthesized since induction — it is a parked ask, and counting it as an
# installed contract inflates coverage.
_PLACEHOLDER_SHAPE = ["acknowledge the recurring correction plainly",
                      "address the specific thing, not a generic apology"]


def _registered_hook_names() -> frozenset:
    """Hook basenames registered in settings.json — artifact_typer.live_hook_names, one source."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from artifact_typer import live_hook_names
        return live_hook_names()
    except Exception:
        return frozenset()   # unknown -> nothing counts as enforced (understate, never overclaim)


def classify_contract_enforcement(short: str, required_shape, checkable, live: frozenset) -> dict:
    if list(required_shape or []) == _PLACEHOLDER_SHAPE:
        return {"tier": "placeholder", "mechanism": None,
                "note": "si_induct boilerplate body — a parked ask, not a learned contract; "
                        "route it to a real terminal or retire it"}
    n_chk = len(checkable or [])
    hook = _CONTRACT_ENFORCERS.get(short)
    if hook and hook in live:
        return {"tier": "enforces", "mechanism": f"hook:{hook}",
                "note": ("predicate hardcoded in the hook; the checkable column is not read by "
                         "any code" + (f" (column carries {n_chk} clause(s), decorative)" if n_chk else ""))}
    return {"tier": "advises", "mechanism": None,
            "note": ("inject-only guidance"
                     + (f"; checkable carries {n_chk} clause(s) NO CODE READS" if n_chk else ""))}


def classify_artifact_enforcement(spec: dict, quarantined: bool, dispatchable) -> dict:
    """Tier for a live-spine artifact, from its own spec + what actually dispatches.

    `dispatchable` is artifact_typer.dispatchable_events() — None means unknown, and unknown
    must not mint the worst label, so the dead-event tripwire requires a POSITIVE read.
    """
    mode = ((spec.get("effect") or {}).get("mode") or "inject")
    enforced = bool(spec.get("enforced"))
    event = spec.get("event")
    if quarantined:
        return {"tier": "inert", "mechanism": None, "note": "quarantined — dispatcher never loads it"}
    if mode != "block":
        return {"tier": "advises", "mechanism": None, "note": "inject-only reminder"}
    if not enforced:
        note = "shadow block — records, never enforces"
        if event and dispatchable is not None and event not in dispatchable:
            note += f"; its event ({event}) does not dispatch, so it cannot even record"
        return {"tier": "shadow", "mechanism": None, "note": note}
    if event and dispatchable is not None and event not in dispatchable:
        # The one state worse than advisory: claims to enforce, and its event never fires.
        # Zero rows today; the label existing is the tripwire.
        return {"tier": "claims-enforce-dead-event", "mechanism": f"event:{event}",
                "note": f"enforced=true but {event} is not a dispatched event — this block "
                        f"CANNOT fire; it is prose wearing an enforcement flag"}
    return {"tier": "enforces", "mechanism": f"dispatch:{event}", "note": "enforced block on a live event"}


# THE DETECTOR GENERATION FILTER (2026-07-30, master plan Phase 0.8).
#
# pattern_observations holds two generations on core-life: 838 rows from the retired `v1`
# detector (2026-05-21 -> 2026-06-05) and 443 from `learned-miner-v1` (2026-06-05 -> now).
# Every contract in learned_contracts shipped on 2026-06-05 — exactly the changeover date.
#
# So every fitness verdict compared a PRE window that is 838 v1 + 189 live rows (82% fossil)
# against a POST window that is 100% live. "plan-not-execute: pre 9.3/wk -> post 0.4/wk,
# DECAYING" is not a behaviour change; a 23x drop across a detector swap is what a detector
# swap looks like. Every DECAYING verdict this module has ever emitted is suspect for the
# same reason.
#
# The plan called for ARCHIVING the 838 rows. They are real corrections and worth keeping —
# the defect is that the MEASUREMENT mixes generations, not that the history exists. Filtering
# here fixes the measurement, keeps the history, and is reversible by deleting one clause.
LIVE_DETECTOR = "learned-miner-v1"
GEN = f"detector_version = '{LIVE_DETECTOR}'"


def per_week(count, days):
    return (count / days * 7.0) if days > 0 else 0.0


def _replies_in(start_iso, end_iso=None):
    """Assistant replies in a window, from `bin/si-objective.reply_count`. None if unanswerable.

    IMPORTED, NEVER REDEFINED. The 2026-08-12 denominator work with core-finance ended on a
    principle rather than a number: call what the pipeline already uses, so the measuring predicate
    and the counted thing cannot drift. There is exactly one definition of "a reply" in this system
    and it lives in si-objective; a second one here would be the drift that principle forbids.

    Returns None rather than 0 on failure or on an empty window. Zero would silently become a
    denominator and produce an infinite rate; None is refused by the caller and reported as
    unanswerable, which is the honest shape for a question the transcripts cannot reach.
    """
    import datetime as _dt
    import glob as _glob
    import importlib.util as _il
    import json as _json

    def _ep(d):
        return int(_dt.datetime.strptime(str(d)[:10], "%Y-%m-%d")
                   .replace(tzinfo=_dt.timezone.utc).timestamp())
    try:
        src = Path(__file__).resolve().parents[2] / "bin" / "si-objective.py"
        spec = _il.spec_from_file_location("_sio", src)
        mod = _il.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # COVERAGE FIRST, COUNT SECOND (2026-08-20). Without this the function happily returns a
        # count for a window the transcripts barely reach, and the resulting rate is not merely
        # noisy — it is systematically flattering. Measured the moment it was written: life's
        # directive pre-window asks for 2026-05-15..07-23, transcripts begin 07-12, so 11 of 69 days
        # are covered and the pre-rate divides by ~1,251 replies against the post-window's ~9,009.
        # That reported -96% where the honest answer is -30%: a threefold improvement invented by a
        # denominator that could not see most of its own window.
        #
        # `cleanupPeriodDays` (default 30) is why. This is §17's retention constraint arriving as a
        # measurement bug, and refusing is the only correct response — an unanswerable question must
        # come back unanswerable, not as a number that happens to favour the thing being measured.
        earliest = None
        for f in _glob.glob(str(mod.TRANSCRIPTS / "*.jsonl")):
            try:
                with open(f, errors="ignore") as fh:
                    for ln in fh:
                        ts = _json.loads(ln).get("timestamp")
                        if isinstance(ts, str) and ts[:4].isdigit():
                            if earliest is None or ts < earliest:
                                earliest = ts
                            break
            except Exception:
                continue
        if earliest is None or str(earliest)[:10] > str(start_iso)[:10]:
            return None   # the window predates every surviving transcript — unanswerable, not zero
        n = mod.reply_count(_ep(start_iso), _ep(end_iso) if end_iso else None)
        return n if n > 0 else None
    except Exception:
        return None


def per_reply(count, start_iso, end_iso=None):
    """Occurrences per 1,000 assistant replies. None when the window has no transcript coverage.

    WHY THIS EXISTS BESIDE per_week RATHER THAN REPLACING IT (2026-08-20).

    On 2026-08-18 core-business ran the first efficacy measurement ever performed on this loop's only
    successful output — life's two applied CLAUDE.md directives — and it FLIPPED SIGN on the choice
    of denominator: -27% per calendar day, +71% per correction-moment. Overall correction volume had
    fallen 57% in the same window, which is exactly the confound a per-day rate cannot see. Reporting
    either number alone is a verdict manufactured by a choice nobody had standardised.

    Replies is the denominator confounded by neither work volume nor correction mix: it counts the
    opportunities the artifact actually had to matter. It is not a cure — §16a's confound stands, the
    treatment changes what Nick has to correct and therefore changes the population being sampled —
    and no denominator fixes that.

    Both are now reported side by side, and `denominator_split` fires when they disagree in
    DIRECTION. A split is not a failure of the instrument; it is the instrument telling the truth
    about a comparison that does not have one answer.
    """
    n = _replies_in(start_iso, end_iso)
    if not n:
        return None
    return count / n * 1000.0


# How far the two windows' activity may differ before a rate comparison is refused.
CONFOUND_FACTOR = 3.0
_CONFOUND_CACHE = {}
# Why a comparison was refused must be visible; a silent refusal is its own fail-toward.
_CONFOUND_ERRORS = []


def _confounded(cur, obs_min, split, obs_max):
    """True when the pre and post windows differ so much in ACTIVITY that a per-calendar-day
    rate cannot be compared across them.

    Activity proxy = ALL corrections in this org in the window, not just this ask's. It is the
    closest stand-in available for exposure (how much Core actually talked) without counting
    turns out of the raw transcripts. Cached per split date: every artifact sharing a split
    asks the same question.

    Fail-CLOSED — a query error returns True (treat as CONFOUNDED) and the rate comparison is REFUSED.
    Corrected 2026-08-09: this line described the OLD polarity for as long as the new code
    existed, which is a doc asserting the opposite of the mechanism beside it — the exact class
    this session spent the day removing from steering files. A measurement guard
    that silently suppresses every verdict when it breaks would be the same silent-absence
    defect it exists to catch.
    """
    # ORG IS IN THE KEY. The cached value answers "are these two windows activity-skewed?", and the
    # query below reads pattern_observations WHERE org_id = the session GUC. So the answer depends on
    # obs_min/split/obs_max — all in the key — AND on the org, which was not. GEN is constant, so it
    # needs no slot; the activity counts are DERIVED from org+window, so keying on them would be
    # redundant and would defeat the cache's purpose.
    #
    # Correct today because one process holds one org for its lifetime. It stops being correct the
    # first time anything sets that GUC twice, or a caller loops orgs — which is exactly what T017
    # (each Core mines its OWN corpus) grows toward. Named by core-finance 2026-08-12; the shape is
    # GEN's, an assumption that holds until it quietly does not, and the fix is one tuple element.
    # Read from get_org_id(), NOT from the cursor. connect_corebrain() SETs the GUC from exactly this
    # value (_env.py:244), so they are the same number — and a `SELECT current_setting(...)` here
    # would consume a slot in core-finance's order-dependent fake cursor and break the probe that
    # caught this whole class. Fixing one seat's organ must not break another seat's instrument.
    # Divergence is possible only if something SETs the GUC directly without going through connect
    # (bin/tests/test_instruments_agree_on_corpus.py does, deliberately) — such callers must clear
    # this cache, and the tests that do already clear it per case.
    key = (get_org_id(), obs_min, split, obs_max)
    if key in _CONFOUND_CACHE:
        return _CONFOUND_CACHE[key]
    try:
        # EXPLICIT ORG PREDICATE. Every other query in this file carries one (see :186, :307, :340);
        # this one — the guard deciding whether a rate comparison is trustworthy AT ALL — did not.
        # core-business measured it on its own live connection:
        #
        #     as written        1082 rows    FLEET-WIDE
        #     with the clause    199 rows    business only
        #
        # The docstring above says "Activity proxy = ALL corrections IN THIS ORG in the window." It
        # was not: the confound guard measured every Core and applied the answer to one.
        #
        # AND THE REASON THE OMISSION SURVIVED IS THE PART TO REMEMBER. The header calls the
        # connection "(brain_app, RLS-scoped to CORE_ORG_ID)", so a reader — and the author — takes
        # scoping as ambient. business checked the policies: pattern_observations has
        # relrowsecurity=True, relforcerowsecurity=True and three policies, and the SELECT policy's
        # qual is literally `true` while the UPDATE policy IS org-scoped. RLS enabled, forced,
        # policies present, every box ticked, and reads unscoped — 30 of 31 org-partitioned tables
        # share that shape.
        #
        # Whether open cross-org READS are intended is a separate design question and NOT settled
        # here: several policies are named `read_all`, and the brain is documented as a shared vault
        # with org PARTITIONING. Partitioning is not isolation. What is unambiguous either way is
        # that a query whose docstring says "in this org" must say so in SQL rather than trusting an
        # ambient guarantee that does not hold for SELECT.
        ORG = "org_id = current_setting('app.current_org_id', true)::bigint"
        q = ("SELECT count(*) FROM pattern_observations WHERE " + ORG + " AND " + GEN +
             " AND COALESCE(session_date, created_at::date) {op} %s")
        cur.execute(q.format(op="<"), (split,))
        pre_all = cur.fetchone()[0]
        cur.execute(q.format(op=">="), (split,))
        post_all = cur.fetchone()[0]
        pre_d = max(1, (split - obs_min).days)
        post_d = max(1, (obs_max - split).days)
        pre_density = pre_all / pre_d
        post_density = post_all / post_d
        lo, hi = sorted((pre_density, post_density))
        res = bool(lo > 0 and hi / lo > CONFOUND_FACTOR) or bool(lo == 0 and hi > 0)
    except Exception as e:
        # FAIL CLOSED. This was `res = False`, and False means NOT-CONFOUNDED means PROCEED — so an
        # unreachable database, a query error, or a schema change silently WAIVED the only guard
        # standing between per_week's confounded denominator and a published verdict. core-business
        # probed it with fake cursors: DB unreachable -> False -> proceeds.
        #
        # The guard works when it can RUN (a 40x density skew is correctly refused). The defect is
        # entirely in what happens when it cannot, and the answer was the flattering one.
        #
        # True here means "treat as confounded", i.e. REFUSE the rate comparison. A measurement
        # that cannot verify its own precondition has not verified it.
        res = True
        _CONFOUND_ERRORS.append("%s: %s" % (type(e).__name__, e))
    _CONFOUND_CACHE[key] = res
    return res


def fire_counts():
    """short-contract-name -> fire count, from the TSV fire log.

    THE LOG HAS TWO FORMATS AND THIS READ COLUMN 1 UNCONDITIONALLY.

        old   timestamp \t hook \t CONTRACT \t prompt      contract in col 2
        new   hook \t CONTRACT \t prompt                    contract in col 1

    So on every old-format line it tallied the HOOK NAME as if it were a contract, and the real
    contract on that line was attributed to nothing. core-business found it: 378 of its 405 lines
    are old-format, so 93% of its records were parsed wrong. On life the split is 28 old / 84 new.

    WHAT IT COST, measured by core-business on its own seat: SEVEN of ten contracts reported as
    never-fired had fired — instruction-directive 0 -> 342, instruction-tooling 0 -> 120,
    instruction-preference 0 -> 52, recall-first 0 -> 20, verify-dont-claim 0 -> 15,
    plan-not-execute 0 -> 7, model-routing 0 -> 3. Exactly one genuinely never had.

    Both Cores built conclusions on those zeros. business's retirement pass proposed deleting eight
    contracts INCLUDING the four good hand-authored ones, purely on the strength of counts this
    function invented; with the fix it proposes one. And "contract_state binds 0%" — a number I
    repeated to Nick — was an artifact of this line.

    Detect by shape: a leading timestamp is digits. Anything else is the newer hook-first form.

    THE COLUMN IS OVERLOADED, which the two-format fix did not address (2026-08-12).

    Three guards write a RECORD KIND into the same column contract names live in:
    learned-stopguard.py:117 and learned-recallguard.py:122 write "shadow" for a near-fire, and
    "block" appears as a verdict. Both are deliberate telemetry, not corruption — but they are not
    contracts, and tallying them inflates any reported total. Measured on this seat: 122 tallies,
    of which 8 "shadow" + 3 "block" are record kinds and 1 is "test-induced-xyz", a fixture a test
    leaked into the LIVE log. The real contract-fire count is 110, not 122.

    Per-contract attribution was never affected — these tokens match no contract name, so the
    lookup at the call site skips them. What was affected is every statement of the form "N fire
    records", which is how the wrong number reached Nick.

    So: split the kinds rather than blanket-filtering. FIRE_RECORDS keeps the near-fire telemetry
    that the guards deliberately emit — it is the denominator those hooks exist to provide, and
    discarding it to make one total tidy would destroy real signal.
    """
    global FIRE_RECORDS, FIRE_UNATTRIBUTED
    c, records, unattributed = Counter(), Counter(), Counter()
    try:
        for line in FIRES_LOG.read_text().splitlines():
            cols = line.split("\t")
            if len(cols) >= 2:
                idx = 2 if cols[0][:4].isdigit() else 1
                if idx >= len(cols):
                    continue
                hook = cols[idx - 1].strip()          # the emitting hook, one column left
                for name in cols[idx].split(","):
                    name = name.strip()
                    if not name:
                        continue
                    if name in _FIRE_RECORD_KINDS:
                        records[name] += 1            # near-fire: real signal, not a fire
                    elif name == "block":
                        target = _BLOCK_HOOK_CONTRACT.get(hook)
                        if target:
                            c[target] += 1            # the contract ENFORCED — a real fire
                        else:
                            unattributed[hook] += 1   # visible, never guessed at
                    else:
                        c[name] += 1
    except FileNotFoundError:
        pass
    FIRE_RECORDS, FIRE_UNATTRIBUTED = records, unattributed
    if unattributed:
        print(f"[fitness] {sum(unattributed.values())} block row(s) from unmapped hook(s) "
              f"{dict(unattributed)} — these are REAL enforcements credited to no contract. "
              f"Add the hook to _BLOCK_HOOK_CONTRACT once its contract is established.",
              file=sys.stderr)
    return c


def _measure_si_artifacts(obs_min, obs_max):
    """Fitness for the LIVE spine (si_artifacts), alongside the legacy learned_contracts pass above.

    Same signal as a contract — did the ask stop recurring after the artifact shipped — because what
    is mined is an ASK, and recurrence-decay is payload-agnostic. What does NOT transfer is
    ATTRIBUTION, so NOT-BINDING is split by whether the artifact ever actually fired:

      NOT-BINDING-NO-FIRE  the trigger never matched. Mechanical, and safely autonomous to retune —
                           the artifact simply is not reaching the moment it was built for.
      NOT-BINDING-FIRED    it fired and the ask STILL recurs. That is a payload-quality or adherence
                           failure, and there is no oracle for "is this procedure any good", so it
                           surfaces to Nick via /core-si and is NEVER auto-rewritten. An autonomous
                           LLM-rewrites-its-own-payload loop has no ground truth to converge on.
    """
    import json as _json
    from datetime import date as _date, datetime as _dt
    out = []
    try:
        con = connect_corebrain()
        cur = con.cursor()
        ORG = "org_id = current_setting('app.current_org_id', true)::bigint"
        # `installed_at`, NOT `updated_at`. THIS SPLIT IS THE WHOLE MEASUREMENT.
        #
        # Fable found this 2026-08-05 and it invalidated every si_artifact verdict in the file.
        # `updated_at` is set to now() by si_project.upsert()'s ON CONFLICT clause, and the pipeline
        # re-installs every live artifact at every close — live revisions here run to 231, with 1,207
        # install_begin events in fourteen days. So splitting at `updated_at` reset the "after it
        # shipped" window to ~zero on every close, `post` came back 0.0 for everything, and the
        # verdict cascade below reads `post == 0 and fires > 0` as GRADUATED. The file therefore
        # reported success BY CONSTRUCTION: 6 GRADUATED, 5 GRADUATED-UNPROVEN, 0 NOT-BINDING among
        # artifacts, while 26 corrections in the last 28 days matched a live artifact's own ask and
        # only ONE of them happened to land after that artifact's churned timestamp.
        #
        # The earlier comment here justified `updated_at` on the grounds that the spec carries no
        # install stamp — true of the JSON spec, since _deep_redact strips leading-underscore engine
        # fields, but the TABLE has an `installed_at` column and it is genuinely immutable: the
        # upsert's ON CONFLICT sets only spec/event/active/prior_spec/revision/updated_at and never
        # touches it. Verified on live rows — artifacts at revision 230+ still carry
        # installed_at = 2026-07-27 against updated_at = 2026-08-05.
        #
        # So the fix is not new plumbing, it is reading the column that was already correct.
        cur.execute(f"SELECT artifact_id, spec, installed_at::date, COALESCE(quarantined, false) "
                    f"FROM si_artifacts WHERE active AND {ORG}")
        arts = cur.fetchall()
        # Which events actually dispatch, for the enforcement tier. None = unknown (never worst-label).
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from artifact_typer import dispatchable_events
            _disp = dispatchable_events()
        except Exception:
            _disp = None
        # Window EXPOSURE, for _apply_zero_floor: the dates of every observation the matcher above
        # could possibly have counted. Fetched once and bisected per artifact rather than queried
        # per artifact — the splits differ but the population does not, so N queries would return N
        # views of one list. Must use the SAME population as the matcher (the canonical_ask filter),
        # or the denominator would describe a corpus the numerator never searched.
        # DISTINCT on the SAME key the numerator dedups by. Counting raw rows here while the
        # numerator counts distinct (correction_text, day) understates the rate and so overstates
        # p — it errs toward refusing, which is the safe direction and therefore the one that would
        # have survived review unnoticed. A denominator must describe the same population the
        # numerator was drawn from, whichever way the error points.
        cur.execute("SELECT d FROM (SELECT DISTINCT correction_text, "
                    "COALESCE(session_date, created_at::date) AS d FROM pattern_observations "
                    "WHERE " + ORG + f" AND {GEN}"
                    " AND canonical_ask IS NOT NULL AND canonical_ask <> '') t ORDER BY d")
        obs_dates = [r[0] for r in cur.fetchall()]
        # fire counts per artifact, from the same action log the dispatcher writes
        fires = Counter()
        log = INSTANCE / ".claude" / "state" / "friction-action-log.jsonl"
        if log.is_file():
            for line in log.read_text(errors="ignore").splitlines():
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("action") in ("fire_inject", "fire_block"):
                    fires[r.get("artifact_id")] += 1
        for aid, spec, split, _quarantined in arts:
            if isinstance(spec, str):
                spec = _json.loads(spec)
            if str(aid).startswith("legacy_"):
                continue
            ask = (spec.get("effect", {}).get("message") or "")
            if not (ask and split):
                continue
            # recurrence of THIS ask, before vs after the artifact shipped
            q = ("SELECT count(DISTINCT (correction_text, COALESCE(session_date, created_at::date))) "
                 "FROM pattern_observations WHERE " + ORG + f" AND {GEN}" +
                 " AND canonical_ask IS NOT NULL AND canonical_ask <> ''"
                 " AND position(canonical_ask in %s) > 0"
                 " AND COALESCE(session_date, created_at::date) {op} %s")
            cur.execute(q.format(op="<"), (ask, split))
            pre = cur.fetchone()[0]
            cur.execute(q.format(op=">="), (ask, split))
            post = cur.fetchone()[0]
            pre_days = max(1, (split - obs_min).days)
            post_days = max(0, (obs_max - split).days)
            pre_rate, post_rate = per_week(pre, pre_days), per_week(post, max(1, post_days))
            # THE SECOND DENOMINATOR, ACTUALLY CALLED (2026-08-20, found by core-business within an
            # hour of me shipping it). `per_reply` was written, tested, verified on live data — and
            # called by NOTHING. Every fitness verdict on every seat still divided by calendar days.
            #
            # That is the eighth instance today of the exact defect this whole day was spent fixing:
            # built, documented, never wired. Seven of them were inherited. This one I authored, in
            # the fix for Nick's own condition that autonomy must be measurable.
            #
            # Reported ALONGSIDE per_week, never replacing it. business measured the first efficacy
            # comparison ever run on this loop's output flipping SIGN on denominator choice (-27% per
            # day, +71% per correction-moment) because work volume had halved in the window. A
            # verdict that depends on which denominator was computed first is not a verdict, and the
            # only honest presentation is both numbers side by side.
            pre_rr = per_reply(pre, str(obs_min)[:10], str(split)[:10])
            post_rr = per_reply(post, str(split)[:10])
            fc = fires.get(aid, 0)
            # THE CONFOUND GATE MUST BEAT THE SUCCESS BRANCHES (core-finance probe 5, 2026-08-12).
            #
            # `elif _confounded(...)` sits BELOW the success branches, so a confounded window that
            # produced GRADUATED or DECAYING exited before the gate could refuse it. finance's case
            # B is core-business's 07-23..07-28 sprint case MIRRORED: a contract installed just AFTER
            # a dense sprint gets an inflated pre-window and a sparse post-window, so its rate
            # collapses for reasons that have nothing to do with the contract. The dense-POST
            # direction landed on guarded branches; the sparse-post direction did not.
            #
            # A verdict that cannot separate "the contract worked" from "Nick worked less that
            # month" must be refused in BOTH directions.
            #
            # Done by GUARDING the success branches in place rather than relocating the block. An
            # earlier attempt moved the branch with a scripted edit and DUPLICATED 680 lines of this
            # file; ast.parse passed on the result. Guarding costs an extra _confounded() call only
            # on the paths where a success verdict would otherwise be emitted — exactly where the
            # check is needed — and cannot restructure the file.
            _conf = _confounded(cur, obs_min, split, obs_max)
            if post_days < MIN_POST_DAYS:
                verdict, why = "INSUFFICIENT", f"only {post_days}d post-install"
            elif not _conf and post == 0 and fc == 0:
                # The ask stopped AND the artifact never fired, so it cannot be the cause. Reporting
                # this as GRADUATED would credit the engine for silence it had no part in — the same
                # overclaim the NOT-BINDING split exists to prevent, in the flattering direction.
                verdict, why = "GRADUATED-UNPROVEN", (
                    "ask stopped recurring but artifact never fired — no evidence it was the cause")
            elif not _conf and post == 0:
                verdict, why = "GRADUATED", f"ask stopped recurring after firing {fc}x"
            elif not _conf and pre_rate > 0 and post_rate < pre_rate * DECAY:
                verdict, why = "DECAYING", (f"post {post_rate:.1f}/wk < {DECAY:.0%} of pre "
                                            f"{pre_rate:.1f}/wk"
                                            # BOTH DENOMINATORS IN THE SENTENCE A HUMAN READS, and
                                            # the disagreement named when they disagree. per_week
                                            # divides by calendar days, so it cannot see a window
                                            # where the WORK volume changed — the confound that made
                                            # the same comparison read -27% and +71% on one artifact.
                                            # per_reply divides by the opportunities the artifact
                                            # actually had. Neither is authoritative; a verdict that
                                            # depends on which was computed first is not a verdict.
                                            # `None` means the transcripts do not cover that window,
                                            # which is said rather than silently omitted.
                                            + _reply_rate_note(pre_rr, post_rr)
                                            # No caveat text and no baseline query when the row is
                                            # under the floor: _apply_power_floor is about to
                                            # replace this verdict AND this sentence, and it states
                                            # the count itself. Appending "[pre n=…]" here as well
                                            # was the first of three places one fact was written.
                                            + ("" if pre < MIN_PRE_N else
                                               _vs_baseline(cur, split, pre_rate, post_rate)))
            elif _conf:   # computed once above, ahead of the success branches
                # ACTIVITY CONFOUND GATE (2026-08-08, found by core-business).
                #
                # per_week divides by CALENDAR DAYS. Not replies, not turns. So an eighteen-hour
                # sprint day and a quiet day count the same, and a contract installed just before
                # a sprint gets a post-window several times denser than its pre-window out of
                # IDENTICAL behaviour. business measured it: 87 of its 159 corrections — 55% of
                # the entire corpus — fall inside six days (07-23..07-28), and every one of its
                # 7 NOT-BINDING verdicts belongs to a contract installed just before or during
                # that window. Detector drift was ruled out separately (one detector_version
                # across the whole range), so this is the remaining explanation.
                #
                # WHAT I DID NOT DO, and business should push back if it disagrees: business asked
                # for per-100-REPLIES to match si-objective.py. That denominator is not available
                # here. si-objective gets it from reply-observations.jsonl, which has ~12h of
                # coverage; these windows are WEEKS. Substituting a denominator I cannot compute
                # for the window I am measuring would be a second wrong number wearing the right
                # units. Counting assistant turns out of the raw JSONL transcripts is the real
                # fix and it is a separate build.
                #
                # Until then this REFUSES rather than reports. A verdict that cannot distinguish
                # "the contract failed" from "Nick worked three times as hard that week" is not a
                # weaker verdict, it is a different claim.
                verdict, why = "INSUFFICIENT-CONFOUNDED", (
                    f"post {post_rate:.1f}/wk vs pre {pre_rate:.1f}/wk, but the two windows differ "
                    f"in ACTIVITY by more than {CONFOUND_FACTOR}x — per_week divides by calendar "
                    f"days, so this is not comparable. Needs a per-reply denominator.")
            elif fc == 0:
                verdict, why = "NOT-BINDING-NO-FIRE", (
                    f"never fired but ask recurs {post_rate:.1f}/wk — trigger too narrow, safe to retune")
            else:
                verdict, why = "NOT-BINDING-FIRED", (
                    f"fired {fc}x and ask still recurs {post_rate:.1f}/wk — payload or adherence "
                    f"failure; no oracle for payload quality, surface to the operator, do NOT auto-rewrite")
            # pre_count/post_count EMITTED, not just the rates. The contract side already carried
            # them and the artifact side did not, so the MIN_PRE_N floor could not be applied here
            # even though the identical verdict logic runs — a rate of 0.1/wk says nothing about
            # whether it came from 1 observation or 40, and only the count decides interpretability.
            #
            # The floor is applied HERE, to the verdict, immediately before the row is emitted — so
            # no reader of this row can obtain an unfloored verdict by any path. See
            # _apply_power_floor for why the two earlier placements (rationale text, emitted-list
            # filter) both left the actual consumer unprotected.
            verdict, why = _apply_power_floor(verdict, why, pre)
            # ...and the same refusal on the other side of the split. Both floors sit at this one
            # point for the identical reason: the verdict field is what every consumer reads.
            verdict, why = _apply_zero_floor(
                verdict, why, pre, post,
                pre_n=_bisect.bisect_left(obs_dates, split),
                post_n=len(obs_dates) - _bisect.bisect_left(obs_dates, split))
            # A QUARANTINED ARTIFACT DID NOT FAIL TO FIRE — IT WAS NOT LOADED (2026-08-12).
            #
            # This pass selects `WHERE active` and si_project.project() builds the runtime file from
            # `active AND NOT quarantined`, so the two disagree by exactly the quarantined set.
            # Measured here: 3 of 23 DB-active artifacts are quarantined and therefore absent from
            # the dispatcher's active.json — art_7da015, art_bf5438, art_wf4e24 (the last being the
            # only hooked_skill on this seat, which is why `payload_mismatch` has been silent since
            # 2026-08-11 and why "8/8 payload_mismatch" cannot currently be re-measured at all).
            #
            # Graded as if live, they read fire_count=0 forever and the cascade calls that
            # GRADUATED-UNPROVEN — "the ask stopped but the artifact never fired, so no evidence it
            # was the cause." True in letter, and it attributes to TRIGGER QUALITY what is actually
            # a quarantine. That is the same misattribution the NOT-BINDING split exists to prevent,
            # and it would have bitten hardest on a quarantined artifact with a real pre-window,
            # which would have received a full comparative verdict computed on the premise that it
            # was running.
            #
            # NOT SILENTLY DROPPED. Excluding the rows would make a quarantined artifact
            # indistinguishable from one that never existed — the disappearance this file already
            # refuses elsewhere. It gets a verdict that says what happened, and joins the
            # non-comparative family so the power floor cannot overwrite it: quarantine is a fact
            # about deployment, true at any sample size, exactly like UNENFORCEABLE.
            if _quarantined:
                verdict, why = "QUARANTINED", (
                    "not loaded by the dispatcher — si_project.project() builds active.json from "
                    "`active AND NOT quarantined`, so this artifact cannot fire and its fire_count "
                    "of 0 says nothing about its trigger. Re-arm it before reading any verdict here."
                    f" (would otherwise have read: {verdict})")
            out.append({"artifact_id": aid, "type": spec.get("type", "contract"),
                        "quarantined": bool(_quarantined),
                        "pre_count": pre, "post_count": post,
                        "pre_rate_per_wk": round(pre_rate, 1), "post_rate_per_wk": round(post_rate, 1),
                        "fire_count": fc, "verdict": verdict, "rationale": why,
                        "enforcement": classify_artifact_enforcement(spec, bool(_quarantined), _disp)})
        con.close()
    except Exception as e:  # fail-open: fitness is observability, never a blocker
        return [{"error": str(e)[:200]}]
    return out


def main(dry_run=False):
    con = connect_corebrain()
    cur = con.cursor()
    # Org scoping (2026-07-19): learned_contracts has RLS DISABLED (unlike
    # pattern_observations), and the pattern_observations SELECT policy is USING(true)
    # — i.e. READS are cross-org by design (peer-Core recall). So neither table
    # self-isolates on read. Without an explicit org filter this organ vacuumed up all
    # 5 Cores' contracts (life saw 30 rows / 6 real contracts → core-si showed each 5×)
    # AND counted every Core's corrections as life's recurrences (false NOT-BINDING).
    # Filter explicitly on the running Core's org via the session GUC connect_corebrain SET.
    ORG = "org_id = current_setting('app.current_org_id', true)::bigint"
    cur.execute(f"SELECT situation, trigger_labels, created_at::date, required_shape, checkable "
                f"FROM learned_contracts WHERE active AND {ORG} ORDER BY created_at")
    contracts = cur.fetchall()
    _live_hooks = _registered_hook_names()
    cur.execute(f"SELECT min(COALESCE(session_date, created_at::date)), max(COALESCE(session_date, created_at::date)) FROM pattern_observations WHERE {ORG} AND {GEN}")
    obs_min, obs_max = cur.fetchone()
    fires = fire_counts()
    gates = _gates()
    unenforceable = _unenforceable()

    rows = []
    print(f"\n{'contract':<26} {'pre/wk':>7} {'post/wk':>8} {'fires':>6}  verdict")
    print("-" * 70)
    for situation, labels, created, _req_shape, _checkable in contracts:
        short = situation.split("—")[0].split("-")[0].strip() if "—" not in situation else situation.split("—")[0].strip()
        # Fix 1 (2026-06-18) — when a gate exists, split the pre/post window at the
        # GATE's since-date, not the contract's creation. Else pre-gate misses get
        # counted as gate failures (the model-routing NOT-BINDING measurement artifact).
        gate = gates.get(short)
        split = created
        if gate:
            try:
                from datetime import date as _date
                split = _date.fromisoformat(gate["since"])
            except Exception:
                split = created
        # Fix 0 (contract-binding proposal, 2026-06-09) — recurrence counts ONLY
        # real, distinct Nick corrections, anchored to when they HAPPENED:
        #   · window splits on session_date (when said), not created_at (when
        #     mined) — batch re-mining was dumping old corrections into "post"
        #   · hook feedback / tool-result blobs excluded (mined-as-Nick pollution)
        #   · DISTINCT (text, day) — re-mined duplicates counted once
        # Split in two so the EXPOSURE denominator can reuse the cleaning without the label
        # predicate. _apply_zero_floor needs "how many corrections happened at all in this window",
        # and it must be the same cleaned corpus the numerator was counted from — otherwise the
        # machine rows and compaction preambles excluded below would inflate the denominator only,
        # making every zero look more significant than it is.
        _CLEAN_OBS = """
            org_id = current_setting('app.current_org_id', true)::bigint
            AND COALESCE(session_date, created_at::date) {op} %s
            AND correction_text IS NOT NULL
            -- 2026-08-12: machine rows carry an EXPLICIT marker now, not the '' sentinel.
            -- 185 fleet-wide; 4 of them had been mined into REAL asks, 3 into life's top
            -- cluster. Excluding by column rather than by text shape, so a new harness string
            -- does not silently reopen it.
            AND excluded_reason IS NULL
            AND correction_text NOT ILIKE '<details>%%'
            AND correction_text NOT ILIKE 'Stop hook feedback%%'
            AND correction_text NOT ILIKE '%%Tool result%%'
            AND correction_text NOT ILIKE '⛔%%'
            -- 2026-08-12: the compaction preamble was being counted as one of Nick's corrections.
            -- It is machine boilerplate the harness injects when a session runs out of context, so
            -- it lands in the user role and every prior text-shape exclusion above missed it. It
            -- inflates whichever side of the split it falls on; found while checking why a real
            -- cluster reported DECAYING. Same class as the 41.6 percent non-Nick corpus
            -- contamination: machine text in the user role, read as the user.
            -- (NO BARE PERCENT SIGN IN THIS COMMENT. psycopg2 parses the whole string for
            --  placeholders before SQL ever sees a comment marker, so a bare percent here raised
            --  IndexError: tuple index out of range. The note explaining a contamination fix
            --  contaminated the query. Escape as %% or spell the word.)
            AND correction_text NOT ILIKE 'This session is being continued from a previous conversation%%'
            AND correction_text NOT ILIKE '%%[SYSTEM NOTIFICATION - NOT USER INPUT]%%'
            AND correction_text NOT ILIKE '<local-command-caveat>%%'
        """
        OBS_FILTER = "pattern_label = ANY(%s) AND " + _CLEAN_OBS
        q = ("SELECT count(DISTINCT (correction_text, COALESCE(session_date, created_at::date))) "
             "FROM pattern_observations WHERE " + OBS_FILTER + f" AND {GEN}")
        cur.execute(q.format(op="<"), (labels, split))
        pre = cur.fetchone()[0]
        cur.execute(q.format(op=">="), (labels, split))
        post = cur.fetchone()[0]
        qx = ("SELECT count(DISTINCT (correction_text, COALESCE(session_date, created_at::date))) "
              "FROM pattern_observations WHERE " + _CLEAN_OBS + f" AND {GEN}")
        cur.execute(qx.format(op="<"), (split,))
        pre_n = cur.fetchone()[0]
        cur.execute(qx.format(op=">="), (split,))
        post_n = cur.fetchone()[0]
        pre_days = max(1, (split - obs_min).days)
        post_days = max(0, (obs_max - split).days)
        pre_rate, post_rate = per_week(pre, pre_days), per_week(post, max(1, post_days))
        fc = fires.get(short, 0)

        # Same ordering fix as the si_artifacts path above — see the long note there. The gate ran
        # BELOW these branches, so a confounded window that produced GRADUATED or DECAYING exited
        # before it. Guarded in place rather than relocated, for the reason recorded above.
        _conf = _confounded(cur, obs_min, split, obs_max)
        if post_days < MIN_POST_DAYS:
            verdict, why = "INSUFFICIENT", f"only {post_days}d post-ship"
        elif not _conf and post == 0:
            verdict, why = "GRADUATED", "recurrence stopped"
        elif not _conf and pre_rate > 0 and post_rate < pre_rate * DECAY:
            verdict, why = "DECAYING", (f"post {post_rate:.1f}/wk < {DECAY:.0%} of pre "
                                            f"{pre_rate:.1f}/wk"
                                            # No caveat text and no baseline query when the row is
                                            # under the floor: _apply_power_floor is about to
                                            # replace this verdict AND this sentence, and it states
                                            # the count itself. Appending "[pre n=…]" here as well
                                            # was the first of three places one fact was written.
                                            + ("" if pre < MIN_PRE_N else
                                               _vs_baseline(cur, split, pre_rate, post_rate)))
        else:
            days_gated = None
            if gate:
                try:
                    from datetime import date as _date
                    days_gated = (obs_max - _date.fromisoformat(gate["since"])).days
                except Exception:
                    days_gated = None
            if gate and days_gated is not None and days_gated < GATE_WATCH_DAYS:
                verdict, why = "GATED-WATCH", (
                    f"recurs {post_rate:.1f}/wk but gate LIVE {days_gated}d ago ({gate.get('gate','?')}) — "
                    f"re-alarms if no decay by day {GATE_WATCH_DAYS}")
            elif _conf:   # computed once above, ahead of the success branches
                # Same activity gate as the artifact branch above. It belongs on BOTH: I added it
                # only to the artifact path first, and core-business's 7 NOT-BINDING verdicts are
                # legacy CONTRACTS, so the fix missed the exact case that prompted it.
                verdict, why = "INSUFFICIENT-CONFOUNDED", (
                    f"post {post_rate:.1f}/wk vs pre {pre_rate:.1f}/wk across windows whose ACTIVITY "
                    f"differs by more than {CONFOUND_FACTOR}x — per_week divides by calendar days, "
                    f"so the comparison is not valid. Needs a per-reply denominator.")
            elif gate:
                verdict, why = "NOT-BINDING", (
                    f"gate ({gate.get('gate','?')}) live {days_gated}d but correction STILL recurs "
                    f"{post_rate:.1f}/wk (pre {pre_rate:.1f}/wk) — gate insufficient, escalate design")
            else:
                verdict, why = "NOT-BINDING", (f"fires {fc}x but correction recurs "
                                               f"{post_rate:.1f}/wk (pre {pre_rate:.1f}/wk) — "
                                               f"structural escalation")
            # The caveat that used to be appended right here is gone: _apply_power_floor now
            # replaces the whole verdict below. Keeping both would leave a NOT-BINDING row whose
            # RATIONALE says "too few to interpret" while its VERDICT still says NOT-BINDING — the
            # exact split (guard in the prose, decision in the label) that let one uninterpretable
            # row drive 111 flags.

        if verdict == "NOT-BINDING" and short in unenforceable:
            verdict, why = "UNENFORCEABLE", (
                "linguistic-signal category error (cluster-by-tone) — no deterministic gate "
                "possible; tracked as a known model-behavior limit, not an actionable fix")
        # AFTER the unenforceable override, deliberately. Unenforceability is a claim about what a
        # gate could ever detect, which is true at any sample size — it is not a rate comparison, so
        # a thin pre-window does not weaken it. The floor's own _NON_COMPARATIVE guard would preserve
        # it either way; ordering it here makes that independent of the guard list.
        verdict, why = _apply_power_floor(verdict, why, pre)
        verdict, why = _apply_zero_floor(verdict, why, pre, post, pre_n, post_n)
        print(f"{short:<26} {pre_rate:>7.1f} {post_rate:>8.1f} {fc:>6}  {verdict} — {why}")
        rows.append({
            "contract": short, "situation": situation, "trigger_labels": list(labels),
            "pre_count": pre, "post_count": post,
            "pre_rate_per_wk": round(pre_rate, 1), "post_rate_per_wk": round(post_rate, 1),
            "fire_count": fc, "verdict": verdict, "rationale": why,
            "enforcement": classify_contract_enforcement(short, _req_shape, _checkable, _live_hooks),
        })
    con.close()

    art_rows = _measure_si_artifacts(obs_min, obs_max)

    # THE FLOOR MUST APPLY TO THE MACHINE-READABLE SET, NOT ONLY TO THE SENTENCE (2026-08-12).
    #
    # The verdict string carries "[pre n=%d, TOO FEW to interpret]" when pre < MIN_PRE_N (see :682).
    # This list filtered on `verdict == "NOT-BINDING"`, which is true regardless of the caveat — so
    # the guard lived in `why` (prose a human reads) while the consumer read `verdict` (a label a
    # machine reads). Same defect the comment forty lines up already records once: "Missed on the
    # first pass because the floor was put only in the DECAYING branch while the comment claimed it
    # covered both ends." It covered one more end than it claimed, and not this one.
    #
    # MEASURED COST: plan-not-execute has pre_count=0, post_count=3 — the measurement's own text
    # calls it uninterpretable — and it entered this list anyway. friction_loop.classify_artifact_
    # health then read the list and emitted tune_flag_needs_oracle for it 111 TIMES across ~110 loop
    # runs. A verdict the instrument declares unreadable drove 111 downstream actions.
    #
    # Excluded rows are PRINTED, never silently dropped: a contract vanishing from a list with no
    # explanation is how the fire_count:0 -> GRADUATED defect read as health.
    # SUPERSEDED, 2026-08-12 (same day): the `pre_count >= MIN_PRE_N` filter that used to be part of
    # these two comprehensions is gone, because it was the WRONG LAYER — see _apply_power_floor.
    # It protected this module's two emitted lists while `friction_loop` re-derived two more sets
    # from the raw verdicts and bypassed it entirely. An under-powered row can no longer carry a
    # NOT-BINDING verdict at all, so a plain verdict match is now sufficient here and everywhere.
    # The reporting stays: an excluded row is always named, never silently dropped.
    _underpowered = [r["contract"] for r in rows
                     if r["verdict"] == "INSUFFICIENT-UNDERPOWERED"]
    not_binding = [r["contract"] for r in rows if r["verdict"] == "NOT-BINDING"]
    if _underpowered:
        print(f"\n  {len(_underpowered)} contract verdict(s) downgraded to INSUFFICIENT-UNDERPOWERED "
              f"— pre-window under MIN_PRE_N={MIN_PRE_N}, so there is nothing for the post rate to "
              f"be measured against: {', '.join(_underpowered)}")

    # THE ARTIFACT SIDE OF THE SAME VERDICT, WHICH WAS MEASURED AND THEN DROPPED ON THE FLOOR.
    #
    # `not_binding` above holds legacy learned_contract NAMES ("plan-not-execute", "stop-and-plan").
    # `_measure_si_artifacts()` independently emits NOT-BINDING-FIRED / NOT-BINDING-NO-FIRE verdicts
    # for the artifacts the loop itself built — and nothing collected them, so nothing could act on
    # them. friction_loop.tune_pass() read only `not_binding` and matched it by substring against
    # artifact_id / case_id, but those namespaces never overlap (`art_…` / `ask_…` hashes vs.
    # human-authored contract slugs), so the flag_rederive path could not fire for anything the loop
    # generated. Latent rather than live at the time of writing — all 14 artifacts currently read
    # GRADUATED / GRADUATED-UNPROVEN / INSUFFICIENT — which is exactly why it needed finding before a
    # real NOT-BINDING appeared and was silently ignored.
    #
    # Kept as a SEPARATE key rather than appended to `not_binding`: consumers match that list by
    # substring, and mixing opaque id hashes into a substring-matched list of slugs is how you get an
    # accidental match later. Artifact ids are matched exactly by the consumer.
    #
    # Both NOT-BINDING-* variants belong here. They are different diagnoses of the same failure —
    # FIRED means it fires and the correction recurs anyway; NO-FIRE means its trigger never matched
    # while the correction recurred — and both call for re-derivation, never for narrowing.
    # THE SAME FLOOR AS THE CONTRACT SIDE, and it was missing here for the same reason it was missing
    # there: the caveat lives in the rationale and the filter reads the verdict. Fixed on the contract
    # list first; this is the sibling path, found by asking whether the identical defect existed in
    # the identical code twenty lines away. It did.
    #
    # An artifact whose pre-window is under MIN_PRE_N has no baseline for "still recurring" to mean
    # anything against, exactly as for a contract. Excluding it here removes it from the set
    # friction_loop.classify_artifact_health acts on — which is what turned one uninterpretable row
    # into 108 repeated tune_flag_needs_oracle events on art_97b6fff21bdf97478d45.
    # Same relocation as the contract side above: the floor is now in the verdict, so a
    # `startswith("NOT-BINDING")` match cannot admit an under-powered artifact by any path —
    # including friction_loop's own re-derivation at :1149, which this filter never governed.
    _under_art = [r["artifact_id"] for r in art_rows
                  if r.get("verdict") == "INSUFFICIENT-UNDERPOWERED" and r.get("artifact_id")]
    not_binding_artifacts = sorted(
        r["artifact_id"] for r in art_rows
        if str(r.get("verdict", "")).startswith("NOT-BINDING") and r.get("artifact_id"))
    if _under_art:
        print(f"  {len(_under_art)} artifact verdict(s) downgraded to INSUFFICIENT-UNDERPOWERED — "
              f"pre-window under MIN_PRE_N={MIN_PRE_N}: {', '.join(_under_art)}")
    # ENFORCEMENT SUMMARY — the honest split, per spine. Not a health metric to optimize (a low
    # `enforces` count may be exactly right — most asks have no oracle); it exists so the estate
    # can never AGAIN read as "42 contracts installed" when 7 are parked placeholders and the
    # blocking work is done by hooks the ledger did not credit. `claims-enforce-dead-event` > 0 is
    # the one state that is a defect at any count.
    def _tally(rs):
        c = Counter((r.get("enforcement") or {}).get("tier", "unknown") for r in rs if "error" not in r)
        return dict(sorted(c.items()))
    enforcement_summary = {
        "contracts": _tally(rows),
        "si_artifacts": _tally(art_rows),
        "note": ("tiers derived from settings.json registration + artifact spec, never from the "
                 "checkable column (no code reads it); 'advises' is the honest default"),
    }
    print("\nenforcement (derived, not claimed):")
    print(f"  contracts    {enforcement_summary['contracts']}")
    print(f"  si_artifacts {enforcement_summary['si_artifacts']}")
    payload = {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "obs_window": [str(obs_min), str(obs_max)],
        "not_binding": not_binding,
        "not_binding_artifacts": not_binding_artifacts,
        "enforcement_summary": enforcement_summary,
        "contracts": rows,
        "si_artifacts": art_rows,
    }
    if dry_run:
        print("\n(--dry-run — no file written)")
    else:
        OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUT_FILE.write_text(json.dumps(payload, indent=2))
        print(f"\nWROTE {OUT_FILE.relative_to(INSTANCE)} — {len(not_binding)} NOT-BINDING contract(s)"
              + (f": {', '.join(not_binding)}" if not_binding else "")
              + f"; {len(not_binding_artifacts)} NOT-BINDING artifact(s)"
              + (f": {', '.join(not_binding_artifacts)}" if not_binding_artifacts else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
