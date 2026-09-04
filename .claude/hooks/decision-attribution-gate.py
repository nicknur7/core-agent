#!/usr/bin/env python3
"""Stop hook — decision-attribution gate. Extends state-claim-gate (rule #1)
to DECISION-HISTORY claims specifically.

Approved SI proposal ee8f354a3314 (core-ux, 2026-07-11). Motivating memory:
`feedback_core_summaries_drift_from_what_nick_said` — Core kept citing its OWN
write-ups as "Nick decided / declined / agreed X" when the brain transcript
showed no such call (the 2026-06-04 "declined" hook was Core's framing, not
Nick's). So: any sentence attributing a decision to Nick / "we" / "you"
("you declined", "we agreed", "we froze", "Nick decided") must be backed, THIS
turn, by a read of the authoritative decision record — `decisions-log.md`, a
session log, or a brain-transcript recall. Otherwise block (append-a-footnote,
non-destructive) so the claim gets verified or downgraded.

Distinct from state-claim-gate: that gate accepts ANY read-shaped tool. This one
requires the read to have actually TARGETED the decision record — grepping an
unrelated file doesn't verify who decided what. So it inspects tool INPUTS, not
just tool names.

Non-destructive: emits {"decision":"block","reason":...}, always exits 0.
Honors stop_hook_active. Kill-switch: DECISION_GATE=0.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # user name from identity.json, never hardcoded
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
try:
    import hooklog
except Exception:
    hooklog = None

PROJECTS_DIR = os.path.expanduser("~/.claude/projects/-" + os.getcwd().lstrip("/").replace("/", "-").replace(" ", "-"))
_SESSION = ""

# ── Decision-attribution triggers ────────────────────────────────────────────
# Anchored on phrasings that attribute a PAST decision to a person/collective.
# Kept deliberately narrow: these read as historical attribution, not as a
# present-turn acknowledgement ("you asked me to X" is intentionally NOT here).
_ACTORS = r"(?:you|we|nick|he)"
_DECISION_VERBS = (
    r"(?:declined|decided|agreed|approved|rejected|vetoed|nixed|"
    r"chose|opted|ruled\s+out|signed\s+off|settled\s+on|committed\s+to|"
    r"locked(?:\s+in)?|froze|okayed|ok['’]?d|greenlit|shot\s+(?:it\s+)?down|"
    r"turned\s+(?:it\s+)?down|passed\s+on|said\s+no|said\s+yes)"
)

PATTERNS = [
    # "you declined", "we agreed", "Nick decided", "we froze", "you ruled out"
    # optional aspect word ("already", "previously", "earlier", "had", "'d").
    re.compile(
        rf"\b{_ACTORS}(?:\s+(?:had|have|'d|'ve|already|previously|earlier|both|all))?\s+{_DECISION_VERBS}\b",
        re.IGNORECASE,
    ),
    # "was/were declined/approved/frozen/rejected/ruled out" — passive attribution
    re.compile(
        r"\b(?:was|were|got)\s+(?:already\s+|previously\s+)?"
        r"(?:declined|approved|rejected|vetoed|frozen|ruled\s+out|shot\s+down|greenlit|signed\s+off\s+on)\b",
        re.IGNORECASE,
    ),
    # "per your/Nick's/our decision", "as we agreed/decided/discussed", "your call was"
    re.compile(
        r"\b(?:per|as\s+per|following)\s+(?:your|nick['’]?s|our|the)\s+"
        r"(?:decision|call|agreement|sign[-\s]?off|approval|instruction)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bas\s+(?:you|we|nick)\s+(?:already\s+|previously\s+|earlier\s+)?"
        r"(?:decided|agreed|discussed|concluded|established|ruled)\b",
        re.IGNORECASE,
    ),
]

# ── What counts as verifying the attribution ─────────────────────────────────
# The read this turn must have TARGETED an authoritative decision record.
# We check tool NAME + INPUT text for these markers.
DECISION_SOURCE_RX = re.compile(
    r"decisions?-log|"                      # memory/decisions-log.md
    r"sessions?/\d{4}-\d{2}-\d{2}|"        # sessions/YYYY-MM-DD.md
    r"sessions?/|session\s+log|"
    r"lessons(?:-archive)?\.md",            # lessons record
    re.IGNORECASE,
)
# Brain-transcript recall tools also satisfy (verify against transcript evidence).
BRAIN_RECALL_RX = re.compile(
    r"mcp__core-brain__(?:get_evidence|recall_at|recall_similar|get_topic|list_recent_sessions)|"
    r"mcp__peer-\w+__peer_(?:read|list_recent_sessions|read_current_state)|"
    r"query\.py",                            # scheduling/brain-pg/query.py recall
    re.IGNORECASE,
)


def _tool_targets_decision_record(name: str, tin) -> bool:
    blob = name or ""
    try:
        blob += " " + json.dumps(tin, ensure_ascii=False)
    except Exception:
        blob += " " + str(tin)
    return bool(DECISION_SOURCE_RX.search(blob) or BRAIN_RECALL_RX.search(blob))


def latest_jsonl():
    try:
        files = [os.path.join(PROJECTS_DIR, f) for f in os.listdir(PROJECTS_DIR) if f.endswith(".jsonl")]
    except FileNotFoundError:
        return None
    return max(files, key=os.path.getmtime) if files else None



def detect(text):
    """Return the attribution claims in `text` that are genuinely ASSERTED.

    Extracted from main() on 2026-07-27 for two reasons. First, this gate blocked a reply whose only
    offence was quoting "you decided" as an EXAMPLE of what it matches — the same defect recall-gate
    had, discovered minutes apart on the same reply, which is why the discriminators now live in
    lib/claimtext.py rather than being fixed twice. Second, bin/grade-gate.py can only measure a hook
    that exposes this contract, and eight of nine gates did not — so their true fire rate against the
    real transcript corpus was simply unknown.
    """
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import claimtext
    except Exception:
        claimtext = None
    hits = []
    spans = claimtext.quoted_spans(text) if claimtext else None
    for rx in PATTERNS:
        for m in rx.finditer(text or ""):
            if claimtext and claimtext.is_mention(text, m.start(), m.end(), spans):
                continue          # quoted or negated — a mention, not a claim
            hits.append(m.group(0).strip())
            if len(hits) >= 3:
                return hits
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
        import hooklog as _hl; _hl.invoked("decision-attribution-gate", "Stop")
    except Exception:
        pass
    if os.environ.get("DECISION_GATE") == "0":
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

    # Accumulate text + (name, input) tool calls across the whole turn.
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
                        turn_tools.append((p.get("name") or "", p.get("input") or {}))
    except FileNotFoundError:
        return 0

    last_text = " ".join(turn_text_parts)
    if not last_text or len(last_text.strip()) < 80:
        return 0

    hits = detect(last_text)
    if not hits:
        return 0

    verified = any(_tool_targets_decision_record(n, i) for n, i in turn_tools)
    if verified:
        return 0

    if hooklog is not None:
        try:
            hooklog.log("decision-attribution-gate", "Stop", verdict="block",
                        trigger=hits[0][:60], session=_SESSION)
        except Exception:
            pass

    hits_str = "; ".join(f"\"{h}\"" for h in hits[:3])
    reason = (
        f"DECISION-ATTRIBUTION GATE — your response attributes a decision "
        f"({hits_str}) to {_U.name()}/us but no read of the decision record ran this turn "
        "(decisions-log.md, a session log, or a brain-transcript recall). "
        f"Core's own summaries drift from what {_U.name()} actually said "
        "(feedback_core_summaries_drift_from_what_nick_said) — don't cite Core's framing "
        f"as {_U.name()}'s call. Append a brief footnote: either (a) grep `memory/decisions-log.md` "
        "(or the session log / brain evidence) and cite it (\"decisions-log 2026-XX-XX confirms\"), "
        "or (b) downgrade — (\"I think we decided X, but I haven't re-checked the record\"). "
        "Keep the prior reply intact; just add the citation/correction."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
