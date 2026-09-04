#!/usr/bin/env python3
"""Give every flat vault hub the org it actually belongs to. DRY-RUN BY DEFAULT.

WHY THIS EXISTS (2026-08-31)
============================
The vault has two tiers. `projects/<slug>/sessions|subagents/` is per-Core, and graph nodes built
from it correctly carry their ORIGIN Core's org. The hub tier — `entities/`, `topics/`, `tools/` —
is one flat namespace of 7,661 files with no per-Core subdivision, and `embed.py::discover_hubs()`
globs it with no filter. `upsert_entities()` then stamps whatever it finds with the org of
WHICHEVER CORE HAPPENS TO BE RUNNING; its docstring calls these "global content".

That assumption was true with one Core and is false with five. Measured consequence: 61% of the
OPS Core's entities came from the flat pool rather than from OPS, so the highest-degree hubs in
a business Core's partition were the operator's PERSONAL entities, not the business's.

THE OWNERSHIP SIGNAL WAS ALWAYS IN THE FILES. Each hub cites the sessions it was built from, as
`projects/<slug>/...` paths. Parsing all 7,661: 88.3% cite exactly one Core, 4.0% cite two or more,
7.6% cite none resolvable. Ownership is therefore derivable, deterministically, with no model.

WHAT THIS IS NOT
================
NOT `bin/repartition-misfiled.py`. That tool repairs a different condition (project-path provenance
naming one wrong Core) and adversarial review found four reasons it cannot be reused here:
its selector requires `/core-<slug>` in source_file and flat hub paths do not encode a Core; it
updates only `entities.org_id` and never `entity_edges.org_id`, Source hubs or ingest_log; it
detects duplicates by NAME while the real key is `(org_id, kind, name)`, so a bulk update collides;
and it connects as `brain_app`, which RLS correctly refuses for cross-org writes.

SAFETY
======
* Dry-run by default. `--apply` is required to write, and refuses without `--journal`.
* NOTHING is DELETEd. Every FK into entities is ON DELETE CASCADE (entity_edges x2, evidence,
  workflow_steps, workflow_triggers), so a DELETE here would silently take derived data with it.
  Duplicates are soft-retired: `valid_until = now(), superseded_by = <survivor>`.
* Every action is journaled BEFORE it is applied, with enough state to reverse it, and
  **`--undo <journal>` IS NOW IMPLEMENTED** (2026-09-02, step two of the ops/Nick sequence:
  lock parity first, this second, then a one-org --apply). This line previously said
  "(`--undo`)" unqualified while the flag itself refused — core-ops read the docstring,
  believed the operation was reversible, and told the operator so twice before learning
  otherwise from a bus message rather than from the code (bus #5875, 2026-09-02). Two of the
  seven journaled action kinds (`edge_repoint`, `edge_reorg`) recorded only aggregate ROW
  COUNTS, not the prior values needed to reverse them, and `revive`/`truth_carry` recorded
  the NEW state but not the state they overwrote — all four were fixed on the RECORDING side
  (apply_plan now snapshots the pre-image before every mutating statement) so a journal
  written by THIS revision is actually reversible. A journal written by an OLDER revision is
  not — `--undo` detects the missing fields per entry and refuses that entry (and therefore
  the whole run; see below) rather than guessing.
  `--undo` is two-phase: (1) VERIFY every entry's recorded post-state against the live DB —
  an unknown action kind or a drifted row aborts here, nothing written; (2) only if every
  entry verifies clean, apply every reversal and commit ONCE. Idempotent via a
  `<journal>.undone` marker file written on success — a second `--undo` on the same journal
  no-ops rather than re-applying (phase 1 would also refuse a second run on its own, since
  the DB now holds the prior state rather than the post-state every entry expects, but the
  marker gives a specific message instead of a generic drift refusal).
* Collisions are the normal case, not the exception: 14,825 (kind,name) pairs exist in more than
  one org. Survivor selection is per (kind, name) group and never blind-updates into a taken key.
* `compiled_truth_md` is carried FORWARD to the survivor when the survivor lacks it. Without this,
  retiring a duplicate discards LLM-synthesised hub text that was paid for.

Usage:
  python3 bin/repartition-hubs.py                  # dry-run report
  python3 bin/repartition-hubs.py --json out.json  # dry-run + machine-readable plan
  python3 bin/repartition-hubs.py --apply --journal j.jsonl
  python3 bin/repartition-hubs.py --undo j.jsonl   # reverse a journal written by --apply
  python3 bin/repartition-hubs.py --only-org 5     # dry-run: just the org-5 slice
  python3 bin/repartition-hubs.py --apply --journal j.jsonl --only-org 5
  python3 bin/repartition-hubs.py --undo j.jsonl --only-org 5  # refuses if j.jsonl wasn't scoped to 5

--only-org (2026-09-02, gate 3 of the ops/Nick sequence: "apply on ONE org and verify"). The
tool had no way to restrict a write to one org before this — every --apply was fleet-wide across
all five orgs or nothing. See parse_only_org() and the ORG SCOPE comment in build_plan() for the
exact per-action-kind membership rule (moves: either side; retires/revives: the row's own org;
edges: follow whichever entity is already in scope) and why a (kind,name) collision group is the
unit of inclusion, not the individual action.

**The org-N slice is NOT "the org-N rows out of the fleet-wide plan" — read before reconciling.**
The unscoped report buckets a MOVE by `to_org` only ("reassignment targets") and a RETIRE by
`org_id` only ("duplicate rows retired, by the org losing them"). --only-org's either-side rule for
moves additionally counts moves where org N is the *source* (`from_org`), which that fleet bucket
never counted — so a scoped move total is >= the fleet "reassignment targets" figure for that org,
not equal to it. And any collision group whose actions span an included and an excluded org is
deferred WHOLE rather than half-applied, which can make a scoped total SMALLER than the fleet
figure for groups that get deferred. The scoped report prints both the fleet-comparable number and
the extra, and lists every deferred group with its reason — a smaller-than-expected number with no
accounting is indistinguishable from a bug.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# CORE_INSTANCE / CLAUDE_PROJECT_DIR FIRST, __file__ ONLY AS FALLBACK (2026-08-31).
#
# This anchored purely on __file__ — `Path(__file__).resolve().parent.parent` — with no
# environment override, caught by bin/tests/test_root_anchors.py as the one remaining VULN in
# bin/*.py. Same defect class as bin/casebook-run.py::_repo_root() before it was repointed at
# bin/core_seat.py (2026-08-10, core-business #914): a repair tool that resolves which tree's
# `scheduling/brain-pg` (and therefore which tree's hub_ownership.py / entities / entity_edges)
# it operates on from the wrong anchor answers confidently about the wrong Core. This script
# WRITES (--apply mutates entities.org_id and entity_edges.org_id), so the wrong-seat failure
# mode here is not a bad report — it is silently repartitioning another Core's graph.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core_seat import seat_root  # noqa: E402

sys.path.insert(0, str(seat_root(fallback=Path(__file__).resolve().parents[1]) / "scheduling" / "brain-pg"))

# THE ownership rule lives in scheduling/brain-pg/hub_ownership.py and is imported by BOTH this
# repair tool and embed.py, the ingester. Two copies would drift, and the failure mode of drift here
# is an oscillation — each pass recreating what the other retired — that looks like corruption.
from hub_ownership import (SHARED_ORG, DOMINANCE, SLUG_TO_ORG, ORG_NAME,  # noqa: E402
                           owner_for_path)


def brain_root() -> Path:
    """RESTORED (2026-08-31). The ownership-rule consolidation above (204fc5a, same day) deleted
    this function along with the ownership logic it sat beside, but `main()` still calls
    `brain = brain_root()` (line ~321) — left uncalled by any test (test_root_anchors.py only
    inspects source text; nothing here executes main()), so the NameError this left behind was
    silent until the script was actually run. Not part of the ownership-rule consolidation this
    file's other comment describes: that was about the ORG classification logic (which lived in
    two places and could drift); $CORE_BRAIN resolution is a one-line env lookup every brain-pg
    script under bin/ and scheduling/ repeats independently already (embed.py, compile-truth.py,
    compile-truth-refresh.py, capture_worker.py all do the identical `os.environ.get("CORE_BRAIN")
    or sys.exit`) — restoring it here, not centralizing it, matches the convention actually in use.
    """
    v = os.environ.get("CORE_BRAIN")
    if not v:
        sys.exit("ERROR: $CORE_BRAIN required.")
    return Path(v)


def classify(path: Path) -> int | None:
    return owner_for_path(path)


def hub_files(brain: Path) -> list[Path]:
    out: list[Path] = []
    for sub in ("entities", "topics", "tools"):
        d = brain / sub
        if d.is_dir():
            out += sorted(d.glob("*.md"))
    return out


def connect(admin: bool):
    """brain_admin for writes — it is the only role that may write across orgs.

    RLS `insert_own`/`update_own` are org-scoped and brain_app is NOBYPASSRLS, so a cross-org
    reassignment as brain_app is silently refused. repartition-misfiled.py's own comments record
    exactly that, measured. Reads work as either role because the SELECT policy is `read_all`.
    """
    import psycopg2
    db = os.environ.get("COREBRAIN_DB", "corebrain")
    user = os.environ.get("BRAIN_ADMIN_USER", "brain_admin") if admin else None
    return psycopg2.connect(dbname=db, user=user) if user else psycopg2.connect(dbname=db)


def enforce_domain_claims(conn, cur, journal: Path, apply: bool) -> dict:
    """Move every hub whose NAME is claimed by a Core's domain into that Core.

    Separate from build_plan because it answers a different question. build_plan asks "whose
    sessions produced this text"; this asks "whose subject is this". A hub citing no resolvable
    session has no answer to the first question and a perfectly good answer to the second — which
    is how several real course-code hubs and the LMS name were left sitting among the OPS Core's
    top-degree hubs after the citation pass had already run.

    Same collision discipline as everywhere else: UNIQUE(org_id, kind, name) is not partial, so the
    target slot may be held by a live row (retire into it), by a tombstone (revive it, retire into
    it), or be free (move).
    """
    from hub_ownership import claimed_by_domain
    cur.execute("SELECT id, org_id, kind, name FROM entities "
                "WHERE valid_until IS NULL AND kind IN ('Entity','Project','Topic','Tool')")
    rows = cur.fetchall()
    cur.execute("SELECT org_id, kind, name, id, valid_until IS NULL FROM entities")
    slot: dict = {}
    for o, k_, n_, i_, livep in cur.fetchall():
        slot.setdefault((o, k_, n_), []).append((i_, livep))

    moved = revived = retired = 0
    j = journal.open("a") if apply else None
    for eid, org, kind, name in rows:
        target = claimed_by_domain(name)
        if target is None or target == org:
            continue
        occ = [x for x in slot.get((target, kind, name), []) if x[0] != eid]
        live = [x for x in occ if x[1]]
        dead = [x for x in occ if not x[1]]
        if live:
            if apply:
                j.write(json.dumps({"op": "domain_retire", "id": eid, "from_org": org,
                                    "survivor": live[0][0]}) + "\n")
                cur.execute("UPDATE entities SET valid_until=now(), superseded_by=%s "
                            "WHERE id=%s AND valid_until IS NULL", (live[0][0], eid))
            retired += 1
        elif dead:
            if apply:
                j.write(json.dumps({"op": "domain_revive_retire", "revive": dead[0][0],
                                    "id": eid, "from_org": org}) + "\n")
                cur.execute("UPDATE entities SET valid_until=NULL, superseded_by=NULL WHERE id=%s",
                            (dead[0][0],))
                cur.execute("UPDATE entities SET valid_until=now(), superseded_by=%s "
                            "WHERE id=%s AND valid_until IS NULL", (dead[0][0], eid))
            slot[(target, kind, name)] = [(dead[0][0], True)] + occ
            revived += 1
            retired += 1
        else:
            if apply:
                j.write(json.dumps({"op": "domain_move", "id": eid, "from_org": org,
                                    "to_org": target}) + "\n")
                cur.execute("UPDATE entities SET org_id=%s WHERE id=%s AND valid_until IS NULL",
                            (target, eid))
            slot[(target, kind, name)] = [(eid, True)]
            moved += 1
    if apply:
        conn.commit()
        j.close()
    return {"moved": moved, "revived": revived, "retired": retired}


def parse_only_org(raw: list[str] | None) -> set[int] | None:
    """`--only-org` accepts repeats AND comma-lists (`--only-org 5`, `--only-org 2,5`, or both
    combined) so ops can name exactly the org(s) it needs without the tool caring which spelling
    was used. Returns None (no filter — the historical, fleet-wide behavior) when the flag was
    never passed; exits with a message on anything that isn't a bare non-negative integer, since a
    typo'd org id here silently scopes an --apply to the wrong slice."""
    if not raw:
        return None
    orgs: set[int] = set()
    for item in raw:
        for piece in item.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if not piece.lstrip("-").isdigit() or int(piece) < 0:
                sys.exit(f"--only-org: {piece!r} is not a non-negative integer org id")
            orgs.add(int(piece))
    if not orgs:
        sys.exit("--only-org given but no org ids parsed out of it")
    return orgs


def build_plan(cur, brain: Path, only_orgs: set[int] | None = None) -> dict:
    files = hub_files(brain)
    target_by_path = {str(p): classify(p) for p in files}

    # TWO-STAGE LOAD, and the second stage is not optional.
    #
    # A first cut selected only rows whose source_file is a flat hub path, then moved a survivor
    # into its target org. It aborted on `(org_id, kind, name)=(2, Entity, Acme Research Institute)
    # already exists` — because the row occupying that key is a GRAPH NODE built from
    # projects/business/, which has a different source_file and so was invisible to the query that
    # was deciding whether the key was free. The unique key spans every live row in the org, so the
    # plan has to see every live row that shares a candidate (kind, name), whatever its source.
    # Load EVERY live entity and filter in Python. `(kind, name) IN (...)` with ~15,000 tuples
    # exceeds Postgres's max_stack_depth (2MB) — the planner recurses per tuple. The whole live set
    # is ~98k narrow rows, which is cheap to hold and makes the collision check exact rather than
    # dependent on how the key list was built.
    cur.execute(
        "SELECT id, org_id, kind, name, source_file, "
        "       (compiled_truth_md IS NOT NULL AND length(trim(compiled_truth_md))>0) AS has_truth, "
        "       last_compiled_at "
        "FROM entities WHERE valid_until IS NULL")
    all_live = cur.fetchall()
    hub_keys = {(r[2], r[3]) for r in all_live if r[4] in target_by_path}
    rows = [r for r in all_live if (r[2], r[3]) in hub_keys]

    # RETIRED rows still occupy the unique key. `UNIQUE (org_id, kind, name)` is NOT partial — there
    # is no `WHERE valid_until IS NULL` on it — so soft-retiring a row does not free its slot, and a
    # move into an org that ever held that (kind, name) collides even though nothing live is there.
    # Measured: 53 of 1,514 moves. For those the correct action is to REVIVE the tombstone in the
    # target org and make it the survivor, not to move a different row on top of a taken key.
    cur.execute(
        "SELECT id, org_id, kind, name FROM entities WHERE valid_until IS NOT NULL")
    tombstone = {}
    for tid, torg, tkind, tname in cur.fetchall():
        if (tkind, tname) in hub_keys:
            tombstone.setdefault((torg, tkind, tname), tid)

    # Every live row for this key, so survivor choice sees the taken keys it must not collide with.
    by_key: dict[tuple, list] = collections.defaultdict(list)
    for r in rows:
        by_key[(r[2], r[3])].append(r)

    moves, retires, keeps, revives, deferred = [], [], [], [], []
    unresolved = collections.Counter()

    for (kind, name), group in by_key.items():
        targets = {target_by_path.get(r[4]) for r in group if r[4] in target_by_path}
        targets.discard(None)
        if not targets:
            unresolved[kind] += len(group)
            continue
        # Rows that are NOT flat-pool hubs (graph nodes from projects/, correctly orged already)
        # are load-bearing context for collision checking but are never moved or retired here.
        non_hub = [r for r in group if r[4] not in target_by_path]
        # A key spanning several source files with different owners is itself cross-cutting.
        target = targets.pop() if len(targets) == 1 else SHARED_ORG

        hub_rows = [r for r in group if r[4] in target_by_path]
        in_target = [r for r in group if r[1] == target]          # incl. non-hub occupants
        others = [r for r in hub_rows if r[1] != target]

        pending_move = pending_revive = None
        if in_target:
            # Prefer a survivor that already holds synthesised truth; else lowest id (oldest).
            # freshest compile first, then oldest id as a stable tiebreak
            in_target.sort(key=lambda r: (not r[5], -(r[6].timestamp() if r[6] else 0), r[0]))
            survivor = in_target[0]
            # never retire a non-hub row: it is the org's own graph node, not a flat-pool copy
            doomed = [r for r in in_target[1:] if r[4] in target_by_path] + others
        elif (target, kind, name) in tombstone:
            revived_id = tombstone[(target, kind, name)]
            others.sort(key=lambda r: (not r[5], -(r[6].timestamp() if r[6] else 0), r[0]))
            pending_revive = {"id": revived_id, "org_id": target, "kind": kind, "name": name}
            survivor = (revived_id, target, kind, name, None, False, None)
            doomed = others
        else:
            others.sort(key=lambda r: (not r[5], -(r[6].timestamp() if r[6] else 0), r[0]))
            survivor = others[0]
            pending_move = {"id": survivor[0], "from_org": survivor[1], "to_org": target,
                            "kind": kind, "name": name}
            doomed = others[1:]

        # CARRY THE FRESHEST SYNTHESIS, not merely "some" synthesis (2026-08-31).
        #
        # The first cut only carried truth when the survivor had NONE. That looked right and was
        # measurably wrong: every survivor in this corpus already has text, so it carried nothing —
        # while one survivor's summary is 66 characters and the copy about to be retired holds a
        # full one compiled hours ago. compile-truth exists to replace stale hub text; a merge that
        # keeps whichever copy is older defeats it. Newest last_compiled_at wins, and a compiled row
        # beats an uncompiled one regardless of age.
        donor = None
        cands = [r for r in group if r[5] and r[0] != survivor[0] and r[6] is not None]
        if cands:
            cands.sort(key=lambda r: r[6], reverse=True)
            best = cands[0]
            if (not survivor[5]) or survivor[6] is None or best[6] > survivor[6]:
                donor = best[0]

        # A retire whose survivor is itself retired leaves the key with NO live row — measured on
        # the first run: life's frequently cited personal contact ended up tombstoned pointing at
        # a tombstone, so the hub survived only as an incidental finance graph node. The survivor
        # is chosen from this group and is never added to `doomed`, so assert it rather than trust it.
        assert survivor[0] not in {d[0] for d in doomed}, f"survivor in doomed for {kind}/{name}"
        pending_retires = [{"id": d[0], "org_id": d[1], "kind": kind, "name": name,
                            "superseded_by": survivor[0],
                            "truth_donor_for_survivor": donor == d[0]} for d in doomed]

        # ── ORG SCOPE (--only-org), per-action-kind, per ops/Nick gate 3 (2026-09-02) ──────────
        #
        # THE RULE, per action kind (documented here because it is not one rule, it is three):
        #   MOVE    — belongs to org N if N is EITHER from_org OR to_org. A move is inherently
        #             cross-org (from != to always), so "belongs to the org losing the row" and
        #             "belongs to the org gaining it" are both legitimate readings; excluding
        #             either would silently hide half of what --only-org N actually touches.
        #   RETIRE  — belongs to org N if N is the row's OWN org_id (the org that loses this
        #             specific duplicate). No either-side ambiguity: a retire has exactly one org.
        #   REVIVE  — belongs to org N if N is the row's OWN org_id (the org regaining the
        #             tombstone). Same single-org shape as retire.
        # These three predicates are evaluated per PENDING action below; a group can carry at most
        # one move-or-revive (never both) plus zero or more retires.
        #
        # THE GROUP IS THE UNIT OF INCLUSION, NOT THE ACTION. Survivor selection above already
        # picked ONE winner by looking at every row in this (kind,name) collision group; applying
        # only some of the resulting actions (say, retiring the in-scope duplicates but leaving an
        # out-of-scope duplicate live) would leave the key in a state survivor selection never
        # evaluated — two "duplicates" both still live, or a survivor pointed at by a retire that
        # never ran. So: if EVERY pending action for this group agrees (all in scope, or all out),
        # commit or skip the whole group normally. If they DISAGREE — some pass the filter, some
        # don't — the group is DEFERRED whole and reported, never half-applied.
        if only_orgs is not None:
            def _incl(org_id):
                return org_id in only_orgs
            flags = []
            if pending_move is not None:
                flags.append(_incl(pending_move["from_org"]) or _incl(pending_move["to_org"]))
            if pending_revive is not None:
                flags.append(_incl(pending_revive["org_id"]))
            flags.extend(_incl(r["org_id"]) for r in pending_retires)
            if not any(flags):
                continue                             # whole group outside the requested scope
            if not all(flags):
                deferred.append({"kind": kind, "name": name,
                                 "group_orgs": sorted({r[1] for r in group} | {target})})
                continue                             # split — defer whole group, never half-apply

        if in_target:
            keeps.append(survivor)
        if pending_move is not None:
            moves.append(pending_move)
        if pending_revive is not None:
            revives.append(pending_revive)
        retires.extend(pending_retires)
        if donor is not None:
            keeps.append(survivor)

    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "hub_files": len(files), "live_hub_rows": len(rows),
            "moves": moves, "revives": revives, "retires": retires,
            "unresolved_rows": sum(unresolved.values()),
            "truth_carries": sum(1 for r in retires if r.get("truth_donor_for_survivor")),
            "only_org": sorted(only_orgs) if only_orgs else None,
            "deferred_groups": deferred}


def report(plan: dict) -> None:
    only_orgs = set(plan["only_org"]) if plan.get("only_org") else None
    if only_orgs:
        print(f"  --only-org {sorted(only_orgs)} active — membership rule (per action kind):")
        print(f"    MOVE            in scope if EITHER from_org OR to_org is in {sorted(only_orgs)}")
        print(f"    RETIRE / REVIVE in scope if the row's OWN org_id is in {sorted(only_orgs)}")
        print( "    a (kind,name) collision GROUP is the unit of inclusion: it commits only when")
        print( "    EVERY action it produces agrees (all in scope, or all out) — a group that")
        print( "    straddles included and excluded orgs is DEFERRED whole, never half-applied")
        print( "    (see 'DEFERRED' below). This slice is therefore NOT the org-N rows carved out")
        print( "    of the fleet-wide plan — see the module docstring's --only-org section.")
        print()
    print(f"  hub files on disk        : {plan['hub_files']:>7,}")
    print(f"  live hub rows in DB      : {plan['live_hub_rows']:>7,}")
    print(f"  rows to REASSIGN (move)  : {len(plan['moves']):>7,}")
    print(f"  tombstones REVIVED       : {len(plan.get('revives', [])):>7,}")
    print(f"  rows to RETIRE (soft)    : {len(plan['retires']):>7,}")
    print(f"  compiled_truth carried   : {plan['truth_carries']:>7,}")
    print(f"  rows left alone (unresolved source): {plan['unresolved_rows']:>7,}")
    dist = collections.Counter(m["to_org"] for m in plan["moves"])
    if dist:
        print("  reassignment targets (to_org):")
        for org, n in sorted(dist.items()):
            print(f"    org {org} ({ORG_NAME.get(org,'?'):<8}) {n:>6,}")
    if only_orgs:
        # Per-org either-side breakdown for MOVES, so a scoped total can be reconciled against the
        # fleet's "reassignment targets" bucket above (which counts to_org only, by construction).
        for org in sorted(only_orgs):
            total = sum(1 for m in plan["moves"] if org in (m["from_org"], m["to_org"]))
            losing = sum(1 for m in plan["moves"] if m["from_org"] == org)
            receiving = sum(1 for m in plan["moves"] if m["to_org"] == org)
            print(f"  org {org} ({ORG_NAME.get(org,'?')}) moves — in scope under either-side rule: {total:>5,}")
            print(f"    of which this org is the LOSING side (from_org)   : {losing:>5,}  "
                  f"[NOT in the fleet 'reassignment targets' bucket above]")
            print(f"    of which this org is the RECEIVING side (to_org)  : {receiving:>5,}  "
                  f"[= the fleet 'reassignment targets' bucket, minus any deferred]")
    freed = collections.Counter(r["org_id"] for r in plan["retires"])
    if freed:
        print("  duplicate rows retired, by the org losing them:")
        for org, n in sorted(freed.items()):
            print(f"    org {org} ({ORG_NAME.get(org,'?'):<8}) {n:>6,}")
    if only_orgs:
        for org in sorted(only_orgs):
            r_total = sum(1 for r in plan["retires"] if r["org_id"] == org)
            v_total = sum(1 for r in plan.get("revives", []) if r["org_id"] == org)
            print(f"  org {org} ({ORG_NAME.get(org,'?')}) retires — in scope under org_id rule: {r_total:>5,}  "
                  f"[org_id IS the losing side here — no either-side ambiguity for RETIRE]")
            print(f"  org {org} ({ORG_NAME.get(org,'?')}) revives — in scope under org_id rule: {v_total:>5,}  "
                  f"[org_id IS the regaining side here, not a loss]")
    dg = plan.get("deferred_groups") or []
    if dg:
        print(f"  collision groups DEFERRED (span included+excluded orgs, never half-applied): {len(dg):>4,}")
        shown = dg if only_orgs and len(dg) <= 25 else dg[:25]
        for g in shown:
            print(f"    {g['kind']:<8} {g['name']!r:<44} group spans orgs {g['group_orgs']}, deferred whole")
        if len(dg) > len(shown):
            print(f"    ... and {len(dg) - len(shown)} more (full list in --json output)")
    elif only_orgs:
        print("  collision groups DEFERRED: 0")


# ── BRAIN LOCK ───────────────────────────────────────────────────────────────────────────────────
# THIS TOOL WROTE ACROSS ALL FIVE ORGS WITH NO LOCK AT ALL (core-ops, bus #5870, 2026-09-02).
#
# Every embed path serialises on /tmp/core-brain-<md5(CORE_BRAIN)>.lock, a mkdir-based mutex taken
# in .claude/hooks/run-brain-update.sh — "one brain, one lock, all Cores queue on it". This script
# took none, and it writes entities and entity_edges across every org as brain_admin.
#
# ops named exactly why that is worse here than a routine race, and it is the right reading: this
# tool is otherwise built to be reversible — nothing is DELETEd, duplicates are soft-retired with
# valid_until/superseded_by, and every action is journaled BEFORE it is applied so --undo can walk
# it back. A write that interleaves with a concurrent embed is the one outcome the journal cannot
# reverse, because the interleaved write was never in the journal. The safety story had a hole
# precisely where the tool is least defended.
#
# Same mkdir protocol, same path, same holder files — a different mechanism here (flock, a pg
# advisory lock) would serialise against nothing, because the processes this must queue behind are
# using mkdir. Compatibility with the existing holder IS the requirement.
#
# DRY RUN TAKES NO LOCK: it only reads, and making a read-only plan wait behind a 40-minute embed
# would push people toward --apply without looking first, which is the opposite of what this tool
# wants.
def _core_brain_env_raw() -> str:
    """The exact bytes bash sees for `$CORE_BRAIN` — used ONLY for lock-path hashing.

    Deliberately reads os.environ directly rather than taking a `Path` (e.g. from brain_root()).
    `str(Path("/x/"))` == "/x" — pathlib silently strips a trailing slash — but bash's
    `echo "$CORE_BRAIN"` echoes the literal env value, trailing slash and all. Hashing a
    Path-normalized string diverges from the shell's hash for any CORE_BRAIN with a trailing
    slash, even though brain_root() correctly uses the Path form for everything else (globbing,
    DB queries) where that normalization is harmless or desirable.
    """
    return os.environ.get("CORE_BRAIN", "")


def _brain_lock_path(raw: str | None = None) -> Path:
    """Byte-identical to run-brain-update.sh:28's
    `BRAIN_HASH=$(echo "$CORE_BRAIN" | md5 -q 2>/dev/null || echo "$CORE_BRAIN" | md5sum | ...)`.

    THE BUG THIS REPLACES (2026-09-02): the prior version hashed `str(brain)` — a `Path`, with no
    trailing newline — while the shell hashes `$CORE_BRAIN` PIPED THROUGH `echo`, which appends
    exactly one trailing newline. `echo "$CORE_BRAIN" | md5 -q` and `hashlib.md5(str(Path(...)))`
    are hashing different byte strings and produce different, unrelated digests — a lock on a path
    nothing else derives, which serialises against nothing while looking exactly like a lock.
    Proven in bin/tests/test_brain_lock_path_matches_shell.py, which computes both sides
    independently (the shell's OWN line, invoked via subprocess — not reimplemented here) for
    several values, including one with a trailing slash.

    hashlib over shelling out to md5/md5sum: byte-for-byte reproducible without depending on
    which binary happens to be on PATH, PROVIDED the digest is computed over the identical bytes
    the shell pipes to it — `raw + "\\n"`, exactly what `echo` (no `-n`) produces on both macOS's
    `md5 -q` path and the Linux `md5sum` fallback path in run-brain-update.sh.
    """
    if raw is None:
        raw = _core_brain_env_raw()
    digest = hashlib.md5((raw + "\n").encode("utf-8")).hexdigest()
    return Path(f"/tmp/core-brain-{digest}.lock")


def _live_cmd(pid: int) -> str:
    """`ps -p <pid> -o command=`, trailing-whitespace-stripped — IDENTICAL to
    run-brain-update.sh's `_holder_cmd()` (line ~64).

    THE DEFECT THIS FIXES: a shell waiter re-derives this SAME command for our pid and compares
    it against whatever is on disk in holder.cmd. Recording a made-up literal (e.g.
    "repartition-hubs.py --apply") instead of the real `ps` output means the comparison ALWAYS
    mismatches for a live process — `ps -o command=` reports the full interpreter path and every
    argument (e.g. "/usr/bin/python3 /path/to/bin/repartition-hubs.py --apply --journal j.jsonl"),
    never the short literal. A mismatch reads to the shell as "pid alive but under a DIFFERENT
    command → recycled pid, not our holder" (run-brain-update.sh:123), which triggers an
    IMMEDIATE ATOMIC RECLAIM — the waiter deletes this lock while --apply is still running and
    writing. That is worse than no lock: it looks held right up until it is silently stolen.
    """
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                               capture_output=True, text=True, check=True).stdout.rstrip()
    except Exception:
        return ""


def acquire_brain_lock(brain: Path, wait_secs: int = 1800):
    """Block until the shared brain lock is ours. Returns the lock dir, or exits non-zero.

    `brain` is accepted (and validated upstream by brain_root(), which exits if $CORE_BRAIN is
    unset) but NOT threaded into the hash — see _brain_lock_path's docstring for why re-deriving
    from a Path would silently diverge from the shell's raw-string hash.

    Refuses rather than proceeding unlocked on timeout: an unserialised cross-org write is the
    thing this exists to prevent, so "could not serialise" must never degrade into "wrote anyway".
    """
    lock = _brain_lock_path()
    deadline = time.time() + wait_secs
    announced = False
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if not announced:
                holder = ""
                try:
                    holder = (lock / "holder.cmd").read_text().strip()[:80]
                except Exception:
                    pass
                print(f"[repartition] waiting for the brain lock{' held by: ' + holder if holder else ''}",
                      file=sys.stderr)
                announced = True
            if time.time() > deadline:
                print(f"[repartition] REFUSING: brain lock at {lock} still held after {wait_secs}s. "
                      f"Not writing unserialised — rerun when the holder finishes.", file=sys.stderr)
                sys.exit(3)
            time.sleep(2)
    try:
        # ORDER MATCHES run-brain-update.sh:149-151 EXACTLY: started, THEN pid, THEN cmd. The
        # shell's own comment there explains why — "Started-first guarantees: pid present ⇒
        # started present." A crash between our OWN writes could otherwise leave holder.pid on
        # disk with no holder.started, and the waiter's backstop check requires holder.started to
        # even consider killing a wedged holder (run-brain-update.sh:94-95) — a pid-without-started
        # holder can never be backstop-reclaimed and would wedge the fleet's queue forever were we
        # to actually wedge. Writing started first closes that window the same way the shell does.
        (lock / "holder.started").write_text(str(int(time.time())))
        (lock / "holder.pid").write_text(str(os.getpid()))
        (lock / "holder.cmd").write_text(_live_cmd(os.getpid()))
    except Exception:
        pass   # metadata is for the waiter's message; the mutex is the mkdir itself
    return lock


def release_brain_lock(lock: Path) -> None:
    try:
        for f in ("holder.pid", "holder.cmd", "holder.started"):
            (lock / f).unlink(missing_ok=True)
        lock.rmdir()
    except Exception:
        pass


def main() -> int:
    """Wrapper that GUARANTEES the shared brain lock is released.

    The lock is a mkdir mutex on a path every Core queues behind, so a crash between acquire and
    release does not fail this run — it wedges the whole fleet's brain pipeline until someone
    rmdir's it by hand. try/finally is the difference between an error and an outage.

    `--undo` takes the SAME lock as `--apply` (2026-09-02) — reusing acquire_brain_lock /
    release_brain_lock rather than a second mechanism, per the BRAIN LOCK section above: the
    processes this must queue behind (embed.py et al., via run-brain-update.sh's mkdir mutex) don't
    know about a second lock, so a second lock would serialise against nothing.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--journal", help="journal path; REQUIRED with --apply")
    ap.add_argument("--undo", help="reverse a journal written by --apply")
    ap.add_argument("--json", help="write the dry-run plan here")
    ap.add_argument("--only-org", action="append", metavar="N",
                     help="Restrict the plan to actions whose target-or-subject org is in this "
                     "set. Repeatable and/or comma-separated (--only-org 5, or --only-org 2,5, "
                     "or both). Per-action-kind membership: a MOVE belongs to org N if N is "
                     "EITHER its from_org OR its to_org (a move from 1 to 5 counts under "
                     "--only-org 1 AND under --only-org 5 — a move is inherently cross-org, so "
                     "either side is a legitimate reading and excluding one would silently hide "
                     "half of what the flag touches); a RETIRE or REVIVE belongs to org N if N "
                     "is the org already recorded on that row (no either-side ambiguity — one "
                     "org per row). A (kind,name) collision GROUP, not the individual action, is "
                     "the unit of inclusion: if a group's actions collectively touch both an "
                     "included and an excluded org, the WHOLE group is deferred and reported "
                     "rather than half-applied. Honoured by dry-run, --apply, and --undo (on "
                     "--undo it is compared against the scope recorded in the journal and "
                     "REFUSES on a mismatch rather than guessing which slice to reverse).")
    args = ap.parse_args()

    if args.undo and args.apply:
        print("--undo and --apply are mutually exclusive.")
        return 2
    if args.apply and not args.journal:
        print("--apply requires --journal: an unreversible bulk reassignment is not acceptable.")
        return 2
    only_orgs = parse_only_org(args.only_org)

    needs_lock = args.apply or bool(args.undo)
    _lock = acquire_brain_lock(brain_root()) if needs_lock else None
    try:
        if args.undo:
            return undo_journal(Path(args.undo), only_orgs)
        return _run(args, _lock, only_orgs)
    finally:
        if _lock is not None:
            release_brain_lock(_lock)


def _run(args, _lock, only_orgs: set[int] | None) -> int:
    brain = brain_root()
    conn = connect(admin=args.apply)
    cur = conn.cursor()
    plan = build_plan(cur, brain, only_orgs=only_orgs)
    report(plan)

    if args.json:
        Path(args.json).write_text(json.dumps(plan, indent=2, default=str))
        print(f"  plan written: {args.json}")

    if not args.apply:
        print("\n  DRY RUN — nothing written. Re-run with --apply --journal <path> to execute.")
        conn.close()
        return 0

    return apply_plan(conn, cur, plan, Path(args.journal), only_orgs=only_orgs)


def apply_plan(conn, cur, plan: dict, journal: Path, only_orgs: set[int] | None = None) -> int:
    """Execute the plan. Journal every statement BEFORE it runs, one JSON object per line.

    ORDER MATTERS AND IS NOT ARBITRARY:
      1. moves      — an entity reaches its target org while its duplicates are still live, so the
                      unique key (org_id, kind, name) is checked against reality, not a half-state.
      2. truth      — carry the freshest compiled_truth_md onto survivors before donors are retired.
      3. edges      — REPOINT edges off retiring rows onto their survivor, then move edge org_id to
                      follow the entity. Repointing rather than deleting is the whole difference
                      between a migration and an outage: graph BFS scopes on entity_edges.org_id
                      (query.py:398-446), so an entity moved without its edges is disconnected from
                      its own neighbourhood, and origin edges key on (source_file, org_id).
      4. retire     — only now, once nothing live points at them.

    EVERY STEP READS BEFORE IT WRITES (2026-09-02, --undo). Each mutating statement is preceded by
    a SELECT of exactly the rows/columns it is about to change, and `rec()` logs that pre-image
    alongside the identifiers the old code already logged. `--undo` walks this journal in reverse
    and restores from those pre-images — see `_plan_undo` and `undo_journal` below, and the module
    docstring's SAFETY section for why the two aggregate-count entries (edge_repoint, edge_reorg)
    and the two new/post-only entries (revive, truth_carry) were the ones that had to change.
    """
    j = journal.open("w")

    def rec(kind, **kw):
        j.write(json.dumps({"op": kind, **kw}, default=str) + "\n")

    # SCOPE HEADER, written FIRST (2026-09-02, --only-org). --undo reads this back (undo_journal
    # strips it out before replaying, since it is metadata, not a reversible action) to know
    # exactly which slice this journal covers, so a scoped undo can refuse a scope mismatch before
    # writing anything rather than guessing or silently reversing the wrong slice.
    rec("_scope", only_org=sorted(only_orgs) if only_orgs else None)

    moved = truthed = repointed = deduped = edge_orged = retired = revived = 0
    survivor_of = {r["id"]: r["superseded_by"] for r in plan["retires"]}

    # 0. revive tombstones that own a target key (see build_plan) — before anything else, so the
    #    survivor exists and is live when retires point at it.
    #
    # PRIOR STATE CAPTURED HERE (2026-09-02, --undo fix). The old recording was `id, org_id` only —
    # enough to say WHICH row was revived, nothing about what it looked like as a tombstone. Undo
    # cannot restore a value it was never given, so a revive journaled by the prior revision cannot
    # be reversed; this SELECT-before-UPDATE is what makes a NEW journal's revive entries reversible.
    for r in plan.get("revives", []):
        cur.execute("SELECT valid_until, superseded_by FROM entities WHERE id=%s", (r["id"],))
        prior_valid_until, prior_superseded_by = cur.fetchone()
        rec("revive", id=r["id"], org_id=r["org_id"],
            prior_valid_until=prior_valid_until, prior_superseded_by=prior_superseded_by)
        cur.execute("UPDATE entities SET valid_until=NULL, superseded_by=NULL WHERE id=%s", (r["id"],))
        revived += cur.rowcount

    # 1. moves
    for m in plan["moves"]:
        rec("move", id=m["id"], from_org=m["from_org"], to_org=m["to_org"])
        cur.execute("UPDATE entities SET org_id=%s WHERE id=%s AND valid_until IS NULL",
                    (m["to_org"], m["id"]))
        moved += cur.rowcount

    # 2. carry freshest truth to survivors
    #
    # PRIOR + EXPECTED-POST STATE CAPTURED HERE (2026-09-02, --undo fix). The old recording named
    # donor and survivor and nothing else — enough to know THAT a copy happened, nothing about what
    # it overwrote. Two SELECTs before the UPDATE: the survivor's own values (to restore on undo)
    # and the donor's current values (which is exactly what the UPDATE is about to copy onto the
    # survivor — reading it now, before either row changes, gives undo a POST snapshot to verify
    # against without a second query later).
    for r in plan["retires"]:
        if not r.get("truth_donor_for_survivor"):
            continue
        cur.execute("SELECT compiled_truth_md, last_compiled_at, confidence FROM entities WHERE id=%s",
                    (r["superseded_by"],))
        prior_truth, prior_compiled_at, prior_confidence = cur.fetchone()
        cur.execute("SELECT compiled_truth_md, last_compiled_at, confidence FROM entities WHERE id=%s",
                    (r["id"],))
        post_truth, post_compiled_at, post_confidence = cur.fetchone()
        rec("truth_carry", donor=r["id"], survivor=r["superseded_by"],
            prior_truth=prior_truth, prior_compiled_at=prior_compiled_at,
            prior_confidence=prior_confidence, post_truth=post_truth,
            post_compiled_at=post_compiled_at, post_confidence=post_confidence)
        cur.execute(
            "UPDATE entities s SET compiled_truth_md=d.compiled_truth_md, "
            "  last_compiled_at=d.last_compiled_at, confidence=d.confidence "
            "FROM entities d WHERE d.id=%s AND s.id=%s", (r["id"], r["superseded_by"]))
        truthed += cur.rowcount

    # 3a. Repoint edges off retiring rows onto their survivor.
    #
    # UNIQUE(from_entity_id, to_entity_id, edge_type) makes this harder than an UPDATE. A plain
    # UPDATE ... WHERE NOT EXISTS only sees rows that already exist; it cannot see that TWO doomed
    # rows in the same statement both remap onto the same triple, which is the common case here
    # because many duplicates share one survivor. So: materialise the remapping, drop self-loops,
    # keep exactly one row per resulting triple, exclude any triple the graph already has, update
    # those — then delete whatever still points at a doomed row, which is by construction a
    # duplicate edge rather than information.
    #
    # MATERIALISE `_winner` FIRST, LOG, THEN MUTATE (2026-09-02, --undo fix). The old recording was
    # `rec("edge_repoint", repointed=<count>, deduped=<count>)` — two integers for a bulk operation
    # touching an unbounded number of rows. That is not reversible at any granularity: undo needs,
    # per repointed edge, its OLD (from,to), and per deleted edge, its FULL row (every column,
    # including embedding) to recreate it byte-identically. Computing the winner set ONCE into a
    # temp table — rather than repeating the WITH-clause once for a logging SELECT and again for the
    # UPDATE — guarantees the rows we log are exactly the rows we mutate; two independently-typed
    # copies of this CTE could drift from each other and log something other than what ran.
    doomed = list(survivor_of.keys())
    if doomed:
        cur.execute("CREATE TEMP TABLE _map(doomed bigint PRIMARY KEY, survivor bigint) ON COMMIT DROP")
        cur.executemany("INSERT INTO _map VALUES (%s,%s)", list(survivor_of.items()))
        cur.execute("ANALYZE _map")
        cur.execute("""
            CREATE TEMP TABLE _winner ON COMMIT DROP AS
            WITH mapped AS (
              SELECT ed.id, ed.edge_type,
                     ed.from_entity_id AS old_from, ed.to_entity_id AS old_to,
                     COALESCE(mf.survivor, ed.from_entity_id) AS nf,
                     COALESCE(mt.survivor, ed.to_entity_id)   AS nt
              FROM entity_edges ed
              LEFT JOIN _map mf ON mf.doomed = ed.from_entity_id
              LEFT JOIN _map mt ON mt.doomed = ed.to_entity_id
              WHERE mf.doomed IS NOT NULL OR mt.doomed IS NOT NULL
            ),
            keepable AS (
              SELECT * FROM mapped m
              WHERE m.nf <> m.nt                                   -- never create a self-loop
                AND NOT EXISTS (SELECT 1 FROM entity_edges x
                                WHERE x.from_entity_id=m.nf AND x.to_entity_id=m.nt
                                  AND x.edge_type=m.edge_type AND x.id <> m.id)
            )
            SELECT DISTINCT ON (nf, nt, edge_type) id, old_from, old_to, nf, nt
            FROM keepable ORDER BY nf, nt, edge_type, id
        """)
        cur.execute("SELECT id, old_from, old_to, nf, nt FROM _winner")
        winners = cur.fetchall()
        # AND id NOT IN _winner IS LOAD-BEARING, not defensive. Every edge referencing a doomed id
        # matches this WHERE clause BEFORE the winner UPDATE below runs — including the winners
        # themselves, since they still point at their doomed id at THIS moment. Logging them here
        # as "deleted" would be wrong: they get repointed, not removed, and the real DELETE (after
        # the UPDATE) never touches them because by then their from/to no longer references a
        # doomed id at all. Measured live by bin/tests/test_repartition_undo.py before this
        # exclusion existed: undo refused every run, correctly reporting a "deleted" edge that had
        # in fact been reassigned to N/TZ, not deleted, because the journal said DELETE about a row
        # apply_plan had never removed.
        cur.execute("""
            SELECT id, from_entity_id, to_entity_id, edge_type, confidence, confidence_label,
                   source_file, embedding, org_id, is_cross_org, created_at
            FROM entity_edges
            WHERE (from_entity_id IN (SELECT doomed FROM _map) OR to_entity_id IN (SELECT doomed FROM _map))
              AND id NOT IN (SELECT id FROM _winner)
        """)
        doomed_edges = cur.fetchall()
        rec("edge_repoint",
            winners=[{"id": w[0], "old_from": w[1], "old_to": w[2], "new_from": w[3], "new_to": w[4]}
                     for w in winners],
            deleted=[{"id": d[0], "from_entity_id": d[1], "to_entity_id": d[2], "edge_type": d[3],
                      "confidence": d[4], "confidence_label": d[5], "source_file": d[6],
                      "embedding": d[7], "org_id": d[8], "is_cross_org": d[9], "created_at": d[10]}
                     for d in doomed_edges])

        cur.execute("UPDATE entity_edges e SET from_entity_id=w.nf, to_entity_id=w.nt "
                    "FROM _winner w WHERE e.id = w.id")
        repointed = cur.rowcount
        cur.execute("DELETE FROM entity_edges WHERE from_entity_id IN (SELECT doomed FROM _map) "
                    "   OR to_entity_id IN (SELECT doomed FROM _map)")
        deduped = cur.rowcount

    # 3b. an edge belongs to the org of the entities it connects
    #
    # PRIOR STATE CAPTURED HERE (2026-09-02, --undo fix). `rec("edge_reorg", rows=<count>)` named
    # how many edges changed, not which ones or what their org_id was before — undo cannot restore
    # a value never given per row. SELECT the exact set the UPDATE is about to touch (same WHERE
    # clause) before running it, so the log and the mutation see identical rows.
    #
    # SCOPED TO only_orgs WHEN SET (2026-09-02, --only-org). This is the one apply_plan step that
    # is NOT already implicitly scoped by reading from `plan` — it is an unconditional global fix
    # ("any edge whose endpoints agree on an org it doesn't itself carry"), independent of which
    # moves/retires/revives ran. Unscoped, a fleet-wide --only-org 5 apply would still silently
    # rewrite an edge_id between two org-2 entities that happens to be mis-orged for reasons
    # unrelated to this run — a write outside the requested slice. "Follows its entity" (see
    # --help) means: scope it to edges whose shared endpoint org is itself in only_orgs.
    if only_orgs:
        cur.execute(
            "SELECT ed.id, ed.org_id, f.org_id FROM entity_edges ed, entities f, entities t "
            "WHERE ed.from_entity_id=f.id AND ed.to_entity_id=t.id "
            "  AND f.org_id = t.org_id AND ed.org_id <> f.org_id AND f.org_id = ANY(%s)",
            (sorted(only_orgs),))
    else:
        cur.execute(
            "SELECT ed.id, ed.org_id, f.org_id FROM entity_edges ed, entities f, entities t "
            "WHERE ed.from_entity_id=f.id AND ed.to_entity_id=t.id "
            "  AND f.org_id = t.org_id AND ed.org_id <> f.org_id")
    reorg_rows = cur.fetchall()
    rec("edge_reorg", edges=[{"id": r[0], "prior_org_id": r[1], "new_org_id": r[2]} for r in reorg_rows])
    if only_orgs:
        cur.execute(
            "UPDATE entity_edges ed SET org_id = f.org_id FROM entities f, entities t "
            "WHERE ed.from_entity_id=f.id AND ed.to_entity_id=t.id "
            "  AND f.org_id = t.org_id AND ed.org_id <> f.org_id AND f.org_id = ANY(%s)",
            (sorted(only_orgs),))
    else:
        cur.execute(
            "UPDATE entity_edges ed SET org_id = f.org_id FROM entities f, entities t "
            "WHERE ed.from_entity_id=f.id AND ed.to_entity_id=t.id "
            "  AND f.org_id = t.org_id AND ed.org_id <> f.org_id")
    edge_orged = cur.rowcount

    # 4. retire duplicates — soft, never DELETE (every FK into entities is ON DELETE CASCADE)
    #
    # PRIOR STATE CAPTURED HERE (2026-09-02, --undo fix). `rec("retire_batch", ids=[...])` named
    # which rows but not what they looked like before — every row here is drawn from `all_live`
    # in build_plan (`WHERE valid_until IS NULL`), so valid_until/superseded_by SHOULD already be
    # NULL/NULL, but reading them explicitly rather than assuming it is what makes this an
    # observation instead of a guess, and gives undo something to verify against.
    for chunk in range(0, len(plan["retires"]), 500):
        batch = plan["retires"][chunk:chunk + 500]
        ids = [r["id"] for r in batch]
        cur.execute("SELECT id, valid_until, superseded_by FROM entities WHERE id = ANY(%s)", (ids,))
        prior_by_id = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        rec("retire_batch", items=[
            {"id": r["id"], "superseded_by": r["superseded_by"],
             "prior_valid_until": prior_by_id[r["id"]][0],
             "prior_superseded_by": prior_by_id[r["id"]][1]} for r in batch])
        cur.executemany(
            "UPDATE entities SET valid_until=now(), superseded_by=%s "
            "WHERE id=%s AND valid_until IS NULL",
            [(r["superseded_by"], r["id"]) for r in batch])
        retired += len(batch)

    conn.commit()
    j.close()
    print(f"\n  APPLIED — journal: {journal}")
    print(f"    tombstones revived       : {revived:>7,}")
    print(f"    entities reassigned      : {moved:>7,}")
    print(f"    survivors given fresher truth: {truthed:>3,}")
    print(f"    edges repointed          : {repointed:>7,}")
    print(f"    duplicate edges removed  : {deduped:>7,}")
    print(f"    edges re-orged           : {edge_orged:>7,}")
    print(f"    duplicates soft-retired  : {retired:>7,}")
    conn.close()
    return 0


# ── UNDO ─────────────────────────────────────────────────────────────────────────────────────────
# Implemented 2026-09-02 — see the module docstring SAFETY section for why the recording side of
# apply_plan had to change first, and for the two-phase (verify-then-write) shape this follows.

class UndoRefused(Exception):
    """Raised by _plan_undo for an unknown action kind, a missing pre-image field (a journal
    written before this revision), or a row whose live state no longer matches what the journal
    says the original action left behind. Caught once, in undo_journal, after which NOTHING has
    been written — _plan_undo only ever SELECTs."""


def _js(v):
    """Make a value fetched fresh from Postgres comparable to the same value after it round-tripped
    through the journal's `json.dumps(..., default=str)`. Only datetimes need this: `rec()` never
    encoded them any other way, so a value's OWN `str()` is the only form that can match what is on
    disk. int/str/float/None survive the round trip natively and are returned unchanged."""
    return str(v) if hasattr(v, "isoformat") else v


def _verify(cond: bool, msg: str) -> None:
    if not cond:
        raise UndoRefused(msg)


def _plan_undo(cur, entries: list[dict]) -> list[tuple[str, tuple, str]]:
    """READ-ONLY. For every journal entry, already given in UNDO order (reverse of how apply_plan
    wrote them), confirm the live DB still holds exactly the post-state that entry's action left
    behind, and build the SQL that reverses it. Returns the full list of (sql, params, description)
    to execute — nothing here writes, so raising UndoRefused partway through leaves the DB
    untouched and aborts the WHOLE undo (requirement: an unknown kind must abort, not skip).
    """
    actions: list[tuple[str, tuple, str]] = []
    for entry in entries:
        op = entry.get("op")

        if op == "revive":
            eid = entry["id"]
            _verify("prior_valid_until" in entry and "prior_superseded_by" in entry,
                    f"revive: entity {eid}'s journal entry predates the undo-capable recording "
                    f"format (no prior_valid_until/prior_superseded_by) — cannot reverse safely")
            cur.execute("SELECT valid_until, superseded_by FROM entities WHERE id=%s", (eid,))
            row = cur.fetchone()
            _verify(row is not None, f"revive: entity {eid} no longer exists")
            vu, sb = row
            _verify(vu is None and sb is None,
                    f"revive: entity {eid} drifted — expected live (valid_until and superseded_by "
                    f"both NULL, the revive's own post-state), found valid_until={vu} superseded_by={sb}")
            actions.append((
                "UPDATE entities SET valid_until=%s::timestamptz, superseded_by=%s WHERE id=%s",
                (entry["prior_valid_until"], entry["prior_superseded_by"], eid),
                f"revive-undo id={eid}"))

        elif op == "move":
            eid, from_org, to_org = entry["id"], entry["from_org"], entry["to_org"]
            cur.execute("SELECT org_id, valid_until FROM entities WHERE id=%s", (eid,))
            row = cur.fetchone()
            _verify(row is not None, f"move: entity {eid} no longer exists")
            org_id, vu = row
            _verify(org_id == to_org,
                    f"move: entity {eid} drifted — expected org_id={to_org} (the move's own "
                    f"post-state), found org_id={org_id}")
            _verify(vu is None,
                    f"move: entity {eid} drifted — expected live, found valid_until={vu} "
                    f"(something retired it after this move; restoring org_id blind would leave "
                    f"a tombstone under the wrong org)")
            actions.append((
                "UPDATE entities SET org_id=%s WHERE id=%s", (from_org, eid),
                f"move-undo id={eid}"))

        elif op == "truth_carry":
            donor, survivor = entry["donor"], entry["survivor"]
            need = ("prior_truth", "prior_compiled_at", "prior_confidence",
                    "post_truth", "post_compiled_at", "post_confidence")
            _verify(all(k in entry for k in need),
                    f"truth_carry: survivor {survivor}'s journal entry predates the undo-capable "
                    f"recording format (missing prior/post truth fields) — cannot reverse safely")
            cur.execute("SELECT compiled_truth_md, last_compiled_at, confidence FROM entities "
                        "WHERE id=%s", (survivor,))
            row = cur.fetchone()
            _verify(row is not None, f"truth_carry: survivor {survivor} no longer exists")
            cur_truth, cur_compiled_at, cur_conf = row
            _verify(_js(cur_truth) == entry["post_truth"]
                    and _js(cur_compiled_at) == entry["post_compiled_at"]
                    and _js(cur_conf) == entry["post_confidence"],
                    f"truth_carry: survivor {survivor} drifted — its compiled_truth_md/"
                    f"last_compiled_at/confidence no longer match what this carry (from donor "
                    f"{donor}) wrote")
            actions.append((
                "UPDATE entities SET compiled_truth_md=%s, last_compiled_at=%s::timestamptz, "
                "  confidence=%s WHERE id=%s",
                (entry["prior_truth"], entry["prior_compiled_at"], entry["prior_confidence"], survivor),
                f"truth_carry-undo survivor={survivor}"))

        elif op == "edge_repoint":
            for w in entry.get("winners", []):
                cur.execute("SELECT from_entity_id, to_entity_id FROM entity_edges WHERE id=%s",
                            (w["id"],))
                row = cur.fetchone()
                _verify(row is not None, f"edge_repoint: winner edge {w['id']} no longer exists")
                _verify(row[0] == w["new_from"] and row[1] == w["new_to"],
                        f"edge_repoint: winner edge {w['id']} drifted — expected "
                        f"({w['new_from']},{w['new_to']}) (this repoint's own post-state), found "
                        f"({row[0]},{row[1]})")
                actions.append((
                    "UPDATE entity_edges SET from_entity_id=%s, to_entity_id=%s WHERE id=%s",
                    (w["old_from"], w["old_to"], w["id"]),
                    f"edge_repoint-undo edge={w['id']}"))
            for d in entry.get("deleted", []):
                cur.execute("SELECT 1 FROM entity_edges WHERE id=%s", (d["id"],))
                _verify(cur.fetchone() is None,
                        f"edge_repoint: deleted edge {d['id']} exists in the DB — expected gone "
                        f"(this action deleted it as a duplicate); refusing rather than overwriting "
                        f"whatever now holds that id")
                actions.append((
                    "INSERT INTO entity_edges (id, from_entity_id, to_entity_id, edge_type, "
                    "  confidence, confidence_label, source_file, embedding, org_id, is_cross_org, "
                    "  created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s::timestamptz)",
                    (d["id"], d["from_entity_id"], d["to_entity_id"], d["edge_type"], d["confidence"],
                     d["confidence_label"], d["source_file"], d["embedding"], d["org_id"],
                     d["is_cross_org"], d["created_at"]),
                    f"edge_repoint-restore edge={d['id']}"))

        elif op == "edge_reorg":
            for e in entry.get("edges", []):
                cur.execute("SELECT org_id FROM entity_edges WHERE id=%s", (e["id"],))
                row = cur.fetchone()
                _verify(row is not None, f"edge_reorg: edge {e['id']} no longer exists")
                _verify(row[0] == e["new_org_id"],
                        f"edge_reorg: edge {e['id']} drifted — expected org_id={e['new_org_id']} "
                        f"(this reorg's own post-state), found org_id={row[0]}")
                actions.append((
                    "UPDATE entity_edges SET org_id=%s WHERE id=%s",
                    (e["prior_org_id"], e["id"]), f"edge_reorg-undo edge={e['id']}"))

        elif op == "retire_batch":
            items = entry.get("items")
            _verify(items is not None,
                    "retire_batch: journal entry predates the undo-capable recording format (no "
                    "per-row prior state) — cannot reverse safely")
            for it in items:
                eid = it["id"]
                cur.execute("SELECT valid_until, superseded_by FROM entities WHERE id=%s", (eid,))
                row = cur.fetchone()
                _verify(row is not None, f"retire_batch: entity {eid} no longer exists")
                vu, sb = row
                _verify(vu is not None and sb == it["superseded_by"],
                        f"retire_batch: entity {eid} drifted — expected retired with "
                        f"superseded_by={it['superseded_by']} (this retire's own post-state), found "
                        f"valid_until={vu} superseded_by={sb}")
                actions.append((
                    "UPDATE entities SET valid_until=%s::timestamptz, superseded_by=%s WHERE id=%s",
                    (it["prior_valid_until"], it["prior_superseded_by"], eid),
                    f"retire_batch-undo id={eid}"))

        else:
            raise UndoRefused(
                f"unknown action kind {op!r} — aborting the whole undo rather than skipping it. "
                f"A partial restore looks like a complete one; this journal may have been written "
                f"by a different revision of this tool (e.g. the dead enforce_domain_claims() path, "
                f"which journals domain_retire/domain_revive_retire/domain_move and is not wired "
                f"into --apply today).")

    return actions


def undo_journal(journal: Path, expected_scope: set[int] | None = None) -> int:
    """Reverse a journal written by apply_plan(), walking it in REVERSE order.

    TWO PHASES, not interchangeable:
      1. VERIFY (read-only, _plan_undo) — every entry's recorded post-state is checked against the
         live DB, for every entry, before a single statement runs. An unknown op or a drifted row
         aborts here with nothing written.
      2. EXECUTE — only once every entry verifies clean: run every reversal statement, then ONE
         commit. A failure here should be unreachable given phase 1 passed; if it happens anyway
         (e.g. a UNIQUE-constraint collision with a row created by something else entirely, after
         this undo started but not touching any row this undo itself is watching), roll back
         everything rather than leave a partial restore.

    IDEMPOTENT via an explicit `<journal>.undone` marker written on success, checked first. Chosen
    over relying solely on phase 1's drift check (which WOULD also refuse a second run — the DB
    would hold the prior state, not the post-state every entry expects) because the marker gives a
    specific, expected message instead of a generic drift refusal that happens to be safe.

    TRANSACTIONAL: everything above shares one connection/transaction; conn.commit() is called
    exactly once, on the success path, after every action has executed without error.

    SCOPE CHECK (2026-09-02, --only-org). apply_plan() now writes a leading `{"op": "_scope", ...}`
    entry recording the org filter it ran under (None for an unscoped, fleet-wide apply). That
    entry is metadata, not a reversible action — it is stripped out here, BEFORE reversal, so
    `_plan_undo` never sees it and the "unknown op" abort doesn't fire on our own header. When the
    caller passes `expected_scope` (from `--undo j.jsonl --only-org N`), it is compared against
    the journal's OWN recorded scope and a mismatch refuses before a single statement runs — this
    is a sanity check the operator opts into, not a requirement: plain `--undo j.jsonl` with no
    `--only-org` always trusts the journal's own recorded scope and reverses exactly that slice,
    because the journal's entries ARE that slice regardless of whether anyone re-asserts it.
    """
    if not journal.exists():
        print(f"undo: journal not found: {journal}", file=sys.stderr)
        return 2

    marker = Path(str(journal) + ".undone")
    if marker.exists():
        print(f"undo: {journal} was already undone — {marker.read_text().strip()} — no-op.")
        return 0

    try:
        raw_entries = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    except json.JSONDecodeError as e:
        print(f"undo: {journal} is not valid journal JSON: {e}", file=sys.stderr)
        return 2

    scope_entries = [e for e in raw_entries if e.get("op") == "_scope"]
    entries = [e for e in raw_entries if e.get("op") != "_scope"]
    recorded_scope = scope_entries[-1].get("only_org") if scope_entries else None
    recorded_scope_set = set(recorded_scope) if recorded_scope else None

    if expected_scope is not None and recorded_scope_set != expected_scope:
        print(f"undo: REFUSING — asked to undo --only-org {sorted(expected_scope)}, but "
              f"{journal} was applied with --only-org "
              f"{sorted(recorded_scope_set) if recorded_scope_set else '(none — fleet-wide)'}. "
              f"Refusing rather than reversing a different slice than the one asked for.",
              file=sys.stderr)
        return 2

    print(f"undo: journal scope = --only-org "
          f"{sorted(recorded_scope_set) if recorded_scope_set else '(none — fleet-wide apply)'}")

    if not entries:
        marker.write_text(f"undone at {datetime.now(timezone.utc).isoformat()} (0 entries)\n")
        print(f"undo: {journal} had no entries — nothing to reverse.")
        return 0

    conn = connect(admin=True)
    cur = conn.cursor()

    try:
        actions = _plan_undo(cur, list(reversed(entries)))
    except UndoRefused as e:
        conn.rollback()
        conn.close()
        print(f"undo: REFUSING — {e}", file=sys.stderr)
        return 2

    try:
        for sql, params, _desc in actions:
            cur.execute(sql, params)
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"undo: aborted mid-write (should be unreachable after verification passed) — "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2
    conn.close()

    marker.write_text(
        f"undone at {datetime.now(timezone.utc).isoformat()} "
        f"({len(entries)} journal entries, {len(actions)} statements)\n")
    print(f"undo: reversed {len(entries)} journal entries ({len(actions)} statements) from {journal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
