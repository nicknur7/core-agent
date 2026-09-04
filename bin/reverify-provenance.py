#!/usr/bin/env python3
"""Re-classify pre-fix corpus rows against the real turn, and say plainly what cannot be checked.

WHY THIS EXISTS (2026-08-12, T007). On 2026-08-11 commit 4f207c7 replaced a blocklist with
turn_provenance.is_human_turn(); its message is "41.6% of what the system mined as Nick's words was
not Nick." Every row mined BEFORE that commit was classified by the broken filter, and on life that
is 537 of 581 rows.

T007 offered two ways out: backfill the pre-fix corpus, or restrict the backtest window. MEASURED
FIRST, because the choice turns on a number nobody had:

    pre-fix rows on life                 537
      transcript still on disk           189  (35.2%)  -> RE-CHECKABLE
      transcript deleted                 348  (64.8%)  -> UNVERIFIABLE, PERMANENTLY
    distinct source transcripts           93, of which 21 survive

So a full backfill is IMPOSSIBLE — not hard, impossible. Two thirds of the evidence is gone, and no
amount of care recovers it. That is the finding, and it constrains Phase 2 whatever anyone prefers.

WHAT THIS DOES: re-runs the CURRENT resolver over every row whose transcript survives. A row that
turns out not to be a human turn is excluded with an explicit reason — the same convention as the
185 rows already excluded as machine-generated, not a new mechanism.

WHAT IT REFUSES TO DO: it does not touch the 348 unverifiable rows. They are not known-bad; they are
UNKNOWN, and excluding them would discard two thirds of the corpus on suspicion. Nor does it mark
them "verified" by silence. It reports them as a distinct third category, because the difference
between "checked and clean" and "could not check" is the distinction this whole suite exists to keep.

DRY-RUN BY DEFAULT. Writes are RLS-scoped to app.current_org_id, so each Core must run its own; life
cannot re-verify a peer's corpus even with the cross-org SELECT open, because the fix is a WRITE.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

GEN = "learned-miner-v1"
FIX_DATE = "2026-08-11"          # 4f207c7 — turn_provenance replaced the blocklist
REASON = "provenance re-check: source turn is not a human turn (mined by the pre-4f207c7 filter)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write exclusions (default: dry run)")
    a = ap.parse_args()

    import turn_provenance as tp
    from _env import connect_corebrain, get_org_id

    org = get_org_id()
    con = connect_corebrain()
    cur = con.cursor()
    cur.execute(
        "SELECT id, source_file, source_uuid FROM pattern_observations "
        "WHERE org_id=%s AND detector_version=%s AND excluded_reason IS NULL "
        "AND created_at < %s AND source_file IS NOT NULL AND source_uuid IS NOT NULL",
        (org, GEN, FIX_DATE))
    rows = cur.fetchall()

    by_file: dict[str, list] = defaultdict(list)
    for rid, sf, su in rows:
        by_file[sf].append((rid, su))

    human, nonhuman, unverifiable = [], [], []
    for sf, items in by_file.items():
        p = Path(sf)
        if not p.is_file():
            unverifiable.extend(rid for rid, _ in items)
            continue
        # ONE UUID CAN CARRY SEVERAL ROWS, and keying a dict on it silently dropped them.
        # core-business caught this by ARITHMETIC, not by reading: the buckets did not sum to the
        # input. On life 490 rows share 435 distinct uuids, so 55 observations mined from the same
        # turn collapsed onto one another and vanished from every bucket — neither counted as human,
        # nor non-human, nor unverifiable. A row that disappears from all three categories is worse
        # than a miscounted one, because no total looks wrong.
        #
        # The check is free and general: buckets must sum to input. It is asserted below rather than
        # left to a reader who happens to add up three numbers.
        want: dict[str, list] = defaultdict(list)
        for rid, su in items:
            want[su].append(rid)
        seen = {}
        for line in p.open(errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            u = d.get("uuid")
            if u in want:
                seen[u] = d
        for su, rids in want.items():
            entry = seen.get(su)
            if entry is None:
                # The file survives but this turn is not in it — a compacted or rewritten
                # transcript. Unverifiable for the same reason as a deleted file, and counted
                # there rather than silently dropped.
                unverifiable.extend(rids)
                continue
            (human if tp.turn_kind(entry) == tp.HUMAN else nonhuman).extend(rids)

    total = len(rows)
    # BUCKETS MUST SUM TO INPUT. Free, general, and it is what core-business used to find the uuid
    # collision — by adding three numbers, not by reading the loop. Asserted here so the next
    # collapse cannot hide behind a plausible-looking breakdown.
    _sum = len(human) + len(nonhuman) + len(unverifiable)
    if _sum != total:
        print(f"*** REFUSING TO REPORT: buckets sum to {_sum} but {total} rows were selected. "
              f"{abs(total - _sum)} row(s) fell out of every category, which no total makes visible. "
              f"Fix the partition before trusting any number below.")
        con.close()
        return 2
    print(f"reverify-provenance — org {org}, {total} pre-{FIX_DATE} rows with a source turn\n")
    print(f"  re-checked, IS a human turn      : {len(human)}")
    print(f"  re-checked, NOT a human turn     : {len(nonhuman)}   <- mined as the operator's words, was not")
    print(f"  UNVERIFIABLE (transcript gone)   : {len(unverifiable)}")
    checked = len(human) + len(nonhuman)
    if checked:
        print(f"\n  of what COULD be checked, {100.0*len(nonhuman)/checked:.1f}% was not a human turn "
              f"(4f207c7 measured 41.6% fleet-wide)")

    if not a.apply:
        print("\n  (dry run — nothing written; pass --apply to exclude the non-human rows)")
        con.close()
        return 0

    # STAMP WHAT WAS VERIFIED, not only what was rejected. The first version of this tool excluded
    # the bad rows and recorded nothing about the good ones — so "the defensible backtest window"
    # was not a queryable set, and the only way to rebuild it was to re-run this whole scan and hope
    # the same transcripts still existed. That silently converts "checked and clean" into
    # "indistinguishable from never checked", which is the exact distinction this file argues for
    # three paragraphs above its own code.
    if human:
        cur.execute("UPDATE pattern_observations SET provenance_verified_at = now() "
                    "WHERE id = ANY(%s) AND org_id=%s", (human, org))
        print(f"\n  stamped {cur.rowcount} rows provenance_verified_at")

    if nonhuman:
        cur.execute("UPDATE pattern_observations SET excluded_reason=%s, excluded_at=now() "
                    "WHERE id = ANY(%s) AND org_id=%s AND excluded_reason IS NULL",
                    (REASON, nonhuman, org))
        n = cur.rowcount
        con.commit()
        print(f"  excluded {n} non-human rows (of {len(nonhuman)} identified)")
        if n != len(nonhuman):
            print("  *** COUNT MISMATCH — writes are RLS-scoped; a shortfall means rows belonging "
                  "to another org were selected, which should be impossible here.")
    else:
        print("  nothing to exclude")
    con.commit()
    print(f"  {len(unverifiable)} rows remain UNVERIFIABLE and were deliberately not touched — "
          f"unknown is not the same as bad.")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
