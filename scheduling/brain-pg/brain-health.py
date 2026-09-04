#!/usr/bin/env python3
"""brain-health.py — standing reliability check for the Core brain.

Asserts every invariant the brain must hold to be trusted, with PASS/WARN/FAIL.
Built 2026-05-31 after a session found the recall-trigger hook silently dead and
recall returning stale data — the recurring "fixed but never verified to keep
working" pattern (see tasks/research/brain-reliability-audit-2026-05-31.md).
The point: the brain proves itself every session instead of us assuming it works.

Run:
  python3 brain-health.py            # human report; exit 1 if any FAIL
  python3 brain-health.py --json     # machine output
  python3 brain-health.py --quiet    # one-line summary (for SessionStart/close)

Checks (FAIL = broken, WARN = known-open defect being tracked, PASS = verified):
  plumbing : embedding coverage · GUC registered · orgs populated · graph fresh ·
             ingestion current · recall-hook liveness
  recall   : recency works on recent-queries (D1 regression) · non-recency unchanged
  tracked  : entity<->evidence linkage (D4) · bi-temporal supersede (D5) ·
             entity fragmentation (D3)  — WARN until fixed, so they can't be forgotten
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "bin"))
import core_seat as _core_seat  # noqa: E402 — canonical seat/slug resolver
_env.load_secrets()

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
STATE_DIR = INSTANCE / ".claude" / "state"
BRAIN = Path(os.environ.get("CORE_BRAIN") or (INSTANCE.parent / "core-brain"))
GRAPH_JSON = BRAIN / "_build" / "output" / "graphify-out" / "graph.json"

results = []  # (name, status, detail)  status in {PASS, WARN, FAIL}


def check(name, status, detail):
    results.append((name, status, detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    today = dt.date.today()
    conn = _env.connect_corebrain()
    cur = conn.cursor()

    def scalar(sql, p=()):
        cur.execute(sql, p); return cur.fetchone()

    # ── PLUMBING ──────────────────────────────────────────────────────────
    # `Source` entities are origin-backbone hubs (name = a source-file path, not
    # semantic content) — embed.py creates them with a DELIBERATELY NULL embedding
    # (see embed.py origin/graph-nodes passes). They are reachable via the graph +
    # origin edges, never meant to be vector-searchable, so they must be EXCLUDED
    # from embedding coverage — otherwise a growing origin backbone permanently
    # reds this check for a non-problem (20,276 Source NULLs, 0 content NULLs at
    # 2026-07-10). Only content entities (kind <> 'Source') require an embedding.
    for t, extra in (("entities", "AND kind <> 'Source'"), ("evidence", "")):
        nul, tot = scalar(
            f"SELECT count(*) FILTER (WHERE embedding IS NULL {extra}), "
            f"count(*) FILTER (WHERE TRUE {extra}) FROM {t}")
        check(f"embedding_coverage:{t}", "PASS" if nul == 0 else "FAIL",
              f"{nul}/{tot} NULL embeddings" + (" (Source hubs excluded — meant NULL)" if extra else ""))

    try:
        cur.execute("SELECT current_setting('app.current_org_id', true)")
        check("guc_registered", "PASS", f"app.current_org_id={cur.fetchone()[0]}")
    except Exception as e:
        check("guc_registered", "FAIL", f"GUC crash: {e}")
        conn.rollback()

    rows = scalar  # alias
    cur.execute("SELECT org_id, count(*) FROM entities GROUP BY org_id")
    ent_by_org = dict(cur.fetchall())
    cur.execute("SELECT org_id, count(*) FROM entity_edges GROUP BY org_id")
    edge_by_org = dict(cur.fetchall())
    # EVERY TENANT, not a hardcoded (1,2,3). finance (org 4) and ops (org 5) were spawned after
    # this line was written, so an empty finance or ops partition PASSED this check for months —
    # and both were in fact empty of compiled hubs the whole time. A population check that cannot
    # see the newest Cores is exactly backwards: they are the ones most likely to be empty.
    cur.execute("SELECT org_id FROM tenants WHERE org_id <> 0 ORDER BY org_id")
    known_orgs = [r[0] for r in cur.fetchall()] or [1, 2, 3]
    missing = [o for o in known_orgs if ent_by_org.get(o, 0) == 0 or edge_by_org.get(o, 0) == 0]
    check("orgs_populated", "PASS" if not missing else "WARN",
          f"entities={ent_by_org} edges={edge_by_org}" + (f" — empty: {missing}" if missing else ""))

    if GRAPH_JSON.exists():
        age_h = (dt.datetime.now().timestamp() - GRAPH_JSON.stat().st_mtime) / 3600
        check("graph_fresh", "PASS" if age_h < 24 * 7 else "WARN",
              f"graph.json {age_h:.0f}h old")
    else:
        check("graph_fresh", "FAIL", "graph.json missing — auto-pipeline not producing it")

    newest = scalar("SELECT max(session_date) FROM evidence")[0]
    if newest:
        age_d = (today - newest).days
        check("ingestion_current", "PASS" if age_d <= 4 else "WARN",
              f"newest evidence {newest} ({age_d}d ago)")
    else:
        check("ingestion_current", "FAIL", "no evidence rows")

    # recall-hook liveness: rot-score telemetry exists but zero brain-recall fires => dead nudge
    rot = len(list(STATE_DIR.glob(".rot-score-*.json"))) if STATE_DIR.is_dir() else 0
    rec = len([p for p in STATE_DIR.glob(".brain-recall-*.json") if "test" not in p.name]) if STATE_DIR.is_dir() else 0
    if rot >= 2 and rec == 0:
        check("recall_hook_live", "FAIL", f"{rot} rot-score files but 0 brain-recall fires — recall nudge DEAD")
    else:
        check("recall_hook_live", "PASS", f"{rec} brain-recall fire-logs present")

    # ── RECALL QUALITY (D1 regression) ────────────────────────────────────
    # 2026-07-19 hardening: the old check counted "how many of the top-5 are recent" against the
    # LIVE aging corpus + real today — wall-clock-fragile (a correct recall could false-FAIL after
    # ~2-3 quiet weeks). Replaced with three wall-clock-IMMUNE invariants (Codex-flagged, D-fragile):
    #   (a) blend mechanism is deterministic (relative synthetic dates, can't decay);
    #   (b) no false-fresh — a candidate must not score ~fresh while its OWN date is old (the exact
    #       vendorvault-shipped bug class: recompile/cross-org/as_of date inflation) + dating attaches;
    #   (c) non-recency queries carry no recency blend (structural).
    conn.close()
    try:
        from query import hybrid_query, _recency_intent, _blend_recency
        tdy = dt.date.today()
        # (a) MECHANISM — a 2d item must score recent AND outrank a 90d item of HIGHER relevance.
        synth = [
            {"kind": "entity", "source": "synth-old",    "rrf_score": 0.90, "date": tdy - dt.timedelta(days=90)},
            {"kind": "entity", "source": "synth-recent", "rrf_score": 0.50, "date": tdy - dt.timedelta(days=2)},
        ]
        blended = _blend_recency([dict(s) for s in synth], 2)
        bl = {r["source"]: r for r in blended}
        mech = (bl["synth-recent"]["recency_score"] >= 0.5
                and bl["synth-old"]["recency_score"] < 0.5
                and blended[0]["source"] == "synth-recent")
        check("recall_recency_blend", "PASS" if mech else "FAIL",
              "recency blend scores+ranks a 2d item over a higher-relevance 90d item"
              if mech else "recency blend math broken (scoring or re-rank)")
        # (b) LIVE pipeline integrity — wall-clock-immune consistency checks (Codex round-2 hardened):
        #   · dating attaches to >=1 candidate (else _attach_candidate_dates is dead/bypassed);
        #   · recency was APPLIED (>=1 candidate carries a recency_score → _blend_recency ran);
        #   · no candidate is scored-recent (>=0.5) with NO date (the false-PASS hole: a defective
        #     path that scores off updated_at without attaching the date);
        #   · no gross false-fresh (>=0.9 while its OWN date is >30d old — impossible under the real
        #     21d half-life, so it flags an internal score/date inconsistency).
        # KNOWN LIMIT (documented follow-up): a WRONG attached date that is self-consistent with its
        # score (the original vendorvault class, where the bad date drove BOTH) can't be caught here
        # without a fixture row whose true content date is known — needs a seeded dated-path fixture.
        rq = "what did we ship recently"
        assert _recency_intent(rq), "recency intent not detected"
        res = hybrid_query(rq, k=8)
        dated = [r for r in res if r.get("date") is not None]
        applied = any(r.get("recency_score") is not None for r in res)
        scored_no_date = [r for r in res if (r.get("recency_score") or 0) >= 0.5 and not r.get("date")]
        ff = [r for r in res if (r.get("recency_score") or 0) >= 0.9 and r.get("date")
              and (tdy - r["date"]).days > 30]
        problems = []
        if not dated:
            problems.append("dating attached to 0 candidates (pipeline dead)")
        if not applied:
            problems.append("recency blend not applied (pipeline bypassed)")
        if scored_no_date:
            problems.append(f"{str(scored_no_date[0].get('source', '?'))[:30]} scored recent with NO date")
        if ff:
            problems.append(f"{str(ff[0].get('source', '?'))[:30]} scores {ff[0]['recency_score']} "
                            f"but is {(tdy - ff[0]['date']).days}d old")
        check("recall_recency_works", "PASS" if not problems else "FAIL",
              "dating attaches + recency applied + no false-fresh/inconsistency"
              if not problems else "; ".join(problems))
        # (c) NON-RECENCY UNTOUCHED — structural invariant.
        nr = hybrid_query("Reciprocal Rank Fusion hybrid retrieval", k=3)
        clean = all(r.get("recency_score") is None for r in nr)
        check("recall_nonrecency_untouched", "PASS" if clean else "FAIL",
              "non-recency query carries no recency blend" if clean else "recency leaked into non-recency query")
    except Exception as e:
        check("recall_recency_works", "FAIL", f"recall check errored: {e}")

    # ── TRACKED OPEN DEFECTS (WARN so they can't be forgotten) ────────────
    conn2 = _env.connect_corebrain(); c2 = conn2.cursor()

    def s2(sql, p=()):
        c2.execute(sql, p); return c2.fetchone()

    # D3: the fix is recall-layer dedup — verify the recall OUTPUT, not the DB root.
    c2.execute("""SELECT count(*) FROM (SELECT lower(name) FROM entities
                  GROUP BY lower(name) HAVING count(DISTINCT name) > 1) q""")
    frag_db = c2.fetchone()[0]
    try:
        from query import hybrid_query as _hq, _norm_name as _nn
        norms = [_nn(r.get("source", "")) for r in _hq("brain vault", k=8)]
        out_dups = len(norms) - len(set(norms))
        check("D3_recall_variant_dedup", "PASS" if out_dups == 0 else "FAIL",
              f"{out_dups} case-variant dup results in probe top-8 (DB root: {frag_db} variant "
              f"groups — canonicalization at extraction is a tracked follow-up)")
    except Exception as e:
        check("D3_recall_variant_dedup", "FAIL", f"dedup check errored: {e}")

    # D5: in-place upsert (ON CONFLICT DO UPDATE) means no versions to supersede, so
    # valid_until is intentionally unused; recall_at filters by valid_from. By design.
    check("D5_bitemporal_model", "PASS",
          "in-place update model — valid_until intentionally unused (recall_at filters valid_from); not a defect")

    # D4: evidence.entity_id is unread by recall — it's the hook for the FROZEN
    # corroboration engine. Deferred with corroboration, not a reliability bug.
    linked, tot = s2("SELECT count(*) FILTER (WHERE entity_id IS NOT NULL), count(*) FROM evidence")
    # ── SUPERSESSION INTEGRITY (2026-07-25) ───────────────────────────────
    # A (subject_key, predicate) pair states the CURRENT value of one thing, so more than one
    # ACTIVE row on that pair means recall can hand back a reversed decision with the same
    # authority as the live one. That is not hypothetical: on 2026-07-25 org 1 held ONE
    # supersedes relation across ~470 assertions, with headless-vs-in-session extraction and
    # detached-vs-synchronous close both sitting 'active'. It misled a full session before
    # anyone noticed, because nothing ever looked.
    #
    # assertions_ingest.py now auto-retires the older row on every same-subject write, so this
    # check should read 0. A non-zero count means either the auto-supersession path regressed,
    # or a subject_key is overloaded (holding two genuinely different facts under one label) —
    # in which case the fix is to SPLIT the key, not to delete a record.
    try:
        _org = _env.get_org_id()
        contra = s2("""SELECT count(*) FROM (
                         SELECT subject_key, predicate FROM assertions
                         WHERE org_id=%s AND lifecycle_status='active'
                         GROUP BY subject_key, predicate HAVING count(*) > 1) t""", (_org,))[0]
        rels = s2("""SELECT count(*) FROM assertion_relations
                     WHERE org_id=%s AND relation='supersedes'""", (_org,))[0]
        check("supersession_integrity", "PASS" if contra == 0 else "WARN",
              f"{contra} subject+predicate pair(s) with >1 ACTIVE assertion; "
              f"{rels} supersedes relation(s) recorded"
              + ("" if contra == 0 else " — overloaded subject_key: SPLIT it, don't delete a record"))
    except Exception as e:
        check("supersession_integrity", "WARN", f"supersession check errored: {e}")

    check("D4_corroboration_linkage", "WARN",
          f"{linked}/{tot} evidence linked — DEFERRED (frozen corroboration-prep; unread by recall, not a reliability bug)")

    # ── CAPTURE LIVENESS — silent-Core detector (2026-07-26) ──────────────
    # A Core can be ACTIVE (sessions running daily) while its evidence stream is
    # silently dead — core-ops ran 13 sessions over 2 weeks while a routing bug
    # (export.py missing its CWD rule) sent every file to life/org1, and NOTHING
    # noticed until a human did. This check compares, per tenant, the newest
    # session JSONL mtime (activity) against the newest evidence row (capture).
    # An active Core whose evidence lags >5d is WARN, >10d is FAIL. A Core with
    # no recent activity is fine (nothing to capture) — the signal is the GAP,
    # not the age. Skips tenants with no JSONL dir (never opened on this Mac).
    try:
        c2.execute("SELECT org_id, name FROM tenants ORDER BY org_id")
        tenants = c2.fetchall()
        silent, lagging, live = [], [], []
        for oid, name in tenants:
            proj_dir = Path.home() / "AI Projects" / f"core-{name}"
            jsonl_dir = _core_seat.transcripts_dir(proj_dir)
            if not jsonl_dir.is_dir():
                continue
            jsonls = list(jsonl_dir.glob("*.jsonl"))
            if not jsonls:
                continue
            act_age = min((dt.datetime.now().timestamp() - p.stat().st_mtime) / 86400 for p in jsonls)
            if act_age > 14:  # dormant Core — no capture expected
                continue
            newest_ev = s2("SELECT max(session_date) FROM evidence WHERE org_id=%s", (oid,))[0]
            gap = (today - newest_ev).days if newest_ev else 999
            tag = f"{name}(org{oid}): active {act_age:.0f}d ago, newest evidence " + \
                  (f"{gap}d old" if newest_ev else "NONE")
            if gap >= 10:
                silent.append(tag)
            elif gap >= 5:
                lagging.append(tag)
            else:
                live.append(name)
        if silent:
            check("capture_liveness", "FAIL",
                  "SILENT CORE — active sessions but dead evidence stream: " + "; ".join(silent))
        elif lagging:
            check("capture_liveness", "WARN", "capture lagging: " + "; ".join(lagging))
        else:
            check("capture_liveness", "PASS",
                  f"all active Cores capturing (live: {', '.join(live) or 'none active'})")
    except Exception as e:
        check("capture_liveness", "WARN", f"liveness check errored: {e}")
    conn2.close()

    # ══ ORGANISATION CHECKS (2026-08-28) ══════════════════════════════════
    # Added after a night that found three subsystems built-but-never-wired. The check suite
    # reported 14 PASS · 0 FAIL over a graph where 99.2% of entities had never been compiled and
    # cross-Core bridging had been frozen for 52 days. Nothing here was WRONG — the narrow things
    # each had a precise instrument, and the wide things had none. These are the missing ones.
    # Without them, tonight's repairs regress silently, which is exactly how it got here.

    ORGS = {1: "life", 2: "business", 3: "school", 4: "finance", 5: "ops"}
    # conn/cur are closed further up; these checks own their own connection.
    # Codex 2026-08-28 (HIGH): the first cut set conn=cur=None on failure, then every handler
    # called conn.rollback() in its except — an AttributeError on None that crashed the process
    # while trying to report the failure, so no report was emitted at all. _rb() is the guard.
    conn = cur = None
    try:
        conn = _env.connect_corebrain()
        cur = conn.cursor()
    except Exception as _e:
        check("organisation_checks", "WARN", f"no connection: {_e}")
        try:
            if conn: conn.close()   # cursor failed after connect — do not leak it
        except Exception:
            pass
        conn = cur = None

    def _rb():
        try:
            if conn: conn.rollback()
        except Exception:
            pass

    # COMPILE COVERAGE — an entity's compiled_truth_md is written once at extraction. If nothing
    # re-synthesises it, every hub keeps its birth summary forever. Measured 2026-08-28:
    # life 1.70%, and business/school/finance/ops at exactly 0.00%.
    try:
        assert cur is not None
        cov, worst = [], 1.0
        for org, nm in ORGS.items():
            cur.execute("SELECT count(*), count(last_compiled_at) FROM entities "
                        "WHERE org_id=%s AND kind<>'Source' AND valid_until IS NULL", (org,))
            tot, comp = cur.fetchone()
            pct = (comp / tot) if tot else 1.0
            worst = min(worst, pct)
            cov.append(f"{nm} {pct*100:.1f}%")
        detail = " · ".join(cov)
        if worst == 0:
            check("compile_coverage", "FAIL", f"a Core has NEVER compiled a hub — {detail}")
        elif worst < 0.10:
            check("compile_coverage", "WARN", f"hub summaries frozen at birth — {detail}")
        else:
            check("compile_coverage", "PASS", detail)
    except Exception as e:
        _rb(); check("compile_coverage", "WARN", f"errored: {e}")

    # CROSS-CORE BRIDGE FRESHNESS — corroborate.py wires the same_as edges that let a query in one
    # Core see what another knows. It was unwired for 52 days and nobody noticed, because a bridge
    # that stops being built looks identical to a bridge with nothing to add.
    try:
        assert cur is not None
        cur.execute("SELECT max(created_at)::date, count(*) FROM entity_edges "
                    "WHERE edge_type='same_as'")
        last, n = cur.fetchone()
        if last is None:
            check("crosscore_bridge", "FAIL", "no same_as edges — corroborate.py has never run")
        else:
            age = (dt.date.today() - last).days
            msg = f"{n:,} edges, newest {last} ({age}d ago)"
            check("crosscore_bridge", "FAIL" if age > 14 else
                  ("WARN" if age > 3 else "PASS"), msg)
    except Exception as e:
        _rb(); check("crosscore_bridge", "WARN", f"errored: {e}")

    # WITHIN-CORE DUPLICATION — UNIQUE(org_id, kind, name) makes kind part of identity and name
    # case-sensitive, so `Core` the Entity, `Core` the Project and `core` are three identities.
    # CROSS-org duplication is NOT counted here: the same concept in life and business is the
    # partition model working, and same_as bridges it deliberately. Counting it was my own error
    # on 2026-08-28 — 9,364 of 9,463 "duplicates" were legitimate partition boundaries.
    try:
        assert cur is not None
        # valid_until IS NULL — count LIVE rows only. Without it this counted tombstones from the
        # 2026-08-28 canonical merge and reported 985 unchanged while four Cores had gone to zero:
        # an instrument that cannot see its own repair is worse than no instrument.
        # Normalise SEPARATORS as well as case (2026-08-29). Using bare lower(name) this reported
        # 564 unchanged while four Cores had gone to zero, because `core-business` and
        # `Core business` are the same subject and lower() alone keeps them apart. Third time in
        # one night an instrument could not see its own repair; regexp_replace matches what
        # canonical-merge and compile-truth's partition step both already use.
        cur.execute("""SELECT count(*) FROM (
                         SELECT org_id, lower(regexp_replace(name, '[\\s_-]+', ' ', 'g'))
                         FROM entities
                         WHERE kind<>'Source' AND valid_until IS NULL
                         GROUP BY 1,2 HAVING count(*)>1) t""")
        dup = cur.fetchone()[0]
        cur.execute("""SELECT org_id, lower(regexp_replace(name, '[\\s_-]+', ' ', 'g')) nm,
                              count(*) c FROM entities
                       WHERE kind<>'Source' AND valid_until IS NULL
                       GROUP BY 1,2 HAVING count(*)>1 ORDER BY c DESC LIMIT 1""")
        w = cur.fetchone()
        worst_s = f"; worst {ORGS.get(w[0], w[0])}/{w[1]} ×{w[2]}" if w else ""
        check("within_core_duplicates", "WARN" if dup > 200 else "PASS",
              f"{dup} same-name groups inside one Core{worst_s}")
    except Exception as e:
        _rb(); check("within_core_duplicates", "WARN", f"errored: {e}")

    # HUB RELEVANCE — rank is raw lifetime degree with no decay, so retired work keeps its
    # centrality. Job Hunter (killed 2026-08-05) was the 4th-largest hub in life; Core UI
    # (archived 2026-05-06) was 6th. This reports the oldest-touched entity in the top 20.
    try:
        assert cur is not None
        cur.execute("""SELECT e.name, count(g.*) d, max(e.updated_at)::date u
                       FROM entities e JOIN entity_edges g
                         ON g.from_entity_id=e.id OR g.to_entity_id=e.id
                       WHERE e.kind<>'Source' AND e.valid_until IS NULL GROUP BY e.id, e.name
                       ORDER BY d DESC LIMIT 20""")
        rows = cur.fetchall()
        stale = [(n, u) for n, d, u in rows if u and (dt.date.today() - u).days > 45]
        if stale:
            check("hub_relevance", "WARN",
                  f"{len(stale)}/20 top hubs untouched >45d — oldest: "
                  + ", ".join(f"{n} ({u})" for n, u in stale[:3]))
        else:
            check("hub_relevance", "PASS", "top-20 hubs all touched within 45d")
    except Exception as e:
        _rb(); check("hub_relevance", "WARN", f"errored: {e}")

    try:
        if conn: conn.close()
    except Exception:
        pass

    # ── REPORT ────────────────────────────────────────────────────────────
    n_fail = sum(1 for _, s, _ in results if s == "FAIL")
    n_warn = sum(1 for _, s, _ in results if s == "WARN")
    n_pass = sum(1 for _, s, _ in results if s == "PASS")

    if args.json:
        print(json.dumps({"summary": {"pass": n_pass, "warn": n_warn, "fail": n_fail},
                          "checks": [{"name": n, "status": s, "detail": d} for n, s, d in results]}, indent=2))
    elif args.quiet:
        print(f"brain-health: {n_pass} PASS · {n_warn} WARN · {n_fail} FAIL"
              + ("" if n_fail == 0 else " — " + ", ".join(n for n, s, _ in results if s == "FAIL")))
    else:
        icon = {"PASS": "✅", "WARN": "🟡", "FAIL": "❌"}
        print("═══ BRAIN HEALTH ═══")
        for n, s, d in results:
            print(f"  {icon[s]} {n:34} {d}")
        print(f"\n  {n_pass} PASS · {n_warn} WARN · {n_fail} FAIL")
        if n_fail == 0 and n_warn == 0:
            print("  Brain verified healthy — tried, tested, true.")
        elif n_fail == 0:
            print("  No breakage. WARNs are tracked open defects (D3/D4/D5), not regressions.")
        else:
            print("  ❌ BREAKAGE DETECTED — a verified invariant regressed. Fix before trusting recall.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
