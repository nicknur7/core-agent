#!/usr/bin/env python3
"""Post-pipeline defensive cleanup: rewrite stale paths in graphify output JSON.

Runs after merge.py in auto-pipeline.sh. Brain vaults that have been renamed or
relocated (rare but happens — e.g., on macOS folder moves, on engine-split
migrations) leave behind stale path references inside older session/subagent
markdown bodies. merge.py preserves whatever's in the chunks. This pass
normalizes the *output* artifacts so hub regen + recall don't surface broken
paths.

Rewrites are CONFIG-DRIVEN, not hardcoded. The engine ships with no rewrites —
fresh instances have no migration history to clean up. Per-instance rewrites
live in:

    $CORE_INSTANCE/scheduling/brain-pg/path-rewrites.json

Schema (JSON list of {from, to} pairs):

    [
      {"from": "/old/vault/path/", "to": "/new/vault/path/"},
      {"from": "projects/legacy-slug/sessions/", "to": "projects/new-slug/sessions/"}
    ]

Rewrites are conservative: only PATH-SHAPED occurrences (preceded by `/` or `~`).
Prose references that don't begin with a path-anchor are LEFT ALONE — e.g.,
"Init core brain as separate private git repo" is intentional naming, not a path.

If the config file is missing, this script is a no-op and exits cleanly.

Targets: graph.json, community-names.json, community-names-by-fingerprint.json,
audit.json under $CORE_BRAIN/_build/output/graphify-out/.
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple


def load_rewrites() -> List[Tuple[re.Pattern, str]]:
    instance = os.environ.get("CORE_INSTANCE")
    if not instance:
        return []
    config_path = Path(instance) / "scheduling" / "brain-pg" / "path-rewrites.json"
    if not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"[cleanup-stale-paths] config unreadable ({config_path}): {e}", file=sys.stderr)
        return []
    rewrites = []
    for entry in data:
        old = entry.get("from")
        new = entry.get("to")
        if not old or new is None:
            continue
        # Negative lookbehind (?<![\w.]) prevents matching mid-token.
        pat = re.compile(r"(?<![\w.])" + re.escape(old))
        rewrites.append((pat, new))
    return rewrites


def rewrite_str(s: str, rewrites) -> str:
    for pat, repl in rewrites:
        s = pat.sub(repl, s)
    return s


def walk(o, rewrites):
    if isinstance(o, dict): return {k: walk(v, rewrites) for k, v in o.items()}
    if isinstance(o, list): return [walk(x, rewrites) for x in o]
    if isinstance(o, str): return rewrite_str(o, rewrites)
    return o


def main():
    brain = os.environ.get("CORE_BRAIN")
    if not brain:
        sys.exit("CORE_BRAIN required")
    rewrites = load_rewrites()
    if not rewrites:
        print("[cleanup-stale-paths] no path-rewrites.json configured — no-op")
        return
    print(f"[cleanup-stale-paths] loaded {len(rewrites)} rewrite rule(s)")

    out_dir = Path(brain) / "_build" / "output" / "graphify-out"
    targets = ["graph.json", "community-names.json",
               "community-names-by-fingerprint.json", "audit.json"]
    total_fixed = 0
    for name in targets:
        p = out_dir / name
        if not p.exists():
            print(f"[cleanup-stale-paths] skip (missing): {name}")
            continue
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            print(f"[cleanup-stale-paths] skip (invalid JSON): {name}: {e}")
            continue
        new_data = walk(data, rewrites)
        before = json.dumps(data, sort_keys=True)
        after = json.dumps(new_data, sort_keys=True)
        if before != after:
            indent = 2 if name == "audit.json" else None
            p.write_text(json.dumps(new_data, indent=indent))
            print(f"[cleanup-stale-paths] fixed: {name}")
            total_fixed += 1
        else:
            print(f"[cleanup-stale-paths] clean: {name}")
    print(f"[cleanup-stale-paths] total artifacts rewritten: {total_fixed}")


if __name__ == "__main__":
    main()
