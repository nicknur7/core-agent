"""Who this Core belongs to — read from identity.json, never hardcoded.

WHY THIS EXISTS. The operator's own strip protocol (2026-05-11) named this as item 2 of four:
parameterize hook bodies so every operator-specific list moves to .claude/config.json or env
vars, alongside item 4: genericize CLAUDE.md so it addresses the user by name instead of a
hardcoded one. Neither shipped. Measured 2026-08-29 on a fresh clone of the public baseline: 43 files under
.claude/hooks/ contained the name, and several were RUNTIME strings injected into the session —
so a stranger who forked the repo got an assistant that called them Nick, in its own steering,
every session, forever. Nothing malfunctioned; it simply addressed the wrong person.

The name is per-Core data. It belongs in .claude/identity.json (per_core_keep, never synced) and
nowhere else. Fall back to "the operator" — deliberately generic, and correct on a fresh fork that
has not been personalised yet.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

FALLBACK = "the operator"


def _identity_path() -> Path | None:
    """Walk up from THIS file to the Core root, same anchoring _env.core_root() uses.

    Anchored to the file on disk, not to CORE_INSTANCE: an env var survives a `cd` into a
    sibling Core, which is exactly how one Core's data gets paired with another's name.
    """
    here = Path(__file__).resolve()
    for cand in [here, *here.parents]:
        p = cand / ".claude" / "identity.json"
        if p.is_file():
            return p
    return None


@lru_cache(maxsize=1)
def name() -> str:
    """This Core's user first name, e.g. 'Nick'. Falls back to 'the operator'.

    Fail-open by construction: a hook that cannot read identity.json still renders a correct
    sentence, it just renders a generic one. A hook must never crash over a display name.
    """
    env = os.environ.get("CORE_USER_NAME")
    if env and env.strip():
        return env.strip()
    p = _identity_path()
    if not p:
        return FALLBACK
    try:
        u = (json.loads(p.read_text()) or {}).get("user") or {}
        n = (u.get("name") or "").strip()
        # A fresh fork ships the literal placeholder — treat it as unset, not as a name.
        if not n or n.upper().startswith("YOUR"):
            return FALLBACK
        return n
    except Exception:
        return FALLBACK


def possessive() -> str:
    """'Nick's' / 'the operator's' — correct for names already ending in s."""
    n = name()
    return f"{n}'" if n.endswith("s") else f"{n}'s"

if __name__ == "__main__":
    print(name())
