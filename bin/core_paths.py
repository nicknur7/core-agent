"""Core path registry (Python loader).

Reads every tracked path from bin/core-paths.json (single source of truth)
and exposes them as module-level Path constants. Shell loader is
bin/core-paths.sh — both read the same JSON, so drift between the two is
structurally impossible.

Importing pattern:
    sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "bin"))
    import core_paths
    # then use core_paths.LAST_SESSION_START, etc.

Edit bin/core-paths.json to add or move a tracked path — this loader will
expose new keys automatically.

Last updated: 2026-05-15 (JSON refactor — replaces literal definitions).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# ── REPO ROOT ───────────────────────────────────────────────────────────────
# ONE RESOLVER. This was a fifth independent answer to "which Core am I", and its acceptance test was
# `Path(CORE_INSTANCE).exists()` — ANY existing directory qualified. Pointed at
# <core>/memory it accepted the value and then crashed loading bin/core-paths.json from inside it.
#
# Crashing is the safe direction and it is still the wrong resolver: the value should have been
# REJECTED, not accepted-then-fatal. core_seat.seat_root() requires the identity marker, so a
# non-Core path falls through to the fallback instead of being adopted.
#
# core-business, #914: "fixing hardcoded by writing a second resolver is the two-implementations
# defect." Consolidating three left a fourth (gate_tier_b) and a fifth (this).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core_seat import seat_root                                      # noqa: E402
INSTANCE = seat_root(fallback=Path(__file__).resolve().parents[1])


# ── LOAD JSON ───────────────────────────────────────────────────────────────
_JSON_PATH = INSTANCE / "bin" / "core-paths.json"
with open(_JSON_PATH) as _f:
    _DATA = json.load(_f)


def _resolve(rel: str) -> Path:
    """Repo-relative paths become absolute Paths under $CORE_INSTANCE."""
    if rel.startswith("/"):
        return Path(rel)
    return INSTANCE / rel


# ── EXPOSE EVERY KEY AS A MODULE CONSTANT ───────────────────────────────────
# ALL_TRACKED is consumed by bin/lint-code-paths.py — only specific files
# belong in it, not broad directory paths. Directory entries (keys ending in
# _DIR or _GLOB) are still exposed as module constants for use in code, but
# excluded from ALL_TRACKED so the lint doesn't flag every reference to
# "memory/" or ".claude/hooks/" as drift.
_LINT_EXCLUDE_SUFFIXES = ("_DIR", "_GLOB")
ALL_TRACKED: dict[str, str] = {}
_ns = globals()
for _section, _entries in _DATA.items():
    if _section.startswith("_") or not isinstance(_entries, dict):
        continue
    for _key, _rel in _entries.items():
        if not isinstance(_rel, str):
            continue
        _path = _resolve(_rel)
        _ns[_key] = _path
        if not any(_key.endswith(suf) for suf in _LINT_EXCLUDE_SUFFIXES):
            ALL_TRACKED[_key] = str(_path)


# ── HELPERS ─────────────────────────────────────────────────────────────────
def session_log_for(date_str: str) -> Path:
    """Path to a per-day session log. `date_str` must be `YYYY-MM-DD`."""
    return SESSIONS_DIR / f"{date_str}.md"  # noqa: F821


def rotated_set(path: Path) -> list[Path]:
    """Every existing generation of a rotated log, OLDEST FIRST, so reading them in order
    reproduces the original append sequence.

        hook-events.log.2  ->  hook-events.log.1  ->  hook-events.log

    WHY (2026-08-12). Four independent readers opened `.claude/state/hook-events.log` directly:
    grade-gate.py:182, refresh-hook-dispositions.py:41, steering-ledger.py:67 and
    scheduling/brain-pg/steering_ingest.py. Rotation moved the bulk of history into
    `hook-events.log.1` (1.0 MB) and left 1,906 lines live — so every reader silently measured the
    tail and reported it as the whole record. Nothing errored; the numbers just got quietly smaller.

    That is the defect class this system keeps hitting: the SENSOR was fine and the READER was
    wrong. A hook telemetry log whose readers see the last few minutes is indistinguishable from a
    fleet where almost nothing fires — and "is everything firing?" is the exact question the log was
    built in 2026-06-22 to make answerable.

    ONE resolver rather than four globs, deliberately. Four copies of a rotation rule is four places
    for the next generation suffix to be missed, and "the same fact computed in two places drifts"
    is defect class 2 in this Core's own catalogue.

    Numeric suffixes sort NUMERICALLY, not lexically: `.10` must come before `.2` in age order, and
    a string sort puts it after. Non-numeric siblings (`.log.gz`, `.log.bak`) are ignored rather
    than guessed at — an unreadable generation should be visibly absent, not silently mixed in.
    """
    live = Path(path)
    gens: list[tuple[int, Path]] = []
    for sib in live.parent.glob(live.name + ".*"):
        suffix = sib.name[len(live.name) + 1:]
        if suffix.isdigit():
            gens.append((int(suffix), sib))
    out = [p for _, p in sorted(gens, reverse=True)]   # .2, .1  (oldest first)
    if live.exists():
        out.append(live)                               # live is always newest
    return out


def read_rotated(path: Path) -> str:
    """Full text of a rotated log across every generation, oldest first. See rotated_set()."""
    return "".join(p.read_text(errors="replace") for p in rotated_set(path))


if __name__ == "__main__":
    # Self-check: dump every tracked path so a human can sanity-scan.
    print(json.dumps(
        {"INSTANCE": str(INSTANCE), "tracked": ALL_TRACKED},
        indent=2,
    ))
