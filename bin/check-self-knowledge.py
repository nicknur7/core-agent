#!/usr/bin/env python3
"""check-self-knowledge.py — the freshness BACKSTOP for a Core's self-knowledge.

Per the HARD RULE (2026-05-28), wiring does the work: capabilities.md is
regenerated from live config at close (gen-capabilities.py), so it can't rot.
This script is the thin residual check for what CANNOT be wired — chiefly the
hand-authored prose in CLAUDE.md (the Core's constitution) — plus a safety net
that flags if the wired pieces somehow fell behind.

Five checks (all fail-open):
  1. CLAUDE.md Core-list DRIFT — every Core reachable via a `peer-*` MCP server
     (+ this Core itself) must be named in CLAUDE.md. Catches the real bug found
     2026-06-09: CLAUDE.md had 0 mentions of finance despite peer-finance being live.
  2. capabilities.md staleness — runs gen-capabilities.py --check; flags if the
     derived inventory would change (i.e. the wired regen didn't run).
  3. core-profile.md presence + freshness stamp age (>30d or missing).
  4. system-rundown.md HOOK COVERAGE — every hook registered in settings.json must be
     named in the end-to-end map. Catches the 2026-06-18 drift class (map said "5 hooks",
     live had 13; arch map was missing recall-gate/approval-gate/model-pin/recall-first).
  5. infra-doc freshness — system-rundown.md + the core-infra project files flagged when
     their date stamp is >45d (the hand-authored-prose backstop the 2026-06-09 build lacked).

Usage: check-self-knowledge.py [--quiet]
  exit 0  = self-knowledge current
  exit 10 = drift found (caller surfaces the report)
Honors $CORE_INSTANCE, else git toplevel, else cwd.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core_paths  # noqa: E402  — path registry (single source of truth)

# 2026-06-24: route the registry-tracked system-rundown path through core_paths
# instead of a hardcoded literal (tripped lint-code-paths → staged-code save-block
# in business/school). Relative form keeps `root / rel` consumption unchanged.
_SYS_RUNDOWN_REL = str(core_paths.TASK_SYSTEM_RUNDOWN.relative_to(core_paths.INSTANCE))

QUIET = "--quiet" in sys.argv


def _root() -> Path:
    r = os.environ.get("CORE_INSTANCE")
    if r:
        return Path(r)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except Exception:
        pass
    return Path.cwd()


def _load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def check_claude_md_cores(root: Path, findings: list):
    """Every peer-* MCP Core + self must be named in CLAUDE.md."""
    mcp = _load_json(root / ".mcp.json")
    identity = _load_json(root / ".claude" / "identity.json")
    self_core = identity.get("domain_label") or re.sub(r"^core-", "", root.name)
    expected = {self_core}
    for srv in (mcp.get("mcpServers") or {}):
        m = re.match(r"peer-(\w+)", srv)
        if m:
            expected.add(m.group(1))
    claude = ""
    try:
        claude = (root / "CLAUDE.md").read_text().lower()
    except Exception:
        return
    missing = sorted(c for c in expected if c and c.lower() not in claude)
    if missing:
        findings.append(
            f"CLAUDE.md DRIFT — names no {', '.join(missing)} Core(s), but they are live "
            f"(peer-MCP / self). Constitution is stale vs the {len(expected)}-Core system.")


def check_capabilities_fresh(root: Path, findings: list):
    gen = root / "bin" / "gen-capabilities.py"
    if not gen.is_file():
        return
    try:
        out = subprocess.run(["python3", str(gen), "--check"],
                             capture_output=True, text=True, timeout=30,
                             env={**os.environ, "CORE_INSTANCE": str(root)})
        if out.returncode == 10:
            findings.append("capabilities.md STALE — derived inventory differs from live "
                            "config (the close-time regen did not run). Run `python3 bin/gen-capabilities.py`.")
    except Exception:
        pass


def check_core_profile(root: Path, findings: list):
    cp = root / "memory" / "core-profile.md"
    if not cp.is_file():
        findings.append("core-profile.md MISSING — this Core has no self-description ('what it is').")
        return
    try:
        txt = cp.read_text()
    except Exception:
        return
    m = re.search(r"[Ll]ast[ _]updated:?\s*(\d{4})-(\d{2})-(\d{2})", txt)
    if not m:
        findings.append("core-profile.md has no `Last updated` stamp — staleness can't be detected.")
        return
    try:
        stamp = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        # No live clock dependency beyond date.today(); informational only.
        age = (date.today() - stamp).days
        if age > 30:
            findings.append(f"core-profile.md is {age}d old (stamp {stamp}) — verify mandate still current.")
    except Exception:
        pass


def _live_hook_stems(root: Path) -> set:
    """Distinct hook-script stems registered in settings.json (say-do-gap.sh/.py -> 'say-do-gap')."""
    settings = _load_json(root / ".claude" / "settings.json")
    stems = set()
    for _ev, groups in (settings.get("hooks") or {}).items():
        for grp in groups or []:
            for h in grp.get("hooks", []) or []:
                m = re.search(r"([\w.-]+)\.(?:sh|py)\b", h.get("command", "") or "")
                if m:
                    stems.add(m.group(1))
    return stems


# Plumbing/orchestrator hooks — real, but not components a self-knowledge map needs to name.
_HOOK_COVERAGE_IGNORE = {
    "session-lifecycle", "run-brain-update", "end-session", "get-session-start-time",
    "sentinel-approve", "apply-shared-hooks",
}


# The two maps whose job is to be the COMPLETE picture — both must name every live hook.
_HOOK_COVERAGE_DOCS = (
    (_SYS_RUNDOWN_REL, "system-rundown.md (end-to-end map)"),
    ("docs/architecture/core-system-architecture.html", "the Core Atlas (architecture tab)"),
)


def _is_writer(root: Path) -> bool:
    """True for the primary/writer Core (life). Reads identity.hook_profile.role; unset → writer."""
    try:
        data = json.loads((root / ".claude" / "identity.json").read_text())
        return ((data.get("hook_profile") or {}).get("role") or "writer") == "writer"
    except Exception:
        return True  # fail toward checking


def check_hook_coverage(root: Path, findings: list):
    """Every LIVE hook must be named in the maps whose job is to be complete: the end-to-end
    rundown AND the architecture tab. This is the check that would have caught the 2026-06-18
    drift (rundown said '5 hooks', live had 13; the Atlas was missing recall-gate/approval-gate/
    model-pin/recall-first).

    WRITER-ONLY (2026-07-11): system-rundown.md + the Core Atlas are the PRIMARY Core's
    architecture map. Pullers get stale life-clones of them at spawn and don't maintain a
    per-Core map, so this check false-flagged ~26 'missing' hooks on every peer forever.
    Scope it to the writer (life). Fail-safe: a Core without one of these docs still skips it."""
    if not _is_writer(root):
        return
    stems = _live_hook_stems(root) - _HOOK_COVERAGE_IGNORE
    if not stems:
        return
    for rel, label in _HOOK_COVERAGE_DOCS:
        try:
            txt = (root / rel).read_text().lower()
        except Exception:
            continue  # this Core doesn't have this map → nothing to check
        missing = sorted(s for s in stems if s.lower() not in txt)
        if missing:
            shown = ", ".join(missing[:6]) + (f" (+{len(missing) - 6} more)" if len(missing) > 6 else "")
            findings.append(
                f"{label} MISSING live hook(s): {shown} — registered in settings.json but absent. "
                f"The map has drifted behind the live hooks.")


_INFRA_DOCS = (
    _SYS_RUNDOWN_REL,
    "memory/projects/core.md",
    "memory/projects/core-brain.md",
    "memory/projects/core-improvement.md",
)


def check_infra_freshness(root: Path, findings: list, max_age: int = 45):
    """Flag high-value infra docs whose date stamp is stale (the hand-authored prose backstop)."""
    for rel in _INFRA_DOCS:
        p = root / rel
        if not p.is_file():
            continue
        try:
            txt = p.read_text()
        except Exception:
            continue
        m = re.search(r"(?:[Ll]ast[ _]updated|[Rr]ebuilt)\s*:?\s*(\d{4})-(\d{2})-(\d{2})", txt)
        if not m:
            continue
        try:
            stamp = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            age = (date.today() - stamp).days
            if age > max_age:
                findings.append(f"{rel} is {age}d old (stamp {stamp}) — infra doc may have drifted; re-verify vs live.")
        except Exception:
            pass


def main():
    root = _root()
    findings = []
    check_claude_md_cores(root, findings)
    check_capabilities_fresh(root, findings)
    check_core_profile(root, findings)
    check_hook_coverage(root, findings)
    check_infra_freshness(root, findings)

    if not findings:
        if not QUIET:
            print(f"[self-knowledge] {root.name}: current — knows what it is + does.")
        sys.exit(0)
    # Drift always prints (even with --quiet) — surfacing it is the whole point;
    # --quiet only silences the success line so SessionStart stays quiet when fresh.
    print(f"🪞 SELF-KNOWLEDGE DRIFT ({root.name}):")
    for f in findings:
        print(f"   - {f}")
    sys.exit(10)


if __name__ == "__main__":
    main()
