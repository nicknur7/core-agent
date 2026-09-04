#!/usr/bin/env python3
"""Cap memory/current-state.md to the N newest session blocks.

WHY THIS EXISTS (2026-05-24): current-state.md repeatedly bloated to 400+ lines /
16 session blocks because the prune step was gated behind an explicit /close-core.
Any session that ended via the unattended defensive-save (Nick walks away) never
pruned, so the file grew unbounded and taxed ~20-33K tokens onto every SessionStart
auto-load. This makes the prune mechanical and runs it on EVERY session end.

CONTRACT (deliberately narrow + fail-safe):
- Operates ONLY inside the "## Latest sessions" section. Every other section is
  byte-for-byte untouched.
- A session block starts with a bold ISO-date line:  **YYYY-MM-DD ...
  Blocks are assumed reverse-chronological (newest first), which is the file's
  convention.
- Keeps the KEEP newest blocks; replaces everything from the (KEEP+1)th block to
  the section boundary with a single pointer line. Full detail of dropped blocks
  lives in sessions/YYYY-MM-DD.md (git-tracked) — nothing is lost.
- Section boundary = the first "## " header OR standalone "---" after the
  "## Latest sessions" header, whichever comes first (so the separator survives).
- If the section header isn't found, or block count <= KEEP, makes NO change and
  exits 0. Idempotent: running twice is a no-op.

Usage: prune-current-state.py [path]   (default: <repo>/memory/current-state.md)
"""
import datetime
import re
import os
import sys
from pathlib import Path

KEEP = 2
SECTION_HEADER = "## Latest sessions"
BLOCK_RX = re.compile(r"^\*\*\d{4}-\d{2}-\d{2}")
BOUNDARY_RX = re.compile(r"^(## |---\s*$)")


def resolve_path() -> Path:
    """Which Core's current-state.md to prune. Explicit argument wins, then env, then __file__.

    THIS ONE PRUNES A MEMORY FILE, so resolving the wrong Core does not report a wrong number —
    it EDITS ANOTHER CORE'S MEMORY. Last of the eleven cross-seat anchor defects; left out of the
    batch fix because it resolves inside a function rather than at module scope, and force-fitting
    a module-level pattern into it was how the batch would have broken something.
    """
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    env = os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        p = Path(env).expanduser()
        if (p / ".claude").is_dir():
            return p.resolve() / "memory" / "current-state.md"
    repo = Path(__file__).resolve().parent.parent
    return repo / "memory" / "current-state.md"


def main() -> int:
    path = resolve_path()
    if not path.is_file():
        print(f"prune-current-state: no file at {path}; nothing to do.")
        return 0

    lines = path.read_text().splitlines(keepends=True)

    # Locate the section header.
    try:
        hdr = next(i for i, ln in enumerate(lines) if ln.strip() == SECTION_HEADER)
    except StopIteration:
        print("prune-current-state: '## Latest sessions' not found; no change (fail-safe).")
        return 0

    # Section end = first boundary line strictly after the header.
    end = len(lines)
    for i in range(hdr + 1, len(lines)):
        if BOUNDARY_RX.match(lines[i]):
            end = i
            break

    # Find block starts inside (hdr, end).
    block_starts = [i for i in range(hdr + 1, end) if BLOCK_RX.match(lines[i])]
    if len(block_starts) <= KEEP:
        print(f"prune-current-state: {len(block_starts)} block(s) <= cap {KEEP}; no change.")
        return 0

    cut_from = block_starts[KEEP]
    today = datetime.date.today().isoformat()
    pointer = (
        f"_Older session blocks pruned {today} — full detail in `sessions/YYYY-MM-DD.md`._\n"
    )

    new_lines = lines[:cut_from] + [pointer, "\n"] + lines[end:]
    path.write_text("".join(new_lines))
    removed = len(block_starts) - KEEP
    print(
        f"prune-current-state: kept {KEEP} newest, pruned {removed} older block(s) "
        f"from {path.name} ({len(lines)} -> {len(new_lines)} lines)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
