#!/usr/bin/env python3
"""Move entities that were filed into the wrong Core's brain partition. Reversible, journaled.

WHY. `template/brain/_build/export.py` routed every `core-ops` session into LIFE's project bucket
for as long as ops has existed: `core-ops` was absent from CWD_PROJECT_RULES, so it fell through
a substring catch-all (`ai projects/core` is a prefix of `ai projects/core-ops`). Fixed 2026-08-09
by deriving the Core list and quarantining unknown Cores. That stopped NEW misfiling; it did not
move the 168 rows already sitting in org 1.

org_id IS THE PRIVACY BOUNDARY. Another Core's content inside life's partition means life's recall
returns ops's data as its own — which is the thing the whole partitioning model exists to prevent.

WHY THIS MOVES AND NEVER DELETES. 73 of the 168 already exist by name in org 5, so a "clean up the
duplicates" framing would mean deleting live brain rows. This tool only ever UPDATES org_id, and
writes a journal of every id it touched with its previous value, so --undo restores the exact prior
state. A duplicate name in org 5 is untidy; a deleted row is unrecoverable, and one of those is a
much worse failure than the other.

    python3 bin/repartition-misfiled.py                 # dry run — default, shows the plan
    python3 bin/repartition-misfiled.py --apply         # move, writing the journal first
    python3 bin/repartition-misfiled.py --undo <file>   # restore from a journal
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scheduling" / "brain-pg"))
from _env import load_secrets, connect_corebrain  # noqa: E402

# Which Core a source path belongs to, by the same derivation export.py now uses.
CORE_ORG = {"life": 1, "business": 2, "school": 3, "finance": 4, "ops": 5}


def misfiled(cur, wrong_org, core, right_org):
    """Rows in `wrong_org` whose source_file names a DIFFERENT Core.

    Deliberately narrow: matches the Core's own path token, not the bare name. `ops` appears in
    ordinary prose; `/core-ops` and `_ops_` are provenance.
    """
    cur.execute("""
        SELECT id, name, kind, source_file FROM entities
        WHERE org_id = %s
          AND (source_file ILIKE %s OR source_file ILIKE %s)
        ORDER BY id
    """, (wrong_org, "%%/core-%s%%" % core, "%%_%s_%%" % core))
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the move (default is a dry run)")
    ap.add_argument("--undo", help="restore from a journal file")
    ap.add_argument("--core", default="ops", help="the Core whose rows were misfiled")
    ap.add_argument("--from-org", type=int, default=1, help="the partition they are wrongly in")
    a = ap.parse_args()

    load_secrets()
    con = connect_corebrain()
    cur = con.cursor()

    if a.undo:
        rows = [json.loads(l) for l in open(a.undo) if l.strip()]
        for r in rows:
            cur.execute("UPDATE entities SET org_id = %s WHERE id = %s", (r["from_org"], r["id"]))
        con.commit()
        print("  restored %d row(s) to their previous org_id" % len(rows))
        return 0

    right = CORE_ORG.get(a.core)
    if right is None:
        sys.exit("unknown core: %s" % a.core)

    rows = misfiled(cur, a.from_org, a.core, right)
    print("\n  MISFILED ENTITIES — %s content sitting in org %d\n" % (a.core, a.from_org))
    print("  found: %d" % len(rows))
    if not rows:
        print("  nothing to do\n")
        return 0

    ids = [r[0] for r in rows]
    cur.execute("""SELECT COUNT(*) FROM entities a WHERE a.id = ANY(%s)
                   AND EXISTS (SELECT 1 FROM entities b
                               WHERE b.org_id = %s AND b.name = a.name)""", (ids, right))
    dup = cur.fetchone()[0]
    print("  of those, %d already exist by name in org %d — they will be MOVED, never deleted"
          % (dup, right))
    print("\n  sample:")
    for _id, name, kind, src in rows[:5]:
        print("    [%s] %-38s %s" % (kind, str(name)[:38], str(src)[-46:]))

    if not a.apply:
        print("\n  DRY RUN. Re-run with --apply to move them. Nothing has changed.\n")
        return 0

    # JOURNAL FIRST, then move. A journal written after the fact describes a state that may no
    # longer be recoverable if the write fails midway.
    jpath = (HERE.parent / ".claude" / "state" /
             ("repartition-%s-%d.jsonl" % (a.core, int(time.time()))))
    jpath.parent.mkdir(parents=True, exist_ok=True)
    with open(jpath, "w") as fh:
        for _id, name, kind, src in rows:
            fh.write(json.dumps({"id": _id, "from_org": a.from_org, "to_org": right,
                                 "name": str(name)[:120], "source_file": str(src)[:200]}) + "\n")
    print("\n  journal: %s" % jpath)

    try:
        cur.execute("UPDATE entities SET org_id = %s WHERE id = ANY(%s)", (right, ids))
        con.commit()
    except Exception as e:
        con.rollback()
        # RLS REFUSES THIS, AND THAT IS THE PARTITIONING WORKING. Measured 2026-08-09: role
        # brain_app with app.current_org_id=1 cannot write a row bearing org_id=5 —
        # "new row violates row-level security policy for table entities". The transaction rolled
        # back cleanly; all 168 rows remained in org 1.
        #
        # SO A CORE CANNOT REPARTITION ANOTHER CORE'S DATA, EVEN TO CORRECT IT. That is the right
        # answer and it is worth stating rather than working around: the same rule that stops life
        # reading ops's rows stops life writing them, and a tool that could bypass it would be a
        # cross-org write channel wearing a maintenance label. The correct actors are ops's own
        # Core (which owns org 5) or a migration run deliberately with elevated privilege.
        print("\n  REFUSED BY ROW-LEVEL SECURITY — and this is correct.")
        print("  %s" % str(e).strip().splitlines()[0][:110])
        print("\n  A Core cannot move rows into another Core's partition. The journal above lists")
        print("  every affected id, so ops's Core (or a deliberate elevated migration) can apply")
        print("  it. Nothing was changed here; the transaction rolled back.")
        return 3
    print("  moved %d row(s) from org %d -> org %d" % (cur.rowcount, a.from_org, right))
    print("  undo: python3 bin/repartition-misfiled.py --undo %s\n" % jpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
