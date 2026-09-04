#!/usr/bin/env python3
"""friction_loop.py — P4 orchestrator. mine -> upsert friction_cases -> route -> gate -> install,
budgeted, then watchdog sweep. Runs at session close (via core-si-close) and on demand. Org-scoped.

Budget (Codex): install <=5 contracts and <=1 blocker per run; time-bounded. Every step logs; a
failure logs and stops the pipeline WITHOUT breaking close.

  CORE_ORG_ID=1 python3 friction_loop.py --days 14              # full: mine+install, budgeted
  CORE_ORG_ID=1 python3 friction_loop.py --days 14 --dry        # mine+route+gate, NO install
  CORE_ORG_ID=1 python3 friction_loop.py --status               # active artifacts + last run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "brain-pg"))
from _env import connect_corebrain, get_org_id  # noqa: E402
import friction_jsonl as fj          # noqa: E402
import friction_miner as fm          # noqa: E402
import friction_router as fr         # noqa: E402
import friction_test_gate as tg      # noqa: E402
import friction_installer as inst    # noqa: E402
import friction_watchdog as wd       # noqa: E402

MAX_CONTRACTS = 5
MAX_BLOCKERS = 1


def _upsert_case(cur, case: dict) -> None:
    # STATUS ONLY MOVES FORWARD ON A RE-MINE (2026-08-31, found by running --dry against a Core
    # that already had funnel history). build_case always recomputes status as 'mined'/'ineligible'
    # — a judgment about the transcript, not about what this or a prior run's routing/gating/install
    # decided. A case still inside the --days window gets re-mined (hence re-upserted) on EVERY
    # call, real or dry. A real run immediately re-decides and re-marks it via _mark_case a few
    # lines later in the same pass, so an unconditional EXCLUDED.status was harmless there — but a
    # DRY run deliberately calls _mark_case for NOTHING (a dry run must not mutate state), so an
    # unconditional reset silently regressed any already-denied/gate_failed/installed case back to
    # 'mined' every time --dry ran, discarding real history while printing "nothing was written".
    # Measured: one --dry pass reset 48 in-window cases, 6 of them 'installed', to 'mined'.
    # The guard: EXCLUDED.status only wins while the stored row hasn't progressed past mine-time
    # yet. Once _mark_case has moved it anywhere else, a later re-mine leaves status alone — the
    # next REAL run's _mark_case call still moves it forward again as normal; only a run that never
    # reaches _mark_case (a dry run, or a case dropped before routing) is affected, which is exactly
    # the case this exists to protect.
    cur.execute(
        """INSERT INTO friction_cases
             (case_id, org_id, status, source_file, source_uuid, detector_version,
              cluster_key, candidate_event, eligible, case_json, content_sha256)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (org_id, source_file, source_uuid, detector_version)
           DO UPDATE SET case_json=EXCLUDED.case_json,
                         status = CASE WHEN friction_cases.status IN ('mined', 'ineligible')
                                       THEN EXCLUDED.status ELSE friction_cases.status END,
                         eligible=EXCLUDED.eligible, updated_at=now()""",
        (case["case_id"], case["org_id"], case["status"], case["source"]["transcript_path"],
         case["source"]["correction_uuid"], case["source"]["detector_version"],
         case["support"]["cluster_key"], case["trigger_context"]["candidate_event"],
         case["quality"]["eligible_for_routing"], json.dumps(case),
         case["source"]["content_sha256"]))


def _mark_case(cur, org: int, case_id: str, status: str, reason: str | None = None,
               artifact_id: str | None = None) -> None:
    """THE WRITE-BACK THAT NEVER EXISTED. `_upsert_case` above sets status ONCE, at mine time,
    to exactly 'mined' or 'ineligible' — a judgment about transcript capture, not about what
    happened when the case was routed, gated and installed three hundred lines later in the same
    function. Measured 2026-08-31: 769 rows fleet-wide, 704 'mined' + 65 'ineligible', nothing
    else, ever — because nothing else was ever written. This is the missing UPDATE.

    Idempotent by construction: `status` records how far the LAST run that reached this function
    got a case. A case still inside the --days mining window gets re-mined every run, but
    `_upsert_case`'s ON CONFLICT (2026-08-31 fix) now leaves status alone once it has moved past
    'mined'/'ineligible' — so a re-mine can never regress a case behind this function's last write,
    dry or real. A real run calling this again simply overwrites with that run's fresh outcome,
    which is correct: routing is deterministic over the same case content, so a repeat run reproduces
    the same verdict. Once a case ages out of the window, _upsert_case stops touching it entirely and
    this UPDATE's last write becomes the permanent record.

    `reason` is redacted before storage — it can be a gate/installer message built from case
    content (a corpus prompt, a trigger word), the same class of caller-supplied string
    friction_installer._log already redacts centrally. This call site has no such logger to run
    through, so it redacts directly."""
    cur.execute(
        """UPDATE friction_cases
              SET status=%s, denied_reason=%s, routed_artifact_id=%s, updated_at=now()
            WHERE org_id=%s AND case_id=%s""",
        (status, (fj.redact(reason)[:200] if reason else None), artifact_id, org, case_id))


def backfill_installed_from_artifacts(org: int, dry: bool = False) -> dict:
    """ONE-TIME (but idempotent) recovery for rows that predate `_mark_case`.

    The 769 rows measured 2026-08-31 genuinely do not know how far they got THROUGH THIS RUNTIME's
    memory — the pipeline never wrote it down. But a subset left a SEPARATE, independent trace: an
    installed si_artifacts row whose spec carries the exact case_id that minted it, stamped with
    this router's own generator_version. That is not an inference, it is the same fact the loop
    itself would have written if `_mark_case` had existed at the time. Only that subset is touched;
    every other row is left alone and unexplained, per the task's own rule against inventing
    history for the rest.

    RLS scopes this to the CALLING Core's own org (connect_corebrain sets app.current_org_id), so
    running this on life only ever touches life's rows — a peer Core recovers its own subset the
    same way, by running this same function, not by life doing it on their behalf.

    ONLY TOUCHES 'mined'/'ineligible' ROWS — the ones `_mark_case` has never visited (2026-08-31,
    found by running this before the fix: it happily overwrote a row a REAL run had just freshly
    denied, because that case's OLD artifact predates today's stricter corpus/support and would
    not gate today). A fresh `_mark_case` verdict is the live pipeline re-run against CURRENT
    evidence and is strictly more authoritative than an old artifact's mere existence — this
    backfill exists only to give an opinion to rows that have never had ANY opinion, not to
    overrule the pipeline's own current judgement of a row it has already re-examined. Idempotent:
    a second run matches only whatever is still sitting at 'mined'/'ineligible'."""
    con = connect_corebrain(); cur = con.cursor()
    cur.execute(
        """SELECT fc.case_id, sa.artifact_id, sa.active, sa.quarantined
             FROM friction_cases fc
             JOIN si_artifacts sa
               ON sa.org_id = fc.org_id AND sa.spec->>'case_id' = fc.case_id
            WHERE fc.org_id = %s
              AND sa.spec->>'generator_version' = 'friction-router/3'
              AND fc.status IN ('mined', 'ineligible')""", (org,))
    rows = cur.fetchall()
    for case_id, artifact_id, active, quarantined in rows:
        reason = (f"backfilled from si_artifacts.{artifact_id} (generator_version="
                  f"friction-router/3, matched by case_id; active={active}, quarantined={quarantined})")
        if not dry:
            _mark_case(cur, org, case_id, "installed", reason, artifact_id)
    if not dry:
        con.commit()
    con.close()
    return {"org": org, "matched": len(rows), "dry": dry,
            "case_ids": [r[0] for r in rows][:10]}


def run(days: int, dry: bool) -> dict:
    org = get_org_id()
    con = connect_corebrain(); cur = con.cursor()
    cur.execute(
        """SELECT source_file, source_uuid, pattern_label, canonical_ask FROM pattern_observations
            WHERE org_id=%s AND source_file LIKE '%%.jsonl' AND source_uuid IS NOT NULL
              AND COALESCE(session_date, created_at::date) >= (CURRENT_DATE - %s)
            ORDER BY created_at DESC""", (org, days))
    rows = cur.fetchall()
    cache, cases = {}, {}
    for sf, su, label, canon in rows:
        p = Path(sf)
        if not p.exists():
            continue
        if sf not in cache:
            cache[sf] = fj.parse_file(sf)
        r = cache[sf]
        if not r.ok:
            continue
        m = fj.reconstruct_moment(su, r.by_uuid)
        if m is None:
            continue
        case = fm.build_case(org, sf, su, label, m, canonical_ask=canon)
        if case["case_id"] in cases:
            continue
        cases[case["case_id"]] = case

    # DENIED-RETRY LANE (2026-08-31, judge-required change 2 for the GAP C fix). Without this,
    # `denied` was a PERMANENT verdict in practice, even though nothing marks it permanent: the
    # query above only ever sees pattern_observations rows still inside the --days window, and a
    # correction ages out of that window in exactly `days` days regardless of what its
    # friction_cases row says. So a case denied under an OLDER fr.GENERATOR_VERSION could never be
    # re-tried once its source correction turned stale — no matter how many times route() improved
    # after that denial, including by this very fix. `_mark_case`'s own docstring calls the
    # write-back "the missing UPDATE"; this is its missing REDO.
    #
    # The retry re-loads the STORED case_json rather than re-parsing the transcript (which may have
    # rotated off disk by now — friction_miner already fails closed on that case, `file_gone`), so a
    # case can be re-judged long after its raw evidence has left the machine. It is stamped
    # `<reason>@<generator_version-at-denial-time>` (below, where `denied` is first written) so this
    # query can tell "already re-judged under the CURRENT router" from "still waiting on a fix"
    # without re-running route() on every denied row on every close — most of which would just
    # re-deny identically and are cheaper left alone until the version actually changes.
    #
    # SCOPED to status='denied' only — the bucket fr.route() itself returns None into. cap_denied
    # (a per-run budget, not a verdict — resets naturally next run) and gate_failed/install_failed
    # (judged by friction_test_gate / friction_installer, whose own versioning this pass does not
    # touch) are deliberately out of scope; widening this is a separate, deliberate change, not a
    # side effect of this one.
    #
    # A row already re-mined THIS run (still inside the window) is skipped — the fresh build_case()
    # copy a few lines above already supersedes whatever is stored, so loading the old JSON over it
    # would throw away this run's up-to-date moment/support capture for no benefit.
    cur.execute(
        """SELECT case_id, case_json FROM friction_cases
            WHERE org_id=%s AND status='denied' AND denied_reason IS NOT NULL
              AND denied_reason NOT LIKE %s""",
        (org, f"%@{fr.GENERATOR_VERSION}"))
    _retry_rows = cur.fetchall()
    for cid, cj in _retry_rows:
        if cid in cases:
            continue
        cases[cid] = cj if isinstance(cj, dict) else json.loads(cj)

    # CLUSTER SUPPORT, before anything is routed or persisted. build_case writes a PLACEHOLDER
    # support block (distinct_sessions=1, no siblings); compute_support replaces it with the real
    # values once every case in this run is known, which is why it cannot live inside build_case.
    #
    # This call was missed when compute_support was added on 2026-07-30 — it went into
    # friction_miner.main(), and this loop calls fm.build_case DIRECTLY and never touches main().
    # So the live path kept the placeholder, the new recurrence gate in friction_router rejected
    # every case for not recurring, and the close reported installed=0 out of 66 eligible. The
    # fix failed SAFE (nothing bad was minted) but it was a mint freeze rather than a quality
    # gate, which is not what it was for. Caught by reading the close log rather than by any test
    # — a gate whose correct behaviour and whose broken behaviour both produce "0 installed"
    # cannot be told apart from the outcome alone.
    fm.compute_support(cases)
    for case in cases.values():
        _upsert_case(cur, case)
    con.commit()

    # DISTILLATION COVERAGE — a fleet-safety check, found before pushing this to the baseline.
    #
    # friction_router refuses to mint without a canonical_ask (the distilled form of the ask), and
    # that rule is right: quoting Nick's raw frustration back at him is worse than silence. But
    # canonical_ask is written by an LLM EXTRACTION step that a parent session has to dispatch —
    # it does not happen on its own. Measured 2026-07-30: life has 287 distilled rows, and orgs
    # 2/3/4/5 have ZERO. So shipping the rule as-is freezes the generator on every peer Core,
    # permanently, and the freeze looks identical to a healthy loop with nothing to say.
    #
    # That silent-absence shape is the exact defect this whole day was spent finding, so it is not
    # allowed to ship inside the fix for it. The rule stays; the freeze is made LOUD.
    # A RATIO, not a zero test. The first version fired only at EXACTLY zero distilled, so a
    # Core with one stale distilled row out of sixty sat frozen at 59/60 and said nothing — the
    # same silent-absence shape one notch along. (Fable, blast-radius review.) Warn below a
    # threshold; the "never ran" wording is reserved for a genuine zero, since that diagnosis and
    # "extraction is lagging" call for different actions.
    _distilled = sum(1 for c in cases.values() if (c.get("canonical_ask") or "").strip())
    _ratio = (_distilled / len(cases)) if cases else 1.0
    coverage = {"cases": len(cases), "distilled": _distilled, "ratio": round(_ratio, 2),
                "extraction_never_ran": bool(cases) and _distilled == 0,
                "extraction_lagging": bool(cases) and 0 < _ratio < 0.5}
    if coverage["extraction_lagging"]:
        coverage["action"] = (
            f"only {_distilled}/{len(cases)} cases carry a canonical_ask ({_ratio:.0%}). The "
            f"generator can only mint from distilled cases, so most of this window is unmintable "
            f"until ask extraction catches up.")
    if coverage["extraction_never_ran"]:
        coverage["action"] = (
            "NO case on this Core has a canonical_ask. The generator will mint NOTHING until the "
            "ask-extraction step runs — this is not a quiet loop, it is an unrun pipeline. "
            "Run ask_miner.extract_pending(org) and cache_asks() to populate it.")

    # pool of REAL other correction prompts → corpus-neighbor negatives (Codex #5)
    neighbor_pool = [c["moment"]["correction"] for c in cases.values()
                     if c.get("moment", {}).get("correction")]
    corpus = inst._fetch_corpus_prompts(org) or []  # real prompts for the specificity gate
    installed = {"contract": 0, "blocker": 0}
    results = []

    # THE FUNNEL — the thing that has never existed (2026-08-31). Every case in `cases` ends
    # this loop in exactly one bucket; the bucket IS what `_mark_case` writes to friction_cases,
    # so this dict and the DB rows can never disagree about how a case was counted. Built ALONGSIDE
    # the existing `results`/`installed` bookkeeping rather than replacing it — `results` stays the
    # short human-readable tail main() prints, this is the full census.
    n_ineligible = sum(1 for c in cases.values() if c["status"] == "ineligible")
    funnel = {"days": days, "dry": dry, "mined": len(cases), "ineligible": n_ineligible,
              "eligible": len(cases) - n_ineligible, "duplicate_ask": 0, "routed": 0,
              "awaiting_ask": 0, "denied": 0, "denied_reasons": {}, "cap_denied": 0,
              "cap_denied_reasons": {}, "gate_passed": 0, "gate_failed": 0,
              "gate_failed_reasons": {}, "installed": 0, "install_failed": 0,
              "install_failed_reasons": {}, "would_install": 0}

    def _bump(bucket: str, reason: str | None = None) -> None:
        funnel[bucket] = funnel.get(bucket, 0) + 1
        if reason:
            rk = f"{bucket}_reasons"
            funnel.setdefault(rk, {})
            _r = str(reason)[:120]
            funnel[rk][_r] = funnel[rk].get(_r, 0) + 1

    # DEDUPE BY THE DISTILLED ASK. Several distinct friction cases legitimately distill to the
    # same canonical_ask — three separate frustrations about session-start context produced three
    # artifacts with identical messages AND an identical (context, brain) trigger. Installing all
    # three costs three injections on every matching prompt and says the same thing three times,
    # which is the accretion this whole pass exists to stop. One ask, one artifact; the extra
    # cases still count toward its recurrence support, which is where they belong.
    _seen_asks = set()
    for case in cases.values():
        if case["status"] == "ineligible":
            continue   # decided at mine time (parser chain incomplete) — route() would refuse
                       # it too (case["_drop_reason"]="not_eligible"), but that is not a NEW fact
                       # worth another write; the row already carries the reason it stopped here.
        cid = case["case_id"]
        _ask = (case.get("canonical_ask") or "").strip().lower()
        if _ask and _ask in _seen_asks:
            _bump("duplicate_ask")
            if not dry:
                _mark_case(cur, org, cid, "duplicate_ask", "same canonical_ask as an earlier case this run")
            continue
        own = case.get("moment", {}).get("correction", "")
        neighbors = [n for n in neighbor_pool if n != own][:8]
        spec = fr.route(case, neighbors=neighbors)
        if spec is None:
            # fr.route sets case["_drop_reason"] immediately before every `return None` (see
            # friction_router.py) — this is the ONLY place that value is read. Before this change
            # the reason was computed and then thrown away every single time.
            _reason = case.get("_drop_reason") or "unknown"
            if _reason == "no_canonical_ask":
                # NOT a judgment about the case — router's own words: "not lost — it returns to
                # the pool and becomes mintable as soon as ask_miner canonicalises it". Kept out
                # of `denied`, which is for refusals that need new EVIDENCE, not just a pending
                # upstream step, to ever resolve.
                _bump("awaiting_ask")
                if not dry:
                    _mark_case(cur, org, cid, "awaiting_ask", _reason)
            else:
                # STAMPED WITH THE GENERATOR VERSION (2026-08-31, judge-required change 2). The
                # DENIED-RETRY LANE above reads this stamp: a row whose stamp does not match the
                # CURRENT fr.GENERATOR_VERSION is stale evidence about an old router, not a
                # permanent verdict about the case, and gets re-queued next run. `_reason` itself
                # (unstamped) still drives the printed/persisted funnel breakdown below, so the
                # human-readable summary doesn't grow a version suffix on every line.
                _bump("denied", _reason)
                if not dry:
                    _mark_case(cur, org, cid, "denied", f"{_reason}@{fr.GENERATOR_VERSION}")
            continue
        funnel["routed"] += 1
        if _ask:
            _seen_asks.add(_ask)
        examples = spec.pop("_examples")
        is_block = spec["effect"]["mode"] == "block"
        if is_block and installed["blocker"] >= MAX_BLOCKERS:
            _bump("cap_denied", "blocker_cap")
            if not dry:
                _mark_case(cur, org, cid, "cap_denied", "per-run blocker cap reached", spec["artifact_id"])
            continue
        if not is_block and installed["contract"] >= MAX_CONTRACTS:
            _bump("cap_denied", "contract_cap")
            if not dry:
                _mark_case(cur, org, cid, "cap_denied", "per-run contract cap reached", spec["artifact_id"])
            continue
        ok, why = tg.gate(spec, examples, corpus_prompts=corpus)
        if not ok:
            results.append((spec["artifact_id"], "gate_fail", why))
            _bump("gate_failed", why)
            if not dry:
                _mark_case(cur, org, cid, "gate_failed", why, spec["artifact_id"])
            continue
        funnel["gate_passed"] += 1
        if dry:
            results.append((spec["artifact_id"], "would_install", spec["event"]))
            installed["blocker" if is_block else "contract"] += 1
            # DRY PROJECTION ONLY. Never written to friction_cases — a dry run must not mutate
            # state (matches the dropped_no_trigger convention: the count is measured, the row
            # is not touched). `would_install` is therefore visible ONLY in this returned/printed
            # funnel, never as a persisted case status.
            funnel["would_install"] += 1
            continue
        res = inst.install(spec, examples)
        results.append((spec["artifact_id"], "installed" if res["ok"] else "install_fail", res["reason"]))
        if res["ok"]:
            installed["blocker" if is_block else "contract"] += 1
            _bump("installed")
            _mark_case(cur, org, cid, "installed", None, spec["artifact_id"])
        else:
            _bump("install_failed", res.get("reason"))
            _mark_case(cur, org, cid, "install_failed", res.get("reason"), spec["artifact_id"])

    # top-5 reasons per bucket for the printed/persisted summary — full counts already live in
    # the per-case denied_reason column for anyone who needs the tail, not just the head.
    for _rk in ("denied_reasons", "cap_denied_reasons", "gate_failed_reasons", "install_failed_reasons"):
        funnel[_rk] = dict(sorted(funnel[_rk].items(), key=lambda kv: -kv[1])[:5])
    if not dry:
        con.commit()   # the loop's own UPDATEs above — lost on con.close() below without this
    # PERSISTED even on a dry run: this is a summary row, not a state mutation, and the whole
    # point of --dry is to let Nick SEE the funnel before anything installs (see deliverable).
    _log_action({"action": "funnel_summary", "org_id": org, **funnel})
    con.close()
    gen = generate_from_asks(org, dry)  # WS4: type-route recurring asks -> contracts / shadow blocks / proposals
    # WORKFLOWS — WIRED 2026-08-20. `generate_from_workflows` was written complete, with its own
    # renderer, trigger derivation, work-shape fallback and test file, and then called by NOTHING.
    # Zero callers in the entire tree; it had never executed once. Nick asked for workflows by name
    # in 67 separate messages across three Cores while a finished implementation sat here dark.
    #
    # It is a sibling of generate_from_asks, not a replacement: asks are things Nick SAID, workflows
    # are sequences he DID. Same installer, same test gate, same rollback path, own cap.
    wf = generate_from_workflows(org, dry)
    # SKILL GRADUATION — WIRED 2026-08-20. `skill_graduate.promote()` turns a hooked_skill that has
    # EARNED its place into a real `.claude/skills/` skill with a description-matched activation
    # surface. It was referenced in three comments across this codebase and called by nothing.
    #
    # Its gates are evidence, not opinion: MIN_FIRES real dispatches, across MIN_SESSIONS distinct
    # sessions, spanning MIN_DAYS — a skill cannot graduate on the strength of having been written.
    # That is the same shape as the enforcement proof window, and it is why this is safe unattended:
    # writing a local markdown file is reversible, and `demote()` retires one that stops earning it.
    #
    # It fires ZERO times today by design — no hooked_skill has ever been installed, so nothing can
    # have fired yet. Wiring it now means the first skill installed by the fix above starts
    # accumulating evidence immediately instead of waiting for someone to notice this was never
    # connected. Chaining it here also makes the whole ladder run in one pass:
    #     ask -> hooked_skill -> (fires, sessions, days) -> real skill
    try:
        import skill_graduate as _sg
        grad = _sg.promote(org, dry=dry)
    except Exception as e:
        grad = {"error": f"{type(e).__name__}: {e}"}
    sweep = wd.sweep(dry=dry)
    return {"cases": len(cases), "eligible": sum(1 for c in cases.values() if c["quality"]["eligible_for_routing"]),
            "installed": installed, "results": results[:8], "generator": gen, "workflows": wf, "graduation": grad,
            "watchdog": sweep, "distillation": coverage, "dry": dry,
            # THE FUNNEL (2026-08-31). Everything above this line already existed; this is the
            # new thing — a full stage-by-stage census of the fc_-case pipeline (mine -> route ->
            # gate -> install), matching the per-case status now persisted to friction_cases by
            # _mark_case. `generator`/`workflows` above cover the SEPARATE ask_-case pipeline
            # (generate_from_asks / generate_from_workflows), which already had its own counters
            # (no_trigger, already_covered, directives_capped, ...) — this does not duplicate that.
            "funnel": funnel}


def _retired_case_ids(org: int) -> dict[str, str]:
    """case_id -> reason, for artifacts deliberately QUARANTINED (not merely deactivated).

    Deactivation says "not in the live set right now"; quarantine says "a judgement was made about
    this". Only the latter should stop the generator re-minting, because a rule can legitimately be
    deactivated by an automated pass (demote-to-shadow, re-arm) and SHOULD come back when its
    evidence returns. Conflating the two would freeze the loop's ability to change its mind, which is
    the opposite failure.
    """
    out: dict[str, str] = {}
    try:
        from _env import connect_corebrain
        con = connect_corebrain()
    except Exception:
        return out
    try:
        cur = con.cursor()
        cur.execute("SET app.current_org_id = %s", (str(org),))
        # ONLY when EVERY artifact for that case_id is quarantined.
        #
        # A case_id can have several artifacts — the same distilled ask legitimately produced both
        # art_aba93e58 and art_bf54388 for `ask_use-codex-alongside-core-for-substantial-system`, one
        # a reminder and one an oracle-backed block. Retiring the case on the strength of ONE of them
        # being quarantined would stop the generator maintaining a sibling that is still live and
        # still doing its job. Found 2026-08-06 by quarantining the Stop artifact and watching the
        # durability test report its own case as "resurrected" — the case was never retired, one
        # artifact was.
        cur.execute(
            "SELECT spec->>'case_id',"
            "       min(COALESCE(quarantine_reason, 'quarantined')),"
            "       count(*) FILTER (WHERE quarantined),"
            "       count(*) "
            "  FROM si_artifacts WHERE org_id = %s AND spec->>'case_id' IS NOT NULL"
            " GROUP BY 1", (org,))
        for cid, why, n_q, n_all in cur.fetchall():
            if cid and n_q and n_q == n_all:
                out[str(cid)] = str(why)[:200]
    except Exception:
        return out
    finally:
        con.close()
    return out


# Re-route bar (2026-08-27). Deliberately ABOVE the ask floor: a triggerless ask reaching
# claude_md_directive skips the positive/negative/specificity gates a contract must pass, so it
# must bring more evidence, not less. 5 keeps business's 17x/10x/10x/8x/5x and drops its 3x.
REROUTE_MIN_SUPPORT = 5

# C1 + M1 (Codex, 2026-08-27): "trigger-grounding failure still escalates asks into always-loaded
# prose", and "'not procedure' does not POSITIVELY identify a standing preference".
#
# Both are the same objection: v2 treated failure-to-ground as permission. It is an ABSENCE, and an
# absence is not evidence. The fix is to measure the thing that actually distinguishes the two.
#
# A recurring ask can fail to ground a prompt trigger for exactly two reasons, and they are opposite:
#
#   NOISE          one incoherent cluster of unrelated moments. No shared vocabulary because there
#                  is no shared subject. Must NOT reach CLAUDE.md.
#   STANDING       one preference Nick applies across many different subjects. No shared vocabulary
#                  because the SUBJECTS differ, not the ask. This is precisely a CLAUDE.md directive.
#
# They are separable by evidence, not by wording. `_trigger_from_ask` grounds terms that recur across
# sibling prompts, so an ask that grounds nothing has members with little shared vocabulary — that is
# the same measurement, read for its other meaning. A genuine standing preference ALSO shows a
# recurring ask text (support >= REROUTE_MIN_SUPPORT) and is STILL recurring: the same request,
# arriving again, about different things. Noise does not hold that shape.
#
# So the bar is: the distilled ask repeats, is still live, and its member prompts are lexically
# DIVERSE. Diversity stops being the reason it failed and becomes the reason it qualifies. Deliberately
# a measurement over the corpus, not a lexicon — DIRECTIVE_SIGNALS is the lexicon that caused this
# defect, and a phrase list can only ever recognise asks someone already wrote down.
#
# M2 is closed by the same change: v2 excluded `_ask_type == "procedure"`, which stranded the one-step
# "procedures" route_type deliberately downgrades to inject_contract as "a rule wearing the wrong
# label". A rule wearing the wrong label is exactly a standing preference. The negative check is gone;
# eligibility is now decided by measured shape, which is what M1 asked for.
_REROUTE_MAX_OVERLAP = 0.34   # mean pairwise Jaccard across member prompts. Above this the members
                              # share a subject, so the ask should have grounded a trigger and become
                              # a CONTRACT, which carries the positive/negative/specificity gates.
_REROUTE_MIN_MEMBERS = 3      # two prompts is one pair; a single pair is not a distribution.


def _is_standing_preference(case: dict, route_reason: str = "") -> bool:  # noqa: ARG001  # privacy-ok: noqa linter directive, not a course code
    """POSITIVE identification that this ask is a standing preference (Codex C1/M1, v3 review).

    v3 argued measured lexical diversity was positive evidence. Codex was right that it is not — it
    is the same absence of shared vocabulary under a new name, and an ask can be diverse because it
    is noise. A positive test has to be a JUDGEMENT that the ask is normative, made from the ask
    itself, and one already exists upstream: `canonical_ask_type`, assigned by the LLM at
    distillation and validated against a closed set by both cache_asks and a DB CHECK.

    ask_miner.ASK_TYPES, verbatim: `constraint` = a rule about HOW to act ("verify before claiming").
    That is the definition of a standing preference, decided by a model reading the correction in
    context — not inferred here from what a regex could not find.

    EXACTLY ONE admissible shape: `constraint`.

    v4 also admitted a `procedure` that artifact_typer had demoted for decomposing into fewer than
    MIN_PROCEDURE_STEPS ("labelled procedure but decomposes into N step — not a procedure"), and
    called that a positive reclassification. Codex refused it on the final pass and was right:
    ARITY DISPROVES THE PROCEDURAL LABEL, IT DOES NOT ESTABLISH A STANDING PREFERENCE. "Not a
    procedure" is one more absence, and this function exists precisely because absences were being
    read as permission. The branch is gone.

    A one-step procedure-labelled ask therefore falls through to the trigger-requirement refusal
    below and is counted in `no_trigger` — visible and attributable, which is the correct outcome
    for an ask with no positive evidence, rather than a defect. Codex logged this as M2; admitting
    it to keep M2 "closed" would have traded an attributable refusal for an unevidenced escalation
    into always-loaded prose.

    Everything else — `procedure`, `none`, NULL, an unrecognised label — is refused. An absent type
    means the extractor made no judgement, and no judgement is not a positive one.
    """
    return (case.get("_ask_type") or "").strip().lower() == "constraint"


def _spans_contexts(org: int, case: dict) -> bool:
    """True when the cluster's member prompts are lexically diverse — positive evidence that the ask
    is a standing preference applied across subjects, rather than one incoherent cluster."""
    try:
        import ask_miner as _am
        ids = (case.get("support") or {}).get("member_ids") or []
        members = _am._member_prompts(org, ids)
    except Exception:
        return False                      # cannot measure -> refuse. Fail closed.
    toks = []
    for m in members:
        w = {x for x in re.findall(r"[a-z']{3,}", (m or "").lower()) if x not in _am._MERGE_STOP}
        if w:
            toks.append(w)
    if len(toks) < _REROUTE_MIN_MEMBERS:
        return False
    sims, n = [], len(toks)
    for i in range(n):
        for j in range(i + 1, n):
            u = toks[i] | toks[j]
            sims.append((len(toks[i] & toks[j]) / len(u)) if u else 0.0)
    if not sims:
        return False
    return (sum(sims) / len(sims)) <= _REROUTE_MAX_OVERLAP


def generate_from_asks(org: int, dry: bool, cap: int = 5) -> dict:
    """WS4 self-building step: for each recurring ASK (from the canonical_ask cache), route it to the
    right artifact type and generate it — inject contracts install live (budgeted), enforcement blocks
    install SHADOW (enforced=false), skills/directives become proposals. Already-covered → skip.
    Fail-open; never raises into close."""
    out = {"contracts": 0, "shadow_blocks": 0, "directives": 0, "procedures": 0, "work_hooks": 0, "no_trigger": 0,
           "slash_commands": 0, "workflow_proposals": 0,
           "procedures_pending": [], "scheduled_proposals": [], "already_covered": 0,
           "skipped": 0, "detail": []}
    try:
        import ask_miner
        import artifact_typer as at
        import artifact_generator as ag
        import friction_promote as fp

        # DELIBERATE RETIREMENT MUST SURVIVE THE NEXT GENERATION PASS.
        #
        # Found 2026-08-05 by deactivating an artifact and watching the loop put it back. I retired
        # `art_7da01522` ("make codex available across all cores") because the ask was verified
        # already satisfied fleet-wide — and the action log then shows install_begin -> test_pass ->
        # install_commit for that same id TWICE within four minutes. It came back at revision 236.
        #
        # The reason is that `active=false` is not a decision, it is a state. The ask is still in the
        # corpus, still clusters at support>=3, and nothing here remembered that a judgement had
        # already been made about it. So the loop could not accrete duplicates faster than dedupe
        # removed them, but it could resurrect anything retired for a REASON — which is worse,
        # because the reason was the whole point.
        #
        # Uses the EXISTING `quarantined` flag rather than a new mechanism: si_project.project()
        # already filters `AND NOT quarantined`, so a quarantined row is durably out of the live set,
        # and `quarantine_reason` already exists to say why. Adding a second parallel "retired" concept
        # beside it is exactly the accretion Nick's standing directive says not to do.
        retired_cases = _retired_case_ids(org)
        if retired_cases:
            out["retired_skipped"] = 0

        ask_drops: list = []
        cases = ask_miner.ask_cases(org, 3, drops=ask_drops)
        # THE LARGEST LOSS IN THE PIPELINE, AND UNTIL NOW THE ONLY SILENT ONE (2026-08-18). The
        # trigger gate runs inside case construction, upstream of routing, so a dropped ask never
        # reaches this loop and left no row anywhere. Measured: 69% on life, 75% business, 80%
        # school. `dropped_no_trigger` is the counter that makes the next question answerable —
        # including whether the gate is in the wrong place for asks bound for a terminal that has
        # no runtime trigger to gate on.
        # The COUNT is returned either way so a dry run can measure the loss; only a real run
        # writes the rows. A dry run that appends to the action log is not dry, and this counter
        # exists to be measured from dry runs.
        out["dropped_no_trigger"] = len(ask_drops)
        if not dry:
            for _d in ask_drops:
                _log_action({"action": "dropped_no_trigger", "org_id": org,
                             "ask": str(_d.get("ask", ""))[:160],
                             "support": _d.get("support"), "last_seen": _d.get("last_seen")})
        for case in cases:
            _cid = str(case.get("case_id") or "")
            if _cid and _cid in retired_cases:
                out["retired_skipped"] = out.get("retired_skipped", 0) + 1
                out["detail"].append((_cid, "retired", retired_cases[_cid]))
                continue
            # _ask_type is the cached, closed-vocabulary shape label from the extraction step; it can
            # only choose between inject-mode shapes, never reach block-mode (see artifact_typer).
            route = at.route_type(case.get("user_wanted", ""), ask_type=case.get("_ask_type"),
                                  still_recurring=case.get("_still_recurring", False),
                                  steps=case.get("_ask_steps", 0),
                                  frustration_share=case.get("_frustration_share", 0.0))
            t = route["type"]
            # THE TRIGGER REQUIREMENT, APPLIED HERE INSTEAD OF UPSTREAM (2026-08-20). ask_miner used
            # to refuse a triggerless ask during case construction, before routing — so an ask bound
            # for a terminal that needs no trigger died on a requirement that does not apply to it.
            #
            # Needs a prompt trigger:  inject_contract, enforcement_block — both fire by MATCHING.
            # Does not:                claude_md_directive (prose appended to CLAUDE.md, nothing to
            #                          match), hooked_skill (may install work-shaped, keyed on a
            #                          mutating tool call — see _gen_procedure's work_shape branch).
            #
            # A refusal here is attributable: it names the terminal that wanted the trigger, so the
            # loss shows up against a route instead of vanishing above all of them.
            # 2026-08-27 — THE DEFAULT ROUTED AN ASK TO ITS OWN REFUSAL. Found by core-business
            # (bus #5561), confirmed on life the same hour, then adversarially reviewed by BOTH
            # Codex and core-business (#5565). Four findings, all addressed below.
            #
            # THE DEFECT. route_type is documented "Deterministic; ambiguous -> inject_contract",
            # and the ONLY route to claude_md_directive is _hit(a, DIRECTIVE_SIGNALS) — eight
            # hardcoded phrases (artifact_typer.py:302) that are the literal text of the two
            # directives already in CLAUDE.md. The lexicon was fitted to the cases that already
            # existed, so no NEW diffuse preference can match it, which is why that terminal has
            # produced exactly two artifacts ever. A standing preference that cannot ground a
            # prompt trigger therefore fell to inject_contract — one of the two terminals that
            # MANDATORILY requires the thing it lacks — and died on the branch below.
            # Measured: 1 such ask on life; 6 on business, at 17x/10x/10x/8x/5x/3x.
            #
            # (1) ENFORCEMENT-DEFERRED IS EXCLUDED (Codex, CRITICAL). Excluding enforcement_block
            #     is NOT sufficient. artifact_typer.py:397-399 returns inject_contract, not
            #     enforcement_block, when an ask matches ORACLE_CATALOG but the oracle is a stub:
            #     "enforcement deferred (oracle X not ready)". Re-routing that would launder a
            #     deliberately-deferred BLOCK into always-loaded prose. Dead today — both catalog
            #     entries are oracle_ready=True — but oracle_ready=False is the DESIGNED state for
            #     staging a new oracle, so this is a landmine, not a hypothetical. Gated on the
            #     reason string because that is what carries the distinction.
            #
            # (2) THE EVIDENCE GRADIENT RAN BACKWARDS (business Q2 / Codex CRITICAL). A COHERENT
            #     cluster grounds a trigger and must then pass route_ask_case's honest-positive and
            #     real-neighbour-negative gates plus the installer's corpus-specificity re-gate. An
            #     INCOHERENT one grounds nothing and — unfixed — would reach permanent prose with no
            #     positive test, no negative test and no specificity bar. The weaker the evidence,
            #     the less scrutiny. So the re-route carries a HIGHER bar than a contract, not a
            #     lower one: still-recurring, and REROUTE_MIN_SUPPORT rather than the ask floor.
            #
            # (3) PER-PASS CAP (both reviewers). `cap` is checked only in the install_contract
            #     branch. Before this change the directive terminal was unreachable for new asks so
            #     that was harmless; after it, a seat with headroom could append the entire
            #     triggerless-constraint set to CLAUDE.md in one pass, permanently.
            #
            # (4) OBSERVABILITY FIRST (business, and it is the 2026-08-20 finance defect from the
            #     other side). This block sits ABOVE the no_trigger branch, so a re-routed ask never
            #     reaches that counter: no_trigger would read 0 and mean "invisible", not "none".
            #     A `rerouted` counter and a detail row are recorded OUTSIDE `if not dry`, so a dry
            #     run — the thing both seats measure with — shows the re-route instead of hiding it.
            _reroute_reason = str(route.get("reason", ""))
            if (not case.get("_ask_trigger")
                    and t == "inject_contract"
                    and "enforcement deferred" not in _reroute_reason
                    and _is_standing_preference(case, _reroute_reason)
                    and case.get("_still_recurring")
                    and int((case.get("support") or {}).get("count", 0) or 0) >= REROUTE_MIN_SUPPORT
                    and _spans_contexts(org, case)):
                t = "claude_md_directive"
                route = dict(route)
                route["type"] = t
                route["reason"] = ("triggerless standing preference re-routed from inject_contract "
                                   "(2026-08-27): the default terminal requires a prompt trigger "
                                   "this ask cannot ground")
                out["rerouted"] = out.get("rerouted", 0) + 1
                # m2 (Codex): `dropped_no_trigger` is incremented upstream by ask_cases for EVERY
                # ungrounded trigger, before routing exists — so after this change it counts asks
                # that were then successfully re-routed, and reads as a loss that did not happen.
                # The upstream counter is not ours to correct here (it is a different quantity, and
                # the 2026-08-20 work exists precisely because the two were once conflated). What is
                # ours is to publish the OVERLAP, so a reader can subtract instead of being misled.
                out["dropped_no_trigger_rerouted"] = out.get("dropped_no_trigger_rerouted", 0) + 1
                out["detail"].append((_cid, "rerouted", "inject_contract->claude_md_directive"))
                if not dry:
                    _log_action({"action": "reroute_to_directive", "org_id": org, "case_id": _cid,
                                 "from": "inject_contract",
                                 "ask": str(case.get("user_wanted", ""))[:160]})
            if not case.get("_ask_trigger") and t in ("inject_contract", "enforcement_block"):
                # COUNT IT IN ITS OWN BUCKET (2026-08-20, found by core-finance). `no_trigger` was
                # initialised at :253 and incremented NOWHERE — a counter that always reads 0 while
                # this branch quietly folded into `skipped`. finance caught it on a 4-case corpus by
                # noticing `ask_cases` reported 3 asks without a trigger while the loop reported
                # `no_trigger: 0`, and flagged it as a question rather than a defect because it had
                # not read this function. It was a defect.
                #
                # Ninth instance today of the same shape — built, named, never wired — and the
                # second I authored myself. A dead counter is worse than no counter: it publishes a
                # zero that reads as a measurement, and on a small seat that zero is exactly the
                # number someone would trust.
                #
                # NOTE the two are not the same quantity, which is why both exist: `dropped_no_trigger`
                # counts asks that could not ground a prompt trigger AT ALL, while this counts the
                # ones a terminal then REFUSED for lacking one. On finance all three of the former
                # routed to terminals that need no trigger, so 3 and 0 were both correct — the bug is
                # that 0 was correct by accident.
                out["no_trigger"] += 1
                out["detail"].append((_cid, "no_trigger", t))
                if not dry:
                    _log_action({"action": "route_needs_trigger", "org_id": org, "case_id": _cid,
                                 "route": t, "ask": str(case.get("user_wanted", ""))[:160]})
                continue
            if t == "already_covered":
                out["already_covered"] += 1
                continue
            if t == "scheduled_job_proposal":
                # Phase 3.5. Handled EXPLICITLY here rather than falling through to ag.generate
                # and landing in the `else: skipped` bucket. Both outcomes install nothing, but
                # only this one leaves a record — and a proposal that is silently skipped is
                # indistinguishable from a proposal that was never detected, which is the exact
                # invisibility this session spent all day finding elsewhere.
                #
                # Proposal is the CEILING, not a stage. A scheduled job runs unattended and can
                # spend money or take an outward action; both are Nick's hard rules, so this
                # surface never auto-installs no matter how much evidence accumulates.
                out.setdefault("scheduled_proposals", []).append(
                    {"ask": case.get("user_wanted", "")[:160],
                     "cadence": route.get("cadence"), "reason": route.get("reason")})
                continue
            if dry:
                # THE CAP APPLIES IN DRY RUNS TOO (Codex, v3 review — a defect I introduced). The
                # cap check below sits after this branch, so a dry run could report more directives
                # than a live pass would ever install and never publish directives_capped. Both
                # seats measure with dry runs; a measurement that does not predict live behaviour is
                # the same class of defect as the dead counter this whole block exists to avoid.
                if t == "claude_md_directive" and out["directives"] >= cap:
                    out.setdefault("directives_capped", 0)
                    out["directives_capped"] += 1
                    out["detail"].append((_cid, "directive_capped", f"per-pass cap {cap}"))
                    continue
                # count by intended type without applying
                out["shadow_blocks" if t == "enforcement_block" else
                    "directives" if t == "claude_md_directive" else
                    "procedures" if t == "hooked_skill" else
                    "work_hooks" if t == "work_hook" else
                    "slash_commands" if t == "slash_command" else
                    "workflow_proposals" if t == "workflow" else "contracts"] += 1
                continue
            # M3 (Codex, 2026-08-27): the per-pass `cap` was checked only in the install_contract
            # branch, so a seat with steering headroom could append the whole triggerless-constraint
            # set to CLAUDE.md in one pass, permanently. v2 capped the RE-ROUTED directives and left
            # natively-routed ones uncapped, which Codex correctly re-flagged as still open. The cap
            # belongs HERE, at the one place every directive passes through, regardless of how it
            # was routed. Checked BEFORE generate() so a capped directive costs no work and is
            # attributable rather than silently absent.
            if t == "claude_md_directive" and out["directives"] >= cap:
                out.setdefault("directives_capped", 0)
                out["directives_capped"] += 1
                out["detail"].append((_cid, "directive_capped", f"per-pass cap {cap}"))
                continue
            g = ag.generate(org, case, route)
            act = g.get("action")
            if act == "directive":
                out["directives"] += 1
                res = g.get("result", {}) or {}
                out["detail"].append((case["case_id"], "directive", res.get("action")))
                # DURABLE RECORD (2026-08-18). Every other terminal in this dispatch leaves an
                # action-log row because it goes through the installer's logger; the directive
                # terminal went through no installer and left nothing. Cost, measured by business:
                # it could not tell "generate() was never reached" from "generate() ran and
                # returned directive_skipped" for its own 4x ask — indistinguishable from outside
                # the process, so its report had to end in a question. Same blindness hid life's
                # TWO APPLIED DIRECTIVES for 26 days: the loop's only successful graduation to date
                # was invisible in its own log, and this fleet spent two days concluding from that
                # silence that the loop had never graduated anything.
                # 2026-08-24: this used to prepend "directive_" to a value that ALREADY is
                # `directive_applied` / `directive_skipped` / `directive_error` (artifact_generator
                # returns all nine that way), writing `directive_directive_applied`. So the canonical
                # grep for `directive_applied` returned 0 fleet-wide while core-business held the
                # loop's only end-to-end success — the exact blindness the comment above describes,
                # reproduced one layer down in the same commit that documented it.
                # MIGRATION, per core-business: rows written BEFORE this date carry the doubled
                # spelling (10 on life, 1 on business). Log history is not rewritten — a query about
                # the loop's full history must match `directive_directive_*` as well. No CODE consumer
                # is affected: every caller checks the RETURN value, which was always correct.
                _log_action({"action": str(res.get("action") or "directive_unknown"),
                             "org_id": org, "case_id": case.get("case_id"),
                             "ask": str(case.get("user_wanted", ""))[:160],
                             "reason": str(res.get("reason") or res.get("line") or "")[:200]})
            elif act == "install_contract":
                if out["contracts"] >= cap:
                    continue
                r = inst.install(g["spec"], g["examples"])
                if r.get("ok"):
                    out["contracts"] += 1
                out["detail"].append((case["case_id"], "contract", r.get("reason")))
            elif act == "install_shadow_block":
                r = inst.install_shadow_block(g["spec"], g["examples"])
                if r.get("ok"):
                    out["shadow_blocks"] += 1
                out["detail"].append((case["case_id"], "shadow_block", r.get("reason")))
            elif act == "install_hooked_skill":
                # A work_hook ROUTE installs as a hooked_skill ARTIFACT — the installer fences
                # PreToolUse to that type on purpose (friction_installer.py:493). Counted under its
                # own key so the frustration terminal stays visible in the ledger rather than being
                # silently absorbed into the procedure count.
                _key = "work_hooks" if t == "work_hook" else "procedures"
                # Same installer, same independent re-gate, same corpus specificity bar as a contract —
                # the payload block is the only difference, and _validate_spec re-verifies its hash.
                r = inst.install(g["spec"], g["examples"])
                if r.get("ok"):
                    out[_key] += 1
                out["detail"].append((case["case_id"], _key.rstrip("s"), r.get("reason")))
            elif act == "hooked_skill_pending":
                # Surfaced for the close directive to author. Nothing is installed until the payload
                # exists and validates, so a pending draft is inert, not a half-installed artifact.
                out["procedures_pending"].append({"artifact_id": g.get("artifact_id"),
                                                  "ask": g.get("ask")})
            elif act == "install_slash_command":
                # Same installer, same independent re-gate as a contract — the payload already
                # lives at its real `.claude/commands/<slug>.md` location (written by
                # _gen_slash_command before this point); install() only re-validates + persists
                # the bookkeeping trigger that makes it quarantinable/reversible.
                r = inst.install(g["spec"], g["examples"])
                if r.get("ok"):
                    out["slash_commands"] += 1
                out["detail"].append((case["case_id"], "slash_command", r.get("reason")))
            elif act == "install_workflow_proposal":
                # Same rails again: the proposal payload is already written; install() gates and
                # persists the pointer artifact. effect.mode is inject-only, so this can never
                # auto-run the proposal — see artifact_generator's module comment on `workflow`.
                r = inst.install(g["spec"], g["examples"])
                if r.get("ok"):
                    out["workflow_proposals"] += 1
                out["detail"].append((case["case_id"], "workflow_proposal", r.get("reason")))
            else:
                out["skipped"] += 1
                # Suppression is the branch with no artifact to point at, so without this row it is
                # the one outcome that leaves NO trace anywhere — which is how a coverage claim
                # naming a hook retired on 08-06 went on suppressing a 19x correction for twelve
                # days on every seat without producing a single readable symptom.
                if act == "skip":
                    _log_action({"action": "route_suppressed", "org_id": org,
                                 "case_id": case.get("case_id"),
                                 "ask": str(case.get("user_wanted", ""))[:160],
                                 "reason": str(g.get("reason") or "")[:200]})
        # AUTONOMOUS enforcement promotion — flip verified-oracle shadow blocks that cleared the proof
        # window to enforced (reversible; watchdog + BLOCKS_ENABLED are the safety, not a human).
        out["promote"] = fp.auto_promote(org, dry=dry)
    except Exception as e:
        out["error"] = str(e)[:200]
    out["detail"] = out["detail"][:8]
    return out


# Same bar consolidate_sessions and the ask path use. Imported rather than redeclared would be
# better still, but consolidate_sessions lives in brain-pg and importing it here to read one int
# would drag the whole brain connection setup into this module's import time.
_MIN_WF_SESSIONS = 2

# Ceiling on WORK-SHAPE workflow artifacts specifically, below friction_installer's
# MAX_ACTIVE_PROCEDURES=10. These are the artifacts whose condition matches every mutation, so ten of
# them is ten reminders competing for the same injection budget on the same edit. Prompt-triggered
# workflow artifacts are self-limiting (their regex has to match) and are not counted here.
MAX_WORKSHAPE_WORKFLOWS = 3

# Words too common to carry a trigger. A regex on any of these fires on most of Nick's traffic, and
# the installer's corpus gate would reject it anyway — filtering here makes the refusal legible
# instead of arriving as an opaque "over-broad trigger".
_WF_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "at", "by", "from",
    "it", "its", "this", "that", "is", "be", "as", "not", "but", "before", "after", "under",
    "into", "than", "then", "so", "do", "does", "did", "run", "make", "get", "one", "all", "any",
}


def _wf_trigger_terms(name: str, real_prompts: list, limit: int = 3) -> list:
    """Trigger terms grounded in BOTH the workflow's identity and real prompt text.

    Terms are the intersection: content words from the workflow NAME that actually appear in at
    least one stored trigger prompt. Neither source works alone —

      * name only: the first version did this and it fails on real data. Both workflows captured on
        2026-08-05 opened with "sent them" and "ok so are you ready to continue", which contain none
        of their own workflow's words. The positive test could never match, so the trigger would be
        asserted with no evidence behind it — and the installer would either reject it or, worse,
        install a rule grounded in nothing.
      * prompts only: circular. Terms derived from the prompt match that prompt by construction,
        which proves nothing and drifts the trigger away from what the workflow IS.

    The intersection guarantees a real positive exists AND keeps the trigger tied to the workflow.

    CONSEQUENCE WORTH KNOWING: a workflow whose opening prompt states no intent — "continue", "go",
    "sent them" — yields NO terms and is correctly refused here. That is not a gap to paper over: a
    workflow triggered by "continue" has no learnable PROMPT trigger. Its correct home is a
    work-shape artifact keyed on the TOOL about to run (event=PreToolUse), which is precisely what
    Phase E1 exists to enable. This function refusing is what makes E1 necessary rather than
    optional.

    CONJUNCTIVE, matching the ask path: each term becomes its own `prompt_regex` op inside `all`.
    Separate ops rather than one lookahead, because `_validate_regex` rejects lookaheads.
    """
    import re as _re
    low_prompts = [(p or "").lower() for p in (real_prompts or [])]
    if not low_prompts:
        return []
    words = [w for w in _re.findall(r"[a-z]{3,}", (name or "").lower())
             if w not in _WF_STOPWORDS]
    seen, terms = set(), []
    for w in words:
        if w in seen:
            continue
        seen.add(w)
        if not any(_re.search(rf"\b{_re.escape(w)}\b", p) for p in low_prompts):
            continue                    # the name says it; no real prompt does. Not groundable.
        terms.append(rf"\b{_re.escape(w)}\b")
        if len(terms) >= limit:
            break

    # THE CONJUNCTION MUST MATCH A REAL PROMPT, NOT JUST EACH TERM SEPARATELY.
    #
    # Each term above is validated INDIVIDUALLY — "does this word appear in at least one stored
    # prompt?" — and then they are combined with `all`. So three terms can each match a DIFFERENT
    # prompt while the conjunction matches NONE, and the artifact installs with a positive test that
    # passes term-by-term and a condition that can never fire.
    #
    # MEASURED, not hypothetical. core-ops installed `art_wf72bf83d8ec6f7b5e` from this path and
    # reported it after four days: **ZERO fires, and the failure it was mined from recurred anyway.**
    # Its condition is `\bclaude\b AND \bchrome\b AND \bright\b` — three words that never
    # co-occur in one prompt on that seat.
    #
    # This is the OPPOSITE failure from the one the specificity bar catches, and the fleet now has
    # one of each from the same weak extraction:
    #     over-broad   stopword alternations firing on 60%+ of everything   (business, school)
    #     over-narrow  an AND of three words that never co-occur            (ops)
    # core-finance named the gap hours before ops measured it: "a specificity floor with no
    # sensitivity ceiling admits inert triggers silently."
    #
    # Drop the last term until a real prompt satisfies the whole conjunction. Dropping from the END
    # keeps the most name-salient words, which are the ones ordered first. If even a single term
    # cannot match — impossible here, since each was individually verified — return nothing rather
    # than install a rule with no reachable input.
    while terms:
        rx = [_re.compile(t) for t in terms]
        if any(all(r.search(p) for r in rx) for p in low_prompts):
            return terms
        terms.pop()
    return terms


def _wf_negatives(org: int, terms: list, limit: int = 4) -> list:
    """Real past prompts that do NOT match the trigger — genuine negatives from live traffic.

    Drawn from `COALESCE(correction_text, prompt_text)` — the only corpus of real prompts this Core
    keeps, read from the column the RUNTIME actually matches. A synthesised negative would prove
    nothing: the point of a negative is that the rule must stay quiet on a DIFFERENT thing Nick
    actually said.

    THE COLUMN WAS WRONG UNTIL 2026-08-20, and of the three places this error appeared, this was the
    worst. `prompt_text` is the PRECEDING turn; `correction_text` is what Nick typed, and the
    dispatcher matches against the current user message. So the NEGATIVES proving a rule stays quiet
    were drawn from a population the rule will never see — the test gate's specificity proof was
    against the wrong text entirely.

    core-business found the collision (73% of rows hold different text in the two fields); this third
    instance was caught by a test assertion written for the other two, scanning every corpus query in
    the file rather than the ones already known to be wrong.
    """
    import re as _re
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain-pg"))
        from _env import connect_corebrain
        conn = connect_corebrain()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT COALESCE(correction_text, prompt_text) AS p "
                "FROM pattern_observations "
                "WHERE org_id=%s AND COALESCE(correction_text, prompt_text) IS NOT NULL "
                "AND length(COALESCE(correction_text, prompt_text)) BETWEEN 40 AND 400 "
                "ORDER BY p LIMIT 400",
                (org,),
            )
            rows = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for p in rows:
        low = (p or "").lower()
        if any(_re.search(t, low) for t in terms):
            continue                    # matches part of the trigger — not a clean negative
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _render_workflow_body(name: str, truth: str, steps: list) -> str:
    """The payload AND the injected message: the ordered steps, as text the model can act on.

    Deterministic — no LLM. This is the whole reason the workflow path is simpler than the ask path:
    the procedure is already structured data, so rendering it is formatting rather than authoring.
    """
    trig = ""
    for line in (truth or "").splitlines():
        if line.strip().lower().startswith("**triggered when:**"):
            trig = line.split("**", 2)[-1].replace("Triggered when:**", "").strip(" :*")
            break
    # DEFENCE IN DEPTH. consolidate_sessions._validate already refuses a workflow whose steps
    # describe an outward or irreversible action, at the point it would become durable. This is the
    # second check, at the only chokepoint every injected byte passes through, because the first one
    # guards the EXTRACTION path and rows can also arrive by a direct DB write, a restored backup, or
    # a future writer that forgets. Returning empty here makes the artifact inert rather than
    # injecting a sequence that tells the agent to send or buy something.
    import re as _re2
    _OUTWARD = _re2.compile(
        r"\b(email|e-mail|sms|imessage|buy|purchase|order|pay|checkout|subscribe|delete|rm -rf"
        r"|drop table|truncate|force[- ]?push|curl|wget|webhook|osascript)\b", _re2.I)
    bad = [a for _, a, _ in steps if _OUTWARD.search(a or "")]
    if bad:
        return ""

    lines = [f"## Learned workflow — {name}"]
    if trig:
        lines.append(f"\n_Applies when: {trig}_")
    lines.append("\nThis sequence is what actually worked before, in this order:\n")
    for idx, action, hint in steps:
        lines.append(f"{idx}. {action}" + (f"   _[{hint}]_" if hint else ""))
    lines.append("\nFollow it unless something about this case makes a step wrong — in which case "
                 "say which step and why, rather than silently skipping it.")
    return "\n".join(lines)


def generate_from_workflows(org: int, dry: bool, cap: int = 3) -> dict:
    """PHASE D3 — a recurring Workflow generates a behaviour that actually fires.

    This is the half of the learning loop that did not exist. `generate_from_asks` above learns from
    FAILURE: a correction recurs, an artifact is minted. Nothing learned from SUCCESS. Phase B gave
    the brain a way to store an ordered sequence and Phase C captured them, but
    `MIN_SESSIONS_TO_GENERATE` was referenced in exactly one place — a print statement in
    `consolidate_sessions --status`. It gated nothing. A workflow could recur forever and produce no
    behaviour, because no consumer existed.

    DELIBERATELY THE SAME INSTALL PATH, not a parallel one:
      * same `friction_installer.install()`, so the corpus-grounded gate, the DSL validation, the
        lease bounds, the shadow/rollback machinery and the `_PAYLOAD_FORBIDDEN` guard-surface refusal
        all apply unchanged;
      * same `hooked_skill` type, so `skill_graduate.promote()` can later graduate a proven workflow
        into a real `.claude/skills/<n>/SKILL.md` with no further wiring;
      * same 2-session recurrence bar the friction loop already uses.
    A second installer for workflows would have been the patch-not-consolidate failure Nick has
    corrected nine times.

    ONE REAL DIFFERENCE from the ask path, and it is a simplification: a correction-derived procedure
    needs an LLM to AUTHOR its payload, so it lands as `hooked_skill_pending` and waits for a subagent.
    A workflow's payload already IS data — the ordered steps — so it renders deterministically and
    installs in the same pass. No pending state, no authoring step, no LLM in this path at all.
    """
    out = {"installed": 0, "skipped": [], "errors": [], "detail": []}
    try:
        import hashlib
        import re as _re
        import friction_router as fr
        import ask_miner
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain-pg"))
        from _env import connect_corebrain

        conn = connect_corebrain()
        try:
            cur = conn.cursor()
            cur.execute("SET app.current_org_id = %s", (str(org),))
            # Recurrence = distinct source_revision_id, i.e. INDEPENDENT sessions. Counting rows or
            # steps would let one long session masquerade as a pattern.
            cur.execute(
                # Sessions counted from workflow_triggers, NOT workflow_steps. steps is unique
                # per (entity, step_index), so a recurring workflow adds no step rows and would
                # forever report 1 session. Occurrences are what recurrence means.
                "SELECT e.id, e.name, COALESCE(e.compiled_truth_md,''), "
                "       count(DISTINCT t.source_revision_id) AS sessions "
                "FROM entities e JOIN workflow_triggers t ON t.workflow_entity_id = e.id "
                "WHERE e.kind='Workflow' AND e.org_id=%s "
                "GROUP BY e.id, e.name, e.compiled_truth_md "
                "HAVING count(DISTINCT t.source_revision_id) >= %s "
                "ORDER BY sessions DESC",
                (org, _MIN_WF_SESSIONS),
            )
            cands = cur.fetchall()

            existing = {a.get("case_id") for a in inst._load_active().get("artifacts", [])}
            for eid, name, truth, sessions in cands:
                case_id = f"wf_{eid}"
                if case_id in existing:
                    out["skipped"].append({"workflow": name, "why": "already installed"})
                    continue
                if out["installed"] >= cap:
                    out["skipped"].append({"workflow": name, "why": f"per-pass cap {cap}"})
                    continue
                # Standing ceiling on work-shape workflow artifacts, independent of the per-pass cap.
                # The per-pass cap bounds one close; without this, three closes install nine.
                _live_ws = sum(1 for a in inst._load_active().get("artifacts", [])
                               if str(a.get("case_id", "")).startswith("wf_")
                               and a.get("event") == "PreToolUse")
                if _live_ws >= MAX_WORKSHAPE_WORKFLOWS:
                    out["skipped"].append(
                        {"workflow": name,
                         "why": f"{_live_ws} work-shape workflow artifacts already live "
                                f"(ceiling {MAX_WORKSHAPE_WORKFLOWS}) — these match EVERY mutation, "
                                f"so more of them just split the same injection budget"})
                    continue

                cur.execute("SELECT step_index, action, tool_hint FROM workflow_steps "
                            "WHERE workflow_entity_id=%s ORDER BY step_index", (eid,))
                steps = cur.fetchall()
                if len(steps) < 2:
                    out["skipped"].append({"workflow": name, "why": "under 2 steps"})
                    continue

                cur.execute("SELECT prompt_text FROM workflow_triggers "
                            "WHERE workflow_entity_id=%s LIMIT 6", (eid,))
                real_prompts = [r[0] for r in cur.fetchall()]
                if not real_prompts:
                    # No honest positive is available, so refuse rather than fabricate one. Same
                    # bar ask_miner.route_ask_case applies ("the ask has no honest positive").
                    out["skipped"].append({"workflow": name, "why": "no stored trigger prompt"})
                    continue

                # TWO SHAPES, and which one applies is decided by the evidence rather than chosen.
                #
                # A workflow whose opening prompt states the intent ("fix the failing lint") can fire
                # on the PROMPT. A workflow opened with "continue" / "sent them" cannot — there is no
                # word to key on, and inventing one installs a rule its own evidence does not
                # support. That second case is the majority of real workflows, and before Phase E1 it
                # had nowhere to go, so D3 simply refused it.
                #
                # It now falls back to WORK SHAPE: fire when the WORK is about to happen — a mutating
                # tool call — rather than when the prompt happens to contain a word. Same install
                # path, same gate; the corpus specificity check is skipped for these by design
                # (friction_installer), because tool fields do not exist in a prompt corpus.
                terms = _wf_trigger_terms(name, real_prompts)
                matching = [p for p in real_prompts
                            if terms and all(_re.search(t, (p or "").lower()) for t in terms)]
                work_shape = not matching

                body = _render_workflow_body(name, truth, steps)
                if dry:
                    out["detail"].append((name, "would install", f"{len(steps)} steps, {sessions} sessions"))
                    out["installed"] += 1
                    continue

                aid = "art_wf" + hashlib.sha256(f"workflow|{eid}".encode()).hexdigest()[:16]
                # write_procedure returns the payload BLOCK itself ({path, sha256, bytes}) — not an
                # {ok, payload} envelope. Checking .get("ok") treated every successful write as a
                # failure and reported why=None, which is how a working path looked broken.
                pay = inst.write_procedure(aid, body)
                if not (isinstance(pay, dict) and pay.get("sha256")):
                    out["errors"].append({"workflow": name, "why": f"payload write failed: {pay}"})
                    continue

                if work_shape:
                    # Keyed on the TOOL, matching artifact_generator's work-shape vocabulary exactly
                    # rather than inventing a second one.
                    event = "PreToolUse"
                    cond = {"all": [
                        {"op": "event_is", "value": "PreToolUse"},
                        {"op": "tool_name_in", "value": ["Edit", "Write", "MultiEdit", "NotebookEdit"]},
                        {"op": "tool_mutability_is", "value": "mutating"}]}
                    pos = [{"id": "p1", "event": "PreToolUse", "expected": "fire",
                            "provenance": "real_positive",
                            "hook_input": fr._hook_input("PreToolUse", tool="Edit")}]
                    neg = [{"id": "n_evt", "event": "UserPromptSubmit", "expected": "no_fire",
                            "provenance": "event_mismatch",
                            "hook_input": fr._hook_input("UserPromptSubmit", prompt=name)},
                           {"id": "n_tool", "event": "PreToolUse", "expected": "no_fire",
                            "provenance": "polarity_mutation",
                            "hook_input": fr._hook_input("PreToolUse", tool="Read")}]
                else:
                    event = "UserPromptSubmit"
                    cond = {"all": [{"op": "event_is", "value": "UserPromptSubmit"}]
                                   + [{"op": "prompt_regex", "value": t} for t in terms]}
                    pos = [{"id": "p1", "event": "UserPromptSubmit", "expected": "fire",
                            "provenance": "workflow_trigger_prompt",
                            "hook_input": fr._hook_input("UserPromptSubmit", prompt=matching[0][:300])}]
                    neg = [{"id": "n_evt", "event": "Stop", "expected": "no_fire",
                            "provenance": "event_mismatch",
                            "hook_input": fr._hook_input("Stop", assistant=name)},
                           {"id": "n_pol", "event": "UserPromptSubmit", "expected": "no_fire",
                            "provenance": "polarity_mutation",
                            "hook_input": fr._hook_input("UserPromptSubmit",
                                                         prompt="an entirely unrelated topic")}]
                    for i, m in enumerate(_wf_negatives(org, terms, limit=4)):
                        neg.append({"id": f"n_nb{i}", "event": "UserPromptSubmit",
                                    "expected": "no_fire", "provenance": "real_neighbor",
                                    "hook_input": fr._hook_input("UserPromptSubmit", prompt=m[:300])})
                    if not any(n["provenance"] == "real_neighbor" for n in neg):
                        out["skipped"].append({"workflow": name, "why": "no real corpus neighbour"})
                        continue

                spec = {
                    "spec_version": 1, "artifact_id": aid, "case_id": case_id, "org_id": org,
                    "type": "hooked_skill", "event": event, "condition": cond,
                    # E2: the STEPS go in the message. Not a path to them. A workflow the agent has
                    # to remember to go read is not a learned workflow.
                    "effect": {"mode": "inject", "message": body[:2000], "skill_id": None},
                    "tests": {"positive_ids": [p["id"] for p in pos],
                              "negative_ids": [n["id"] for n in neg]},
                    "template": {"id": "workflow-procedure-v1", "sha256": "pending"},
                    "scope": "org_local",
                    # ONE fire per session for these. A work-shape condition carries no
                    # workflow-specific predicate — it matches every Edit/Write in the session — so
                    # the lease is the only bound that exists. Codex measured the worst case at
                    # ~2k tokens of injected context across the first two mutations with 10
                    # artifacts installed; a workflow reminder is about the approach to the task,
                    # not about each edit, so a second firing buys nothing and costs the same.
                    "lease": {"max_fires_per_session": 1 if work_shape else 2, "expires_at": None},
                    "generator_version": "workflow-d3/1",
                    "payload": pay,
                }
                r = inst.install(spec, {"positive": pos, "negative": neg})
                if r.get("ok"):
                    out["installed"] += 1
                else:
                    out["skipped"].append({"workflow": name, "why": r.get("reason")})
                out["detail"].append((name, "install", r.get("reason")))
        finally:
            conn.close()
    except Exception as e:                                  # fail-open; never raises into close
        out["errors"].append(str(e)[:200])
    return out


_MAX_TUNES_PER_CLOSE = 2      # churn budget; two narrowings a close, more is thrashing
_OVERFIRE_RATIO = 3.0
_HEALTH_WINDOW_DAYS = 30      # both sides of the over-fire ratio read this same window         # fires-per-correction above which a rule is noise rather than signal


def _dsl_evaluate(spec: dict, text: str) -> bool:
    """Does this spec's condition match `text`? The oracle handed to friction_tune.verify().

    Uses the DISPATCHER's own evaluator. Two implementations of "does this rule fire" is how a
    tuner starts verifying against semantics the live path does not have — the same class of defect
    as the numerator/denominator mismatch found in the correction metric.
    """
    try:
        import friction_dispatch as fdp
        ctx = {"event": spec.get("event") or "UserPromptSubmit", "prompt_text": text,
               "assistant_text": text, "tool_name": None, "session_id": "tune-verify"}
        return bool(fdp.evaluate(spec.get("condition") or {},
                                ctx, spec.get("trusted_regex") is True))
    except Exception:
        return False          # fail CLOSED: an unverifiable narrowing must not be applied


def _log_action(rec: dict) -> None:
    """Append one action row — stamped and redacted, matching friction_installer._log.

    TWO WRITERS, ONE FILE, DIFFERENT SAFETY PROPERTIES (found 2026-08-12). This wrote `rec` verbatim
    while `friction_installer._log` (:154-163) stamps `ts` and redacts every string value centrally.
    Same log, same readers, and only one half was safe or dateable.

    WHAT THE MISSING TIMESTAMP COST, measured: the log holds 219 `tune_flag_needs_oracle` rows and
    NOT ONE carries a ts, so they cannot be aged, rated, or told apart as a burst versus a trend.
    Reconstructing anything required counting LINE NUMBERS — which is how the real shape finally
    surfaced: 219 events across just TWO artifacts (legacy_plan-not-execute 111x,
    art_97b6fff21bdf97478d45 108x), emitted back-to-back once per loop run across ~110 runs. Not 219
    findings. One unactionable conclusion, re-derived every pass.

    Every other action in this file — payload_mismatch, fire_inject, dispatch_error — carries a ts
    because it goes through the installer's logger. The loop's own rows were the exception.

    REDACTION MATTERS MORE THAN THE STAMP. The installer's central redaction exists because a
    caller-supplied field can carry a secret verbatim (Codex 5th review). This writer bypassed it
    entirely, so the ONE logger that records the loop's own reasoning — including `why` strings built
    from artifact content — was the one that could persist a secret.

    A caller-supplied `ts` is preserved rather than overwritten: a replayed or backfilled row should
    keep the time it describes, not the time it was written.
    """
    try:
        safe = {k: (fj.redact(v) if isinstance(v, str) else v) for k, v in (rec or {}).items()}
        safe.setdefault("ts", int(time.time()))
        inst.ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(inst.ACTION_LOG, "a") as f:
            f.write(json.dumps(safe) + "\n")
    except Exception:
        pass


def _rewrite_active(aid: str, new_spec: dict) -> None:
    """Replace one artifact in active.json atomically.

    Shares the lost-update race Codex flagged across every active.json writer: atomic rename
    prevents torn JSON, not a concurrent writer working from a stale snapshot. Recorded, not fixed
    here — it needs one lock across the installer, the watchdog, the repin and this, which is its
    own change.
    """
    import tempfile, os as _os
    p = inst.ACTIVE
    d = json.loads(p.read_text())
    d["artifacts"] = [new_spec if a.get("artifact_id") == aid else a for a in d.get("artifacts", [])]
    tmp = tempfile.NamedTemporaryFile("w", dir=str(p.parent), delete=False)
    try:
        json.dump(d, tmp, indent=1); tmp.flush(); _os.fsync(tmp.fileno()); tmp.close()
        _os.replace(tmp.name, p)
    except Exception:
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass
        raise


def _persist_tuned_spec(cur, org: int, aid: str, old: dict, new_spec: dict, term) -> None:
    """Write the narrowed spec, keeping the prior one for one-statement rollback.

    PRESERVES PROJECTOR-APPLIED FIELDS. Restoring a spec from `prior_spec` alone silently dropped  # privacy-ok: generic engineering vocabulary
    `trusted_regex` when I rolled back a demotion earlier today: both stores then read "inject", the
    contract looked restored, and it matched nothing because the strict regex path rejected its
    alternation. A rollback that looks successful and isn't is worse than a failed one.
    """
    # `enforced` is deliberately NOT carried: a rewritten rule must re-earn enforcement through the
    # proof window rather than inherit it (Codex — a stale enforced=True surviving a replacement is
    # authorization state outliving the thing it authorized). trusted_regex and _installed_at ARE
    # carried, because dropping trusted_regex is what silently broke a rollback earlier today.
    carried = {k: old[k] for k in ("trusted_regex", "_installed_at") if k in old}
    merged = {**new_spec, **carried}
    cur.execute("UPDATE si_artifacts SET prior_spec = spec, spec = %s, "
                "revision = COALESCE(revision,0)+1, updated_at = now() "
                "WHERE artifact_id=%s AND org_id=%s", (json.dumps(merged), aid, org))
    if cur.rowcount != 1:
        # No canonical row: writing only active.json would leave the file ahead of the DB and the
        # next projection would revert it. Refuse rather than half-apply (Codex: the UPDATE ignored
        # rowcount, so a missing row still reported success).
        raise RuntimeError(f"no si_artifacts row for {aid} (rowcount={cur.rowcount}) — "
                           f"refusing to write active.json alone and create a split-brain")
    _rewrite_active(aid, merged)
    _log_action({"action": "tune_narrow", "artifact_id": aid, "org_id": org, "term": term})


def _demote_to_shadow(cur, org: int, aid: str, art: dict, reason: str) -> None:
    """Stop ENFORCING, keep LOOKING. Safe only because rearm_shadowed() now exists."""
    shadow = {**art}
    eff = dict(shadow.get("effect") or {})
    eff["mode"] = "shadow"
    shadow["effect"] = eff
    shadow["enforced"] = False
    cur.execute("UPDATE si_artifacts SET prior_spec = spec, spec = %s, "
                "revision = COALESCE(revision,0)+1, updated_at = now() "
                "WHERE artifact_id=%s AND org_id=%s", (json.dumps(shadow), aid, org))
    if cur.rowcount != 1:
        raise RuntimeError(f"no si_artifacts row for {aid} — refusing to demote in active.json "
                           f"alone; a projection would silently restore enforcement")
    _rewrite_active(aid, shadow)
    _log_action({"action": "tune_demote_shadow", "artifact_id": aid, "org_id": org,
                 "reason": reason[:200]})


def _ask_positives(cur, org: int, case_id: str, spec_message: str = "") -> list:
    """Positives selected by the ORIGINATING ASK, never by the artifact's own trigger.

    THIS IS THE FIX FOR THE CIRCULARITY CODEX CALLED FATAL. My first version built positives by
    regex-matching the artifact's own condition against the corpus, so `friction_tune.verify()` only
    proved the narrowed rule still matched what the old rule matched — tautological preservation of
    the trigger, not evidence that the rule SHOULD fire. It could not justify narrowing anything.

    `canonical_ask` is assigned by the LLM extraction pass over the CORRECTION TEXT and is
    independent of any contract's regex. A contract's `case_id` is `ask_<slug-of-that-ask>`, so the
    ask is recoverable and the prompts of every correction sharing it are cases the rule is
    *supposed* to cover — established without reference to how the rule currently matches.

    CONSEQUENCE, AND IT IS A GOOD ONE: a `legacy_*` contract has no originating ask, so it gets no
    positives and is therefore NOT auto-tunable. The four legacy contracts are the hand-authored
    floor rules (recall-first, verify-dont-claim, plan-not-execute, model-routing). They are exactly
    the artifacts where an autonomous narrowing would be least defensible, and the honest evidence
    requirement excludes them on its own rather than by a special case I would have to remember.
    """
    if not case_id.startswith("ask_"):
        return []
    # EXACT ask, not a slug reconstruction. The first version rebuilt an ILIKE pattern from the
    # case_id slug, which is truncated at 60 chars and hyphen-joined — Codex: "syntactically
    # independent of the trigger but not semantically reliable; it can select unrelated asks."
    # The artifact already carries the ask VERBATIM in its own injected message
    # ("Recurring ask (3x): <the canonical ask>"), so the exact string is available and no
    # reconstruction is needed.
    ask = _ask_text_from_message(spec_message)
    if not ask or len(ask) < 12:
        return []
    # WINDOWED to match _fire_count's window, and ORDERED so "20" means the 20 most recent rather
    # than 20 arbitrary rows. Codex flagged the mismatch twice: fires were windowed to 30d while
    # positives stayed all-time and unordered, so the ratio could both mask current noise and tune
    # against occasions from months ago. A ratio is only meaningful if both sides describe the same
    # period, and this is the second time that exact error appeared in this function.
    cur.execute(
        "SELECT DISTINCT prompt_text, max(COALESCE(session_date, created_at::date)) AS d "
        "FROM pattern_observations "
        "WHERE org_id=%s AND prompt_text IS NOT NULL AND canonical_ask = %s "
        "  AND length(prompt_text) BETWEEN 30 AND 2000 "
        "  AND COALESCE(session_date, created_at::date) >= (CURRENT_DATE - %s) "
        "GROUP BY prompt_text ORDER BY d DESC LIMIT 20",
        (org, ask, _HEALTH_WINDOW_DAYS),
    )
    return [{"text": r[0], "channel": "prompt"} for r in cur.fetchall()]


def _ask_text_from_message(msg: str) -> str:
    """Pull the canonical_ask back out of the artifact's own injected message.

    The generator writes "Recurring ask (Nx): <ask>" / "Recurring ask (Nx demonstrated of M in
    cluster): <ask>" / "Recurring expectation (<type>): <ask>". Every form ends with the verbatim
    ask, so one split recovers it exactly — no fuzzy matching, and a mismatch yields no positives
    rather than wrong ones.
    """
    import re as _re
    m = _re.match(r"^Recurring (?:ask|expectation)\s*\([^)]*\)\s*:\s*(.+)$",
                  (msg or "").strip(), _re.S)
    if not m:
        return ""
    ask = m.group(1).strip()
    # Procedure messages append a payload pointer / body after the ask; keep the first line only.
    return ask.split("\n")[0].strip().rstrip(".")


def classify_artifact_health(cur, org: int, art: dict, not_binding: set,
                             not_binding_artifacts: set | None = None,
                             not_binding_fired: set | None = None,
                             not_binding_fired_slugs: set | None = None,
                             corpus_prompts: list | None = None) -> dict:
    """Which PROBLEM does this artifact have, and therefore which action applies?

    THE CONCEPTUAL ERROR IN MY FIRST ATTEMPT, and Codex's finding #5 is what exposed it: I pointed
    the NARROWING tool at `not_binding`. Those are opposite problems.

      NOT_BINDING  — the rule fires and the correction recurs anyway. It is not too WIDE, it is
                     ineffective. Narrowing makes it fire less, which is strictly worse. The honest
                     action is re-derivation, and that is a mint decision, not a tune.
      OVER-FIRING  — the rule fires far more often than the correction it guards occurs. That is
                     noise, and narrowing is exactly right.

    Conflating them is how a tuner would have "fixed" an ineffective rule by making it quieter.

    TWO SOURCES OF THE NOT-BINDING VERDICT, because there are two generations of artifact:

      `not_binding`            legacy learned_contract SLUGS ("plan-not-execute"). Matched by
                               substring against case_id/artifact_id, as it always was.
      `not_binding_artifacts`  artifact IDs from the si_artifacts fitness pass, whose verdicts are
                               NOT-BINDING-FIRED / NOT-BINDING-NO-FIRE. Matched EXACTLY — these are
                               opaque hashes and substring-matching them invites a collision.

    The second source was measured all along and read by nobody: tune_pass only ever loaded the
    legacy slug list, and slugs never appear inside an `art_…`/`ask_…` id, so this branch could not
    fire for any artifact the loop generated. Nothing was misbehaving yet — every artifact currently
    reads GRADUATED or INSUFFICIENT — so the bug was invisible and would have stayed invisible until
    the first genuinely ineffective artifact was silently left in place.
    """
    aid = art.get("artifact_id", "")
    case_id = str(art.get("case_id", ""))
    name = case_id[4:] if case_id.startswith("ask_") else case_id

    # FOSSIL FIRST, and the ordering is the point.
    #
    # This check needs NO efficacy data. Every other branch below waits on fitness verdicts or fire
    # counts, which is why the three live fossils were invisible until 2026-08-06: none of them is
    # NOT-BINDING-FIRED, so the only caller of trigger_is_fossil (build_oracle_spec) never saw them,
    # and a fossil that has never fired fell through to `silent` -> action "none". A detector whose
    # findings cannot reach a queue is the exact "measuring without acting" failure this whole arm of
    # the system was rebuilt to remove, and I had just reproduced it in the fix for it.
    #
    # Before the fitness branches because a trigger that cannot fire on its own subject makes every
    # downstream measurement meaningless: fires, recurrence and proportion are all computed against a
    # trigger that was never pointed at the right thing. Fix the aim before reading the target.
    _fossil = trigger_is_fossil(art)
    if _fossil:
        return {"class": "fossil_trigger", "action": "flag_fossil", "fossil": _fossil,
                "why": "the trigger shares no topical word with the ask it guards — it cannot fire on "
                       "its own subject, so it is injected-token cost with no reachable benefit. "
                       "friction_router no longer mints these; this one predates the fix."}
    # UNREACHABLE, checked right after FOSSIL and for the same reason: a condition that matches
    # nothing makes every downstream number meaningless. Fossil asks "is it aimed at the right
    # subject"; this asks "can it hit anything at all". An artifact can pass the first and fail the
    # second, because terms are validated separately and combined with `all`.
    #
    # Measured on life the day this was written: 6 of 18 prompt-conditioned artifacts match ZERO of
    # 400 real corpus prompts. Undecidable below MIN_CORPUS, where it returns None rather than
    # retiring a working artifact on a thin sample.
    _unreach = trigger_is_unreachable(art, corpus_prompts or [])
    if _unreach and _unreach.get("reachable"):
        # FRAGILE, NOT DEAD — surfaced, never retired. See trigger_is_unreachable: a rule matching
        # one or two corpus rows is reachable and may be legitimately rare. ops's exemplar sat at
        # 2 of 107 and I wrongly called it broken.
        return {"class": "fragile_trigger", "action": "none",
                "why": f"reachable but thin — matches {_unreach['hits']} of "
                       f"{_unreach['corpus']} own prompts. Not retired: rare is not broken, and the "
                       f"fragility line is a judgement nobody has made."}
    if _unreach:
        return {"class": "unreachable_trigger", "action": "flag_fossil", "fossil": _unreach,
                "why": f"its {_unreach['legs']} prompt terms are combined with ALL and no prompt in "
                       f"{_unreach['corpus']} matches every one — the condition is unsatisfiable "
                       f"against the corpus it was mined from, so it can never fire. Each term was "
                       f"validated separately; the conjunction never was."}
    if aid and aid in (not_binding_fired or set()):
        # NOT-BINDING-FIRED is a COMPLIANCE failure, not a stale-data failure, and that is the whole
        # of D3. The artifact fired — the words were delivered — and the correction recurred anyway.
        # Re-deriving it from the corpus re-reads the same unchanged ask and produces the same prose
        # behind a different keyword filter. The mechanism is what failed, so the escalation has to
        # change the KIND of mechanism: an oracle that inspects what Core actually did, evaluated
        # before the reply exists. flag_rederive here would treat a rule that was obeyed-and-still-
        # wrong as a rule that was merely out of date.
        return {"class": "not_binding_fired", "action": "flag_needs_oracle",
                "why": "fired and the correction still recurred — the words reached the model and "
                       "did not bind. Prose cannot be fixed by re-deriving more prose; this needs a "
                       "mechanical oracle on Core's own behaviour, or retirement in favour of one "
                       "that already exists."}
    if aid and aid in (not_binding_artifacts or set()):
        # ASK THE DISPATCHER BEFORE BLAMING THE TRIGGER. "NO-FIRE" is measured from fire_count;
        # whether the trigger MATCHED is a different fact, recorded on dispatch_nofire.matched.
        _m = _matched_without_firing(aid)
        if _m:
            return {"class": "suppressed", "action": "flag_suppressed",
                    "why": f"the trigger MATCHED in {_m} session(s) and the effect still did not "
                           "apply — so this is not a targeting problem and re-deriving it would "
                           "discard a working trigger for a fresh guess. Find what ate the result: "
                           "a missing payload, a capped budget, or a suppression."}
        return {"class": "not_binding", "action": "flag_rederive",
                "why": "the fitness pass judged this artifact NOT-BINDING-NO-FIRE — its trigger "
                       "never matched while the correction recurred. That IS a targeting/staleness "
                       "problem, so re-derivation is the honest action. Narrowing would be backwards."}
    if any(n in aid or n in case_id for n in (not_binding_fired_slugs or set())):
        # The LEGACY generation gets the same split, for the same reason. Its fitness verdict is a
        # flat "NOT-BINDING" with no FIRED/NO-FIRE suffix — but the row carries `fire_count`, so the
        # distinction is measured, just not named. Routing on it here also removes an overclaim that
        # was live in this function until 2026-08-06: this branch asserted "fires and the correction
        # still recurs" for EVERY legacy not-binding contract, including one that had never fired at
        # all. Both live cases happen to have fired (plan-not-execute 6x, stop-and-plan 7x), so the
        # sentence was true of today's data and would have quietly become false on tomorrow's.
        return {"class": "not_binding_fired", "action": "flag_needs_oracle",
                "why": "a legacy contract that FIRES and whose correction still recurs — obeyed and "
                       "still wrong. Re-deriving the prose cannot fix a mechanism failure; this needs "
                       "an oracle on Core's behaviour, or retirement in favour of one that exists."}
    if any(n in aid or n in case_id for n in not_binding):
        _m = _matched_without_firing(aid) if aid else 0
        if _m:
            return {"class": "suppressed", "action": "flag_suppressed",
                    "why": f"the trigger MATCHED in {_m} session(s) and the effect still did not "
                           "apply — a suppression problem, not a targeting one. Re-derivation "
                           "would replace a trigger that demonstrably works."}
        return {"class": "not_binding", "action": "flag_rederive",
                "why": "judged NOT-BINDING with NO recorded fires — the trigger never matched while "
                       "the correction recurred. That IS a targeting/staleness problem, so "
                       "re-derivation is the honest action. Narrowing would be backwards."}
    fires = _fire_count(aid, days=_HEALTH_WINDOW_DAYS)
    if fires == 0:
        return {"class": "silent", "action": "none",
                "why": "no fires recorded — nothing measured yet, and silence is not evidence"}
    positives = _ask_positives(cur, org, case_id,
                              (art.get("effect") or {}).get("message") or "")
    if len(positives) < 2:
        return {"class": "unevidenced", "action": "none",
                "why": f"only {len(positives)} independent positive(s) from the originating ask; "
                       f"narrowing cannot be verified, so it is not attempted"}
    ratio = fires / max(len(positives), 1)
    if ratio >= _OVERFIRE_RATIO:
        return {"class": "over_firing", "action": "narrow", "positives": positives,
                "why": f"{fires} fires against {len(positives)} real occasions ({ratio:.1f}x)"}
    return {"class": "proportionate", "action": "none",
            "why": f"{fires} fires against {len(positives)} occasions ({ratio:.1f}x) — in proportion"}


def _fire_count(aid: str, days: int = 30) -> int:
    """fire_inject events for this artifact WITHIN a window.

    Windowed because the unwindowed version was a time bomb (Codex): it counted the whole historical
    log while the positive set is capped at 20, so every long-lived HEALTHY rule eventually crosses
    3x20 fires and would be narrowed and then shadowed for the crime of existing a long time. A
    ratio is only meaningful if both sides describe the same period.
    """
    import time as _t
    cutoff = int(_t.time()) - days * 86400
    n = 0
    try:
        for ln in Path(inst.ACTION_LOG).read_text(errors="ignore").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("artifact_id") != aid or r.get("action") != "fire_inject":
                continue
            ts = r.get("ts")
            if isinstance(ts, int) and ts >= cutoff:
                n += 1
    except Exception:
        pass
    return n


def _matched_without_firing(aid: str, days: int = 30) -> int:
    """Sessions where this artifact's condition MATCHED and the effect still did not apply.

    THIS MAKES "matched but did not fire" A CONSUMED STATE, which is the half of master-plan
    Phase 4 that ecec751 left undone. That commit added the `matched` field to `dispatch_nofire`
    so the state became READABLE. Nothing read it. A field written and never consulted is the
    void-write shape the same session spent hours cataloguing — and it was in my own Phase 4 work.

    WHAT IT DECIDES, and it is not decoration. `flag_rederive` below justifies itself with "its
    trigger NEVER MATCHED while the correction recurred". That sentence is an INFERENCE from
    fire_count == 0, and it conflates two states the dispatcher already distinguishes:

        considered=19, matched=[]         no trigger fits the traffic   -> re-derive is right
        considered=19, matched=[art_x]    art_x matched and was DROPPED -> re-derive is WRONG

    In the second case the trigger works and something downstream ate the result — a missing
    payload, a capped budget, a suppression. Re-deriving there discards a WORKING trigger and
    replaces it with a fresh guess, which friction_dispatch.py:624 names exactly: "re-deriving a
    rule whose trigger is fine is how a working artifact gets replaced by a worse one."

    Windowed like _fire_count, and for the same reason: a match from four months ago says nothing
    about whether the trigger fits today's traffic, and comparing a lifetime match count against a
    30-day fire count would be the two-different-frames error this file already carries scars from.
    """
    import time as _t
    cutoff = int(_t.time()) - days * 86400
    n = 0
    try:
        for ln in Path(inst.ACTION_LOG).read_text(errors="ignore").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("action") != "dispatch_nofire":
                continue
            # Rows written before ecec751 have no `matched` key at all. Absent is NOT empty: an
            # old row cannot testify that the trigger failed to match, it simply did not record
            # the answer. Treating missing as "no match" would manufacture support for exactly the
            # re-derivation this function exists to prevent.
            m = r.get("matched")
            if not isinstance(m, list) or aid not in m:
                continue
            ts = r.get("ts")
            if isinstance(ts, int) and ts >= cutoff:
                n += 1
    except Exception:
        pass
    return n


_ASK_PREFIX_RX = re.compile(
    r"^\s*recurring\s+(?:ask|expectation)\s*\([^)]*\)\s*:\s*", re.I)
_MAX_DEDUPE_PER_CLOSE = 4


def _dedupe_key(msg: str) -> str:
    """Identity of an injected reminder, ignoring the bookkeeping prefix that varies per mint.

    The prefix is why exact-match dedupe never caught anything: the SAME directive ships as
    "Recurring ask (10x): use codex alongside core..." from the ask path and as
    "Recurring expectation (instruction-directive): use codex alongside core..." from the friction
    path. Byte-identical bodies, different prefixes, so `_load_active`'s exact-lowercase check saw
    two distinct artifacts and installed both.
    """
    m = _ASK_PREFIX_RX.sub("", str(msg or ""))
    m = re.sub(r"\s+", " ", m).strip().lower().rstrip(".")
    return m


def dedupe_active(org: int, dry: bool = False, cap: int = _MAX_DEDUPE_PER_CLOSE) -> dict:
    """Deactivate artifacts that restate a reminder another live artifact already makes.

    WHY THIS EXISTS — the loop was producing the exact accretion it was built to prevent.
    Measured 2026-08-05 on life: SIX active artifacts about routing work to Codex, including two
    pairs whose effect messages were byte-identical after prefix stripping. The consequence is not
    cosmetic — every one of them matches on a prompt about code, so a single turn had three
    near-identical Codex reminders injected into context at once, which is both token waste and the
    thing that trains Nick to stop reading injected context.

    Fable named the root cause and it is a real design defect, not a bug: three arms define "the
    same ask" three different ways. Minting merges clusters at Jaccard 0.20 (loose,
    `ask_miner.merge_similar`), install-time dedupe compares exact lowercased text (strict), and
    fitness matches by substring (stricter still). Because measurement is stricter than minting, a
    reworded recurrence mints a duplicate on one side and reads as a separate success on the other.

    This pass closes the loop's own output side of that mismatch. It does NOT try to unify the three
    relations — that is a design change needing Nick — it just stops the duplicates accumulating.

    KEEP-OLDEST, deliberately: the earliest `installed_at` is the one with the longest fitness
    history and the most fire evidence, so keeping it preserves measurement continuity. Deactivation
    is reversible (`active=false`, revision bumped) and capped per close.
    """
    out = {"groups": 0, "deactivated": [], "kept": [], "skipped": 0, "errors": []}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain-pg"))
        from _env import connect_corebrain
        import si_project as sp
    except Exception as exc:
        out["errors"].append(f"import: {exc}")
        return out

    try:
        con = connect_corebrain()
    except Exception as exc:
        out["errors"].append(f"connect: {exc}")
        return out
    try:
        cur = con.cursor()
        cur.execute("SET app.current_org_id = %s", (str(org),))
        cur.execute(
            "SELECT artifact_id, spec->'effect'->>'message', installed_at, spec->>'type' "
            "FROM si_artifacts WHERE org_id=%s AND active AND NOT quarantined "
            "ORDER BY installed_at ASC, artifact_id ASC", (org,))
        rows = cur.fetchall()
    finally:
        con.close()

    groups: dict[str, list] = {}
    for aid, msg, inst, atype in rows:
        # A hooked_skill's payload lives in a file, not the message, so message equality does not
        # imply the behaviours are equal. Only reminder-shaped artifacts are compared.
        if atype not in (None, "contract"):
            continue
        key = _dedupe_key(msg)
        if not key or len(key) < 20:
            continue
        groups.setdefault(key, []).append((aid, inst))

    acted = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        out["groups"] += 1
        keep, dupes = members[0], members[1:]
        out["kept"].append({"artifact_id": keep[0], "restated_by": len(dupes),
                            "key": key[:70]})
        for aid, _inst in dupes:
            if acted >= cap:
                out["skipped"] += 1
                continue
            if dry:
                out["deactivated"].append({"artifact_id": aid, "duplicate_of": keep[0],
                                           "dry": True})
                acted += 1
                continue
            try:
                if sp.deactivate(org, aid):
                    out["deactivated"].append({"artifact_id": aid, "duplicate_of": keep[0]})
                    _log_action({"action": "dedupe_deactivate", "artifact_id": aid,
                                 "duplicate_of": keep[0], "key": key[:120]})
                    acted += 1
                else:
                    out["errors"].append(f"{aid}: deactivate reported no change")
            except Exception as exc:
                out["errors"].append(f"{aid}: {exc}")
    return out


def tune_pass(org: int, dry: bool = False) -> dict:
    """PHASE T — the acting arm. Classify every live artifact, then take the action its problem calls
    for. Runs at close.

    Before this, `friction_tune.py` had existed for weeks with every caller in `bin/tests/`:
    1,145 installs, 1,070 test-passes, **0 tune actions of any kind, ever**, while
    `contract-fitness.json` named its `not_binding` findings and nothing read them.

    Actions are deliberately narrow, and MOST ARTIFACTS GET NONE. Measured on life at build time:
    7 unevidenced, 7 silent, 4 proportionate, 1 not_binding, **0 over-firing** — so one action out
    of nineteen artifacts. A tuner that finds something to do on every pass is not tuning, it is
    churning.
    """
    out = {"narrowed": 0, "shadowed": 0, "flagged_rederive": 0, "needs_oracle": 0, "fossils": 0,
           "flagged_suppressed": 0, "skipped": 0, "detail": [], "errors": []}
    # Real corpus prompts for the unreachable-trigger check. Fetched ONCE per pass rather than per
    # artifact — `_fetch_corpus_prompts` hits the DB and this pass walks every active artifact.
    # Empty on failure, which makes `trigger_is_unreachable` return None: undecidable, not clean.
    # THE FULL CORPUS, NOT `_fetch_corpus_prompts` — and that distinction is the difference between
    # a sound retirement signal and a coin flip. That helper is `ORDER BY random() LIMIT 150`, built  # privacy-ok: SQL LIMIT clause, not a course code
    # for the SPECIFICITY gate where a random sample is exactly right: "does this fire too often" is
    # a rate question and a sample estimates a rate.
    #
    # "Can this EVER fire" is an EXISTENCE question. A rule matching 1 prompt in 400 usually misses a
    # 150-row random draw, so the flag would flip between runs and retire working artifacts on
    # sampling luck. Caught before shipping because the first run flagged two artifacts that my own
    # full-corpus check showed matching 1 and 9 times — a false positive in the dangerous direction.
    try:
        from _env import connect_corebrain as _cc
        _c = _cc().cursor()
        # Same column correction as friction_installer._fetch_corpus_prompts — see the note there.
        # The runtime matches the CURRENT user message, which is this table's `correction_text`.
        _c.execute("SELECT COALESCE(correction_text, prompt_text) FROM pattern_observations "
                   "WHERE org_id=%s AND COALESCE(correction_text, prompt_text) IS NOT NULL "
                   "AND length(COALESCE(correction_text, prompt_text)) > 10", (org,))
        _tune_corpus = [r[0] for r in _c.fetchall()]
    except Exception:
        _tune_corpus = []
    try:
        import friction_tune as ft
        from _env import connect_corebrain
        # ABSENT INPUTS ARE REPORTED, NEVER RETURNED AS ZEROS.
        #
        # On a fresh clone or a Core that has never run the fitness pass there is no
        # contract-fitness.json and no artifacts, and the first version returned
        # {narrowed:0, shadowed:0, flagged_rederive:0, skipped:0} — identical to a healthy Core with
        # nothing to tune. That is the silent-absence shape that had four Cores reading "0 installed"
        # as health while their pipeline had never run, and it is the specific defect this whole arm
        # exists to close. It is not allowed to ship inside the fix for it.
        fitp = Path(inst.STATE) / "contract-fitness.json"
        not_binding = set()
        if not fitp.is_file():
            out["skipped"] = -1
            out["reason"] = ("NO contract-fitness.json — the fitness pass has never run on this "
                             "Core, so `not_binding` is unknown. This is an UNRUN measurement, not "
                             "an empty one: nothing can be tuned until measure-contract-fitness "
                             "produces a verdict. Not the same as 'nothing to tune'.")
            return out
        _fit = json.loads(fitp.read_text())
        not_binding = set(_fit.get("not_binding") or [])
        # Absent on a fitness file written before 2026-08-05 — treated as empty, which degrades to
        # exactly the old behaviour rather than crashing on an older Core's state file.
        not_binding_artifacts = set(_fit.get("not_binding_artifacts") or [])
        # THE SPLIT IS DERIVED, NOT EMITTED AS A SECOND KEY. measure-contract-fitness already
        # records the exact verdict per artifact (NOT-BINDING-FIRED vs NOT-BINDING-NO-FIRE) in
        # `si_artifacts`; adding parallel `not_binding_fired` / `not_binding_no_fire` keys beside the
        # union would be a third representation of one fact, which is the accretion the consolidate
        # directive names. Read the verdict where it already lives.
        #
        # A fitness file written before the split existed has rows without a NOT-BINDING-* verdict,
        # so `fired` comes out empty and every not-binding artifact degrades to flag_rederive —
        # exactly the pre-D3 behaviour. Degrading to the WEAKER action on unknown data is the right
        # direction: it queues a re-derivation for a human to read, rather than asserting a
        # compliance failure the measurement never actually established.
        not_binding_fired = {
            r.get("artifact_id") for r in (_fit.get("si_artifacts") or [])
            if str(r.get("verdict", "")) == "NOT-BINDING-FIRED" and r.get("artifact_id")}
        # Legacy slugs kept in their OWN set rather than merged into the one above. The two are
        # matched differently — artifact ids exactly, slugs by substring — and merging a substring-
        # matched slug list with opaque `art_…` hashes is the accidental-match trap the
        # not_binding/not_binding_artifacts split already exists to avoid. Same reason, same shape.
        not_binding_fired_slugs = {
            r.get("contract") for r in (_fit.get("contracts") or [])
            if str(r.get("verdict", "")) == "NOT-BINDING"
            and int(r.get("fire_count") or 0) > 0 and r.get("contract")}
        not_binding -= not_binding_fired_slugs
        _live = inst._load_active().get("artifacts", [])
        if not _live:
            out["skipped"] = -1
            out["reason"] = ("NO active artifacts on this Core — the generator has installed nothing "
                             "yet. Again an unrun pipeline rather than a quiet one.")
            return out

        conn = connect_corebrain()
        try:
            cur = conn.cursor()
            cur.execute("SET app.current_org_id = %s", (str(org),))
            acted = 0
            for art in inst._load_active().get("artifacts", []):
                aid = art.get("artifact_id", "?")
                health = classify_artifact_health(cur, org, art, not_binding,
                                                  not_binding_artifacts, not_binding_fired,
                                                  not_binding_fired_slugs, _tune_corpus)
                action = health["action"]
                if action == "none":
                    out["skipped"] += 1
                    continue
                if acted >= _MAX_TUNES_PER_CLOSE:
                    out["skipped"] += 1
                    out["detail"].append((aid, "deferred", f"per-close cap {_MAX_TUNES_PER_CLOSE}"))
                    continue

                if action == "flag_fossil":
                    # Queued, not retired. The ASK is intact and still recurring — only the trigger is
                    # worthless — so deleting the artifact on an automated pass would discard evidence
                    # a person may want to re-mint from. And since friction_router now refuses to build
                    # an off-subject trigger, retiring this case sends it back to a generator that will
                    # either produce a correct trigger or decline, which is the outcome we want.
                    if not dry:
                        spec = build_oracle_spec(art, health["why"],
                                                 _fire_count(aid, days=_HEALTH_WINDOW_DAYS))
                        spec["recommended_action"] = "retire_and_remint"
                        spec["verdict"] = "FOSSIL-TRIGGER"
                        spec["rationale"] = (
                            "The trigger cannot fire on this ask's subject. friction_router was fixed "
                            "2026-08-06 to refuse off-subject triggers, so retiring this case returns "
                            "it to a generator that will either mint a correct trigger or decline. "
                            "Retire; do not hand-patch the regex.")
                        _persist_oracle_spec(spec)
                        _log_action({"action": "tune_flag_fossil", "artifact_id": aid,
                                     "org_id": org,
                                     "trigger_terms": (health.get("fossil") or {}).get("trigger_terms"),
                                     "why": health["why"][:200]})
                    out["fossils"] = out.get("fossils", 0) + 1
                    out["detail"].append((aid, "flag_fossil:retire_and_remint", health["why"][:90]))
                    acted += 1
                    continue

                if action == "flag_needs_oracle":
                    # Deliberately NOT a demotion and NOT a quarantine. Retiring the artifact here
                    # would remove the only thing currently standing where the ask lives, on the
                    # authority of an automated pass, before anyone has read the spec. The queue is
                    # the handoff; /core-si surfaces it; a person decides. What this pass changes is
                    # that the failure is now WRITTEN DOWN with its mechanical evidence attached,
                    # instead of being counted as data staleness and re-derived into more prose.
                    # Built unconditionally so --dry-run reports the REAL recommendation. The
                    # first version hardcoded "write_oracle" on the dry path, which reported
                    # `write_oracle` for a case whose oracle is DECLARED and whose correct action is
                    # `retire` — wrong in the one mode whose whole purpose is to be read before acting.
                    fires = _fire_count(aid, days=_HEALTH_WINDOW_DAYS)
                    spec = build_oracle_spec(art, health["why"], fires)
                    rec = spec.get("recommended_action")
                    if not dry:
                        _persist_oracle_spec(spec)
                        _dec = spec.get("declared_oracle")
                        _log_action({"action": "tune_flag_needs_oracle", "artifact_id": aid,
                                     "org_id": org, "recommended": rec,
                                     "declared_oracle": _dec.get("hook") if isinstance(_dec, dict) else None,
                                     "why": health["why"][:200]})
                    out["needs_oracle"] = out.get("needs_oracle", 0) + 1
                    out["detail"].append((aid, f"flag_needs_oracle:{rec}", health["why"][:90]))
                    acted += 1
                    continue

                if action == "flag_suppressed":
                    # NOT queued for re-derivation, which is the entire point. The trigger matched,
                    # so the targeting is fine and re-minting would throw away a working one. This
                    # is surfaced for a human to find the suppression — every action in this loop
                    # is handled by an explicit branch, and an unhandled one falls silently through
                    # to `untouched`, which for a NEW state would be indistinguishable from never
                    # having computed it.
                    if not dry:
                        _log_action({"action": "tune_flag_suppressed", "artifact_id": aid,
                                     "org_id": org, "why": health["why"][:200]})
                    out["flagged_suppressed"] += 1
                    out["detail"].append((aid, "flag_suppressed", health["why"][:90]))
                    acted += 1
                    continue

                if action == "flag_rederive":
                    # NOT a narrowing and NOT a demotion. An ineffective rule is surfaced for
                    # re-derivation from the current corpus; making it quieter would hide the
                    # problem, and silently re-minting behavioural guidance without it appearing
                    # anywhere is how a loop changes what steers the agent unobserved.
                    if not dry:
                        _log_action({"action": "tune_flag_rederive", "artifact_id": aid,
                                     "org_id": org, "why": health["why"][:200]})
                        _queue_rederive(aid, health["why"])
                    out["flagged_rederive"] += 1
                    out["detail"].append((aid, "flag_rederive", health["why"][:90]))
                    acted += 1
                    continue

                if action == "narrow":
                    positives = health.get("positives") or []
                    cur.execute("SELECT COALESCE(revision,0) FROM si_artifacts "
                                "WHERE artifact_id=%s AND org_id=%s", (aid, org))
                    row = cur.fetchone()
                    prior = int(row[0]) if row else 0
                    r = ft.tune(art, positives, [], _dsl_evaluate, prior=prior)
                    if r.get("ok"):
                        if not dry:
                            _persist_tuned_spec(cur, org, aid, art, r["spec"], r.get("term"))
                        out["narrowed"] += 1
                        out["detail"].append((aid, "narrowed", r.get("term")))
                    else:
                        # Cannot narrow further -> SHADOW, which is reversible now that
                        # friction_promote.rearm_shadowed() exists. It was NOT reversible when I
                        # first wrote this, and demoting then was a slow retirement.
                        if not dry:
                            _demote_to_shadow(cur, org, aid, art, r.get("reason", ""))
                        out["shadowed"] += 1
                        out["detail"].append((aid, "shadowed", r.get("reason", "")[:90]))
                    acted += 1

            # RETRACT WHAT THE MEASUREMENT NO LONGER SUPPORTS. Runs after the classify loop, so it
            # sees exactly the sets this pass acted on rather than re-deriving them. Placed OUTSIDE
            # the per-artifact loop because a queued request survives its artifact dropping out of
            # the actionable set entirely — which is the case it exists to catch, and one no
            # per-artifact branch can reach.
            _actionable = (set(not_binding) | set(not_binding_artifacts)
                           | set(not_binding_fired) | set(not_binding_fired_slugs))
            _retracted = _retract_stale_oracle_requests(_actionable, dry=dry)
            if _retracted:
                out["oracle_retracted"] = len(_retracted)
                for _aid in _retracted:
                    out["detail"].append((_aid, "oracle_request_retracted",
                                          "verdict no longer supports the queued work order"))
            if not dry:
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        out["errors"].append(str(e)[:200])        # fail-open; never breaks a close
    return out


# ── D3: NOT-BINDING-FIRED escalates to an ORACLE SPEC ────────────────────────────────────────────
#
# WHO DECLARES COVERAGE, and why it is a hardcoded dict rather than a matcher.
#
# When an artifact needs an oracle, the first question is whether one already exists — because the
# standing directive (recurring 9x) is to consolidate rather than add a second mechanism beside a
# working one. The loop cannot answer that question by itself: deciding "this hand-written PreToolUse
# gate covers that distilled ask" is a semantic judgement, and every attempt to automate a semantic
# judgement in this subsystem is precisely what produced the artifacts D3 exists to clean up.
#
# So coverage is DECLARED, by exact case_id, by a person, once. No regex against the ask text — that
# would reintroduce keyword-guessing one level up, in the tool built to remove it. An undeclared case
# does not fall through to a guess; it produces a spec that says plainly that no oracle covers it and
# a human must either write one or add a line here. Failing to the honest state is the point.
#
# Keyed on case_id (stable across re-mints) rather than artifact_id (changes on every re-derivation).
_ORACLE_COVERAGE = {
    # The triad-orchestration ask. Its artifact fires on `\bcodex\b` AND `\bfable\b` in NICK'S
    # PROMPT — so it fires only when he has already asked for the triad, which is the one occasion the
    # reminder is unnecessary, and it is silent on every occasion it was built for. Measured
    # 2026-08-06: 4 fires, ask still recurring 1.4/wk. adversarial-review-gate.py checks the same
    # intent mechanically and at the right time: PreToolUse, on a blast-radius command, asking whether
    # a review signal exists in this turn. The keyword artifact is the redundant second mechanism.
    "fc_0146d41e14f19fa997d67005": {
        "hook": ".claude/hooks/adversarial-review-gate.py",
        "event": "PreToolUse",
        "checks": "a blast-radius command (migration / --apply / cutover) is about to execute with "
                  "no Codex or Fable review signal recorded in this turn",
        # THIS FIELD IS WHY THE RECOMMENDATION IS NOT `retire`, and it was added because the first
        # version of this entry claimed full coverage and was WRONG. Codex checked the hook instead of
        # the claim (2026-08-06) and found three gaps: the gate scopes to four blast-radius command
        # families, it runs shadow-only, and it accepts ONE reviewer signal where the ask asks for the
        # triad. So retiring the artifact would have deleted the only remaining reminder for
        # substantial work that is not blast-radius — a false retirement authorised by a coverage
        # declaration that did not hold. Same defect class as the boilerplate retirement reasons
        # sentinel-code blocked earlier the same day: asserting coverage that does not exist.
        "does_not_cover": "substantial work that is NOT one of the four blast-radius command families; "
                          "and the gate is shadow-only, so even in scope it advises rather than binds",
        "declared": "2026-08-06",
    },
    # The unsourced-financial-figure work order, filed by si-objective.propose_preempts on
    # core-life: 10 unsourced claims / 826 replies (1.21/100), threshold cleared, action
    # write_preempt_hook, must_be_handwritten. preempt-gate.py is that hook — PostToolBatch, which
    # fires after the tool batch and BEFORE the reply is composed, so a missing read can still be
    # supplied rather than punished.
    "observed_financial_figure": {
        "hook": ".claude/hooks/preempt-gate.py",
        "event": "PostToolBatch",
        "checks": "the user's prompt this turn asks for an account/brokerage figure and no live "
                  "account read appears in the turn's tool calls — judged with reply-observer's own "
                  "_SOURCE_TOOLS entry, imported rather than restated, so prevention and "
                  "measurement cannot disagree",
        # PARTIAL, AND SAID SO ON THE FIRST VERSION RATHER THAN THE SECOND. The fc_0146d41e entry
        # above claimed full coverage, was wrong, and had to be corrected after Codex read the hook
        # instead of the claim. The gap here is structural and known in advance: the trigger reads
        # NICK'S PROMPT, so a figure Core volunteers in a reply to a non-financial prompt is not
        # reached at all. Declaring that up front is what keeps the proposal alive for the
        # remainder instead of retiring it on an overstatement.
        "does_not_cover": "a financial figure Core volunteers when the operator did not ask a financial "
                          "question — the trigger is prompt-scoped, so an unprompted figure is "
                          "invisible to it; and it verifies that SOME live account read happened "
                          "this turn, not that the specific number came from it",
        # THE REMAINDER IS DECLINED, NOT DEFERRED — and declining is one of the two outcomes the
        # queue itself asks for ("write or decline the rest"). It is declined because it is
        # un-implementable at any event this harness offers, which is a fact about the events and
        # not a shortage of effort:
        #
        #   PostToolBatch — fires BEFORE the reply is composed. That is precisely what makes it the
        #     right place to supply a missing read, and precisely what makes an unprompted figure
        #     invisible: the figure does not exist yet, and nothing in the turn's tool calls
        #     distinguishes a turn that is about to volunteer one from a turn that is not.
        #   PreToolUse — same blindness, one step earlier.
        #   MessageDisplay — the only event that SEES the reply, and its authority here was tested
        #     2026-08-06 as observe-yes / act-no: a returned exit 2 is ignored. A gate there could
        #     read the figure and could not stop it.
        #   Stop — post-reply. Nick 2026-08-04, and the nine gates retired 2026-08-06 for it.
        #
        # So the remainder stays MEASURED and unenforced, which is the honest end state rather than
        # a placeholder: reply-observer still counts every unsourced financial_figure, prompted or
        # not, so if unprompted figures become a real pattern the number will say so and this
        # decision gets revisited with evidence. Reopen ONLY if an event gains reply-visibility
        # plus refusal authority.
        "declined": "the unprompted-figure remainder is not implementable at any available event — "
                    "the events that can act cannot see the reply, and the event that can see the "
                    "reply cannot act. Left measured by reply-observer, unenforced by design.",
        "declared": "2026-08-25",
        # Recorded because it changes what the originating number means. The detector that produced
        # the 1.21/100 was `\$\s?[\d,]+`, which matches `$1` — every shell positional in every awk
        # snippet this seat writes into a reply. Tightened to a currency shape the same day. The
        # hook is still right and still wanted; the RATE that justified it was inflated, so re-read
        # the post-fix window before treating this class as a live behavioural problem.
        "measurement_caveat": "detector over-counted shell positionals until 2026-08-25",
    },
}


# The REAL combinators, read off friction_dispatch.evaluate(): all / any / none. There is no `not` —
# the first version of this walker handled `not` (which the DSL does not have) and ignored `none`
# (which it does). Codex found it 2026-08-06. The consequence was specific and bad: a condition like
# {"all":[{"op":"event_is",...},{"none":[{"op":"tool_name_in",...}]}]} observes Core's TOOL CHOICE, but
# the walker saw only `event_is`, concluded "no substantive op", and filed `unconditional` as
# MECHANICAL EVIDENCE in an oracle escalation. Fabricated evidence in the one function whose whole
# claim is that its evidence is not a judgement.
_COMBINATORS = ("all", "any", "none")


def _condition_ops(cond) -> tuple[list, bool]:
    """(op names, fully_understood). Flattens an artifact condition tree.

    Returns a SECOND value because a partial read is worse than no read here. If the tree contains a
    dict this walker cannot classify — a combinator added to the DSL later, a hand-written legacy
    shape, anything — then the op list is incomplete, and any conclusion drawn from its incompleteness
    ("no substantive op", "all ops are prompt-scoped") is false. The caller abstains instead.
    """
    ops, understood = [], True
    def walk(n):
        nonlocal understood
        if isinstance(n, dict):
            if "op" in n:
                ops.append(str(n["op"]))
                return
            hit = False
            for k in _COMBINATORS:
                if k in n:
                    hit = True
                    walk(n[k])
            if not hit and n:
                understood = False   # a non-empty dict that is neither an op nor a combinator
        elif isinstance(n, list):
            for x in n:
                walk(x)
        elif n is not None:
            understood = False
    walk(cond)
    return ops, understood


# Ops that read NICK'S PROMPT. `event_is` is deliberately not here — it is a dispatch guard present
# on essentially every artifact and says nothing about which signal is being watched.
_PROMPT_SCOPED_OPS = {"prompt_regex", "prompt_contains", "prompt_matches", "prompt_len"}
_STRUCTURAL_OPS = {"event_is", "org_is", "scope_is"}


def watches_wrong_signal(art: dict) -> dict | None:
    """MECHANICAL evidence that an artifact watches the wrong signal. Not an inference.

    This is the part of D3 that is a real derivation rather than a story. The claim "inject-mode
    artifacts trigger on keywords in Nick's prompt, but the mistakes are Core's behaviours" was the
    root cause in the plan, and it is CHECKABLE: read the condition tree, and if every non-structural
    op is prompt-scoped, then the artifact provably cannot observe anything Core did. No judgement
    about the ask's meaning is required to establish that, which is why it is safe to state as fact
    in a generated spec.

    Returns the evidence, or None when the artifact already observes something other than the prompt
    (in which case its ineffectiveness has some OTHER cause and must not be mislabelled).
    """
    cond = (art.get("condition") or (art.get("spec") or {}).get("condition") or {})
    ops, understood = _condition_ops(cond)
    if not understood:
        # ABSTAIN. Emitting "prompt_only" or "unconditional" from a tree we only partly parsed would be
        # exactly the fabricated-evidence failure this function exists to avoid.
        return None
    substantive = [o for o in ops if o not in _STRUCTURAL_OPS]
    if not substantive:
        return {"ops": ops, "kind": "unconditional",
                "evidence": "the condition has no substantive op at all — it fires on the event "
                            "alone, so it cannot distinguish the occasions it was built for."}
    if all(o in _PROMPT_SCOPED_OPS for o in substantive):
        return {"ops": ops, "kind": "prompt_only",
                "evidence": f"every substantive op is prompt-scoped ({', '.join(sorted(set(substantive)))}) "
                            f"— the condition reads NICK'S WORDS and nothing Core did. It therefore "
                            f"fires when he has already said the thing (redundant) and stays silent "
                            f"when he has not (the occasion it exists for)."}
    return None


# Words that carry no topic. Present so a trigger built from the filler in a complaint ("that", "all",
# "what") is not credited as topically related to the ask just because the ask also contains filler.
_TOPICLESS = {"the", "you", "that", "not", "and", "for", "all", "just", "what", "more", "when",
              "have", "done", "this", "with", "from", "was", "are", "its", "but", "can", "how",
              "why", "who", "she", "him", "her", "will", "would", "then", "than", "them", "there"}


def _trigger_terms(cond) -> list:
    """Topical words a prompt-scoped condition actually matches on.

    STRIPS THE REGEX ESCAPES FIRST, and that line is the whole reason this is a function rather than an
    inline comprehension. Extracting words from `\bcodex\b` without stripping `\b` yields "bcodex",
    which matches nothing in any ask — my first measurement reported that 17 of 21 artifacts had
    triggers unrelated to their own ask when the true number was 3, and the other 14 were this bug. It
    flagged `\bcodex\b` against an ask containing the word "codex". A detector whose preprocessing is
    wrong produces confident, specific, entirely fictional findings, and it produces them in the exact
    format that makes them look verified.
    """
    out = []
    def walk(n):
        if isinstance(n, dict):
            if str(n.get("op", "")).startswith("prompt"):
                v = str(n.get("value") or "")
                # STRIP EVERY backslash-escape, not an enumerated list of the ones I could think of.
                # The enumerated version ("\\b","\\w","\\s","\\d","\\B") is sufficient for today's corpus —
                # audited all 98 prompt-scoped regex values across every artifact including retired ones,
                # and `\\b` is the only escape present. But "is the list complete?" is a question that
                # should not need asking again every time the generator changes, and the failure mode of
                # getting it wrong is silent: an unstripped escape leaks its letter into the term list and
                # the detector reports fictional findings in a clean-looking table. That already happened
                # once here. A rule that cannot be incomplete beats a list that is merely complete now.
                v = re.sub(r"\\.", " ", v)
                for w in re.findall(r"[A-Za-z]{3,}", v):
                    if w.lower() not in _TOPICLESS:
                        out.append(w.lower())
            for k in _COMBINATORS:
                if k in n:
                    walk(n[k])
        elif isinstance(n, list):
            for x in n:
                walk(x)
    walk(cond)
    return out


def trigger_is_unreachable(art: dict, corpus: list) -> dict | None:
    """Does this artifact's prompt condition match ANYTHING in the corpus it was mined from?

    THE RETIRE-SIDE COUNTERPART TO THE INSTALL-SIDE SENSITIVITY FLOOR (2026-08-20). Distinct from
    `trigger_is_fossil`, which asks whether the trigger is ABOUT the right subject. An artifact can
    be perfectly on-topic and still unreachable, because the terms are combined with `all` and were
    each validated separately: three words that every appear, but never together, produce a condition
    no prompt satisfies.

    core-ops measured the consequence: `art_wf72bf83d8ec6f7b5e`, `\bclaude\b AND \bchrome\b AND
    \bright\b`, ZERO fires in four days while the failure it was mined from recurred. Life then ran
    the same check on itself and found **6 of 18 prompt-conditioned artifacts match nothing in their
    own corpus** — a third of them, and more instances than the seat that reported it.

    `_wf_trigger_terms` now refuses to MINT one. Nothing retired the ones already installed, and they
    are invisible in every counter: an artifact that never fires is indistinguishable from one that
    works, which is precisely why this needed measuring rather than reasoning about.

    UNDECIDABLE ON A THIN CORPUS, and it says so rather than flagging. Below `MIN_CORPUS` a zero
    match count is as likely to mean "not enough prompts" as "unreachable condition" — finance runs
    at 36 observations, where flagging everything would retire a working artifact on no evidence.
    """
    legs = [l for l in ((art.get("condition") or {}).get("all") or [])
            if isinstance(l, dict) and l.get("op") == "prompt_regex"]
    if not legs:
        return None                      # tool/event-shaped: nothing to be unreachable ABOUT
    try:
        import friction_test_gate as _tg
        if not corpus or len(corpus) < _tg.MIN_CORPUS:
            return None                  # undecidable, not clean
    except Exception:
        return None
    import re as _re
    try:
        rx = [_re.compile(l["value"], _re.I) for l in legs]
    except Exception:
        return None                      # a malformed regex is a different defect, handled elsewhere
    hits = sum(1 for p in corpus if all(r.search(p or "") for r in rx))
    if hits:
        # REACHABLE IS NOT THE SAME AS ALIVE, and the exemplar this function was built around proves
        # it (2026-08-20, core-business raised the distinction, core-ops supplied the falsification).
        #
        # I diagnosed ops's `art_wf72bf83d8ec6f7b5e` — zero fires in four days — as an unsatisfiable
        # conjunction, from reading its condition. ops ran the check on its own seat: **it matches 2
        # of 107 prompts.** Reachable. My diagnosis was wrong, and the real explanation is a rate:
        # 1.9% of historical prompts means four days of no fires is exactly what a RARE rule looks
        # like, not a broken one.
        #
        # business then found the general shape: one of its four survivors hangs on a SINGLE corpus
        # row. On life, 6 of 19 prompt-conditioned artifacts match 2 or fewer. A binary
        # reachable/unreachable verdict treats 1 match and 18 matches identically, and an artifact
        # surviving on one row is one aged-out prompt away from being dead.
        #
        # So the count rides along on the healthy path too. It does NOT retire anything — where to
        # draw a fragility line is a judgement, and inventing a threshold here is how a measurement
        # becomes a policy nobody chose. Report it; let a human decide.
        return {"reachable": True, "hits": hits, "corpus": len(corpus), "legs": len(legs),
                "fragile": hits <= 2} if hits <= 2 else None
    return {"legs": len(legs), "corpus": len(corpus), "hits": 0,
            "terms": [l.get("value") for l in legs][:4]}


def trigger_is_fossil(art: dict) -> dict | None:
    """Does the trigger share ANY topical word with the ask it is supposed to guard?

    A SECOND DEFECT, DISTINCT FROM watches_wrong_signal AND STRICTLY WORSE. That function proves an
    artifact watches Nick's words rather than Core's behaviour, which is true of every generated
    artifact here. This catches the subset where the words are not even ABOUT the ask — where the
    generator took the salient terms from the prompt the complaint arrived in instead of from the
    distilled ask. Measured 2026-08-06, 3 of 21 live artifacts:

        "warn before usage or cost approaches a spend limit"        fires on: fucked, close
        "answer design questions independently before approval"     fires on: come, well
        "monitor external model usage during autonomous execution"  fires on: whatever, working

    A prompt-only artifact at least fires when the TOPIC comes up, so its reminder lands late but lands.
    A fossil cannot fire on its own subject at all: the spend warning arrives when Nick swears, which
    coincides with the spend moment only by accident. It is injected-token cost with no reachable
    benefit.

    ZERO is the threshold rather than a fraction, and that is what keeps the legacy contracts out.
    Their triggers are long phrase lists mined from correction text and score 0.12-0.62 — low by
    construction, not this defect. All three real cases score exactly 0.
    """
    t = set(_trigger_terms(art.get("condition")
                           or (art.get("spec") or {}).get("condition") or {}))
    if not t:
        return None                      # nothing prompt-scoped to judge
    ask = ((art.get("effect") or {}).get("message") or "").lower()
    if not ask:
        return None
    # WORD MATCHING WITH A BOUNDED INFLECTION ALLOWANCE, and both halves of that are deliberate.
    #
    # A bare substring test (`w in ask`, the first version) exculpates on a coincidence: trigger "plan"
    # matches inside "planet", "close" inside "disclose". That direction is the safe one — a fossil
    # escapes detection rather than a healthy artifact being condemned — but it is still wrong, and it
    # happens to agree with word matching on all 22 live artifacts only by luck.
    #
    # Bare word matching is not right either, and it fails in the DANGEROUS direction: trigger "install"
    # against an ask saying "installs" would be called a fossil, condemning a correctly-targeted
    # artifact. So: exact word, or the word plus one of a closed list of inflections. Explicit suffixes
    # rather than a stemmer, because a stemmer is another thing that can be subtly wrong in a detector
    # whose output is treated as mechanical evidence.
    ask_words = set(re.findall(r"[a-z]+", ask))
    _INFLECT = ("", "s", "es", "ed", "d", "ing")
    if any((w + suf) in ask_words for w in t for suf in _INFLECT):
        return None
    return {"trigger_terms": sorted(t), "shared_with_ask": [],
            "evidence": f"the trigger fires on {sorted(t)}, and not one of those words appears in the "
                        f"ask it guards. Those terms come from the prompt the complaint arrived in, "
                        f"not from the behaviour being asked for, so it cannot fire on its subject."}


def build_oracle_spec(art: dict, why: str, fires: int = 0) -> dict:
    """Emit a work order for a hand-written oracle — or a retirement, when one already covers it.

    WHY THIS IS A SPEC AND NOT AN INSTALL. friction_dispatch structurally refuses block-mode on
    PreToolUse, because PreToolUse is where the trust root lives; a learned artifact must never be
    able to mint something that runs there. That bar is correct and D3 does not lower it. So the
    output of this escalation is a reviewed work order for a person, and the queue is the handoff.
    The loop's job ends at "here is exactly what needs to exist, and here is the mechanical evidence
    that prose will not do."
    """
    aid = art.get("artifact_id", "?")
    case_id = str(art.get("case_id", ""))
    ask = (art.get("effect") or {}).get("message") or ""
    wrong = watches_wrong_signal(art)
    fossil = trigger_is_fossil(art)
    declared = _ORACLE_COVERAGE.get(case_id)

    spec = {
        "artifact_id": aid,
        "case_id": case_id,
        "ask": ask[:300],
        "verdict": "NOT-BINDING-FIRED",
        "fires": fires,
        "why": why[:300],
        "targeting_evidence": wrong,
        # Present only when it applies, so its absence is not read as "checked and clean" on an
        # artifact whose condition had nothing prompt-scoped to judge.
        **({"fossil_trigger": fossil,
            "priority": "the trigger cannot fire on its own subject — injected-token cost with no "
                        "reachable benefit until the trigger is replaced"} if fossil else {}),
        "eligible_events": ["PreToolUse", "PostToolBatch"],
        "ineligible_events": {
            "Stop": "post-reply. The operator's standing directive 2026-08-04: a gate that fires after the "
                    "reply is sent cannot prevent anything, only fail the turn afterwards.",
            "SubagentStop": "same, and it does not see the main turn's reply at all.",
        },
        "must_be_handwritten": True,
        "why_handwritten": "friction_dispatch refuses block-mode on PreToolUse by design — that is "
                           "the trust root. An oracle that PREVENTS is hand-written and reviewed, "
                           "never minted by the loop.",
    }
    if declared:
        spec["declared_oracle"] = declared
        gap = (declared.get("does_not_cover") or "").strip()
        if gap:
            # PARTIAL coverage is its own state and must not collapse into either of the other two.
            # Reading it as "covered" retires a live reminder for the uncovered remainder; reading it as
            # "uncovered" throws away a working hook and invites a duplicate. So: credit the hook for
            # its part, and ask for the remainder only.
            spec["recommended_action"] = "extend_oracle"
            spec["coverage"] = "partial"
            spec["rationale"] = (
                f"{declared['hook']} covers PART of this at {declared['event']} ({declared['checks']}), "
                f"but NOT: {gap}. Do NOT retire the artifact — that would delete the only cover for the "
                f"remainder. Either widen the existing hook or accept the gap explicitly.")
            spec["what_the_oracle_must_observe"] = (
                f"the UNCOVERED remainder only: {gap}. Reuse {declared['hook']} rather than adding a "
                f"second hook beside it.")
        else:
            spec["recommended_action"] = declared.get("action", "retire")
            spec["coverage"] = "full"
            spec["rationale"] = (
                f"{declared['hook']} already checks this mechanically at {declared['event']}: "
                f"{declared['checks']}. Two mechanisms for one ask is the accretion the consolidate "
                f"directive forbids, and the prose one is the ineffective half — retire it.")
    else:
        spec["declared_oracle"] = None
        spec["recommended_action"] = "write_oracle"
        spec["rationale"] = (
            "NO declared oracle covers this case. A human must either write the PreToolUse/"
            "PostToolBatch check and register it, or — if an existing hook already does the job — add "
            "the case_id to _ORACLE_COVERAGE in friction_loop.py. This is deliberately not guessed.")
        spec["what_the_oracle_must_observe"] = (
            "a mechanical condition on Core's own turn — the tool calls made, the files written, the "
            "commands about to run. NOT text in the operator's prompt: that is the failure being escalated.")

    return spec


def _persist_oracle_spec(spec: dict) -> None:
    """Write the work order. Separated from building it so --dry-run computes a real recommendation
    without touching state — the property the dry path was missing."""
    p = Path(inst.STATE) / "oracle-request-queue.json"
    try:
        q = json.loads(p.read_text()) if p.is_file() else []
    except Exception:
        q = []
    q = [x for x in q if x.get("artifact_id") != spec.get("artifact_id")]
    q.append(spec)
    p.write_text(json.dumps(q, indent=1))


def _retract_stale_oracle_requests(actionable: set, dry: bool = False) -> list:
    """Withdraw queued work orders whose verdict no longer supports them.

    WHY THIS EXISTS (2026-08-12). `_persist_oracle_spec` upserts — it replaces an entry for the same
    artifact and appends — so the queue never accumulated duplicates. That is why 219
    `tune_flag_needs_oracle` rows in the action log resolved to only 2 queue entries, and it is worth
    stating plainly because I twice described those 219 as "downstream actions": they were 219
    RECOMPUTATIONS of the same 2 work orders, not 219 divergent ones. The cost was compute and log
    noise, not a flood of duplicate work.

    But upsert is not retraction. Nothing removed an entry once the evidence for it went away, and
    moving the MIN_PRE_N floor into the verdict earlier today did exactly that: both queued requests

        art_97b6fff21bdf97478d45   queued NOT-BINDING-FIRED  ->  now INSUFFICIENT-UNDERPOWERED
        legacy_plan-not-execute    queued NOT-BINDING-FIRED  ->  now INSUFFICIENT-UNDERPOWERED

    are work orders the measurement no longer supports, sitting in the queue a human and `/core-si`
    both read. A fix that silently leaves behind the instructions it invalidated is half a fix, and
    "the instrument stopped saying it but the work order still stands" is the same
    stale-authority shape core-finance named tonight: an artifact stays authoritative after the
    thing it was derived from stops agreeing with it.

    RETRACTS LOUDLY. Each removal is logged with the artifact and the reason, so a queue that
    empties is distinguishable from a queue nobody wrote to — the distinction this whole night has
    been about. `legacy_` ids are normalised because the queue stores `legacy_plan-not-execute`
    while the actionable set holds the bare slug.
    """
    p = Path(inst.STATE) / "oracle-request-queue.json"
    try:
        q = json.loads(p.read_text()) if p.is_file() else []
    except Exception:
        return []
    if not isinstance(q, list):
        return []

    def _live(entry) -> bool:
        aid = str(entry.get("artifact_id") or "")
        return aid in actionable or aid.replace("legacy_", "") in actionable

    keep = [x for x in q if _live(x)]
    dropped = [str(x.get("artifact_id") or "?") for x in q if not _live(x)]
    if dropped and not dry:
        # BOTH the log write and the file write are gated on `dry`, and the log is gated for the
        # same reason the file is. The first version logged unconditionally, so a PREVIEW run
        # appended `oracle_request_retracted` rows for retractions that never happened — an action
        # log asserting work that was not done, which is the defect this file exists to measure,
        # and it would have fed those rows to the loop that learns from them. Caught by sentinel on
        # the push review; every sibling branch in tune_pass already wraps _log_action in
        # `if not dry`, so this was also the only inconsistent one.
        for aid in dropped:
            _log_action({"action": "oracle_request_retracted", "artifact_id": aid,
                         "why": "the verdict that justified this work order no longer holds — most "
                                "often downgraded to INSUFFICIENT-UNDERPOWERED once the MIN_PRE_N "
                                "floor moved into the verdict. Retracted rather than left standing."})
        p.write_text(json.dumps(keep, indent=1))
    return dropped


def _queue_rederive(aid: str, why: str) -> None:
    """Surface an ineffective artifact for re-derivation where a human and /core-si both see it."""
    p = Path(inst.STATE) / "rederive-queue.json"
    try:
        q = json.loads(p.read_text()) if p.is_file() else []
    except Exception:
        q = []
    if not any(x.get("artifact_id") == aid for x in q):
        q.append({"artifact_id": aid, "why": why[:300]})
        p.write_text(json.dumps(q, indent=1))


def refresh_workflow_payloads(org: int, dry: bool = False) -> dict:
    """PHASE D1/D2 — keep a workflow-backed artifact's payload CURRENT with the brain.

    The join the plan asked for: an artifact stops being frozen at mint time and follows the brain
    subject it came from. A workflow's steps can change — a later session refines the sequence, or a
    step is corrected — and before this, the installed artifact kept injecting the wording captured
    the first time. `case_id` is the join key (`wf_<entity_id>`); no new column is needed, which is
    why D1's `subject_key` is not added here.

    DELIBERATE DEVIATION FROM THE PLAN'S D2, and the reason matters. The plan says
    "friction_dispatch MAY CONSULT THE BRAIN when firing." I am not doing that. friction-dispatch now
    runs on PreToolUse, before every Write/Edit; putting a Postgres round-trip there means a DB that
    is slow, locked or down stalls or delays every file edit in the session. Codex flagged exactly
    this class on the E1 review (the registration had no timeout) and it was right. Refreshing at
    CLOSE achieves the same freshness with none of that risk, and preserves the property that makes
    the payload trustworthy: the sha256 pin. Re-render, re-write, re-pin — so
    `_payload_verified` still fails closed on tampering, which a fire-time brain read would have had
    to abandon.
    """
    out = {"checked": 0, "refreshed": 0, "unchanged": 0, "detail": [], "errors": []}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain-pg"))
        from _env import connect_corebrain
        active = inst._load_active().get("artifacts", [])
        wf_arts = [a for a in active
                   if a.get("type") == "hooked_skill" and str(a.get("case_id", "")).startswith("wf_")]
        if not wf_arts:
            return out
        conn = connect_corebrain()
        try:
            cur = conn.cursor()
            cur.execute("SET app.current_org_id = %s", (str(org),))
            for art in wf_arts:
                out["checked"] += 1
                aid = art.get("artifact_id")
                try:
                    eid = int(str(art["case_id"]).split("_", 1)[1])
                except Exception:
                    out["errors"].append({"artifact_id": aid, "why": "unparseable case_id"})
                    continue
                cur.execute("SELECT name, COALESCE(compiled_truth_md,'') FROM entities "
                            "WHERE id=%s AND kind='Workflow'", (eid,))
                row = cur.fetchone()
                if not row:
                    # The workflow is gone from the brain but its behaviour is still installed.
                    # Reported, never silently retired — retirement is the watchdog's job and a
                    # surprise deletion here would be indistinguishable from a bug.
                    out["errors"].append({"artifact_id": aid,
                                          "why": f"brain Workflow {eid} no longer exists; artifact "
                                                 f"still installed and still firing"})
                    continue
                name, truth = row
                cur.execute("SELECT step_index, action, tool_hint FROM workflow_steps "
                            "WHERE workflow_entity_id=%s ORDER BY step_index", (eid,))
                steps = cur.fetchall()
                if len(steps) < 2:
                    out["errors"].append({"artifact_id": aid, "why": "brain now has <2 steps"})
                    continue
                fresh = _render_workflow_body(name, truth, steps)
                current = ""
                try:
                    current = inst._procedure_path(aid).read_text(errors="ignore")
                except Exception:
                    pass
                if fresh.strip() == current.strip():
                    out["unchanged"] += 1
                    continue
                if dry:
                    out["detail"].append((aid, "would refresh", f"{len(steps)} steps"))
                    out["refreshed"] += 1
                    continue
                pay = inst.write_procedure(aid, fresh)
                if not (isinstance(pay, dict) and pay.get("sha256")):
                    out["errors"].append({"artifact_id": aid, "why": f"re-write failed: {pay}"})
                    continue
                # Re-pin: the spec's hash must match the file we just wrote, or the artifact goes
                # inert at its next fire (fail-closed) — which is precisely the silent-death mode
                # find_inert_artifacts() now reports.
                r = inst.repin_payload(aid, pay) if hasattr(inst, "repin_payload") else None
                if r is None:
                    _repin_active(aid, pay, fresh)
                out["refreshed"] += 1
                out["detail"].append((aid, "refreshed", f"{len(steps)} steps"))
        finally:
            conn.close()
    except Exception as e:
        out["errors"].append({"why": str(e)[:200]})
    return out


def _repin_active(aid: str, pay: dict, body: str) -> None:
    """Update the installed spec's payload block and injected message in place, atomically."""
    import tempfile, os as _os
    p = inst.ACTIVE
    d = json.loads(p.read_text())
    for a in d.get("artifacts", []):
        if a.get("artifact_id") == aid:
            a["payload"] = pay
            eff = a.get("effect") or {}
            eff["message"] = body[:2000]      # E2: the message IS the body
            a["effect"] = eff
    tmp = tempfile.NamedTemporaryFile("w", dir=str(p.parent), delete=False)
    try:
        json.dump(d, tmp, indent=1)
        tmp.flush(); _os.fsync(tmp.fileno()); tmp.close()
        _os.replace(tmp.name, p)
    except Exception:
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass
        raise


def status() -> dict:
    active = inst._load_active().get("artifacts", [])
    return {"active_artifacts": len(active),
            "by_event": {e: sum(1 for a in active if a.get("event") == e)
                         for e in ("UserPromptSubmit", "PreToolUse", "Stop")},
            "ids": [a.get("artifact_id") for a in active][:12]}


def _print_funnel(f: dict) -> None:
    """Human-readable form of `run()`'s funnel dict — the readout that has never existed. The
    JSON in `out["funnel"]` (below) is the same numbers; this is for a person to read at a glance."""
    print(f"\nFRICTION FUNNEL — {f['days']}d window{' (DRY)' if f['dry'] else ''}")
    print(f"  mined ......... {f['mined']:>4}   ineligible .... {f['ineligible']:>4}"
          f"   eligible ...... {f['eligible']:>4}")
    print(f"  duplicate_ask . {f['duplicate_ask']:>4}   awaiting_ask .. {f['awaiting_ask']:>4}"
          f"   denied ........ {f['denied']:>4}")
    print(f"  cap_denied .... {f['cap_denied']:>4}   routed ........ {f['routed']:>4}"
          f"   gate_passed ... {f['gate_passed']:>4}")
    print(f"  gate_failed ... {f['gate_failed']:>4}   installed ..... {f['installed']:>4}"
          f"   install_failed  {f['install_failed']:>4}")
    if f["dry"]:
        print(f"  would_install . {f['would_install']:>4}   (dry — nothing above this line was "
              f"written to friction_cases)")
    for bucket in ("denied_reasons", "cap_denied_reasons", "gate_failed_reasons", "install_failed_reasons"):
        top = f.get(bucket) or {}
        if top:
            print(f"  top {bucket}: " + ", ".join(f"{k!r}:{v}" for k, v in top.items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="one-time: mark friction_cases 'installed' where a real si_artifacts "
                         "row (generator_version=friction-router/3) already proves it. Idempotent.")
    args = ap.parse_args()
    if args.status:
        print(json.dumps(status(), indent=2)); return 0
    if args.backfill:
        print(json.dumps(backfill_installed_from_artifacts(get_org_id(), dry=args.dry), indent=2))
        return 0
    out = run(args.days, args.dry)
    _print_funnel(out["funnel"])
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
