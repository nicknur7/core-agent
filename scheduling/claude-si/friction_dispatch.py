#!/usr/bin/env python3
"""friction_dispatch.py — THE ONE static, human-reviewed interpreter for friction artifacts.

This is the whole safety story: generated artifacts are DECLARATIVE JSON (a closed DSL);
this module is the only thing that ever INTERPRETS one, and it is pinned/reviewed. It has NO
eval/exec, no shell, no dynamic import, no artifact-controlled filesystem path. A malformed
or over-broad artifact can at most express a bad *condition* — contained by fire-budget +
lease + the watchdog's out-of-band rollback. On ANY internal error it FAILS OPEN (allows the
action) — a broken artifact must never brick a session.

"the only thing that ever executes" WAS true, and stopped being true 2026-08-31 when mode:"run"
shipped (GAP A-executable-effect). It stays true for THIS module: a "run" effect only appends a
request row (_enqueue_run) — no process-spawn, no exec, no new banned surface here, and this file
stays in test_static_no_codegen's full-ban scan unchanged. Actual execution moved to a NEW,
separate, out-of-process module (friction_runner.py) with its own single locked process-launch
call site, specifically so an action's success or failure can never feed back into the decision
this file makes about whether to fire. Read friction_runner.py's own docstring for that half of
the story; this file's guarantee is now "the only thing that ever decides", not "the only thing
that ever runs a command".

Invoked as a hook (UserPromptSubmit / PreToolUse / Stop). Reads the hook payload on stdin,
evaluates every active org-local artifact whose event matches, applies EVERY firing effect
(inject guidance / block / enqueue a run-request) — combined and length-bounded at :584 —
and appends an action-log row.

    Said "the first firing effect" until 2026-08-13 (core-finance DOSE 41). The behaviour was
    deliberately changed and the reason is recorded twenty lines into run() at :412: a unified
    spine carries many contracts, and first-match-only "silently dropped every contract after the
    first" (Codex, WS1). The fix landed; this sentence kept describing the version it replaced.

    Worth one line because of WHERE it sits. This header is the file's safety story — it is what a
    reviewer reads to decide whether autonomous artifact generation is contained — so a stale
    sentence in it is read as a current guarantee. A reader taking "applies the first" at face value
    would conclude at most one artifact can affect any turn, and size the blast radius accordingly.

Active artifacts live in `.claude/state/friction-artifacts/active.json` (written atomically by
the installer). This module NEVER writes artifacts and NEVER edits settings.json.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

# Global master kill-switch for ALL enforcement. Autonomous enforcement is on (Nick 2026-07-23:
# test-gate + reversibility, not a human gate) — but a block still fires ONLY if it ALSO carries
# per-artifact enforced=True, which friction_promote.auto_promote sets only after a verified-oracle
# block clears its shadow-proof window. Flip this to False to instantly disable ALL enforcement fleet-
# wide (the emergency brake), or set env FRICTION_BLOCKS_OFF=1.
BLOCKS_ENABLED = os.environ.get("FRICTION_BLOCKS_OFF") != "1"

# SEAT VIA THE CANONICAL RESOLVER. This read CLAUDE_PROJECT_DIR only, while bin/core_seat.py —
# which the runner, the casebook, the gate and the miners all use — honours CORE_INSTANCE FIRST and
# CLAUDE_PROJECT_DIR second. Two resolvers, one subject, inside the component that decides which
# artifacts fire and where their fires are recorded.
#
# Consequence, hit while testing this very change: a sandboxed run with CORE_INSTANCE set wrote its
# fire row into the LIVE seat, because this line never looked at CORE_INSTANCE. I read the empty
# sandbox log and nearly concluded the dispatcher does not log at all.
#
# Fail-soft: if core_seat cannot be imported (a fork, a partial checkout), keep the old behaviour
# rather than break a hook that must never block.
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
    # brain-pg too, for the single org resolver (_env.get_org_id). Without this the import inside
    # run() raises, the except returns 0, and the dispatcher fails CLOSED — every artifact silently
    # stops firing. test_dispatch_ignores_machine_turns caught exactly that, and calls it "the
    # unrecoverable direction" because nothing downstream can tell a no-fire from a clean turn.
    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scheduling" / "brain-pg"))
    from core_seat import seat_root as _seat_root
    INSTANCE = _seat_root(fallback=Path(__file__).resolve().parents[2])
except Exception:
    INSTANCE = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STATE = INSTANCE / ".claude" / "state"
ACTIVE = STATE / "friction-artifacts" / "active.json"
ACTION_LOG = STATE / "friction-action-log.jsonl"
FIRE_COUNT = STATE / "friction-artifacts" / "fire-counts.json"   # per-session fire budget
# RUN_QUEUE (GAP A-executable-effect, 2026-08-31). This dispatcher NEVER executes a run_action —
# it only appends a request row here. friction_runner.py is a SEPARATE, out-of-process hook that
# drains it; see that module's docstring for why the split exists (short version: an action
# failure must never be able to affect the dispatch decision that fired it, and this file must
# never grow a second process-launch call site beside the runner's one locked one). Same STATE seat
# resolution as ACTIVE/ACTION_LOG above — the queue is per-Core, gitignored runtime state.
RUN_QUEUE = STATE / "friction-artifacts" / "run-queue.jsonl"
RUN_QUEUE_LOCK = STATE / "friction-artifacts" / "run-queue.lock"   # shared with friction_runner.py
_QUEUE_LOCK_BUDGET_SEC = 0.5     # bounded retry, never a blocking flock() — see _queue_lock()
MAX_RUN_QUEUE_LINES = 500        # size cap (judge requirement 4) — a runaway enqueue must not grow
                                  # an unbounded file; once at cap, new rows are dropped and logged,
                                  # never silently lost without a trace.
# Global kill-switch for run_action, mirroring BLOCKS_ENABLED exactly (same env-var shape, same
# "flip to instantly disable fleet-wide" semantics) — a SEPARATE flag from BLOCKS_ENABLED because
# blocking and running are different authorities with different worst cases; disabling one must
# never silently disable the other.
RUN_ENABLED = os.environ.get("FRICTION_RUN_OFF") != "1"

MAX_TEXT = 8000          # bound inputs so regex backtracking is bounded (ReDoS defense)
MAX_REGEX_LEN = 256
SHADOW_OBS_PER_SESSION = 3   # storage bound on shadow observations; keeps fires AND sessions
_OPS = {"event_is", "tool_name_in", "tool_mutability_is", "prompt_regex", "assistant_regex",
        "tool_call_present", "tool_result_present", "state_flag_is", "artifact_delivery_present",
        "adversarial_review_present"}
MUTATING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}

# self-contained secret redaction (belt-and-suspenders — also redacted at install)
_SECRET_RX = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|Bearer\s+[A-Za-z0-9._-]{12,}"
    r"|-----BEGIN[^-]+PRIVATE KEY-----|[A-Fa-f0-9]{40,})")


def _redact(text):
    return _SECRET_RX.sub("[REDACTED]", text) if isinstance(text, str) else (text or "")


class _RegexTimeout(Exception):
    pass


def _validate_regex(pattern: str) -> bool:
    """Reject patterns prone to catastrophic backtracking or disallowed by the DSL.
    (Defense in depth — the router only ever generates flat alternations; this guards
    against a malformed/hand-crafted artifact reaching the interpreter.)"""
    if not isinstance(pattern, str) or not (1 <= len(pattern) <= MAX_REGEX_LEN):
        return False
    if any(b in pattern for b in ("(?<", "(?=", "(?!", "\\1", "\\2", "\\3")):
        return False
    # nested / adjacent / counted quantifiers: (x+)+  (x*)*  a++  a{2,}{3,}  etc.
    if re.search(r"\)[*+?][*+?]", pattern) or re.search(r"[*+?][*+?]", pattern):
        return False
    if re.search(r"\}[{*+?]", pattern):          # counted quantifier followed by another
        return False
    if re.search(r"\)[*+?{]", pattern):          # ANY quantified/re-quantified group, e.g. (a{1,9}){1,9}
        return False
    if re.search(r"\([^)]*[*+][^)]*\)[*+]", pattern):
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def _record_regex_failure(pattern, exc):
    """Append an unevaluatable pattern to state so a silently-degrading matcher becomes visible.

    Best-effort and never raises: a failure to RECORD a failure must not take down the dispatch
    path that was still working. But it is the only thing standing between "this artifact decided
    not to fire" and "this artifact can no longer decide anything".
    """
    try:
        import json as _json
        import os as _os
        import time as _time
        root = _os.environ.get("CORE_INSTANCE") or _os.path.dirname(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        path = _os.path.join(root, ".claude", "state", "regex-eval-failures.jsonl")
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(_json.dumps({
                "ts": int(_time.time()),
                "pattern": str(pattern)[:200],
                "error": "%s: %s" % (type(exc).__name__, exc),
            }) + "\n")
    except Exception:
        pass


def _safe_regex(pattern: str, text: str, trusted: bool = False) -> bool:
    # trusted=True (human-authored legacy contracts only) skips the STRUCTURAL pre-check — which is
    # tuned for machine-generated `\bword\b` patterns and over-rejects legitimate optional groups like
    # `(just )?` — but ALWAYS keeps the 50ms runtime timer, the real ReDoS guard (Codex WS1).
    if not isinstance(pattern, str) or not (1 <= len(pattern) <= MAX_REGEX_LEN):
        return False
    if not trusted and not _validate_regex(pattern):
        return False
    old = None
    have_timer = False
    try:
        old = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(_RegexTimeout()))
        signal.setitimer(signal.ITIMER_REAL, 0.05)  # 50ms hard cap — bounds any residual backtracking
        have_timer = True
    except (ValueError, AttributeError, OSError):
        have_timer = False  # not main thread / unsupported → rely on validation alone
    try:
        return re.search(pattern, (text or "")[:MAX_TEXT], re.IGNORECASE) is not None
    except (_RegexTimeout, re.error) as e:
        # RECORDED, NOT SWALLOWED. core-business found this 2026-08-09: a ReDoS timeout or a regex
        # error returned False, which the caller reads as "the artifact does not apply". So an
        # enforcement artifact whose pattern can no longer be EVALUATED silently stops firing, and
        # is indistinguishable from one that correctly decided not to. Latent on business; LIVE
        # here, where active.json carries 20 artifacts.
        #
        # Still returns False, deliberately: an undecidable match must not FIRE either, because a
        # wrongly-fired enforcement advisory is its own harm and would train Nick to ignore them.
        # The defect was never the return value — it was that the degradation left no trace. A
        # pattern that keeps failing now surfaces instead of quietly never matching again.
        _record_regex_failure(pattern, e)
        return False
    finally:
        if have_timer:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if old is not None:
                    signal.signal(signal.SIGALRM, old)
            except Exception:
                pass


def evaluate(cond: dict, ctx: dict, trusted: bool = False) -> bool:
    """Recursive closed-DSL evaluator. Unknown shapes → False (fail closed on match). `trusted` (set
    only for human-authored legacy artifacts) relaxes the structural regex pre-check, not the timer."""
    if not isinstance(cond, dict):
        return False
    if "all" in cond:
        return all(evaluate(c, ctx, trusted) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, ctx, trusted) for c in cond["any"])
    if "none" in cond:
        return not any(evaluate(c, ctx, trusted) for c in cond["none"])
    op = cond.get("op")
    val = cond.get("value")
    if op not in _OPS:
        return False
    if op == "event_is":
        return ctx.get("event") == val
    if op == "tool_name_in":
        return isinstance(val, list) and ctx.get("tool_name") in val
    if op == "tool_mutability_is":
        return ctx.get("tool_mutability") == val
    if op == "prompt_regex":
        return _safe_regex(val, ctx.get("prompt_text", ""), trusted)
    if op == "assistant_regex":
        return _safe_regex(val, ctx.get("assistant_text", ""), trusted)
    if op == "tool_call_present":
        return bool(ctx.get("has_tool_call")) == bool(val) if val is not None else bool(ctx.get("has_tool_call"))
    if op == "tool_result_present":
        return bool(ctx.get("has_tool_result")) == bool(val) if val is not None else bool(ctx.get("has_tool_result"))
    if op == "state_flag_is":
        return isinstance(val, dict) and ctx.get("state_flags", {}).get(val.get("flag")) == val.get("equals")
    if op == "adversarial_review_present":
        # True when an adversarial review ran this turn. Absent evidence reports True so a
        # block can never fire because the transcript was unreadable (same fail-safe polarity
        # as artifact_delivery_present).
        return bool(ctx.get("adversarial_review", True)) is bool(val)
    if op == "artifact_delivery_present":
        return bool(ctx.get("artifact_delivered")) == bool(val) if val is not None else bool(ctx.get("artifact_delivered"))
    return False


def _normalize(payload: dict, event: str) -> dict:
    """Map a hook payload to the DSL context. Never stores/inspects raw secrets — structural only.

    Real oracles (not stubs) are computed ONLY at Stop, where deliverable blocks live, to keep the
    UserPromptSubmit path fast. FAIL-SAFE so a block NEVER fires on unknown (undetectable delivery →
    DELIVERED, undetectable request → NOT-requested). There is NO payload override in production — the
    oracle is computed from the transcript, so a hook payload can't pin it (`_test` was an
    unauthenticated override — removed, Codex WS4). Tests use normalize_for_test()."""
    tool = payload.get("tool_name")
    oracles = _stop_oracles(payload) if event == "Stop" else {"state_flags": {}, "artifact_delivered": False}
    return {
        "event": event,
        "tool_name": tool,
        "tool_mutability": ("mutating" if tool in MUTATING_TOOLS else "readonly") if tool else None,
        "prompt_text": payload.get("prompt") or payload.get("user_prompt") or "",
        "assistant_text": payload.get("assistant_text", ""),
        "has_tool_call": payload.get("tool_name") is not None,
        "has_tool_result": bool(payload.get("tool_result")),
        **oracles,
    }


def _stop_oracles(payload: dict) -> dict:
    """ONE bounded transcript read + ONE scan → both oracle values (Codex WS4). FAIL-SAFE on unknown:
    not-requested + delivered, so a block never fires when the transcript is unavailable/truncated."""
    try:
        import oracle_adapter
        recs = oracle_adapter.records_for(payload)
        if not recs:
            return {"state_flags": {"deliverable_requested": False, "blast_radius_action": False},
                    "artifact_delivered": True, "adversarial_review": True}
        req, deliv = oracle_adapter.deliverable_signals(recs)
        # Second oracle rides the SAME transcript read — one parse per Stop, not two
        # (the constraint the first oracle was built under; adding a second scan would
        # double the cost of every Stop).
        blast, reviewed = oracle_adapter.review_signals(recs)
        return {"state_flags": {"deliverable_requested": req, "blast_radius_action": blast},
                "artifact_delivered": deliv, "adversarial_review": reviewed}
    except Exception:
        return {"state_flags": {"deliverable_requested": False}, "artifact_delivered": True}


def normalize_for_test(hook_input: dict, event: str) -> dict:
    """TEST-ONLY: build a DSL context that takes oracle values (state_flags/artifact_delivered) directly
    from hook_input instead of the live transcript. Used by the test-gate + WS4 tests to exercise Stop
    blocks deterministically. NOT called by run() — production always uses the live oracle."""
    # Oracle-value keys are passed through verbatim rather than through _normalize, which only knows
    # about prompt/tool fields. This list must grow with ORACLE_CATALOG: when the second oracle was
    # added, its examples carried `adversarial_review` and it was silently dropped here, so the
    # positive could never fire and the block failed its own gate for a reason that had nothing to do
    # with the block.
    _ORACLE_KEYS = ("state_flags", "artifact_delivered", "adversarial_review")
    ctx = _normalize({k: v for k, v in hook_input.items() if k not in _ORACLE_KEYS}, event)
    for k in _ORACLE_KEYS:
        if k in hook_input:
            ctx[k] = hook_input[k]
    return ctx


# Phase E2 bound. ~600 tokens of procedure is enough for an ordered sequence with tool hints; past
# that a "reminder" is an essay and starts costing more than the mistake it prevents.
MAX_INJECTED_BODY_CHARS = 2400


def _payload_body(art: dict) -> str:
    """The procedure body, for injection. CALL ONLY AFTER _payload_verified() has passed.

    Returns "" on any problem so the caller falls back to the pointer message. Truncation is marked
    rather than silent — a procedure that stops mid-step would otherwise read as a complete one, and
    an agent following a half-sequence is worse off than one told to go read the file.
    """
    try:
        pl = art.get("payload") or {}
        p = STATE / "friction-artifacts" / "procedures" / str(pl.get("path") or "")
        raw = p.read_text(errors="ignore").strip()
    except Exception:
        return ""
    if not raw:
        return ""
    if len(raw) > MAX_INJECTED_BODY_CHARS:
        raw = raw[:MAX_INJECTED_BODY_CHARS].rstrip() + (
            f"\n\n[…truncated at {MAX_INJECTED_BODY_CHARS} chars — full procedure at "
            f".claude/state/friction-artifacts/procedures/{pl.get('path')}]")
    return raw


def _payload_verified(art: dict) -> bool:
    """Re-verify a procedure payload against its pinned hash at FIRE time.

    Deliberately re-implemented here rather than imported from friction_installer: the dispatcher
    runs on every prompt and must stay dependency-light and fail-closed. Refuses symlinks, refuses a
    path that is not exactly this artifact's own file, and refuses on any read error — a procedure
    that cannot be proven intact simply does not fire.
    """
    import hashlib
    try:
        pl = art.get("payload") or {}
        aid = art.get("artifact_id") or ""
        name, want, size = pl.get("path"), pl.get("sha256"), pl.get("bytes")
        if not (isinstance(name, str) and name == f"{aid}.md"):
            return False
        if not (isinstance(want, str) and re.fullmatch(r"[0-9a-f]{64}", want)):
            return False
        p = STATE / "friction-artifacts" / "procedures" / name
        if p.is_symlink() or not p.is_file():
            return False
        raw = p.read_bytes()
        if size is not None and len(raw) != size:
            return False
        return hashlib.sha256(raw).hexdigest() == want
    except Exception:
        return False


def _load_active(org: int) -> list:
    try:
        data = json.loads(ACTIVE.read_text())
        return [a for a in data.get("artifacts", []) if a.get("org_id") == org]
    except Exception:
        return []


def _deep_redact_log(obj):
    # RECURSIVE redaction — a caller-controlled field (e.g. a list/dict session_id) must not smuggle
    # a secret past top-level string checks (Codex 7th review).
    if isinstance(obj, str):
        return _redact(obj)
    if isinstance(obj, list):
        return [_deep_redact_log(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _deep_redact_log(v) for k, v in obj.items()}
    return obj


def _log(row: dict) -> None:
    # Redact EVERY string value (recursively) before writing — the caller-controlled session_id and
    # any other payload-derived field must never persist a secret verbatim in the shared action log
    # (Codex 6th/7th review: this dispatcher writes the same file as the installer's redacting logger).
    try:
        ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        safe = _deep_redact_log(row)
        safe.setdefault("ts", int(time.time()))  # every row carries a timestamp — the proof ledger
        with ACTION_LOG.open("a") as f:            # (friction_promote.evaluate) requires it (Codex WS4)
            f.write(json.dumps(safe) + "\n")
    except Exception:
        pass


def _budget_ok(artifact_id: str, session: str, cap: int) -> bool:
    """Per-session fire budget: return True if under cap, and increment."""
    try:
        FIRE_COUNT.parent.mkdir(parents=True, exist_ok=True)
        d = json.loads(FIRE_COUNT.read_text()) if FIRE_COUNT.exists() else {}
    except Exception:
        d = {}
    if d.get("_session") != session:
        d = {"_session": session}
    key = artifact_id
    n = int(d.get(key, 0))
    if n >= cap:
        return False
    d[key] = n + 1
    # If the increment cannot be PERSISTED, the budget is not being enforced — every subsequent fire
    # this session would read the same stale count and pass. Returning True there means an unbounded
    # artifact on a read-only or full disk (Codex review, 2026-07-27). Refusing to fire is the safe
    # direction: a missed reminder costs nothing, an unbounded one floods every turn.
    #
    # Written to a unique temp then renamed so a concurrent hook cannot observe a half-written file.
    try:
        tmp = FIRE_COUNT.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, FIRE_COUNT)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False
    return True


@contextlib.contextmanager
def _queue_lock():
    """Advisory exclusive lock over the FULL read-modify-write span on RUN_QUEUE (CRITICAL, Codex,
    2026-09-01) — an INDEPENDENT copy of friction_runner.py's own _queue_lock(), not an import
    (this dispatcher must keep working even if friction_runner.py is broken — see this file's own
    "never trust the queue writer" framing and friction_runner.py's docstring on why the two stay
    separate failure domains). Both resolve the SAME lock file path because both resolve STATE via
    the same per-Core seat logic, so the two independent copies still serialize against each
    other. This is the WRITER half of the fix: friction_runner._drain_queue() used to read
    RUN_QUEUE, compute what's left, then os.replace() the file — a row appended here between that
    read and that replace was silently clobbered, because locking the runner's side alone does not
    stop THIS function from writing into that exact window. See friction_runner.py's
    _queue_lock() docstring for the fuller account; kept in sync there, not here, since this copy
    exists only to be locked against, not read for its own logic.

    Bounded non-blocking retry, not a bare `fcntl.flock(fd, LOCK_EX)` — this fires inside a
    synchronous hook and must never stall a turn waiting on contention it can instead skip and
    retry from a clean slate on the next invocation (fails CLOSED — the enqueue is dropped and
    logged, never silently retried in a loop that could itself stall the hook)."""
    RUN_QUEUE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(RUN_QUEUE_LOCK, "a+")
    got = False
    deadline = time.time() + _QUEUE_LOCK_BUDGET_SEC
    try:
        while time.time() < deadline:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                time.sleep(0.005)
        yield got
    finally:
        if got:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        f.close()


def _enqueue_run(artifact_id: str, action_id: str, session: str, org: int) -> bool:
    """Append ONE run-request row for friction_runner.py to drain. This is the entire extent of
    what this dispatcher does for a "run" effect — no process-spawn, no exec, nothing that resembles
    the generator's banned surface. Symlink-refused and size-capped (judge requirement 4): a
    hostile or corrupted queue file must not become an execution surface, and an unbounded one
    must not grow forever if the runner ever falls behind.

    Deliberately minimal columns — artifact_id, action_id, session, org, ts. No prompt text, no
    tool input, nothing artifact-controlled beyond the two ids that were ALREADY validated at
    install time (action_id against action_registry's catalog) and are re-validated again by the
    runner before it ever touches a filesystem path derived from them (never trust the queue
    writer, judge requirement 4 — the runner does not take this function's word for anything).

    Runs under _queue_lock() (CRITICAL, Codex) — the same lock friction_runner._drain_queue() take
    on its side, so an append here can never land in the middle of a drain's read-modify-write and
    get silently thrown away by the drain's replace."""
    with _queue_lock() as got:
        if not got:
            _log({"action": "run_enqueue_lock_timeout", "artifact_id": artifact_id})
            return False
        try:
            RUN_QUEUE.parent.mkdir(parents=True, exist_ok=True)
            if RUN_QUEUE.is_symlink():
                _log({"action": "run_queue_refused", "reason": "symlink", "artifact_id": artifact_id})
                return False
            if RUN_QUEUE.exists():
                # Cheap line count via a bounded read — this file is capped at MAX_RUN_QUEUE_LINES
                # short JSON rows, never large enough to justify a smarter incremental counter.
                n = sum(1 for _ in RUN_QUEUE.open("r", errors="ignore"))
                if n >= MAX_RUN_QUEUE_LINES:
                    _log({"action": "run_queue_full", "artifact_id": artifact_id,
                          "action_id": action_id, "lines": n})
                    return False
            row = {"artifact_id": artifact_id, "action_id": action_id, "session_id": session,
                   "org_id": org, "ts": int(time.time())}
            with RUN_QUEUE.open("a") as f:
                f.write(json.dumps(row) + "\n")
            return True
        except Exception as exc:
            _log({"action": "run_enqueue_error", "artifact_id": artifact_id, "error": str(exc)[:200]})
            return False


def run(event: str) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    if payload.get("stop_hook_active"):
        return 0
    org = 0
    try:
        # SAME RESOLVER THE INSTALLER USES, ANCHORED TO THE SAME SEAT INSTANCE ALREADY RESOLVED.
        # It used to read CORE_ORG_ID bare while install() went through _env.get_org_id() (identity
        # wins) — so a leaked env var made the dispatcher FILTER an org the installer never STAMPED,
        # and every installed artifact silently stopped firing with nothing logged. _env reports that
        # disagreement loudly; this path swallowed it.
        #
        # BARE get_org_id() is a SECOND, uncoordinated resolver — core-business, 2026-09-01: it walks
        # up from _env.py's own on-disk location, never consulting CORE_INSTANCE, while INSTANCE two
        # lines above (:78) is already the canonical CORE_INSTANCE-first seat via core_seat.seat_root.
        # In production the two agree (this file and _env.py live in the same tree), so the split was
        # invisible on the writer. It breaks the moment the two diverge — a sandboxed test with
        # CORE_INSTANCE pointed at a temp seat, or a peer running this file to review another Core —
        # exactly the "two resolvers, one subject" class core_seat.py's own header describes (bus
        # #914). Passing INSTANCE reuses the seat already decided instead of re-deriving a second one.
        from _env import get_org_id
        org = int(get_org_id(INSTANCE))
    except Exception as _e:
        # FAIL CLOSED, BUT NOT SILENTLY. A hook must never block, so returning 0 stays correct — but
        # an unresolvable org means EVERY artifact stops firing on this seat, and an unlogged
        # no-fire is indistinguishable from a turn with nothing to do. That is precisely how the
        # ImportError from a missing sys.path entry hid here for one test run.
        _log({"action": "dispatch_no_org", "reason": type(_e).__name__, "detail": str(_e)[:200]})
        return 0
    if org <= 0:
        return 0  # fail closed — never fire artifacts without a real org (Codex WS1 review)
    # session_id is caller-controlled JSON — coerce to a bounded string so a list/dict can't slip a
    # secret past logging or corrupt the budget-file key (Codex 7th review).
    _sid = payload.get("session_id", "")
    session = _redact(_sid)[:128] if isinstance(_sid, str) else "non_str_session"
    ctx = _normalize(payload, event)
    # RUNTIME-INJECTED TURNS ARE NOT THE USER SPEAKING (2026-08-18). The prompt stage fires for
    # task-notifications, monitor events and command stdout as well as for Nick, and every
    # `prompt_regex` leg matches whatever lands in `prompt_text`. Measured on the classifier's log:
    # ops 90% of fires on machine text (427/473), school 76%, business 68%, finance 53% — against
    # life at 7%, the seat with no over-broad induced triggers.
    #
    # THIS PATH WAS THE GAP IN THE FIRST FIX. `_prompt_source` was wired into learned-classifier and
    # six siblings, but the SAME contracts also reach the prompt through here: si_project translates
    # each snapshot contract into a `legacy_<key>` inject artifact, and this dispatcher fires it.
    # `verify-dont-claim` injected into life's own context on a `<task-notification>` turn AFTER that
    # fix shipped, which is how the gap was found — observed in the running seat, not inferred.
    #
    # Blanking `prompt_text` rather than returning early: only prompt-matching legs are disarmed, and
    # every other op (tool_name_in, event_is, the Stop-side oracles) keeps working, so nothing that
    # does not depend on the user's words changes behaviour. Applied HERE, on the live path only —
    # not in `_normalize`, which `normalize_for_test` and the specificity gate also call, where it
    # would silently rewrite locked test fixtures and the measured fire-rate.
    _user_turn = None  # None = not classified (non-prompt event, or the guard is unavailable)
    if event == "UserPromptSubmit" and ctx.get("prompt_text"):
        try:
            sys.path.insert(0, str(INSTANCE / ".claude" / "hooks"))
            from _prompt_source import is_user_text as _is_user_text
            _user_turn = bool(_is_user_text(ctx["prompt_text"]))
            if not _user_turn:
                ctx["prompt_text"] = ""
        except Exception:
            pass  # fail toward today's behaviour: guard absent, dispatcher unchanged
    injects = []  # accumulate ALL matching inject artifacts — a unified spine has many contracts, and
                  # first-match-only silently dropped every contract after the first (Codex, WS1).
    _considered = 0
    # WHICH artifacts matched, not just how many were considered. See the dispatch_nofire block at
    # the end of this function for why this exists and what it distinguishes.
    _matched: list = []
    for art in _load_active(org):
        try:
            if art.get("event") != event:
                continue
            _considered += 1
            eff = art.get("effect", {})
            mode = eff.get("mode")
            # `invoke_skill` was removed 2026-07-27: it was dead scaffolding (no installer path could
            # ever persist it) whose only behavior was prefixing a string. Procedures ride `inject`
            # and carry their payload pointer in the message, so the dispatcher stays two-mode.
            #
            # "run" added 2026-08-31 (GAP A-executable-effect). Only friction_installer.py's
            # run_action branch can ever persist mode:"run" — every other type is still refused at
            # install time (_validate_spec) — but this loop reads active.json directly and must
            # not assume the file it is reading was written by that path, so it re-checks the mode
            # string itself rather than trusting the type field.
            if mode not in ("inject", "block", "shadow", "run"):
                continue  # unknown/malformed mode NEVER fires and never consumes budget
            # trusted-regex path is PROJECTOR-CONTROLLED: si_project.project() sets trusted_regex=True
            # ONLY for rows whose DB provenance is 'legacy'. It is NOT derived from any artifact-controlled
            # field (template.id etc.), so an imported/corrupt spec can't grant itself the ReDoS bypass
            # (Codex WS1 review). Absent the projector (raw active.json), it defaults False = strict.
            trusted = art.get("trusted_regex") is True
            if not evaluate(art.get("condition", {}), ctx, trusted):
                continue
            # PAST THIS LINE THE TRIGGER MATCHED. Every `continue` below is a matched-but-suppressed
            # path (shadow, budget cap, payload mismatch), so recording the id here is what makes
            # "never matched" and "matched then dropped" separable at all.
            _matched.append(art.get("artifact_id"))
            if mode == "shadow":
                # SHADOW — an artifact that has stopped earning enforcement but must keep LOOKING.
                #
                # PLACED HERE DELIBERATELY: AFTER evaluate(). The first version of this branch sat
                # above the condition check, so it logged on every matching EVENT regardless of
                # whether the trigger fired — fabricating "detections" that measured nothing. Codex
                # caught it (Critical), and it mattered because the tuning objective at the time was
                # "minimise ENFORCED blocks subject to SHADOW detections not rising": if the shadow
                # count is noise, the safety property it underwrites is noise too. A shadow
                # observation is only meaningful if the pattern actually matched — still true, and
                # still the reason this branch sits here.
                #
                # THAT OBJECTIVE IS SUPERSEDED as of 2026-08-06 (D4). Read as an optimiser would, both
                # of its terms are minimised by doing nothing: promote nothing and enforced blocks are
                # zero, generate nothing and shadow detections cannot rise. This subsystem installed
                # 1,070 artifacts and promoted 0, which was not a failure against that objective — it
                # was a perfect score. Worse, "0 enforced blocks" is equally the signature of every
                # detector being dead, and for months that was the true reading.
                #
                # The live objective is `bin/si-objective.py`: maximise the FALL in unsourced
                # violations per 100 replies (measured on the real final text at MessageDisplay),
                # subject to a LIVENESS probe proving each detector still detects, with enforcement
                # fires and injected tokens as explicit costs. Doing nothing now scores flat, and a
                # zero has to be earned. The shadow count remains an INPUT to the promotion proof
                # window in friction_promote — it is no longer the thing being optimised.
                #
                # BEFORE the budget check, and that part IS intentional: an observation costs zero
                # tokens and consumes no injection budget, so rationing it would only blind the
                # signal that decides whether to re-arm. Bounded instead by the condition itself —
                # it logs when the pattern matches, exactly as the enforcing version would have.
                # BOUNDED per (artifact, session). Codex: "every matching event appends before any
                # budget; a match requirement is not a storage bound." Correct — a broad shadowed
                # rule in a long session would append indefinitely. Capped at
                # SHADOW_OBS_PER_SESSION, which preserves BOTH signals the proof window reads
                # (total fires and distinct sessions) while making growth linear in sessions rather
                # than in events. Uses the same fire-budget ledger, so it needs no new state file.
                if _budget_ok("shadow:" + str(art.get("artifact_id")), session,
                              SHADOW_OBS_PER_SESSION):
                    _log({"action": "shadow_observe", "artifact_id": art.get("artifact_id"),
                          "event": event, "org_id": org, "session_id": session})
                continue

            cap = int(art.get("lease", {}).get("max_fires_per_session", 1) or 1)
            if not (1 <= cap <= 5):
                cap = 1
            if not _budget_ok(art.get("artifact_id", "?"), session, cap):
                # MATCHED BUT DID NOT FIRE — the last silent skip in this loop (2026-08-12, Phase 4).
                #
                # Every other exit from this loop records itself: payload_mismatch, shadow_block,
                # fire_block, dispatch_error. This one did not, and it is the one that distorts the
                # measurement rather than merely hiding an error.
                #
                # The artifact MATCHED. It was suppressed only because it had already fired its
                # per-session cap. So fire_count reads 2 whether the rule matched twice or matched
                # fifty times and was capped forty-eight — and every downstream verdict
                # (GRADUATED / NOT-BINDING / DECAYING) reads that number as evidence about the RULE
                # when it is partly evidence about the CAP.
                #
                # The master plan names this exactly: "Make 'matched but did not fire' a permanently
                # logged first-class state. Rete/OPS5 conflict-set logging exists precisely because a
                # zero fire count is otherwise indistinguishable from no match."
                #
                # `cap` and `event` ride along because the suppression rate is only interpretable
                # against the budget that caused it — a rule capped at 1 and a rule capped at 5 with
                # the same suppressed count are not the same finding.
                #
                # HOW TO READ A RAW COUNT OF THIS ACTION, because it misled a reader on 2026-08-26
                # badly enough to be reported to Nick as a fleet-wide problem:
                #
                # This is a PER-ARTIFACT, PER-SESSION fire budget (see the `cap` check above, keyed
                # on artifact_id alone). It is NOT "how many artifacts may inject per turn" — every
                # matching artifact injects in the same turn and there is no slot contention. So a
                # large raw total does NOT mean the loop is being throttled.
                #
                # It is dominated by SESSION LENGTH and by event type. Measured over 2026-08-20..26
                # on core-life: 1,227 rows, 100% of them PreToolUse, and 1,106 of those from ONE
                # long agentic session — a work-shape artifact matches every Edit/Write, fires its
                # 1-2, then logs this on every subsequent tool call. Across the same six days the
                # 23 conversational artifacts hit their cap exactly zero times.
                #
                # So: never compare this total against install counts, and never read it without
                # splitting by `event` and normalising per 100 events of that type. Un-normalised,
                # one long session reads as strangulation. Nothing currently consumes this as a
                # metric — it is log-only — which is precisely why a human reading the raw log is
                # the failure mode to guard against.
                _log({"action": "budget_capped", "artifact_id": art.get("artifact_id"),
                      "event": event, "org_id": org, "session_id": session, "cap": cap})
                continue
            if mode == "run":
                # GAP A-executable-effect (2026-08-31). This dispatcher does NOT execute anything —
                # see the module docstring's safety story and _enqueue_run's own comment. It only
                # writes a request row for friction_runner.py, a SEPARATE out-of-process hook, to
                # drain. event is already forced to UserPromptSubmit-only for run_action specs at
                # install time (friction_installer._validate_spec) — re-asserted here too, because
                # this loop reads active.json directly and must not assume every row it iterates was
                # written by that path (same reasoning as the mode allowlist above).
                if event != "UserPromptSubmit":
                    _log({"action": "run_wrong_event", "artifact_id": art.get("artifact_id"),
                          "event": event, "org_id": org, "session_id": session})
                    continue
                if not RUN_ENABLED:
                    _log({"action": "run_disabled", "artifact_id": art.get("artifact_id"),
                          "org_id": org, "session_id": session})
                    continue
                action_id = eff.get("action_id")
                if not isinstance(action_id, str) or not re.match(r"^[a-z][a-z0-9_]{1,63}$", action_id):
                    _log({"action": "run_bad_action_id", "artifact_id": art.get("artifact_id"),
                          "org_id": org, "session_id": session})
                    continue
                enq = _enqueue_run(art.get("artifact_id", "?"), action_id, session, org)
                _log({"action": "run_enqueued" if enq else "run_enqueue_failed",
                      "artifact_id": art.get("artifact_id"), "action_id": action_id,
                      "event": event, "org_id": org, "session_id": session})
                continue  # never injects text — a run_action has no message to surface
            if mode == "block":
                # DOUBLE gate: global BLOCKS_ENABLED (kill-switch) AND per-artifact `enforced` (set only
                # after a shadow-proof window — WS4). Until BOTH are true a block runs in SHADOW: it logs
                # a would-block event for the proof ledger and falls through to an advisory inject. This
                # is how enforcement is generated autonomously yet never enforces unproven (Nick's plan).
                # PreToolUse IS INJECT-ONLY, STRUCTURALLY (Phase E1, 2026-08-05).
                #
                # Phase E1 registered this dispatcher on PreToolUse so work-shape artifacts could
                # fire at all. I claimed to Codex that the dispatcher "cannot block" — grepping for
                # permissionDecision/deny and missing THIS line, which prints a real
                # {"decision":"block"} for any artifact that has reached `enforced`. Codex broke the
                # claim on review.
                #
                # PreToolUse is the event the trust root (pretooluse-guard.sh) lives on. Quietly
                # gaining the ability to refuse a Write there — as a side effect of wiring up
                # workflow procedures — is exactly the posture change the plan reserved for Nick
                # (§6: "another thing runs on the event where the security gate lives" is a posture
                # question, not a test question). So the capability is refused for this event by
                # construction rather than by the accident of no artifact being enforced yet: a
                # block on PreToolUse degrades to the same shadow path an unproven block takes.
                # UserPromptSubmit and Stop are unchanged.
                if event == "PreToolUse":
                    _log({"action": "shadow_block_pretooluse_inject_only",
                          "artifact_id": art.get("artifact_id"), "event": event,
                          "org_id": org, "session_id": session})
                elif BLOCKS_ENABLED and art.get("enforced") is True:
                    _log({"action": "fire_block", "artifact_id": art.get("artifact_id"), "event": event,
                          "org_id": org, "session_id": session})
                    print(json.dumps({"decision": "block", "reason": (eff.get("message") or "friction gate")[:2000]}))
                    return 0  # a block is terminal — short-circuit
                _log({"action": "shadow_block", "artifact_id": art.get("artifact_id"), "event": event,
                      "org_id": org, "session_id": session})  # would-block; proof-ledger signal
            # A `procedure` injects a POINTER to a payload file the model will then read and follow.
            # The payload hash is pinned at install, but install-time verification alone is not
            # enough: anything that edits the file afterwards would be followed as durable
            # instruction with no check (Codex HIGH #2). So re-verify against the pinned hash HERE,
            # at fire time, and stay silent on any mismatch rather than pointing at unverified
            # content. Fail-closed: unreadable or changed payload means no injection at all.
            body_override = None
            if art.get("type") == "hooked_skill":
                if not _payload_verified(art):
                    _log({"action": "payload_mismatch", "artifact_id": art.get("artifact_id"),
                          "event": event, "org_id": org, "session_id": session})
                    continue
                # PHASE E2 — INJECT THE BODY, NOT A PATH.
                #
                # The message was "...Follow the procedure at .claude/state/.../<id>.md". A workflow
                # the agent has to REMEMBER TO GO READ is not a learned workflow; it is a footnote,
                # and it is followed only when the model happens to spend a tool call on it. The
                # original rationale — keep the body out of prompts until the trigger fires — is
                # already satisfied by the trigger itself: nothing is injected unless the artifact
                # matched.
                #
                # Read AFTER _payload_verified, so the pinned-hash check still gates it and a
                # tampered payload still refuses. The file stays the single copy rollback retires;
                # this only changes what reaches the model when the gate has already opened.
                # Bounded, because an unbounded body on every matching prompt is how a helpful
                # injection becomes a context tax.
                body_override = _payload_body(art)
            # inject — and block DOWNGRADED to inject while BLOCKS_ENABLED is False — surfaces as
            # additionalContext (never blocks, never executes).
            # E2: prefer the verified body; fall back to the pointer message if it could not be read.
            # Never the other way round — a silent fallback to "go read this file" is better than no
            # injection, but a body that failed verification must never reach here (it `continue`d).
            msg = _redact(body_override or eff.get("message") or "")
            # `user_turn` — ONE BOOLEAN, AND THE REASON IT IS A BOOLEAN (2026-08-18).
            #
            # finance's aim axis — "what did the fire LAND ON" — separates a useful contract from a
            # useless one where no rate can: `plan-not-execute` fired once in that seat's lifetime, on
            # a notification. Computable from `learned-fires.log`, which stores the triggering text.
            # NOT computable here: this row carried artifact_id, event, org_id, session_id, ts and
            # nothing about the turn. business measured the blind spot — four seats are effectively
            # fully covered by the classifier's ledger, **life is 72%**, and life's number is the one
            # the breadth hypothesis rests on.
            #
            # The prompt itself is deliberately NOT logged. It would put arbitrary user text through
            # this writer on every fire, and `_log`'s redaction exists precisely because a
            # caller-supplied string can carry a secret verbatim. A boolean answers the question with
            # no such surface.
            #
            # AND IT DOUBLE-ENTRIES THE GUARD — with one qualification the first version of this
            # comment got wrong, caught by its own test. After the prompt-source fix, a fire on
            # machine text should be impossible FOR AN ARTIFACT WHOSE CONDITION INCLUDES A
            # `prompt_regex` LEG, because that leg is what the guard disarms. An artifact matching
            # only on `event_is` / `tool_name_in` fires on every turn by design and will legitimately
            # read `user_turn: false`. So the alarm is "false on a prompt-matching artifact", not
            # "false" — a distinction that would have produced a false alarm on the first artifact to
            # trip it. `null` means unclassified (non-prompt event, or the guard failed to import),
            # which is its own signal and must not be read as `true`.
            _log({"action": "fire_inject", "artifact_id": art.get("artifact_id"), "event": event,
                  "org_id": org, "session_id": session, "user_turn": _user_turn})
            if msg:
                injects.append(msg)
        except Exception:
            _log({"action": "dispatch_error", "artifact_id": art.get("artifact_id"), "event": event, "org_id": org})
            continue  # fail open past a broken artifact
    # THE DISPATCHER MUST RECORD THAT IT RAN, not only that it fired.
    #
    # `fire_inject` had NEVER been written in this system's history — every artifact reads
    # fire_count: 0, so promotion (needs 5 fires), skill graduation (needs 5) and trigger-narrowing
    # (needs a ratio) have all been starved by one absent number. The obvious reading is "the
    # triggers never match", and Fable falsified it: 14 of 19 triggers DO match real prompts, 44
    # matches. The alternative is that this path never runs at all.
    #
    # NOTHING COULD TELL THOSE APART, because a dispatcher that fires logs and a dispatcher that
    # runs-and-matches-nothing was silent. Absence of evidence was being read as evidence — I nearly
    # asserted "the hook never runs" from an empty log, when this file simply never wrote to it.
    #
    # ONCE PER SESSION PER EVENT, not once per invocation. PreToolUse runs hundreds of times a day;
    # a row each would bury the ledger it is meant to clarify. One row answers the only question
    # being asked — does this path execute — and the fire rows carry the rest.
    if _considered and not injects:
        try:
            _seen = FIRE_COUNT.parent / ".dispatch-seen.json"
            _d = json.loads(_seen.read_text()) if _seen.exists() else {}
            if _d.get("_session") != session:
                _d = {"_session": session}
            if not _d.get(event):
                # `matched` IS THE PHASE-4 FIELD: "matched but did not fire" as a first-class,
                # readable state (2026-08-12).
                #
                # `considered` alone cannot separate the two reasons nothing fired, and they call
                # for opposite responses:
                #
                #   considered=19, matched=[]         no trigger fits the traffic  -> re-derive
                #   considered=19, matched=[art_x]    art_x matched and was DROPPED -> find out why
                #
                # MEASURED BEFORE BUILDING IT: 6 of 20 active artifacts have never fired, and ZERO
                # of the six carry a named suppression reason (budget_capped / payloadless_artifact
                # / orphan_payload). For every one of them the two rows above were indistinguishable,
                # so "the trigger is fossil" and "the trigger works and something ate the result"
                # looked identical to every consumer — and re-deriving a rule whose trigger is fine
                # is how a working artifact gets replaced by a worse one.
                #
                # Costs NO new rows: this row already existed, once per session per event, and this
                # only gives it the field that makes it answerable. That also retires its own
                # standing complaint — dispatch_nofire was written and read by nothing, which the
                # void audit that prompted this change flagged.
                _log({"action": "dispatch_nofire", "event": event, "org_id": org,
                      "considered": _considered, "matched": _matched, "session_id": session})
                _d[event] = 1
                _seen.parent.mkdir(parents=True, exist_ok=True)
                _seen.write_text(json.dumps(_d))
        except Exception:
            pass  # never let telemetry break a fail-open dispatcher

    if injects:
        combined = "\n\n".join(injects)[:4000]  # emit ALL matched reminders together, bounded
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": combined}}))
    return 0


if __name__ == "__main__":
    ev = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FRICTION_EVENT", "UserPromptSubmit")
    sys.exit(run(ev))
