#!/usr/bin/env python3
"""wiring-audit.py — is every built component actually reachable from something that RUNS?

WHY THIS EXISTS (2026-08-28). Three separate subsystems were built, given an entry point, and
never wired to anything automatic. Nobody noticed for months because each one FAILS SILENTLY —
a component that never runs looks exactly like a component with nothing to do:

  * compile-truth-refresh.py  — 461 loc. Only reachable via the manual /refresh-truth command.
    Result: 534 of 68,296 non-Source entities ever compiled (0.78%), and ZERO on four of five
    Cores. Every hub carried the summary it was born with.
  * corroborate.py            — wires the cross-Core `same_as` bridge. All 19,977 edges were
    created 2026-07-07 in one manual run. 52 days unbridged.
  * artifact_utility          — a table built to measure whether an installed rule was useful.
    0 rows, ever.

The common defect is not any of those three. It is that NOTHING CHECKED whether a built thing was
reachable. This script is that check. It is deliberately dumb and deterministic: no heuristics
about intent, just "can control flow reach this file from something the machine actually starts."

REACHABILITY = referenced by any of:
  - a registered hook (.claude/settings.json + the hook scripts it names)
  - a LaunchAgent plist under ~/Library/LaunchAgents
  - a slash command (.claude/commands/*.md)  [counts as MANUAL, see below]
  - a shell script under bin/ or scheduling/ that is itself reachable
  - a Python `import <module>` from a reachable Python file

MANUAL-ONLY is a legitimate state, but it must be DECLARED. A script reachable only from a slash
command is reported as MANUAL, not as wired — that distinction is the whole point: /refresh-truth
existed the entire time and the brain still went stale, because "a human can run it" is not the
same as "it runs."

Exit 0 = clean. Exit 1 = new unreachable scripts (not on the allowlist).
Usage:  python3 bin/wiring-audit.py [--json] [--quiet]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parent.parent))
ALLOWLIST = REPO / "bin" / "wiring-allowlist.json"

SCAN_GLOBS = ["scheduling/*/*.py", "scheduling/*/*.sh", "bin/*.py", "bin/*.sh"]
SKIP_PARTS = ("archive", "_archive", "/tests/", "__pycache__", "/migrations/")


def _scan_targets() -> list[Path]:
    out = []
    for g in SCAN_GLOBS:
        for p in REPO.glob(g):
            s = str(p)
            if any(x in s for x in SKIP_PARTS):
                continue
            if p.name.startswith("_"):
                continue
            out.append(p)
    return sorted(set(out))


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="ignore")
    except Exception:
        return ""


def _strip_comments(txt: str, suffix: str) -> str:
    """Comments are not execution.

    Codex 2026-08-28 (MEDIUM): the first cut accepted any substring hit, so a comment saying
    "run foo.py manually", a help string, or a bare `[[ -f foo.py ]]` existence check marked
    foo.py reachable. The lifecycle comments this very change added were themselves full-strength
    edges. An instrument for finding unwired code cannot count prose as a caller.
    """
    out = []
    for line in txt.splitlines():
        st = line.lstrip()
        if suffix in (".sh", ".py") and st.startswith("#"):
            continue
        if suffix == ".md":          # slash-command prose: keep fenced code only
            pass
        # strip trailing shell/python comments, naive but conservative
        if suffix in (".sh", ".py") and " #" in line:
            line = line.split(" #", 1)[0]
        out.append(line)
    return "\n".join(out)


def _ref(name: str, mod: str, body: str) -> bool:
    """A reference must look like an INVOCATION, not an incidental substring.

    Codex 2026-08-28 (MEDIUM): bare `t.name in body` made `ledger.py` match any mention of
    `steering-ledger.py`, and `mcp-server.py` match `peer-mcp-server.py` — so an unreachable
    short-named script inherited reachability from an unrelated longer filename.

    The preceding character must not be a NAME character (letter, digit, _ or -) — that kills the
    collision. It MAY be `/` or a space or a quote, because an absolute path is exactly how these
    are actually invoked: my first attempt excluded `/` too and produced three false negatives
    (si-drain.sh, si-drain-fleet.sh, brain-health.py all invoked by full path). An instrument that
    cries wolf is as useless as one that sleeps.
    """
    if re.search(rf"(^|[^A-Za-z0-9_-]){re.escape(name)}($|[^A-Za-z0-9])", body):
        return True
    if re.search(rf"\bimport\s+{re.escape(mod)}\b", body):
        return True
    if re.search(rf"\bfrom\s+{re.escape(mod)}\s+import\b", body):
        return True
    return False


def _roots() -> tuple[list[Path], list[Path]]:
    """(auto_roots, manual_roots) — things the machine starts vs things a human types."""
    auto, manual = [], []
    for f in ("settings.json", "settings.local.json"):
        q = REPO / ".claude" / f
        if q.exists():
            auto.append(q)
    # MCP servers are started by the harness from .mcp.json — as automatic as a hook.
    mcp = REPO / ".mcp.json"
    if mcp.exists():
        auto.append(mcp)
    # Only hooks the settings actually REGISTER are automatic roots.
    # Codex 2026-08-28 (MEDIUM): treating every file in .claude/hooks/ as an entry point meant a
    # retired, typo-named or never-registered hook made everything it mentions look wired —
    # defeating the instrument for precisely the "built but never registered" class it exists for.
    settings_txt = "".join(_read(REPO / ".claude" / f) for f in ("settings.json", "settings.local.json"))
    for p in (REPO / ".claude" / "hooks").glob("*"):
        if p.is_file() and p.name in settings_txt:
            auto.append(p)
    la = Path(os.path.expanduser("~/Library/LaunchAgents"))
    if la.is_dir():
        for p in la.glob("*.plist"):
            # only plists that mention THIS repo
            if str(REPO) in _read(p):
                auto.append(p)
    for p in (REPO / ".claude" / "commands").glob("*.md"):
        manual.append(p)
    return auto, manual


def _reachable_from(roots: list[Path], targets: list[Path]) -> set[Path]:
    """Transitive closure: a target referenced by a root, or by an already-reachable target."""
    texts = {p: _strip_comments(_read(p), p.suffix) for p in roots + targets}
    reached: set[Path] = set()
    frontier = list(roots)
    seen_sources = set(roots)
    while frontier:
        cur = frontier.pop()
        body = texts.get(cur, "")
        if not body:
            continue
        for t in targets:
            if t in reached:
                continue
            if _ref(t.name, t.stem.replace("-", "_"), body):
                reached.add(t)
                if t not in seen_sources:
                    seen_sources.add(t)
                    frontier.append(t)
    return reached


def main() -> int:
    as_json = "--json" in sys.argv
    quiet = "--quiet" in sys.argv

    targets = _scan_targets()
    auto_roots, manual_roots = _roots()

    auto_reached = _reachable_from(auto_roots, targets)
    manual_reached = _reachable_from(manual_roots, targets) - auto_reached
    unreachable = [p for p in targets if p not in auto_reached and p not in manual_reached]

    allow = {}
    if ALLOWLIST.exists():
        try:
            allow = json.loads(ALLOWLIST.read_text()).get("manual_only", {})
        except Exception:
            allow = {}

    rel = lambda p: str(p.relative_to(REPO))
    undeclared = [p for p in unreachable if rel(p) not in allow]
    declared = [p for p in unreachable if rel(p) in allow]

    result = {
        "scanned": len(targets),
        "auto_wired": len(auto_reached),
        "manual_only": sorted(rel(p) for p in manual_reached),
        "unreachable_undeclared": sorted(rel(p) for p in undeclared),
        "unreachable_declared": sorted(rel(p) for p in declared),
    }

    if as_json:
        print(json.dumps(result, indent=1))
    elif not quiet:
        print("═══ WIRING AUDIT ═══")
        print(f"  scanned {result['scanned']} · auto-wired {result['auto_wired']} · "
              f"manual-only {len(result['manual_only'])} · "
              f"unreachable {len(undeclared)} undeclared / {len(declared)} declared")
        if result["manual_only"]:
            print("\n  MANUAL-ONLY (a human must type it — 'someone can run it' is not 'it runs'):")
            for r in result["manual_only"]:
                print(f"    ⚠ {r}")
        if undeclared:
            print("\n  UNREACHABLE and NOT DECLARED — built, never wired:")
            for r in result["unreachable_undeclared"]:
                print(f"    ✗ {r}")
            print(f"\n  Add to {rel(ALLOWLIST)} under 'manual_only' with a reason, or wire it.")
        if not undeclared and not result["manual_only"]:
            print("\n  ✅ everything built is reachable from something that runs.")

    return 1 if undeclared else 0


if __name__ == "__main__":
    sys.exit(main())
