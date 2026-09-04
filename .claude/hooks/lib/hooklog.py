#!/usr/bin/env python3
"""hooklog — durable hook-event telemetry (L0).

Every hook calls log() once per fire (pass | block | inject) so System Health
can answer "is this hook firing, and how?" without parsing JSONL. The line schema
is IDENTICAL to the JSONL backfill (scheduling/system-health/backfill-hook-events.py)
so historical + live events form one continuous dataset read by core-ux health.ts.

Durable location: $CORE_INSTANCE/.claude/state/hook-events.log (version-controlled,
survives reboots — unlike the old /tmp/core-hook-events.log).

Fail-open by contract: log() NEVER raises into a hook. A telemetry failure must
never break a turn.

Usage from any hook:
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
    import hooklog
    hooklog.log("my-hook", "PreToolUse", verdict="block", trigger="osascript", session=sid)
"""
import datetime
import json
import os
from pathlib import Path


def _instance() -> Path:
    # parents[3] of .claude/hooks/lib/hooklog.py is the instance root
    # (parents: [0]=lib, [1]=hooks, [2]=.claude, [3]=root). Env wins when set
    # (always set in the live hook path); the path fallback covers headless runs.
    return Path(
        os.environ.get("CORE_INSTANCE")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or Path(__file__).resolve().parents[3]
    )


def invoked(hook: str, event: str, session: str = "") -> None:
    """Record that a hook RAN, regardless of whether it matched anything.

    log() is called once per FIRE. That makes two very different hooks look identical: one that
    runs on every turn and never matches, and one that is not registered at all — both emit zero
    lines. Those need opposite treatment (retune the trigger vs. retire the hook), so autonomous
    maintenance cannot tell them apart without this.

    Emitted as verdict=invoke so it never pollutes fire counts; readers filter on verdict.
    Same fail-open contract as log() — telemetry must never break a turn.
    """
    log(hook, event, verdict="invoke", session=session)


MAX_LOG_BYTES = 1_000_000   # rotate at ~1MB; one .1 generation kept


def _rotate_if_needed(p: Path) -> None:
    """Keep hook-events.log bounded. Phase 0 of the steering-surface plan folds rotation into
    this lib so the per-hook copies (gate-fires.log and friends) can be retired — one stream,
    not five. Best-effort and silent: a rotation failure must never break a turn, and losing a
    rotation is strictly less bad than losing the write.
    """
    try:
        if p.exists() and p.stat().st_size > MAX_LOG_BYTES:
            p.replace(p.with_suffix(p.suffix + ".1"))
    except Exception:
        pass


def log(hook: str, event: str, verdict: str = "pass", trigger: str = "", session: str = "",
        detail: str = "", tokens_injected: int = 0) -> None:
    """Append one telemetry line. verdict ∈ {pass, block, inject, invoke}. Fail-open.

    detail / tokens_injected added 2026-07-30 (steering-surface governance, Phase 0 — the sensor).

      detail          free slug for WHY this verdict happened, e.g. "suppress:current-session".
                      Lets recall-gate and brain-recall-trigger retire their private
                      gate-fires.log and ride this stream instead: their suppress-reasons are
                      exactly this field. One substrate, which is the whole point of Phase 0.
      tokens_injected int, chars/4 of any emitted additionalContext/reason. This is the COST
                      side of a steering surface. Without it there is no way to answer "what is
                      this gate costing per session", and cost is half of the fitness question —
                      a gate that helps occasionally and injects 6k tokens every turn is not
                      obviously worth keeping. Published practice treats bloat as a cost signal;
                      the plan's Phase 3 flags any hook whose tokens/session doubles vs its
                      14-day baseline, and it can only do that if this is recorded now.

    FIELD ORDER IS DELIBERATE: the two new fields go BEFORE excerpt so excerpt stays terminal.
    excerpt is free text and may contain anything; anything appended after it would be swallowed
    by a `.*$` capture. Only one strict parser exists (backfill-hook-events.py's LINE_RX) and it
    is updated in the same commit to treat both fields as optional, so historical lines without
    them still parse.

    No-op when CORE_HOOKLOG_OFF is set — test harnesses and the benchmark drive the LIVE hooks to
    exercise detection, and must not pollute the durable, version-controlled production log with
    synthetic events.
    """
    if os.environ.get("CORE_HOOKLOG_OFF"):
        return
    try:
        ts = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        # Strip the field separator out of free-text fields — a stray '|' in a trigger excerpt
        # would otherwise forge an extra column and corrupt every downstream parse.
        ex = " ".join((trigger or "").replace("|", "/").split())[:80]
        det = " ".join((detail or "").replace("|", "/").split())[:40]
        try:
            tok = max(0, int(tokens_injected or 0))
        except Exception:
            tok = 0
        line = (f"{ts} | hook={hook} | event={event} | verdict={verdict} | session={session} "
                f"| detail={det} | tokens_injected={tok} | excerpt={ex}\n")
        p = _instance() / ".claude" / "state" / "hook-events.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(p)
        with open(p, "a") as f:
            f.write(line)
    except Exception:
        pass  # telemetry must never break a turn


def emit(hook: str, event: str, context: str, session: str = "",
         verdict: str = "inject", detail: str = "", trigger: str = "") -> None:
    """Print a UserPromptSubmit-style additionalContext payload AND record what it cost.

    ADDED 2026-07-30, master plan Phase 2.3. `tokens_injected` had existed since the Phase 0
    sensor and every row in the log carried 0, because recording the cost was a SEPARATE call
    that each hook had to remember to make after building its payload. None of them did. The
    cost half of the fitness function — value = (re-explaining avoided) − (tokens injected) —
    was therefore permanently unmeasurable, while looking instrumented.

    The fix is to make the two inseparable. A hook that emits through this cannot fail to
    account for the emission, because the accounting IS the emission. That is the same reasoning
    that put the anti-pattern rules into hooks instead of prose: a discipline that can be
    forgotten will be.

    Fail-open in the direction that matters: if telemetry raises, the payload is still printed.
    Steering the turn is the job; measuring it is the improvement.
    """
    try:
        payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
        print(json.dumps(payload))
    except Exception:
        return
    try:
        log(hook, event, verdict=verdict, session=session, detail=detail,
            trigger=trigger or (context or "")[:80],
            tokens_injected=max(0, len(context or "") // 4))
    except Exception:
        pass


def emit_block(hook: str, event: str, reason: str, session: str = "", detail: str = "") -> None:
    """Same contract for a Stop-hook block decision: print it, then record its cost.

    A block's `reason` is injected text exactly like an advisory is — the model reads it and
    continues. Measured on 2026-07-30, a Stop block is a CONTINUATION rather than a
    regeneration, so the reason's tokens land in the transcript ON TOP of the response that
    triggered it. If anything a block costs more than an advisory of the same length, and it
    has to be on the same ledger.
    """
    try:
        print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:
        return
    try:
        log(hook, event, verdict="block", session=session, detail=detail,
            trigger=(reason or "")[:80], tokens_injected=max(0, len(reason or "") // 4))
    except Exception:
        pass
