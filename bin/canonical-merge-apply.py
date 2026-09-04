#!/usr/bin/env python3
"""canonical-merge-apply.py — execute the canonical merge for ONE org, journaled and reversible.

Design: Fable, 2026-08-28. Dry-run half: bin/canonical-merge-dryrun.py (shares the group logic).
Run one org at a time, smallest first. Requires --confirm; refuses if the brain lock is held.

SAFETY, and the mechanism behind each choice:
  * SOFT-DELETE, NEVER DELETE. `DELETE` fires ON DELETE CASCADE on evidence.entity_id,
    entity_edges.from/to and workflow_steps — it would destroy rows we never meant to touch.
    Losers get valid_until = now() + superseded_by = survivor. Every retrieval leg filters
    `valid_until IS NULL`, so they leave recall cleanly, and `as_of` history stays exact.
  * ONE TRANSACTION PER ORG. MVCC then gives every concurrent reader either fully-pre or
    fully-post state. Never batched within an org: 10k row-writes is seconds, and batching
    sells consistency for nothing.
  * FULL-ROW JOURNAL. Every action stores to_jsonb(row) BEFORE the change. A description of a
    change is not a restore path; a row snapshot is.
  * BRAIN LOCK. Refuse rather than queue, matching si-drain.sh's posture.

Usage: python3 bin/canonical-merge-apply.py --org 5 --confirm
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scheduling", "brain-pg"))
import _env  # noqa: E402

sys.path.insert(0, HERE)
from importlib import import_module
_dry = import_module("canonical-merge-dryrun".replace("-", "_")) if False else None

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
JOURNAL = "merge_journal_20260828"


def eligible(kinds):
    if kinds & NEVER_FOLD:
        return False
    if kinds <= CONCEPT_KINDS:
        return True
    if len(kinds) == 1 and kinds <= REASONING_KINDS:
        return True
    return False


def brain_lock_held():
    brain = os.environ.get("CORE_BRAIN", os.path.expanduser("~/AI Projects/core-brain"))
    h = subprocess.run(["md5", "-q", "-s", brain], capture_output=True, text=True)
    digest = h.stdout.strip() or hashlib.md5(brain.encode()).hexdigest()
    return os.path.isdir(f"/tmp/core-brain-{digest}.lock")


def main():
    if "--org" not in sys.argv:
        print("need --org N"); return 2
    org = int(sys.argv[sys.argv.index("--org") + 1])
    confirm = "--confirm" in sys.argv
    if brain_lock_held():
        print("brain lock HELD — refusing (rerun when clear)"); return 1

    conn = _env.connect_corebrain_admin()   # BYPASSRLS: a loser can be an endpoint on another org's edge row
    cur = conn.cursor()
    batch = f"{ORGS.get(org, org)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # The journal table is created by a superuser migration, not here — brain_admin is
    # NOSUPERUSER and cannot CREATE in public, and a merge that silently ran without a journal
    # would be an unreversible merge. Refuse loudly instead.
    cur.execute("SELECT to_regclass(%s)", (JOURNAL,))
    if cur.fetchone()[0] is None:
        print(f"FATAL: journal table {JOURNAL} missing — refusing to merge without a restore path")
        conn.rollback()
        return 1

    # ── build the map, identically to the dry run ──
    cur.execute("""
        SELECT e.name, e.id, e.kind, e.scope, e.source_file,
               e.last_compiled_at IS NOT NULL, e.created_at,
               (SELECT count(*) FROM entity_edges g
                 WHERE g.from_entity_id=e.id OR g.to_entity_id=e.id)
        FROM entities e WHERE e.org_id=%s AND e.valid_until IS NULL
        ORDER BY lower(e.name)""", (org,))
    groups = {}
    for r in cur.fetchall():
        groups.setdefault(_norm(r[0]), []).append(r[1:])

    pairs = []          # (loser_id, survivor_id)
    for lname, rows in groups.items():
        if len(rows) < 2:
            continue
        kinds = {r[1] for r in rows}
        scopes = {r[2] for r in rows if r[2]}
        if not eligible(kinds):
            continue
        if "private" in scopes and len(scopes) > 1:
            continue
        hub = [r for r in rows if r[3] and ("/entities/" in r[3] or "/topics/" in r[3])]
        pool = hub or [r for r in rows if r[4]] or rows
        surv = sorted(pool, key=lambda r: (-r[6], r[5], r[0]))[0]
        for r in rows:
            if r[0] != surv[0]:
                pairs.append((r[0], surv[0]))

    if not pairs:
        print(f"org {org}: nothing to merge"); conn.rollback(); return 0
    m = dict(pairs)
    losers = list(m)
    print(f"org {ORGS.get(org,org)}: {len(set(m.values()))} survivors · {len(losers)} losers")

    # HARD PRECONDITION — same-org only. A cross-org fold would destroy the partition model.
    cur.execute("SELECT count(*) FROM entities WHERE id = ANY(%s) AND org_id <> %s", (losers, org))
    assert cur.fetchone()[0] == 0, "cross-org loser in map — ABORT"

    if not confirm:
        print("dry pass only (no --confirm) — rolling back"); conn.rollback(); return 0

    # Capture pre-existing self-edges BEFORE any write. The gate below must answer "did the merge
    # create one", not "does the graph contain one" — orgs carry 215/49/32/22 pre-existing
    # `references` self-edges that have nothing to do with this. ops happened to have zero, so a
    # global assert passed there and tripped on every other org: a gate that fires on unrelated
    # state is a gate that gets disabled, and this one nearly was.
    cur.execute("SELECT id FROM entity_edges WHERE from_entity_id = to_entity_id")
    preexisting_self = {r[0] for r in cur.fetchall()}

    def jrn(action, table, pk, loser, surv, old):
        cur.execute(f"INSERT INTO {JOURNAL} (batch_id,org_id,action,table_name,row_pk,loser_id,survivor_id,old_row)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (batch, org, action, table, pk, loser, surv, old))

    # ── edges ──
    cur.execute("""SELECT id, from_entity_id, to_entity_id, edge_type, to_jsonb(entity_edges.*)
                   FROM entity_edges WHERE from_entity_id = ANY(%s) OR to_entity_id = ANY(%s)""",
                (losers, losers))
    edges = cur.fetchall()
    repointed = dropped_self = dropped_dup = 0
    for eid, f, t, et, old in edges:
        nf, nt = m.get(f, f), m.get(t, t)
        if nf == nt:
            jrn("drop_self_edge", "entity_edges", eid, f if f in m else t, nf, json.dumps(old))
            cur.execute("DELETE FROM entity_edges WHERE id=%s", (eid,)); dropped_self += 1; continue
        cur.execute("SELECT id FROM entity_edges WHERE from_entity_id=%s AND to_entity_id=%s "
                    "AND edge_type=%s AND id<>%s", (nf, nt, et, eid))
        if cur.fetchone():
            jrn("drop_dup_edge", "entity_edges", eid, None, None, json.dumps(old))
            cur.execute("DELETE FROM entity_edges WHERE id=%s", (eid,)); dropped_dup += 1; continue
        jrn("repoint_edge", "entity_edges", eid, None, None, json.dumps(old))
        cur.execute("UPDATE entity_edges SET from_entity_id=%s, to_entity_id=%s, "
                    "is_cross_org=(SELECT a.org_id<>b.org_id FROM entities a, entities b "
                    "WHERE a.id=%s AND b.id=%s) WHERE id=%s", (nf, nt, nf, nt, eid))
        repointed += 1
    assert repointed + dropped_self + dropped_dup == len(edges), "journal arithmetic does not balance"

    # ── other id holders ──
    # Other id holders. pattern_promotions.entity_id has NO foreign key — unremapped it goes
    # silently wrong with zero errors, which is exactly the failure class this whole session is
    # about. A missing table is survivable; any OTHER error aborts, because a partial remap is
    # worse than no merge.
    for tbl, col in (("evidence", "entity_id"), ("pattern_promotions", "entity_id")):
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        if cur.fetchone()[0] is None:
            print(f"  (table {tbl} absent on this seat — skipped)")
            continue
        cur.execute(f"SELECT id, {col} FROM {tbl} WHERE {col} = ANY(%s)", (losers,))
        rows = cur.fetchall()
        for rid, old_id in rows:
            jrn(f"repoint_{tbl}", tbl, rid, old_id, m[old_id], json.dumps({col: old_id}))
            cur.execute(f"UPDATE {tbl} SET {col}=%s WHERE id=%s", (m[old_id], rid))
        print(f"  {tbl}.{col}: {len(rows)} remapped")

    # ── soft-delete the losers ──
    cur.execute("SELECT id, to_jsonb(entities.*) FROM entities WHERE id = ANY(%s)", (losers,))
    for lid, old in cur.fetchall():
        jrn("tombstone_entity", "entities", lid, lid, m[lid], json.dumps(old))
    cur.execute("UPDATE entities SET valid_until=now(), superseded_by=v.s FROM (VALUES %s) "
                "AS v(l,s) WHERE entities.id=v.l" % ",".join(f"({l},{s})" for l, s in m.items()))

    # ── verification, inside the transaction — any failure rolls the whole thing back ──
    cur.execute("SELECT count(*) FROM entity_edges WHERE from_entity_id=ANY(%s) OR to_entity_id=ANY(%s)",
                (losers, losers))
    assert cur.fetchone()[0] == 0, "edges still reference a loser"
    # Scoped to THIS batch's survivors, not the whole graph. The first cut asserted globally and
    # tripped on 318 pre-existing `references` self-edges that have nothing to do with the merge
    # (zero of them in ops). A gate that fires on unrelated pre-existing state is a gate that
    # gets disabled. The question is only: did the merge CREATE a self-edge?
    cur.execute("SELECT id FROM entity_edges WHERE from_entity_id = to_entity_id")
    new_self = {r[0] for r in cur.fetchall()} - preexisting_self
    assert not new_self, f"merge CREATED {len(new_self)} self-edge(s): {sorted(new_self)[:5]}"
    cur.execute("SELECT count(*) FROM entities WHERE id=ANY(%s) AND (valid_until IS NULL OR superseded_by IS NULL)", (losers,))
    assert cur.fetchone()[0] == 0, "a loser was not properly tombstoned"
    cur.execute("SELECT count(*) FROM entities WHERE id=ANY(%s) AND valid_until IS NOT NULL",
                (list(set(m.values())),))
    assert cur.fetchone()[0] == 0, "a SURVIVOR was tombstoned"
    cur.execute("""SELECT count(*) FROM (SELECT lower(name) FROM entities
                   WHERE org_id=%s AND valid_until IS NULL AND kind NOT IN ('Source','Workflow')
                   GROUP BY 1 HAVING count(*)>1) t""", (org,))
    left = cur.fetchone()[0]

    conn.commit()
    print(f"  ✓ committed batch {batch}")
    print(f"    edges: {repointed} repointed · {dropped_self} self dropped · {dropped_dup} dup dropped")
    print(f"    entities tombstoned: {len(losers)}")
    print(f"    remaining dup groups in org (incl. refused kinds): {left}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
