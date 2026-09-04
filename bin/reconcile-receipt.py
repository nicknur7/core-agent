#!/usr/bin/env python3
"""reconcile-receipt.py — the reconcile enforcement receipt (Codex-specified 2026-07-17).

Two states are required before a full close may commit:
  1. RAN        — close-reconciler actually terminated (authenticated by the SubagentStop
                  hook writing .reconcile-ran; the model cannot forge this).
  2. DISPOSITIONED — the parent handled the reconciler's findings and wrote a receipt.

Modes:
  write   — called by the model AFTER it dispositions the reconciler's findings. REQUIRES
            .reconcile-ran to exist (else refuses — "spawn close-reconciler first"). Captures
            the start→close changeset (reconcile-inventory diff) + report digest, writes
            .reconcile-receipt.json. Exit 0 on success, 2 if RAN evidence is missing.
  check   — called by the close controller. Exit 0 = OK to commit a full close (a valid
            receipt exists, OR the in-scope changeset is empty → NO_RECONCILIATION_NEEDED).
            Exit 1 = NOT ok (in-scope files changed but no dispositioned receipt).
  pending — called by the defensive (walk-away) path. Writes .reconcile-pending with the
            unreconciled changeset so the NEXT session catches it. Exit 0.

Fail-open on unexpected errors (never hard-crash a close): unknown error → exit 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

INSTANCE = Path(
    os.environ.get("CORE_INSTANCE")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parents[1]
)
STATE = INSTANCE / ".claude" / "state"
RAN = STATE / ".reconcile-ran"
REPORT = STATE / ".reconcile-report"
RECEIPT = STATE / ".reconcile-receipt.json"
PENDING = STATE / ".reconcile-pending.json"
INVENTORY = INSTANCE / "bin" / "reconcile-inventory.py"


def _changeset() -> dict:
    """The start→close in-scope changeset via the content inventory (not git diff).
    On ANY failure returns {"error": True} — NOT an empty changeset — so the enforcement
    decision can fail CLOSED instead of masquerading a broken inventory as 'no changes'."""
    try:
        r = subprocess.run(
            ["python3", str(INVENTORY), "diff"],
            capture_output=True, text=True, timeout=20,
            env={**os.environ, "CORE_INSTANCE": str(INSTANCE)},
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {"error": True, "total": 0}
        cs = json.loads(r.stdout)
        if "total" not in cs or "state_fp" not in cs:
            return {"error": True, "total": 0}
        # A MISSING baseline means we cannot know what changed this session — treat it as an
        # inventory ERROR (fail-closed), NOT an empty baseline. Otherwise "no baseline + empty
        # current scope" would read as total:0 → clean close (Codex round 7).
        if not cs.get("baseline_present"):
            return {"error": True, "total": 0, "reason": "no-baseline"}
        return cs
    except Exception:
        return {"error": True, "total": 0}


def _receipt_valid(cs: dict) -> bool:
    """A receipt is valid for the CURRENT state iff it was dispositioned, has run-evidence,
    and its state_fp matches the current scope fingerprint (nothing changed since)."""
    if cs.get("error") or not RECEIPT.exists() or not RAN.exists():
        return False
    try:
        r = json.loads(RECEIPT.read_text())
    except Exception:
        return False
    return bool(r.get("disposition") == "dispositioned"
                and r.get("state_fp")
                and r.get("state_fp") == cs.get("state_fp"))


def _digest(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:16]


def cmd_write() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    if not RAN.exists():
        sys.stderr.write(
            "reconcile-receipt: REFUSED — no .reconcile-ran evidence. The close-reconciler "
            "subagent has not run this session. Spawn it (Task → close-reconciler), disposition "
            "its findings, THEN write the receipt. (A receipt without the reconciler having run "
            "is exactly the silent-skip this gate exists to stop.)\n"
        )
        return 2
    cs = _changeset()
    if cs.get("error"):
        sys.stderr.write("reconcile-receipt: REFUSED — inventory could not be computed; cannot bind a "
                         "receipt to an unknown state. Fix the inventory, then retry.\n")
        return 2
    report = REPORT.read_text(errors="ignore") if REPORT.exists() else ""
    # Audit-bind any carried pending being dispositioned now (D — clear-without-binding).
    carried = None
    if PENDING.exists():
        try:
            carried = json.loads(PENDING.read_text()).get("changeset")
        except Exception:
            carried = "unparseable"
    receipt = {
        "ran_at": RAN.read_text(errors="ignore").strip(),
        "changeset": cs,
        # state_fp BINDS the receipt to the exact scope state it dispositioned — check
        # compares the current state_fp, so any post-receipt edit invalidates it.
        "state_fp": cs.get("state_fp", ""),
        "carried_pending_dispositioned": carried,
        "report_digest": _digest(report),
        "report_len": len(report),
        "disposition": "dispositioned",
        "schema": 2,
    }
    RECEIPT.write_text(json.dumps(receipt, indent=2))
    PENDING.unlink(missing_ok=True)  # dispositioned (recorded above); clear the carry
    print(f"reconcile-receipt: written — {cs['total']} in-scope change(s) reconciled + dispositioned"
          + (f" (+carried pending recorded)" if carried else ""))
    return 0


def cmd_check() -> int:
    cs = _changeset()
    # FAIL CLOSED: if the inventory couldn't be computed, we cannot certify a clean close.
    if cs.get("error"):
        sys.stderr.write("reconcile-check: NOT RECONCILED — inventory could not be computed (fail-closed).\n")
        return 1
    valid = _receipt_valid(cs)
    # A prior session's unreconciled work (.reconcile-pending) must be reconciled before this
    # close can be clean — even if this session itself changed nothing. A VALID current receipt
    # (which write() produces only after clearing pending) satisfies this.
    if PENDING.exists() and not valid:
        try:
            pc = json.loads(PENDING.read_text()).get("changeset", {})
            n = pc.get("total", "?")
        except Exception:
            n = "?"
        sys.stderr.write(
            f"reconcile-check: NOT RECONCILED — a PRIOR session left {n} file(s) unreconciled "
            "(.reconcile-pending). Reconcile it (spawn close-reconciler, disposition, write receipt) first.\n"
        )
        return 1
    if cs.get("total", 0) == 0:
        print("reconcile-check: OK (NO_RECONCILIATION_NEEDED — no in-scope changes this session)")
        return 0
    if valid:
        print(f"reconcile-check: OK (dispositioned receipt bound to current state; {cs['total']} change(s))")
        return 0
    sys.stderr.write(
        f"reconcile-check: NOT RECONCILED — {cs['total']} in-scope file(s) changed this session "
        "with no valid dispositioned receipt bound to the current state (run the reconciler, disposition, "
        "then reconcile-receipt.py write).\n"
    )
    return 1


def cmd_pending() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    cs = _changeset()
    # Inventory error: we can't compute the delta. Preserve any existing pending; if none,
    # mark that reconciliation is owed so the next session still stops. Never silently drop.
    if cs.get("error"):
        if not PENDING.exists():
            PENDING.write_text(json.dumps({"changeset": {"total": 1, "added": [], "modified": ["<inventory-error>"], "deleted": []}, "carried": True, "error": True, "schema": 2}))
        return 0
    # A VALID receipt bound to the current state means this session's changes ARE reconciled —
    # nothing to carry. A STALE/absent receipt does NOT suppress the carry (the post-receipt-edit
    # fix): fall through and carry the delta.
    if _receipt_valid(cs) or cs.get("total", 0) == 0:
        return 0
    # MERGE with any existing pending (a prior session's carried delta) — never overwrite.
    merged = {"added": set(cs["added"]), "modified": set(cs["modified"]), "deleted": set(cs["deleted"])}
    if PENDING.exists():
        try:
            old = json.loads(PENDING.read_text()).get("changeset", {})
            for k in ("added", "modified", "deleted"):
                merged[k] |= set(old.get(k, []))
        except Exception:
            # Malformed prior pending: DON'T silently overwrite (would lose the carry) —
            # quarantine it so it's recoverable, and record that we couldn't merge it.
            try:
                PENDING.rename(STATE / ".reconcile-pending.corrupt")
                merged["modified"].add("<unmergeable-prior-pending-quarantined>")
            except Exception:
                pass
    out = {k: sorted(v) for k, v in merged.items()}
    out["total"] = len(out["added"]) + len(out["modified"]) + len(out["deleted"])
    PENDING.write_text(json.dumps({"changeset": out, "carried": True, "schema": 2}, indent=2))
    print(f"reconcile-receipt: pending written (merged) — {out['total']} unreconciled change(s) carried")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "write":
        return cmd_write()
    if mode == "check":
        return cmd_check()
    if mode == "pending":
        return cmd_pending()
    sys.stderr.write("usage: reconcile-receipt.py {write|check|pending}\n")
    return 0


if __name__ == "__main__":
    _mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"reconcile-receipt: {e}\n")
        # FAIL-CLOSED for check (exit 1 → not reconciled). For pending, GUARANTEE a marker is
        # left even if the command threw internally, so the reconciliation obligation is never
        # silently lost (Codex round 6). write fails-open (never brick the close).
        if _mode == "pending":
            try:
                if not PENDING.exists():
                    PENDING.write_text(json.dumps({"changeset": {"total": 1, "added": [], "modified": ["<pending-error>"], "deleted": []}, "carried": True, "error": True, "schema": 2}))
            except Exception:
                pass
        sys.exit(1 if _mode == "check" else 0)
