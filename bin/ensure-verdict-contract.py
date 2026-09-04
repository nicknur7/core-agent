#!/usr/bin/env python3
"""ensure-verdict-contract.py — every sentinel spec in THIS Core carries the canonical
machine-readable verdict contract, byte-identical.

WHY THIS IS A SCRIPT AND NOT TEN HAND EDITS
-------------------------------------------
`.claude/agents/sentinel.md` and `.claude/agents/sentinel-code.md` are `per_core_keep`:
`/sync` never touches them, in either direction. So the verdict contract — which is NOT a
per-Core customisation but the anti-forgery property `sentinel-receipt.sh` depends on —
drifted to ZERO occurrences on school, finance, ops and the baseline while working fine on
life and business.

That is exactly the signature the peers found in `test_org_isolation.py`: a shared constant
only ever exercised where it happens to be true. Hand-patching the ten files re-creates the
drift the first time anyone edits one. One canonical source plus an idempotent installer
does not.

Canonical text: `bin/verdict-contract.md` (in `bin/`, which IS synced fleet-wide, so every
Core receives the text even though its own specs are never overwritten).

WHAT IT DOES
------------
For each flat `.claude/agents/sentinel*.md`:
  1. strips any existing verdict-contract section, whatever revision it is
  2. rewrites the `Nothing else. No preamble.` line, which flatly contradicts a required
     trailing marker line, to name the marker as the one exception
  3. inserts the canonical block immediately after it

Idempotent: a second run is a no-op. `--check` reports drift and writes nothing (exit 1 if
any file is out of date), which is what the close calls so this self-heals silently instead
of becoming another notification.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Matched against a WHOLE LINE, never as a bare substring. sentinel-code.md's line reads
# `Nothing else. No preamble. No "here is my analysis."` — a substring anchor inserted the
# block mid-line and orphaned the tail, which is why this is line-based.
ANCHOR = "Nothing else. No preamble."
ANCHOR_SENTENCE = "The single exception is the verdict marker below, which is REQUIRED."

# Any revision of the block, so an older rendering is replaced rather than duplicated.
_BLOCK_START = re.compile(
    r"^###\s+(?:The verdict marker|MACHINE-READABLE VERDICT)\b", re.M
)
# A block ends at the next level-2 heading, or EOF (life's old copy was a trailing appendix).
_NEXT_H2 = re.compile(r"^##\s+\S", re.M)


def canonical_text(core: Path) -> str:
    src = core / "bin" / "verdict-contract.md"
    if not src.is_file():
        sys.exit(f"FATAL: canonical contract missing at {src}")
    return src.read_text(encoding="utf-8").strip("\n")


def strip_existing(text: str) -> str:
    """Remove every existing verdict-contract section, any revision."""
    while True:
        m = _BLOCK_START.search(text)
        if not m:
            return text
        nxt = _NEXT_H2.search(text, m.end())
        end = nxt.start() if nxt else len(text)
        text = text[: m.start()] + text[end:]


def render(text: str, canon: str) -> str:
    lines = strip_existing(text).split("\n")

    idx = next((i for i, ln in enumerate(lines) if ln.strip().startswith(ANCHOR)), None)
    if idx is None:
        return ""  # caller reports the miss; never guess an insertion point

    # Append the exception clause to the WHOLE anchor line, preserving anything else on it.
    if ANCHOR_SENTENCE not in lines[idx]:
        lines[idx] = lines[idx].rstrip() + " " + ANCHOR_SENTENCE

    head = lines[: idx + 1]
    tail = lines[idx + 1 :]
    while tail and not tail[0].strip():
        tail.pop(0)

    out = head + ["", canon, ""] + tail
    return "\n".join(out)


def specs(core: Path) -> list[Path]:
    # Flat native-format specs only. The dir-form `sentinel/CLAUDE.md` files do not load
    # (see .claude/rules/privacy.md) — stamping a contract into them would be theatre.
    return sorted(p for p in (core / ".claude" / "agents").glob("sentinel*.md") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=".", help="Core root (default: cwd)")
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    ap.add_argument("--quiet", action="store_true", help="print only on drift or error")
    a = ap.parse_args()

    core = Path(a.core).resolve()
    canon = canonical_text(core)
    found = specs(core)
    if not found:
        print(f"[verdict-contract] no sentinel specs under {core}/.claude/agents", file=sys.stderr)
        return 0

    drifted, failed = [], []
    for p in found:
        before = p.read_text(encoding="utf-8")
        after = render(before, canon)
        if not after:
            failed.append(p)
            print(f"  ANCHOR-MISS  {p.relative_to(core)} — no '{ANCHOR}' line; not modified",
                  file=sys.stderr)
            continue
        if after == before:
            if not a.quiet:
                print(f"  ok           {p.relative_to(core)}")
            continue
        drifted.append(p)
        if a.check:
            print(f"  DRIFT        {p.relative_to(core)}")
        else:
            p.write_text(after, encoding="utf-8")
            print(f"  INSTALLED    {p.relative_to(core)}")

    if failed:
        return 2
    if a.check and drifted:
        return 1
    if not a.quiet and not drifted:
        print(f"[verdict-contract] {core.name}: {len(found)} spec(s) current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
