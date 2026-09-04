#!/usr/bin/env python3
"""canonical-merge-dryrun.py — what a within-Core canonical merge WOULD do. Writes nothing.

Designed by Fable 2026-08-28 against the live schema; this implements the read-only half.
The merge collapses rows that are the same subject split by `kind` or by letter case, because
`UNIQUE (org_id, kind, name)` makes both part of identity.

WHAT IT WILL NEVER TOUCH, and why (these are correctness boundaries, not caution):

  Source    names are file paths. Folding them is meaningless, and `pass_hubs` would recreate them.
  Workflow  three hard dependencies on BOTH id and kind: workflow_steps has a FK plus
            UNIQUE(workflow_entity_id, step_index) — merging two Workflows with steps 1..n is a
            constraint violation with no defined semantics; si_artifacts ids are literally
            `wf_<entity_id>`; and query.py:287 joins on kind='Workflow' to attach steps, so a
            folded Workflow loses its steps silently.
  Reasoning kinds (Decision/Lesson/Rule/Incident) are never folded INTO or OUT OF concept kinds.
            embed.py:472 OVERWRITES compiled_truth_md for reasoning kinds on re-ingest and only
            COALESCE-protects concept kinds — so a Topic's synthesized truth living under a Rule
            survivor gets clobbered by the next nightly. Case-folding *within* one reasoning kind
            is safe and is included.
  Mixed scope  a group containing a 'private' row is NEVER auto-merged. Folding private into a
            shared survivor is a cross-org content leak through the visibility filter. Reported
            for manual review instead. Fails closed.

CROSS-ORG IS NOT A DEFECT. The same concept in life and business is the partition model working;
corroborate.py bridges those deliberately with same_as. Grouping is strictly within one org_id.
(I reported cross-org duplication as corruption earlier on 2026-08-28. It is not.)

SURVIVOR PRECEDENCE (Fable): hub-owned row first — not because it is "best" but because pass_hubs
will keep upserting that exact (org, kind, name) forever, so any other choice means future truth
and embeddings flow into a tombstone while the live survivor silently starves. Then: a row with
compiled truth, then highest degree, then oldest id.

Usage:  python3 bin/canonical-merge-dryrun.py [--org N] [--json]
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scheduling", "brain-pg"))
import _env  # noqa: E402

CONCEPT_KINDS = {"Entity", "Topic", "Project", "Tool"}
REASONING_KINDS = {"Decision", "Lesson", "Rule", "Incident"}
NEVER_FOLD = {"Source", "Workflow"}

# SEPARATOR NORMALISATION (2026-08-29). The first merge folded case and kind only, so
# `core-business`, `Core business` and `Core Business` stayed three entities. Measured after that
# merge: 2,545 further duplicate groups / 2,713 extra rows, 2,226 of them in life. compile-truth's
# own partition step ALREADY normalises `[\s_-]+` when matching hubs — so the pipeline downstream
# knew these were the same subject while the entity table did not, and compiling them would have
# paid to synthesise the same hub two or three times.
def _norm(s):
    import re
    return re.sub(r"[\s_-]+", " ", str(s)).strip().lower()

ORGS = {1: "life", 2: "business", 3: "school", 4: "finance", 5: "ops"}


def eligible(kinds: set[str]) -> tuple[bool, str]:
    if kinds & NEVER_FOLD:
        return False, f"contains {sorted(kinds & NEVER_FOLD)[0]} — never folded"
    if kinds <= CONCEPT_KINDS:
        return True, "concept-kind fold"
    if len(kinds) == 1 and kinds <= REASONING_KINDS:
        return True, "case fold within one reasoning kind"
    if kinds & REASONING_KINDS and kinds & CONCEPT_KINDS:
        return False, "reasoning×concept — embed.py:472 would clobber the truth"
    return False, f"unhandled kind mix {sorted(kinds)}"


def main() -> int:
    as_json = "--json" in sys.argv
    only_org = None
    if "--org" in sys.argv:
        only_org = int(sys.argv[sys.argv.index("--org") + 1])

    conn = _env.connect_corebrain()
    cur = conn.cursor()

    cur.execute("""
        SELECT e.org_id, e.name, e.id, e.name, e.kind, e.scope, e.source_file,
               e.last_compiled_at IS NOT NULL, e.created_at,
               (SELECT count(*) FROM entity_edges g
                 WHERE g.from_entity_id = e.id OR g.to_entity_id = e.id)
        FROM entities e
        WHERE e.valid_until IS NULL
        ORDER BY e.org_id, lower(e.name)
    """)
    groups = defaultdict(list)
    for row in cur.fetchall():
        groups[(row[0], _norm(row[1]))].append(row[2:])

    out = {"merge": [], "refused": [], "manual_review": []}
    tot_removed = tot_edges = 0
    per_org = defaultdict(lambda: {"groups": 0, "removed": 0, "edges": 0})

    for (org, lname), rows in groups.items():
        if len(rows) < 2:
            continue
        if only_org and org != only_org:
            continue
        kinds = {r[2] for r in rows}
        ok, why = eligible(kinds)
        scopes = {r[3] for r in rows if r[3]}
        rec = {"org": ORGS.get(org, org), "name": lname, "rows": len(rows),
               "kinds": sorted(kinds), "edges": sum(r[7] for r in rows)}
        if "private" in scopes and len(scopes) > 1:
            rec["reason"] = "mixed scope — private present; fails closed"
            out["manual_review"].append(rec); continue
        if not ok:
            rec["reason"] = why
            out["refused"].append(rec); continue
        hub = [r for r in rows if r[4] and ("/entities/" in r[4] or "/topics/" in r[4])]
        pool = hub or [r for r in rows if r[5]] or rows
        survivor = sorted(pool, key=lambda r: (-r[7], r[6], r[0]))[0]
        losers = [r for r in rows if r[0] != survivor[0]]
        rec.update({"survivor": f"{survivor[2]}/{survivor[1]} (id {survivor[0]}, {survivor[7]} edges)",
                    "survivor_basis": "hub-owned" if hub else ("has compiled truth" if survivor[5] else "highest degree"),
                    "removed": len(losers),
                    "edges_repointed": sum(r[7] for r in losers)})
        out["merge"].append(rec)
        tot_removed += len(losers); tot_edges += rec["edges_repointed"]
        per_org[ORGS.get(org, org)]["groups"] += 1
        per_org[ORGS.get(org, org)]["removed"] += len(losers)
        per_org[ORGS.get(org, org)]["edges"] += rec["edges_repointed"]

    conn.close()
    summary = {"groups_to_merge": len(out["merge"]), "entities_removed": tot_removed,
               "edges_repointed": tot_edges, "refused": len(out["refused"]),
               "manual_review": len(out["manual_review"]), "per_org": dict(per_org)}

    if as_json:
        print(json.dumps({"summary": summary, **out}, indent=1, default=str))
        return 0

    print("═══ CANONICAL MERGE — DRY RUN (nothing written) ═══\n")
    print(f"  would merge      {summary['groups_to_merge']} groups")
    print(f"  entities removed {summary['entities_removed']}  (soft-delete: valid_until + superseded_by)")
    print(f"  edges re-pointed {summary['edges_repointed']}")
    print(f"  REFUSED          {summary['refused']} groups (kind rules)")
    print(f"  manual review    {summary['manual_review']} groups (mixed scope)\n")
    print("  per Core:")
    for k, v in sorted(per_org.items(), key=lambda x: -x[1]["edges"]):
        print(f"    {k:10} {v['groups']:>4} groups · {v['removed']:>4} removed · {v['edges']:>6} edges")
    print("\n  biggest merges:")
    for r in sorted(out["merge"], key=lambda x: -x["edges_repointed"])[:8]:
        print(f"    {r['org']:9} {r['name'][:30]:32} {r['rows']}→1  {r['edges_repointed']:>5} edges  [{r['survivor_basis']}]")
    if out["refused"]:
        print("\n  refused (correctness boundaries, not caution):")
        seen = set()
        for r in out["refused"]:
            if r["reason"] in seen:
                continue
            seen.add(r["reason"])
            print(f"    {r['reason']}  — e.g. {r['org']}/{r['name'][:34]}")
    if out["manual_review"]:
        print("\n  ⚠ MIXED SCOPE — never auto-merged (private→shared would leak across Cores):")
        for r in out["manual_review"][:5]:
            print(f"    {r['org']}/{r['name'][:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
