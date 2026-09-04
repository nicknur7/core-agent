#!/usr/bin/env python3
"""Per-Core peer MCP server — exposes this Core's memory/, tasks/, sessions/ read-only.

Each Core runs ONE instance of this server; other Cores connect to it via
their `.mcp.json` to read this Core's current state without going through
the brain (which is historical/derived). Read-only by design — no write
tools across Cores (keeps Sentinel boundaries clean).

Allowlist:
- DIRS:  memory/, tasks/, sessions/
- EXTS:  .md, .json
- NEVER: .claude/state/, secrets/, identity.json, .env, .git/

Path traversal rejected via `is_relative_to(REPO_ROOT)`.  # privacy-ok: generic engineering vocabulary

Spec: tasks/specs/spec-multi-core-architecture-2026-05-19.md Phase 7.5.

Run standalone (stdio):
    CORE_DOMAIN_LABEL=life CLAUDE_PROJECT_DIR=/Users/.../core-life \\
        uv run --python 3.12 --with mcp python peer-mcp-server.py
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write(
        "ERROR: mcp Python SDK not installed. Install via uv run --with mcp ...\n"
    )
    sys.exit(1)


REPO_ROOT = Path(
    os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parent.parent
).resolve()

ALLOWED_DIRS = {"memory", "tasks", "sessions"}
ALLOWED_EXTS = {".md", ".json"}
DOMAIN = os.environ.get("CORE_DOMAIN_LABEL", "unknown")

mcp = FastMCP(f"peer-{DOMAIN}")


def _safe_resolve(rel_path: str) -> Path | None:
    """Resolve `rel_path` against REPO_ROOT. Reject:
    - paths outside REPO_ROOT (traversal),
    - paths whose first component isn't in ALLOWED_DIRS,
    - files whose extension isn't in ALLOWED_EXTS.

    Returns the resolved Path on success, None on rejection.
    """
    try:
        p = (REPO_ROOT / rel_path).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        rel = p.relative_to(REPO_ROOT)
    except ValueError:
        return None  # outside REPO_ROOT
    if not rel.parts or rel.parts[0] not in ALLOWED_DIRS:
        return None
    if p.is_file() and p.suffix not in ALLOWED_EXTS:
        return None
    return p


@mcp.tool()
def peer_read(path: str) -> str:
    """Read a file from this Core's memory/, tasks/, or sessions/ tree (read-only).

    Args:
        path: relative path from repo root (e.g. 'memory/current-state.md').

    Returns:
        File contents, or 'ERROR: <reason>' string on failure.
    """
    p = _safe_resolve(path)
    if not p:
        return f"ERROR: path not accessible (outside allowlist or traversal): {path}"
    if not p.is_file():
        return f"ERROR: file does not exist: {path}"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"ERROR: read failed: {e}"


@mcp.tool()
def peer_read_current_state() -> str:
    """Shortcut for the peer's core_paths.MEM_CURRENT_STATE (memory/current-state.md)."""
    return peer_read("memory/current-state.md")  # peer-relative path mirroring core_paths.MEM_CURRENT_STATE


@mcp.tool()
def peer_list_pending() -> str:
    """Shortcut for the peer's core_paths.MEM_PENDING (memory/pending.md) content."""
    return peer_read("memory/pending.md")  # peer-relative path mirroring core_paths.MEM_PENDING


@mcp.tool()
def peer_list_recent_sessions(days: int = 7) -> list[str]:
    """List session filenames written within the last N days (mtime-based).

    Args:
        days: window in days (default 7).

    Returns:
        Sorted list of session filenames (e.g. ['2026-05-19.md', ...]).
    """
    sessions_dir = REPO_ROOT / "sessions"
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return []
    cutoff = time.time() - days * 86400
    out: list[str] = []
    for p in sessions_dir.glob("*.md"):
        try:
            if p.stat().st_mtime > cutoff:
                out.append(p.name)
        except OSError:
            continue
    return sorted(out)


@mcp.tool()
def peer_list_active_projects() -> list[dict]:
    """List project hubs with frontmatter Status + Last updated (best-effort parse).

    Returns:
        [{name, status, last_updated}, ...] for each *.md in memory/projects/.
    """
    proj_dir = REPO_ROOT / "memory" / "projects"
    out: list[dict] = []
    if not proj_dir.exists():
        return out
    for f in proj_dir.glob("*.md"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:2000]  # frontmatter window
        except OSError:
            continue
        status = ""
        last_updated = ""
        for line in text.splitlines():
            ls = line.strip()
            if ls.lower().startswith("status:"):
                status = ls.partition(":")[2].strip()
            elif ls.lower().startswith("last updated:"):
                last_updated = ls.partition(":")[2].strip()
            if status and last_updated:
                break
        out.append({"name": f.stem, "status": status, "last_updated": last_updated})
    return sorted(out, key=lambda d: d["name"])


if __name__ == "__main__":
    mcp.run()
