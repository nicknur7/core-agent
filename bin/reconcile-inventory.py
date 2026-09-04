#!/usr/bin/env python3
"""reconcile-inventory.py — the reliable changeset signal for close-reconciliation.

Codex-specified (2026-07-17): `git diff` is NOT the authority — it misses committed,
untracked, ignored, deleted, and externally-written files. Instead we take a CONTENT
INVENTORY of the reconcile SCOPE at genuine SessionStart and again at close, and diff
the two inventories. This correctly covers ignored / per_core_keep files because they
are enumerated by scope, not by git.

Modes:
  capture   — hash every in-scope file → write baseline JSON (at SessionStart).
  diff      — re-inventory the same scope, compare to the baseline, print the changeset
              (added / modified / deleted) as JSON. Exit 0 always (read-only, fail-open).

Scope (Phase 1 = universal only; Phase 2 will union identity.json `reconcile_scope`):
  the files close-reconciler is meant to keep current — current-state, project +
  relationship hubs, and the keeper-orphans.

Fail-open: any error → best-effort empty result, never blocks a close.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core_paths  # noqa: E402  path registry — single source of truth for scope-file locations

INSTANCE = Path(
    os.environ.get("CORE_INSTANCE")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parents[1]
)


def _rel(abs_const: str) -> str:
    """Registry constant (absolute, this-Core) → repo-relative scope string.
    Keeps SCOPE_FILES_UNIVERSAL sourced from core_paths so a registry move
    follows automatically — no hardcoded literal to drift (lint-code-paths)."""
    return os.path.relpath(abs_const, INSTANCE)
STATE_DIR = INSTANCE / ".claude" / "state"
BASELINE = STATE_DIR / ".reconcile-baseline.json"

# Dirs whose *.md are walked recursively (excluding archive/).
SCOPE_DIRS_UNIVERSAL = ["memory/projects", "memory/relationships"]
# Individual keeper files. DELIBERATELY EXCLUDED (Codex 2026-07-17): memory/current-state.md
# and memory/capabilities.md — both are DETERMINISTICALLY mutated by the close controller
# AFTER the model writes its receipt (prune-current-state.py + gen-capabilities.py in
# session-lifecycle.sh close full). Including them in the binding fingerprint would break every
# receipt (state_fp mismatch after the generators run) or, if checked early, let post-check
# generator mutations commit under a clean receipt. They are controller-owned, not model-
# reconciled; the reconciler still reads current-state, it just can't trigger/break the gate.
# Sourced from the path registry (core_paths) so these follow a registry move
# automatically — no drift, nothing for lint-code-paths to flag. backlog.md is
# NOT registry-tracked, so it stays a literal (the lint doesn't track it).
SCOPE_FILES_UNIVERSAL = [
    _rel(core_paths.MEM_PENDING),
    _rel(core_paths.MEM_DECISIONS_LOG),
    _rel(core_paths.TASK_SYSTEM_RUNDOWN),
    _rel(core_paths.TASK_LESSONS),
    "tasks/backlog.md",
]
EXCLUDE_PARTS = {"archive", "brain-lint-reports", "_sources"}


def _identity_scope() -> tuple[list[str], list[str]]:
    """Phase 2 seam: per-Core scope from identity.json `reconcile_scope`
    (additive to the universal set; universal is always included). Declarative
    policy — NOT derived from discovery. Fail-open to universal-only."""
    extra_dirs: list[str] = []
    extra_files: list[str] = []
    try:
        idj = json.loads((INSTANCE / ".claude" / "identity.json").read_text())
        rs = idj.get("reconcile_scope") or {}

        def _safe(rel_in: str) -> str | None:
            p = (INSTANCE / rel_in).resolve()
            if INSTANCE.resolve() == p or INSTANCE.resolve() in p.parents:
                rel = os.path.relpath(p, INSTANCE)
                return rel if not rel.startswith("..") else None
            return None

        for d in rs.get("dirs", []) or []:
            r = _safe(d)
            if r:
                extra_dirs.append(r)
        for f in rs.get("files", []) or []:
            r = _safe(f)
            if r:
                extra_files.append(r)
    except Exception:
        pass
    return extra_dirs, extra_files


def _iter_scope_files() -> list[Path]:
    extra_dirs, extra_files = _identity_scope()
    out: list[Path] = []
    for d in SCOPE_DIRS_UNIVERSAL + extra_dirs:
        root = INSTANCE / d
        if not root.exists():
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in EXCLUDE_PARTS and not x.startswith(".")]
            for fn in files:
                if fn.endswith(".md"):
                    out.append(Path(dirpath) / fn)
    for f in SCOPE_FILES_UNIVERSAL + extra_files:
        p = INSTANCE / f
        if p.exists():
            out.append(p)
    # de-dup, stable order
    seen, uniq = set(), []
    for p in sorted(out):
        rp = os.path.relpath(p, INSTANCE)
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def _hash(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except Exception:
        # An unreadable file must NEVER hash-equal another unreadable file (that would
        # conceal a change). Bind ERR to the path + mtime so it always differs / re-fires.
        try:
            return f"ERR-{p.stat().st_mtime_ns}"
        except Exception:
            return f"ERR-{p}"


def _inventory() -> dict:
    return {os.path.relpath(p, INSTANCE): _hash(p) for p in _iter_scope_files()}


def _fingerprint(inv: dict) -> str:
    """Stable fingerprint of a full inventory — binds a reconcile receipt to the EXACT
    scope state it dispositioned, so a post-receipt edit invalidates it."""
    return hashlib.sha256(json.dumps(inv, sort_keys=True).encode()).hexdigest()[:32]


def _git_head() -> str:
    try:
        import subprocess
        return subprocess.run(
            ["git", "-C", str(INSTANCE), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        return ""


def cmd_capture() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {"head": _git_head(), "inventory": _inventory()}
    BASELINE.write_text(json.dumps(data))
    print(f"reconcile-inventory: captured {len(data['inventory'])} in-scope files")
    return 0


def cmd_diff() -> int:
    now = _inventory()
    try:
        base = json.loads(BASELINE.read_text()).get("inventory", {})
    except Exception:
        base = {}
    added = sorted(f for f in now if f not in base)
    deleted = sorted(f for f in base if f not in now)
    modified = sorted(f for f in now if f in base and now[f] != base[f])
    changeset = {"added": added, "modified": modified, "deleted": deleted,
                 "total": len(added) + len(modified) + len(deleted),
                 "baseline_present": BASELINE.exists(),
                 # state_fp binds a receipt to the EXACT current scope state.
                 "state_fp": _fingerprint(now)}
    print(json.dumps(changeset, indent=2))
    return 0


def cmd_fingerprint() -> int:
    print(_fingerprint(_inventory()))
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "capture":
        return cmd_capture()
    if mode == "diff":
        return cmd_diff()
    if mode == "fingerprint":
        return cmd_fingerprint()
    sys.stderr.write("usage: reconcile-inventory.py {capture|diff|fingerprint}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # fail-open: never block a close
        sys.stderr.write(f"reconcile-inventory: {e}\n")
        sys.exit(0)
