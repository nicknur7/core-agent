#!/usr/bin/env python3
"""friction_watchdog.py — P3 out-of-band safety. Runs at SessionStart / close / after a fire.
Scans the action log + active snapshot for misbehavior and QUARANTINES (rolls back) an artifact
that: threw a dispatch_error, exceeded a block budget, or whose short lease expired without a
healthy renewal. Fail-safe: any doubt → quarantine (fewer artifacts, never more). No human gate.

  CORE_ORG_ID=1 python3 friction_watchdog.py [--check]
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scheduling" / "brain-pg"))
import friction_installer as inst

STATE = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[1]) / ".claude" / "state"
ACTION_LOG = STATE / "friction-action-log.jsonl"

ERROR_ACTIONS = {"dispatch_error"}
BLOCK_BUDGET_SESSION = 2       # a blocker firing >2x in a session is quarantined
LEASE_MAX_AGE = 24 * 3600      # blockers auto-deactivate after 24h without renewal


def _recent_actions(limit=2000) -> list[dict]:
    try:
        lines = ACTION_LOG.read_text().splitlines()[-limit:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _sweep_payloadless_artifacts(arts: dict, dry: bool) -> list:
    """Quarantine live `hooked_skill` artifacts whose payload cannot be verified.

    THE ASYMMETRY THIS CLOSES (2026-08-12, Phase 4). _sweep_orphan_payloads below handles a payload
    with no artifact: swept, logged, retired. The INVERSE — an artifact with no payload — had no
    handler at all, so it stayed live and failed verification silently on every single fire.

    Found by measurement, not by reading. friction_dispatch:522 logs `payload_mismatch` and
    `continue`s, and the action log carries 8 of them, ALL for one artifact:

        art_wf4e24d222a3d9b9a7   type=hooked_skill
        payload declares art_wf4e24d222a3d9b9a7.md, 878 bytes, sha256 65640d13...
        procedures/ is EMPTY — 0 files

    So the dispatcher was correctly refusing to inject unverifiable content (fail-closed, as
    designed) and correctly logging it — and nothing consumed the log. That is the master plan's
    "matched but did not fire" state: it IS recorded, and it was recorded into a void.

    The plan's framing of this item — "the hooked_skill payload-hash bug (8/8 payload_mismatch)" —
    reads as eight skills failing. It is one skill failing eight times. Different problem, and the
    real one is the missing sweep rather than a hash computation.

    QUARANTINE RATHER THAN DELETE, and rather than regenerate: a payload that vanished may have been
    retired deliberately by a rollback that then failed to deactivate its artifact. Regenerating body
    text the reviewer never approved would be worse than leaving it inert. Quarantine records the
    reason, removes it from the projection, and is reversible — and since T024 the flag survives a
    re-install of an identical spec, so this cannot silently un-quarantine itself.

    Fail-open: never let this block a sweep.
    """
    broken = []
    try:
        import friction_dispatch as _fd
    except Exception:
        return broken
    for aid, art in list(arts.items()):
        if art.get("type") != "hooked_skill":
            continue
        try:
            if _fd._payload_verified(art):
                continue
        except Exception:
            continue                      # cannot judge it -> do not act on it
        reason = ("payload unverifiable at sweep time: the artifact is live and its procedure file "
                  "is missing or does not match its pinned hash, so every fire logs payload_mismatch "
                  "and injects nothing")
        broken.append((aid, reason))
        if not dry:
            inst._log("payloadless_artifact", artifact_id=aid, reason=reason)
            inst.rollback(aid, reason=reason)
    return broken


def _sweep_orphan_payloads(arts: dict, dry: bool) -> list:
    """Retire procedure payload files with no active artifact pointing at them.

    Rollback retires a payload as part of the same operation, so an orphan means something skipped
    that path — a crash between write and install, or a spec removed out-of-band. An orphan is inert
    (nothing references it; there is no discovery surface), but leaving it means the procedures dir
    stops being an accurate picture of what is live, and the next artifact reusing that id would
    silently inherit a stale body. Fail-open: never let this block a sweep."""
    orphans = []
    try:
        live = {a["artifact_id"] for a in arts.values()
                if a.get("type") == "hooked_skill"}
        candidates = []
        for p in inst.PROCDIR.glob("*.md"):
            if p.stem in live:
                continue
            # DO NOT DESTROY ON THE STRENGTH OF THE CACHE (2026-08-12).
            #
            # `arts` is active.json. si_project's own header states the rule this violated:
            # "Postgres si_artifacts is the source of truth ... active.json must never be both
            # truth and cache." Absence from the projection is not absence from the system, and
            # this sweep took an IRREVERSIBLE action on that inference.
            #
            # MEASURED, and it is the whole of the plan's "hooked_skill payload-hash bug":
            #
            #   08-10 22:34   this sweep retired art_wf4e24d222a3d9b9a7's payload as an orphan
            #   08-11 12:13   the artifact — still live — matched and could not verify its payload
            #         ...     seven more times, through 17:41
            #   08-12 12:37   the payloadless sweep finally caught it and quarantined the artifact
            #
            # 38 hours live and structurally unable to fire. The payload was never corrupt: the
            # quarantined copy still hashes to its pinned sha256, byte-for-byte, 878/878. There was
            # no hash bug, no path divergence, and no projection field loss — this sweep deleted a
            # working artifact's body because a rebuild of the cache had not yet listed it.
            #
            # So the canonical store decides. An id the DB still holds as active is NOT an orphan,
            # whatever the projection currently says.
            candidates.append(p.stem)

        if not candidates:
            return orphans

        # ONE QUERY FOR THE WHOLE SWEEP, not one per file (core-finance's counter 2). The first
        # version called connect_corebrain() inside this loop, so N payloads meant N connections and
        # each one independently converted a transient failure into a silent "not an orphan". With a
        # per-connection failure probability q the sweep is at least partly blind with probability
        # 1-(1-q)^N — reliability degrading with N, and N grows precisely because failures leave
        # payloads un-retired. That feedback loop is the part worth removing; the batched form also
        # gives the failure ONE place to be reported, which is what makes the logging below possible.
        canonical, checked = _canonical_orphans(candidates)
        if not checked:
            # THE NO-OP ANNOUNCES ITSELF (core-finance's counter 1). Returning [] silently made
            # "swept, nothing orphaned" and "could not sweep at all" the same result — and :342
            # reports that list as `orphan_payloads`, so a permanently-inert sweep on a flaky DB
            # reads as clean.
            #
            # This is the shape I fixed in friction_test_gate hours earlier — cannot-tell recorded
            # as a verdict — and then committed here while fixing it there. Same three-line
            # treatment: name the state, and say what it was unable to check.
            if not dry:
                inst._log("orphan_sweep_undecidable", candidates=len(candidates),
                          reason="the canonical store could not be reached, so no payload can be "
                                 "proven orphaned. Nothing retired — 'cannot check' must not "
                                 "authorise an irreversible delete.")
            return orphans

        for stem in candidates:
            if stem not in canonical:
                continue
            orphans.append(stem)
            if not dry:
                inst._log("orphan_payload", artifact_id=stem)
                inst._retire_payload(stem)
    except Exception:
        pass
    return orphans


def _canonical_orphans(aids: list) -> tuple:
    """(set of ids the CANONICAL store has no live artifact for, whether the check actually ran).

    Returns `(orphans, True)` on a successful query and `(set(), False)` when it could not run.
    The second element is the whole point: a caller must be able to tell an empty result from an
    unanswered question, which is the distinction this module kept losing.

    FAILS CLOSED. On any error the orphan set is EMPTY, so nothing is retired. That inverts this
    module's usual fail-open posture deliberately: everywhere else a failed sweep means work is
    skipped, whereas here it would mean a live artifact's body is destroyed. An orphan left in place
    is inert — nothing references it, there is no discovery surface. A wrongly-retired payload
    silently breaks a working rule for as long as nobody looks, which was 38 hours the one time it
    happened.

    One connection, one round trip, bound parameters, org-scoped.
    """
    try:
        import sys as _s
        from pathlib import Path as _P
        _s.path.insert(0, str(_P(__file__).resolve().parent))
        from _env import connect_corebrain, get_org_id
        conn = connect_corebrain()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT artifact_id FROM si_artifacts "
                "WHERE org_id = %s AND artifact_id = ANY(%s) "
                "AND active AND NOT COALESCE(quarantined, false)",
                (get_org_id(), list(aids)))
            live = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        return ({a for a in aids if a not in live}, True)
    except Exception:
        return (set(), False)


def _ctx_for(example, event: str) -> dict:
    """Build an evaluation context that puts a stored example in the channel it CAME FROM.

    Both call sites used to do {"prompt_text": text, "assistant_text": text} — the same string
    in both channels. That let an assistant_regex clause match a string captured as a prompt,
    which manufactures wrong-fire evidence the artifact never produced, and lets a positive
    satisfy the invariant through a channel it was never seen in. (core-business, finding 7.)

    Accepts the legacy bare-string shape too, and in that case marks the channel UNKNOWN so
    callers can refuse rather than guess — an example whose provenance is unknown is not
    evidence.
    """
    if isinstance(example, dict):
        text, channel = example.get("text") or "", example.get("channel") or "unknown"
    else:
        text, channel = (example or ""), "unknown"
    ctx = {"event": event, "prompt_text": "", "assistant_text": "",
           "tool_name": None, "session_id": "tune-eval"}
    if channel == "prompt":
        ctx["prompt_text"] = text
    elif channel == "assistant":
        ctx["assistant_text"] = text
    return ctx, channel, text

def _has_wrong_fire_evidence(art: dict) -> bool:
    """Is there evidence this rule fired where it should NOT have?

    Firing often is not firing wrongly. The action log records that a block fired, never that
    the fire was a mistake — so "over budget" alone cannot distinguish a rule that is too wide
    from a rule that is exactly right about something Nick did repeatedly.

    The one signal available today: the rule now matches its OWN recorded negatives, examples
    that were used to justify installing it precisely because it must not fire on them. That
    is unambiguous — it is the rule contradicting its own gate.

    Returns False when there is no such evidence, which sends the artifact to quarantine
    exactly as before. Fail-closed into the prior behaviour.
    """
    try:
        import friction_dispatch as _fd
        # Evidence comes from the evidence store, not the spec. It used to live in
        # spec["tests"], which _validate_spec enforces as a closed {positive_ids, negative_ids}
        # set — so storing it there put the persisted spec out of schema. (finding 8.)
        evidence = inst.read_evidence(art.get("artifact_id") or "")
        negatives = [t for t in (evidence.get("negative_texts") or []) if t]
        if not negatives:
            return False
        cond = art.get("condition") or {}
        event = art.get("event") or "UserPromptSubmit"
        for n in negatives:
            ctx, channel, text = _ctx_for(n, event)
            if channel == "unknown" or not text:
                continue        # provenance unknown -> not evidence, never guess the channel
            if _fd.evaluate(cond, ctx, trusted=False):
                return True     # fires on something it was gated to reject, in its own channel
        return False
    except Exception:
        return False

def _try_narrow(aid: str, art: dict):
    """Attempt one evidence-based narrowing. Returns a note on success, None to fall through
    to quarantine.

    Fail-CLOSED into quarantine on any doubt: no positives recorded, no separating term, the
    invariant refused, or anything raising. The pre-existing behaviour was always to
    quarantine, so every failure path here lands exactly where the system already was — this
    can only ever ADD a save, never remove a protection.
    """
    try:
        import friction_tune, si_project
        ev = inst.read_evidence(art.get("artifact_id") or "")
        # Written at install time into the evidence store (friction_installer._write_evidence).
        # Artifacts installed BEFORE that exist with no evidence at all, so they are not tunable
        # and correctly fall through to the pre-existing quarantine behaviour — no regression.
        positives = [t for t in (ev.get("positive_texts") or []) if t]
        # The over-fired text is what the rule caught that it should not have. Nothing records
        # that yet, so narrowing currently works from the positives alone: find a term common
        # to every positive and add it as a conjunct. That still strictly narrows, and the
        # invariant still guarantees the positives survive. Passing [] here is honest — an
        # empty over-fired set simply means no term is excluded on that basis.
        overfired = [t for t in (ev.get("negative_texts") or []) if t]
        if not positives:
            return None
        # USE THE PRODUCTION MATCHER. The first version of this was a local lambda that
        # checked only prompt_regex clauses and silently ignored event_is, tool_name_in,
        # tool_mutability_is, assistant_regex and state_flag_is.
        #
        # That made the invariant VACUOUS in the dangerous direction: a narrowing would be
        # verified against a matcher strictly weaker than the dispatcher, so a positive could
        # "still pass" here and fail in production — which is precisely the outcome the
        # invariant exists to prevent. Verifying a safety property with a different evaluator
        # than the one that runs is not verification. (core-business hostile read, finding 3.)
        import friction_dispatch as _fd

        def _eval(spec, example):
            # ONE context builder, shared with _has_wrong_fire_evidence. This function used to
            # build its own and put the same value in BOTH channels — the finding-7a defect.
            #
            # The 7a fix was written and it did NOT apply here. The edit anchored on the two
            # lines around the ctx literal, an intervening comment block meant the anchor never
            # matched, and str.replace with no match is a SILENT no-op. So _has_wrong_fire_evidence
            # got the fix, this did not, and the channel-isolation test passed because it only
            # exercised the former. Found 2026-07-28 by running the tune path end to end for the
            # first time — it had never executed, so the survivor was never reached.
            #
            # KEYS ALSO MATTER: friction_dispatch reads ctx["prompt_text"], not ctx["prompt"].
            # An earlier version passed "prompt", so every regex evaluated against "" and the
            # invariant refused every narrowing. _ctx_for owns both concerns now.
            ctx, channel, text = _ctx_for(example, spec.get("event") or "UserPromptSubmit")
            if channel == "unknown" or not text:
                return False   # provenance unknown -> cannot verify -> refuse
            return bool(_fd.evaluate(spec.get("condition") or {}, ctx, trusted=False))
        # The narrowing count lives in the EVIDENCE STORE, not in the spec. It used to be a
        # top-level "_tune" key, which _validate_spec rejects — so the first successful narrowing
        # would have written a permanently un-reinstallable artifact, silently. (finding 8.)
        prior = int(ev.get("narrow_count") or 0)
        r = friction_tune.tune(art, positives, overfired, _eval, prior=prior)
        if not r.get("ok"):
            # LOG the untunable case — a silent skip stays invisible forever, and nobody
            # notices one rule quietly never being considered. (core-business, finding 7b.)
            #
            # TWO ACTIONS, NOT ONE. Every enforcing block is untunable by construction (see
            # the boundary in friction_tune), so a single label would fire on every block
            # forever and become noise that trains the reader to skip it — the exact failure
            # we just removed from estate-sweep. tune_boundary is expected and quiet;
            # tune_untunable means something genuinely surprising and is worth a look.
            if r.get("untunable"):
                inst._log("tune_boundary" if r.get("expected") else "tune_untunable",
                          artifact_id=aid, reason=r["reason"])
            return None
        from _env import get_org_id  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
        org = get_org_id()
        si_project.upsert(org, r["spec"])   # stores prior_spec + bumps revision = reversible
        si_project.project(org)
        # Record the narrowing OUTSIDE the spec, so the churn cap is enforceable on the next
        # sweep without putting bookkeeping into a validated object. si_artifacts also versions
        # every write via prior_spec + revision, so the durable history is there regardless.
        hist = list(ev.get("narrow_history") or [])
        if r.get("term"):
            hist.append(r["term"])
        inst._write_evidence(aid, {**ev, "narrow_count": prior + 1, "narrow_history": hist}, org)
        return r["reason"]
    except Exception as e:
        # FAIL-CLOSED BUT NOT SILENT. This blanket except is correct for safety — any doubt
        # sends the artifact to quarantine, exactly where it went before tuning existed — but
        # it was also swallowing PROGRAMMING errors as though they were legitimate "cannot
        # narrow" answers.
        #
        # It hid one for a whole night: after the finding-7 fix stored examples as
        # {text, channel} dicts, propose_narrowing's first act was to call .lower() on a dict.
        # AttributeError, caught here, return None, quarantine. Indistinguishable from "no term
        # separates the positives", and invisible because the tune path had never run.
        #
        # A declined tune is normal and quiet. A tune that RAISED is a defect and now says so.
        inst._log("tune_error", artifact_id=aid,
                  reason=f"{type(e).__name__}: {str(e)[:160]}")
        return None

def _sweep_undispatchable_events(arts: dict, dry: bool) -> list:
    """Quarantine live artifacts whose EVENT has no dispatcher registration.

    THE THIRD ASYMMETRY (2026-08-28). _sweep_orphan_payloads handles a payload with no artifact.
    _sweep_payloadless_artifacts handles an artifact with no payload. Neither handles an artifact
    that is whole and correct and simply has NOWHERE TO FIRE.

    Found by measurement on core-life: of 38 active artifacts, 13 had never fired once, and exactly
    one of those was structurally incapable of it — art_331154505c87c73ffffe, event "Stop". The
    Stop registration of friction-dispatch was retired 2026-08-06; friction_installer names this
    artifact in a comment as "still ACTIVE and can never fire on any turn" and it then sat live for
    a further seventeen days, because naming a defect in a comment retires nothing.

    This is the same class the other two sweeps exist for and the reason Nick's standing complaint
    keeps recurring: a thing the loop built, that the loop reports as active, that cannot run. It
    counts toward `active`, toward every health readout, and toward the evidence a reader uses to
    conclude the tier is working.

    SCOPED TO A KNOWN-GOOD READ. artifact_typer.dispatchable_events() returns None when settings
    cannot be read, and this sweep does nothing at all in that case — an unreadable config must
    never look like "no event dispatches", which would quarantine the entire corpus in one pass.
    It also refuses to act when the live set is EMPTY, which is the same failure wearing a
    different mask: a settings.json that parses but registers no dispatcher (mid-edit, mid-sync,
    or a fresh clone before reconcile-hooks runs) would otherwise retire every artifact on the
    seat. Both are "cannot judge" — and the rule the other two sweeps already follow is that what
    cannot be judged is not acted on.

    Reversible by construction: rollback(), the same retirement path the other sweeps use, so the
    artifact and its reason stay on disk as the evidence for why it went.
    """
    gone: list = []
    try:
        import artifact_typer as _at
        live = _at.dispatchable_events()
    except Exception:
        return gone
    if live is None or not live:
        return gone
    for aid, art in list(arts.items()):
        ev = art.get("event")
        if not ev or ev in live:
            continue
        reason = (f"event {ev!r} has no live friction-dispatch registration in settings.json, so "
                  f"this artifact cannot fire on any turn (dispatches: {', '.join(sorted(live))})")
        gone.append((aid, reason))
        if not dry:
            inst._log("undispatchable_event", artifact_id=aid, event=ev, reason=reason)
            inst.rollback(aid, reason=reason)
    return gone


def sweep(dry=False) -> dict:
    active = inst._load_active()
    arts = {a["artifact_id"]: a for a in active.get("artifacts", [])}
    orphan_payloads = _sweep_orphan_payloads(arts, dry)
    # BOTH DIRECTIONS, not just the one that was noticed. A payload without an artifact was swept;
    # an artifact without a payload was not, and stayed live failing silently on every fire.
    payloadless = _sweep_payloadless_artifacts(arts, dry)
    # An artifact that is whole and correct but has nowhere to fire. Swept BEFORE the tuning and
    # blocking logic below for the same reason payloadless is: a retired artifact must not also be
    # narrowed or block-budgeted, and its zero fire count is a property of the EVENT, not the rule.
    undispatchable = _sweep_undispatchable_events(arts, dry)
    if not dry:
        for _aid, _ in payloadless:
            arts.pop(_aid, None)          # quarantined above; do not also block/tune it below
        for _aid, _ in undispatchable:
            arts.pop(_aid, None)
    if not arts:
        return {"active": 0, "quarantined": [], "orphan_payloads": orphan_payloads,
                "payloadless": payloadless, "undispatchable": undispatchable}
    actions = _recent_actions()
    err = Counter(a.get("artifact_id") for a in actions if a.get("action") in ERROR_ACTIONS)
    # per-(session, artifact) block counts; quarantine if ANY single session exceeded the budget —
    # counting across sessions would falsely quarantine a rule that fired once in each of several
    # healthy sessions (Codex #14).
    from collections import defaultdict
    per = defaultdict(int)
    for a in actions:
        if a.get("action") == "fire_block":
            per[(a.get("session_id"), a.get("artifact_id"))] += 1
    blocks = Counter()
    for (sess, aid), n in per.items():
        if n > blocks.get(aid, 0):
            blocks[aid] = n
    now = int(time.time())
    quarantine = []
    tuned_ok = []
    for aid, art in arts.items():
        reason = None
        # NEVER auto-quarantine a legacy (human-authored, trusted) guardrail on a transient error —
        # silently dropping stop-and-plan / verify-dont-claim is worse than a rare dispatch hiccup.
        if isinstance(aid, str) and aid.startswith("legacy_"):
            continue
        if err.get(aid, 0) > 0:
            reason = f"dispatch_error x{err[aid]}"
        elif art.get("effect", {}).get("mode") == "block" and art.get("enforced") is True and blocks.get(aid, 0) > BLOCK_BUDGET_SESSION:
            reason = f"over block budget x{blocks[aid]}"
        elif (art.get("effect", {}).get("mode") == "block" and art.get("enforced") is True
              and (now - int(art.get("enforced_at") or art.get("_installed_at") or now)) > LEASE_MAX_AGE):
            # lease-expiry applies ONLY to ENFORCED blocks, measured from ENFORCED_AT (promotion time,
            # renewed each close while still eligible) — not install time (Codex WS4: else a block
            # promoted after its 7-day proof window was instantly >24h old and quarantined). A SHADOW
            # block is never lease-expired, so it survives its multi-day proof window.
            reason = "enforced-lease expired (24h without renewal)"
        if reason:
            # NARROW BEFORE KILLING. Over-firing is usually a trigger that is too wide, not a
            # rule that is wrong — and quarantine throws away something the system learned
            # about how Nick works on evidence that only shows the trigger needs tightening.
            #
            # The operator, 2026-07-28, shown a drop-or-downgrade choice, pushed back: why drop a
            # rule instead of tuning it? The framing was wrong; this is the correction.
            #
            # Only the OVER-BUDGET case is tunable. A dispatch error means the rule is broken
            # rather than broad, and an expired lease means it stopped earning enforcement —
            # neither is fixed by a narrower trigger, so both still quarantine immediately.
            # OVER-BUDGET IS VOLUME, NOT ERROR. Nothing in the fire log labels a fire as
            # WRONG — a rule that fired three times in a session may simply mean Nick did the
            # guarded thing three times and the rule worked correctly each time. Narrowing on
            # that evidence would punish a rule for succeeding.
            #
            # So a tune requires evidence the rule fired where it should NOT have: at least
            # one recorded negative it now matches. Without that, over-budget means "look at
            # this", and the pre-existing quarantine stands rather than a silent tightening.
            # (core-business, hostile read finding 4.)
            if reason.startswith("over block budget") and _has_wrong_fire_evidence(art):
                tuned = _try_narrow(aid, art)
                if tuned:
                    tuned_ok.append((aid, tuned))
                    continue
            quarantine.append((aid, reason))
    # ── CONTRACT TUNING — the path that makes the tuner reachable at all ────────────────
    #
    # Until now _try_narrow was called from exactly one place: the "over block budget" branch,
    # which requires effect.mode == "block". si_project.upsert refuses to persist a block
    # without allow_block=True. So every artifact that could TRIGGER a tune was one the writer
    # would not ACCEPT, and nothing had ever been narrowed. Found 2026-07-28 by running the
    # path end to end for the first time, after four adversarial reads had passed over it.
    #
    # THE TRIGGER IS PURPOSE, NOT RATE. The operator, 2026-07-27, quoted in bin/grade-intent.py:
    # narrowing isn't enough — a rule should tune itself against what it's supposed to be doing
    # versus what it's actually doing, and whether that's even the right thing to be doing.
    #
    # A rate cannot answer that. Two rules can fire at the same rate for opposite reasons — one
    # catching exactly the right thing, which happens to be common; the other matching a
    # character pattern unrelated to its purpose. financial-figure-gate matched "era" inside
    # "generated"; recall-gate fired on its own quoted example while explaining its own fix.
    # Both had unremarkable rates while doing something unrelated to what they were for.
    # Narrowing on rate makes those rarer, not righter.
    #
    # So the condition is the rule CONTRADICTING ITS OWN EVIDENCE: it still catches every
    # positive it was installed for, and it now also matches a negative it was explicitly
    # gated to reject. That is unambiguous in a way a rate never is, and
    # _has_wrong_fire_evidence already computes exactly it.
    #
    # Rate stays a WATCH signal, not a trigger. Calibrated 2026-07-28 against 346 real prompts:
    # 24 of 26 live artifacts fire under the 3% install bar, the top two at 3.5% and 3.2%, the
    # next at 1.7%. The bar sits at the top of the real distribution rather than arbitrarily
    # inside it — a guess, but a well-placed one, and now measured rather than assumed.
    #
    # Safety: touches only mode=="inject" and never quarantines. Every decline path in
    # _try_narrow returns None and leaves the artifact exactly as it was, so the worst case is
    # the pre-existing behaviour. The invariant (every positive must still match) and the churn
    # cap (MAX_NARROWINGS) both still apply.
    _handled = {q for q, _ in quarantine} | {t for t, _ in tuned_ok}
    for aid, art in arts.items():
        if isinstance(aid, str) and aid.startswith("legacy_"):
            continue                                # hand-authored guardrails are never tuned
        if aid in _handled:
            continue                                # already resolved by the block path above
        if (art.get("effect") or {}).get("mode") != "inject":
            continue
        if not _has_wrong_fire_evidence(art):
            continue                                # no contradiction -> nothing to narrow
        if dry:
            tuned_ok.append((aid, "would narrow (dry run)"))
            continue
        note = _try_narrow(aid, art)
        if note:
            tuned_ok.append((aid, note))

    if not dry:
        for aid, note in tuned_ok:
            inst._log("tune_narrow", artifact_id=aid, reason=note)
        for aid, reason in quarantine:
            inst._log("quarantine", artifact_id=aid, reason=reason)
            # PASS THE REASON. This logged "quarantine" and then performed a deactivate — the log
            # line named the intent, the call did not carry it, and the generator's suppression
            # signal reads the flag the call never set.
            inst.rollback(aid, reason=reason)
    return {"active": len(arts), "quarantined": quarantine, "tuned": tuned_ok,
            "orphan_payloads": orphan_payloads, "payloadless": payloadless,
            "undispatchable": undispatchable}


if __name__ == "__main__":
    dry = "--check" in sys.argv
    r = sweep(dry=dry)
    print(f"watchdog: active={r['active']} quarantined={r['quarantined']}"
          f" payloadless={r.get('payloadless') or []}" + (" (dry)" if dry else ""))
