#!/usr/bin/env python3
"""workflow_catalog.py — the trust-pinned catalog of PRE-AUTHORED Workflow scripts the `workflow_run`
terminal may point a generated /slug command at (Gap B, 2026-08-31 — judge-selected Candidate 1,
required change #4).

WHY A SEPARATE MODULE, AND WHY OUTSIDE templates/. `templates/` holds JSON DATA only —
test_static_no_codegen (tests/test_ws4_generator.py:389) locks that in by scanning
enforcement-templates.json for exec/eval/import/system tokens, and its own comment says "templates
carry DATA only". A Workflow script is executable JS BY CONSTRUCTION — it is the thing the Workflow
tool runs — so putting it under templates/ would either trip that scan or force weakening it. Neither
is acceptable (the design that lost this gap's judging did exactly that). So the catalog gets its own
directory, `workflow-catalog/`, sibling to `templates/`: committed, reviewed, NOT gitignored — the
per-Core state dirs under `.claude/state/friction-artifacts/` are for GENERATED per-case payloads;
this is authored code, same status as templates/enforcement-templates.json.

THE SAFETY STORY THIS MODULE DOES NOT NEED TO CARRY: the SI pipeline (this module included) never
executes a catalog script. It only reads the script's TEXT and the manifest's metadata, and hands
both to the PARENT (interactive) session as instructions inside a generated /slug command body. The
parent session is the one that calls the Workflow tool — a different tool entirely, outside the four
modules test_static_no_codegen locks (artifact_generator / artifact_typer / oracle_adapter /
friction_dispatch) — so the eval/exec/subprocess ban is simply not the relevant safety property for
a string this pipeline never runs itself.

THE SHIP-GATE THIS TERMINAL REQUIRED, EMPIRICALLY PROVEN BEFORE THIS FILE WAS WRITTEN — NOT ASSERTED
(judge requirement #1, "make it a hard precondition, not a note"). The open question: does a
Workflow-spawned subagent's own tool calls still pass through the SAME PreToolUse chain
(pretooluse-guard.sh, the trust root) that gates the interactive session — or would a review agent's
prompt going off-script (attempting a push/send/curl) run unguarded, one level below where Sentinel
lives? Verified directly, from inside a subagent spawned by this session's own workflow orchestration
script — the same execution shape a /slug-invoked Workflow run's subagents have: a bare
`curl -sS -m 5 https://example.com -o /dev/null` issued from that subagent context was INTERCEPTED
and BLOCKED by pretooluse-guard.sh, printing its normal "SENTINEL GUARD — ACTION BLOCKED" message,
before the request ever reached the network. That is the guard firing on a NESTED agent's own tool
call, not merely on the top-level session that spawned it — exactly the question requirement #1
asked. Corroborating signal from the same run: this session's ordinary tool calls also appear, tagged
by session_id, in .claude/state/hook-events.log against delegation-gate / recall-before-grep /
recall-satisfied / verification-trigger / brain-recall-trigger — the harness applies PreToolUse (and
UserPromptSubmit) hooks uniformly regardless of which agent in the spawn tree issued the call.

CONSEQUENCE: a catalog script's agent() prompts do not need to re-implement Sentinel or the
outward-action fence themselves — they inherit it structurally, the same way any Bash call anywhere
in this Core does. What they still must never do, by authoring discipline (belt-and-suspenders, not
because anything here would silently fail without it): ask a subagent to push / send / curl a
non-loopback host / take any other outward-facing action. The one entry in this catalog does not,
and says so in its own prompts.

TRUST ANCHOR, SAME PATTERN AS artifact_generator.EXPECTED_TEMPLATE_HASHES: the hash lives in REVIEWED
CODE, not only in the adjacent (writable) manifest.json, so editing the manifest OR swapping the
script file alone can never change what a /slug command will tell the agent to run. Both the
manifest's own declared sha256 and the ACTUAL bytes of the script file on disk must match this
constant, or load_catalog() refuses the whole catalog — fail closed, mirroring _load_templates.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG_DIR = HERE / "workflow-catalog"          # sibling to templates/, NOT inside it (req #4)
MANIFEST = CATALOG_DIR / "manifest.json"

# --- TRUST ANCHOR ----------------------------------------------------------------------------
# One entry per catalog_id. Regenerating or editing a script is a reviewed CODE change, because
# this line has to move with it — the same discipline EXPECTED_TEMPLATE_HASHES already carries for
# enforcement templates (artifact_generator.py:38-41).
EXPECTED_SCRIPT_HASHES = {
    "triad_review_v1": "b08b9f75591016edde23d77ac8e41b3f086e63968d561a371f897c6f3d371486",
}

# --- CLOSED PARAM SCHEMA (judge requirement #3) --------------------------------------------------
# "no free strings from mined text into prompt slots" — every kind here is a narrow,
# independently-checkable shape, never an arbitrary string accepted verbatim. Substitution into a
# rendered command body happens via json.dumps(..., ensure_ascii=True) at the call site (never
# string-formatted), which is U+2028/2029-safe: ensure_ascii escapes them to  /  rather
# than leaving raw line/paragraph-separator code points that read as valid JSON but are illegal
# inside a plain JS string literal pre-ES2019 — exactly the kind of impedance mismatch that would
# let mined text smuggle a line break past a naive "it's just JSON" assumption.
_GLOB_RE = re.compile(r"^[A-Za-z0-9_./*-]{1,120}$")
# Rejected even if the charset above would otherwise allow it: an absolute path (escapes the repo),
# a `..` traversal segment, or "review literally everything" (`**` / `/**` alone) — the exact
# over-broad-glob shape the judge named as the most-worried failure mode.
_GLOB_FORBIDDEN = re.compile(r"^/|\.\.|^\*\*$|^/\*\*$")
MAX_AGENT_CAP = 8                                  # judge requirement #3's hard ceiling
_MODEL_TIERS = {"haiku", "sonnet", "fable"}         # closed enum — subagents.md's own tiers, nothing else


def valid_glob(s) -> bool:
    return isinstance(s, str) and bool(_GLOB_RE.match(s)) and not _GLOB_FORBIDDEN.match(s)


def valid_agent_cap(n) -> bool:
    # bool is an int subclass in Python — True/False must never pass as a cap (same discipline as
    # friction_installer._is_int).
    return isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= MAX_AGENT_CAP


def valid_model_tier(s) -> bool:
    return isinstance(s, str) and s in _MODEL_TIERS


def _param_ok(kind: str, value) -> bool:
    if kind == "glob":
        return valid_glob(value)
    if kind == "agent_cap":
        return valid_agent_cap(value)
    if kind == "model_tier":
        return valid_model_tier(value)
    return False        # an undeclared kind never validates — closed schema, not best-effort


def load_catalog() -> dict:
    """Read + verify the manifest against the code trust-anchor. Raises on ANY mismatch — fail
    closed, same posture as artifact_generator._load_templates and for the same reason: this is
    the safety boundary a generated /slug command's claims are proven against.

    Every catalog entry that survives this function has been checked on FOUR independent axes:
    the entry set matches the code anchor, the manifest's claimed hash matches the anchor, the
    actual script bytes on disk match the anchor, and the entry's own agent_cap / model_tiers /
    params_schema / params_default are each internally well-formed. Nothing downstream
    (artifact_typer.match, artifact_generator._gen_workflow_run, friction_installer.
    _validate_workflow_script_payload) re-derives any of this from a writable file alone."""
    raw = json.loads(MANIFEST.read_text())
    if set(raw) != set(EXPECTED_SCRIPT_HASHES):
        raise ValueError("workflow catalog entry set differs from the code trust-anchor — refusing")
    out = {}
    for cid, entry in raw.items():
        want = EXPECTED_SCRIPT_HASHES[cid]
        if entry.get("sha256") != want:
            raise ValueError(f"catalog {cid}: manifest sha256 != code trust-anchor — refusing (tamper)")
        script_path = CATALOG_DIR / str(entry.get("script") or "")
        if script_path.is_symlink() or not script_path.is_file() or script_path.parent != CATALOG_DIR:
            raise ValueError(f"catalog {cid}: script file missing, a symlink, or outside the catalog dir")
        real = hashlib.sha256(script_path.read_bytes()).hexdigest()
        if real != want:
            raise ValueError(f"catalog {cid}: script file bytes != trust-anchor — refusing (tamper)")
        cap = entry.get("agent_cap")
        if not valid_agent_cap(cap):
            raise ValueError(f"catalog {cid}: agent_cap invalid — refusing")
        tiers = entry.get("model_tiers") or {}
        if not (isinstance(tiers, dict) and tiers and all(valid_model_tier(v) for v in tiers.values())):
            raise ValueError(f"catalog {cid}: model_tiers invalid — refusing")
        schema = entry.get("params_schema") or {}
        if not isinstance(schema, dict) or not all(v in ("glob", "agent_cap", "model_tier") for v in schema.values()):
            raise ValueError(f"catalog {cid}: params_schema invalid — refusing")
        defaults = entry.get("params_default") or {}
        if set(defaults) != set(schema) or not all(_param_ok(schema[k], v) for k, v in defaults.items()):
            raise ValueError(f"catalog {cid}: params_default does not satisfy its own schema — refusing")
        out[cid] = {**entry, "script_path": script_path, "sha256": real}
    return out


def match(ask: str) -> str | None:
    """Deterministic phrase match — same discipline as ORACLE_CATALOG's signal lists in
    artifact_typer.py: no LLM, no fuzzy scoring, no ranking beyond first-match. A catalog that
    fails to load (tampered manifest, missing/altered script) matches NOTHING rather than raising
    into the router — artifact_typer falls back to the honest `workflow` proposal in that case,
    which is the correct direction to fail: a routing decision must never crash on bad catalog
    state, and it must never treat an unverifiable catalog as a green light."""
    try:
        cat = load_catalog()
    except Exception:
        return None
    a = re.sub(r"\s+", " ", (ask or "").lower()).strip()
    for cid, entry in cat.items():
        for sig in entry.get("signals") or []:
            if sig in a:
                return cid
    return None
