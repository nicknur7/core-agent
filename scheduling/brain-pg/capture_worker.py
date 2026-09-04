#!/usr/bin/env python3
"""capture_worker.py — drains 'captured' stage jobs (memory-brain-SI unified redesign, step 1).

Plan: tasks/research/memory-brain-si-unified-redesign-2026-07-18.md PART 7.6 step ①.

The 'captured' stage = this Core's session JSONL is durably present in the brain vault as Markdown.
Work: run the existing exporter ($CORE_BRAIN/_build/export.py, JSONL → vault Markdown, skip-existing),
then for each claimed 'captured' job VERIFY that session's vault Markdown now exists before marking it
done (Codex's rule: completion is verified output, never a loose worker claim). A session whose vault
file is missing after export → fail (retry), so capture debt is visible instead of silently lost.

This replaces "capture only happens on the nightly heavy run or a full /close-core" with a job-driven
drain that any close can run — walk-aways included. Fork-safe (CORE_INSTANCE/CORE_BRAIN/CORE_ORG_ID).

Usage:
  CORE_ORG_ID=1 CORE_INSTANCE=... CORE_BRAIN=... python3 capture_worker.py
  ... python3 capture_worker.py --no-export   # verify/drain against the current vault (no re-export)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
from discover import CAPTURE_PROCESSOR_VERSION  # noqa: E402
from _env import connect_corebrain, connect_or_skip  # noqa: E402

WORKER_ID = f"capture/{os.getpid()}"


def _brain() -> Path:
    b = os.environ.get("CORE_BRAIN")
    if not b:
        raise SystemExit("CORE_BRAIN required")
    return Path(b)


def _run_export() -> int:
    """Run the vault exporter (all sessions, skip-existing). Returns its exit code."""
    export = _brain() / "_build" / "export.py"
    if not export.exists():
        print(f"  export.py not found at {export}", file=sys.stderr)
        return 1
    r = subprocess.run([sys.executable, str(export)], env={**os.environ},
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"  export.py exit={r.returncode}: {r.stderr[-300:]}", file=sys.stderr)
    return r.returncode


def _vault_md_for_session(session_key: str) -> Path | None:
    """The vault filename embeds the session UUID's first 8 hex chars (e.g. ..._47e9e38d.md).
    Return the vault Markdown path for this session if it exists, else None."""
    short = session_key.split("-")[0][:8].lower()
    for p in (_brain() / "projects").glob(f"*/sessions/*_{short}.md"):
        return p
    return None


def _source_key(conn, source_revision_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.source_key FROM source_revisions r JOIN sources s ON s.id = r.source_id
               WHERE r.id = %s""", (source_revision_id,))
        row = cur.fetchone()
        return row[0] if row else ""


def main() -> int:
    # Degrade with a named status instead of killing the close chain (see
    # _env.connect_or_skip). A Core with no database must still be able to close.
    conn = connect_or_skip("CAPTURE-worker")
    if conn is None:
        return 0
    stats = {"recovered": 0, "done": 0, "failed": 0, "claimed": 0}
    try:
        stats["recovered"] = ledger.recover_expired_leases(conn)
        export_rc = 0
        if "--no-export" not in sys.argv:
            print("  running export.py (JSONL → vault markdown, skip-existing) ...")
            export_rc = _run_export()
            if export_rc != 0:
                # Codex 2026-07-24: do NOT discard export's exit. A failed export can leave a
                # session's vault markdown STALE/partial while an old file still exists — the
                # existence-only job check below would then "complete" against stale content and
                # the caller (/close-core step 1) would mark the brain synced on unrefreshed data.
                # Propagate the failure so the close does NOT falsely certify sync.
                print(f"  WARNING: export.py failed (rc={export_rc}) — vault markdown may be stale; "
                      f"capture will report FAILURE so the close does not mark synced", file=sys.stderr)
        # Drain: claim each runnable 'captured' job, verify its vault md, complete or fail.
        while True:
            job = ledger.claim_job(conn, "captured", CAPTURE_PROCESSOR_VERSION, WORKER_ID)
            if not job:
                break
            stats["claimed"] += 1
            skey = _source_key(conn, job["source_revision_id"])
            vault = _vault_md_for_session(skey) if skey else None
            # Verify CONTENT, not just existence (Codex 2026-07-24): export.py can silently produce an
            # EMPTY session on a JSONL parse failure yet return 0, and a stale/empty pre-existing file
            # would otherwise "complete" the job → the close certifies sync against no real content.
            # Reject a zero-byte / whitespace-only vault md. (Deeper freshness/content-hash verification
            # + fixing export.py to fail-loud at source is tracked separately.)
            _vault_ok = False
            if vault is not None:
                try:
                    _vault_ok = bool(vault.read_text(errors="replace").strip())
                except OSError:
                    _vault_ok = False
            if _vault_ok:
                ledger.complete_job(conn, job["job_id"], job["attempt_no"],
                                    output_ref=str(vault), output_hash=ledger.content_hash_of(vault.read_bytes()),
                                    item_total=1, item_done=1)
                stats["done"] += 1
            else:
                ledger.fail_job(conn, job["job_id"], job["attempt_no"], "no-or-empty-vault-md",
                                f"export ran but vault markdown for session {skey} is missing or empty")
                stats["failed"] += 1
        print(f"capture_worker: {stats['done']} captured, {stats['failed']} failed, "
              f"{stats['recovered']} lease(s) recovered ({stats['claimed']} claimed)"
              + (f" [export rc={export_rc}]" if export_rc else ""))
        # Fail the whole run if ANY job failed OR the export itself failed (Codex 2026-07-24).
        return 0 if (stats["failed"] == 0 and export_rc == 0) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
