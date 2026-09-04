#!/usr/bin/env python3
"""preempt-gate — PostToolBatch. Supplies the missing read BEFORE the reply is composed.

WHY THIS EXISTS
---------------
`reply-observer.py` measures seven claim classes and decides, per class, what would have SOURCED
the claim. For five of them it records — in its own comments — that no cheap per-turn supply is
possible:

    "state_claim": None,
    "decision_attribution": None,
    ...
    # These three are PRE-EMPT territory, not supply territory: each needs a specific action in
    # the turn (a write, an account pull, a file produced) ... These are the classes the PRE-EMPT
    # layer (PostToolBatch) has to cover.

That layer did not exist. The observer has been counting violations in classes it had already
documented as un-preventable-by-supply, and the counting is all that happened. On core-life the
`financial_figure` class reached the work-order threshold: **10 unsourced claims across 826
observed replies (1.21/100) over a fully-covered window** — `.claude/state/oracle-request-queue.json`,
verdict OBSERVED-VIOLATION-RATE, `recommended_action: write_preempt_hook`, `must_be_handwritten: true`.

This is that layer.

WHY PostToolBatch AND NOT Stop
------------------------------
The queue file rules out the alternatives itself, and they are worth keeping here because the
reasoning is Nick's:

  · Stop — "post-reply. Nick 2026-08-04: a gate that fires after the reply is sent cannot prevent
    anything, only fail the turn afterwards." Nine Stop gates were retired 2026-08-06 for exactly
    this. Do not put this back on Stop.
  · MessageDisplay — "sees the reply but provably cannot block it — that is what makes it safe to
    observe from, and useless to enforce from." That is where the observer lives, correctly.

PostToolBatch fires after the tool batch and BEFORE the model composes its reply, so a missing read
can still be **supplied rather than punished**. That is the whole design: this hook never blocks and
never fails a turn. It says "you don't have the evidence yet, and you still have time to go get it."

VERIFIED, NOT ASSUMED
---------------------
`event-probe.py` established that PostToolBatch *fires* on this build. It could not establish that
the event can *inject*, because it is deliberately non-injecting, and its own header warns: "Before
porting any gate to a new event, the question is whether that event fires here at all — and the
answer cannot come from the docs." So injection was tested directly on 2026-08-25 with a throwaway
probe emitting a sentinel string; the sentinel arrived in-session, without a restart. If a future
build stops honouring it, this hook goes quiet rather than wrong — but it will be quiet, so re-run
that probe before trusting silence.

ONE MECHANISM, NOT ONE HOOK PER CLASS
-------------------------------------
Built as a class TABLE with one class switched on, not as a financial-claim hook. Nick's standing
directive (recurring 9x): "consolidate patched/redundant subsystems into one clean, efficient
design, not more patches." Four more classes are already named as pre-empt territory by the
observer; when one of them clears the same threshold it becomes a row here, not a sibling file.
Rows that have NOT cleared the threshold ship inert — declared so the shape is visible, `active`
False so they cost nothing and cannot generate noise nobody asked for.

SOURCING IS NOT RE-IMPLEMENTED HERE
-----------------------------------
Whether a class is already sourced is decided by importing `reply-observer.py` and using ITS
`_SOURCE_TOOLS` and `_turn_tool_blob`. Re-declaring those regexes would create two implementations
of one rule, which is the precise defect the observer's own comments describe surviving in its
casebook probes: "A probe that can disagree with the thing it probes is not a probe." If the
observer's definition of sourced drifts, this gate drifts with it, and the gate's prevention stays
commensurable with the observer's measurement. If the import fails, the hook exits silently —
degraded to nothing, never to a second opinion.

CONTRACT
--------
  · never blocks, never non-zero, never mutates anything but its own log and marker
  · at most ONE fire per turn, per class
  · every failure path is a silent exit 0
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # operator name from identity.json, never hardcoded
import json
import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
MARKER_TTL = 6 * 3600
LOG_MAX = 128 * 1024


# ── The class table. `active` gates whether a row can fire at all. ──────────────────────────────
#
# ask     — does the USER's prompt this turn indicate a claim of this class is being asked for?
#           Deliberately narrow. The measured base rate is 1.21 violations per 100 replies, so a
#           gate that fires on 20% of turns is noise wearing a gate's name. Prefer a miss.
# missing — what to tell the model it does not yet have.
#
# A row is NOT a rule about how to behave; it is a statement that the evidence for a specific claim
# class is absent from this turn while the turn appears to be heading toward making one.
CLASSES = {
    "financial_figure": {
        "active": True,
        # Cleared the work-order threshold on core-life: 10 absolute, 1.21/100, covered window.
        # Threshold is ">=3 absolute and >=1.0/100 sustained" per the oracle queue's own rationale.
        "ask": re.compile(
            r"\b(?:"
            r"balance|brokerage|portfolio|position(?:s)?|holding(?:s)?|buying\s+power"
            r"|account\s+(?:value|balance|total)|net\s+worth|p\s*&\s*l|pnl"
            r"|how\s+much\s+(?:money|cash|do\s+i\s+have|is\s+in)"
            r"|what(?:'|’)?s\s+in\s+my\s+(?:account|portfolio)"
            r"|robinhood|snaptrade|alpaca"
            r")\b", re.I),
        "missing": (
            "a LIVE account read. This rule is hard and has no exceptions: an account figure is "
            "pulled live, never recalled. Nothing in this turn has read a brokerage or account "
            "source yet."),
        "do": (
            "Pull the live source before you write any number. If you cannot pull it, say plainly "
            "that you have not pulled it — do not quote a remembered figure, and do not round one "
            "from context."),
    },

    # ── INERT ROWS. Declared, not enabled. ──────────────────────────────────────────────────────
    # Each is named by reply-observer.py as pre-empt territory ("no supply is possible"), and each
    # is genuinely un-preventable by supply. None has cleared the >=3-absolute-and->=1.0/100 bar on
    # this seat, and enabling a row that has not cleared it means firing at Nick on a hunch. The
    # threshold is the whole discipline: it is what made the financial row a work order rather than
    # an opinion. Flip `active` only when the oracle queue says so.
    "decision_attribution": {
        "active": False,
        "ask": re.compile(r"\b(?:did\s+(?:i|we|nick)\s+(?:decide|say|approve|decline)"
                          r"|what\s+did\s+(?:i|we)\s+decide|was\s+it\s+decided)\b", re.I),
        "missing": ("a read of the record being attributed — `memory/decisions-log.md`, a session "
                    "log, or a brain recall."),
        "do": f"Grep the record before attributing a decision to {_U.name()}.",
    },
    "say_do_gap": {
        "active": False,
        "ask": None,   # not askable from the prompt; needs the composed reply. Shape unresolved.
        "missing": "an actual mutating tool call for the write you are about to promise.",
        "do": "Write it now or do not say it.",
    },
}


def _state_dir() -> Path:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CORE_INSTANCE")
                or _HERE.parents[1])
    d = root / ".claude" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cheap_prefilter(payload: dict) -> bool:
    """True if it is even POSSIBLE for an active class to fire. Runs before the observer import.

    PostToolBatch fires once per tool batch — the hottest event this Core registers — and importing
    reply-observer.py costs ~22ms of the hook's ~55ms (measured 2026-08-25), mostly regex compilation
    and the peer-alternation filesystem walk. Paying that on every batch to answer "no" is the shape
    of diagnostic that becomes a problem, which is the warning event-probe.py wrote into its own
    rotation logic.

    This is a TRIGGER prefilter, not a second implementation of `sourced`. It is deliberately
    STRICTLY BROADER than the real `ask` patterns — a bare keyword scan over the raw tail, no word
    boundaries, no structure. Anything the real check would match, this matches first; the only
    thing it can do is skip work that was going to return nothing. If that stops being true, the
    gate under-fires silently, so keep it dumb: keywords only, never logic.
    """
    try:
        tp = payload.get("transcript_path") or payload.get("transcriptPath")
        if not isinstance(tp, str) or not tp:
            return False
        p = Path(tp)
        if not p.is_file():
            return False
        size = p.stat().st_size
        window = 256 * 1024
        with p.open("rb") as fh:
            if size > window:
                fh.seek(-window, os.SEEK_END)
            raw = fh.read().decode("utf-8", errors="replace").lower()
    except Exception:
        return False
    return any(k in raw for k in _PREFILTER_KEYS)


# Flat keyword set drawn from the active rows' `ask` patterns. Broader on purpose — see above.
_PREFILTER_KEYS = (
    "balance", "brokerage", "portfolio", "position", "holding", "buying power",
    "account", "net worth", "p&l", "pnl", "how much", "robinhood", "snaptrade", "alpaca",
)


def _observer():
    """Import reply-observer.py by path (hyphenated name is not a legal module identifier).

    Returns None on ANY failure. A gate that cannot reach the observer's definition of `sourced`
    must go silent, not invent its own — see the module docstring.
    """
    try:
        import importlib.util
        src = _HERE / "reply-observer.py"
        if not src.is_file():
            return None
        spec = importlib.util.spec_from_file_location("_reply_observer_for_preempt", src)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _user_text(injected: str) -> str:
    """The user's own words this turn, with hook-injected supply stripped.

    `_turn_tool_blob` returns the whole user record, and on this Core that record carries the
    injected clock, PEERS/baseline SHAs, the rot signal and any bus digest. Matching an `ask`
    pattern against that would let Core's own supply trigger Core's own gate — a hook firing at
    itself. Strip the known injected blocks and the system-reminder envelopes first.
    """
    t = injected or ""
    t = re.sub(r"<system-reminder>.*?</system-reminder>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<task-notification>.*?</task-notification>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<command-(?:name|message|args)>.*?</command-[^>]*>", " ", t, flags=re.S | re.I)
    # Core's own SessionStart / UserPromptSubmit supply lines.
    for pat in (r"⏰[^\n]*", r"\bPEERS\b[^\n]*", r"\[ROT signal:[^\]]*\]",
                r"CORE-BUS[^\n]*", r"SESSION-START[^\n]*"):
        t = re.sub(pat, " ", t)
    return t


def _fire_marker(state: Path, sid: str, pid: str, cls: str) -> Path:
    key = re.sub(r"[^A-Za-z0-9]", "", f"{sid}{pid}{cls}")[:48]
    return state / f".preempt-{key}"


def _prune(state: Path) -> None:
    now = time.time()
    try:
        for f in state.glob(".preempt-*"):
            try:
                if now - f.stat().st_mtime > MARKER_TTL:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _log(state: Path, row: dict) -> None:
    """Instrument the gate the same way everything else here is instrumented.

    A prevention layer that cannot be measured is indistinguishable from one that does nothing —
    which is the state the observer's five pre-empt classes were already in. This log is what lets
    a later pass ask whether the 1.21/100 actually fell.
    """
    try:
        p = state / "preempt-gate.log"
        if p.exists() and p.stat().st_size > LOG_MAX:
            keep = p.read_text(errors="ignore").splitlines(keepends=True)[-800:]
            p.write_text("".join(keep))
        with p.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        if not any(c.get("active") for c in CLASSES.values()):
            return 0
        if not _cheap_prefilter(payload):
            return 0

        state = _state_dir()
        _prune(state)

        obs = _observer()
        if obs is None:
            return 0

        blob, injected = obs._turn_tool_blob(payload)
        prompt = _user_text(injected)
        if not prompt.strip():
            return 0

        sid = str(payload.get("session_id") or "")
        pid = str(payload.get("prompt_id") or payload.get("turn_id") or "")

        notes = []
        for name, spec in CLASSES.items():
            if not spec.get("active"):
                continue
            ask = spec.get("ask")
            if ask is None or not ask.search(prompt):
                continue

            # Already sourced? Use the OBSERVER's rule, never a local copy.
            srx = obs._SOURCE_TOOLS.get(name)
            if srx is not None and srx.search(blob or ""):
                continue

            marker = _fire_marker(state, sid, pid, name)
            if marker.exists():
                continue
            try:
                marker.write_text(str(int(time.time())))
            except Exception:
                pass

            notes.append(f"**{name}** — this turn does not yet have {spec['missing']} {spec['do']}")
            _log(state, {"ts": int(time.time()), "cls": name, "sid": sid[:12], "pid": pid[:12],
                         "fired": True})

        if not notes:
            return 0

        body = ("[preempt-gate] Before you compose this reply:\n\n- "
                + "\n- ".join(notes)
                + "\n\nThis is not a block — you still have the turn. It fires only when the "
                  "evidence for a claim you appear about to make is absent from this turn's tool "
                  "calls. If you are not about to make that claim, ignore it.")

        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": payload.get("hook_event_name") or "PostToolBatch",
                "additionalContext": body,
            }
        }))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
