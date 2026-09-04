#!/usr/bin/env python3
"""core-si trusted-fix admission — ADAS-style threshold-gated autonomy.

A deterministic core-si fix earns the right to auto-apply by accumulating K consecutive
POSITIVE signals. There are two roads to a positive signal and they carry equal weight:

  · Nick approves it explicitly            kind='approve'
  · a SHADOW run of its registered applier verifies it would have succeeded, applying
    nothing                                kind='evidence'

A 'reject' (his) or an 'evidence_fail' (a shadow run that would not have succeeded)
resets the streak.

WHY THE SECOND ROAD EXISTS (2026-08-26). Until today the only road was the first, and this
module's own docstring said so: "earns the right to auto-apply by being APPROVED K times
running." That is not a threshold, it is a dependency — an autonomy gate seeded by human
approvals CANNOT OUTRUN THE HUMAN. It can only ever act on classes Nick has already done by
hand, repeatedly, which is the exact labour he has been asking to be rid of since 2026-07-16.
It is also the opposite of the standing rule in tasks/lessons.md:132, written 2026-07-23:

    "Autonomous self-improvement = test-gate + reversibility, NOT a human approval gate"

The rule said test-gate for five weeks while the code said approval-gate. Evidence-seeded
admission is what makes the code agree with the rule.

SCOPE OF THE CHANGE — narrow on purpose. Admission still only marks a fix TRUSTED; it applies
nothing. The apply path remains `in_safe AND trusted AND has_applier`, so a key still cannot
be touched unless it is on scheduling/core-si/auto-safe.txt AND has a deterministic applier.
Evidence widens how the *trusted* term can be satisfied and touches neither of the other two.
Nick's approvals still count for exactly what they always did.

  python3 si-fix-admission.py --record sys-marker "verify vs baseline; clear if pushed"
  python3 si-fix-admission.py --record sys-docpath "exempt archival" --reject
  python3 si-fix-admission.py --check  sys-marker "verify vs baseline; clear if pushed"
  python3 si-fix-admission.py --list

Org-scoped: writes/reads through connect_corebrain (RLS -> app.current_org_id), so
each Core admits its OWN fixes from its OWN approval history. Mirrors learned-corpus-miner.

SAFETY: admission only marks a fix as trusted. It does NOT apply anything. The core-si
apply path is responsible for restricting auto-apply to life-local, reversible actions —
admission never makes an outward/destructive fix autonomous.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "brain-pg"))
from _env import load_secrets, connect_corebrain  # noqa: E402

load_secrets()

K_DEFAULT = 2  # consecutive approvals required to admit a fix to the trusted set
# (2026-06-27: lowered 5→2. K=5 was untested theory — nothing ever crossed it, and the
# loop was broken until 2026-06-23, so autonomy never visibly fired. K only governs
# graduation SPEED for fixes already on the AUTO_SAFE allowlist; the hard floor
# (outward/destructive never auto-applies) and the allowlist are the real safety gates.)


POSITIVE_KINDS = ("approve", "evidence")
RESET_KINDS = ("reject", "evidence_fail")


def _consecutive_approvals(cur, signal_key: str, fix_action: str) -> int:
    """Consecutive POSITIVE signals since the most recent reset, for this exact (signal, fix).

    Counts approve AND evidence; resets on reject AND evidence_fail. The old version broke only
    on the literal string "reject" and counted everything else as positive — which would have
    silently counted an `evidence_fail` as PROGRESS TOWARD TRUST the moment that kind existed.
    A failed shadow run advancing a fix toward autonomy is the exact inversion this gate exists
    to prevent, so the reset set is explicit and the positive set is a whitelist, not a
    not-reject default.
    """
    cur.execute(
        """SELECT kind FROM core_si_fix_approvals
           WHERE signal_key=%s AND fix_action=%s
           ORDER BY approved_at DESC, id DESC""",
        (signal_key, fix_action),
    )
    streak = 0
    for (kind,) in cur.fetchall():
        if kind in RESET_KINDS:
            break
        if kind in POSITIVE_KINDS:
            streak += 1
            continue
        # An unrecognised kind is NOT treated as positive. A future kind added to the CHECK
        # constraint without being classified here must not graduate anything by default.
        break
    return streak


def record_evidence(conn, signal_key: str, fix_action: str, ok: bool,
                    k: int = K_DEFAULT) -> dict:
    """Record the outcome of a SHADOW run — the applier's preconditions were verified and
    NOTHING was applied. `ok=False` resets the streak exactly as a human reject does."""  # privacy-ok: generic engineering vocabulary
    return record(conn, signal_key, fix_action,
                  kind="evidence" if ok else "evidence_fail", k=k)


def record(conn, signal_key: str, fix_action: str, kind: str = "approve",
           k: int = K_DEFAULT) -> dict:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO core_si_fix_approvals (signal_key, fix_action, kind) VALUES (%s,%s,%s)",
        (signal_key, fix_action, kind),
    )
    streak = _consecutive_approvals(cur, signal_key, fix_action)
    admitted = False
    # `kind in POSITIVE_KINDS`, not `kind == "approve"`. This read `== "approve"` until 2026-08-26,
    # which meant an evidence row counted toward the streak and then could not graduate it — the
    # streak hit K and admitted=False, forever. Caught by the test rather than by reading: the
    # counter was generalised and the admission trigger was not, so the new road led to a wall one
    # step short of the door.
    if kind in POSITIVE_KINDS and streak >= k:
        cur.execute(
            """INSERT INTO core_si_trusted_fixes (signal_key, fix_action, approvals_at_admission)
               VALUES (%s,%s,%s)
               ON CONFLICT (signal_key, fix_action, org_id) DO NOTHING
               RETURNING id""",
            (signal_key, fix_action, streak),
        )
        admitted = cur.fetchone() is not None
    conn.commit()
    return {"streak": streak, "k": k, "admitted": admitted}


def streak_of(conn, signal_key: str, fix_action: str) -> int:
    """Read-only current approval streak (for the visibility counter; records nothing)."""
    cur = conn.cursor()
    return _consecutive_approvals(cur, signal_key, fix_action)


def is_trusted(conn, signal_key: str, fix_action: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        """SELECT 1 FROM core_si_trusted_fixes
           WHERE signal_key=%s AND fix_action=%s AND active=true LIMIT 1""",
        (signal_key, fix_action),
    )
    return cur.fetchone() is not None


def list_trusted(conn) -> list:
    cur = conn.cursor()
    cur.execute(
        """SELECT signal_key, fix_action, approvals_at_admission, admitted_at
           FROM core_si_trusted_fixes WHERE active=true ORDER BY admitted_at DESC"""
    )
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", nargs=2, metavar=("SIGNAL", "FIX"))
    ap.add_argument("--reject", action="store_true", help="with --record: log a reject (resets the streak)")
    ap.add_argument("--check", nargs=2, metavar=("SIGNAL", "FIX"))
    ap.add_argument("--streak", nargs=2, metavar=("SIGNAL", "FIX"),
                    help="print current approval streak (read-only; for the x/K counter)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-k", type=int, default=K_DEFAULT, help=f"approvals to admit (default {K_DEFAULT})")
    args = ap.parse_args()
    if not (args.record or args.check or args.streak or args.list):
        ap.error("specify --record, --check, --streak, or --list")
    conn = connect_corebrain()  # brain_app; RLS-scoped to CORE_ORG_ID
    try:
        if args.record:
            sig, fix = args.record
            r = record(conn, sig, fix, "reject" if args.reject else "approve", args.k)
            verb = "REJECT logged" if args.reject else "APPROVE logged"
            print(f"[{verb}] {sig!r}: streak={r['streak']}/{r['k']}"
                  + ("  -> ADMITTED to trusted-fix set" if r["admitted"] else ""))
        if args.check:
            sig, fix = args.check
            print(f"trusted={is_trusted(conn, sig, fix)}  ({sig!r})")
        if args.streak:
            sig, fix = args.streak
            print(streak_of(conn, sig, fix))
        if args.list:
            rows = list_trusted(conn)
            if not rows:
                print("trusted-fix set: EMPTY (no fix has reached the approval threshold yet)")
            for sig, fix, n, when in rows:
                print(f"  · {sig}  (admitted after {n} approvals, {when:%Y-%m-%d})  {fix[:60]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
