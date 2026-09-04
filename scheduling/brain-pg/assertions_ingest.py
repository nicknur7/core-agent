#!/usr/bin/env python3
"""assertions_ingest.py — write extracted decision assertions into the DB (unified redesign, step ②).

Plan: PART 7.2.2. The PARENT (a live session) fans out Sonnet subagents to read each decision and
return structured assertions; this module ingests that JSON (dispatch-verify: parent writes, not the
subagent). It writes candidate assertions + candidate/accepted supersede links, and completes the
decision's 'semantically_interpreted' ledger job.

Input JSON (one object per decision):
  {"decision_id":"d_...", "assertions":[
      {"subject_key":"pretooluse-guard sync policy","predicate":"is",
       "object":"shared (not per_core_keep)","applicability":{"target_org_ids":[1]},
       "confidence":0.9,
       "reverses":{"subject_key":"pretooluse-guard sync policy","explicit":true}}   # optional
   ]}

Supersession: a 'reverses' with explicit=true on a same-subject prior assertion → an ACCEPTED
supersedes relation (Codex: explicit "replace X with Y" may auto-accept) and the prior is marked
lifecycle_status='superseded'. Non-explicit → CANDIDATE relation, both stay active (conflict-visible).
All within one org (the same-org DB trigger enforces it).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
from decisions_segment import DECISION_PROCESSOR_VERSION, decision_date_map  # noqa: E402
from _env import connect_corebrain, get_org_id  # noqa: E402


def _canonical_hash(subject_key: str, predicate: str, object_str: str) -> str:
    return ledger.content_hash_of(f"{subject_key}\x1f{predicate}\x1f{object_str}")


def _latest_revision_for_decision(cur, org: int, decision_id: str) -> int | None:
    cur.execute(
        """SELECT r.id FROM source_revisions r JOIN sources s ON s.id=r.source_id
           WHERE s.org_id=%s AND s.source_key=%s ORDER BY r.revision_seq DESC LIMIT 1""",
        (org, decision_id))
    row = cur.fetchone()
    return row[0] if row else None


def _find_prior_active(cur, org: int, subject_key: str, exclude_id: int) -> int | None:
    """Most recent active accepted-or-candidate assertion with the same subject, other than this one."""
    cur.execute(
        """SELECT id FROM assertions
           WHERE org_id=%s AND subject_key=%s AND lifecycle_status='active' AND id<>%s
           ORDER BY recorded_at DESC LIMIT 1""",
        (org, subject_key, exclude_id))
    row = cur.fetchone()
    return row[0] if row else None


def _find_stale_same_subject(cur, org: int, subject_key: str, predicate: str,
                             exclude_id: int, exclude_rev: int | None) -> list[int]:
    """AUTO-SUPERSESSION (2026-07-25): every OTHER active assertion on the same
    (subject_key, predicate), from a different source revision.

    Why this exists: supersession used to fire ONLY when the extractor LLM happened to emit a
    `reverses` block, and even then only marked the prior dead when explicit=true AND
    confidence>=0.8. Everything else left BOTH rows active — so recall returned a reversed
    decision with the same authority as the live one. Measured 2026-07-25: org 1 held exactly ONE
    supersedes relation across ~470 assertions, while known-reversed pairs (headless-vs-in-session
    extraction, detached-vs-synchronous close) both sat 'active'. That misled a full session.

    A (subject_key, predicate) pair is single-valued by construction — it states the CURRENT value
    of one thing ("close-cycle model" IS x). So a newer assertion on that pair always replaces the
    older one; recency wins, no LLM judgment required. Same-revision rows are excluded so one
    decision emitting several assertions never cannibalises itself.
    """
    cur.execute(
        """SELECT id FROM assertions
           WHERE org_id=%s AND subject_key=%s AND predicate=%s
             AND lifecycle_status='active' AND id<>%s
             AND source_revision_id IS DISTINCT FROM %s
           ORDER BY recorded_at DESC""",
        (org, subject_key, predicate, exclude_id, exclude_rev))
    return [r[0] for r in cur.fetchall()]


def _same_subject_other_predicate(cur, org: int, subject_key: str, predicate: str,
                                 exclude_id: int) -> list:
    """Active rows on this subject_key under a DIFFERENT predicate — the silent-miss shape.

    WHY (2026-08-28, found live by core-business on its own close). Supersession keys on the PAIR
    (subject_key, predicate). close-core.md told the assertion worker only that "subject_key MUST be
    reused", so a worker writing a STATE CHANGE naturally gave the reversing assertion a new
    predicate — "ends this session OFF" replacing "is restored and load-bearing". Same subject,
    different predicate, no competition detected, nothing superseded. BOTH rows stayed active, and
    recall would have returned the reversed decision as current. That is exactly the failure
    auto-supersession was built in July to end, arriving through the one door it does not cover.

    The doc is fixed, but a doc is advisory and this is not: a mis-written pair now announces itself
    at the moment it is written, instead of sitting active until someone queries for it.

    REPORTS, NEVER ACTS. Two active rows on one subject under different predicates are usually
    CORRECT — a subject legitimately has many facts ("X is-a hook", "X fires-on Stop"). Only a human
    or a later decision can tell an independent fact from a botched reversal, so auto-retiring here
    would delete real knowledge to fix a reporting problem. business's own query returns 107
    candidates on core-life alone, which is precisely why it is a candidate list and not a finding
    list.
    """
    cur.execute(
        """SELECT id, predicate FROM assertions
           WHERE org_id=%s AND subject_key=%s AND predicate<>%s
             AND lifecycle_status='active' AND id<>%s
           ORDER BY recorded_at DESC LIMIT 5""",
        (org, subject_key, predicate, exclude_id))
    return cur.fetchall()


def _existing_identical(cur, org: int, canonical_hash: str) -> int | None:
    """Idempotency: an active row with this exact (subject, predicate, object) already exists.

    Without this, re-ingesting the same decision would mint a duplicate AND auto-supersede its own
    twin, churning the ledger on every replay.
    """
    cur.execute(
        """SELECT id FROM assertions
           WHERE org_id=%s AND canonical_hash=%s AND lifecycle_status='active'
           ORDER BY recorded_at DESC LIMIT 1""",
        (org, canonical_hash))
    row = cur.fetchone()
    return row[0] if row else None


def ingest(records: list[dict]) -> dict:
    org = get_org_id()
    conn = connect_corebrain()
    stats = {"assertions": 0, "supersedes": 0, "candidates": 0, "jobs_done": 0, "skipped": 0}

    # effective_from — WRITE IT. This INSERT omitted the column from 2026-07-18 (when the
    # migration declared it) until 2026-07-29, so every assertion ingested in that window
    # was undated: 315 rows on this Core alone, 44% of the active set.
    #
    # A NULL here is not a harmless blank. query.py ranks recency with
    # `0.15 / (1 + age(COALESCE(effective_from, now())))`, so a NULL scores as though the
    # decision were made THIS INSTANT and takes the MAXIMUM boost — strictly above every
    # correctly-dated row. Undated assertions therefore OUTRANKED dated ones, and the
    # older a real decision was, the more surely a dateless one buried it.
    #
    # That is not theoretical: `sync-trust-root-paths`, asserting a policy Nick reversed on
    # 2026-07-10, outranked the three dated 2026-07-10 rows recording the reversal.
    # core-business recalled the stale one, took it for current, and reported a fabricated
    # trust-root security hole. Meanwhile start_brief.py filters `IS NOT NULL`, so the same
    # rows were invisible to the startup brief — one missing write, two opposite failures.
    try:
        date_of_decision = decision_date_map()
    except Exception:
        date_of_decision = {}   # fail-open: a missing/unreadable log must not block ingest

    try:
        with conn.cursor() as cur:
            for rec in records:
                did = rec.get("decision_id")
                rev_id = _latest_revision_for_decision(cur, org, did) if did else None
                if rev_id is None:
                    stats["skipped"] += 1
                    continue
                for a in rec.get("assertions", []):
                    subj = (a.get("subject_key") or "").strip()
                    pred = (a.get("predicate") or "is").strip()
                    obj = a.get("object")
                    if not subj or obj is None:
                        continue
                    obj_str = obj if isinstance(obj, str) else json.dumps(obj)
                    reverses = a.get("reverses") or {}
                    explicit = bool(reverses.get("explicit"))
                    review = "accepted" if explicit and (a.get("confidence", 0) >= 0.8) else "candidate"
                    chash = _canonical_hash(subj, pred, obj_str)
                    if _existing_identical(cur, org, chash) is not None:
                        stats["duplicates"] = stats.get("duplicates", 0) + 1
                        continue
                    cur.execute(
                        """INSERT INTO assertions
                             (org_id, subject_key, predicate, object_json, applicability, canonical_hash,
                              source_revision_id, review_status, lifecycle_status, confidence,
                              effective_from)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s::date) RETURNING id""",
                        (org, subj, pred, json.dumps(obj_str if isinstance(obj, str) else obj),
                         json.dumps(a.get("applicability", {})),
                         chash, rev_id, review, a.get("confidence"),
                         date_of_decision.get(did)))
                    new_id = cur.fetchone()[0]
                    stats["assertions"] += 1

                    # ── Supersession ──────────────────────────────────────────────────────────
                    # 1) EXPLICIT cross-subject reversal: the extractor named a DIFFERENT subject
                    #    this decision reverses. Only this case needs the LLM, because only the
                    #    LLM can know that subject A replaces differently-named subject B.
                    rsubj = (reverses.get("subject_key") or "").strip()
                    if rsubj and rsubj != subj:
                        prior = _find_prior_active(cur, org, rsubj, new_id)
                        if prior:
                            rstatus = "accepted" if (explicit and a.get("confidence", 0) >= 0.8) else "candidate"
                            cur.execute(
                                """INSERT INTO assertion_relations
                                     (org_id, from_assertion_id, to_assertion_id, relation, reviewer_status, source_revision_id)
                                   VALUES (%s,%s,%s,'supersedes',%s,%s)
                                   ON CONFLICT (from_assertion_id, to_assertion_id, relation) DO NOTHING""",
                                (org, new_id, prior, rstatus, rev_id))
                            stats["supersedes"] += 1
                            if rstatus == "accepted":
                                cur.execute("UPDATE assertions SET lifecycle_status='superseded' WHERE id=%s", (prior,))
                            else:
                                stats["candidates"] += 1

                    # 2) AUTOMATIC same-subject supersession — NO LLM cooperation required.
                    #    Any other active row on this (subject_key, predicate) is by definition a
                    #    stale value of the same fact. Always retire it and record the link, so a
                    #    reversal can never again leave two live contradictory answers.
                    for stale in _find_stale_same_subject(cur, org, subj, pred, new_id, rev_id):
                        cur.execute(
                            """INSERT INTO assertion_relations
                                 (org_id, from_assertion_id, to_assertion_id, relation, reviewer_status, source_revision_id)
                               VALUES (%s,%s,%s,'supersedes','accepted',%s)
                               ON CONFLICT (from_assertion_id, to_assertion_id, relation) DO NOTHING""",
                            (org, new_id, stale, rev_id))
                        cur.execute(
                            "UPDATE assertions SET lifecycle_status='superseded' WHERE id=%s", (stale,))
                        stats["auto_superseded"] = stats.get("auto_superseded", 0) + 1

                    # 3) SILENT-MISS WARNING (2026-08-28, core-business). If this assertion
                    #    superseded NOTHING but its subject already has active rows under other
                    #    predicates, the pair may have been mis-written and a reversal may have
                    #    just failed to link. Report; never act — see _same_subject_other_predicate.
                    if not _find_stale_same_subject(cur, org, subj, pred, new_id, rev_id):
                        others = _same_subject_other_predicate(cur, org, subj, pred, new_id)
                        if others:
                            stats["subject_predicate_mismatch"] = stats.get("subject_predicate_mismatch", 0) + 1
                            print(f"[assertions_ingest] NOTE superseded nothing, but subject "
                                  f"{subj!r} has {len(others)} active row(s) under other "
                                  f"predicate(s): {[o[1] for o in others][:3]} — if this was a "
                                  f"reversal, reuse the PRIOR predicate so the pair competes "
                                  f"(close-core.md 7.5 2b).", file=sys.stderr)
                conn.commit()
                # complete the decision's extraction job
                cur.execute(
                    """SELECT id, attempts FROM stage_jobs
                       WHERE source_revision_id=%s AND stage='semantically_interpreted'
                         AND processor_version=%s ORDER BY id DESC LIMIT 1""",
                    (rev_id, DECISION_PROCESSOR_VERSION))
                jrow = cur.fetchone()
                if jrow:
                    job_id, attempts = jrow
                    # open+complete an attempt (extraction ran in-session, not via claim_job)
                    cur.execute(
                        """INSERT INTO stage_attempts (org_id, stage_job_id, attempt_no, worker_id, started_at, completed_at, outcome, item_done, item_total)
                           VALUES (%s,%s,%s,'assertion-extractor', now(), now(), 'done', %s, %s)
                           ON CONFLICT (stage_job_id, attempt_no) DO NOTHING""",
                        (org, job_id, attempts + 1, len(rec.get("assertions", [])), len(rec.get("assertions", []))))
                    cur.execute("UPDATE stage_jobs SET status='done', attempts=attempts+1, updated_at=now() WHERE id=%s", (job_id,))
                    stats["jobs_done"] += 1
                    conn.commit()
        return stats
    finally:
        conn.close()


def main() -> int:
    """Read a JSON array of decision records from stdin or a file arg, ingest, print stats."""
    raw = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else sys.stdin.read()
    records = json.loads(raw)
    if isinstance(records, dict):
        records = records.get("results", [records])
    stats = ingest(records)
    print(f"assertions_ingest: {stats}")
    _report_fragmented_chains()
    return 0


def _report_fragmented_chains() -> None:
    """Flag any subject_key left with more than one ACTIVE assertion.

    Supersession keys on (subject_key, predicate). So a chain only links when BOTH are reused —
    and the extraction subagent chooses the predicate in free text. Vary it across one logical
    chain and the chain silently forks: every fork stays `active`, and recall serves reversed
    positions alongside current ones as equally true.

    Measured by core-business 2026-07-28 on their venture-name chain: `locked_as` superseded
    correctly while `status` and `spelling_corrected_to` branched off, leaving a REJECTED name and
    a misspelling active next to the real one. career_track forked the same way on
    clarified_as vs set_to.

    This is NOT the documented "first backlog ingests old+new together" caveat in close-core.md
    7.5(2b) — that one is cosmetic and self-heals. This recurs on every Core, every close, whenever
    a subagent picks a natural-sounding predicate instead of a stable one.

    A check rather than another line in the brief, deliberately: the brief has told subagents to
    reuse subject_key since 07-24 and business and I have now each watched an instruction fail to
    reach a subagent. Reporting, not auto-merging — collapsing two actives requires knowing which
    is current, which is exactly the judgment that cannot be inferred from the rows.
    """
    try:
        from _env import connect_corebrain, get_org_id
        org = get_org_id()
        con = connect_corebrain()
        try:
            cur = con.cursor()
            cur.execute("SET LOCAL app.current_org_id = %s", (str(org),))
            cur.execute(
                # TIER 1 — a genuine supersession FAILURE. Same (subject_key, predicate) with
                # both rows active: the newer assertion should have retired the older BY
                # DEFINITION and did not. This is the only tier that is unambiguously wrong.
                """SELECT subject_key, predicate, count(*) AS n
                     FROM assertions
                    WHERE org_id = %s AND lifecycle_status = 'active'
                    GROUP BY subject_key, predicate HAVING count(*) > 1
                    ORDER BY n DESC, subject_key LIMIT 25""", (org,))
            dupes = cur.fetchall()
            # TIER 2 — ADVISORY ONLY, and deliberately quiet.
            #
            # The first version of this check flagged every subject_key with >1 active
            # assertion. Measured on org 1 the moment it went live: 25 flags, of which exactly
            # ONE was a real problem. status / root_cause / mechanism / verified_state are
            # FACETS of a subject, not rival claims about it, and a subject legitimately
            # carries several. A check that cries wolf 24 times out of 25 trains everyone to
            # skip it, which is worse than no check — the same "success and failure look
            # identical" disease this file was written to fix, just inverted.
            cur.execute(
                """SELECT subject_key, count(*) AS n,
                          string_agg(DISTINCT predicate, ' | ') AS preds
                     FROM assertions
                    WHERE org_id = %s AND lifecycle_status = 'active'
                    GROUP BY subject_key HAVING count(*) > 5
                    ORDER BY n DESC, subject_key LIMIT 10""", (org,))
            wide = cur.fetchall()
        finally:
            con.close()
    except Exception as e:
        print(f"  (chain check skipped: {type(e).__name__}: {str(e)[:80]})")
        return

    if dupes:
        print(f"  ⚠ SUPERSESSION FAILED — {len(dupes)} (subject_key, predicate) pair(s) hold >1 "
              f"ACTIVE assertion.")
        print("    Identical subject AND predicate means the newer one should have retired the")
        print("    older and did not, so a stale position is being served as current. Retire one,")
        print("    or split onto distinct subject_keys if they are about genuinely different things.")
        for sk, pred, n in dupes:
            print(f"      {n}x  {sk[:40]:40}  predicate={pred}")
    else:
        print("  chain check: no (subject_key, predicate) pair has competing active assertions.")

    if wide:
        print(f"  · advisory: {len(wide)} subject_key(s) carry >5 active facets. Usually correct "
              f"— glance only if one reads as a rival claim rather than a facet:")
        for sk, n, preds in wide:
            print(f"      {n}x  {sk[:40]:40}  {preds[:70]}")


if __name__ == "__main__":
    sys.exit(main())
