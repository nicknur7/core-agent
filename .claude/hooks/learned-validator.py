#!/usr/bin/env python3
"""learned-validator.py — Stop hook. The BLOCKING half of the learned-workflow layer.

Enforces the single deterministic ('checkable') clause from learned_contracts —
BY PORTING IT, NOT BY READING IT (clarified 2026-08-31): the predicate below is
hardcoded; this hook never queries the DB, and nothing else reads the `checkable`
column either. The column documents which rule this hook implements. Consequence:
adding a clause to `checkable` enforces nothing until someone writes the code here,
and the seeded peer-org rows with checkable='[]' are enforced by this same hook.
The derived per-contract enforcement tier lives in measure-contract-fitness.py.

The clause:

  stop-and-plan / no-exec-after-stop — if the user prompt that triggered THIS turn
  was an unambiguous HALT signal, and Core's response ran a mutating tool
  (Write/Edit/MultiEdit/NotebookEdit) WITHOUT first acknowledging the redirect → block.

Hybrid model: ONLY this provable case blocks. Judgment-shaped contract clauses
(not-what-i-want, flip-flop, frustration, …) are injected as guidance by
learned-classifier.py and never block.

Deliberately conservative to protect the daily driver:
  - Trigger is HALT_RX only (NOT 'not what I want' — that often means 'edit it to X').
  - An acknowledgement anywhere in the response is an escape hatch (no block).
  - A false block is low-harm: Core just re-responds with an ack, then proceeds.

SAFETY — fails OPEN. Any error / missing data / kill-switch / stop_hook_active →
return 0 (allow). A bug here can NEVER brick Core; worst case it stops blocking.
Kill-switch: LEARNED_LAYER=0. Test override: LEARNED_JSONL_DIR=<fixture dir>.

Reuses the turn-accumulation pattern from say-do-gap.py (Opus-4.8 turn split) and
the HALT regex from stop-signal-gate.py.
"""
import json
import os
import re
import sys
from pathlib import Path


def _fire_ts():
    """Epoch seconds, first field of every fire row.

    Added 2026-08-20 (core-ops). This writer omitted it while learned-classifier and
    validator-block wrote one, so learned-fires.log carried four row shapes from five
    writers and every reader guessed at a column. On ops the same predicate returned
    1% via split[2] and 67% via split[-1] — a 66-point swing from the index alone.
    Timestamp first, prompt last, on every row: then [0] and [-1] are always right.
    """
    # MATCHES learned-classifier.py:128 EXACTLY — ISO 8601, UTC, seconds, WITH the +00:00 offset.
    #
    # The first version of this returned `str(int(time.time()))`, an epoch int, while the classifier
    # wrote ISO. So the fix for the ARITY problem introduced a TIMESTAMP-FORMAT problem in the same
    # file, and shipped it to the baseline. core-business found the class of defect the same hour on
    # its own disk and warned the fleet:
    #
    #     learned-fires.log         ISO + explicit +00:00  -> a lexical compare fabricates a LEAK
    #     reply-observations.jsonl  Unix epoch int         -> an ISO parse fabricates an EMPTY WINDOW
    #
    # Both fail silently and in opposite directions, which is why one format per file is the only
    # safe answer. A reader that must sniff the format of field 0 is a reader that will eventually
    # sniff wrong, and the wrong answer looks exactly like a real result.
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


SLUG = str(Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2]).resolve()).replace("/", "-").replace(" ", "-")
JSONL_DIR = Path(os.environ.get("LEARNED_JSONL_DIR")
                 or (Path.home() / ".claude" / "projects" / SLUG))
FIRE_LOG = (Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
            / ".claude" / "state" / "learned-fires.log")

# Unambiguous halt signals only (calibrated in stop-signal-gate.py). Intentionally
# excludes EXPLICIT_NO / frustration — those go to the inject classifier, not here.
HALT_RX = re.compile(
    r"\b(hol+d+\s*up|hol+dup|wait\s+wait|stop\s+(with|doing|wasting|right now)|"
    r"cut\s+it\s+out|quit\s+it|that'?s\s+enough|knock\s+it\s+off|pump\s+the\s+brakes)\b",
    re.I,
)

MUTATING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Any genuine course-change acknowledgement disarms the block.
ACK_RX = re.compile(
    r"(you'?re right|i strayed|i overran|stopping|halt(ed|ing)?\b|i'?ll stop|"
    r"pausing|paus(e|ed)\b|stepping back|step back|my mistake|let me re-?read|"
    r"re-?read what|acknowledg|you'?re correct|good (call|catch)|i was about to|"
    r"not going to (continue|barrel|push)|hold(ing)? up)",
    re.I,
)

# HOOK_PREFIXES lived here in FOUR byte-identical copies (sha a69e7ba31ca1) and none
# of them listed `<task-notification>` — 72% of this seat's prompt-stage traffic. One
# definition now: .claude/hooks/_prompt_source.py. Fails toward firing on import error.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _prompt_source import is_user_text as _is_user_text
except Exception:  # pragma: no cover
    def _is_user_text(_p):
        return True


def detect(text):
    """Halt/stop-signal phrases in `text` — the condition this gate arms on.

    Extracted so bin/grade-gate.py can measure this gate against the real corpus and
    bin/grade-intent.py can compare what it CATCHES against what it was BUILT to catch.
    A gate with no detect() is outside both instruments — it fires in production and nothing
    can say whether it is firing on the right things.

    Deliberately reports the ARMING condition (HALT_RX) only, not ACK_RX. ACK_RX is the
    disarm — a separate question asked of the assistant's own turn, not of the prompt — and
    folding it in here would make detect() answer "did this gate ultimately block", which
    depends on session state a corpus replay does not have. The measurable question is
    "is this a halt signal", and that is what this answers.

    main() calls this rather than re-applying HALT_RX, so the measured code and the live
    code cannot drift. Pure: no I/O, no writes, safe to run in bulk outside a session.
    """
    return [m.group(0) for m in HALT_RX.finditer(text or "")][:3]


def latest_jsonl():
    try:
        files = sorted(JSONL_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None
    except Exception:
        return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if os.environ.get("LEARNED_LAYER", "1") == "0":
        return 0
    if data.get("stop_hook_active"):  # harness loop guard
        return 0

    path = latest_jsonl()
    if not path:
        return 0

    trigger_prompt = ""
    turn_text_parts = []
    turn_tools = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                dtype = d.get("type")
                msg = d.get("message") or {}
                content = msg.get("content")
                if dtype == "user":
                    is_tool_result_only = (
                        isinstance(content, list) and len(content) > 0
                        and all(isinstance(p, dict) and p.get("type") == "tool_result" for p in content)
                    )
                    if not is_tool_result_only:  # real user prompt → new turn
                        turn_text_parts = []
                        turn_tools = []
                        trigger_prompt = content if isinstance(content, str) else ""
                    continue
                if dtype != "assistant" or not isinstance(content, list):
                    continue
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        t = p.get("text", "") or ""
                        if t:
                            turn_text_parts.append(t)
                    elif p.get("type") == "tool_use":
                        nm = p.get("name") or ""
                        if nm:
                            turn_tools.append(nm)
    except Exception:
        return 0

    # Ignore hook-injected / slash-command synthetic prompts — not a real halt.
    if not _is_user_text(trigger_prompt):
        return 0
    if not detect(trigger_prompt):
        return 0
    if not (set(turn_tools) & MUTATING_TOOLS):
        return 0
    if ACK_RX.search(" ".join(turn_text_parts)):
        try:  # shadow (2026-06-30): HALT+mutating-tool fired but an ack disarmed the block — near-fire telemetry to decide keep/remove
            FIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with FIRE_LOG.open("a") as f:
                f.write(_fire_ts() + "\tvalidator\tshadow\t" + trigger_prompt[:80].replace("\n", " ") + "\n")
        except Exception:
            pass
        return 0

    # Violation: HALT signal + mutating tool + no acknowledgement.
    try:
        import datetime as _dt
        _ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")  # L3 fix 2026-06-23: timestamp + this is the first real fire-record for the dormant blocking half
        FIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FIRE_LOG.open("a") as f:
            f.write(_ts + "\tvalidator\tblock\t" + trigger_prompt[:80].replace("\n", " ") + "\n")
    except Exception:
        pass
    reason = (
        "LEARNED CONTRACT — stop-and-plan: your prompt was a halt signal, but this turn ran a "
        "mutating tool (Write/Edit) without first acknowledging the redirect. Per the learned "
        "contract: STOP, acknowledge what changed explicitly, lay out the plan or your one-line "
        "interpretation, and WAIT — do not execute on momentum. Re-respond: acknowledge the "
        "redirect first, then ask or lay out the plan instead of editing."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
