#!/usr/bin/env python3
"""verify-close-sequence.py — does the close CODE still match the declared sequence?

WHY. Before 2026-08-28 the close was 27 invocations across 732 lines of shell with no stated
order, no phases, and no way to tell which steps the NEXT session depends on. Six reversals in
that file's 58-commit history came from moving something without a shared picture of what the
close is. `.claude/close-sequence.json` is that picture. This asserts the code has not drifted
from it — because a spec nothing checks is exactly the class of defect this repo keeps finding
(compile-truth unwired 3 months, corroborate frozen 52 days, artifact_utility 0 rows).

Detects three drifts:
  MISSING   declared in the manifest, not invoked in lifecycle_close  -> the step silently stopped running
  UNDECLARED invoked in the code, absent from the manifest            -> a step was added without a phase
  ORDER      declared order does not match invocation order           -> a step moved; phases exist to make that visible

Also reports the SYNC/DETACHED mismatch, which is the one that has actually bitten: a step
declared sync but detached (or the reverse) is how a 6-minute pg_dump ended up inside a
60-second Stop hook and produced 128 GB of orphaned fragments.

Exit 0 clean, 1 on drift.  Usage: python3 bin/verify-close-sequence.py [--json]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parent.parent))
MANIFEST = REPO / ".claude" / "close-sequence.json"
LIFECYCLE = REPO / ".claude" / "hooks" / "session-lifecycle.sh"


def code_sequence():
    """Invocations inside lifecycle_close, in order, with sync/detached."""
    src = LIFECYCLE.read_text(errors="ignore").splitlines()
    try:
        start = next(i for i, l in enumerate(src) if l.startswith("lifecycle_close"))
    except StopIteration:
        return []
    end = next((i for i, l in enumerate(src[start + 1:], start + 1) if l == "}"), len(src))
    pat = re.compile(r"([A-Za-z0-9_.-]+\.(?:py|sh))")
    out, seen = [], set()
    for i, line in enumerate(src[start:end], start + 1):
        s = line.strip()
        if s.startswith("#") or not s:
            continue
        if not re.search(r"(python3|bash|_detach_guarded|nohup)", s):
            continue
        for name in pat.findall(line):
            if name == "session-lifecycle.sh" or name in seen:
                continue
            seen.add(name)
            detached = ("_detach_guarded" in line) or ("nohup" in line)
            out.append({"step": name, "line": i, "sync": not detached})
    return out


def main() -> int:
    as_json = "--json" in sys.argv
    if "--emit" in sys.argv:
        # Emit a starter manifest FROM THIS SEAT'S OWN CLOSE. Phases start as "unphased" — naming
        # them is a judgement call the seat's owner makes; the point is that the order and the
        # sync/detached facts are captured from the code rather than guessed.
        seq = [{"n": i + 1, "phase": "unphased", "step": e["step"], "sync": e["sync"],
                "next_session_needs": None} for i, e in enumerate(code_sequence())]
        print(json.dumps({"_comment": "Ordered close sequence for this seat, emitted from the live "
                                      "code by bin/verify-close-sequence.py --emit. Assign phases "
                                      "and next_session_needs by hand.",
                          "sequence": seq, "phase_notes": {}, "missing": []}, indent=2))
        return 0
    if not MANIFEST.exists():
        # NOT a failure on a peer. This script is SHARED (bin/ syncs fleet-wide) but
        # .claude/close-sequence.json is per-Core — each seat's close differs (school runs no
        # close-reconciler, for one), so a single shared manifest would assert the wrong sequence
        # on four seats. A shared script that errors on every peer is how a fleet learns to ignore
        # its own output, which is the failure this whole class of instrument exists to prevent.
        if as_json:
            print(json.dumps({"status": "no-manifest", "declared": 0, "invoked": 0,
                              "order_matches": True, "missing": [], "undeclared": [],
                              "sync_drift": [], "declared_missing_from_close": []}, indent=1))
        else:
            print("═══ CLOSE SEQUENCE ═══")
            print(f"  no manifest on this seat ({MANIFEST.name}) — declare one to enable drift checks")
            print("  generate with: python3 bin/verify-close-sequence.py --emit > .claude/close-sequence.json")
        return 0
    man = json.loads(MANIFEST.read_text())

    declared, decl_meta = [], {}
    for e in man["sequence"]:
        declared.append(e["step"])
        decl_meta[e["step"]] = {"phase": e["phase"], "sync": e["sync"],
                                "next_session_needs": e["next_session_needs"]}

    actual = code_sequence()
    actual_names = [a["step"] for a in actual]
    actual_meta = {a["step"]: a for a in actual}

    missing = [s for s in declared if s not in actual_names]
    undeclared = [s for s in actual_names if s not in decl_meta]

    common = [s for s in declared if s in actual_names]
    order_ok = common == [s for s in actual_names if s in decl_meta]
    sync_drift = [
        {"step": s, "declared": "sync" if decl_meta[s]["sync"] else "detached",
         "actual": "sync" if actual_meta[s]["sync"] else "detached"}
        for s in common if decl_meta[s]["sync"] != actual_meta[s]["sync"]
    ]

    res = {"declared": len(declared), "invoked": len(actual),
           "missing": missing, "undeclared": undeclared,
           "order_matches": order_ok, "sync_drift": sync_drift,
           "declared_missing_from_close": [m["step"] for m in man.get("missing", [])]}
    drift = bool(missing or undeclared or sync_drift or not order_ok)

    if as_json:
        print(json.dumps(res, indent=1))
        return 1 if drift else 0

    print("═══ CLOSE SEQUENCE ═══")
    print(f"  declared {len(declared)} · invoked {len(actual)} · order {'OK' if order_ok else 'DRIFTED'}")
    last = None
    for e in man["sequence"]:
        flag = "→next" if e["next_session_needs"] else "     "
        mode = "sync" if e["sync"] else "detach"
        ph = e["phase"] if e["phase"] != last else ""
        last = e["phase"]
        print(f"    {e['n']:>2}. {flag} {ph:<11} {mode:<7} {e['step']}")
    if missing:
        print("\n  ✗ DECLARED BUT NOT INVOKED (a step silently stopped running):")
        for s in missing:
            print(f"      {s}   [{decl_meta[s]['phase']}]")
    if undeclared:
        print("\n  ✗ INVOKED BUT NOT DECLARED (added without a phase):")
        for s in undeclared:
            print(f"      {s}   L{actual_meta[s]['line']}")
    if sync_drift:
        print("\n  ✗ SYNC/DETACHED MISMATCH (this is the one that caused the 128 GB incident):")
        for d in sync_drift:
            print(f"      {d['step']}: declared {d['declared']}, actually {d['actual']}")
    if not order_ok:
        print("\n  ✗ ORDER DRIFT — a step moved relative to the manifest")
    for r in man.get("proposed_reorder", []):
        print(f"\n  ⚠ PROPOSED (not applied): {r['proposal']}")
    for m in man.get("missing", []):
        print(f"\n  ⚠ DECLARED MISSING: {m['step']}")
        print(f"      belongs in '{m['belongs_in_phase']}' · {m['status']}")
    if not drift:
        print("\n  ✅ code matches the declared sequence")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
