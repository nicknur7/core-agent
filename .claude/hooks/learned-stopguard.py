#!/usr/bin/env python3
"""learned-stopguard.py — PreToolUse hook. Proposal 1 of the resynth-surfaced
blocking rules: catch the PURE-BARREL case *before* the edit lands.

Blocks a mutating tool (Write/Edit/MultiEdit/NotebookEdit) iff the user prompt that
triggered this turn was an unambiguous HALT signal AND Core has produced NO assistant
text yet this turn (i.e. it went straight to editing with zero words — no
acknowledgement possible). Complements learned-validator.py (which catches the
has-words-but-no-ack case at Stop): this one prevents the file mutation entirely in
the most clear-cut barrel.

Deliberately narrow (no text at all this turn) so it can't over-block the legitimate
"acknowledge in words, then edit" flow — that always has assistant text first.

Block protocol: print reason to stderr + exit 2 (Claude Code PreToolUse convention).
SAFETY: fails open (exit 0) on any error / kill-switch. LEARNED_LAYER=0 disables.
LEARNED_JSONL_DIR overrides the transcript dir for tests.
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

HALT_RX = re.compile(
    r"\b(hol+d+\s*up|hol+dup|wait\s+wait|stop\s+(with|doing|wasting|right now)|"
    r"cut\s+it\s+out|quit\s+it|that'?s\s+enough|knock\s+it\s+off|pump\s+the\s+brakes)\b",
    re.I,
)
MUTATING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def detect(text):
    """Halt/stop-signal phrases in `text`.

    Extracted so bin/grade-gate.py can measure this gate against the real corpus and
    bin/grade-intent.py can compare what it CATCHES against what it was BUILT to catch.
    A gate with no detect() is outside both instruments — it fires in production and nothing
    can say whether it is firing on the right things.

    main() calls this rather than re-applying the regex, so the measured code and the live
    code cannot drift. Pure: no I/O, no writes, safe to run in bulk outside a session.
    """
    return [m.group(0) for m in HALT_RX.finditer(text or "")][:3]


# HOOK_PREFIXES lived here in FOUR byte-identical copies (sha a69e7ba31ca1) and none
# of them listed `<task-notification>` — 72% of this seat's prompt-stage traffic. One
# definition now: .claude/hooks/_prompt_source.py. Fails toward firing on import error.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _prompt_source import is_user_text as _is_user_text
except Exception:  # pragma: no cover
    def _is_user_text(_p):
        return True


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
    if data.get("tool_name") not in MUTATING_TOOLS:
        return 0

    path = latest_jsonl()
    if not path:
        return 0
    trigger_prompt = ""
    turn_has_text = False
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
                    if not is_tool_result_only:  # real prompt → new turn
                        trigger_prompt = content if isinstance(content, str) else ""
                        turn_has_text = False
                    continue
                if dtype != "assistant" or not isinstance(content, list):
                    continue
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text" and (p.get("text") or "").strip():
                        turn_has_text = True
    except Exception:
        return 0

    if not _is_user_text(trigger_prompt):
        return 0
    if not detect(trigger_prompt):
        return 0
    if turn_has_text:  # words were produced → not a pure barrel; let the Stop validator judge
        try:  # shadow (2026-06-30): HALT matched but text was produced — near-fire telemetry (the pure-barrel denominator)
            FIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with FIRE_LOG.open("a") as f:
                f.write(_fire_ts() + "\tstopguard\tshadow\t" + trigger_prompt[:80].replace("\n", " ") + "\n")
        except Exception:
            pass
        return 0

    try:
        FIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FIRE_LOG.open("a") as f:
            f.write(_fire_ts() + "\tstopguard\tblock\t" + trigger_prompt[:80].replace("\n", " ") + "\n")
    except Exception:
        pass
    sys.stderr.write(
        "LEARNED STOPGUARD: you got a HALT signal and went straight to a mutating tool with zero "
        "words this turn. Stop — acknowledge the redirect and lay out the plan first; don't edit on "
        "momentum. (This blocked the edit before it ran.)\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
