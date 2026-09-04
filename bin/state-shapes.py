#!/usr/bin/env python3
"""Count distinct durable-state SHAPES per Core, across the whole fleet.

WHY THIS IS A FILE AND NOT A ONE-LINER. The 2026-08-08 engineering plan concludes that "the
schema estate is a property of shared code" — and therefore that the typed store can be defined
once in the baseline — from exactly TWO measurements, business's 65 shapes and life's 84, taken
by two ad-hoc scripts written independently. Both scripts had the same bug: they normalised hex
runs with `[0-9a-f]{8,}`, which does not match a UUID, because the hyphens break the run. Life's
count was inflated 2.2x and business's 1.8x before that was caught.

Two hand-rolled measurements, one shared bug, and an architectural decision resting on them. So:
one implementation, run from one place, over all five Cores. n=5 either supports the claim much
more strongly than n=2 or kills it.

A SHAPE is the recursive key structure of a JSON document with values, array lengths, and
identifier-shaped keys erased — two files share a shape when the same code could read both.

    python3 bin/state-shapes.py            # fleet table
    python3 bin/state-shapes.py --json
    python3 bin/state-shapes.py --core life --list   # shapes on one Core, with examples
"""
import json
import os
import re
import sys
from collections import defaultdict

CORES = {
    "life": "core-life", "business": "core-business", "school": "core-school",
    "finance": "core-finance", "ops": "core-ops",
}
BASE = os.path.expanduser("~/AI Projects")

# An identifier-shaped key: UUIDs (hyphens and all), long hex, epochs, dates, sha prefixes.
# The hyphen inside the class is the entire lesson from the 2.2x/1.8x inflation — a UUID is
# 8-4-4-4-12, so a pattern anchored on an unbroken hex run never sees one.
IDENT = re.compile(
    r"^("
    r"[0-9a-fA-F-]{8,}"           # hex or UUID, hyphens permitted
    r"|\d{4}-\d{2}-\d{2}.*"       # dates and timestamps
    r"|\d{9,}"                    # epochs
    r"|[a-f0-9]{7,40}"            # sha prefixes
    r")$"
)


def shape(obj, depth=0):
    if depth > 6:
        return "..."
    if isinstance(obj, dict):
        keys = sorted({("<id>" if IDENT.match(str(k)) else str(k)) for k in obj})
        # A dict keyed entirely by identifiers is a MAP, not a record: its shape is the shape
        # of its values, not its (unbounded, data-dependent) key set.
        if keys == ["<id>"] and obj:
            return "{<id>: %s}" % shape(next(iter(obj.values())), depth + 1)
        parts = []
        for k in keys:
            v = next((obj[o] for o in obj if ("<id>" if IDENT.match(str(o)) else str(o)) == k),
                     None)
            parts.append("%s:%s" % (k, shape(v, depth + 1)))
        return "{%s}" % ",".join(parts)
    if isinstance(obj, list):
        return "[%s]" % (shape(obj[0], depth + 1) if obj else "")
    return type(obj).__name__


def scan(root):
    """Return {shape: [relpaths]} for durable state under .claude/state and memory/*.json."""
    out = defaultdict(list)
    unreadable = []
    targets = [os.path.join(root, ".claude", "state")]
    for d in targets:
        if not os.path.isdir(d):
            continue
        for dirpath, _, files in os.walk(d):
            for f in files:
                if not f.endswith(".json"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    with open(p) as fh:
                        doc = json.load(fh)
                except Exception:
                    # NOT skipped silently: an unreadable state file is a finding, and counting
                    # it as absent is the fail-toward-PASS shape this whole effort exists to kill.
                    unreadable.append(os.path.relpath(p, root))
                    continue
                out[shape(doc)].append(os.path.relpath(p, root))
    return out, unreadable


def main():
    as_json = "--json" in sys.argv
    only = None
    if "--core" in sys.argv:
        only = sys.argv[sys.argv.index("--core") + 1]
    show = "--list" in sys.argv

    rows = {}
    for name, d in CORES.items():
        if only and name != only:
            continue
        root = os.path.join(BASE, d)
        if not os.path.isdir(root):
            rows[name] = {"present": False}
            continue
        shapes, bad = scan(root)
        rows[name] = {
            "present": True,
            "files": sum(len(v) for v in shapes.values()),
            "shapes": len(shapes),
            "unreadable": bad,
            "shape_keys": sorted(shapes) if show or as_json else [],
        }

    if as_json:
        print(json.dumps(rows, indent=2))
        return 0

    print("\n  DURABLE STATE SHAPES — .claude/state/**.json, all Cores\n")
    print("  %-10s %8s %8s %10s  %s" % ("core", "files", "shapes", "files/shape", "unreadable"))
    live = []
    for name, r in rows.items():
        if not r["present"]:
            print("  %-10s %8s" % (name, "absent"))
            continue
        live.append(r)
        print("  %-10s %8d %8d %10.1f  %s"
              % (name, r["files"], r["shapes"], r["files"] / max(r["shapes"], 1),
                 len(r["unreadable"]) or ""))

    if len(live) > 1:
        s = [r["shapes"] for r in live]
        f = [r["files"] for r in live]
        print("\n  shapes  %d-%d  (spread %.1fx)" % (min(s), max(s), max(s) / max(min(s), 1)))
        print("  files   %d-%d  (spread %.1fx)" % (min(f), max(f), max(f) / max(min(f), 1)))
        print("\n  The plan's inference — schema estate is a property of SHARED code — predicts")
        print("  a shape spread much smaller than the file spread. Read the two lines above.")

    for name, r in rows.items():
        if r.get("unreadable"):
            print("\n  UNREADABLE on %s (a state file that will not parse is a finding):" % name)
            for p in r["unreadable"][:10]:
                print("    %s" % p)

    if show:
        for name, r in rows.items():
            if r.get("shape_keys"):
                print("\n  %s shapes:" % name)
                for k in r["shape_keys"]:
                    print("    %s" % (k[:150] + ("..." if len(k) > 150 else "")))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
