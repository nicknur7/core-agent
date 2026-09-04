#!/usr/bin/env python3
"""gen-core-manifest.py — the single discovery engine for "what Core has".

WHY: three parallel partial inventories (gen-capabilities.py, core-ux stack.ts,
services.ts) each drifted independently and NONE saw user-scope plugins — so Codex
was invisible everywhere. This is the one discovery pass. It emits versioned,
machine-readable manifests that every renderer (capabilities.md, the Core UX
dashboard, system-rundown, Atlas) reads FROM. Renderers must never re-scan the
filesystem themselves. Derived-from-truth can't rot.

EMITS (into <core>/memory/):
  core-manifest.json   — THIS Core's project-scoped inventory (commands, agents,
                         skills, hooks, mcp, daemons).
  host-manifest.json   — user/host-scoped inventory (plugins + their components,
                         user skills, user mcp). Host-wide; life is canonical.
  fleet-manifest.json  — deterministic aggregate of every per-Core manifest on
                         disk + the host manifest. Only meaningful from a Core that
                         can see its siblings (life). The dashboard consumes this.

DESIGN INVARIANTS (from the 2026-07-12 Core+Codex design review):
  - Scope is a first-class fact: core vs host vs fleet are different manifests.
  - Canonical structural IDs, never display names (`command:plugin:codex@openai-codex:review`).
  - Errors are DATA, not swallowed: `errors[]` + `sources_unavailable[]`. An
    unreadable source is distinguishable from "nothing installed".
  - Atomic write (temp→rename) + retain last-known-good: a discovery that comes back
    empty-with-errors never overwrites a good manifest with a fake-empty one.
  - Sanitized: NEVER serialize `.mcp.json` env/args, hook command lines, or tokens.
  - `fingerprint` (sha256 over inventory only) is separate from `generated_at`, so an
    unchanged inventory produces an identical fingerprint → no spurious re-publish.

Plugin discovery contract: `claude plugin list --json` (effective install state:
scope/enabled/projectPath/installPath) → walk the returned installPath for typed
components. NOT marketplace dirs (source material, may be stale/disabled). Falls back
to ~/.claude/plugins/installed_plugins.json marked `degraded` if the CLI is unavailable.

Shared baseline file (bin/), per-Core-safe by construction (reads each Core's own
root; host discovery is host-wide so identical everywhere). No args needed; honors
$CORE_INSTANCE, else git toplevel, else cwd.  `--check` = dry-run (exit 10 if changed).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
GENERATOR_VERSION = "1.0.0"

# Hook script/command basename patterns we recognize (generalized beyond .sh/.py to
# catch plugin hooks like Codex's node .mjs — the gap in gen-capabilities._hooks).
_SCRIPT_RE = re.compile(r"([\w.\-/]+\.(?:sh|py|mjs|js|ts))")
_ORDERED_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse",
                   "PostToolUse", "Stop", "SessionEnd", "Notification", "PreCompact"]


# ---------------------------------------------------------------------------
# small io helpers (all fail-soft; failures are recorded as errors, not raised)
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
    """Return parsed json, or {} — but only push an error if the file EXISTS and
    failed to parse (a missing optional source is `sources_unavailable`, not an error)."""
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        errors.append(f"{label}: unreadable ({e.__class__.__name__})")
        return {}


def _command_detail(text: str) -> str | None:
    """One-line purpose from a command/skill .md (frontmatter description / first prose)."""
    fm = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if fm:
        d = re.search(r"^description:\s*(.+)$", fm.group(1), re.MULTILINE)
        if d:
            return d.group(1).strip().strip("\"'")[:140]
    body = re.sub(r"^---\n.*?\n---", "", text, flags=re.DOTALL)
    for line in body.splitlines():
        s = re.sub(r"^#+\s*", "", line).replace("**", "").strip()
        if s and not s.startswith("<") and len(s) > 8:
            return s[:140]
    return None


def _retired(text: str) -> bool:
    return bool(re.search(r"\bRETIRED\b|\bdeprecated\b", text[:800], re.IGNORECASE))


# ---------------------------------------------------------------------------
# core-scoped discovery (this Core's own .claude + .mcp.json + launchd)
# ---------------------------------------------------------------------------
def _core_commands(claude: Path, core_id: str, errors: list):
    out = []
    d = claude / "commands"
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            t = ""
            try:
                t = f.read_text()
            except Exception as e:
                errors.append(f"command {f.name}: {e.__class__.__name__}")
            name = f.stem
            out.append({"id": f"command:core:{core_id}:{name}", "name": "/" + name,
                        "source": "core", "scope": core_id,
                        "detail": _command_detail(t), "retired": _retired(t)})
    return out


def _core_agents(claude: Path, core_id: str, errors: list):
    out = []
    d = claude / "agents"
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            t = ""
            try:
                t = f.read_text()
            except Exception:
                pass
            out.append({"id": f"agent:core:{core_id}:{f.stem}", "name": f.stem,
                        "source": "core", "scope": core_id, "detail": _command_detail(t)})
    return out


def _core_skills(claude: Path, core_id: str):
    out = []
    d = claude / "skills"
    if d.is_dir():
        for sd in sorted(d.iterdir()):
            sp = sd / "SKILL.md"
            if sp.is_file():
                t = ""
                try:
                    t = sp.read_text()
                except Exception:
                    pass
                out.append({"id": f"skill:core:{core_id}:{sd.name}", "name": sd.name,
                            "source": "core", "scope": core_id, "detail": _command_detail(t)})
    return out


def _core_hooks(settings: dict, core_id: str):
    """Structured hooks from settings.json — captures event + every script basename,
    including .mjs/.js (the gap in gen-capabilities' sh|py-only regex)."""
    out = []
    for ev, groups in (settings.get("hooks") or {}).items():
        scripts = []
        for grp in groups or []:
            for h in grp.get("hooks", []) or []:
                cmd = h.get("command", "")
                m = _SCRIPT_RE.search(cmd)
                base = Path(m.group(1)).name if m else (cmd.split()[0].rsplit("/", 1)[-1] if cmd else "?")
                if base not in scripts:
                    scripts.append(base)
        for base in sorted(scripts):
            out.append({"id": f"hook:core:{core_id}:{ev}:{base}", "name": base,
                        "event": ev, "source": "core", "scope": core_id})
    return out


def _core_mcp(mcp_json: dict, core_id: str):
    """MCP servers from this Core's .mcp.json — SANITIZED: name + transport only.
    NEVER env/args/tokens."""
    out = []
    for name, cfg in sorted((mcp_json.get("mcpServers") or {}).items()):
        transport = cfg.get("type") or ("http" if cfg.get("url") else "stdio")
        out.append({"id": f"mcp:{core_id}/{name}", "name": name, "source": "core",
                    "scope": core_id, "transport": transport,
                    "wiring": [core_id]})
    return out


def _launchd(prefix: str, errors: list):
    if not prefix:
        return []
    la = Path.home() / "Library" / "LaunchAgents"
    try:
        return [{"id": f"daemon:{p.stem}", "name": p.stem}
                for p in sorted(la.glob(f"{prefix}.*.plist"))]
    except Exception as e:
        errors.append(f"launchd: {e.__class__.__name__}")
        return []


def discover_core(root: Path) -> dict:
    errors, unavailable = [], []
    claude = root / ".claude"
    identity = _load_json(claude / "identity.json", errors, "identity.json") or {}
    settings = _load_json(claude / "settings.json", errors, "settings.json") or {}
    mcp_json = _load_json(root / ".mcp.json", errors, ".mcp.json")
    if mcp_json is None:
        unavailable.append(".mcp.json")
        mcp_json = {}
    core_id = identity.get("domain_label") or re.sub(r"^core-", "", root.name) or "?"
    prefix = identity.get("launchagent_label_prefix", "")

    inv = {
        "commands": _core_commands(claude, core_id, errors),
        "agents": _core_agents(claude, core_id, errors),
        "skills": _core_skills(claude, core_id),
        "hooks": _core_hooks(settings, core_id),
        "mcp": _core_mcp(mcp_json, core_id),
        "daemons": _launchd(prefix, errors),
    }
    return _wrap("core", core_id, inv, errors, unavailable)


# ---------------------------------------------------------------------------
# host-scoped discovery (plugins via CLI + user skills + user mcp)
# ---------------------------------------------------------------------------
def _plugin_list(errors: list):
    """Primary contract: `claude plugin list --json`. Fallback: installed_plugins.json
    (marked degraded). Returns (list, degraded_bool)."""
    try:
        out = subprocess.run(["claude", "plugin", "list", "--json"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout), False
        errors.append(f"claude plugin list: exit {out.returncode}")
    except Exception as e:
        errors.append(f"claude plugin list: {e.__class__.__name__}")
    # degraded fallback — internal persistence format
    reg = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        raw = json.loads(reg.read_text())
        flat = []
        for pid, entries in (raw.get("plugins") or {}).items():
            for e in entries:
                flat.append({"id": pid, "version": e.get("version"),
                             "scope": e.get("scope"), "enabled": True,
                             "installPath": e.get("installPath"),
                             "projectPath": e.get("projectPath")})
        return flat, True
    except Exception as e:
        errors.append(f"installed_plugins.json fallback: {e.__class__.__name__}")
        return [], True


def _walk_plugin_components(install_path: str, pid: str, errors: list):
    """Typed enumeration by walking the resolved installPath (NOT the marketplace)."""
    ip = Path(install_path) if install_path else None
    comps = {"commands": [], "skills": [], "agents": [], "hooks": []}
    if not ip or not ip.is_dir():
        if install_path:
            errors.append(f"{pid}: installPath missing {install_path}")
        return comps
    for f in sorted((ip / "commands").glob("*.md")) if (ip / "commands").is_dir() else []:
        t = ""
        try:
            t = f.read_text()
        except Exception:
            pass
        comps["commands"].append({"id": f"command:plugin:{pid}:{f.stem}",
                                  "name": f"/{pid.split('@')[0]}:{f.stem}",
                                  "source": f"plugin:{pid}", "scope": "host",
                                  "detail": _command_detail(t), "retired": _retired(t)})
    sd = ip / "skills"
    if sd.is_dir():
        for d in sorted(sd.iterdir()):
            sp = d / "SKILL.md"
            if sp.is_file():
                t = ""
                try:
                    t = sp.read_text()
                except Exception:
                    pass
                comps["skills"].append({"id": f"skill:plugin:{pid}:{d.name}", "name": d.name,
                                        "source": f"plugin:{pid}", "scope": "host",
                                        "detail": _command_detail(t)})
    for f in sorted((ip / "agents").glob("*.md")) if (ip / "agents").is_dir() else []:
        comps["agents"].append({"id": f"agent:plugin:{pid}:{f.stem}", "name": f.stem,
                                "source": f"plugin:{pid}", "scope": "host"})
    hj = ip / "hooks" / "hooks.json"
    if hj.is_file():
        try:
            for ev in json.loads(hj.read_text()).get("hooks", {}):
                comps["hooks"].append({"id": f"hook:plugin:{pid}:{ev}", "name": pid.split("@")[0],
                                       "event": ev, "source": f"plugin:{pid}", "scope": "host"})
        except Exception as e:
            errors.append(f"{pid} hooks.json: {e.__class__.__name__}")
    return comps


def discover_host() -> dict:
    errors, unavailable = [], []
    plugins_raw, degraded = _plugin_list(errors)

    plugins, commands, skills, agents, hooks, mcp = [], [], [], [], [], []
    for p in plugins_raw:
        pid = p.get("id")
        enabled = bool(p.get("enabled"))
        comps = _walk_plugin_components(p.get("installPath"), pid, errors) if enabled else \
            {"commands": [], "skills": [], "agents": [], "hooks": []}
        plugins.append({
            "id": f"plugin:{pid}", "name": pid, "version": p.get("version"),
            "scope": p.get("scope"), "enabled": enabled,
            "projectPath": p.get("projectPath"),
            "components": {k: len(v) for k, v in comps.items()},
        })
        # Plugin-provided MCP (declared inline in the plugin list entry, e.g. vercel).
        for mname, mcfg in (p.get("mcpServers") or {}).items():
            transport = mcfg.get("type") or ("http" if mcfg.get("url") else "stdio")
            mcp.append({"id": f"mcp:plugin:{pid}/{mname}", "name": mname,
                        "source": f"plugin:{pid}", "scope": "host",
                        "transport": transport, "wiring": [f"plugin:{pid}"]})
        if enabled:
            commands += comps["commands"]
            skills += comps["skills"]
            agents += comps["agents"]
            hooks += comps["hooks"]

    # User-scope skills (~/.claude/skills) not owned by a plugin.
    usk = Path.home() / ".claude" / "skills"
    if usk.is_dir():
        for d in sorted(usk.iterdir()):
            sp = d / "SKILL.md"
            if sp.is_file():
                t = ""
                try:
                    t = sp.read_text()
                except Exception:
                    pass
                skills.append({"id": f"skill:user:{d.name}", "name": d.name,
                               "source": "user", "scope": "host", "detail": _command_detail(t)})

    # User-scope MCP (~/.claude.json) — sanitized.
    ujson = _load_json(Path.home() / ".claude.json", errors, "~/.claude.json")
    if ujson is None:
        unavailable.append("~/.claude.json")
        ujson = {}
    for name, cfg in sorted((ujson.get("mcpServers") or {}).items()):
        transport = cfg.get("type") or ("http" if cfg.get("url") else "stdio")
        mcp.append({"id": f"mcp:user/{name}", "name": name, "source": "user",
                    "scope": "host", "transport": transport, "wiring": ["user"]})

    inv = {"plugins": plugins, "commands": commands, "skills": skills,
           "agents": agents, "hooks": hooks, "mcp": mcp}
    m = _wrap("host", "host", inv, errors, unavailable)
    if degraded:
        m["discovery_state"] = "degraded"
    return m


# ---------------------------------------------------------------------------
# fleet aggregate (all per-Core manifests on disk + host)
# ---------------------------------------------------------------------------
def build_fleet(root: Path, this_core: dict, host: dict, errors: list) -> dict:
    ai = root.parent
    cores = {}
    for d in sorted(ai.glob("core-*")):
        mp = d / "memory" / "core-manifest.json"
        if mp.is_file():
            try:
                m = json.loads(mp.read_text())
                cores[m.get("scope_id", d.name)] = m.get("inventory", {})
            except Exception as e:
                errors.append(f"fleet: {d.name} core-manifest unreadable ({e.__class__.__name__})")
    # THIS run's own core manifest may not be on disk yet (writes happen after the
    # fleet is built) — seed it from memory so the fleet is self-consistent within a
    # single run and its fingerprint is stable run-to-run.
    cores[this_core["scope_id"]] = this_core.get("inventory", {})
    inv = {"cores": cores, "host": host.get("inventory", {})}
    return _wrap("fleet", "fleet", inv, errors, [])


# ---------------------------------------------------------------------------
# manifest envelope + atomic, last-known-good-preserving write
# ---------------------------------------------------------------------------
def _wrap(scope: str, scope_id: str, inventory: dict, errors: list, unavailable: list) -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "scope": scope,
        "scope_id": scope_id,
        "inventory": inventory,
        "errors": errors,
        "sources_unavailable": unavailable,
    }
    payload["fingerprint"] = _fingerprint(payload)
    payload["generated_at"] = os.environ.get("GEN_MANIFEST_DATE") or \
        datetime.now(timezone.utc).isoformat()
    return payload


def _fingerprint(payload: dict) -> str:
    """sha256 over the content that matters — inventory + errors + scope. Excludes
    generated_at so an unchanged system yields a stable fingerprint (no churn)."""
    core = {k: payload[k] for k in ("schema_version", "scope", "scope_id",
                                    "inventory", "errors", "sources_unavailable")}
    return "sha256:" + hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _is_empty_inventory(m: dict) -> bool:
    inv = m.get("inventory", {})
    return not any(v for v in inv.values())


def write_manifest(path: Path, new: dict) -> str:
    """Atomic write with last-known-good preservation. Returns a status word.
    If the new manifest is empty AND carries errors, and a good prior exists,
    keep the prior (annotated) rather than publish a fake-empty inventory."""
    old = None
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except Exception:
            old = None

    if _is_empty_inventory(new) and new.get("errors") and old and not _is_empty_inventory(old):
        old["last_discovery_failed_at"] = new["generated_at"]
        old["last_discovery_errors"] = new["errors"]
        new = old
        status = "held-last-good"
    elif old and old.get("fingerprint") == new.get("fingerprint"):
        status = "unchanged"
    else:
        status = "changed"

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(new, indent=2))
    tmp.replace(path)
    return status


def main():
    root = _root()
    mem = root / "memory"
    check = "--check" in sys.argv or "--dry-run" in sys.argv

    core = discover_core(root)
    host = discover_host()
    fleet = build_fleet(root, core, host, [])

    targets = [(mem / "core-manifest.json", core),
               (mem / "host-manifest.json", host),
               (mem / "fleet-manifest.json", fleet)]

    if check:
        changed = False
        for path, m in targets:
            old_fp = ""
            if path.exists():
                try:
                    old_fp = json.loads(path.read_text()).get("fingerprint", "")
                except Exception:
                    pass
            c = old_fp != m.get("fingerprint")
            changed = changed or c
            print(f"[gen-core-manifest] {path.name}: {'WOULD CHANGE' if c else 'current'} "
                  f"({m['fingerprint']}, {len(m.get('errors', []))} err)")
        sys.exit(10 if changed else 0)

    for path, m in targets:
        try:
            status = write_manifest(path, m)
            print(f"[gen-core-manifest] {path.name}: {status} "
                  f"({m['fingerprint']}, {len(m.get('errors', []))} err)")
        except Exception as e:
            # fail-open — never block a close
            print(f"[gen-core-manifest] WARN {path.name}: {e}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
