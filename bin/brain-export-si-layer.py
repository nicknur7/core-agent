#!/usr/bin/env python3
"""Export the part of the brain database that the vault CANNOT rebuild, into the brain repo.

THE PROBLEM THIS SOLVES. `core-brain` holds the vault — 11,065 markdown files, on GitHub, safe.
The Postgres database is a different thing in a different place (`/opt/homebrew/var/postgresql@17`,
not even inside the home folder) and is in no repo at all. Most of it does not need to be: entities,
evidence and edges are DERIVED from the vault and can be rebuilt by re-running extraction and
embedding. Expensive, not lost.

But a third layer exists that the vault cannot reproduce, because it never came from the vault:
every correction mined from a session transcript, every artifact the SI loop learned and installed,
the steering telemetry that decides whether a rule is earning its keep. That came from watching Nick
work. There is nothing to rebuild it from, and today it exists on exactly one disk.

    full database dump          336 MB   GitHub hard-rejects anything over 100 MB
    the irreplaceable layer      51 MB   as plain per-table SQL, largest file 30 MB

WHY PLAIN SQL AND NOT A GZIP, which is the counterintuitive half. Git stores what CHANGED between
commits. Plain text that is 95% identical to last week's export deltas down to almost nothing. A
gzip is effectively random bytes — flip one input byte and the whole file differs, so git stores a
fresh full copy every single time. The 13 MB compressed file makes the repository BIGGER than the
51 MB uncompressed one, within a few weeks. Per-table rather than one combined file keeps every
file under GitHub's 50 MB warning threshold and makes a diff show which layer actually moved.

EVERY TABLE IS CLASSIFIED, and `test_brain_export_covers_every_table.py` fails if one is not. That
matters more than it looks: the failure mode here is not a broken export, it is a NEW table added
six months from now that nobody thinks about, silently outside the backup, discovered only when it
is needed. An unclassified table is the bug; the test makes it impossible to introduce quietly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = os.environ.get("COREBRAIN_DB", "corebrain")
BRAIN = Path(os.environ.get("CORE_BRAIN") or (Path.home() / "AI Projects/core-brain"))
OUT = BRAIN / "db"
MANIFEST = OUT / "export-manifest.json"

# ── Cannot be rebuilt from the vault. This is what gets committed. ──────────────────────────────
IRREPLACEABLE = [
    "steering_events", "steering_judgments",     # did a rule earn its keep
    "friction_cases", "pattern_observations",    # corrections mined from transcripts
    "pattern_promotions", "detector_runs",
    "assertions", "assertion_relations",         # the recall/supersession layer
    "si_artifacts", "artifact_event", "artifact_outcome", "artifact_utility",
    "learned_contracts", "core_si_fix_approvals", "core_si_trusted_fixes",
    "workflow_steps", "workflow_triggers",
    "si_projection_state", "stage_attempts",
    # canonical-merge-apply.py's undo log (2026-08-31c migration made it brain_app-read-only,
    # brain_admin-write). "A description of a change is not a restore path; a row snapshot is" —
    # its own docstring. Not EXCLUDED: unlike entities_bak_orphans it is not a superseded backup,
    # it is the live, still-being-appended-to restore path for tonight's merges, and nothing else
    # holds these to_jsonb(row) snapshots. Test caught it unclassified at 21,472 rows.
    "merge_journal_20260828",
]

# ── Derived from the vault. Rebuildable by re-running extraction + embed; deliberately NOT here. ─
REGENERABLE = [
    "entities", "evidence", "entity_edges", "sources", "source_revisions",
    "ingest_log", "stage_jobs", "artifacts", "tenants", "schema_migrations",
]

# ── Superseded backups and dupes. Restorable from their own files; not brain content. ───────────
EXCLUDED = [
    "entities_bak_orphans", "entity_edges_bak_orphans",
    "pattern_observations_dupe_backup_20260805",
]

# ── One-off, hand-created tables from a specific incident on THIS seat's live corebrain —
# NOT created by schema.sql or any migration, so a fresh Core (or any seat that never hit that
# incident) legitimately never has them. Unlike the rest of IRREPLACEABLE/EXCLUDED, whose staleness
# would mean a real rename slipped past classification, these were always seat-local and dated on
# purpose (test_brain_export_covers_every_table.py exempts exactly this set from its "every listed
# table must currently be live" staleness check — found 2026-09-03 auditing a stranger's fresh
# clone, where all four failed that check unfixably). If one of these gets dropped by hand later,
# that's expected too — remove it from EXCLUDED/IRREPLACEABLE at that point, don't leave it stale.
ADHOC_SEAT_LOCAL = {
    "entities_bak_orphans", "entity_edges_bak_orphans",
    "pattern_observations_dupe_backup_20260805", "merge_journal_20260828",
}


def psql(sql: str, raw: bool = False) -> "str | None":
    """Run one statement. raw=True returns stdout UNSTRIPPED (for \\copy bodies, where trailing
    newlines are part of the payload) and None on failure, so a caller can tell empty from failed."""
    r = subprocess.run(["psql", "-d", DB, "-tAc", sql],
                       capture_output=True, text=True, timeout=600)
    if raw:
        return r.stdout if r.returncode == 0 else None
    return r.stdout.strip() if r.returncode == 0 else ""


# THIS SEAT'S ORG, resolved from identity — never a bare default (2026-08-28). A hardcoded
# CORE_ORG_ID=1 in fleet-shared code already cross-wrote partitions once, caught 2026-07-25.
# get_org_id() prefers identity.json over the environment and reports a disagreement loudly.
def _seat_org() -> int:
    inst = os.environ.get("CORE_INSTANCE") or str(Path(__file__).resolve().parents[1])
    sys.path.insert(0, str(Path(inst) / "scheduling" / "brain-pg"))
    from _env import get_org_id
    return int(get_org_id())


ORG = _seat_org()


def live_tables() -> list[str]:
    out = psql("select tablename from pg_tables where schemaname='public' order by 1")
    return [t for t in out.splitlines() if t.strip()]


def main() -> int:
    if not BRAIN.is_dir():
        print(f"[brain-export] no brain at {BRAIN} — nothing to do")
        return 0
    if not psql("select 1"):
        # Fail SOFT. This runs at close and a close must never break because Postgres is down.
        print("[brain-export] database not reachable — skipped (close continues)")
        return 0

    tables = live_tables()
    unclassified = [t for t in tables
                    if t not in IRREPLACEABLE and t not in REGENERABLE and t not in EXCLUDED]
    if unclassified:
        # Loud, but not fatal: an unclassified table is un-backed-up, and the operator needs to
        # decide which list it belongs in. The test is what actually enforces this.
        print(f"[brain-export] ⚠️  UNCLASSIFIED TABLE(S), currently NOT exported: {unclassified}")
        print("[brain-export]    add each to IRREPLACEABLE, REGENERABLE or EXCLUDED in this file.")

    (OUT / "si-layer").mkdir(parents=True, exist_ok=True)

    # Schema first — 118 KB, and it is what lets a restore rebuild structure from nothing.
    subprocess.run(["pg_dump", "-d", DB, "--schema-only", "-f", str(OUT / "schema.sql")],
                   capture_output=True, text=True, timeout=300)

    rows_total, bytes_total, per_table = 0, 0, {}
    for t in IRREPLACEABLE:
        if t not in tables:
            continue
        dest = OUT / "si-layer" / f"{t}.sql"
        # ORG-SCOPED (2026-08-28, found by core-school reviewing the pull before it landed on four
        # seats). This was a bare `pg_dump --data-only -t <table>`, which has no WHERE clause and
        # dumps the ENTIRE TABLE ACROSS ALL ORGS. It is wired into lifecycle_close(), so the moment
        # any peer pulled it, that seat's every close would write org 1-5 wholesale to
        # core-brain/db/si-layer/ — school's close copying finance's brokerage-derived SI rows, and
        # every seat becoming an exporter of every other seat's layer.
        #
        # That is the Privacy Principle's own case: "invoked only, scoped only, MINIMUM NECESSARY."
        # A cross-org backup may well be what Nick wants — core-brain is one shared vault — but it is
        # his call made knowingly, not one inherited silently from a hook that starts firing on pull.
        # Scoped is the safe default; widening it later is one flag, un-leaking is not.
        #
        # pg_dump cannot filter rows, so the org-scoped path uses COPY ... TO STDOUT with a WHERE and
        # writes a restorable COPY block. Tables without an org_id column (schema_migrations and the
        # like) fall back to the whole-table dump — they carry no per-seat data.
        has_org = (psql("select 1 from information_schema.columns "
                        f"where table_name='{t}' and column_name='org_id' limit 1") or "").strip() == "1"
        if has_org:
            cols = (psql("select string_agg(column_name, ', ' order by ordinal_position) "
                         f"from information_schema.columns where table_name='{t}'") or "").strip()
            body = psql(f"\\copy (select {cols} from {t} where org_id = {ORG}) to stdout", raw=True)
            if body is None:
                print(f"[brain-export] {t}: org-scoped copy failed — skipped")
                continue
            dest.write_text(
                f"-- org-scoped export: {t} WHERE org_id = {ORG}\n"
                f"COPY {t} ({cols}) FROM stdin;\n{body}\\.\n")
        else:
            r = subprocess.run(["pg_dump", "-d", DB, "--data-only", "-t", t, "-f", str(dest)],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                print(f"[brain-export] {t}: pg_dump failed — {r.stderr.strip()[:120]}")
                continue
        n = int(psql(f"select count(*) from {t}" + (f" where org_id = {ORG}" if has_org else "")) or 0)
        size = dest.stat().st_size
        per_table[t] = {"rows": n, "bytes": size}
        rows_total += n
        bytes_total += size

    # NEWEST ROW IN THE DATABASE, so staleness is measurable rather than assumed. A timestamp the
    # exporter writes about itself only proves the exporter ran; comparing against the data proves
    # the export is CURRENT. That distinction is what `contract-fitness.json` got wrong for nine
    # days in August — it had a timestamp and nothing compared it to anything.
    newest = psql("select max(ts)::text from steering_events") or ""

    MANIFEST.write_text(json.dumps({
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "newest_row_at_export": newest,
        "tables": per_table,
        "total_rows": rows_total,
        "total_bytes": bytes_total,
        "regenerable_not_exported": REGENERABLE,
        "excluded": EXCLUDED,
        "unclassified": unclassified,
    }, indent=2) + "\n")

    mb = bytes_total / 1_048_576
    print(f"[brain-export] {len(per_table)} table(s), {rows_total:,} rows, {mb:.1f} MB -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
