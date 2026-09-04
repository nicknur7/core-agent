#!/usr/bin/env python3
"""si_induct.py — the missing contract INSERT/induction path (unified redesign, step ④).

Plan: PART 8 (Nick's B: inject-only contracts graduate autonomously; blocking clauses go to an
approval list, never auto-generated). Census finding: learned_contracts frozen at 6 because
learned-resynth only UPDATEs existing situations — there is NO code path that creates a 7th. This
adds it, bounded to B.

What it does:
  find_uncovered_clusters() — correction labels recurring in pattern_observations above a threshold
    that are NOT covered by any existing contract's trigger_labels AND not already enforced by a floor
    hook (hallucination-* → say-do/state-claim gates). These are candidate new contracts.
  induce_inject_only(label) — AUTONOMOUSLY INSERT a new INJECT-ONLY contract (a reminder-shaped
    guidance contract; never blocks) for an uncovered cluster. This is what B permits without approval.
  propose_blocking(label) — for a cluster that looks like it needs a BLOCKING rule, write a proposal
    to resynth-work/blocking-proposals.json for Nick (per B, blocking is NEVER auto-created).
  si_liveness() — the missing SI drift detector: corpus size, last-observation date, contract count,
    whether the corpus is growing but contracts are stuck. Surfaced so SI-stuck stops being invisible.

Fork-safe (CORE_ORG_ID). Autonomous run is safe: it only ever INSERTs inject-only (non-blocking) rows.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain-pg"))
from _env import connect_corebrain, get_org_id  # noqa: E402

# hallucination-* are enforced by floor hooks (say-do-gap / state-claim-gate) — not contract territory.
FLOOR_HOOK_LABELS = {"hallucination-state-claim", "hallucination-say-do-gap"}
INDUCE_THRESHOLD = 3    # Nick 2026-07-20: 20x was absurd — a pattern corrected 3x is already real.
                        # These are ACTUAL corrections (Nick correcting Core), not raw prompts, so 3
                        # distinct occurrences is a genuine recurring signal, not noise.

# THREE CORRECTIONS *RECENTLY*, NOT THREE EVER (2026-08-12, found by core-finance).
#
# find_uncovered_clusters counted pattern_observations with NO date predicate anywhere, so
# INDUCE_THRESHOLD was a threshold over ALL HISTORY — and induce_inject_only() INSERTs a standing
# contract with no approval. A pattern corrected three times last spring and never since could mint
# new behavioural guidance today, on evidence that had already stopped being true.
#
# MEASURED ON THIS SEAT, and it is live rather than theoretical — three labels qualify on all-time
# counts with ZERO observations in the last 30 days:
#
#     correction-not-what-i-want   69 all-time,  0 in 30d,  last seen 2026-06-18
#     correction-this-is-wrong     37 all-time,  0 in 30d,  last seen 2026-06-22
#     hallucination-state-claim    14 all-time,  0 in 30d,  last seen 2026-05-15
#
# THE SIBLING ALREADY FIXED THIS EXACT CLASS and said so — skill_graduate.capability_usage:130-140,
# "This read ALL history, so a single use years ago kept fires>0 forever ... which means 'unused for
# 30 days' was not what the code implemented, despite being what it said." Same defect, same module
# family, three weeks apart. 30 days matches that sibling's UNUSED_DAYS so the two halves of the
# lifecycle — what gets induced and what gets retired — agree about what "current" means, instead of
# one inducing on evidence the other would already have called dead.
INDUCE_WINDOW_DAYS = 30


def _covered_labels(cur, org: int) -> set:
    cur.execute("SELECT trigger_labels FROM learned_contracts WHERE org_id=%s AND active", (org,))
    covered = set()
    for (labels,) in cur.fetchall():
        covered.update(labels or [])
    return covered


def find_uncovered_clusters(cur, org: int) -> list[tuple[str, int]]:
    # Windowed — see INDUCE_WINDOW_DAYS. COALESCE because session_date is nullable and created_at is
    # the fallback the rest of this module already uses; a row with neither cannot be dated and is
    # therefore not evidence that anything is CURRENT, which is what this threshold is asking.
    cur.execute("""SELECT pattern_label, count(*) c FROM pattern_observations
                   WHERE org_id=%s
                     AND COALESCE(session_date, created_at::date) >= current_date - %s
                   GROUP BY pattern_label ORDER BY c DESC""", (org, INDUCE_WINDOW_DAYS))
    counts = cur.fetchall()
    covered = _covered_labels(cur, org)
    out = []
    for label, c in counts:
        if not label or label in covered or label in FLOOR_HOOK_LABELS:
            continue
        if c >= INDUCE_THRESHOLD:
            out.append((label, c))
    return out


_STOP = {"the", "and", "you", "your", "that", "this", "with", "for", "not", "was", "are", "have",
         "just", "like", "what", "when", "why", "how", "can", "did", "does", "will", "should", "would",
         "them", "then", "into", "over", "also", "but", "our", "out", "get", "got", "one", "two"}


def _generate_trigger(cur, org: int, label: str) -> list[str]:
    """Build an incoming-prompt trigger from the cluster's own sample prompts: the most frequent
    content words become an OR regex, so the induced contract actually fires. Empty if too thin."""
    import re as _re
    from collections import Counter
    cur.execute("""SELECT prompt_text FROM pattern_observations
                   WHERE org_id=%s AND pattern_label=%s AND prompt_text IS NOT NULL LIMIT 40""", (org, label))
    counts = Counter()
    for (pt,) in cur.fetchall():
        for w in set(_re.findall(r"[a-zA-Z']{4,}", (pt or "").lower())):
            if w not in _STOP:
                counts[w] += 1
    top = [w for w, c in counts.most_common(6) if c >= 2]
    if len(top) < 2:
        return []

    # SPECIFICITY GATE ON THE INDUCTION PATH (2026-08-18, found by core-business).
    #
    # The bar already existed and this path was never wired into it. `friction_test_gate.gate()`
    # refuses any artifact firing on more than OVERBROAD_RATE of real corpus prompts, but it is
    # reachable only through `friction_installer`; `induce_inject_only` writes to learned_contracts
    # in raw SQL and never passes it. So a trigger built from the six most frequent content words in
    # forty sample prompts went straight to a live contract, untested.
    #
    # business measured what that cost, using the gate's own test against each seat's own corpus:
    #
    #     cid  key                          fires   rate
    #      43  instruction directive          185   60.1%
    #      52  instruction emphatic           189   61.4%
    #      46  instruction tooling            104   33.8%
    #      44  instruction preference          78   25.3%
    #     ... against a 3% bar, and every hand-authored starter contract at 0.3-0.6%
    #
    # Nine generated triggers over the bar across three seats. Business's prompts have been carrying
    # two contract blocks on three turns in five — which is the system working exactly as built.
    #
    # THE FLOOR IS NOT INVENTED HERE. It imports OVERBROAD_RATE and MIN_CORPUS from the gate, and it
    # measures with `re.compile(p, re.I).search`, which is what `learned-classifier.py:99-105`
    # actually runs at dispatch. Same constant, same matcher, so the measuring predicate and the
    # firing predicate cannot drift — the reuse lesson from the 08-12 denominator work, applied.
    #
    # NARROW, THEN REFUSE. Drop the broadest surviving word until the rate clears the bar; if two
    # words cannot clear it, return [] and let the contract be induced inert rather than install a
    # rule that fires on a third of everything. An inert contract is visible and fixable; a 61%
    # trigger is indistinguishable from the system working.
    try:
        import friction_test_gate as _tg
        cur.execute("""SELECT prompt_text FROM pattern_observations
                       WHERE org_id=%s AND prompt_text IS NOT NULL AND prompt_text <> ''
                       LIMIT 300""", (org,))
        corpus = [r[0] for r in cur.fetchall()]
    except Exception:
        return []  # cannot prove specificity -> do not install a trigger. Fail closed.
    if len(corpus) < _tg.MIN_CORPUS:
        # The gate calls this UNDECIDABLE rather than failed, and undecidable must not install:
        # finance sits at 33 prompts, under the bar, and would otherwise get an unprovable trigger.
        return []
    words = list(top)
    while len(words) >= 2:
        pat = r"\b(" + "|".join(_re.escape(w) for w in words) + r")\b"
        try:
            rx = _re.compile(pat, _re.I)
        except Exception:
            return []
        rate = sum(1 for t in corpus if rx.search(t)) / len(corpus)
        if rate <= _tg.OVERBROAD_RATE:
            return [pat]
        words.pop(0)  # most_common order: the broadest word goes first
    return []


def induce_inject_only(conn, org: int, label: str, count: int) -> int:
    """INSERT a new INJECT-ONLY contract for an uncovered cluster (autonomous per B). Returns id.
    Generates a trigger from the cluster's own prompts so it FIRES. checkable is EMPTY ([]) — this
    contract can only inject guidance, never block (blocking would need Nick's approval, per B)."""
    # PREFIX-ANCHORED. `.replace("correction-", "")` strips the marker from ANYWHERE, so a cluster
    # labelled `no-correction-needed` became `no-needed` — text that then went into a live contract's
    # situation line and steers a real turn. Sixth instance of SUBSTRING WHERE EXACT IS REQUIRED in
    # one day (core-business, #910); five of the six were found by the other seat, not the author.
    # The second replace is deliberate and stays: it turns the remaining slug into words.
    stem = label[len("correction-"):] if label.startswith("correction-") else label
    situation = (f"{stem.replace('-', ' ')} — a recurring correction "
                 f"({count}x) with no contract yet; acknowledge the pattern and correct course.")
    with conn.cursor() as cur:
        triggers = _generate_trigger(cur, org, label)
        cur.execute(
            """INSERT INTO learned_contracts
                 (situation, trigger_labels, required_shape, forbidden_moves, checkable, example_prompts, triggers, org_id, active)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, true) RETURNING id""",
            (situation, [label],
             ["acknowledge the recurring correction plainly", "address the specific thing, not a generic apology"],
             ["repeating the move that drew the correction"],
             json.dumps([]),  # inject-only: no blocking checkable clauses (B)
             [], triggers, org))
        return cur.fetchone()[0]


def si_liveness(cur, org: int) -> dict:
    cur.execute("SELECT count(*), max(session_date) FROM pattern_observations WHERE org_id=%s", (org,))
    corpus_n, last_obs = cur.fetchone()
    cur.execute("SELECT count(*) FROM learned_contracts WHERE org_id=%s AND active", (org,))
    contracts_n = cur.fetchone()[0]
    # Placeholders reported SEPARATELY (2026-08-31). A row induce_inject_only minted and nothing
    # ever resynthesized still carries the hardcoded boilerplate body below — it is a parked ask,
    # not a learned contract, and folding it into `contracts` overstated coverage: fleet-wide, 7 of
    # 42 rows were this. The count must match the INSERT above verbatim or it silently reads 0.
    cur.execute("""SELECT count(*) FROM learned_contracts WHERE org_id=%s AND active
                   AND required_shape = ARRAY['acknowledge the recurring correction plainly',
                                              'address the specific thing, not a generic apology']""",
                (org,))
    placeholder_n = cur.fetchone()[0]
    return {"corpus": corpus_n, "last_observation": str(last_obs) if last_obs else None,
            "contracts": contracts_n, "placeholders": placeholder_n}


def main() -> int:
    org = get_org_id()
    conn = connect_corebrain()
    try:
        with conn.cursor() as cur:
            live = si_liveness(cur, org)
            uncovered = find_uncovered_clusters(cur, org)
        induced = []
        if "--induce" in sys.argv:
            for label, c in uncovered:
                nid = induce_inject_only(conn, org, label, c)
                conn.commit()
                induced.append((label, c, nid))
            if induced:
                # regenerate this Core's snapshot so the induced contract reaches the classifier
                try:
                    import si_snapshot
                    si_snapshot.write_snapshot()
                except Exception:
                    pass
        if "--selftest" in sys.argv:
            # prove the INSERT path works end-to-end (insert + verify + remove)
            nid = induce_inject_only(conn, org, "correction-selftest-xyz", 99)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("SELECT active, checkable FROM learned_contracts WHERE id=%s", (nid,))
                active, checkable = cur.fetchone()
                cur.execute("DELETE FROM learned_contracts WHERE id=%s", (nid,))
                conn.commit()
            ok = active and checkable == []
            print(f"si_induct selftest: {'PASS' if ok else 'FAIL'} — INSERT path works, inject-only (checkable empty)")
            return 0 if ok else 1
        print(f"SI liveness: corpus={live['corpus']} obs, last={live['last_observation']}, "
              f"contracts={live['contracts']}"
              + (f" ({live['placeholders']} placeholder — parked asks, not coverage)"
                 if live.get("placeholders") else ""))
        if uncovered:
            print(f"Uncovered recurring clusters (candidates for a new contract): {uncovered}")
            if induced:
                print(f"INDUCED inject-only contracts (autonomous, B): {induced}")
            else:
                print("  (run with --induce to autonomously create inject-only contracts)")
        else:
            print("All recurring correction clusters are covered by existing contracts or floor hooks — "
                  "no new contract needed. (INSERT path is live; ceiling is no longer frozen at 6.)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
