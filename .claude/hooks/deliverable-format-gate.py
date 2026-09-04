#!/usr/bin/env python3
"""Stop hook — deliverable-format gate (ops resynth proposal #4, 2026-07-19).

Nick's recurring pain: he asks for something he'll VIEW/keep (a doc, a docx, a
report, "copy/paste ready", a file) and Core answers with a terminal text dump
instead of a delivered artifact ("YOU JUST GAVE IT TO ME IN THE TERMINAL").
Backed by the memory 'View artifacts -> dropped clickable file'.

Detects, on the just-finished turn: did Nick's prompt ask for a viewable
deliverable, AND did the turn produce NO satisfying delivery (a Write/Edit to a
deliverable-extension file, or a SendUserFile / Artifact call)? If so it FLAGS.

SHADOW MODE (SHADOW=True): logs the would-block to hook-events.log and exits 0 —
NEVER blocks. This is the shadow-first calibration phase; Nick reviews the fire
log (`.claude/state/hook-events.log`, tag 'deliverable-format-gate') to tune the
trigger against false positives, THEN flips SHADOW=False to make it binding.

Mirrors the turn-accumulation + JSONL-scan pattern of say-do-gap.py.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # user name from identity.json, never hardcoded
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
try:
    import hooklog
except Exception:
    hooklog = None

SHADOW = False  # LIVE (Nick 2026-07-20: "no more shadowing" — enforce autonomously, reversible).

PROJECTS_DIR = os.path.expanduser("~/.claude/projects/-" + os.getcwd().lstrip("/").replace("/", "-").replace(" ", "-"))

# The user asked for something they'll VIEW / KEEP as an artifact. Scoped to avoid
# firing on ordinary "edit this file" / "fix the code" asks — keys on doc/report
# nouns, explicit file extensions, "copy/paste", and "write it up / put it in a".
DELIVERABLE_ASK = re.compile(
    r"(?:"
    r"\b(?:give|send|make|build|draft|write|put|prepare)\s+(?:me\s+|us\s+)?(?:it\s+|this\s+|that\s+|the\s+|a\s+|an\s+)?"
    r"(?:up\s+)?(?:in(?:to)?\s+)?(?:a\s+|an\s+|the\s+)?(?:doc|docx|document|report|memo|letter|write[- ]?up|writeup|spreadsheet|slide|deck|pdf|word\s+doc)"
    r"|\bin\s+(?:a\s+)?(?:docx?|pdf|markdown|word|excel|spreadsheet|powerpoint|pptx?|xlsx?)\b"
    r"|\bas\s+(?:a\s+)?(?:docx?|pdf|document|report|write[- ]?up|writeup|spreadsheet|deck)\b"
    r"|\.(?:docx?|pdf|xlsx?|pptx?|csv)\b"
    r"|\bcopy[\s/-]?paste(?:able|-ready)?\b"
    r"|\bnot\s+(?:in\s+)?the\s+terminal\b"
    r")",
    re.IGNORECASE,
)

# Delivery tools that always satisfy (they hand Nick a viewable/keepable thing).
SATISFYING_NAMES = {"SendUserFile", "Artifact"}
# A Write/Edit satisfies ONLY if it targets a deliverable-extension file.
DELIVERABLE_EXT = re.compile(r"\.(?:md|docx?|pdf|xlsx?|csv|html?|pptx?)$", re.IGNORECASE)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def latest_jsonl():
    try:
        files = [os.path.join(PROJECTS_DIR, f) for f in os.listdir(PROJECTS_DIR) if f.endswith(".jsonl")]
    except FileNotFoundError:
        return None
    return max(files, key=os.path.getmtime) if files else None


def _claimtext():
    """Shared mention discriminators (lib/claimtext.py). Fail-open: without the lib the gate
    still works, just without quote/negation suppression."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import claimtext
        return claimtext
    except Exception:
        return None


def detect(text):
    """Deliverable-shaped asks in `text` — a request for something Nick will VIEW.

    Extracted 2026-07-28 so bin/grade-gate.py can measure this gate against the real corpus
    and bin/grade-intent.py can check it against its own intent record. Until a gate exposes
    detect() it is invisible to every measurement, tuning and retirement path in the system.
    """
    ct = _claimtext()
    text = text or ""
    spans = ct.quoted_spans(text) if ct else None
    hits = []
    for m in DELIVERABLE_ASK.finditer(text):
        if ct and ct.is_mention(text, m.start(), m.end(), spans):
            continue
        hits.append(m.group(0))
        if len(hits) >= 3:
            break
    return hits


def main():
    # Record that this hook RAN, matched or not. Without it the ledger can count FIRES but not
    # INVOCATIONS, so yield (fires/invocations) is not computable — and a gate that fires 4 times
    # out of 4 looks identical to one that fires 4 times out of 400. Added 2026-07-30 across every
    # instrumented hook that lacked it, so low-yield becomes a measurable verdict rather than a
    # guess. (The ledger could retire EXPENSIVE components but not cheap-and-useless ones; this is
    # the missing term.)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("deliverable-format-gate", "Stop")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("stop_hook_active"):
        return 0
    session = data.get("session_id", "") or ""
    path = latest_jsonl()
    if not path:
        return 0

    # Accumulate the CURRENT turn: the user prompt text that started it + the
    # assistant tool calls (with Write/Edit file paths) since.
    user_text_parts = []
    satisfied = False
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
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
                        # new real user turn → reset
                        user_text_parts = []
                        satisfied = False
                        if isinstance(content, str):
                            user_text_parts.append(content)
                        elif isinstance(content, list):
                            for p in content:
                                if isinstance(p, dict) and p.get("type") == "text":
                                    user_text_parts.append(p.get("text", "") or "")
                    continue
                if dtype != "assistant" or not isinstance(content, list):
                    continue
                for p in content:
                    if not isinstance(p, dict) or p.get("type") != "tool_use":
                        continue
                    nm = p.get("name") or ""
                    if nm in SATISFYING_NAMES:
                        satisfied = True
                    elif nm in WRITE_TOOLS:
                        fp = str((p.get("input") or {}).get("file_path", "") or "")
                        if DELIVERABLE_EXT.search(fp):
                            satisfied = True
    except FileNotFoundError:
        return 0

    user_text = " ".join(user_text_parts).strip()
    if not user_text:
        return 0
    m = DELIVERABLE_ASK.search(user_text)
    if not m or satisfied:
        return 0

    # Gap: a deliverable was asked for, none delivered.
    trigger = m.group(0)
    if hooklog is not None:
        hooklog.log("deliverable-format-gate", "Stop",
                    verdict=("shadow" if SHADOW else "block"),
                    trigger=trigger, session=session)
    if SHADOW:
        return 0  # calibration phase — never block
    reason = (
        f"DELIVERABLE-FORMAT GATE — {_U.possessive()} prompt asked for a viewable/keepable deliverable "
        f"(matched: \"{trigger}\") but this turn produced no Write to a .md/.docx/.pdf/.xlsx file, "
        "no SendUserFile, and no Artifact. Don't hand him a terminal text dump. Re-respond: "
        "WRITE the deliverable to a file and SendUserFile it (or publish an Artifact for a web view), "
        "then give a tight summary in chat. (Memory: view-artifacts-to-dropped-clickable-file.)"
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
