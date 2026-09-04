#!/usr/bin/env python3
"""bin/gen-arch-roster.py — one row per capability, derived from measurement, not memory.

WHY THIS EXISTS (2026-09-02). docs/architecture/core-system-architecture.html had never
rendered — a hand-written diagram that drifted from the running system until it named hooks
that don't exist and omitted ones that do. The fix is not a better hand edit: it's removing the
hand from the loop. This script IS the measurement. bin/gen-architecture-doc.py turns its output
into the diagram; nothing else should ever hand-author that file again.

WHAT IT DOES: walks every capability surface this Core actually exposes —

  hook      .claude/settings.json (the live wiring) cross-referenced against
            bin/hook-registry.json (the shared registry, which also carries `retired` +
            `retired_reason` for hooks that were pulled from settings.json but kept on
            record). A `.sh` wrapper that only `exec`s a sibling script in the same
            directory is resolved to that script — a wrapper and its implementation are
            ONE hook, not two rows.
  skill     .claude/skills/<name>/SKILL.md (frontmatter name + description).
  command   .claude/commands/<name>.md (slash commands).
  agent     .claude/agents/<name>.md (the native flat format Claude Code loads) PLUS any
            .claude/agents/<name>/CLAUDE.md dir-form relic — the migration that replaced
            dir-form specs with flat .md files left some on disk; they do not load, so
            they are recorded reachable=false, never as a live row.
  mcp       .mcp.json if present, else .mcp.json.template (a template ships with no live
            .mcp.json; keys starting with "_" are documentation examples, not servers).
  daemon    ~/Library/LaunchAgents/<identity.launchagent_label_prefix>.*.plist. Empty and
            correct on a template identity.json, which ships with no prefix set.

SI artifacts and learned contracts are DATA, not architecture — this reports their counts
(COUNT(*) only, never content) from Postgres when reachable, and degrades honestly with an
errors[] entry when it is not (no psycopg2, no server, no schema). No query in this file
ever selects artifact/contract CONTENT.

FIVE-LAYER MODEL (Sense / Judge / Learn / Act / Police) — assigned, not asserted:
  - Hooks: primarily by EVENT (SessionStart/UserPromptSubmit -> Sense; SessionEnd -> Act),
    with PreToolUse/Stop/PostToolBatch/PostToolUse/SubagentStop split further by the
    registry's own `intent.effect` (block -> Police, inject -> Judge, side-effect -> Act,
    log-only -> Learn). See classify_hook_layer(). Everything else observational
    (MessageDisplay, PreCompact/PostCompact, Notification, ...) -> Learn.
  - Skills/commands/agents/mcp: a small curated table (function is legible from the name;
    a keyword heuristic would be noisier than just naming them), with an honest default
    for anything the table doesn't recognize so a future addition still gets a row instead
    of crashing the generator.

ID SCHEME (mirrors bin/gen-core-manifest.py): "<rung>:<event-or-scope>:<name>" — structural,
never a display string, so two runs of an unchanged system produce identical ids.

Fail-soft throughout: a missing optional source is `sources_unavailable`, a source that
exists but doesn't parse is `errors[]`. Never raises out of main().

Usage:
    CORE_INSTANCE=/path/to/core python3 bin/gen-arch-roster.py --out /path/to/roster.json
    (no --out => prints to stdout)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
GENERATOR = "bin/gen-arch-roster.py"
LAYERS = ("Sense", "Judge", "Learn", "Act", "Police")

_SCRIPT_RE = re.compile(r"([\w./$-]+\.(?:sh|py))")
_DISPATCH_RE = re.compile(r'(?:python3|bash|exec)\s+"?\$?\{?[\w./${}:-]*?/([\w-]+\.(?:py|sh))"?')


# ---------------------------------------------------------------------------
# small io helpers — fail-soft; failures are recorded as errors, not raised
# ---------------------------------------------------------------------------
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


def _load_json(p: Path, errors: list, label: str):
    """None = source doesn't exist (sources_unavailable, not an error). {} + error = exists
    but failed to parse."""
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        errors.append(f"{label}: unreadable ({e.__class__.__name__}: {e})")
        return {}


def _frontmatter_field(text: str, field: str) -> str | None:
    fm = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not fm:
        return None
    m = re.search(rf"^{field}:\s*(.+)$", fm.group(1), re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else None


def _first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.{20,}?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s[:limit]


def _seat(root: Path, identity: dict) -> str:
    return identity.get("core_slug") or identity.get("domain") or root.name or "core"


# ---------------------------------------------------------------------------
# hooks — settings.json (ground truth of what's wired) + hook-registry.json
# (shared intent/retirement record) + wrapper-dispatch resolution
# ---------------------------------------------------------------------------
def _relpath(raw: str) -> str:
    """Strip $CLAUDE_PROJECT_DIR / ${CLAUDE_PROJECT_DIR} prefixes and quoting to get a
    repo-relative path from a settings.json command token."""
    p = raw.strip('"\'')
    p = re.sub(r"^\$\{?CLAUDE_PROJECT_DIR\}?/", "", p)
    return p


def _wired_hooks(settings: dict) -> list[dict]:
    """Every (event, matcher, basename, relpath, dir) actually registered in settings.json.
    Most live under .claude/hooks/, but at least one (sync-from-baseline.sh) lives under
    bin/ — the path is taken from the command itself, never assumed."""
    out = []
    for event, groups in (settings.get("hooks") or {}).items():
        for grp in groups or []:
            matcher = grp.get("matcher", "")
            for h in grp.get("hooks", []) or []:
                cmd = h.get("command", "")
                m = _SCRIPT_RE.search(cmd)
                if not m:
                    continue
                rel = _relpath(m.group(1))
                out.append({"event": event, "matcher": matcher, "basename": Path(rel).name,
                            "dir": Path(rel).parent.as_posix(), "relpath": rel, "command": cmd})
    return out


def _resolve_dispatch(hooks_dir: Path, sh_name: str, seen: set) -> str | None:
    """A .sh file that only exec/invokes a sibling script IS that script, for census
    purposes — a wrapper and its implementation are one hook. Follows one hop past the
    wrapper itself (stop-hook.sh -> session-lifecycle.sh is a controller dispatch, not a
    1:1 wrapper, so it resolves to the name but is annotated as a dispatch, not folded)."""
    if sh_name in seen:
        return None
    seen.add(sh_name)
    p = hooks_dir / sh_name
    if not p.is_file():
        return None
    try:
        text = p.read_text()
    except Exception:
        return None
    for m in _DISPATCH_RE.finditer(text):
        target = m.group(1)
        if target == sh_name:
            continue
        if (hooks_dir / target).is_file():
            return target
    return None


def classify_hook_layer(event: str, effect: str | None) -> str:
    """Primary axis is the EVENT — that's the architecture Nick asked to see. `effect`
    (from hook-registry.json intent) splits the events that legitimately span more than
    one layer."""
    if event in ("SessionStart", "InstructionsLoaded"):
        return "Sense"
    if event == "UserPromptSubmit":
        return "Police" if effect == "block" else "Sense"
    if event == "PreToolUse":
        return {"block": "Police", "inject": "Judge", "log-only": "Learn"}.get(effect, "Judge")
    if event == "Stop":
        return {"block": "Police", "side-effect": "Act", "inject": "Judge",
                "log-only": "Learn"}.get(effect, "Act")
    if event == "SessionEnd":
        return "Act"
    if event == "PostToolBatch":
        return "Judge" if effect == "inject" else "Learn"
    if event == "PostToolUse":
        return "Act" if effect == "side-effect" else "Learn"
    if event == "SubagentStop":
        return "Act" if effect == "side-effect" else "Learn"
    # MessageDisplay, PostToolUseFailure, PreCompact, PostCompact, Notification,
    # UserPromptExpansion, StopFailure, SubagentStart — pure observation.
    return "Learn"


def gather_hooks(root: Path, errors: list) -> list[dict]:
    settings = _load_json(root / ".claude" / "settings.json", errors, "settings.json") or {}
    registry = _load_json(root / "bin" / "hook-registry.json", errors, "hook-registry.json")
    reg_entries = (registry or {}).get("hooks", [])
    reg_missing = registry is None

    wired = _wired_hooks(settings)
    # Resolve each wired basename through wrapper/dispatch, deduping by (event, resolved name).
    wired_keys = set()
    rows = []
    for w in wired:
        script_dir = root / w["dir"]
        stem = Path(w["basename"]).stem
        resolved, resolved_rel = stem, w["relpath"]
        if w["basename"].endswith(".sh"):
            target = _resolve_dispatch(script_dir, w["basename"], set())
            if target:
                resolved = Path(target).stem
                resolved_rel = (Path(w["dir"]) / target).as_posix()
        key = (w["event"], resolved)
        if key in wired_keys:
            continue
        wired_keys.add(key)
        # Find registry intent for this (name, event) if the registry is present.
        reg = next((r for r in reg_entries if r["name"] == resolved and r["event"] == w["event"]),
                   None)
        effect = (reg or {}).get("intent", {}).get("effect")
        layer = classify_hook_layer(w["event"], effect)
        # Prefer the resolved implementation's own path; else the original wired path.
        py_sibling = script_dir / f"{resolved}.py"
        owner = py_sibling.relative_to(root).as_posix() if py_sibling.is_file() else resolved_rel
        evidence = f"wired: .claude/settings.json {w['event']} matcher={w['matcher'] or '(none)'}"
        if resolved != stem:
            evidence += f"; {w['basename']} dispatches to {resolved}"
        rows.append({
            "id": f"hook:{w['event']}:{resolved}", "rung": "hook", "seat": None,
            "name": resolved, "event": w["event"], "layer": layer,
            "owner_file": owner, "evidence": evidence,
            "reachable": True, "retired_reason": None,
        })

    if reg_missing:
        errors.append("hook-registry.json absent — hook rows built from settings.json wiring "
                       "only, no intent.effect available for layer refinement, no retired-hook "
                       "rows possible")
        return rows

    # Retired: registry says retired=true. These are exactly the ones NOT in wired_keys
    # (the registry's own field is authoritative; settings.json agreeing is a consistency
    # check, not a second source of truth).
    for r in reg_entries:
        if not r.get("retired"):
            continue
        key = (r["event"], r["name"])
        if key in wired_keys:
            continue  # inconsistent registry (marked retired but still wired) — trust wiring
        layer = classify_hook_layer(r["event"], r.get("intent", {}).get("effect"))
        reason = r.get("retired_reason") or "marked retired in bin/hook-registry.json (no reason on file)"
        rows.append({
            "id": f"hook:{r['event']}:{r['name']}", "rung": "hook", "seat": None,
            "name": r["name"], "event": r["event"], "layer": layer,
            "owner_file": f".claude/hooks/{r['name']}.py", "evidence": "retired in bin/hook-registry.json",
            "reachable": False, "retired_reason": _first_sentence(reason),
        })
    return rows


# ---------------------------------------------------------------------------
# skills / commands / agents — small curated layer tables; unknown -> honest default
# ---------------------------------------------------------------------------
_SKILL_LAYER = {"claude-brain": "Sense", "codex-routing-detail": "Judge"}
_COMMAND_LAYER = {
    "close-core": "Act", "core-si": "Learn", "deep-plan": "Judge", "handoff": "Act",
    "health": "Sense", "rebuild-graph": "Learn", "recall-similar": "Sense",
    "refresh-truth": "Learn", "retire-legacy": "Act", "ship": "Act", "sync": "Act",
}
_AGENT_LAYER = {"close-reconciler": "Learn", "sentinel": "Police", "sentinel-code": "Police"}


def gather_skills(root: Path) -> list[dict]:
    out = []
    d = root / ".claude" / "skills"
    if not d.is_dir():
        return out
    for sd in sorted(p for p in d.iterdir() if p.is_dir()):
        sp = sd / "SKILL.md"
        if not sp.is_file():
            continue
        try:
            text = sp.read_text()
        except Exception:
            text = ""
        desc = _frontmatter_field(text, "description") or ""
        out.append({
            "id": f"skill:{sd.name}", "rung": "skill", "seat": None, "name": sd.name,
            "event": None, "layer": _SKILL_LAYER.get(sd.name, "Judge"),
            "owner_file": f".claude/skills/{sd.name}/SKILL.md",
            "evidence": _first_sentence(desc) if desc else "SKILL.md present, no description field",
            "reachable": True, "retired_reason": None,
        })
    return out


def gather_commands(root: Path) -> list[dict]:
    out = []
    d = root / ".claude" / "commands"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        try:
            text = f.read_text()
        except Exception:
            text = ""
        desc = _frontmatter_field(text, "description")
        out.append({
            "id": f"command:{f.stem}", "rung": "command", "seat": None, "name": f"/{f.stem}",
            "event": None, "layer": _COMMAND_LAYER.get(f.stem, "Judge"),
            "owner_file": f".claude/commands/{f.name}",
            "evidence": _first_sentence(desc) if desc else "command file present, no description field",
            "reachable": True, "retired_reason": None,
        })
    return out


def gather_agents(root: Path) -> list[dict]:
    out = []
    d = root / ".claude" / "agents"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        out.append({
            "id": f"agent:{f.stem}", "rung": "agent", "seat": None, "name": f.stem,
            "event": None, "layer": _AGENT_LAYER.get(f.stem, "Judge"),
            "owner_file": f".claude/agents/{f.name}",
            "evidence": "native flat-format agent spec — the format Claude Code loads",
            "reachable": True, "retired_reason": None,
        })
    for sd in sorted(p for p in d.iterdir() if p.is_dir()):
        if (sd / "CLAUDE.md").is_file():
            out.append({
                "id": f"agent:{sd.name}:dir-form", "rung": "agent", "seat": None,
                "name": sd.name, "event": None, "layer": _AGENT_LAYER.get(sd.name, "Judge"),
                "owner_file": f".claude/agents/{sd.name}/CLAUDE.md",
                "evidence": "dir-form spec, pre-dates the native flat-.md agent format",
                "reachable": False,
                "retired_reason": "dir-form specs survived past the native-format migration; "
                                   "Claude Code loads the flat .md, not this file — it does not "
                                   "run",
            })
    return out


def gather_mcp(root: Path, errors: list) -> list[dict]:
    out = []
    live = root / ".mcp.json"
    tmpl = root / ".mcp.json.template"
    src, is_template = (live, False) if live.is_file() else (tmpl, True)
    cfg = _load_json(src, errors, src.name)
    if cfg is None:
        return out
    for name, entry in (cfg.get("mcpServers") or {}).items():
        if name.startswith("_"):
            continue  # documentation example, not a registered server
        transport = entry.get("type") or ("http" if entry.get("url") else "stdio")
        note = " (template default — not a live .mcp.json on this seat)" if is_template else ""
        out.append({
            "id": f"mcp:{name}", "rung": "mcp", "seat": None, "name": name,
            "event": None, "layer": "Sense",
            "owner_file": f"{src.name}",
            "evidence": f"{transport} MCP server registered in {src.name}{note}",
            "reachable": True, "retired_reason": None,
        })
    return out


def gather_daemons(root: Path, identity: dict, errors: list) -> list[dict]:
    out = []
    prefix = identity.get("launchagent_label_prefix", "")
    if not prefix:
        return out  # honest empty — a fresh identity.json ships with no daemons configured
    la = Path.home() / "Library" / "LaunchAgents"
    try:
        for p in sorted(la.glob(f"{prefix}.*.plist")):
            out.append({
                "id": f"daemon:{p.stem}", "rung": "daemon", "seat": None, "name": p.stem,
                "event": None, "layer": "Act",
                "owner_file": str(p), "evidence": f"registered LaunchAgent {p.name}",
                "reachable": True, "retired_reason": None,
            })
    except Exception as e:
        errors.append(f"launchd discovery: {e.__class__.__name__}: {e}")
    return out


# ---------------------------------------------------------------------------
# SI artifacts / learned contracts — DATA, not architecture. Count only, never content.
# ---------------------------------------------------------------------------
def gather_si_summary(root: Path, identity: dict, errors: list) -> dict:
    summary = {"available": False, "si_artifacts": None, "si_artifacts_active": None,
               "learned_contracts": None, "learned_contracts_active": None}
    try:
        import psycopg2  # noqa: local import — optional dependency
    except Exception as e:
        errors.append(f"si_summary: psycopg2 unavailable ({e.__class__.__name__}) — "
                       "SI artifact/learned contract counts skipped")
        return summary

    org_id = identity.get("org_id", 1)
    dbname = os.environ.get("COREBRAIN_DB", "corebrain")
    try:
        conn = psycopg2.connect(dbname=dbname, connect_timeout=5)
    except Exception as e:
        errors.append(f"si_summary: cannot connect to Postgres db={dbname} "
                       f"({e.__class__.__name__}) — SI artifact/learned contract counts skipped")
        return summary

    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET app.current_org_id = %s", (str(org_id),))
            try:
                cur.execute("SELECT count(*), count(*) FILTER (WHERE active AND NOT quarantined) "
                            "FROM si_artifacts WHERE org_id = %s", (org_id,))
                total, active = cur.fetchone()
                summary["si_artifacts"], summary["si_artifacts_active"] = total, active
            except Exception as e:
                conn.rollback()
                errors.append(f"si_summary: si_artifacts query failed ({e.__class__.__name__})")
            try:
                cur.execute("SELECT count(*), count(*) FILTER (WHERE active) "
                            "FROM learned_contracts WHERE org_id = %s", (org_id,))
                total, active = cur.fetchone()
                summary["learned_contracts"], summary["learned_contracts_active"] = total, active
            except Exception as e:
                conn.rollback()
                errors.append(f"si_summary: learned_contracts query failed ({e.__class__.__name__})")
        summary["available"] = summary["si_artifacts"] is not None or \
            summary["learned_contracts"] is not None
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def build_roster(root: Path) -> dict:
    errors: list[str] = []
    identity = _load_json(root / ".claude" / "identity.json", errors, "identity.json") or {}
    seat = _seat(root, identity)

    rows = []
    rows += gather_hooks(root, errors)
    rows += gather_skills(root)
    rows += gather_commands(root)
    rows += gather_agents(root)
    rows += gather_mcp(root, errors)
    rows += gather_daemons(root, identity, errors)
    for r in rows:
        r["seat"] = seat

    si_summary = gather_si_summary(root, identity, errors)

    by_layer: dict[str, int] = {}
    by_rung: dict[str, int] = {}
    reachable_n = retired_n = 0
    for r in rows:
        by_layer[r["layer"]] = by_layer.get(r["layer"], 0) + 1
        by_rung[r["rung"]] = by_rung.get(r["rung"], 0) + 1
        if r["reachable"]:
            reachable_n += 1
        else:
            retired_n += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "generated_at": os.environ.get("GEN_ROSTER_DATE") or datetime.now(timezone.utc).isoformat(),
        "seat": seat,
        "rows": rows,
        "si_summary": si_summary,
        "counts": {"total": len(rows), "reachable": reachable_n, "retired": retired_n,
                   "by_layer": by_layer, "by_rung": by_rung},
        "errors": errors,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    args = ap.parse_args()

    root = _root()
    roster = build_roster(root)
    text = json.dumps(roster, indent=2)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(out)
        print(f"[gen-arch-roster] wrote {out} — {roster['counts']['total']} capabilities "
              f"({roster['counts']['reachable']} reachable, {roster['counts']['retired']} "
              f"retired), {len(roster['errors'])} error(s)", file=sys.stderr)
    else:
        print(text)
    sys.exit(0)


if __name__ == "__main__":
    main()
