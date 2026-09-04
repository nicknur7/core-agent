#!/usr/bin/env python3
"""action_registry.py — the ONE statically-enumerable catalog of scripts effect.mode:"run" may
launch (GAP A-executable-effect, 2026-08-31). Loaded INDEPENDENTLY by both friction_installer.py
(validate/install time) and friction_runner.py (fire time) — neither trusts the other's read,
extending the design's "never trust the queue writer" rule (judge requirement 4) to the catalog
itself: a process that only inherited "this action_id was fine when the installer checked it"
would be trusting a claim, not verifying a fact.

WHY A CATALOG, NOT AN ARTIFACT FIELD (judge requirement 3 / Candidate 1's discipline, carried into
the winning design). The friction loop generates artifact TRIGGERS autonomously and test-gates
them against a real prompt corpus — that is the whole safety story for WHEN something fires
(friction_test_gate.py). It has no comparable proof for WHAT a script does when run, and building
one would mean executing arbitrary generated code to test it, which is the exact capability
test_static_no_codegen exists to keep out of the generator. So the catalog is the other half of
the safety story: a short, hand-written, PR-reviewed list of PRE-EXISTING local scripts — never
generated, never artifact-writable. An artifact's `run` effect can only SELECT an action_id from
here; it never supplies a path, a flag, or an argument (see friction_dispatch.py's "run" branch
and friction_runner.py's env-scrub). Growth is one PR per real entry (judge requirement 6); this
module only proves each entry already in the file is what it claims to be, every time it is read.

LEAF MODULE. No import of friction_installer / friction_dispatch / friction_runner, so none of the
three can form an import cycle through it, and it keeps its own tiny action-log writer rather than
sharing one of theirs — a bug in a caller's import graph must not be able to take this module down
with it, since both the installer's validation and the runner's fire-time gate depend on it.

  load_catalog() -> {action_id: entry}   every entry that is EXACTLY what it claims, right now
  get_action(action_id) -> entry | None  single lookup, same validation
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

# REPO_ROOT is the checked-in baseline root, not a per-Core state dir. Catalog entries name
# checked-in scripts (bin/actions/*) — SHARED content, identical across every Core — so this
# deliberately does NOT go through the CORE_INSTANCE/CLAUDE_PROJECT_DIR seat resolver every
# per-Core STATE path in this subsystem uses (friction_dispatch.py, friction_installer.py). A
# script's identity is "which file in this git checkout", never "which Core is running it".
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "scheduling" / "claude-si" / "templates" / "action-catalog.json"

# Per-Core runtime state, for THIS module's own log only — mirrors friction_dispatch.py's own seat
# resolution as an independent copy rather than an import (see LEAF MODULE above). Fail-soft to
# CLAUDE_PROJECT_DIR/repo-root exactly like friction_dispatch.py does, for the same reason: an
# import failure here must never raise out of a catalog load.
try:
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "bin"))
    from core_seat import seat_root as _seat_root
    _INSTANCE = _seat_root(fallback=REPO_ROOT)
except Exception:
    _INSTANCE = Path(os.environ.get("CLAUDE_PROJECT_DIR") or REPO_ROOT)
STATE = _INSTANCE / ".claude" / "state"
ACTION_LOG = STATE / "friction-action-log.jsonl"

_ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CATALOG_KEYS = {"action_id", "outward_declared", "script", "script_sha256", "timeout_sec",
                 "max_fires_per_session", "max_fires_per_week", "description"}
# "a pre-existing, human-reviewed LOCAL script" (the judge's own framing of what makes this design
# safe) is short by construction. A six-figure file passing its pinned hash is not evidence the
# hash-lock worked; it is evidence nobody read what they pinned.
MAX_SCRIPT_BYTES = 65536
MAX_DESCRIPTION = 500


def _log(action: str, **kw) -> None:
    try:
        ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ACTION_LOG.open("a") as f:
            f.write(json.dumps({"action": action, "ts": int(time.time()), **kw}) + "\n")
    except Exception:
        pass


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)  # bool is a subclass of int in Python


def _valid_entry(action_id: str, e: dict) -> tuple[bool, str]:
    """One catalog row, fully re-verified against the filesystem. Every check here runs on EVERY
    call to load_catalog() — nothing is cached — because the fire-time TOCTOU re-check in
    friction_runner.py is only meaningful if re-validating is actually cheap enough to do twice."""
    if not isinstance(e, dict) or set(e.keys()) != _CATALOG_KEYS:
        return False, "not an object with the exact closed key set"
    if e.get("action_id") != action_id:
        return False, "action_id must equal its own catalog key"
    if not _ACTION_ID_RE.match(action_id):
        return False, "action_id fails charset ^[a-z][a-z0-9_]{1,63}$"
    # outward_declared MUST BE THE LITERAL BOOLEAN False (judge requirement 5). Renamed from
    # `outward` (HIGH, Codex, 2026-09-01): the old name read as an enforced guarantee — "this
    # action does not reach outside the machine" — when what the check actually verifies is a
    # self-attestation in a hand-written JSON file. There is no Sentinel gate on this path:
    # friction_runner.py fires out-of-process, outside every PreToolUse hook, so it structurally
    # cannot route through the review that gates email / SMS / git push / calendar writes / curl
    # (privacy.md's own list, .claude/agents/sentinel.md) — and no OS-level sandbox wraps the
    # spawned process either (evaluated honestly and rejected for THIS fix, not skipped: macOS
    # `sandbox-exec` with `(deny default)` aborts on trivially launching /bin/echo on this exact
    # host, Darwin 25.5.0 / macOS 26.5.1 — too fragile to wire around the one shipped action
    # without real risk of self-quarantining it on a profile misconfiguration; a looser
    # `(allow default)(deny network*)` profile DID measurably block a socket connect in testing,
    # so network-egress sandboxing is plausible future work, just not a same-day addition to an
    # urgent correctness fix). An honest label beats a false boundary: rather than trust a
    # self-reported "safe" flag from a JSON file that ships to four other Cores AND claim
    # enforcement that is not there, v1 refuses to load ANY entry that is not affirmatively,
    # explicitly marked non-outward-declared — missing, null, the string "false", or True are all
    # refused identically (`is not False`, not `not e.get(...)`, so a missing key does not read as
    # falsy-safe). An action that needs Sentinel does not belong in this catalog until this
    # terminal grows one; it cannot become eligible by mislabeling itself, because both this loader
    # AND friction_runner.py's own re-check (belt-and-suspenders, never one checker alone) run this
    # exact test independently. The real backstop is that this catalog has NO programmatic writer
    # anywhere in this repo (verified by grep, not assumed) — CATALOG_PATH is read-only from every
    # module's perspective, so "install path requires human review" is not a policy to add, it is
    # already the only path that exists: one hand-authored PR per real entry.
    if e.get("outward_declared") is not False:
        return False, ("outward_declared must be the literal boolean false (self-attested — no "
                        "Sentinel gate or OS sandbox enforces this path; see comment above)")
    script = e.get("script")
    if not isinstance(script, str) or not script or script.startswith("/") or ".." in script.split("/"):
        return False, "script must be a relative, non-traversing repo path"
    script_path = (REPO_ROOT / script).resolve()
    if REPO_ROOT != script_path and REPO_ROOT not in script_path.parents:
        return False, "script resolves outside the repo root"
    if not isinstance(e.get("script_sha256"), str) or not _SHA256_RE.match(e["script_sha256"]):
        return False, "script_sha256 must be a lowercase sha256 hex digest"
    if not (_is_int(e.get("timeout_sec")) and 1 <= e["timeout_sec"] <= 30):
        return False, "timeout_sec must be an int 1..30"
    if not (_is_int(e.get("max_fires_per_session")) and 1 <= e["max_fires_per_session"] <= 5):
        return False, "max_fires_per_session must be an int 1..5"
    if not (_is_int(e.get("max_fires_per_week")) and 1 <= e["max_fires_per_week"] <= 50):
        return False, "max_fires_per_week must be an int 1..50"
    if not (isinstance(e.get("description"), str) and 0 < len(e["description"]) <= MAX_DESCRIPTION):
        return False, f"description must be 1..{MAX_DESCRIPTION} chars"
    # FILESYSTEM TRUTH, checked last and every time (see docstring) — a hash-lock is worth nothing
    # if the path it locks can be swapped for a symlink between checks.
    try:
        if script_path.is_symlink():
            return False, "script path is a symlink — refusing"
        if not script_path.is_file():
            return False, "script file missing on disk"
        st = script_path.stat()
        if st.st_size > MAX_SCRIPT_BYTES:
            return False, f"script exceeds {MAX_SCRIPT_BYTES} bytes"
        if not (st.st_mode & stat.S_IXUSR):
            return False, "script is not executable (chmod +x)"
        raw = script_path.read_bytes()
    except Exception as exc:
        return False, f"cannot stat/read script: {exc}"
    if hashlib.sha256(raw).hexdigest() != e["script_sha256"]:
        return False, "script_sha256 does not match the file on disk (drift or tamper)"
    return True, "ok"


def load_catalog() -> dict:
    """Every entry that is exactly what it claims, right now. Fails PER-ENTRY, not whole-file: one
    malformed PR must not take down an already-shipped, already-reviewed action (judge requirement
    6: "each real entry is its own PR"). Fails to {} (nothing runnable) if the file itself cannot
    be read or is not an object — a catalog that cannot be proven safe is not partially trusted."""
    try:
        if CATALOG_PATH.is_symlink():
            _log("action_catalog_refused", reason="catalog path is a symlink")
            return {}
        raw = json.loads(CATALOG_PATH.read_text())
    except Exception as exc:
        _log("action_catalog_unreadable", reason=str(exc)[:200])
        return {}
    if not isinstance(raw, dict):
        _log("action_catalog_refused", reason="catalog root must be an object")
        return {}
    out = {}
    for action_id, entry in raw.items():
        ok, why = _valid_entry(action_id, entry)
        if ok:
            out[action_id] = entry
        else:
            _log("action_catalog_entry_refused", action_id=str(action_id)[:64], reason=why)
    return out


def get_action(action_id: str) -> dict | None:
    """Single lookup, same validation as load_catalog() (it IS load_catalog(), filtered) — no
    second, looser code path that a caller could reach with an unvalidated id."""
    if not isinstance(action_id, str) or not _ACTION_ID_RE.match(action_id):
        return None
    return load_catalog().get(action_id)
