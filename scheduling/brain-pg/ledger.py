#!/usr/bin/env python3
"""ledger.py — the source-revision ledger writer/queue (keystone of the unified redesign).

Plan: tasks/research/memory-brain-si-unified-redesign-2026-07-18.md PART 7.2.
Tables: 2026-07-18-source-revision-ledger.sql (sources / source_revisions / stage_jobs / stage_attempts).

This is the control-plane library every processor uses. It provides:
  register_source()   — stable identity, rename-safe (get-or-create by source_key).
  register_revision() — append an immutable revision iff content changed (dedup by content_hash).
  enqueue_job()       — create a stage job for a revision (idempotent on the version fingerprint).
  claim_job()         — atomically lease the next runnable job (SKIP LOCKED); opens an attempt.
  complete_job()/fail_job() — close the attempt (append-only) + advance job state.
  recover_expired_leases() — reclaim leases whose owner died.

Fork-safe: all identity/paths come from CORE_ORG_ID + the caller; no life-specific assumptions.
Org-scoped via connect_corebrain() (brain_app + app.current_org_id GUC → RLS 'write own').
Caller owns the connection lifecycle unless a helper opens its own (claim/complete/fail commit atomically).
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import connect_corebrain, get_org_id  # noqa: E402

STAGES = ("captured", "mechanically_indexed", "semantically_interpreted", "recall_ready")
MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 600
DEFAULT_RETRY_BACKOFF_SECONDS = 120


# ── hashing helpers ──────────────────────────────────────────────────────────
def content_hash_of(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "ignore")
    return hashlib.sha256(data).hexdigest()


def compute_fingerprint(content_hash: str, stage: str, processor_version: str,
                        upstream_output_hashes: list[str] | None = None) -> str:
    """A stage job's identity. A change in content, stage, processor version, or any
    required upstream output creates a NEW fingerprint → a NEW job → version-triggered reprocessing."""
    parts = [content_hash or "", stage, processor_version]
    parts += sorted(upstream_output_hashes or [])
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# ── sources + revisions ──────────────────────────────────────────────────────
def register_source(conn, source_key: str, source_kind: str, current_uri: str | None) -> int:
    """Get-or-create a source by (org_id, source_key). On an existing source, refresh current_uri
    (rename tracking). Returns source_id. Caller commits."""
    org = get_org_id()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sources (org_id, source_key, source_kind, current_uri)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (org_id, source_key) DO UPDATE
                 SET current_uri = EXCLUDED.current_uri
               RETURNING id""",
            (org, source_key, source_kind, current_uri),
        )
        return cur.fetchone()[0]


def register_revision(conn, source_id: int, content_hash: str | None, source_uri: str,
                      operation: str = "upsert", byte_size: int | None = None,
                      previous_uri: str | None = None) -> tuple[int, int, bool]:
    """Append a source_revision iff it represents a NEW occurrence. Dedup rule: an 'upsert' whose
    content_hash equals the current latest revision's content_hash is a no-op (returns existing,
    is_new=False). tombstone/rename always append. Serializes per-source via a row lock on `sources`
    so revision_seq is gap-free under concurrency. Returns (revision_id, revision_seq, is_new)."""
    org = get_org_id()
    if operation not in ("upsert", "tombstone", "rename"):
        raise ValueError(f"bad operation {operation!r}")
    if operation == "tombstone":
        content_hash = None
    elif content_hash is None:
        raise ValueError("content_hash required for non-tombstone revision")
    with conn.cursor() as cur:
        # Lock the source row to serialize revision creation for this source.
        cur.execute("SELECT id FROM sources WHERE id = %s FOR UPDATE", (source_id,))
        if cur.fetchone() is None:
            raise ValueError(f"source {source_id} not found")
        cur.execute(
            """SELECT id, revision_seq, operation, content_hash
               FROM source_revisions WHERE source_id = %s
               ORDER BY revision_seq DESC LIMIT 1""",
            (source_id,),
        )
        latest = cur.fetchone()
        if latest and operation == "upsert" and latest[3] == content_hash and latest[2] != "tombstone":
            return latest[0], latest[1], False  # unchanged content → no new revision
        next_seq = (latest[1] + 1) if latest else 1
        supersedes = latest[0] if latest else None
        cur.execute(
            """INSERT INTO source_revisions
                 (org_id, source_id, revision_seq, operation, source_uri, previous_uri,
                  content_hash, byte_size, supersedes_revision_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (org, source_id, next_seq, operation, source_uri, previous_uri,
             content_hash, byte_size, supersedes),
        )
        rev_id = cur.fetchone()[0]
        return rev_id, next_seq, True


# ── stage jobs (durable queue) ───────────────────────────────────────────────
def enqueue_job(conn, source_revision_id: int, stage: str, processor_version: str,
                input_fingerprint: str) -> tuple[int, bool]:
    """Create a stage job (idempotent on the version fingerprint). Returns (job_id, is_new).
    An already-'done' job for the same fingerprint is not re-created. Caller commits."""
    org = get_org_id()
    if stage not in STAGES:
        raise ValueError(f"bad stage {stage!r}")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO stage_jobs
                 (org_id, source_revision_id, stage, processor_version, input_fingerprint, status)
               VALUES (%s,%s,%s,%s,%s,'pending')
               ON CONFLICT (source_revision_id, stage, processor_version, input_fingerprint)
                 DO NOTHING
               RETURNING id""",
            (org, source_revision_id, stage, processor_version, input_fingerprint),
        )
        row = cur.fetchone()
        if row:
            return row[0], True
        cur.execute(
            """SELECT id FROM stage_jobs
               WHERE source_revision_id=%s AND stage=%s AND processor_version=%s AND input_fingerprint=%s""",
            (source_revision_id, stage, processor_version, input_fingerprint),
        )
        return cur.fetchone()[0], False


def claim_job(conn, stage: str, processor_version: str, worker_id: str,
              lease_seconds: int = DEFAULT_LEASE_SECONDS):
    """Atomically lease the next runnable job for (stage, processor_version). Uses SELECT ... FOR
    UPDATE SKIP LOCKED so parallel workers never grab the same job. Opens a stage_attempt. Commits.
    Returns a dict {job_id, attempt_no, lease_token, source_revision_id} or None if nothing runnable."""
    token = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, attempts, source_revision_id FROM stage_jobs
               WHERE stage=%s AND processor_version=%s
                 AND status IN ('pending','retry_wait') AND next_attempt_at <= now()
               ORDER BY next_attempt_at, id
               FOR UPDATE SKIP LOCKED LIMIT 1""",
            (stage, processor_version),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        job_id, attempts, rev_id = row
        attempt_no = attempts + 1
        cur.execute(
            """UPDATE stage_jobs
               SET status='leased', lease_owner=%s, lease_token=%s,
                   lease_expires_at = now() + (%s || ' seconds')::interval,
                   attempts=%s, updated_at=now()
               WHERE id=%s""",
            (worker_id, token, str(lease_seconds), attempt_no, job_id),
        )
        cur.execute(
            """INSERT INTO stage_attempts
                 (org_id, stage_job_id, attempt_no, worker_id, lease_token, started_at)
               VALUES (%s,%s,%s,%s,%s, now())""",
            (get_org_id(), job_id, attempt_no, worker_id, token),
        )
    conn.commit()
    return {"job_id": job_id, "attempt_no": attempt_no, "lease_token": token,
            "source_revision_id": rev_id}


def complete_job(conn, job_id: int, attempt_no: int, output_ref: str | None = None,
                 output_hash: str | None = None, item_total: int | None = None,
                 item_done: int | None = None) -> None:
    """Mark the attempt done + the job done. Commits."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE stage_attempts SET completed_at=now(), outcome='done',
                   output_ref=%s, output_hash=%s, item_total=%s, item_done=%s
               WHERE stage_job_id=%s AND attempt_no=%s""",
            (output_ref, output_hash, item_total, item_done, job_id, attempt_no),
        )
        cur.execute(
            "UPDATE stage_jobs SET status='done', updated_at=now(), lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL WHERE id=%s",
            (job_id,),
        )
    conn.commit()


def fail_job(conn, job_id: int, attempt_no: int, error_code: str, error: str,
             retry_backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS) -> None:
    """Record a failed attempt (append-only). Reschedule for retry, or mark 'dead' at MAX_ATTEMPTS. Commits."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE stage_attempts SET completed_at=now(), outcome='failed',
                   error_code=%s, error_detail=%s WHERE stage_job_id=%s AND attempt_no=%s""",
            (error_code, error, job_id, attempt_no),
        )
        if attempt_no >= MAX_ATTEMPTS:
            cur.execute(
                """UPDATE stage_jobs SET status='dead', last_error_code=%s, last_error=%s,
                       updated_at=now(), lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL WHERE id=%s""",
                (error_code, error, job_id),
            )
        else:
            cur.execute(
                """UPDATE stage_jobs SET status='retry_wait', last_error_code=%s, last_error=%s,
                       next_attempt_at = now() + (%s || ' seconds')::interval,
                       updated_at=now(), lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL WHERE id=%s""",
                (error_code, error, str(retry_backoff_seconds), job_id),
            )
    conn.commit()


def recover_expired_leases(conn) -> int:
    """Reclaim jobs whose lease expired (worker died mid-stage): close the open attempt as
    lease_expired and return the job to retry_wait. Returns count recovered. Commits."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE stage_attempts a SET completed_at=now(), outcome='lease_expired'
               FROM stage_jobs j
               WHERE a.stage_job_id=j.id AND j.status='leased' AND j.lease_expires_at < now()
                 AND a.attempt_no=j.attempts AND a.completed_at IS NULL""",
        )
        cur.execute(
            """UPDATE stage_jobs SET status='retry_wait', next_attempt_at=now(),
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, updated_at=now()
               WHERE status='leased' AND lease_expires_at < now()""",
        )
        n = cur.rowcount
    conn.commit()
    return n


# ── smoke self-test: python3 ledger.py --selftest ───────────────────────────
def _selftest() -> int:
    conn = connect_corebrain()
    key = f"selftest-{uuid.uuid4().hex[:8]}"
    try:
        sid = register_source(conn, key, "memory_file", "selftest/x.md")
        r1, s1, new1 = register_revision(conn, sid, content_hash_of("v1"), "selftest/x.md", byte_size=2)
        r2, s2, new2 = register_revision(conn, sid, content_hash_of("v1"), "selftest/x.md", byte_size=2)  # dedup
        r3, s3, new3 = register_revision(conn, sid, content_hash_of("v2"), "selftest/x.md", byte_size=2)
        conn.commit()
        assert new1 and not new2 and new3 and s1 == 1 and s2 == 1 and s3 == 2, (new1, new2, new3, s1, s2, s3)
        fp = compute_fingerprint(content_hash_of("v2"), "captured", "test-v1")
        jid, jnew = enqueue_job(conn, r3, "captured", "test-v1", fp)
        conn.commit()
        assert jnew
        claim = claim_job(conn, "captured", "test-v1", "selftest-worker")
        assert claim and claim["job_id"] == jid
        complete_job(conn, jid, claim["attempt_no"], output_ref="selftest/out", output_hash="abc", item_total=1, item_done=1)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM stage_jobs WHERE id=%s", (jid,))
            assert cur.fetchone()[0] == "done"
        print("ledger selftest: PASS (source/revision dedup, job enqueue/claim/complete, RLS write-own)")
        return 0
    finally:
        # cleanup
        with conn.cursor() as cur:
            cur.execute("DELETE FROM stage_attempts WHERE stage_job_id IN (SELECT id FROM stage_jobs WHERE source_revision_id IN (SELECT id FROM source_revisions WHERE source_id IN (SELECT id FROM sources WHERE source_key=%s)))", (key,))
            cur.execute("DELETE FROM stage_jobs WHERE source_revision_id IN (SELECT id FROM source_revisions WHERE source_id IN (SELECT id FROM sources WHERE source_key=%s))", (key,))
            cur.execute("DELETE FROM source_revisions WHERE source_id IN (SELECT id FROM sources WHERE source_key=%s)", (key,))
            cur.execute("DELETE FROM sources WHERE source_key=%s", (key,))
        conn.commit()
        conn.close()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("ledger.py — import as a library, or run with --selftest", file=sys.stderr)
    sys.exit(0)
