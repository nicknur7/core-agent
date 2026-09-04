#!/usr/bin/env python3
"""Stop hook — cross-Core completion gate. Blocks a response that declares a
sync / rollout / fix "done" or "clean" ACROSS all Cores unless this turn shows
tool evidence that a PEER Core was actually checked (not just life).

Approved by Nick 2026-07-11 (proposed by the learned-resynth pass). Motivating
failure mode (verify-dont-claim / recall-first / model-routing corpora): Core
verifies something in ONE Core — usually life, where it's working — then claims
it's "clean across all Cores" / "synced to every Core" / "done on business,
school, and finance" without ever reading the peer Cores. The peer might be
stale, unpulled, or broken.

Life is the working dir, so any local read covers life. The historically-missing
evidence is the PEER check — so this gate requires ≥1 tool call THIS turn that
targets a peer Core: a peer-* MCP read, a path under core-business/school/finance,
or a baseline/remote check (the peers pull from baseline). Fires only when a
cross-Core phrase co-occurs with a completion/clean word, so architectural prose
("the hook runs on every Core") doesn't trigger.

Non-destructive: {"decision":"block","reason":...}, always exit 0.
Honors stop_hook_active. Kill-switch: CROSS_CORE_GATE=0.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
try:
    import hooklog
except Exception:
    hooklog = None

PROJECTS_DIR = os.path.expanduser("~/.claude/projects/-" + os.getcwd().lstrip("/").replace("/", "-").replace(" ", "-"))
_SESSION = ""

# ── Cross-Core collective phrasing ───────────────────────────────────────────
_CROSS_CORE_RX = re.compile(
    r"(?:across\s+(?:all\s+)?(?:the\s+)?(?:four\s+|4\s+|peer\s+)?cores)|"
    r"(?:(?:on|to|for)\s+(?:all|every|each|both)\s+(?:the\s+)?(?:peer\s+)?cores)|"
    r"(?:all\s+(?:four|4)\s+cores)|"
    r"(?:every\s+(?:peer\s+)?core)|"
    r"(?:(?:all|both)\s+(?:the\s+)?peers?)|"
    r"(?:propagat\w+\s+to\s+(?:all\s+)?(?:the\s+)?(?:peer\s+)?cores)|"
    r"(?:business,?\s+school,?\s+(?:and\s+)?finance)|"
    r"(?:life,?\s+business,?\s+school)",
    re.IGNORECASE,
)
# ── Completion / clean assertion ─────────────────────────────────────────────
_COMPLETION_RX = re.compile(
    r"\b(?:done|complete[d]?|clean|synced|in\s+sync|verified|passing|passes|green|"
    r"propagated|rolled\s+out|live|shipped|deployed|fixed|resolved|working|"
    r"up\s+to\s+date|updated|consistent|aligned|all\s+set|good\s+to\s+go|no\s+drift)\b",
    re.IGNORECASE,
)

# ── Peer-Core evidence: a tool call this turn that actually touched a peer ────
_PEER_EVIDENCE_RX = re.compile(
    r"mcp__peer-(?:business|school|finance)__|"     # peer MCP read
    r"core-(?:business|school|finance)|"             # peer instance path
    r"nicknur7/core-agent|"                          # baseline remote (peers pull from it)
    r"sync-(?:from|to)-baseline|"                    # sync scripts
    r"\bpeer_(?:read|list|read_current_state)\b",
    re.IGNORECASE,
)


def latest_jsonl():
    try:
        files = [os.path.join(PROJECTS_DIR, f) for f in os.listdir(PROJECTS_DIR) if f.endswith(".jsonl")]
    except FileNotFoundError:
        return None
    return max(files, key=os.path.getmtime) if files else None


def _sentences(text):
    return re.split(r"(?<=[.!?\n])\s+", text)


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
    """A completion claim about ANOTHER Core's work — the class that needs peer evidence.

    Requires BOTH a cross-Core reference and a completion assertion in the same text: either
    alone is ordinary. Extracted 2026-07-28 for measurement and intent-grading.
    """
    ct = _claimtext()
    text = text or ""
    if not (_CROSS_CORE_RX.search(text) and _COMPLETION_RX.search(text)):
        return []
    spans = ct.quoted_spans(text) if ct else None
    hits = []
    for m in _COMPLETION_RX.finditer(text):
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
        import hooklog as _hl; _hl.invoked("cross-core-completion-gate", "Stop")
    except Exception:
        pass
    if os.environ.get("CROSS_CORE_GATE") == "0":
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    global _SESSION
    _SESSION = data.get("session_id", "") or ""
    if data.get("stop_hook_active"):
        return 0
    path = latest_jsonl()
    if not path:
        return 0

    turn_text_parts, turn_tools = [], []
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
                        turn_text_parts, turn_tools = [], []
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
                        blob = (p.get("name") or "")
                        try:
                            blob += " " + json.dumps(p.get("input") or {}, ensure_ascii=False)
                        except Exception:
                            pass
                        turn_tools.append(blob)
    except FileNotFoundError:
        return 0

    last_text = " ".join(turn_text_parts)
    if not last_text or len(last_text.strip()) < 60:
        return 0

    # Fire on a sentence claiming completion/clean state ACROSS Cores.
    hits = []
    for s in _sentences(last_text):
        if _CROSS_CORE_RX.search(s) and _COMPLETION_RX.search(s):
            hits.append(s.strip()[:120])
            if len(hits) >= 2:
                break
    if not hits:
        return 0

    peer_checked = any(_PEER_EVIDENCE_RX.search(b) for b in turn_tools)
    if peer_checked:
        return 0

    if hooklog is not None:
        try:
            hooklog.log("cross-core-completion-gate", "Stop", verdict="block",
                        trigger=hits[0][:60], session=_SESSION)
        except Exception:
            pass

    hits_str = "; ".join(f"\"{h}\"" for h in hits[:2])
    reason = (
        f"CROSS-CORE COMPLETION GATE — your response claims a cross-Core done/clean state "
        f"({hits_str}) but no tool call this turn actually checked a PEER Core "
        "(peer-business/school/finance MCP read, a core-business/school/finance path, or a "
        "baseline/remote check). Verifying life alone and claiming it holds for every Core is "
        "the exact recurring miss. Append a footnote: either (a) read the peer Core(s) and cite "
        "what you found on each, or (b) scope the claim down (\"verified in life; peers pull on "
        "next open / not yet checked\"). Keep the prior reply intact."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
