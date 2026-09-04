#!/usr/bin/env python3
"""Backfill assertions.effective_from from the decision heading that produced each row.

WHY THIS EXISTS
---------------
`assertions.effective_from` was declared in migration 2026-07-18b and is READ in two
places — `query.py`'s assertion-recall ranking and `start_brief.py`'s startup brief —
but it was never WRITTEN by anything. `assertions_ingest.py`'s INSERT omitted the
column entirely, so every assertion created after the migration carried NULL.

That produced two opposite failures from one missing field:

  1. RANKING INVERSION (the serious one). query.py boosted recency with
     `0.15 / (1 + age(COALESCE(effective_from, now())))`. COALESCE to `now()` means a
     row with NO date is scored as though it were created THIS INSTANT, earning the
     MAXIMUM boost of 0.1500 — strictly higher than any correctly-dated row (a
     2026-07-10 decision scores 0.0906). So undated assertions outranked dated ones by
     construction, and 44% of the active set was undated.

     Live consequence: `sync-trust-root-paths` (undated, asserting a policy Nick
     REVERSED on 2026-07-10) outranked the three dated 2026-07-10 assertions that
     record the reversal. core-business recalled the stale row, believed it was
     current, and reported a fabricated trust-root security hole to core-life on
     2026-07-28. The record was not merely stale — the ranking actively preferred it.

  2. STARTUP-BRIEF BLINDNESS. start_brief.py filters `effective_from IS NOT NULL`, so
     those same undated rows were invisible to the brief. Half the brain dominated
     recall while being absent from the startup summary.

WHY THE DATE IS RECOVERABLE EXACTLY
-----------------------------------
Each assertion links to a source_revision whose `source_uri` is
`memory/decisions-log.md#d_<hex>`, and the decisions-log carries
`<!-- core-decision-id: d_<hex> -->` immediately after each entry's heading. The
heading carries the date. So decision_id -> date is an exact lookup, not an estimate.
`source_revisions.observed_at` is NOT used: that is when the file was scanned, which
for a backfilled historical log is months after the decision was made.

Date inheritance matches decisions_segment.py: an undated heading inherits the nearest
preceding dated heading, so sub-entries of a dated decision get that decision's date.

SCOPE
-----
Only rows whose source is THIS Core's decisions-log, and only where effective_from IS
NULL. Each Core must run this against its own log — org 2's rows are recoverable only
from core-business's decisions-log, which this Core does not read.

Usage:
  python3 backfill_effective_from.py            # dry run, prints what would change
  python3 backfill_effective_from.py --apply    # writes
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import connect_corebrain  # noqa: E402  (same connector every brain-pg tool uses)
from decisions_segment import decision_date_map  # noqa: E402

CORE_DIR = Path(os.environ.get("CORE_INSTANCE")
                or os.environ.get("CLAUDE_PROJECT_DIR")
                or Path(__file__).resolve().parents[2])
LOG_PATH = CORE_DIR / "memory" / "decisions-log.md"

# LIKE pattern for the ledger identifier stored in source_revisions.source_uri. Data in the
# database, not a filesystem path — nothing ever opens this string.
_LEDGER_URI_LIKE = "memory/decisions-log.md#%"  # lint-code-paths: ignore — ledger identifier, not a path op

# The heading/date walk deliberately lives in decisions_segment.decision_date_map() and is
# NOT copied here. A duplicated copy of this parse is how the `^### `-vs-`^#{2,3} ` heading
# bug survived — one copy was fixed, the other silently kept matching nothing.


def main() -> int:
    apply = "--apply" in sys.argv

    if not LOG_PATH.exists():
        print(f"backfill_effective_from: no decisions-log at {LOG_PATH}", file=sys.stderr)
        return 1

    dates = decision_date_map(LOG_PATH.read_text().splitlines())
    if not dates:
        # Parsing nothing from a non-empty log is a failure, not a no-op — the same
        # lesson the `##`-heading bug taught. Refuse rather than silently updating zero.
        print("backfill_effective_from: parsed 0 decision ids from a non-empty log — "
              "refusing to proceed (heading/id pattern may have drifted)", file=sys.stderr)
        return 1
    print(f"backfill_effective_from: {len(dates)} dated decision ids in {LOG_PATH.name}")

    conn = connect_corebrain()
    try:
        with conn.cursor() as cur:
            # Bound as a PARAMETER, not inlined. The literal is a ledger IDENTIFIER stored in
            # source_revisions.source_uri — assertions are keyed "memory/decisions-log.md#<id>"
            # and that string is never opened, so the registry constant (an absolute Path) does
            # not fit. Hoisting it to a named constant lets the ordinary `#` pragma cover it and
            # removed the only user of the `--` form, which was defeatable three separate ways.
            # Note the pattern is a single `%` here: with a bound parameter psycopg2 performs
            # %-interpolation on the QUERY, so the doubled `%%` the inline version needed would
            # arrive as a literal `%%`. No other `%` appears in this statement.
            cur.execute("""
                SELECT a.id, sr.source_uri
                  FROM assertions a
                  JOIN source_revisions sr ON sr.id = a.source_revision_id
                 WHERE a.effective_from IS NULL
                   AND sr.source_uri LIKE %s
                 ORDER BY a.id
            """, (_LEDGER_URI_LIKE,))
            rows = cur.fetchall()

            updates, unmatched = [], []
            for aid, uri in rows:
                did = uri.split("#", 1)[1] if "#" in uri else ""
                d = dates.get(did)
                (updates if d else unmatched).append((aid, d or did))

            print(f"  undated assertions sourced from decisions-log: {len(rows)}")
            print(f"  resolvable to a heading date:                  {len(updates)}")
            print(f"  unresolvable (decision id not in this log):    {len(unmatched)}")
            if unmatched:
                print("  unresolvable ids (first 10, expected for other Cores' rows):")
                for aid, did in unmatched[:10]:
                    print(f"    assertion {aid} -> {did}")

            if not apply:
                print("\n  DRY RUN — no writes. Re-run with --apply.")
                for aid, d in updates[:10]:
                    print(f"    would set assertion {aid}.effective_from = {d}")
                return 0

            for aid, d in updates:
                cur.execute("UPDATE assertions SET effective_from = %s::date WHERE id = %s "
                            "AND effective_from IS NULL", (d, aid))
            conn.commit()
            print(f"\n  APPLIED: {len(updates)} rows dated.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
