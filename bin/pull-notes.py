#!/usr/bin/env python3
"""Read the pull notes this Core has not seen, run the declared actions, surface the rest.

The operator's requirement, 2026-07-30: a Core should always know why it's pulling and what to do
with it, so it can act without approval unless the change touches something Sentinel-gated.

THE GAP THIS CLOSES. A pull already applies files, runs reconcile-hooks --apply, and runs new
brain-pg migrations — all idempotent and unattended. None of it carries INTENT. A Core could pull a
change needing a one-time adoption, apply every byte correctly, and sit in a state that looks broken
with nothing having told it what to expect. Moving bytes is not the same as knowing what they mean.

WHY A CLOSED VOCABULARY, NOT COMMANDS. docs/PULL-NOTES.md arrives over the network from the shared
baseline. The rule that governs bin/hook-registry.json governs it: a data file from the network gets
no benefit of the doubt. So a note names an ACTION and this file maps that name to a LOCAL
implementation. A hostile note can request `run-migrations`; it cannot supply what that means, and
an unrecognised name is reported rather than executed. That is the same shape as _EVENTS in
reconcile-hooks and it is deliberate.

WHY `Needs Operator` NEVER BLOCKS. Every action in the vocabulary is local, reversible and idempotent,
so it runs unattended — that is the point of the whole design. Trust-root changes are the one
exception, and not because applying one is risky: because APPROVING one is a decision that must be
un-forgeable. sentinel-approve.sh: "THERE IS NO SELF-SERVICE PATH FOR A TRUST-ROOT CHANGE. THIS IS
DELIBERATE." No agent message is the operator's consent, including this Core's own claim that they gave it.
So the line is surfaced, the pull proceeds, and nothing waits on them.

  python3 bin/pull-notes.py              # what is unread, and what would run
  python3 bin/pull-notes.py --apply      # run the declared actions, mark read
  python3 bin/pull-notes.py --brief      # one-line summary for SessionStart
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[1])
NOTES = ROOT / "docs" / "PULL-NOTES.md"
SEEN = ROOT / ".claude" / "state" / ".pull-notes-seen.json"

ENTRY = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*·\s*(\S+)\s*$")
FIELD = re.compile(r"^\*\*(What changed|Actions|Needs Operator|Heads-up):\*\*\s*(.*)$", re.S)


def _local_actions() -> dict:
    """Action name -> (argv, human description). LOCAL implementations only.

    Nothing here is derived from the note. The note may only choose among these.
    """
    return {
        "run-migrations": ([str(ROOT / "bin" / "run-migrations.sh")],
                           "apply any new brain-pg migration (idempotent)"),
        "reconcile-hooks": (["python3", str(ROOT / "bin" / "reconcile-hooks.py"),
                             "--core", str(ROOT), "--apply"],
                            "re-derive settings.json from the registry"),
        "adopt-si-projection": (["python3", str(ROOT / "bin" / "si-adopt-projection.py")],
                                "one-time: adopt existing artifacts into the unified spine"),
        # Surfaced, not executed: both need a live session to spend a subagent or a human's own
        # judgement about which entities are genuinely stale.
        "extract-asks": (None, "run ask extraction so the generator has distilled asks to mint from"),
        "retire-stale-entities": (None, "retire this Core's own stale trust-root entities (RLS "
                                        "means no other Core can)"),
        "none": (None, "nothing to do"),
    }


def parse_notes(text: str) -> list[dict]:
    out, cur, field = [], None, None
    for line in text.splitlines():
        m = ENTRY.match(line.strip())
        if m:
            if cur:
                out.append(cur)
            cur = {"date": m.group(1), "sha": m.group(2), "what": "", "actions": "",
                   "needs_operator": "", "heads_up": ""}
            field = None
            continue
        if cur is None:
            continue
        f = FIELD.match(line.strip())
        if f:
            field = {"What changed": "what", "Actions": "actions",
                     "Needs Operator": "needs_operator", "Heads-up": "heads_up"}[f.group(1)]
            cur[field] = f.group(2).strip()
        elif field and line.strip():
            cur[field] += " " + line.strip()
    if cur:
        out.append(cur)
    return out


def _seen() -> set:
    try:
        return set(json.loads(SEEN.read_text()).get("shas", []))
    except Exception:
        return set()


def unread() -> list[dict]:
    if not NOTES.exists():
        return []
    seen = _seen()
    return [n for n in parse_notes(NOTES.read_text()) if n["sha"] not in seen]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--brief", action="store_true")
    a = ap.parse_args()

    notes = unread()
    if a.brief:
        if notes:
            needs_op = [n for n in notes if n["needs_operator"] and n["needs_operator"].lower() != "none"]
            print(f"📥 {len(notes)} unread baseline pull note(s)"
                  + (f" · {len(needs_op)} needs the operator's own hands" if needs_op else "")
                  + " — run `python3 bin/pull-notes.py --apply`")
        return 0

    if not notes:
        print("  no unread pull notes — this Core is current with the baseline's intent.")
        return 0

    impl = _local_actions()
    ran, surfaced, unknown = [], [], []

    for n in notes:
        print(f"\n=== {n['date']} · {n['sha']} ===")
        if n["what"]:
            print(f"  {n['what'][:400]}")
        if n["heads_up"] and n["heads_up"].lower() != "none":
            print(f"\n  HEADS-UP: {n['heads_up'][:500]}")
        if n["needs_operator"] and n["needs_operator"].lower() != "none":
            print(f"\n  ⚠ NEEDS THE OPERATOR (surfaced, does NOT block this pull):\n    {n['needs_operator'][:500]}")

        names = [x.strip() for x in (n["actions"] or "").split(",") if x.strip()]
        for name in names:
            if name not in impl:
                unknown.append((n["sha"], name))
                continue
            argv, desc = impl[name]
            if argv is None:
                surfaced.append((name, desc))
                continue
            if not a.apply:
                ran.append((name, desc, "would run"))
                continue
            exe = Path(argv[0] if argv[0] != "python3" else argv[1])
            if not exe.exists():
                ran.append((name, desc, "skipped — not present on this Core"))
                continue
            try:
                r = subprocess.run(argv if argv[0] != "run-migrations.sh" else ["bash"] + argv,
                                   capture_output=True, text=True, timeout=180)
                ran.append((name, desc, "ok" if r.returncode == 0 else f"exit {r.returncode}"))
            except Exception as e:
                ran.append((name, desc, f"error: {type(e).__name__}"))

    if ran:
        print("\n  ACTIONS:")
        for name, desc, status in ran:
            print(f"    {name:22} {status:12} {desc}")
    if surfaced:
        print("\n  FOR THIS SESSION TO DO (needs a live session, not a script):")
        for name, desc in surfaced:
            print(f"    {name:22} {desc}")
    if unknown:
        # Reported, never executed. An unrecognised name means the note is newer than this Core's
        # vocabulary — pull the code, do not invent the action.
        print("\n  UNKNOWN ACTION NAMES (reported, NOT executed — update this Core, or the note is wrong):")
        for sha, name in unknown:
            print(f"    {sha}  {name!r}")

    if a.apply:
        try:
            SEEN.parent.mkdir(parents=True, exist_ok=True)
            SEEN.write_text(json.dumps({"shas": sorted(_seen() | {n["sha"] for n in notes})}, indent=2))
            print(f"\n  marked {len(notes)} note(s) read.")
        except Exception as e:
            print(f"\n  could not mark read: {e}")
    else:
        print("\n  DRY RUN — pass --apply to run the actions and mark read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
