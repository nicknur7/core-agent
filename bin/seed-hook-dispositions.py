#!/usr/bin/env python3
"""seed-hook-dispositions.py — create/refresh a Core's hook-disposition matrix (L3).

The hook-health dashboard reads .claude/state/hook-dispositions.json. On life this
file is hand-curated; on the peers it never existed, so their per-Core system views
had nothing to render. This seeds a matrix for ANY Core from two universal inputs:

  L1  bin/hook-curation-base.json  — intrinsic per-hook curation (event, priority,
                                     telemetry_class, plain, origin), identical on
                                     every Core because it describes what the hook IS.
  L2  <core>/.claude/settings.json — the hooks THIS Core actually registers.

  L3  <core>/.claude/state/hook-dispositions.json  = the composition:
        for every hook this Core registers, an entry seeded from the base curation
        with disposition="keep" (never inherit life's telemetry-driven tuning) and an
        empty live block that refresh-hook-dispositions.py fills from hook-events.log.

Idempotent + additive + non-destructive:
  - An existing matrix is PRESERVED. Only hooks this Core registers that are MISSING
    from the matrix get added. Curated entries (disposition/rationale/action) are never
    overwritten. Entries for hooks no longer registered are flagged `registered:false`
    (kept, not deleted — history + re-enable).
  - Safe to run every close. Pair with refresh-hook-dispositions.py to stamp live data.

Usage:
  CORE_INSTANCE=$(git rev-parse --show-toplevel) python3 bin/seed-hook-dispositions.py [--core PATH] [--check]
    --check   report what WOULD change, write nothing (exit 1 if drift, 0 if clean)
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path

DISPO_DEFAULT_COMMENT = (
    "Per-Core hook-disposition matrix (L3, derived). Seeded by bin/seed-hook-dispositions.py "
    "from bin/hook-curation-base.json (L1) x this Core's registered hooks (L2). disposition/"
    "rationale/action are curated locally; live is stamped by bin/refresh-hook-dispositions.py."
)


def hook_name(cmd: str | None) -> str | None:
    m = re.search(r"([A-Za-z0-9_-]+)\.(sh|py)", cmd or "")
    return m.group(1) if m else None


def registered_hooks(settings: dict) -> dict[str, str]:
    """{hook_name: event} for every hook registered in this Core's settings.json."""
    found: dict[str, str] = {}
    for event, blocks in (settings.get("hooks") or {}).items():
        for block in blocks:
            for h in block.get("hooks", []):
                nm = hook_name(h.get("command", ""))
                if nm:
                    found.setdefault(nm, event)
    return found


def seed_entry(name: str, event: str, base: dict) -> dict:
    b = base.get(name, {})
    return {
        "event": b.get("event") or event,
        "disposition": "keep",
        "priority": b.get("priority", "medium"),
        "rationale": (
            "Seeded from the universal base; re-curate against this Core's own telemetry."
            if name in base
            else "Seeded with defaults — no base curation found for this hook; curate locally."
        ),
        "action": "",
        "origin": b.get("origin", {}),
        "telemetry_class": b.get("telemetry_class", "signal"),
        "plain": b.get("plain", ""),
        "seeded": True,
        "registered": True,
        "live": {},
    }


def disposition_counts(hooks: dict) -> dict:
    counts = {"keep": 0, "tune": 0, "fix": 0, "merge": 0, "remove": 0}
    for e in hooks.values():
        if isinstance(e, dict):
            d = e.get("disposition")
            if d in counts:
                counts[d] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=os.environ.get("CORE_INSTANCE") or os.getcwd())
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    core = Path(a.core)
    script_dir = Path(__file__).resolve().parent
    base_path = script_dir / "hook-curation-base.json"
    settings_path = core / ".claude" / "settings.json"
    dispo_path = core / ".claude" / "state" / "hook-dispositions.json"

    if not settings_path.exists():
        print(f"ERROR: no settings.json at {settings_path}", file=sys.stderr)
        return 2
    if not base_path.exists():
        print(f"ERROR: no base curation at {base_path}", file=sys.stderr)
        return 2

    base = json.loads(base_path.read_text()).get("hooks", {})
    settings = json.loads(settings_path.read_text())
    reg = registered_hooks(settings)

    created = not dispo_path.exists()
    if created:
        matrix = {"_comment": DISPO_DEFAULT_COMMENT, "hooks": {}, "summary": {}}
    else:
        matrix = json.loads(dispo_path.read_text())
    hooks = matrix.setdefault("hooks", {})

    added, reflagged = [], []
    for name, event in sorted(reg.items()):
        if name not in hooks:
            hooks[name] = seed_entry(name, event, base)
            added.append(name)
        elif hooks[name].get("registered") is False:
            hooks[name]["registered"] = True
            reflagged.append(name)
    # Flag matrix entries for hooks this Core no longer registers.
    deregistered = []
    for name, e in hooks.items():
        if isinstance(e, dict) and name not in reg and e.get("registered") is not False:
            e["registered"] = False
            deregistered.append(name)

    # Summary: keep/tune/fix/merge/remove are CURATED — only compute them for a matrix we
    # just created (peers had none). For an existing curated matrix, never overwrite those
    # counts; only keep `total` current. Live totals are owned by refresh-hook-dispositions.py.
    summary = matrix.get("summary", {})
    if created:
        summary.update(disposition_counts(hooks))
    summary["total"] = sum(1 for e in hooks.values() if isinstance(e, dict))
    matrix["summary"] = summary

    core_label = core.name
    if a.check:
        drift = bool(added or reflagged or deregistered)
        print(f"[seed --check] core={core_label} registered={len(reg)} matrix={len(hooks)}")
        if added:
            print(f"  WOULD ADD ({len(added)}): {', '.join(added)}")
        if reflagged:
            print(f"  WOULD RE-FLAG registered ({len(reflagged)}): {', '.join(reflagged)}")
        if deregistered:
            print(f"  WOULD FLAG deregistered ({len(deregistered)}): {', '.join(deregistered)}")
        if not drift:
            print("  ✓ in sync — no changes")
        return 1 if drift else 0

    dispo_path.parent.mkdir(parents=True, exist_ok=True)
    dispo_path.write_text(json.dumps(matrix, indent=2, ensure_ascii=False) + "\n")
    print(f"[seed] core={core_label}: +{len(added)} added, {len(reflagged)} re-flagged, "
          f"{len(deregistered)} deregistered · matrix now {len(hooks)} hooks")
    if added:
        print(f"  added: {', '.join(added)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
