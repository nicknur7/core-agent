#!/usr/bin/env python3
"""Soft-retire live entities whose source_file points into the vault but the file is gone.
DRY-RUN BY DEFAULT.

WHY THIS EXISTS (2026-08-31)
============================
Two agents independently hit the same symptom today: `compile-truth-refresh.py --partition`
prints "N drifted hub(s) have NO matching hub file and cannot be refreshed" for rows that can
NEVER be refreshed, because the file they cite is gone. Reported anecdotally as 11/26 on
core-business, 3/14 on core-school, 4/77 on core-ops.

MEASURED, and it changes the diagnosis. Two DIFFERENT things are being conflated:

  1. "source_file is a vault-absolute path and the file no longer exists" — the literal defect
     this tool fixes. Fleet-wide count on 2026-08-31: **2 rows, both org 1 (life)**, both citing
     `topics/playwright-screenshots.md` (id 383, kind Entity, name "playwright screenshots"; id
     217462, kind Source, name = the dead path itself). `git log --diff-filter=D` in the vault
     dates the deletion to commit fd2504fa, "Brain hygiene lint + Spec 4 hub overlap
     consolidation" (2026-05-18) — a RENAME, not a purge: `topics/playwright-screenshot.md`
     (singular) exists today and is live entity id 382 in this same org. Retiring 383 blind would
     not be cleaning up junk, it would be dropping the record that a merge happened.

  2. The reported 11/26/3/14/4/77 figures. Reproducing `detect_drift()` + `partition_drifted()`'s
     OWN matching exactly (both live in compile-truth-refresh.py / compile-truth.py) gives 11
     (life) / 32 (business) / 17 (school) / 68 (finance) / 8 (ops) unmatched — same shape,
     confirming it's the same defect class those two agents saw. But that match is by
     **(kind, name) against whatever hub file currently exists**, entirely independent of the
     `source_file` column. Of the 136 rows it flags fleet-wide, 132 have a `source_file` that
     is NOT a vault path at all (`chunk-body-*.json` chunk identifiers, `__merge_stub__`, bare
     doc-relative paths) — overwhelmingly `kind='Project'` rows from a different, non-hub
     ingestion path that never had an `entities/`/`topics/` markdown file to begin with. Of the
     remaining 4, the file the entity would need EXISTS on disk — the miss there is a
     `kind`/frontmatter `type:` mismatch, not a deleted file. ZERO of the 136 are "vault path,
     file missing" — the population this tool targets and the population inflating the drift
     report's "would re-synthesize" count are almost entirely disjoint.

  CONCLUSION SURFACED, NOT ACTED ON: this tool is scoped exactly to what it was asked to fix —
  #1 — because that is a real, narrow, provable defect. It will NOT touch the reported noise
  floor in #2; that needs a fix to the name-matching in compile-truth-refresh.py / a decision
  about what Project-kind, non-vault-sourced entities should do on a partition miss, and is a
  different, unrelated change that should not be bundled here.

THE SKIP-IF-EDGES RULE, stated explicitly (measured, not assumed)
===================================================================
Both of the 2 known orphans have live `entity_edges`: id 383 carries an `originates_in` edge to
the dead file's own Source row (217462) AND three `same_as` edges to live siblings in school
(35598), business (44484) and finance (40655) — i.e. it is the cross-Core anchor for a concept
four other Cores still point at. Soft-retiring it would not delete those edges (entity_edges has
no ON DELETE behavior triggered by a soft-retire — only a hard DELETE cascades, and this tool
never deletes), but it WOULD leave four live `same_as` edges hanging off a tombstoned node, which
is a graph-integrity regression a human should choose, not a script.

**The rule:** an orphan is auto-retirable ONLY if it has ZERO rows in `entity_edges` (as either
`from_entity_id` or `to_entity_id`). Nothing points at it and it points at nothing — there is
nothing left for a retirement to disconnect. Any orphan WITH edges is always skipped and reported
with a best-effort successor guess (same directory, name normalized by stripping a trailing 's'
and collapsing separators) so a human can decide whether to remap the edges onto the successor
before anything is retired. This tool does not attempt that remap itself — guessing a rename
target and rewriting graph edges on that guess is exactly the kind of silent, hard-to-reverse
move `repartition-hubs.py` earns its complexity to do safely for duplicates; an orphan that turns
out to be a rename is a `repartition`/manual-remap job, not a `retire` job.

WHAT THIS IS NOT
================
Not a fix for compile-truth-refresh.py's name-matching (see measurement above — different rows,
different mechanism). Not `repartition-hubs.py` (that reassigns org ownership among LIVE hub
files; this retires rows whose file is gone). Not a general dedup tool — it never merges or picks
a survivor, because "no candidate is live" (dedup) and "no file exists" (this) are different
questions with different safe defaults.

SAFETY
======
* Dry-run by default. `--apply` is required to write, and refuses without `--journal`.
* NEVER DELETE. Every FK into entities is ON DELETE CASCADE (entity_edges x2, evidence,
  workflow_steps, workflow_triggers) — a DELETE here would silently take derived data with it.
  The only write is `UPDATE entities SET valid_until = now() WHERE id = ... AND valid_until IS
  NULL` — reversible by clearing `valid_until` from the journal.
* Zero-edge check only (see rule above) — never inferred from name similarity or kind alone.
* Connects as `brain_admin` for writes: RLS `update_own` is org-scoped and `brain_app` is
  NOBYPASSRLS, so it cannot retire rows across orgs in one run. Reads work as either role
  (`read_all` is unconditional SELECT).

Usage:
  python3 bin/retire-orphan-hubs.py                       # dry-run report (default)
  python3 bin/retire-orphan-hubs.py --json out.json       # dry-run + machine-readable plan
  python3 bin/retire-orphan-hubs.py --apply --journal j.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("CORE_BRAIN") or (Path.home() / "AI Projects" / "core-brain"))
ORG_NAME = {0: "shared", 1: "life", 2: "business", 3: "school", 4: "finance", 5: "ops"}


def connect(admin: bool):
    """brain_admin for writes — the only role RLS lets retire a row it doesn't itself own.

    Same posture as repartition-hubs.py: `update_own` requires org_id = app.current_org_id,
    which brain_app cannot set across orgs; `brain_admin` has NOBYPASSRLS off (i.e. bypasses
    RLS). Reads work as either role since `read_all` has no org condition.
    """
    import psycopg2
    db = os.environ.get("COREBRAIN_DB", "corebrain")
    user = os.environ.get("BRAIN_ADMIN_USER", "brain_admin") if admin else None
    return psycopg2.connect(dbname=db, user=user) if user else psycopg2.connect(dbname=db)


def guess_successor(source_file: str) -> str | None:
    """Best-effort, INFORMATIONAL ONLY — never used to auto-remap or auto-retire.

    Strips a trailing 's' and collapses separators, then checks the same vault subdirectory
    for a file matching that stem. This is exactly the shape of the one confirmed case
    (playwright-screenshots.md -> playwright-screenshot.md from the 2026-05-18 hub-overlap
    consolidation) — a naming/plural cleanup leaving the old name behind. It is deliberately
    narrow: a miss here means "no obvious successor", not "safe to delete".
    """
    p = Path(source_file)
    stem = p.stem
    candidates = set()
    if stem.endswith("s"):
        candidates.add(stem[:-1])
    norm = re.sub(r"[\s_]+", "-", stem.strip().lower())
    if norm != stem:
        candidates.add(norm)
        if norm.endswith("s"):
            candidates.add(norm[:-1])
    for cand in candidates:
        cand_path = p.with_name(cand + p.suffix)
        if cand_path.exists():
            return str(cand_path)
    return None


def find_orphans(cur) -> list[dict]:
    """Live entities whose source_file is a vault-absolute path that no longer exists.

    Two-step deliberately: Postgres cannot stat the filesystem, so the LIKE-filtered rows are
    pulled once (14,659 fleet-wide as of 2026-08-31 — cheap) and existence is checked in Python.
    Edge counts are pulled in the SAME query via a correlated subquery rather than N+1 round
    trips per candidate.
    """
    cur.execute("""
        SELECT id, org_id, kind, name, source_file, last_compiled_at,
               (SELECT count(*) FROM entity_edges e
                WHERE e.from_entity_id = entities.id OR e.to_entity_id = entities.id) AS edge_count
        FROM entities
        WHERE valid_until IS NULL AND source_file LIKE %s
        ORDER BY org_id, id
    """, (str(VAULT) + "/%",))
    rows = cur.fetchall()
    orphans = []
    for (id_, org, kind, name, src, last_compiled, edge_count) in rows:
        if os.path.exists(src):
            continue
        orphans.append({
            "id": id_, "org_id": org, "kind": kind, "name": name, "source_file": src,
            "last_compiled_at": last_compiled.isoformat() if last_compiled else None,
            "edge_count": edge_count,
        })
    return orphans


def build_plan(cur) -> dict:
    orphans = find_orphans(cur)
    retire, skip = [], []
    for o in orphans:
        if o["edge_count"] == 0:
            retire.append(o)
        else:
            o["possible_successor"] = guess_successor(o["source_file"])
            skip.append(o)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vault": str(VAULT),
        "total_orphans": len(orphans),
        "retire": retire,
        "skip_has_edges": skip,
    }


def report(plan: dict) -> None:
    print(f"  vault                    : {plan['vault']}")
    print(f"  orphaned hub rows found  : {plan['total_orphans']:>7,}")
    print(f"  safe to retire (0 edges) : {len(plan['retire']):>7,}")
    print(f"  skipped (has edges)      : {len(plan['skip_has_edges']):>7,}")

    by_org = collections.Counter(o["org_id"] for o in plan["retire"])
    if by_org:
        print("\n  retiring, by org:")
        for org, n in sorted(by_org.items()):
            print(f"    org {org} ({ORG_NAME.get(org,'?'):<8}) {n:>6,}")
    for o in plan["retire"]:
        print(f"    RETIRE  id={o['id']:<8} org={ORG_NAME.get(o['org_id'],'?'):<8} "
              f"{o['kind']:<7} {o['name']!r}")
        print(f"            source_file={o['source_file']}")

    if plan["skip_has_edges"]:
        print("\n  skipped — has live graph edges, needs a human remap decision, not retirement:")
        for o in plan["skip_has_edges"]:
            succ = f" -> possible successor: {o['possible_successor']}" if o["possible_successor"] else " -> no obvious successor found"
            print(f"    SKIP    id={o['id']:<8} org={ORG_NAME.get(o['org_id'],'?'):<8} "
                  f"{o['kind']:<7} {o['name']!r}  edges={o['edge_count']}{succ}")
            print(f"            source_file={o['source_file']}")


def apply_plan(conn, cur, plan: dict, journal: Path) -> int:
    j = journal.open("w")
    retired = 0
    for o in plan["retire"]:
        j.write(json.dumps({"op": "retire", "id": o["id"], "org_id": o["org_id"],
                             "kind": o["kind"], "name": o["name"],
                             "source_file": o["source_file"]}, default=str) + "\n")
        cur.execute("UPDATE entities SET valid_until = now() WHERE id = %s AND valid_until IS NULL",
                    (o["id"],))
        retired += cur.rowcount
    conn.commit()
    j.close()
    print(f"\n  APPLIED — journal: {journal}")
    print(f"    rows soft-retired: {retired:>7,}")
    print(f"    rows skipped (had edges, unchanged): {len(plan['skip_has_edges']):>7,}")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--journal", help="journal path; REQUIRED with --apply")
    ap.add_argument("--json", help="write the dry-run plan here")
    args = ap.parse_args()

    if args.apply and not args.journal:
        print("--apply requires --journal: an unreversible bulk retirement is not acceptable.")
        return 2

    conn = connect(admin=args.apply)
    cur = conn.cursor()
    plan = build_plan(cur)
    report(plan)

    if args.json:
        Path(args.json).write_text(json.dumps(plan, indent=2, default=str))
        print(f"\n  plan written: {args.json}")

    if not args.apply:
        print("\n  DRY RUN — nothing written. Re-run with --apply --journal <path> to execute.")
        conn.close()
        return 0

    return apply_plan(conn, cur, plan, Path(args.journal))


if __name__ == "__main__":
    sys.exit(main())
