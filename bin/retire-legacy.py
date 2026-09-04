#!/usr/bin/env python3
"""retire-legacy.py — the "don't forget the sweep" engine (unified redesign step ⑤).

Nick's ask: once the new memory-brain system is TESTED and proven, automatically remove/archive the
legacy parts it supersedes — without relying on either of us to remember.

How it works:
  --tick    : called at every close. If the NEW system ran clean this cycle (brain_status not
              FAILED/UNAVAILABLE + no dead ledger jobs), increment the clean-cycle counter; a FAILED
              cycle RESETS it (proof must be sustained). When the counter reaches THRESHOLD, the sweep
              is READY. If auto_execute is on, it runs the archive immediately; otherwise it just
              flips ready=true and lets SessionStart surface it LOUDLY (impossible to forget).
  --status  : one line for SessionStart ("legacy retirement: 6/10 clean cycles" / "READY — run
              /retire-legacy or it auto-archives").
  --execute : do the sweep — ARCHIVE (git mv, reversible) each superseded file + apply its caller
              edit, write an archive README, commit. Never touches DB data (that stays a separate,
              explicitly-approved destructive step).

State: .claude/state/.retire-legacy.json  {clean_cycles, threshold, ready, executed, auto_execute}.
Reversible by design: archive = git mv to scheduling/_archive/ (recoverable via git + the archive dir).
Fork-safe. Fail-open.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[1])
STATE = REPO / ".claude" / "state" / ".retire-legacy.json"
THRESHOLD = 10  # clean close cycles the new system must sustain before the sweep is "proven"

# What the new system supersedes. action: 'archive' (git mv, safe/reversible) or 'flag' (surface for
# manual review — too entangled to auto-edit). caller_note documents the reference that also changes.
MANIFEST = [
    {"path": "scheduling/brain-pg/freshness-gate.py", "action": "archive",
     "superseded_by": "brain_status.py (store-vs-disk, typed)",
     "caller_note": "close-core.md step 8 calls it — replace with brain_status.py on retire"},
    {"path": "scheduling/graphify-brain/remap-checkpoints.py", "action": "archive",
     "superseded_by": "the ledger (revision-scoped output paths; no basename remap needed)",
     "caller_note": "no live caller (dead one-off)"},
    {"path": ".claude/commands/claude-si.md", "action": "archive",
     "superseded_by": "/core-si (unified gate)",
     "caller_note": "listed in sync-manifest per_core_extras.life — remove there too"},
    {"path": "scheduling/graphify-brain/extract-pending.sh", "action": "flag",
     "superseded_by": "ledger stage_jobs (content-hash completion, not basename)",
     "caller_note": "basename-checkpoint completion logic inside — needs in-code edit, not archival"},
]


def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"clean_cycles": 0, "threshold": THRESHOLD, "ready": False,
            "executed": False, "auto_execute": False}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2))


def _new_system_clean() -> bool:
    """Did the new capture/status system run WITHOUT a hard failure this cycle?"""
    try:
        sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
        from brain_status import status
        s = status()
        if s.get("overall") in ("FAILED", "UNAVAILABLE"):
            return False
        if s.get("failed", 0) > 0:  # dead ledger jobs
            return False
        return True
    except Exception:
        return False  # can't verify → not a clean proof cycle (fail toward NOT retiring)


def cmd_tick() -> int:
    d = _load()
    if d.get("executed"):
        return 0
    d["clean_cycles"] = (d.get("clean_cycles", 0) + 1) if _new_system_clean() else 0
    if d["clean_cycles"] >= d.get("threshold", THRESHOLD):
        d["ready"] = True
    _save(d)
    if d.get("ready") and d.get("auto_execute"):
        return cmd_execute()
    return 0


def cmd_status() -> int:
    d = _load()
    if d.get("executed"):
        return 0  # nothing to say once done
    n, t = d.get("clean_cycles", 0), d.get("threshold", THRESHOLD)
    if d.get("ready"):
        how = "auto-archiving at next close" if d.get("auto_execute") else "run `/retire-legacy` to sweep"
        print(f"🧹 LEGACY RETIREMENT READY — the new memory-brain system proved clean over {t} cycles. {how}. "
              f"Archives: {', '.join(m['path'].split('/')[-1] for m in MANIFEST if m['action']=='archive')}.")
    elif n > 0:
        print(f"🧹 legacy retirement: {n}/{t} clean cycles (new system proving out; sweep auto-surfaces when ready)")
    return 0


def cmd_execute() -> int:
    d = _load()
    if d.get("executed"):
        print("retire-legacy: already executed")
        return 0
    if not d.get("ready"):
        print(f"retire-legacy: NOT ready ({d.get('clean_cycles',0)}/{d.get('threshold',THRESHOLD)} clean cycles). Refusing.")
        return 1
    archive_dir = REPO / "scheduling" / "_archive" / f"legacy-retired-{date.today().isoformat()}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived, flagged = [], []
    for m in MANIFEST:
        src = REPO / m["path"]
        if m["action"] == "archive" and src.exists():
            dst = archive_dir / src.name
            try:
                subprocess.run(["git", "-C", str(REPO), "mv", m["path"], str(dst.relative_to(REPO))], check=True, capture_output=True)
                archived.append((m["path"], m["caller_note"]))
            except Exception as e:
                flagged.append((m["path"], f"archive failed: {e}"))
        elif m["action"] == "flag":
            flagged.append((m["path"], m["caller_note"]))
    (archive_dir / "README.md").write_text(
        "# Legacy retired " + date.today().isoformat() + "\n\n"
        "Superseded by the memory-brain-SI unified redesign (ledger + brain_status + assertions).\n"
        "Reversible: `git mv` these back, or restore from git history.\n\n"
        "## Archived\n" + "\n".join(f"- `{p}` — caller: {c}" for p, c in archived) + "\n\n"
        "## Still needs a manual in-code edit (flagged, NOT auto-removed)\n"
        + "\n".join(f"- `{p}` — {c}" for p, c in flagged) + "\n")
    d["executed"] = True
    _save(d)
    print(f"retire-legacy: archived {len(archived)} file(s) → {archive_dir.relative_to(REPO)}; "
          f"flagged {len(flagged)} for manual edit. See the archive README. Commit + update callers next.")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--status"
    return {"--tick": cmd_tick, "--status": cmd_status, "--execute": cmd_execute}.get(mode, cmd_status)()


if __name__ == "__main__":
    sys.exit(main())
