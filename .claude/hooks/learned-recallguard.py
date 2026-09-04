#!/usr/bin/env python3
"""learned-recallguard.py — Stop hook. Proposal 2 of the resynth-surfaced blocking
rules: enforce recall-first.

Blocks iff the user prompt that triggered this turn was a clear RECALL-MISS signal
("you should know/see", "go look in the brain", "you forgot", "we talked about") AND
Core answered with ZERO tool calls this turn — i.e. it paraphrased from memory instead
of reading the brain/files. Scoped tight (specific recall phrases + zero tools) so it
only fires on the pure paraphrase-from-memory case, not every mention of a name.

SAFETY: fails open (return 0) on any error / kill-switch / stop_hook_active. A false
block just makes Core re-respond after a lookup. LEARNED_LAYER=0 disables.
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

RECALL_MISS_RX = re.compile(
    r"\b(you\s+should\s+(know|see|be\s+able\s+to\s+see|remember)|go\s+look\s+in\s+the\s+brain|"
    r"check\s+(my\s+|the\s+)?(brain|memory)|you\s+forgot|we\s+(talked|discussed)\s+about|"
    r"remember\s+when|don'?t\s+you\s+remember)\b",
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
    """Recall-demand phrases in `text` — Nick saying the answer is already in memory.

    Extracted so bin/grade-gate.py can measure this gate against the real corpus and
    bin/grade-intent.py can compare what it CATCHES against what it was BUILT to catch.
    Without a detect() a gate is outside both instruments entirely — it fires in production
    and nothing can say whether it is firing on the right things.

    main() calls this rather than re-applying the regex, so the measured code and the live
    code cannot drift apart. Pure: no I/O, no writes, safe to run in bulk outside a session.
    """
    return [m.group(0) for m in RECALL_MISS_RX.finditer(text or "")][:3]


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
    if os.environ.get("LEARNED_LAYER", "1") == "0" or data.get("stop_hook_active"):
        return 0

    path = latest_jsonl()
    if not path:
        return 0
    trigger_prompt = ""
    turn_tools = 0
    brain_tools = 0
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
                    if not is_tool_result_only:
                        trigger_prompt = content if isinstance(content, str) else ""
                        turn_tools = 0
                        brain_tools = 0
                    continue
                if dtype != "assistant" or not isinstance(content, list):
                    continue
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "tool_use":
                        turn_tools += 1
                        nm = str(p.get("name", ""))
                        inp = p.get("input") if isinstance(p.get("input"), dict) else {}
                        if nm.startswith("mcp__core-brain__"):
                            brain_tools += 1
                        elif nm == "Skill" and str(inp.get("skill", "")).strip().lower() in ("claude-brain", "recall-similar"):
                            brain_tools += 1
                        elif nm in ("Bash", "BashOutput") and any(
                            k in str(inp.get("command", "")).lower()
                            for k in ("query.py", "graphify", "claude-brain", "recall_similar", "corebrain")
                        ):
                            brain_tools += 1
    except Exception:
        return 0

    if not _is_user_text(trigger_prompt):
        return 0
    if not detect(trigger_prompt):
        return 0
    if brain_tools > 0:  # 2026-06-26: a BRAIN read clears it; a local Read/Grep no longer does
        try:  # shadow (2026-06-30): recall-demand matched and a brain read cleared it — near-fire telemetry (the contract working)
            FIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with FIRE_LOG.open("a") as f:
                f.write(_fire_ts() + "\trecallguard\tshadow\t" + trigger_prompt[:80].replace("\n", " ") + "\n")
        except Exception:
            pass
        return 0

    try:
        FIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FIRE_LOG.open("a") as f:
            f.write(_fire_ts() + "\trecallguard\tblock\t" + trigger_prompt[:80].replace("\n", " ") + "\n")
    except Exception:
        pass
    reason = (
        "LEARNED CONTRACT — recall-first: your prompt was a recall signal (\"you should know\" / "
        "\"go look in the brain\" / \"we talked about\"), but this turn answered with NO read/recall "
        "tool call — that's paraphrase-from-memory, the exact failure this guards. Re-respond: "
        "read the brain/memory/session files FIRST (Read/Grep/brain recall), then answer from what "
        "you find, not from memory."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
