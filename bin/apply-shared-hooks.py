#!/usr/bin/env python3
"""apply-shared-hooks.py — idempotently register the universal hook set into a
Core's .claude/settings.json.

WHY: hook *scripts* propagate via the shared .claude/hooks/ dir, but their
*registration* lives in settings.json, which is per_core_keep and never syncs.
A fresh fork therefore has the scripts on disk but unwired — recall
and the discipline gates never fire. This script is the transport for the wiring.
It is invoked at the tail of sync-from-baseline.sh (every pull), so one `/sync
pull` wires a fork permanently.

CONTRACT (safety is the whole point — this auto-writes settings.json on pulls):
  - STRICTLY ADDITIVE. Never removes, reorders, or edits an existing hook entry.
  - IDEMPOTENT. A hook is added only if no existing entry in that event already
    contains its `match` substring. Re-runs are no-ops (and write nothing).
  - ATOMIC + FAIL-SAFE. Writes to a temp file, re-parses it to validate, then
    os.replace()s. On ANY error, settings.json is left exactly as it was.
  - WRITER-AWARE. Entries flagged "writer_skip" are not added on the baseline
    writer core (mirrors the sync-from-baseline.sh writer-guard — e.g. the
    SessionStart auto-pull hook is never registered on life).

Usage:
  apply-shared-hooks.py [CORE_DIR] [--check]
    CORE_DIR  defaults to $CLAUDE_PROJECT_DIR, else script-dir/.. .
    --check   report what WOULD be added; write nothing. Exit 0.
"""
import json
import os
import sys
import tempfile


def load_json(path):
    with open(path) as f:
        return json.load(f)


def baseline_writer_slug(manifest_path):
    try:
        m = load_json(manifest_path)
    except Exception:
        return None
    w = m.get("baseline_writer")
    if not w:
        return None
    return w[5:] if w.startswith("core-") else w


def core_slug(core_dir):
    base = os.path.basename(os.path.normpath(core_dir))
    return base[5:] if base.startswith("core-") else base


def event_has_match(blocks, match):
    """True if any hook command in any block of this event contains `match`."""
    for b in blocks or []:
        for h in b.get("hooks", []) or []:
            if match in (h.get("command", "") or ""):
                return True
    return False


def first_default_block(blocks):
    """Return the first matcher-'' block, or None. (These events use a single
    matcher-'' block; we never touch matcher-specific blocks.)"""
    for b in blocks:
        if b.get("matcher", "") == "":
            return b
    return None


def compute_additions(settings, shared, is_writer):
    """Pure planner. Returns a list of (event, entry_dict, prepend) to add.
    Does not mutate `settings`."""
    additions = []
    hooks = settings.get("hooks", {}) or {}
    for event, entries in (shared.get("events", {}) or {}).items():
        blocks = hooks.get(event, []) or []
        for e in entries:
            if e.get("writer_skip") and is_writer:
                continue
            if event_has_match(blocks, e["match"]):
                continue
            entry = {"type": "command", "command": e["command"]}
            if "timeout" in e:
                entry["timeout"] = e["timeout"]
            additions.append((event, entry, bool(e.get("prepend"))))
    return additions


def apply_additions(settings, additions):
    """Mutate `settings` in place, applying planned additions. Creates the
    event list + a matcher-'' block where missing."""
    hooks = settings.setdefault("hooks", {})
    for event, entry, prepend in additions:
        blocks = hooks.setdefault(event, [])
        blk = first_default_block(blocks)
        if blk is None:
            blk = {"matcher": "", "hooks": []}
            blocks.append(blk)
        blk.setdefault("hooks", [])
        if prepend:
            blk["hooks"].insert(0, entry)
        else:
            blk["hooks"].append(entry)
    return settings


def atomic_write_json(path, obj):
    """Write obj as pretty JSON atomically; validate by re-parsing before replace."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".shared-hooks-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
        with open(tmp) as f:          # validate the temp file parses
            json.load(f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv):
    check = "--check" in argv
    positional = [a for a in argv if not a.startswith("-")]
    core_dir = (
        positional[0] if positional
        else os.environ.get("CLAUDE_PROJECT_DIR")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    )
    settings_path = os.path.join(core_dir, ".claude", "settings.json")
    shared_path = os.path.join(core_dir, "bin", "shared-hooks.json")
    manifest_path = os.path.join(core_dir, "bin", "sync-manifest.json")

    if not os.path.isfile(settings_path) or not os.path.isfile(shared_path):
        return 0  # nothing to do; fail-safe silent

    try:
        settings = load_json(settings_path)
        shared = load_json(shared_path)
    except Exception as e:
        print(f"[apply-shared-hooks] skip — unreadable JSON ({e})", file=sys.stderr)
        return 0

    is_writer = core_slug(core_dir) == baseline_writer_slug(manifest_path)
    additions = compute_additions(settings, shared, is_writer)

    if not additions:
        if check:
            print("[apply-shared-hooks] up to date — 0 hooks to add.")
        return 0

    summary = ", ".join(f"{ev}:{e['command'].split('/')[-1].strip(chr(34))}" for ev, e, _ in additions)
    if check:
        print(f"[apply-shared-hooks] WOULD add {len(additions)} hook(s): {summary}")
        return 0

    try:
        apply_additions(settings, additions)
        atomic_write_json(settings_path, settings)
        print(f"[apply-shared-hooks] registered {len(additions)} missing hook(s): {summary}")
    except Exception as e:
        print(f"[apply-shared-hooks] ABORTED, settings.json untouched ({e})", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
