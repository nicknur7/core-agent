#!/usr/bin/env python3
"""friction_installer.py — P3 installer + rollback. Takes a routed artifact spec + its examples,
INDEPENDENTLY re-runs the test-gate (never trusts a supplied PASS), and — only on pass — writes it
into the per-Core active snapshot ATOMICALLY, saving the prior snapshot for out-of-band rollback.
Never edits settings.json (the one static dispatcher is registered separately, once). Fail-safe:
any error leaves the prior active snapshot intact.

  install(spec, examples) -> {"ok": bool, "reason": str}
  rollback(artifact_id)   -> deactivate THIS artifact only (artifact-local; never a
                             whole-snapshot restore, which would silently drop healthy
                             rules installed after it). Retires its payload too.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "brain-pg"))
import friction_dispatch as fd
import friction_test_gate as tg
import friction_jsonl as fj

# 2026-07-30 — CORE_INSTANCE is honoured FIRST, and it did not used to be. This module keyed off
# CLAUDE_PROJECT_DIR alone while every other shared tool (grade-gate.py, lint-doc-paths.py,
# correction-rate.py) keys off CORE_INSTANCE. In a normal session both point at the same Core, so
# the disagreement was invisible. It stopped being invisible the moment anything ran against a
# DIFFERENT Core than the one it was launched from: si-adopt-projection.py run with
# CORE_ORG_ID=2..5 and CORE_INSTANCE=<peer> reported that all four peers held the SAME 14
# artifacts — because it silently read core-life's active.json four times. Ground truth was
# business 20, and school/finance/ops none at all. With --apply that would have written life's
# artifacts into four peer orgs as if the peers had mined them. Explicit operator intent
# (CORE_INSTANCE) therefore outranks the harness default (CLAUDE_PROJECT_DIR); in-session they
# are identical, so nothing changes for a normal run.
STATE = Path(os.environ.get("CORE_INSTANCE")
             or os.environ.get("CLAUDE_PROJECT_DIR")
             or HERE.parents[1]) / ".claude" / "state"
ARTDIR = STATE / "friction-artifacts"
ACTIVE = ARTDIR / "active.json"
SNAPDIR = ARTDIR / "snapshots"
ACTION_LOG = STATE / "friction-action-log.jsonl"
# Mined cases + the recurring-ask digest are PER-CORE personal data (verbatim prompts).
# They must resolve under .claude/state/ — never inside scheduling/claude-si/, which is a
# SHARED dir: sync-to-baseline rsyncs shared dirs, so a cases file parked there is pushed
# to nicknur7/core-agent AND clobbered on every pull by the baseline copy (2026-07-27 fix).
CASES = STATE / "friction-cases.jsonl"
CAPTURED_ASKS = STATE / "captured-requests.json"
# PROCEDURE PAYLOADS (2026-07-27). A `procedure` artifact is a gated trigger PLUS a markdown
# payload. The payload deliberately does NOT live in .claude/skills/ — a file there is
# model-invoked on description match every turn, with no corpus gate, and skill invocations
# never reach friction-action-log.jsonl, so the watchdog would be structurally blind to the
# misbehavior it exists to quarantine (Fable review 2026-07-27). Payloads here have NO
# discovery surface: the only path that ever reaches one is a trigger that passed the gate.
PROCDIR = ARTDIR / "procedures"
# COMMANDS_DIR is the REAL, live `.claude/commands/` surface — not a hidden per-Core state dir.
# Safe to write there directly (2026-08-31): PROCDIR hides a hooked_skill's payload because a
# SKILL activates on DESCRIPTION match, which the gate cannot test in advance (artifact_typer.py
# module docstring). A slash command has no such surface — it activates ONLY when Nick types
# `/<slug>` himself, which is exactly the 'invoked deliberately' shape the terminal exists for, so
# a hidden copy would add a discovery step with no matching safety benefit.
COMMANDS_DIR = STATE.parent / "commands"
# WFPROPDIR holds workflow_proposal payloads — markdown briefs, never a runnable script location.
# Kept beside PROCDIR (both per-Core state, both gitignored) rather than under COMMANDS_DIR: a
# proposal nobody has approved yet has no business being a real, nameable surface.
WFPROPDIR = ARTDIR / "workflow-proposals"
# WFSCRIPTDIR holds workflow_run RUN-MANIFESTS — a tiny per-artifact JSON binding {catalog_id,
# script_sha256, agent_cap, model_tiers, params}, never the workflow SCRIPT itself. The reviewed
# .js scripts live in the COMMITTED scheduling/claude-si/workflow-catalog/ (workflow_catalog.py) —
# authored code, same status as templates/. This directory is the opposite: GENERATED per case,
# per-Core, personal-data-adjacent (an ask-derived slug/glob can end up here), so it lives under
# ARTDIR like PROCDIR/WFPROPDIR and inherits the SAME wholesale gitignore line those two already
# rely on (.gitignore:123, `.claude/state/friction-artifacts/`) — no new ignore rule needed.
WFSCRIPTDIR = ARTDIR / "workflow-scripts"

# RESTORED 2026-08-04. 1e6df4b ("evidence belongs outside the SPEC but inside the DATABASE")
# deleted four constants from this block. Dropping EVIDENCE was the point of that commit and
# nothing references it now. The other three were collateral — every use site stayed, so each
# has raised NameError ever since. core-ops reported MAX_PROCEDURE_BYTES on the bus; an AST
# sweep for used-but-never-bound names found the other two, which nobody had noticed.
#
# Restored at their original values from 92eb9a6 rather than re-guessed: the tests assert
# against these exact numbers, so a fresh guess would have moved the contract instead of
# fixing the regression.
PROCQDIR = ARTDIR / "quarantined"          # rollback moves payloads here (2 sites, ~L376)
MAX_PROCEDURE_BYTES = 8192                 # bounded: payload is read into context on every fire
MAX_ACTIVE_PROCEDURES = 10                 # proliferation cap; watchdog alarms above it
# slash_command / workflow_proposal (2026-08-31): same shape of bound, own ceilings so a run of
# commands cannot crowd out procedure headroom or vice versa. Neither payload is read into context
# on fire (both terminals inject a POINTER message, never the body — see artifact_generator), so
# these bound authoring size and command-file bloat rather than a per-turn token cost.
MAX_COMMAND_BYTES = 8192
MAX_WORKFLOW_PROPOSAL_BYTES = 8192
MAX_ACTIVE_COMMANDS = 10
MAX_ACTIVE_WORKFLOW_PROPOSALS = 10
# workflow_run (Gap B, 2026-08-31, judge requirement #7): its OWN size cap + dir constant, same
# shape as the three above — not a share of MAX_COMMAND_BYTES, so a run of workflow_run installs
# can never crowd out ordinary command headroom or vice versa. The payload here is a tiny JSON
# run-manifest (catalog_id/hash/cap/tiers/params — see friction_installer._validate_workflow_
# script_payload), not prose, so 8192 is generous rather than tight; kept at the same magnitude as
# the other three for one uniform mental model instead of a fourth bespoke number.
MAX_WORKFLOW_SCRIPT_BYTES = 8192
# Deliberately the TIGHTEST proliferation cap of the four (below, in _PROLIFERATION_CAPS): each
# workflow_run artifact can launch a real multi-agent fan-out, so the blast-radius-per-artifact is
# strictly higher than a hooked_skill/slash_command/workflow_proposal's inject-only message.
MAX_ACTIVE_WORKFLOW_RUNS = 5
# One table, not four copies of the cap-check block in install() (2026-08-31).
_PROLIFERATION_CAPS = {"hooked_skill": MAX_ACTIVE_PROCEDURES, "slash_command": MAX_ACTIVE_COMMANDS,
                       "workflow_proposal": MAX_ACTIVE_WORKFLOW_PROPOSALS,
                       "workflow_run": MAX_ACTIVE_WORKFLOW_RUNS}

# Worth naming, because it is the more dangerous half: MAX_ACTIVE_PROCEDURES is read at L359 in
# the "fail CLOSED: unknown count blocks the install" branch. Undefined, that branch raised
# NameError instead of returning the cap — so the guard that is supposed to REFUSE on an unknown
# count could not refuse. A missing constant turned a fail-closed path into a crash.


def _write_evidence(artifact_id: str, payload: dict, org: int | None = None) -> bool:
    """Persist an artifact's example texts and tuning bookkeeping. Returns True on success.

    OUTSIDE THE SPEC, INSIDE THE DATABASE. Those are two different requirements and the first
    version of this satisfied only the first.

    Out of the spec, because spec has a closed key set enforced by _validate_spec and writing
    bookkeeping into it made tuned artifacts un-reinstallable. (core-business, finding 8.)

    In Postgres, because the local JSON file it went to instead was gitignored and invisible to
    pg_dump — so the evidence was backed up by nothing — and because its read path turned any
    parse failure into an empty dict that was then atomically written over the top, destroying
    every OTHER artifact's evidence on a single corrupt read. (core-business, finding 9.)

    si_artifacts.evidence is a column beside spec, not inside it: same row, same RLS, same org
    partition, same backup. No second file to corrupt and nothing to keep in sync.

    Fail-soft but NOT silent. An install must not die over bookkeeping, but a swallowed failure
    reproduces finding 1's symptom exactly — no evidence, so no narrowing, so quarantine — with
    nothing reported. So a failure is logged and the boolean is returned for callers that care.
    """
    try:
        from _env import get_org_id  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
        org = int(org) if org is not None else get_org_id()
        from _env import connect_corebrain
        con = connect_corebrain()
        try:
            cur = con.cursor()
            cur.execute("SET LOCAL app.current_org_id = %s", (str(org),))
            cur.execute("UPDATE si_artifacts SET evidence = %s, updated_at = now() "
                        "WHERE org_id = %s AND artifact_id = %s",
                        (json.dumps(payload), org, artifact_id))
            wrote = cur.rowcount
            con.commit()
        finally:
            con.close()
        if not wrote:
            _log("evidence_write_missed", artifact_id=artifact_id,
                 reason="no si_artifacts row matched — evidence not stored")
            return False
        return True
    except Exception as e:
        # LOUD, not silent. The whole point of finding 9b.
        _log("evidence_write_failed", artifact_id=artifact_id,
             reason=f"{type(e).__name__}: {str(e)[:120]}")
        return False


def read_evidence(artifact_id: str, org: int | None = None) -> dict:
    """Evidence for one artifact, or {} — callers must treat absence as 'not tunable'.

    {} on any failure is correct here and is NOT the fail-open problem from 9b: a reader that
    cannot prove an artifact has evidence must not tune it, and every caller already falls
    through to the pre-existing quarantine behaviour. The dangerous direction was the WRITE
    silently succeeding-as-nothing, which is now logged.
    """
    try:
        from _env import get_org_id  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
        org = int(org) if org is not None else get_org_id()
        from _env import connect_corebrain
        con = connect_corebrain()
        try:
            cur = con.cursor()
            cur.execute("SET LOCAL app.current_org_id = %s", (str(org),))
            cur.execute("SELECT evidence FROM si_artifacts WHERE org_id = %s AND artifact_id = %s",
                        (org, artifact_id))
            row = cur.fetchone()
        finally:
            con.close()
        return (row[0] or {}) if row else {}
    except Exception:
        return {}



def _undecidable_is_new(aid: str, corpus_n: int) -> bool:
    """True when this artifact's undecidable state DIFFERS from the last one recorded for it.

    Supports the dedup at the gate's undecidable branch. Reads the last `test_undecidable` row for
    `aid` and compares `corpus_n` — the only thing that can change the verdict. Same corpus size
    means the same unsatisfiable precondition, and a second row saying so carries no information.

    FAILS OPEN, deliberately. An unreadable or malformed log returns True, so the row is written. A
    dedup that silently swallows records when it cannot read its own history would turn a logging
    optimisation into data loss — and "the log looked quiet" is precisely the failure mode this
    file has spent the day removing. Over-logging is recoverable; a dropped observation is not.
    """
    try:
        if not ACTION_LOG.is_file():
            return True
        last = None
        for line in ACTION_LOG.read_text(errors="ignore").splitlines():
            if '"test_undecidable"' not in line or aid not in line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("action") == "test_undecidable" and row.get("artifact_id") == aid:
                last = row
        if last is None:
            return True
        return last.get("corpus_n") != corpus_n
    except Exception:
        return True


def _log(action, **kw):
    # CENTRAL redaction: every string value is redacted before it lands in the action log, so no
    # caller-supplied field (e.g. an unvalidated rollback artifact_id) can persist a secret verbatim
    # (Codex 5th review). Non-strings (int org_id, ts) pass through.
    try:
        ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        safe = {k: (fj.redact(v) if isinstance(v, str) else v) for k, v in kw.items()}
        with ACTION_LOG.open("a") as f:
            f.write(json.dumps({"action": action, "ts": int(time.time()), **safe}) + "\n")
    except Exception:
        pass


def _load_active() -> dict:
    try:
        return json.loads(ACTIVE.read_text())
    except Exception:
        return {"artifacts": []}


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with tmp.open("w") as f:
        f.write(json.dumps(data, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX
    try:  # fsync the directory so the rename is durable
        dfd = os.open(str(path.parent), os.O_DIRECTORY)
        os.fsync(dfd)
        os.close(dfd)
    except Exception:
        pass


_ALLOWED_OPS = {"event_is", "tool_name_in", "tool_mutability_is", "prompt_regex", "assistant_regex",
                "tool_call_present", "tool_result_present", "state_flag_is", "artifact_delivery_present",
                # 2026-07-27: the installer keeps its OWN op allowlist, deliberately — the dispatcher
                # deciding an op is valid must not by itself make it installable. A new oracle op has
                # to be admitted in BOTH places, which is the intended friction.
                "adversarial_review_present"}
# The prompt-grounded set: the ONLY operators whose field the specificity corpus (prompt_text) can
# validate. Applies to every contract, and to a hooked_skill on a prompt event. Non-prompt ops
# (assistant_regex, tool_*) are rejected there so a hand-crafted spec can't escape corpus grounding
# (Codex 5th review). A hooked_skill on PreToolUse uses _WORKSHAPE_OPS instead — see _validate_spec.
_V1_ALLOWED_OPS = {"event_is", "prompt_regex"}
# Work-shape ops: what WORK is about to happen, rather than what the prompt said. Restricted to
# hooked_skill on PreToolUse (see _validate_spec) — these fields are not present in the prompt
# corpus, so the 3%-specificity check cannot ground them and they must not leak into contracts.
_WORKSHAPE_OPS = {"event_is", "tool_name_in", "tool_mutability_is", "prompt_regex"}
_ALLOWED_EVENTS = {"UserPromptSubmit", "PreToolUse", "Stop", "SessionStart"}
# Spec types that carry a `payload` block at all (2026-08-31). Every one of these rides the SAME
# gated-trigger-plus-payload shape hooked_skill introduced; the type only decides WHERE the payload
# lives and how it is named (see write_procedure/write_command_file/write_workflow_proposal).
# workflow_run (Gap B) added 2026-08-31: its PUBLIC payload is a real .claude/commands/<slug>.md
# file, byte-for-byte the same shape a slash_command's is (see _validate_command_payload, reused
# verbatim below) — its ADDITIONAL catalog binding lives in the separate `workflow_ref` block,
# not here, because that block has no analog in slash_command and closing `payload` over it too
# would make one key mean two different contracts depending on type.
_PAYLOAD_TYPES = {"hooked_skill", "slash_command", "workflow_proposal", "workflow_run"}
# Types allowed to key their trigger on a PreToolUse work-shape condition instead of a prompt.
# slash_command is deliberately excluded: its trigger is bookkeeping over the literal `/slug`
# invocation text, which only ever arrives on UserPromptSubmit. workflow_run is excluded for the
# IDENTICAL reason — same invocation model, reused on purpose (judge requirement: "C1's invocation
# model reuses the slash_command terminal's own verified justification").
_TOOL_EVENT_TYPES = {"hooked_skill", "workflow_proposal"}
_MAX_DEPTH = 6


_COND_COMBINATORS = {"all", "any", "none"}
_COND_LEAF_KEYS = {"op", "value"}


def _validate_condition(cond, depth=0, allowed_ops=_ALLOWED_OPS) -> bool:
    if depth > _MAX_DEPTH or not isinstance(cond, dict):
        return False
    keys = [k for k in ("all", "any", "none", "op") if k in cond]
    if len(keys) != 1:
        return False
    k = keys[0]
    if k in _COND_COMBINATORS:
        # closed: a combinator node carries ONLY that key (Codex 4th review — nested unknown keys)
        if set(cond.keys()) - {k}:
            return False
        v = cond[k]
        if not isinstance(v, list) or not (1 <= len(v) <= 12):
            return False
        return all(_validate_condition(c, depth + 1, allowed_ops) for c in v)
    # leaf: only {op, value} allowed
    if set(cond.keys()) - _COND_LEAF_KEYS:
        return False
    op = cond.get("op")
    if op not in allowed_ops:
        return False
    val = cond.get("value")
    if op in ("prompt_regex", "assistant_regex"):
        return fd._validate_regex(val)
    if op == "tool_name_in":
        return isinstance(val, list) and 1 <= len(val) <= 30 and all(isinstance(x, str) for x in val)
    return True


def _is_int(v) -> bool:
    # Python treats True==1; a real int field must NOT accept a bool (Codex 4th review)
    return isinstance(v, int) and not isinstance(v, bool)


def _closed(obj, allowed: set, label: str) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return (False, f"{label} must be an object")
    missing = allowed - set(obj.keys())
    if missing:
        return (False, f"{label} missing {sorted(missing)}")
    extra = set(obj.keys()) - allowed
    if extra:
        return (False, f"{label} unknown keys {sorted(extra)}")
    return (True, "ok")


_ARTIFACT_ID_RE = __import__("re").compile(r"^art_[a-z0-9_]{1,64}$")
# rollback/quarantine may target friction (art_), legacy (legacy_), or seed (seed_) artifacts —
# restricted charset still blocks secret-shaped ids reaching the log (Codex 5th review carries over).
_ROLLBACK_ID_RE = __import__("re").compile(r"^(art|legacy|seed)_[a-z0-9_-]{1,80}$")
# effect.action_id for type=="run_action" — same charset action_registry.py enforces on a catalog
# KEY, kept as a literal copy rather than an import at module scope so this file's own closed
# key-set / regex inventory stays self-contained and grep-able (matches the existing style of
# _ARTIFACT_ID_RE / _ROLLBACK_ID_RE immediately above, both hand-duplicated across modules for the
# same reason: these are validated BEFORE anything is logged or persisted, and validation must not
# depend on an import succeeding).
_ACTION_ID_RE = __import__("re").compile(r"^[a-z][a-z0-9_]{1,63}$")


def _procedure_path(artifact_id: str) -> Path:
    """The ONE legal payload location for a hooked_skill. Derived from the artifact_id (already
    charset-restricted by _ARTIFACT_ID_RE), never taken from the spec — a spec cannot aim the
    runtime at a path of its choosing."""
    return PROCDIR / f"{artifact_id}.md"


# A public command name: lowercase, starts with a letter, kebab-case, bounded length. Distinct
# from _ARTIFACT_ID_RE because a slug is a NAME Nick types (`/keep-diagrams-current`), not an
# opaque id — but it still gets the same charset discipline: no secret can be smuggled through it
# into a filename or a log line.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,48}$")


def _command_path(slug: str) -> Path:
    """The ONE legal location for a slash_command payload — validates the slug shape as a side
    effect (raises before any write on a bad one), same discipline as _procedure_path."""
    if not _SLUG_RE.match(slug or ""):
        raise ValueError("bad command slug")
    return COMMANDS_DIR / f"{slug}.md"


def _workflow_proposal_path(artifact_id: str) -> Path:
    """The ONE legal location for a workflow_proposal payload. Keyed on artifact_id like a
    procedure — a proposal nobody has approved yet has no public name, unlike a slash_command."""
    return WFPROPDIR / f"{artifact_id}.workflow.md"


def _workflow_script_path(artifact_id: str) -> Path:
    """The ONE legal location for a workflow_run's run-manifest — keyed on artifact_id like a
    procedure or a workflow_proposal, not on the slug: the manifest is bookkeeping the installer
    and _validate_workflow_script_payload read, never something Nick names or opens directly (the
    public, nameable surface for a workflow_run is its COMMANDS_DIR/<slug>.md, exactly like a
    slash_command's)."""
    return WFSCRIPTDIR / f"{artifact_id}.run.json"


# Content the payload may never contain. These are TRIPWIRES, not a wall — see write_procedure.
# A procedure is durable instruction at Core's own trust level, so anything steering Core at its own
# guards is refused outright rather than redacted, because a redacted instruction is still an
# instruction. Outward actions (push/send/curl) remain separately gated by pretooluse-guard +
# Sentinel, which sit outside the model and are untouched by any payload.
_PAYLOAD_FORBIDDEN = re.compile(
    r"(\.claude/hooks|\.claude/settings|settings\.local\.json|pretooluse-guard|sentinel-approve"
    r"|shared-write-guard|--dangerously|danger-full-access|--no-verify|skip.{0,10}hook"
    r"|force.?push|\.env\b|id_rsa|/secrets?/)", re.I)


def _payload_content_ok(text: str) -> tuple[bool, str]:
    """Fail-closed content lint. Returns (ok, reason)."""
    m = _PAYLOAD_FORBIDDEN.search(text or "")
    if m:
        return (False, f"payload references a guard/credential surface: {m.group(1)[:40]}")
    return (True, "")


def _hardened_write(dirpath: Path, filename: str, body: str, max_bytes: int, redact: bool = True) -> dict:
    """Redact, lint, bound, and atomically write ANY artifact payload; return its closed spec
    block. Extracted from write_procedure (2026-08-31) — a slash_command and a workflow_proposal
    payload need the exact SAME hardening a hooked_skill payload gets (redaction, the guard-surface
    content lint, size bound, symlink containment, O_EXCL|O_NOFOLLOW atomic write). A second,
    hand-rolled copy of this per new artifact type is precisely the accretion Nick's standing
    directive forbids, and it is also how a new payload kind would end up with WEAKER guarantees
    than an old one by omission rather than by decision.

    HONEST SCOPE OF THE REDACTION (Codex HIGH #4, carried over unchanged): `fj.redact` is pattern
    masking for known secret SHAPES (sk-/AKIA/gh_/xox/JWT/Bearer/PEM header/long hex). It is NOT a
    declassification boundary — passwords in prose, connection strings, PEM bodies, and PII pass
    straight through. The real controls are upstream and structural: the authoring brief forbids
    personal data, the body is a distilled PROCEDURE/PROPOSAL rather than a verbatim correction,
    every target dir is gitignored (.gitignore:110) so nothing here reaches a commit or the
    baseline, and `_payload_content_ok` refuses anything that names a guard surface. Do not
    describe this function as sanitizing arbitrary untrusted text; it does not.

    `redact=False` EXISTS FOR EXACTLY ONE CALLER (write_workflow_run_manifest, Gap B 2026-08-31)
    and must never become the default. A run-manifest's `script_sha256` field is ALWAYS a 64-hex-
    char digest — that is `fj.redact`'s OWN `_SECRET_RX` "long hex" shape (`[A-Fa-f0-9]{40,}`), so
    redaction does not protect this content, it CORRUPTS it every single time, deterministically —
    not an edge case, a guarantee. Safe to skip ONLY because the caller proves (in its own
    docstring) that every field in that JSON is either a catalog-pinned constant or a value already
    validated against a closed regex (workflow_catalog._GLOB_RE) too narrow to form any of
    `_SECRET_RX`'s shapes — there is no free-text channel here for redaction to have been guarding
    in the first place. `_payload_content_ok`'s guard-surface lint still runs regardless (line
    below) — this flag skips ONLY the secret-shape masking, never the content lint.
    """
    safe = fj.redact(body or "") if redact else (body or "")
    cok, cwhy = _payload_content_ok(safe)
    if not cok:
        raise ValueError(cwhy)
    if len(safe.encode("utf-8")) > max_bytes:
        safe = safe.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    dirpath.mkdir(parents=True, exist_ok=True)
    # SYMLINK CONTAINMENT (Codex HIGH #3). Without this, a symlinked target dir or a pre-planted
    # symlink at the target redirects the write outside the tree entirely.
    if dirpath.is_symlink() or not dirpath.is_dir():
        raise ValueError("target dir is a symlink or not a directory — refusing")
    p = dirpath / filename
    if p.is_symlink():
        raise ValueError("payload path is a symlink — refusing")
    raw = safe.encode("utf-8")
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    # O_NOFOLLOW|O_EXCL: never follow a planted symlink, never reuse an existing temp file.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fdesc = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fdesc, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    os.replace(tmp, p)
    return {"path": p.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def write_procedure(artifact_id: str, body: str) -> dict:
    """Write a hooked_skill payload to PROCDIR/<artifact_id>.md. Thin wrapper over
    _hardened_write — kept as its own name because every existing caller and test targets it."""
    if not _ARTIFACT_ID_RE.match(artifact_id or ""):
        raise ValueError("bad artifact_id")
    return _hardened_write(PROCDIR, f"{artifact_id}.md", body, MAX_PROCEDURE_BYTES)


def write_command_file(slug: str, body: str) -> dict:
    """Write a slash_command payload STRAIGHT into `.claude/commands/<slug>.md` — see COMMANDS_DIR
    for why the real, public location carries no activation risk a hidden copy would have avoided."""
    p = _command_path(slug)  # validates the slug shape; raises before any write on a bad one
    return _hardened_write(COMMANDS_DIR, p.name, body, MAX_COMMAND_BYTES)


def write_workflow_proposal(artifact_id: str, body: str) -> dict:
    """Write a workflow_proposal payload — a markdown BRIEF, never a runnable script location and
    never auto-run: workflow_proposal's effect.mode is pinned to inject (_validate_spec refuses
    any other mode for this type — run_action is the only type mode:"run" is valid for, 2026-08-31),
    which cannot invoke a tool at all, so 'never auto-run' is a structural property of the DSL this
    function writes into, not a policy this function enforces on its own."""
    if not _ARTIFACT_ID_RE.match(artifact_id or ""):
        raise ValueError("bad artifact_id")
    return _hardened_write(WFPROPDIR, f"{artifact_id}.workflow.md", body, MAX_WORKFLOW_PROPOSAL_BYTES)


def write_workflow_run_manifest(artifact_id: str, body: str) -> dict:
    """Write a workflow_run's run-manifest — the small JSON binding {catalog_id, script_sha256,
    agent_cap, model_tiers, params} that _validate_workflow_script_payload cross-checks against
    workflow_catalog.load_catalog() at install. `body` is expected to already be
    json.dumps(..., ensure_ascii=True)-encoded text (artifact_generator._gen_workflow_run builds
    it) — this still runs it through _hardened_write for every OTHER guarantee a payload gets
    (guard-surface content lint, size bound, symlink containment, atomic write): a run-manifest is
    still text an installer will trust, and a second, unhardened write path for 'just this one JSON
    file' is exactly the accretion Nick's standing directive forbids.

    `redact=False` — see _hardened_write's own docstring for the full reasoning. Short version:
    `script_sha256` is ALWAYS a 64-hex digest, which is `fj.redact`'s own long-hex secret shape, so
    running this content through redaction does not protect it, it deterministically corrupts it —
    found empirically (this exact call, this exact field, on the first real spec generated)."""
    if not _ARTIFACT_ID_RE.match(artifact_id or ""):
        raise ValueError("bad artifact_id")
    return _hardened_write(WFSCRIPTDIR, f"{artifact_id}.run.json", body, MAX_WORKFLOW_SCRIPT_BYTES,
                            redact=False)


def _count_active_by_type(org: int, kind: str, exclude: str = "") -> int:
    """Count active artifacts of one spec TYPE from the CANONICAL store when the unified spine is
    live. Generalized from _count_active_procedures (2026-08-31) so slash_command, workflow_proposal,
    and (same day, Gap B) workflow_run all get the same proliferation cap a hooked_skill gets, from
    ONE query shape instead of a fourth hand-copied cap-check block drifting apart from the rest.

    Admission control must never read active.json: post-cutover that file is a disposable
    projection, and install() has a documented path where the canonical write commits but the
    projection does not ("canonical_committed_projection_pending"). Counting the stale projection
    there would under-count and let the cap be exceeded (Codex MEDIUM #5). Falls back to the
    projection only pre-cutover, where it IS the canonical store.
    """
    if _unified_spine():
        try:
            from _env import connect_corebrain
            con = connect_corebrain()
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT count(*) FROM si_artifacts WHERE org_id=%s AND active "
                    "AND spec->>'type' = %s AND artifact_id <> %s", (org, kind, exclude))
                return int(cur.fetchone()[0])
            finally:
                con.close()
        except Exception:
            return MAX_ACTIVE_PROCEDURES  # fail CLOSED: unknown count blocks the install
    return len([a for a in _load_active().get("artifacts", [])
                if a.get("type") == kind and a.get("artifact_id") != exclude])


def _count_active_procedures(org: int, exclude: str = "") -> int:
    """Back-compat name — every existing caller/test targets this. Thin wrapper over
    _count_active_by_type."""
    return _count_active_by_type(org, "hooked_skill", exclude)


def _retire_payload(artifact_id: str, spec: dict | None = None) -> bool:
    """Move an artifact's payload out of its live location on rollback/quarantine, into PROCQDIR
    as audit evidence.

    GENERALIZED 2026-08-31. Only a hooked_skill's payload path is DERIVED from artifact_id
    (PROCDIR/<artifact_id>.md); a slash_command's lives at a SLUG the artifact_id does not encode
    (COMMANDS_DIR/<slug>.md), so retiring one needs the artifact's OWN spec, not just its id.
    `spec=None` preserves the original hooked_skill-only lookup for the two existing callers
    (friction_watchdog's orphan sweep, which only ever scans PROCDIR) — nothing about their
    behaviour changes.

    Without this, rollback would deactivate the trigger and leave the payload file in place —
    "artifact-local rollback" and "watchdog fails safe to quarantine" would both become false
    statements (Fable review 2026-07-27). Moved, not deleted: the file is audit evidence for why
    the artifact was retired. Fail-open — never block a rollback.
    """
    try:
        kind = (spec or {}).get("type") or "hooked_skill"
        if spec is None or kind == "hooked_skill":
            p = _procedure_path(artifact_id)
        elif kind == "slash_command":
            path = ((spec.get("payload") or {}).get("path")) or ""
            if not re.fullmatch(r"[a-z][a-z0-9-]{2,48}\.md", path):
                return False  # unresolvable path — nothing safe to retire
            if COMMANDS_DIR.is_symlink():
                return False
            p = COMMANDS_DIR / path
        elif kind == "workflow_proposal":
            p = _workflow_proposal_path(artifact_id)
        elif kind == "workflow_run":
            # The public, invocable surface is the command file — same path resolution as
            # slash_command, since that IS what makes /slug stop working once retired. The
            # run-manifest is bookkeeping only; best-effort alongside it, never blocking on it.
            path = ((spec.get("payload") or {}).get("path")) or ""
            if not re.fullmatch(r"[a-z][a-z0-9-]{2,48}\.md", path):
                return False
            if COMMANDS_DIR.is_symlink():
                return False
            p = COMMANDS_DIR / path
            try:
                rmp = _workflow_script_path(artifact_id)
                if rmp.is_file() and not rmp.is_symlink():
                    PROCQDIR.mkdir(parents=True, exist_ok=True)
                    os.replace(rmp, PROCQDIR / f"{rmp.stem}.{int(time.time())}.json")
            except Exception:
                pass
        else:
            return False
        if p.is_symlink() or not p.is_file():
            return False
        PROCQDIR.mkdir(parents=True, exist_ok=True)
        # p.stem, not artifact_id: for a hooked_skill p.name == f"{artifact_id}.md" so p.stem IS
        # artifact_id — BYTE-IDENTICAL to the original naming (test_procedure_artifact.py globs
        # PROCQDIR for "*.md" containing the artifact_id). For a slash_command (and a workflow_run,
        # which shares its path shape) p.stem is the human slug; for a workflow_proposal it is
        # "<artifact_id>.workflow" — all still end in .md.
        os.replace(p, PROCQDIR / f"{p.stem}.{int(time.time())}.md")
        return True
    except Exception:
        return False


def _validate_payload(spec: dict) -> tuple[bool, str]:
    """Closed {path, sha256, bytes} for a hooked_skill, path pinned to this artifact's own file,
    and the recorded hash RE-VERIFIED against the bytes actually on disk. The readback is the
    point: it proves the file the dispatcher will read is the file that was validated, and makes
    later tampering detectable — the same trust-anchor pattern block templates use for their
    pinned sha256."""
    ok, why = _closed(spec.get("payload"), {"path", "sha256", "bytes"}, "payload")
    if not ok:
        return (False, why)
    pl = spec["payload"]
    aid = spec["artifact_id"]
    if pl.get("path") != f"{aid}.md":
        return (False, "payload.path must be <artifact_id>.md")
    if not (_is_int(pl.get("bytes")) and 0 < pl["bytes"] <= MAX_PROCEDURE_BYTES):
        return (False, f"payload.bytes must be 1..{MAX_PROCEDURE_BYTES}")
    if not (isinstance(pl.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", pl["sha256"])):
        return (False, "payload.sha256 must be a sha256 hex digest")
    p = _procedure_path(aid)
    if p.is_symlink() or PROCDIR.is_symlink():
        return (False, "payload path or dir is a symlink — refusing")
    try:
        raw = p.read_bytes()
    except Exception:
        return (False, "payload file missing on disk")
    if len(raw) != pl["bytes"]:
        return (False, "payload size mismatch")
    if hashlib.sha256(raw).hexdigest() != pl["sha256"]:
        return (False, "payload hash mismatch (file changed after authoring)")
    return (True, "")


def _validate_command_payload(spec: dict) -> tuple[bool, str]:
    """Same readback-verification contract as _validate_payload, for a slash_command: the payload
    lives at COMMANDS_DIR/<slug>.md — a human-chosen SLUG, not <artifact_id>.md, because the whole
    point of this artifact type is a real, named `/slug` file rather than a hidden one keyed on an
    opaque id."""
    ok, why = _closed(spec.get("payload"), {"path", "sha256", "bytes"}, "payload")
    if not ok:
        return (False, why)
    pl = spec["payload"]
    path = pl.get("path") or ""
    m = re.fullmatch(r"([a-z][a-z0-9-]{2,48})\.md", path)
    if not m:
        return (False, "payload.path must be <slug>.md for a slash_command")
    if not (_is_int(pl.get("bytes")) and 0 < pl["bytes"] <= MAX_COMMAND_BYTES):
        return (False, f"payload.bytes must be 1..{MAX_COMMAND_BYTES}")
    if not (isinstance(pl.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", pl["sha256"])):
        return (False, "payload.sha256 must be a sha256 hex digest")
    if COMMANDS_DIR.is_symlink():
        return (False, "commands dir is a symlink — refusing")
    p = COMMANDS_DIR / path
    if p.is_symlink():
        return (False, "payload path is a symlink — refusing")
    try:
        raw = p.read_bytes()
    except Exception:
        return (False, "payload file missing on disk")
    if len(raw) != pl["bytes"]:
        return (False, "payload size mismatch")
    if hashlib.sha256(raw).hexdigest() != pl["sha256"]:
        return (False, "payload hash mismatch (file changed after authoring)")
    return (True, "")


def _validate_workflow_payload(spec: dict) -> tuple[bool, str]:
    """Same contract, for a workflow_proposal payload at WFPROPDIR/<artifact_id>.workflow.md —
    keyed on artifact_id like a procedure, since a proposal nobody has approved yet has no public
    name."""
    ok, why = _closed(spec.get("payload"), {"path", "sha256", "bytes"}, "payload")
    if not ok:
        return (False, why)
    pl = spec["payload"]
    aid = spec["artifact_id"]
    if pl.get("path") != f"{aid}.workflow.md":
        return (False, "payload.path must be <artifact_id>.workflow.md")
    if not (_is_int(pl.get("bytes")) and 0 < pl["bytes"] <= MAX_WORKFLOW_PROPOSAL_BYTES):
        return (False, f"payload.bytes must be 1..{MAX_WORKFLOW_PROPOSAL_BYTES}")
    if not (isinstance(pl.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", pl["sha256"])):
        return (False, "payload.sha256 must be a sha256 hex digest")
    p = _workflow_proposal_path(aid)
    if p.is_symlink() or WFPROPDIR.is_symlink():
        return (False, "payload path or dir is a symlink — refusing")
    try:
        raw = p.read_bytes()
    except Exception:
        return (False, "payload file missing on disk")
    if len(raw) != pl["bytes"]:
        return (False, "payload size mismatch")
    if hashlib.sha256(raw).hexdigest() != pl["sha256"]:
        return (False, "payload hash mismatch (file changed after authoring)")
    return (True, "")


def _validate_workflow_script_payload(spec: dict) -> tuple[bool, str]:
    """The ADDITIONAL check a workflow_run spec must pass beyond the ordinary command-file payload
    every slash_command gets (judge requirement #5, Gap B 2026-08-31). Cross-verifies the spec's
    `workflow_ref` block against workflow_catalog's own trust-anchored load — never trusting either
    side alone, the same double-readback discipline _validate_payload uses for a hooked_skill.

    Every fact asserted here is checked against TWO independent sources that must agree: the
    spec's own claim (inside its own run-manifest file, itself readback-verified against a pinned
    hash first) and workflow_catalog.load_catalog() (which re-verifies the manifest against
    EXPECTED_SCRIPT_HASHES — reviewed code, not a writable file). Nothing here trusts case-mined
    data for agent_cap or model_tiers; both must be BYTE-IDENTICAL to the catalog's own pinned
    values — the judge's most-worried failure mode was a wrong-scoped fan-out surviving a gate that
    only proved trigger specificity, and this is the check that closes that specific gap: an
    artifact cannot claim a bigger cap or a cheaper tier than the catalog entry it points at."""
    ok, why = _closed(spec.get("workflow_ref"), {"catalog_id", "run_manifest"}, "workflow_ref")
    if not ok:
        return (False, why)
    wr = spec["workflow_ref"]
    cid = wr.get("catalog_id")
    if not isinstance(cid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,48}", cid):
        return (False, "workflow_ref.catalog_id malformed")
    # run_manifest — same {path, sha256, bytes} readback contract as every other payload block
    ok, why = _closed(wr.get("run_manifest"), {"path", "sha256", "bytes"}, "workflow_ref.run_manifest")
    if not ok:
        return (False, why)
    rm = wr["run_manifest"]
    aid = spec["artifact_id"]
    if rm.get("path") != f"{aid}.run.json":
        return (False, "run_manifest.path must be <artifact_id>.run.json")
    if not (_is_int(rm.get("bytes")) and 0 < rm["bytes"] <= MAX_WORKFLOW_SCRIPT_BYTES):
        return (False, f"run_manifest.bytes must be 1..{MAX_WORKFLOW_SCRIPT_BYTES}")
    if not (isinstance(rm.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", rm["sha256"])):
        return (False, "run_manifest.sha256 must be a sha256 hex digest")
    p = _workflow_script_path(aid)
    if p.is_symlink() or WFSCRIPTDIR.is_symlink():
        return (False, "run-manifest path or dir is a symlink — refusing")
    try:
        raw = p.read_bytes()
    except Exception:
        return (False, "run-manifest file missing on disk")
    if len(raw) != rm["bytes"] or hashlib.sha256(raw).hexdigest() != rm["sha256"]:
        return (False, "run-manifest hash/size mismatch (file changed after authoring)")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception:
        return (False, "run-manifest is not valid JSON")
    ok, why = _closed(manifest, {"catalog_id", "script_sha256", "agent_cap", "model_tiers", "params"},
                      "run_manifest content")
    if not ok:
        return (False, why)
    if manifest.get("catalog_id") != cid:
        return (False, "run-manifest catalog_id does not match workflow_ref.catalog_id")
    # CROSS-CHECK AGAINST THE CATALOG ITSELF — the one place agent_cap/model_tiers/params can be
    # PROVEN rather than merely internally consistent with the run-manifest's own say-so.
    try:
        import workflow_catalog as wc
        catalog = wc.load_catalog()
    except Exception as exc:
        return (False, f"workflow catalog unreadable/tampered — refusing: {exc}")
    entry = catalog.get(cid)
    if not entry:
        return (False, f"catalog_id {cid!r} not in the trust-anchored catalog")
    if manifest.get("script_sha256") != entry["sha256"]:
        return (False, "run-manifest script_sha256 does not match the catalog's pinned hash")
    cap = manifest.get("agent_cap")
    if not (wc.valid_agent_cap(cap) and cap == entry["agent_cap"]):
        return (False, f"run-manifest agent_cap must equal the catalog's pinned cap ({entry['agent_cap']})")
    tiers = manifest.get("model_tiers")
    if not (isinstance(tiers, dict) and tiers == entry["model_tiers"]
            and all(wc.valid_model_tier(v) for v in tiers.values())):
        return (False, "run-manifest model_tiers must equal the catalog's pinned tiers exactly")
    params = manifest.get("params")
    schema = entry.get("params_schema") or {}
    if not isinstance(params, dict) or (set(params) - set(schema)):
        return (False, "run-manifest params has keys the catalog schema does not declare")
    for k, kind in schema.items():
        if k in params and not wc._param_ok(kind, params[k]):
            return (False, f"run-manifest params.{k} fails its declared schema kind {kind!r}")
    return (True, "ok")


def _validate_spec(spec: dict, org: int) -> tuple[bool, str]:
    """Hard schema/DSL/invariant check BEFORE any write. Closed key-sets recursively (top level AND
    every nested object), exact int typing (no bool-as-int), restricted-charset artifact_id (so a
    secret can't be smuggled in the id and logged), org match, bounded lease. effect.mode is
    inject-only for every type except run_action (2026-08-31), whose action_id must resolve to a
    verified, non-outward action_registry.py catalog entry — see the effect block below."""
    try:
        if not isinstance(spec, dict):
            return (False, "spec must be an object")
        allowed = {"spec_version", "artifact_id", "case_id", "org_id", "type", "event", "condition",
                   "effect", "tests", "template", "scope", "lease", "generator_version"}
        # A hooked_skill / slash_command / workflow_proposal carries ONE extra closed block: the
        # payload pointer. Key-set stays exact per type — a contract with a payload, or one of
        # these without one, is rejected.
        if spec.get("type") in _PAYLOAD_TYPES:
            allowed = allowed | {"payload"}
        # workflow_run carries ONE more closed block beyond the ordinary command payload: the
        # catalog binding (see _validate_workflow_script_payload). Its own key, not folded into
        # `payload`, because `payload` already means "the command file" for this type — see the
        # comment on _PAYLOAD_TYPES.
        if spec.get("type") == "workflow_run":
            allowed = allowed | {"workflow_ref"}
        ok, why = _closed(spec, allowed, "spec")
        if not ok:
            return (False, why)
        # artifact_id: restricted charset, validated BEFORE it is ever logged/persisted
        if not (isinstance(spec.get("artifact_id"), str) and _ARTIFACT_ID_RE.match(spec["artifact_id"])):
            return (False, "bad artifact_id (must match art_[a-z0-9_])")
        for sk in ("case_id", "type", "generator_version"):
            if not isinstance(spec.get(sk), str) or not spec[sk]:
                return (False, f"{sk} must be a non-empty string")
        if spec.get("spec_version") != 1:
            return (False, "bad spec_version")
        if not _is_int(spec.get("org_id")) or spec["org_id"] != org:
            return (False, "org mismatch")
        if spec.get("scope") != "org_local":
            return (False, "scope must be org_local")
        if spec.get("type") not in ("contract", "hooked_skill", "slash_command", "workflow_proposal",
                                     "run_action", "workflow_run"):
            return (False, "type must be contract, hooked_skill, slash_command, workflow_proposal, "
                          "run_action, or workflow_run")
        # EVENT / OP SURFACE (widened 2026-07-27 for hooked_skill; widened again 2026-08-31 for
        # workflow_proposal, which reuses hooked_skill's work-shape fallback verbatim).
        #
        # A contract is a reminder about HOW to act, so it belongs at the prompt. A hooked_skill or
        # workflow_proposal may instead be about a piece of WORK, where the moment that matters is
        # when the work is about to happen — not when a word appears in a sentence. The live
        # example: the orchestrate-with-Codex-and-Fable artifact fires on the words "work"+"code",
        # when what it should key on is "an Edit is about to touch a shared path". slash_command is
        # excluded from this set on purpose: its trigger is bookkeeping over a literal `/slug`
        # invocation, which only ever arrives on UserPromptSubmit — see _TOOL_EVENT_TYPES.
        #
        # Fenced deliberately: contracts (and slash_command) keep the narrow prompt-grounded op
        # set, because their specificity is proven against a corpus of PROMPTS and a tool-shaped
        # condition has no such grounding there. effect.mode stays inject either way — no new
        # blocking power rides in on this, and enforcement remains reachable only through
        # ORACLE_CATALOG.
        #
        # run_action is DELIBERATELY absent from _TOOL_EVENT_TYPES (GAP A-executable-effect,
        # 2026-08-31, judge requirement 1). Dropping PreToolUse from the runner's v1 registration
        # was a required change, not a preference: friction_dispatch.py's block branch documents
        # that "another thing runs on the event where the security gate lives" is a posture
        # question reserved for Nick, and an out-of-process EXECUTOR there is exactly that
        # question, exit-0 or not. Leaving run_action out of this set means the `allowed_events`
        # line below falls through to its `else` branch and pins it to UserPromptSubmit ONLY, the
        # same way it already did for `contract` — no new set, no new branch, no way to widen it
        # by editing this file without also touching the comment that explains why it is narrow.
        is_hooked = spec.get("type") in _TOOL_EVENT_TYPES
        allowed_events = {"UserPromptSubmit", "PreToolUse"} if is_hooked else {"UserPromptSubmit"}
        if spec.get("event") not in allowed_events:
            return (False, f"event must be one of {sorted(allowed_events)} for type={spec.get('type')}")
        # payload — validated per-type, closed {path, sha256, bytes}. Every path is pinned so a
        # spec can never point the runtime at an arbitrary file, and the hash is re-verified
        # against the bytes on disk at install (see _validate_payload / _validate_command_payload /
        # _validate_workflow_payload).
        _ptype = spec.get("type")
        if _ptype == "hooked_skill":
            ok, why = _validate_payload(spec)
            if not ok:
                return (False, why)
        elif _ptype == "slash_command":
            ok, why = _validate_command_payload(spec)
            if not ok:
                return (False, why)
        elif _ptype == "workflow_proposal":
            ok, why = _validate_workflow_payload(spec)
            if not ok:
                return (False, why)
        elif _ptype == "workflow_run":
            # The command file first — byte-for-byte the same shape/validator a slash_command
            # gets, because that IS the public invocable surface here (see _PAYLOAD_TYPES). Only
            # once that passes does the catalog-binding cross-check run; a workflow_run whose
            # command file itself is malformed is rejected on the same grounds a slash_command
            # would be, before its more expensive catalog re-verification ever runs.
            ok, why = _validate_command_payload(spec)
            if ok:
                ok, why = _validate_workflow_script_payload(spec)
            if not ok:
                return (False, why)
        elif "payload" in spec:
            return (False, "payload is only valid on a hooked_skill, slash_command, workflow_proposal, "
                          "or workflow_run")
        # effect — GAP A-executable-effect (2026-08-31). Every type EXCEPT run_action keeps the
        # ORIGINAL inject-only shape byte-for-byte: closed {mode, message, skill_id}, mode must be
        # "inject". That branch is UNCHANGED below — the fix is scoped to one new type rather than
        # loosening the check every existing artifact already passes through.
        #
        # run_action is the one way to express "when X happens, RUN this" (the gap the owner named
        # verbatim: "why is it not safe — is that not the whole point of the testing of what is
        # being built?"). It is NOT a loosening of "no eval/exec/subprocess" — that ban is about
        # the GENERATOR building code FROM ARTIFACT TEXT (test_static_no_codegen's own docstring),
        # and nothing here does that. A run_action spec can only ever name an action_id that
        # ALREADY exists, byte-identical, human-reviewed, and PR-merged, in action_registry.py's
        # catalog — see that module's docstring for why the catalog and not an artifact field is
        # the trust boundary. Execution itself never happens here or in friction_dispatch.py; both
        # only enqueue/validate. The one place a script is ever spawned is friction_runner.py's
        # single locked subprocess call site (judge requirement 3).
        if spec.get("type") == "run_action":
            ok, why = _closed(spec.get("effect"), {"mode", "action_id"}, "effect")
            if not ok:
                return (False, why)
            eff = spec["effect"]
            if eff.get("mode") != "run":
                return (False, f"run_action effect.mode must be 'run', got {eff.get('mode')}")
            aid_ = eff.get("action_id")
            if not (isinstance(aid_, str) and _ACTION_ID_RE.match(aid_)):
                return (False, "effect.action_id fails charset ^[a-z][a-z0-9_]{1,63}$")
            # INSTALL-TIME VERIFY-WITHOUT-EXECUTE ("dry-run" in the judge's framing). This proves
            # the action_id resolves, is non-outward, and its script hashes clean RIGHT NOW —
            # exactly what action_registry.load_catalog() already checks — WITHOUT spawning a
            # process. The installer never gets a subprocess call site; only friction_runner.py
            # does (judge requirement 3: "runner keeps the single locked-call-site subprocess
            # exemption", singular). This is still a real install-time gate, not a rubber stamp:
            # an action_id that is malformed, outward, unhashed, or whose script has drifted off
            # its pinned hash is refused HERE, before the artifact can ever reach active.json —
            # the fire-time re-check in friction_runner.py is TOCTOU defense on top of this, not a
            # substitute for it.
            try:
                import action_registry as _ar
            except Exception as exc:
                return (False, f"cannot load action_registry to verify action_id: {exc}")
            if _ar.get_action(aid_) is None:
                return (False, f"action_id {aid_!r} is not a valid, verified catalog entry "
                              f"(unknown, malformed, outward, or hash-mismatched)")
        else:
            ok, why = _closed(spec.get("effect"), {"mode", "message", "skill_id"}, "effect")
            if not ok:
                return (False, why)
            eff = spec["effect"]
            if eff.get("mode") != "inject":
                return (False, f"v1 is INJECT-ONLY for type={spec.get('type')}, got mode={eff.get('mode')}")
            if not isinstance(eff.get("message"), str) or not eff["message"]:
                return (False, "effect.message must be a non-empty string")
            if eff.get("skill_id") is not None and not isinstance(eff.get("skill_id"), str):
                return (False, "effect.skill_id must be null or string")
        # condition DSL — v1 restricts operators to the prompt-grounded subset (Codex 5th review)
        # Work-shape ops are available ONLY to a hooked_skill/workflow_proposal on PreToolUse. A
        # prompt-event artifact keeps the prompt-grounded set whatever its type, so the corpus
        # specificity check still governs everything it can actually evaluate.
        ops = (_WORKSHAPE_OPS if (is_hooked and spec.get("event") == "PreToolUse")
               else _V1_ALLOWED_OPS)
        if not _validate_condition(spec.get("condition", {}), allowed_ops=ops):
            return (False, "invalid/empty/over-deep/non-prompt condition DSL")
        # template — closed {id, sha256}
        ok, why = _closed(spec.get("template"), {"id", "sha256"}, "template")
        if not ok:
            return (False, why)
        # lease — closed {max_fires_per_session, expires_at}, bounded cap
        ok, why = _closed(spec.get("lease"), {"max_fires_per_session", "expires_at"}, "lease")
        if not ok:
            return (False, why)
        cap = spec["lease"].get("max_fires_per_session")
        if not (_is_int(cap) and 1 <= cap <= 5):
            return (False, "bad lease cap")
        # tests — closed {positive_ids, negative_ids}, non-empty string-id lists
        ok, why = _closed(spec.get("tests"), {"positive_ids", "negative_ids"}, "tests")
        if not ok:
            return (False, why)
        t = spec["tests"]
        for tk in ("positive_ids", "negative_ids"):
            v = t.get(tk)
            if not (isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)):
                return (False, f"tests.{tk} must be a non-empty list of ids")
            if len(set(v)) != len(v):  # declared-list multiplicity (Codex 5th review)
                return (False, f"tests.{tk} has duplicate ids")
        if set(t["positive_ids"]) & set(t["negative_ids"]):
            return (False, "positive/negative test ids overlap")
        return (True, "ok")
    except Exception as e:
        return (False, f"validation error: {e}")


def _snapshot(active: dict) -> str:
    """Save the current active snapshot by content hash; return the hash."""
    SNAPDIR.mkdir(parents=True, exist_ok=True)
    h = _sha(active)
    snap = SNAPDIR / f"{h}.json"
    if not snap.exists():
        snap.write_text(json.dumps(active, indent=2))
    # keep only the newest ~10 snapshots
    snaps = sorted(SNAPDIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snaps[10:]:
        try: old.unlink()
        except Exception: pass
    return h


def _fetch_corpus_prompts(org: int, n: int = 150) -> list | None:
    """Pull a random sample of REAL past prompts for this org from the trusted corpus. Used by the
    gate to prove a rule is specific (low fire-rate) against actual data. None on DB failure."""
    try:
        from _env import connect_corebrain
        con = connect_corebrain()
        cur = con.cursor()
            # COALESCE(correction_text, prompt_text) — THE COLUMN THE RUNTIME ACTUALLY MATCHES
            # (2026-08-20, found by core-business). `prompt_text` in this table is the PRECEDING
            # turn's prompt; `correction_text` is the message Nick actually typed. The dispatcher
            # matches `prompt_regex` against `ctx["prompt_text"]`, which `_normalize:237` sets from
            # `payload["prompt"]` — the CURRENT user message. So the runtime's prompt corresponds to
            # this table's `correction_text`, not to its `prompt_text`.
            #
            # `ask_miner._member_prompts:562` already documents this and coalesces for the same
            # reason: preferring `prompt_text` "grounded every trigger in text unrelated to the ask"
            # and was the root cause of the nonsense triggers found on 2026-07-27.
            #
            # MEASURED COST OF THE WRONG COLUMN, on life: the reachability check reported 2 dead and
            # 4 fragile artifacts against `prompt_text` and **0 and 0** against the right column —
            # the entire finding was an artifact of the query. Specificity rates move ~20 points
            # (directive 57% -> 37%, emphatic 46% -> 32%); no refusal flips today because everything
            # sits far above the 3% bar, but a trigger near the line would be decided by the wrong
            # population.
        cur.execute(
            "SELECT COALESCE(correction_text, prompt_text) AS p FROM pattern_observations "
            "WHERE org_id=%s AND COALESCE(correction_text, prompt_text) IS NOT NULL "
            "AND length(COALESCE(correction_text, prompt_text)) > 10 "
            "ORDER BY random() LIMIT %s", (org, n))
        rows = [r[0] for r in cur.fetchall()]
        con.close()
        return rows if rows else None
    except Exception:
        return None


# The ONE exemption from deep redaction, keyed by exact path. `fj.redact` masks long hex runs as
# possible secrets, which silently destroyed the pinned payload hash — the fire-time integrity check
# then failed for every procedure, including intact ones (caught by a live tamper test, 2026-07-27).
# A SHA-256 of a local file is a content address, not a credential. The value is re-validated against
# `^[0-9a-f]{64}$` in _validate_payload, so nothing else can hide here.
_REDACT_EXEMPT = {("payload", "sha256")}


def _deep_redact(obj, _path=()):
    """Redact EVERY string in a nested structure before it's persisted (Codex 3rd review) — not
    only effect.message. Safe for our generated condition regexes (plain `\\bword\\b`)."""
    if isinstance(obj, str):
        return obj if _path in _REDACT_EXEMPT else fj.redact(obj)
    if isinstance(obj, list):
        return [_deep_redact(x, _path) for x in obj]
    if isinstance(obj, dict):
        return {k: _deep_redact(v, (_path[-1:] + (k,)) if _path else (k,))
                for k, v in obj.items()}
    return obj


def _unified_spine() -> bool:
    """The cutover switch (WS1): once .claude/state/.si-unified-spine exists, Postgres si_artifacts is
    the canonical store and install()/rollback() go DB-first + re-project. Until then the hardened
    active.json path runs unchanged — so nothing destabilizes before the single switch."""
    return (STATE / ".si-unified-spine").exists()


def install(spec: dict, examples: dict) -> dict:
    try:  # TRUSTED org from the environment — never trust the spec's own org claim (Codex re-review)
        from _env import get_org_id
        org = get_org_id()
    except Exception:
        return {"ok": False, "reason": "no trusted org"}
    # 0) HARD validation FIRST — nothing caller-controlled (esp. artifact_id) is logged or persisted
    #    before this passes, so a secret-shaped id can't leak into the action log (Codex 4th review).
    vok, vwhy = _validate_spec(spec, org)
    if not vok:
        _log("validate_fail", reason=vwhy)  # deliberately NO raw artifact_id — it may be unvalidated
        return {"ok": False, "reason": f"validate: {vwhy}"}
    aid = spec["artifact_id"]  # now guaranteed to match art_[a-z0-9_] — safe to log
    _log("install_begin", artifact_id=aid, org_id=org)

    # PERSIST THE EXAMPLE TEXTS, not only their ids.
    #
    # spec.tests carried positive_ids/negative_ids and nothing else, so the TEXTS that
    # justified the install existed only in the caller's memory and were gone the moment
    # install() returned. Everything downstream that needs to reason about a rule against
    # its own evidence — the tune path, any future re-gating, an audit of why a rule exists
    # — had nothing to read. friction_watchdog's narrowing was written against fields that
    # do not exist anywhere (positive_texts/overfired_texts), so it silently returned None
    # for every artifact and always fell through to quarantine. Found by core-business on a
    # hostile read, 2026-07-28.
    #
    # An artifact that cannot state the evidence for its own condition cannot be tuned
    # against it; it can only be killed. Storing the texts is what makes narrowing possible
    # at all, and it is the same principle as the intent records on hand-written gates.
    _pending_evidence = None
    try:
        # The text lives under hook_input, not at the top level of the example — an example is
        # {id, event, expected, provenance, hook_input:{prompt, assistant_text, ...}}. Reading
        # e["prompt"] silently yielded empty strings, which would have stored a list of blanks
        # and made every artifact look tunable while carrying no evidence. Verified against the
        # real structure rather than assumed, after that exact mistake produced the bug this
        # whole change is fixing.
        # KEEP THE CHANNEL. An example knows whether it was a PROMPT or ASSISTANT text, and
        # collapsing that to a bare string discards it irrecoverably.
        #
        # Why it matters: _OPS includes assistant_regex, which reads ctx["assistant_text"].
        # If a negative captured as a PROMPT is later evaluated with that string injected into
        # assistant_text, an assistant_regex clause can match text the artifact never saw as
        # assistant output — manufacturing "wrong-fire evidence" out of the harness and
        # authorising a tune on something that never happened. The mirror case makes a positive
        # pass through the wrong channel, so the invariant holds for the wrong reason.
        #
        # Found by core-business (finding 7) after the persistence fix made the collapse
        # reachable: F1 and F4 are each correct alone and interact badly. Stored as
        # {text, channel} so evaluation can put each string in the field it actually came from.
        def _ex(e):
            hi = e.get("hook_input") or {}
            if (hi.get("prompt") or "").strip():
                return {"text": hi["prompt"].strip(), "channel": "prompt"}
            if (hi.get("assistant_text") or "").strip():
                return {"text": hi["assistant_text"].strip(), "channel": "assistant"}
            for k, ch in (("prompt", "prompt"), ("assistant", "assistant")):
                if (e.get(k) or "").strip():
                    return {"text": e[k].strip(), "channel": ch}
            return None
        # COMPUTED here (where `examples` is in scope), PERSISTED after the canonical upsert.
        # Evidence lives in si_artifacts.evidence — a column beside spec, not a key inside it
        # (finding 8), and in Postgres rather than a local file (finding 9). Because the store
        # is an UPDATE on the artifact's own row, writing it here would have found no row yet
        # and silently missed on every install.
        _pending_evidence = {
            "positive_texts": [x for x in (_ex(e) for e in (examples.get("positive") or [])) if x][:6],
            "negative_texts": [x for x in (_ex(e) for e in (examples.get("negative") or [])) if x][:12],
        }
    except Exception:
        _pending_evidence = None   # never block an install on bookkeeping
    # 0b) PROLIFERATION CAP (hooked_skill / slash_command / workflow_proposal). Each adds a file
    #     someone or something may be told to read, so unbounded growth is context/surface bloat
    #     with no ceiling. Re-installing an artifact that is already active is an update, not
    #     growth, so it never trips the cap. One dict of (type -> cap) rather than a copy of this
    #     block per type (2026-08-31) — a third hand-written cap check is exactly the accretion
    #     Nick's standing directive forbids.
    _cap = _PROLIFERATION_CAPS.get(spec.get("type"))
    if _cap is not None:
        n = _count_active_by_type(org, spec.get("type"), exclude=aid)
        if n >= _cap:
            _log("procedure_cap", artifact_id=aid, active=n, kind=spec.get("type"))
            return {"ok": False, "reason": f"{spec.get('type')} cap reached ({_cap} active)"}
    # 1) INDEPENDENT re-gate against the REAL corpus — the installer pulls its OWN sample of actual
    #    past prompts (org-scoped) and rejects any rule that fires on too many; never trusts
    #    caller-supplied 'real_neighbor' labels (Codex 3rd review). Fail-closed if the corpus is
    #    unavailable.
    corpus = _fetch_corpus_prompts(org)
    # A PreToolUse work-shape artifact keys on tool_name / mutability, which do not exist in the
    # prompt corpus — running the 3%-of-prompts check against it would measure nothing and pass
    # everything, which is worse than not running it. Its specificity comes instead from the
    # condition being a CONJUNCTION over a closed tool vocabulary (tool_name_in + mutability), and
    # its blast radius is bounded the same way every other artifact's is: inject-only, fire-budgeted,
    # watchdog-swept, rollback-able. Positives and negatives are still evaluated in full.
    worksh = spec.get("type") in _TOOL_EVENT_TYPES and spec.get("event") == "PreToolUse"
    ok, why = tg.gate(spec, examples,
                      corpus_prompts=None if worksh else (corpus if corpus is not None else []))
    if not ok:
        # UNDECIDABLE IS LOGGED AS ITSELF, NOT AS A FAILURE (2026-08-12, found by core-finance).
        #
        # This wrote `test_fail` for all three of the gate's refusal kinds. On a small-corpus seat
        # that produced 234 `test_fail` rows of which 234 were undecidable — two artifacts retried
        # 117 times each, the rate rising, against a precondition roughly eighteen days from being
        # satisfiable. A reader of that log concludes the seat has two badly-broken rules; in fact
        # they had never been tested. The refusal is unchanged and still blocks the install — only
        # the record of WHY now matches what happened.
        #
        # `corpus_n` is recorded because it is the thing that has to move before the verdict can.
        # A refusal that does not say what would change it reads as permanent.
        if tg.is_undecidable(why):
            _corpus_n = len(corpus) if corpus is not None else 0
            # DEDUP ON UNCHANGED STATE — NOT A BACKOFF. core-finance's design, and their argument
            # against mine is the reason: a delay curve postpones detection at the one moment you
            # want the retry prompt, which is when the corpus finally crosses MIN_CORPUS. It trades
            # a real capability for log hygiene. So the gate still runs EVERY pass — nothing is
            # deferred — and only the LOG WRITE is suppressed while the state is identical.
            #
            # Measured cost of not doing this, from finance's own rows: corpus growing ~0.5/day
            # (27 -> 31 over eight days) against a threshold of 40 is ~18 more days, at ~100 rows
            # per day and rising — roughly 1800 further rows of a known-unsatisfiable state, in the
            # same log where real signals live and which is itself a diagnostic surface.
            #
            # `corpus_n` is the discriminator, which is the second reason that field earned its
            # place: one row per artifact per corpus increment is exactly the information content
            # actually present. The retry COUNT is deliberately not preserved — it is the same fact
            # repeated, and the row's own ts plus the next increment bound it.
            if not _undecidable_is_new(aid, _corpus_n):
                return {"ok": False, "undecidable": True, "unchanged": True,
                        "reason": f"gate: {why}"}
            _log("test_undecidable", artifact_id=aid, reason=why, corpus_n=_corpus_n)
            return {"ok": False, "undecidable": True, "reason": f"gate: {why}"}
        _log("test_fail", artifact_id=aid, reason=why)
        return {"ok": False, "reason": f"gate: {why}"}
    _log("test_pass", artifact_id=aid)
    # 2) persist. Strip engine-only fields + deep-redact either way.
    clean = _deep_redact({k: v for k, v in spec.items() if not k.startswith("_")})
    try:
        if isinstance(clean.get("effect"), dict) and clean["effect"].get("message"):
            clean["effect"]["message"] = clean["effect"]["message"][:2000]
    except Exception:
        pass
    if _unified_spine():
        # DB-FIRST canonical write + rebuild the projection (Codex WS1: Postgres authoritative,
        # active.json disposable — never file-first; upsert saves prior_spec for reversibility).
        import si_project
        try:
            si_project.upsert(org, clean)  # canonical commit — the artifact is now durably installed
        except Exception as e:
            _log("install_error", artifact_id=aid, error=str(e)[:200])
            return {"ok": False, "reason": f"canonical write failed: {e}"}
        # Evidence AFTER the canonical write — it is an UPDATE on the row upsert just created.
        if _pending_evidence:
            _write_evidence(aid, _pending_evidence, org)   # fail-soft, but logs on failure
        try:
            proj = si_project.project(org)
            _log("install_commit", artifact_id=aid, event=spec.get("event"),
                 mode=spec.get("effect", {}).get("mode"), projected=proj.get("artifacts"))
            return {"ok": True, "reason": "installed"}
        except Exception as e:
            # canonical is committed; only the runtime projection failed — the next close's project()
            # self-heals active.json. Distinct state so this is NOT retried as a fresh install (Codex).
            _log("install_projection_pending", artifact_id=aid, error=str(e)[:200])
            return {"ok": True, "reason": "canonical_committed_projection_pending"}
    # LEGACY (pre-cutover) atomic active.json path — unchanged, hardened, single-writer.
    try:
        active = _load_active()
        prior_hash = _snapshot(active)
        arts = [a for a in active.get("artifacts", []) if a.get("artifact_id") != aid]
        clean["_prior_snapshot"] = prior_hash
        clean["_installed_at"] = int(time.time())
        arts.append(clean)
        _atomic_write(ACTIVE, {"artifacts": arts})
        _log("install_commit", artifact_id=aid, event=spec.get("event"),
             mode=spec.get("effect", {}).get("mode"), prior_snapshot=prior_hash)
        return {"ok": True, "reason": "installed"}
    except Exception as e:
        _log("install_error", artifact_id=aid, error=str(e)[:200])
        return {"ok": False, "reason": f"install error: {e}"}


def _event_is_dispatchable(event) -> tuple:
    """Is there a LIVE registration of the dispatcher on this event? (ok, reason)

    WHY (2026-08-13). Both entries in `artifact_typer.ORACLE_CATALOG` — the only path to an
    enforceable block — declare `event: "Stop"`. The Stop registration of friction-dispatch was
    RETIRED 2026-08-06 under Nick's policy that nothing may drive the agent after the reply is sent;
    `bin/hook-registry.json` tombstones it properly, with sound reasoning.

    Four days later, on 08-10, this function installed two shadow blocks from those templates
    anyway. It was not a bypass: it validates the event against a hash-pinned template and did so
    correctly — **the template was stale.** The result is a lifecycle that cannot turn:

        shadow_block_install events : 6   (2 artifacts x 3 revisions)
        shadow_block events         : 0   — and 0 is the only value reachable
        friction_promote input      : empty by construction

    One of them (`art_331154505`) is still ACTIVE and can never fire on any turn.

    Checked against the LIVE registration rather than a hardcoded event list, deliberately. A
    constant here would be a third place recording which events dispatch — after settings.json and
    the registry — and would go stale the same way the template did. Reading the registration means
    this re-enables itself automatically if a Stop dispatcher ever returns, and needs no edit.

    FAILS CLOSED. If the registration cannot be read, a block install is refused. That matches the
    rule already applied to specificity a few lines below — "for an artifact that can stop work,
    unprovable specificity must fail closed" — and the same reasoning holds here: a config read
    failure must not be indistinguishable from a live dispatcher.
    """
    if not event or not isinstance(event, str):
        return (False, "block spec has no event")
    try:
        import json as _j
        # STATE is <root>/.claude/state, so settings.json is its sibling. Derived from the same
        # constant the rest of this module uses rather than re-deriving the root a second way.
        settings = STATE.parent / "settings.json"
        conf = _j.loads(settings.read_text())
        live = set()
        for ev, groups in (conf.get("hooks") or {}).items():
            for g in groups or []:
                for hk in (g.get("hooks") or []):
                    if "friction-dispatch" in str(hk.get("command", "")):
                        live.add(ev)
    except Exception as exc:
        return (False, f"cannot read hook registration to prove {event} dispatches ({exc}) — "
                       f"refusing a block install rather than assuming")
    if event not in live:
        return (False, f"event {event!r} has no live friction-dispatch registration "
                       f"(live: {sorted(live) or 'none'}) — an artifact installed here could never "
                       f"fire, so the block lifecycle would never turn")
    return (True, "")


def install_shadow_block(spec: dict, examples: dict) -> dict:
    """The ONLY path that installs a block artifact. Tightly bounded (Codex WS4): the condition/event
    MUST match a hash-pinned enforcement template EXACTLY (no arbitrary block conditions), enforced is
    FORCED False (shadow — it never actually blocks until friction_promote's window + an explicit flip),
    and it gates on the oracle truth-table examples (no corpus — a block's safety is its oracle, not
    fire-rate). Persists DB-first when unified, else active.json."""
    try:
        from _env import get_org_id
        org = get_org_id()
    except Exception:
        return {"ok": False, "reason": "no trusted org"}
    if not isinstance(spec, dict) or spec.get("effect", {}).get("mode") != "block":
        return {"ok": False, "reason": "not a block spec"}
    _ok_ev, _why_ev = _event_is_dispatchable(spec.get("event"))
    if not _ok_ev:
        _log("block_install_dead_event", artifact_id=spec.get("artifact_id"), reason=_why_ev)
        return {"ok": False, "reason": _why_ev}
    if not _is_int(spec.get("org_id")) or spec.get("org_id") != org:
        return {"ok": False, "reason": "org mismatch"}
    aid = spec.get("artifact_id", "")
    if not (isinstance(aid, str) and _ARTIFACT_ID_RE.match(aid)):
        return {"ok": False, "reason": "bad artifact_id"}
    # CLOSED KEY-SET (Codex review, 2026-07-27). This path validated far less than install() did, so
    # a block spec could carry arbitrary extra top-level fields straight into the canonical store —
    # and blocks are the artifacts that can eventually STOP work. A block should be held to a higher
    # bar than an inject, not a lower one. `enforced` is the one field a block legitimately carries
    # that a contract does not.
    # _closed() requires its allowed set EXACTLY — missing keys fail as well as extra ones. So the
    # genuinely-optional fields are stripped before the check rather than listed in it; listing them
    # made `_provenance` mandatory on every block, which the first version of this did and which its
    # own test caught immediately.
    _required = {"spec_version", "artifact_id", "case_id", "org_id", "type", "event",
                 "condition", "effect", "tests", "template", "scope", "lease", "generator_version"}
    _optional = {"enforced", "_provenance"}
    ok, why = _closed({k: v for k, v in spec.items() if k not in _optional}, _required, "block spec")
    if not ok:
        return {"ok": False, "reason": why}
    ok, why = _closed(spec.get("effect"), {"mode", "message", "skill_id"}, "effect")
    if not ok:
        return {"ok": False, "reason": why}
    ok, why = _closed(spec.get("template"), {"id", "sha256"}, "template")
    if not ok:
        return {"ok": False, "reason": why}
    ok, why = _closed(spec.get("lease"), {"max_fires_per_session", "expires_at"}, "lease")
    if not ok:
        return {"ok": False, "reason": why}
    cap = spec["lease"].get("max_fires_per_session")
    if not (_is_int(cap) and 1 <= cap <= 5):
        return {"ok": False, "reason": "lease cap out of range"}
    # condition/event MUST match a hash-verified template exactly
    try:
        import artifact_generator as ag
        tpls = ag._load_templates()
    except Exception as e:
        return {"ok": False, "reason": f"templates unavailable: {e}"}
    # TEMPLATE IDENTITY MUST BIND (Codex 2nd round, 2026-07-27 — CRITICAL).
    #
    # This used to check only that the condition matched SOME pinned template, never that the spec's
    # own `template.id` identified THAT one. friction_promote trusts template.id to decide what may
    # be enforced, so a spec carrying the adversarial-review condition while labelled with the
    # promotable deliverable id would install as shadow and then be auto-enforced — defeating
    # never_promote completely. The fence was real but reachable around.
    #
    # Now: resolve exactly ONE template BY ID, then require event, condition, effect mode and the
    # recorded digest to all match that same entry.
    tid = (spec.get("template") or {}).get("id")
    match = next((k for k, t in tpls.items() if t.get("template_id") == tid), None)
    if match is None:
        return {"ok": False, "reason": f"template.id {tid!r} is not a pinned enforcement template"}
    t = tpls[match]
    if t.get("effect_mode") != "block":
        return {"ok": False, "reason": "named template is not a block template"}
    if spec.get("event") != t["event"] or spec.get("condition") != t["condition"]:
        return {"ok": False,
                "reason": "block event/condition does not match the template it claims to be"}
    try:
        import artifact_generator as _ag2
        if (spec.get("template") or {}).get("sha256") != _ag2.EXPECTED_TEMPLATE_HASHES.get(match):
            return {"ok": False, "reason": "block template.sha256 does not match the pinned digest"}
    except Exception as e:
        return {"ok": False, "reason": f"cannot verify template digest: {e}"}
    if not _validate_condition(spec.get("condition", {}), allowed_ops=_ALLOWED_OPS):
        return {"ok": False, "reason": "invalid condition DSL"}
    # THE OVER-BROAD CHECK WAS OFF ON THIS PATH, WHICH IS THE ONE THAT MATTERS MOST.
    #
    # `corpus_prompts=None` makes friction_test_gate skip the specificity test entirely — both
    # branches fall through, silently. So the single check that measures whether a trigger fires on
    # a large fraction of real prompts was disabled for BLOCKING artifacts, while inject-only
    # artifacts were held to 3%. This file's own comment fifty lines up says "A block should be held
    # to a higher bar than an inject, not a lower one." It was held to no bar.
    #
    # Dosed with the trigger core-business found in its live state — \b(want|company|they|core|
    # really|work)\b — against this seat's real corpus: it fires on 83 of 150 prompts, 55.3%. The
    # gate refuses it at 3% the moment a corpus is supplied, and admitted it while None was passed.
    #
    # An EMPTY corpus is not a pass here: friction_test_gate returns "no corpus sample available to
    # prove specificity", which refuses. For an artifact that can stop work, unprovable specificity
    # must fail closed — the alternative is that a database hiccup is indistinguishable from a
    # narrow trigger.
    _block_corpus = _fetch_corpus_prompts(org)
    ok, why = tg.gate(spec, examples,
                      corpus_prompts=_block_corpus if _block_corpus is not None else [])
    if not ok:
        _log("block_gate_fail", artifact_id=aid, reason=why)
        return {"ok": False, "reason": f"gate: {why}"}
    clean = _deep_redact({k: v for k, v in spec.items() if not k.startswith("_")})
    clean["enforced"] = False  # forced shadow at persistence too — belt and suspenders
    try:
        if _unified_spine():
            import si_project
            si_project.upsert(org, {**clean, "_provenance": "enforcement"}, allow_block=True)
            si_project.project(org)
        else:
            active = _load_active()
            arts = [a for a in active.get("artifacts", []) if a.get("artifact_id") != aid]
            clean["_installed_at"] = int(time.time())
            arts.append(clean)
            _atomic_write(ACTIVE, {"artifacts": arts})
        _log("shadow_block_install", artifact_id=aid, event=spec.get("event"))
        return {"ok": True, "reason": "shadow block installed (enforced=false)"}
    except Exception as e:
        _log("shadow_block_error", artifact_id=aid, error=str(e)[:200])
        return {"ok": False, "reason": str(e)}


def rollback(artifact_id: str, reason: str = "") -> dict:
    """ARTIFACT-LOCAL rollback: remove ONLY the quarantined artifact, leaving every other
    (healthy, possibly later-installed) rule intact. Snapshots are kept for audit but NOT
    restored wholesale — a whole-snapshot restore would silently delete healthy rules installed
    after this one (Codex #7).

    A CALLER THAT CAN SAY WHY IS QUARANTINING; ONE THAT CANNOT IS ONLY DEACTIVATING. That is the
    whole distinction, and it is expressed by `reason` rather than by a second function, because a
    second function beside this one is how the fleet got here (2026-08-12).

    Until now every caller landed on si_project.deactivate() — active=false, quarantined untouched.
    But the generator's case-retirement query reads QUARANTINED, not active (friction_loop.py:194),
    so the watchdog removing a misbehaving artifact never reached the signal that stops the case
    being re-authored. Remove -> re-author -> re-install -> remove. Nothing accumulated, and each
    step looked correct in isolation.

    si_project.quarantine() had NO caller anywhere; its own module docstring said "(used by
    watchdog)" — describing an integration that was never wired. Consolidating here rather than
    adding a quarantine() call beside the deactivate() call, per the standing directive: when a
    subsystem already has two paths for one job, unify it, do not add a third.
    """
    if not (isinstance(artifact_id, str) and _ROLLBACK_ID_RE.match(artifact_id)):
        return {"ok": False, "reason": "bad artifact_id"}  # validate BEFORE any log (Codex 5th review)
    if artifact_id.startswith("legacy_"):
        # a legacy human-authored guardrail is NEVER removed by the generic/autonomous rollback path
        # (watchdog, self-quarantine) — that would silently drop a core behavior. Legacy removal is a
        # separate deliberate admin action (Codex WS1 review).
        return {"ok": False, "reason": "legacy artifact — use the admin path, not generic rollback"}
    _log("rollback_begin", artifact_id=artifact_id)
    # ORDER MATTERS (Codex MEDIUM #6): deactivate the trigger FIRST, retire the payload only after
    # the canonical write succeeds. Retiring first meant a failed deactivation left an ACTIVE trigger
    # pointing at a payload that no longer existed. This ordering fails the safe way instead — a
    # deactivated artifact with a stale payload file, which the watchdog's orphan sweep then retires.
    if _unified_spine():
        # DB path: deactivate the one artifact in si_artifacts + rebuild the projection.
        try:
            from _env import get_org_id
            import si_project
            # READ THE SPEC BEFORE QUARANTINE/DEACTIVATE (2026-08-31). _retire_payload needs to
            # know the artifact's TYPE and (for a slash_command) its slug to find the right file —
            # only a hooked_skill's payload path is derivable from artifact_id alone. A read
            # failure here is not fatal: _retire_payload(id, None) falls back to the original
            # hooked_skill-only lookup, so the worst case is a stray payload file, never a wrong
            # deletion (see _retire_payload's own docstring).
            _spec_for_retire = None
            try:
                from _env import connect_corebrain as _cc
                con = _cc()
                try:
                    cur = con.cursor()
                    cur.execute("SELECT spec FROM si_artifacts WHERE org_id=%s AND artifact_id=%s",
                               (get_org_id(), artifact_id))
                    row = cur.fetchone()
                    _spec_for_retire = row[0] if row else None
                finally:
                    con.close()
            except Exception:
                _spec_for_retire = None
            if reason:
                # Durable: quarantined=true is what suppresses re-authoring. project() selects
                # `active AND NOT quarantined`, so this removes it from the live set too — the
                # extra deactivate() the old path did is not needed to take it out of service.
                changed = si_project.quarantine(get_org_id(), artifact_id, reason)
                _restored = "db_quarantined"
            else:
                changed = si_project.deactivate(get_org_id(), artifact_id)
                _restored = "db_deactivated"
            si_project.project(get_org_id())
            _retire_payload(artifact_id, _spec_for_retire)
            _log("rollback_commit", artifact_id=artifact_id, restored=_restored)
            return {"ok": bool(changed), "reason": "artifact removed" if changed else "not active"}
        except Exception as e:
            _log("rollback_error", artifact_id=artifact_id, error=str(e)[:200])
            return {"ok": False, "reason": str(e)}
    try:
        active = _load_active()
        before = active.get("artifacts", [])
        # The artifact's OWN record, read BEFORE it is filtered out — _retire_payload needs its
        # type (and, for a slash_command, its slug) to retire the right file. None if it was
        # already gone, which _retire_payload treats as "assume hooked_skill" (unchanged
        # behaviour for the only type that existed before 2026-08-31).
        _spec_for_retire = next((a for a in before if a.get("artifact_id") == artifact_id), None)
        arts = [a for a in before if a.get("artifact_id") != artifact_id]
        if len(arts) == len(before):
            return {"ok": False, "reason": "not active"}
        _atomic_write(ACTIVE, {"artifacts": arts})
        _retire_payload(artifact_id, _spec_for_retire)
        _log("rollback_commit", artifact_id=artifact_id, restored="artifact_removed")
        return {"ok": True, "reason": "artifact removed"}
    except Exception as e:
        _log("rollback_error", artifact_id=artifact_id, error=str(e)[:200])
        return {"ok": False, "reason": str(e)}
