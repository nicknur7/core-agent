#!/usr/bin/env python3
"""repartition-hubs.py --undo must restore the pre-apply DB byte-for-byte, EXECUTED end to end
against a throwaway scratch Postgres database — never the live corebrain.

WHY THIS EXISTS (2026-09-02). core-ops reported (bus #5870, #5875) that repartition-hubs.py had
never been applied anywhere, and that its OWN docstring told the operator TWICE the operation was
reversible when `--undo` was in fact a refusal stub. ops and Nick agreed the sequence: lock parity
first (done), implement `--undo` second, apply on ONE org and verify third, then the rest. This file
is the acceptance test for steps two AND three — step three ("apply on ONE org") turned out to have
NO WAY to happen at all: every flag the tool had was fleet-wide or nothing, so section 6 below is
the acceptance test for `--only-org`, the flag that makes gate 3 possible. Neither section touches a
real corebrain, real orgs, or real hub files anywhere on disk.

WHAT IT PROVES, as six sub-tests sharing one provisioned scratch DB:
  1. full cycle    — seed a fixture spanning every journaled action kind (revive, move, retire,
                     truth_carry, edge repoint x2 flavours, edge reorg), run the REAL CLI
                     `--apply --journal J` then `--undo J` as two subprocesses, and diff the DB
                     before/after byte-for-byte (see BYTE-IDENTICAL CAVEAT below for the one
                     column this cannot cover and why).
  2. unknown kind  — a journal with one entry whose "op" this tool has never heard of must abort
                     the WHOLE undo before writing anything, not skip the bad entry.
  3. drift         — mutating a touched row after --apply (simulating an intervening write) must
                     make --undo refuse rather than silently overwrite past the drift.
  4. double-undo   — running --undo twice on the same journal must be a no-op the second time,
                     not a re-application or a corruption.
  5. DOSE          — a deliberately broken copy of repartition-hubs.py (one restore path in the
                     "move" case swapped to write the WRONG value) run through the SAME fixture
                     must make sub-test 1's byte-identical check go RED, and the shipped,
                     unmodified script must still go GREEN on the next cycle. Without this, a
                     byte-identical assertion that never fires proves nothing — the DOSE is what
                     tells "the check has teeth" apart from "the fixture happens to pass".
  6. --only-org    — a second fixture (seed_scope_fixture) exercising the org-scope filter added
                     for gate 3 ("apply on ONE org and verify"): (a) a scoped apply touches ONLY
                     the named org's rows — a clean cross-org MOVE and a clean same-org RETIRE both
                     land, while a SplitCo group straddling org 5/2/1 is DEFERRED whole rather than
                     half-applied, and every row outside scope stays byte-identical; (b) undoing
                     that journal with no `--only-org` (trusting its own recorded scope) restores
                     exactly that slice; (c) undoing it with a MISMATCHED `--only-org` refuses
                     before writing anything, and the matching scope still undoes cleanly
                     afterward; (d) a second DOSE — forcing every RETIRE to pass the org filter
                     regardless of its actual org — must leak the excluded-org Untouched retire
                     (RED), and the shipped script on the same fixture must not (GREEN).

BYTE-IDENTICAL CAVEAT: `entities` has a BEFORE UPDATE trigger (`entities_touch_updated_at`,
schema.sql) that stamps `updated_at=now()` on every UPDATE — including the undo's own restorative
UPDATE. A row touched by apply and then restored by undo necessarily ends with a LATER
`updated_at` than it started with; that is a property of the trigger, not a defect in --undo, and
no reversal mechanism built on UPDATE statements can avoid it without disabling the trigger (which
would then diverge from what a real apply/undo actually does on the live corebrain). Every OTHER
column is compared, including `valid_from` (untouched by any statement in this whole file) and
every entity_edges column (no such trigger there).

ISOLATION (same posture as test_fresh_spawn_brain.sh, lighter-weight): a fresh scratch DB
(`corebrain_repartundo_<pid>`) provisioned from the SAME schema.sql / run-migrations.sh /
init-brain-roles.sh the real installer uses, and a scratch vault dir under a tempfile.mkdtemp() —
never $CORE_BRAIN, never the `corebrain` database. Every subprocess invocation of
repartition-hubs.py is pinned to these via COREBRAIN_DB / CORE_BRAIN env vars. `dropdb` + `rmtree`
run in a `finally`. SKIPs (never FAILs) when Postgres/psql/createdb aren't available at all, per
this directory's convention (test_fresh_spawn_brain.sh) for an environment-dependent acceptance test.

Run: python3 bin/tests/test_repartition_undo.py
Needs: local Postgres with createdb rights (same box repartition-hubs.py itself runs on).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

REPO = core_root()
SRC = REPO / "bin" / "repartition-hubs.py"
SCHEMA = REPO / "scheduling" / "brain-pg" / "schema.sql"

PID = os.getpid()
SCRATCH_DB = f"corebrain_repartundo_{PID}"

ENTITY_COLS = ("id", "name", "kind", "compiled_truth_md", "last_compiled_at", "confidence",
               "ownership_tag", "scope", "source_file", "valid_from", "valid_until",
               "superseded_by", "org_id")  # updated_at deliberately excluded — see module docstring
EDGE_COLS = ("id", "from_entity_id", "to_entity_id", "edge_type", "confidence", "confidence_label",
             "source_file", "org_id", "is_cross_org", "created_at")

passes: list[str] = []
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    (passes if cond else failures).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"\n          {detail}"))
    return cond


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def run_cli(args: list[str], vault: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CORE_BRAIN"] = str(vault)
    env["COREBRAIN_DB"] = SCRATCH_DB
    env.pop("CLAUDE_PROJECT_DIR", None)
    return sh([sys.executable, str(SRC), *args], env=env)


def hub(vault: Path, stem: str, cite_slug: str = "business") -> str:
    """A real markdown file under the vault's entities/ dir whose citations dominate-resolve to
    `cite_slug` (hub_ownership.SLUG_TO_ORG) — the REAL classification rule, not a stand-in."""
    p = vault / "entities" / f"{stem}.md"
    p.write_text(
        f"---\nname: {stem}\n---\n\nDiscussed in projects/{cite_slug}/sessions/2026-01-01.md and "
        f"projects/{cite_slug}/subagents/note.md for context.\n")
    return str(p)


def provision() -> None:
    sh(["dropdb", "--if-exists", SCRATCH_DB])
    r = sh(["createdb", SCRATCH_DB])
    if r.returncode != 0:
        raise RuntimeError(f"createdb failed: {r.stderr}")
    r = sh(["psql", "-d", SCRATCH_DB, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(SCHEMA)])
    if r.returncode != 0:
        raise RuntimeError(f"schema.sql failed: {r.stderr}\n{r.stdout}")
    r = sh(["bash", str(REPO / "bin" / "run-migrations.sh"), "--ensure"],
           env={**os.environ, "COREBRAIN_DB": SCRATCH_DB})
    if r.returncode != 0:
        raise RuntimeError(f"run-migrations.sh --ensure failed: {r.stderr}\n{r.stdout}")
    r = sh(["bash", str(REPO / "bin" / "init-brain-roles.sh")],
           env={**os.environ, "COREBRAIN_DB": SCRATCH_DB})
    if r.returncode != 0:
        raise RuntimeError(f"init-brain-roles.sh failed: {r.stderr}\n{r.stdout}")


def connect():
    import psycopg2
    return psycopg2.connect(dbname=SCRATCH_DB, user="brain_admin")


def reset_tables(conn) -> None:
    """DELETE, not TRUNCATE: brain_admin (the same role apply/undo connect as — see connect() in
    repartition-hubs.py) has DML grants but not the separate TRUNCATE privilege, and granting it
    here would test against a wider permission set than the tool actually runs with. Sequence
    counters are left to keep climbing across resets, which is fine — each sub-test's assertions
    compare rows by value within that ONE cycle's own ids, never across cycles."""
    cur = conn.cursor()
    cur.execute("DELETE FROM entity_edges")
    cur.execute("DELETE FROM entities")
    conn.commit()


def seed_fixture(conn, vault: Path) -> dict:
    """The scenario documented in the module docstring: three (kind,name) groups exercising
    move / revive / retire+truth_carry twice, plus five edges exercising both edge_repoint
    branches (a repointed winner and a deleted duplicate) and one edge_reorg fix. Every hub file
    cites "business" (SLUG_TO_ORG business=2) so every hub-driven group targets org 2; the
    entities being corrected sit at org 5 to mirror the real ops-flat-pool scenario this tool
    was built for.
    """
    (vault / "entities").mkdir(parents=True, exist_ok=True)
    widget_md = hub(vault, "widget")
    gadget_md = hub(vault, "gadget")
    gizmo_md = hub(vault, "gizmo")

    cur = conn.cursor()

    def ins(name, kind, org, source_file, truth=None, compiled_at=None, conf=None,
            valid_until=None, superseded_by=None):
        cur.execute(
            "INSERT INTO entities (name, kind, org_id, source_file, compiled_truth_md, "
            "  last_compiled_at, confidence, valid_until, superseded_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (name, kind, org, source_file, truth, compiled_at, conf, valid_until, superseded_by))
        return cur.fetchone()[0]

    ids = {}
    # --- MOVE: one hub row, misfiled at org 5, no occupant at its target (org 2) ---
    ids["widget"] = ins("Widget", "Entity", 5, widget_md)

    # --- RETIRE + TRUTH_CARRY into an EXISTING graph node ---
    ids["n_gadget"] = ins("Gadget", "Entity", 2,
                          "projects/business/sessions/2026-01-10/subagents/gadget-note.md")
    ids["h_gadget"] = ins("Gadget", "Entity", 5, gadget_md,
                          truth="H TRUTH TEXT", compiled_at="2026-08-20 00:00:00+00", conf=0.77)

    # --- REVIVE + TRUTH_CARRY into a REVIVED tombstone ---
    ids["tz_gizmo"] = ins("Gizmo", "Entity", 2, None,
                          truth="STALE TOMBSTONE TRUTH", compiled_at="2026-01-01 00:00:00+00",
                          conf=0.20, valid_until="2026-01-01 00:00:00+00")
    ids["l_gizmo"] = ins("Gizmo", "Entity", 5, gizmo_md,
                         truth="L FRESH TRUTH", compiled_at="2026-08-25 00:00:00+00", conf=0.90)

    # --- stable anchors: never touched by any group above ---
    ids["anchor"] = ins("Anchor Thing", "Entity", 2,
                        "projects/business/sessions/2026-02-01/subagents/anchor.md")
    ids["anchor2"] = ins("Second Anchor", "Entity", 2,
                         "projects/business/sessions/2026-02-02/subagents/anchor2.md")

    def edge(frm, to, etype, org):
        cur.execute(
            "INSERT INTO entity_edges (from_entity_id, to_entity_id, edge_type, org_id) "
            "VALUES (%s,%s,%s,%s) RETURNING id", (frm, to, etype, org))
        return cur.fetchone()[0]

    # N already owns this edge — H's identical edge (after remap) must be DELETED as a duplicate.
    ids["ex"] = edge(ids["n_gadget"], ids["anchor"], "references", 2)
    ids["ey"] = edge(ids["h_gadget"], ids["anchor"], "references", 5)
    # No collision after remap — this one is REPOINTED (a winner).
    ids["ez"] = edge(ids["h_gadget"], ids["anchor2"], "motivated_by", 5)
    # Repoints into the REVIVED tombstone — also a winner.
    ids["ea"] = edge(ids["l_gizmo"], ids["anchor"], "learned_from", 5)
    # Deliberately wrong org_id (99) on an edge whose endpoints agree (both org 2) — edge_reorg
    # must fix it.
    ids["ew"] = edge(ids["anchor"], ids["anchor2"], "cross_impacts", 99)

    conn.commit()
    return ids


def seed_scope_fixture(conn, vault: Path) -> dict:
    """Fixture for `--only-org` (ops/Nick gate 3, 2026-09-02). Four (kind,name) groups, scoped
    against --only-org 5 throughout the tests that use this fixture.

    EVERY group here respects `entities_kind_name_org_unique` -- `UNIQUE(org_id, kind, name)`,
    UNCONDITIONAL, not partial on valid_until (schema.sql:50) -- so at most ONE row, live or
    retired, can EVER exist per (org, kind, name). No two rows in any group below share an org.
    That constraint has a real scoping consequence: a RETIRE's org_id can never equal its own
    group's target org (the target is where the SURVIVOR sits, and the survivor already occupies
    that (org,kind,name) slot), while a MOVE's to_org always EQUALS the target -- so a move into
    org 5 is unconditionally "in scope" under --only-org 5 (to_org=5), but a retire in that SAME
    group is in scope only when that specific duplicate's OWN org happens to be 5, never because
    the group's target is 5.

      - ScopeMove   : ONE row, misfiled at org 3, hub-cited "ops" (target 5) -> a clean MOVE
                      3->5. Single-action group: can never be "split" on its own, and is in scope
                      purely because to_org=5 (either-side rule).
      - ScopeRetire : survivor SURV already at org 2 (hub-cited "business", target 2 -- a KEEP,
                      no gating action of its own) with ONE duplicate DUP at org 5 (ALSO hub-cited
                      "business", so it shares SURV's target) -> a clean RETIRE, org_id=5, the
                      ONLY action this group produces. Fully in scope under --only-org 5, and SURV
                      itself (org 2) is never written to -- only DUP (org 5) is touched.
      - SplitCo     : three rows, at org 2, org 1, and org 3, ALL hub-cited "ops" (target 5, none
                      of them already at 5) -> the org-2 row wins survivor selection and MOVES to
                      org 5 (to_org=5 -> in scope), while the org-1 and org-3 rows RETIRE
                      (org_id=1, org_id=3 -> both out of scope). A genuine split: the group must be
                      DEFERRED whole under --only-org 5, so even the winning move never happens.
      - Untouched   : survivor at org 2 (hub-cited "business", target 2 -- a KEEP) with one
                      duplicate at org 1 (non-hub) -> a retire entirely outside {5}; must be
                      silently excluded and left byte-identical.
      - Bystander   : one unrelated org-2 entity, no hub file, not part of any collision key at
                      all -- pure control for "everything else is byte-identical".

    Every hub-driven target is computed from real citations via hub_ownership.owner_for_path
    (through hub()/classify()), same as seed_fixture() above -- not a stand-in.
    """
    (vault / "entities").mkdir(parents=True, exist_ok=True)
    scopemove_md = hub(vault, "scopemove", "ops")
    scoperetire_surv_md = hub(vault, "scoperetire-surv", "business")
    scoperetire_dup_md = hub(vault, "scoperetire-dup", "business")
    split_org2_md = hub(vault, "splitco-org2", "ops")
    split_org1_md = hub(vault, "splitco-org1", "ops")
    split_org3_md = hub(vault, "splitco-org3", "ops")
    untouched_surv_md = hub(vault, "untouched-surv", "business")

    cur = conn.cursor()

    def ins(name, kind, org, source_file, truth=None, compiled_at=None, conf=None):
        cur.execute(
            "INSERT INTO entities (name, kind, org_id, source_file, compiled_truth_md, "
            "  last_compiled_at, confidence) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (name, kind, org, source_file, truth, compiled_at, conf))
        return cur.fetchone()[0]

    ids = {}
    # --- ScopeMove: misfiled at org 3, belongs at org 5 (either-side: to_org=5 -> in scope) ---
    ids["scope_move"] = ins("ScopeMove", "Entity", 3, scopemove_md)

    # --- ScopeRetire: survivor stays at org 2 untouched; the org-5 duplicate is the only action --
    ids["scope_retire_surv"] = ins("ScopeRetire", "Entity", 2, scoperetire_surv_md)
    ids["scope_retire_dup"] = ins("ScopeRetire", "Entity", 5, scoperetire_dup_md)

    # --- SplitCo: org-2 row wins (MOVES to 5); org-1 and org-3 rows retire -- a genuine split.
    #     Inserted in this order so the (untimestamped, no-truth) tiebreak -- lowest id -- picks
    #     the org-2 row as survivor, matching the comment above.
    ids["split_winner"] = ins("SplitCo", "Entity", 2, split_org2_md)
    ids["split_dup1"] = ins("SplitCo", "Entity", 1, split_org1_md)
    ids["split_dup3"] = ins("SplitCo", "Entity", 3, split_org3_md)

    # --- Untouched: survivor at org 2, one duplicate at org 1 -- nothing here is ever near org 5 --
    ids["untouched_surv"] = ins("Untouched", "Entity", 2, untouched_surv_md)
    ids["untouched_dup"] = ins("Untouched", "Entity", 1,
                               "projects/life/sessions/2026-03-01/subagents/untouched-dup.md")

    # --- Bystander: not part of any (kind,name) collision key at all ---
    ids["bystander"] = ins("Bystander Thing", "Entity", 2,
                           "projects/business/sessions/2026-03-02/subagents/bystander.md")

    conn.commit()
    return ids


def snapshot(conn):
    cur = conn.cursor()
    cur.execute(f"SELECT {','.join(ENTITY_COLS)} FROM entities ORDER BY id")
    ents = cur.fetchall()
    cur.execute(f"SELECT {','.join(EDGE_COLS)} FROM entity_edges ORDER BY id")
    edges = cur.fetchall()
    return ents, edges


def diff(before, after):
    b_ents, b_edges = before
    a_ents, a_edges = after
    problems = []
    if b_ents != a_ents:
        bmap = {r[0]: r for r in b_ents}
        amap = {r[0]: r for r in a_ents}
        for eid in sorted(set(bmap) | set(amap)):
            if bmap.get(eid) != amap.get(eid):
                problems.append(f"entities id={eid} before={bmap.get(eid)} after={amap.get(eid)}")
    if b_edges != a_edges:
        bmap = {r[0]: r for r in b_edges}
        amap = {r[0]: r for r in a_edges}
        for eid in sorted(set(bmap) | set(amap)):
            if bmap.get(eid) != amap.get(eid):
                problems.append(f"entity_edges id={eid} before={bmap.get(eid)} after={amap.get(eid)}")
    return problems


def full_cycle(vault_root: Path, script: Path, label: str):
    """Reset, seed, apply, undo.

    Returns (pre_snapshot, post_apply_snapshot, post_undo_snapshot, problems (pre vs post-undo),
    apply_cp, undo_cp, journal_path).
    """
    conn = connect()
    reset_tables(conn)
    vault = vault_root / label
    shutil.rmtree(vault, ignore_errors=True)
    vault.mkdir(parents=True)
    seed_fixture(conn, vault)
    pre = snapshot(conn)

    journal = vault_root / f"{label}.journal.jsonl"
    journal.unlink(missing_ok=True)
    apply_cp = sh([sys.executable, str(script), "--apply", "--journal", str(journal)],
                  env={**os.environ, "CORE_BRAIN": str(vault), "COREBRAIN_DB": SCRATCH_DB})
    post_apply = snapshot(conn)
    undo_cp = sh([sys.executable, str(script), "--undo", str(journal)],
                 env={**os.environ, "CORE_BRAIN": str(vault), "COREBRAIN_DB": SCRATCH_DB})
    post_undo = snapshot(conn)
    conn.close()
    return pre, post_apply, post_undo, diff(pre, post_undo), apply_cp, undo_cp, journal


def scoped_cycle(vault_root: Path, script: Path, label: str,
                  only_org_apply: list[str] | None, only_org_undo: list[str] | None,
                  skip_undo: bool = False):
    """Same shape as full_cycle, but against seed_scope_fixture() and with `--only-org` passed to
    the apply and/or undo subprocess (each is a list of raw CLI values, e.g. ["5"] or ["2", "5"];
    None means the flag is omitted entirely for that call). Also writes the dry-run --json
    alongside the apply, so callers can inspect `deferred_groups` without re-parsing stdout.

    `skip_undo=True` runs the apply ONLY and returns before touching the journal again — for a
    test (like the mismatched-scope refusal below) that needs to drive its OWN separate undo
    invocation(s) against a journal that has NOT already been undone once by this helper.

    Returns (ids, pre_snapshot, post_apply_snapshot, post_undo_snapshot, apply_cp, undo_cp,
    journal_path, plan_dict). post_undo/undo_cp are (None, None) when skip_undo=True.
    """
    conn = connect()
    reset_tables(conn)
    vault = vault_root / label
    shutil.rmtree(vault, ignore_errors=True)
    vault.mkdir(parents=True)
    ids = seed_scope_fixture(conn, vault)
    pre = snapshot(conn)

    journal = vault_root / f"{label}.journal.jsonl"
    journal.unlink(missing_ok=True)
    jsonout = vault_root / f"{label}.plan.json"
    jsonout.unlink(missing_ok=True)
    apply_args = ["--apply", "--journal", str(journal), "--json", str(jsonout)]
    for o in (only_org_apply or []):
        apply_args += ["--only-org", o]
    apply_cp = sh([sys.executable, str(script), *apply_args],
                  env={**os.environ, "CORE_BRAIN": str(vault), "COREBRAIN_DB": SCRATCH_DB})
    post_apply = snapshot(conn)
    plan = json.loads(jsonout.read_text()) if jsonout.exists() else {}

    if skip_undo:
        conn.close()
        return ids, pre, post_apply, None, apply_cp, None, journal, plan

    undo_args = ["--undo", str(journal)]
    for o in (only_org_undo or []):
        undo_args += ["--only-org", o]
    undo_cp = sh([sys.executable, str(script), *undo_args],
                 env={**os.environ, "CORE_BRAIN": str(vault), "COREBRAIN_DB": SCRATCH_DB})
    post_undo = snapshot(conn)
    conn.close()
    return ids, pre, post_apply, post_undo, apply_cp, undo_cp, journal, plan


def main() -> int:
    print("=== repartition-hubs.py --undo: byte-identical restore, executed against a scratch DB ===\n")

    for tool in ("psql", "createdb", "dropdb", "pg_isready"):
        if not shutil.which(tool):
            print(f"SKIP: '{tool}' not found — cannot run this acceptance test")
            return 0
    if sh(["pg_isready"]).returncode != 0:
        print("SKIP: Postgres not reachable — cannot run this acceptance test")
        return 0
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        print("SKIP: psycopg2 not importable")
        return 0

    work = Path(tempfile.mkdtemp(prefix="repart-undo-test-"))
    provisioned = False
    try:
        try:
            provision()
            provisioned = True
        except Exception as e:
            print(f"SKIP: could not provision scratch DB {SCRATCH_DB} ({e})")
            return 0

        # ============================================================================ 1. FULL CYCLE
        print("--- 1. full apply+undo cycle: byte-identical to pre-apply ---")
        pre, post_apply, post_undo, problems, apply_cp, undo_cp, journal = full_cycle(work, SRC, "cycle1")
        check("apply exits 0", apply_cp.returncode == 0,
              f"stdout={apply_cp.stdout}\nstderr={apply_cp.stderr}")
        check("apply actually changed the DB (the fixture exercises the code, not a no-op)",
              pre != post_apply,
              "pre-apply and post-apply snapshots are identical — the fixture never engaged "
              "build_plan/apply_plan, so a later byte-identical pass would prove nothing")
        check("undo exits 0", undo_cp.returncode == 0,
              f"stdout={undo_cp.stdout}\nstderr={undo_cp.stderr}")
        kinds_seen = set()
        for line in journal.read_text().splitlines():
            kinds_seen.add(json.loads(line)["op"])
        check("journal contains _scope (2026-09-02, --only-org's header, None here since this "
              "apply is unscoped) plus revive/move/truth_carry/edge_repoint/edge_reorg/retire_batch",
              kinds_seen == {"_scope", "revive", "move", "truth_carry", "edge_repoint", "edge_reorg",
                             "retire_batch"},
              f"got: {sorted(kinds_seen)}")
        check("DB after undo is byte-identical to DB before apply (entities+entity_edges, "
              "excluding updated_at — see module docstring)",
              not problems, "\n          ".join(problems))
        check("undo wrote the idempotency marker",
              Path(str(journal) + ".undone").exists())

        # ============================================================================ 2. UNKNOWN KIND
        print("\n--- 2. unknown action kind aborts the WHOLE undo ---")
        conn = connect()
        reset_tables(conn)
        vault2 = work / "cycle2"
        shutil.rmtree(vault2, ignore_errors=True)
        vault2.mkdir(parents=True)
        seed_fixture(conn, vault2)
        pre2 = snapshot(conn)
        journal2 = work / "cycle2.journal.jsonl"
        journal2.unlink(missing_ok=True)
        apply_cp2 = run_cli(["--apply", "--journal", str(journal2)], vault2)
        check("cycle 2 apply exits 0", apply_cp2.returncode == 0, apply_cp2.stderr)
        post_apply2 = snapshot(conn)
        lines = journal2.read_text().splitlines()
        lines.insert(len(lines) // 2, json.dumps({"op": "mystery_action_nobody_wrote", "id": 999999}))
        journal2.write_text("\n".join(lines) + "\n")
        undo_cp2 = sh([sys.executable, str(SRC), "--undo", str(journal2)],
                      env={**os.environ, "CORE_BRAIN": str(vault2), "COREBRAIN_DB": SCRATCH_DB})
        check("undo on a journal with an unknown op exits non-zero", undo_cp2.returncode != 0,
              f"stdout={undo_cp2.stdout} stderr={undo_cp2.stderr}")
        check("...and mentions the unknown op", "mystery_action_nobody_wrote" in undo_cp2.stderr)
        after_bad_undo2 = snapshot(conn)
        check("...and wrote NOTHING — DB unchanged from post-apply state (whole-abort, not partial)",
              after_bad_undo2 == post_apply2)
        conn.close()

        # ============================================================================ 3. DRIFT
        print("\n--- 3. a drifted row causes refusal ---")
        conn = connect()
        reset_tables(conn)
        vault3 = work / "cycle3"
        shutil.rmtree(vault3, ignore_errors=True)
        vault3.mkdir(parents=True)
        ids3 = seed_fixture(conn, vault3)
        journal3 = work / "cycle3.journal.jsonl"
        journal3.unlink(missing_ok=True)
        apply_cp3 = run_cli(["--apply", "--journal", str(journal3)], vault3)
        check("cycle 3 apply exits 0", apply_cp3.returncode == 0, apply_cp3.stderr)
        # Simulate an intervening external write: the moved Widget's org_id gets changed again by
        # something else, AFTER the apply this journal describes.
        cur = conn.cursor()
        cur.execute("UPDATE entities SET org_id=4 WHERE id=%s", (ids3["widget"],))
        conn.commit()
        drifted_state = snapshot(conn)
        undo_cp3 = sh([sys.executable, str(SRC), "--undo", str(journal3)],
                      env={**os.environ, "CORE_BRAIN": str(vault3), "COREBRAIN_DB": SCRATCH_DB})
        check("undo refuses when a touched row drifted after apply", undo_cp3.returncode != 0,
              f"stdout={undo_cp3.stdout} stderr={undo_cp3.stderr}")
        check(f"...and names the drifted entity id ({ids3['widget']})",
              str(ids3["widget"]) in undo_cp3.stderr, undo_cp3.stderr)
        check("...and left the drifted DB state untouched (no partial write)",
              snapshot(conn) == drifted_state)
        conn.close()

        # ============================================================================ 4. DOUBLE-UNDO
        print("\n--- 4. double-undo is a safe no-op ---")
        conn = connect()
        reset_tables(conn)
        vault4 = work / "cycle4"
        shutil.rmtree(vault4, ignore_errors=True)
        vault4.mkdir(parents=True)
        seed_fixture(conn, vault4)
        journal4 = work / "cycle4.journal.jsonl"
        journal4.unlink(missing_ok=True)
        apply_cp4 = run_cli(["--apply", "--journal", str(journal4)], vault4)
        check("cycle 4 apply exits 0", apply_cp4.returncode == 0, apply_cp4.stderr)
        undo_cp4a = sh([sys.executable, str(SRC), "--undo", str(journal4)],
                       env={**os.environ, "CORE_BRAIN": str(vault4), "COREBRAIN_DB": SCRATCH_DB})
        check("first undo exits 0", undo_cp4a.returncode == 0, undo_cp4a.stderr)
        state_after_first_undo = snapshot(conn)
        undo_cp4b = sh([sys.executable, str(SRC), "--undo", str(journal4)],
                       env={**os.environ, "CORE_BRAIN": str(vault4), "COREBRAIN_DB": SCRATCH_DB})
        check("second undo on the same journal ALSO exits 0 (no-op, not an error)",
              undo_cp4b.returncode == 0, undo_cp4b.stderr)
        check("...reports it was already undone", "already undone" in undo_cp4b.stdout.lower(),
              undo_cp4b.stdout)
        check("...and made no further change to the DB",
              snapshot(conn) == state_after_first_undo)
        conn.close()

        # ============================================================================ 5. DOSE
        print("\n--- 5. DOSE: break one restore path, confirm RED, restore, confirm GREEN ---")
        src_text = SRC.read_text()
        target = ('"UPDATE entities SET org_id=%s WHERE id=%s", (from_org, eid),\n'
                  '                f"move-undo id={eid}"))')
        replacement = ('"UPDATE entities SET org_id=%s WHERE id=%s", (to_org, eid),\n'
                       '                f"move-undo id={eid}"))')
        if target not in src_text:
            check("could locate the move-undo restore line to break", False,
                  "the exact string changed — re-point this DOSE at the new shape rather than "
                  "deleting it (the point is a real break, not a re-implementation of the check)")
        else:
            mutated = src_text.replace(target, replacement, 1)
            check("the mutation actually changed something",
                  mutated != src_text and replacement in mutated)
            # repartition-hubs.py resolves core_seat/hub_ownership relative to ITS OWN __file__,
            # so the broken copy needs those siblings importable the same way the original does —
            # copying the whole bin/ dir alongside it would defeat the point (we'd be running a
            # second full copy of the tool). Simplest correct fix: run it FROM bin/'s own
            # directory by writing the broken copy INTO bin/ under a name run-all.sh's test_*.py
            # glob won't pick up, then removing it in `finally`.
            broken_in_place = REPO / "bin" / "_repartition_hubs_DOSE_test_only.py"
            broken_in_place.write_text(mutated)
            try:
                _pre5, _pa5, _pu5, problems5, apply_cp5, undo_cp5, _j5 = full_cycle(
                    work, broken_in_place, "cycle5")
                check("broken script's apply still exits 0 (the bug is in undo, not apply)",
                      apply_cp5.returncode == 0, apply_cp5.stderr)
                check("broken script's undo exits 0 (it 'succeeds' — silently wrong, which is why "
                      "a byte-identical check and not just an exit code matters)",
                      undo_cp5.returncode == 0, undo_cp5.stderr)
                check("RED: the broken move-undo IS caught — DB is NOT byte-identical after undo",
                      bool(problems5), "no diff found — the DOSE did not actually break anything, "
                      "which means check 1 above has no teeth")
            finally:
                broken_in_place.unlink(missing_ok=True)

            # Control: the SAME fixture through the SHIPPED script must be clean (confirms check 1
            # isn't failing for some unrelated reason, and that the fix is real, not the fixture).
            _pre6, _pa6, _pu6, problems6, apply_cp6, undo_cp6, _j6 = full_cycle(work, SRC, "cycle6")
            check("cycle 6 (control) apply exits 0", apply_cp6.returncode == 0, apply_cp6.stderr)
            check("cycle 6 (control) undo exits 0", undo_cp6.returncode == 0, undo_cp6.stderr)
            check("GREEN: the shipped (unbroken) script is byte-identical on the same fixture",
                  not problems6, "\n          ".join(problems6))

        # ============================================================================ 6. --ONLY-ORG
        print("\n--- 6. --only-org: scoped apply/undo, split-group deferral, scope-mismatch refusal ---")

        def row_of(snap, eid):
            for r in snap[0]:
                if r[0] == eid:
                    return r
            return None

        # -- 6a. scoped apply touches ONLY org-5 rows; everything else is byte-identical --
        # skip_undo=True: 6b drives its OWN separate undo call against this same journal below,
        # and a journal already undone once by scoped_cycle itself would make that call a no-op.
        ids6, pre6, post_apply6, _pu6x, apply_cp6, _u6x, journal6, plan6 = scoped_cycle(
            work, SRC, "scope6a", ["5"], None, skip_undo=True)
        check("6a scoped apply exits 0", apply_cp6.returncode == 0, apply_cp6.stderr)

        moved_row = row_of(post_apply6, ids6["scope_move"])
        check("6a ScopeMove (misfiled org 3) actually moved to org 5",
              moved_row is not None and moved_row[ENTITY_COLS.index("org_id")] == 5,
              f"row={moved_row}")
        dup_row = row_of(post_apply6, ids6["scope_retire_dup"])
        check("6a ScopeRetire's org-5 duplicate retired (superseded by its org-2 survivor) — the "
              "ONLY row this group touches is the one actually at org 5",
              dup_row is not None and dup_row[ENTITY_COLS.index("valid_until")] is not None
              and dup_row[ENTITY_COLS.index("superseded_by")] == ids6["scope_retire_surv"],
              f"row={dup_row}")

        out_of_scope_ids = [ids6[k] for k in ("scope_retire_surv", "split_winner", "split_dup1",
                                              "split_dup3", "untouched_surv", "untouched_dup",
                                              "bystander")]
        bmap6, amap6 = {r[0]: r for r in pre6[0]}, {r[0]: r for r in post_apply6[0]}
        leaked6a = [eid for eid in out_of_scope_ids if bmap6.get(eid) != amap6.get(eid)]
        check("6a everything OUT of scope (SplitCo x4, Untouched x2, Bystander) is byte-identical "
              "after a scoped apply — rows in other orgs are untouched",
              not leaked6a, f"changed ids: {leaked6a}")

        dg_names6 = {g["name"] for g in plan6.get("deferred_groups", [])}
        check("6a the split SplitCo group (org 5 + org 2 + org 1) is reported as DEFERRED, "
              "not silently dropped or half-applied", "SplitCo" in dg_names6,
              f"deferred groups seen: {dg_names6}")

        # -- 6b. scoped undo restores exactly that slice (no --only-org given: trusts the journal's
        #    own recorded scope, per --undo's documented behavior) --
        undo_cp6b = sh([sys.executable, str(SRC), "--undo", str(journal6)],
                       env={**os.environ, "CORE_BRAIN": str(work / "scope6a"), "COREBRAIN_DB": SCRATCH_DB})
        check("6b scoped undo (trusting the journal's own recorded scope) exits 0",
              undo_cp6b.returncode == 0, undo_cp6b.stderr)
        conn = connect()
        post_undo6b = snapshot(conn)
        conn.close()
        problems6b = diff(pre6, post_undo6b)
        check("6b DB after undo is byte-identical to pre-apply — the move AND the retire both "
              "reversed, and nothing outside that slice ever needed reversing",
              not problems6b, "\n          ".join(problems6b))

        # -- 6c. undo with a MISMATCHED scope refuses before writing --
        # skip_undo=True: this test drives its OWN two undo attempts (mismatched, then matching)
        # against a journal that must NOT already be undone when the first one runs.
        ids6c, pre6c, post_apply6c, _pu6c, apply_cp6c, _u6c, journal6c, _plan6c = scoped_cycle(
            work, SRC, "scope6c", ["5"], None, skip_undo=True)
        check("6c apply (--only-org 5) exits 0", apply_cp6c.returncode == 0, apply_cp6c.stderr)
        undo_cp6c = sh([sys.executable, str(SRC), "--undo", str(journal6c), "--only-org", "2"],
                       env={**os.environ, "CORE_BRAIN": str(work / "scope6c"), "COREBRAIN_DB": SCRATCH_DB})
        check("6c undo asked for a DIFFERENT scope (--only-org 2) than was applied (--only-org 5) "
              "exits non-zero", undo_cp6c.returncode != 0,
              f"stdout={undo_cp6c.stdout} stderr={undo_cp6c.stderr}")
        check("6c ...and names BOTH the requested and the recorded scope",
              "2" in undo_cp6c.stderr and "5" in undo_cp6c.stderr, undo_cp6c.stderr)
        conn = connect()
        after_refused_undo6c = snapshot(conn)
        conn.close()
        check("6c ...and wrote NOTHING — state unchanged from post-apply (whole-refuse, not partial)",
              after_refused_undo6c == post_apply6c)
        undo_cp6c_ok = sh([sys.executable, str(SRC), "--undo", str(journal6c), "--only-org", "5"],
                          env={**os.environ, "CORE_BRAIN": str(work / "scope6c"), "COREBRAIN_DB": SCRATCH_DB})
        check("6c ...but undo with the MATCHING scope (--only-org 5) exits 0",
              undo_cp6c_ok.returncode == 0, undo_cp6c_ok.stderr)

        # -- 6d. DOSE: break the org filter so a RETIRE leaks out of an excluded org --
        src_text6 = SRC.read_text()
        target6 = 'flags.extend(_incl(r["org_id"]) for r in pending_retires)'
        replacement6 = 'flags.extend(True for r in pending_retires)  # DOSE: ignore retire scoping'
        if target6 not in src_text6:
            check("6d could locate the retire-scoping line to break", False,
                  "the exact string changed — re-point this DOSE at the new shape rather than "
                  "deleting it (the point is a real break, not a re-implementation of the check)")
        else:
            mutated6 = src_text6.replace(target6, replacement6, 1)
            check("6d the mutation actually changed something",
                  mutated6 != src_text6 and replacement6 in mutated6)
            broken6 = REPO / "bin" / "_repartition_hubs_DOSE_scope_test_only.py"
            broken6.write_text(mutated6)
            try:
                idsD, preD, post_applyD, _puD, apply_cpD, _uD, _jD, _planD = scoped_cycle(
                    work, broken6, "scope6d", ["5"], None)
                check("6d broken script's scoped apply still exits 0 (the bug is in the filter, "
                      "not a crash)", apply_cpD.returncode == 0, apply_cpD.stderr)
                bmapD, amapD = {r[0]: r for r in preD[0]}, {r[0]: r for r in post_applyD[0]}
                # Forcing every retire flag True also fully includes SplitCo (its move flag was
                # already True on to_org=5 alone), so this DOSE leaks THREE ways: Untouched's org-1
                # duplicate retires, and SplitCo's org-1/org-3 duplicates retire alongside its move.
                leak_candidates = ("untouched_dup", "split_dup1", "split_dup3")
                leakedD = [k for k in leak_candidates
                           if bmapD.get(idsD[k]) != amapD.get(idsD[k])]
                check("6d RED: the broken filter LEAKS excluded-org retires (Untouched's org-1 "
                      "duplicate, and/or SplitCo's org-1/org-3 duplicates) that a correct "
                      "--only-org 5 must never touch", bool(leakedD),
                      "no leak found — the DOSE did not actually break anything, which means the "
                      "byte-identical check in 6a has no teeth")
            finally:
                broken6.unlink(missing_ok=True)

            # Control: the SAME fixture through the SHIPPED script must show no leak (confirms 6a
            # isn't failing for some unrelated reason, and that the scoping is real, not the fixture).
            idsG, preG, post_applyG, _puG, apply_cpG, _uG, _jG, _planG = scoped_cycle(
                work, SRC, "scope6e", ["5"], None)
            check("6d (control) shipped script's scoped apply exits 0", apply_cpG.returncode == 0,
                  apply_cpG.stderr)
            bmapG, amapG = {r[0]: r for r in preG[0]}, {r[0]: r for r in post_applyG[0]}
            leak_candidates_g = ("untouched_dup", "split_dup1", "split_dup3")
            leakedG = [k for k in leak_candidates_g
                       if bmapG.get(idsG[k]) != amapG.get(idsG[k])]
            check("6d GREEN: the shipped (unbroken) script leaks none of the excluded-org rows",
                  not leakedG, f"changed keys: {leakedG}")

    finally:
        if provisioned:
            # Terminate stragglers before dropping — a psycopg2 connection this file opened and
            # .close()'d can leave its server-side backend in a brief closing state, and dropdb
            # refuses (silently, since sh() never checks its exit code) while ANY backend still
            # holds the DB. Observed live: two scratch DBs from two earlier runs of this exact
            # file survived their own cleanup and had to be dropped by hand.
            sh(["psql", "-d", "postgres", "-v", "ON_ERROR_STOP=0", "-c",
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{SCRATCH_DB}' AND pid <> pg_backend_pid()"])
            drop_cp = sh(["dropdb", "--if-exists", SCRATCH_DB])
            if drop_cp.returncode != 0:
                print(f"  WARN: dropdb {SCRATCH_DB} failed — {drop_cp.stderr.strip()}. "
                      f"Drop it by hand: dropdb {SCRATCH_DB}")
        shutil.rmtree(work, ignore_errors=True)
        (REPO / "bin" / "_repartition_hubs_DOSE_test_only.py").unlink(missing_ok=True)
        (REPO / "bin" / "_repartition_hubs_DOSE_scope_test_only.py").unlink(missing_ok=True)

    print(f"\n=== Results: {len(passes)} passed, {len(failures)} failed ===")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
