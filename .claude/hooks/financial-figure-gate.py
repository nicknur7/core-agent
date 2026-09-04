#!/usr/bin/env python3
"""Stop hook — financial-figure gate. Blocks outbound text that asserts a live
brokerage/account figure (balance, position, buying power, P&L, $ amount tied to
Robinhood / Era / Alpaca / SnapTrade) unless a LIVE account-pull ran this turn.

Approved SI proposal 4be4e18f7ff2 (core-ux, 2026-07-11). Same family as
state-claim-gate: a number asserted from memory about a moving account is a
state-claim, and account balances move. Quoting last session's balance as if
current is the exact failure mode.

SCOPED TIGHT ON PURPOSE — fires ONLY when a figure co-occurs with a
brokerage/account keyword, so routine money talk in life (Etsy revenue, hourly
pay, salary bands, job offers, prices) never triggers. Account keywords +
live-pull tool markers are parameterized in identity.json
(`financial_figure_gate`) with a safe fallback, so each Core (esp. finance)
tunes its own vocabulary.

Natural home: core-finance (where the figures get asserted and Alpaca/SnapTrade
pulls run). Built here because life is the baseline writer; propagates via
`/sync push`. In life the only legit satisfier is a peer-finance MCP read.

Non-destructive: {"decision":"block","reason":...}, always exit 0.
Honors stop_hook_active. Kill-switch: FINANCE_FIGURE_GATE=0.
"""
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
_IDENTITY_FILE = Path(__file__).resolve().parents[1] / "identity.json"
_SESSION = ""

# ── Account keywords (fallback; identity.json can extend) ────────────────────
_FALLBACK = {
    "account_keywords": [
        "robinhood", "era", "alpaca", "snaptrade", "quiver",
        "buying power", "portfolio", "brokerage", "the account", "my position",
        "positions", "cost basis", "unrealized", "realized p&l", "realized pnl",
        "day trade", "equity balance", "cash balance", "account balance",
    ],
    # tool-call markers that count as a LIVE pull this turn
    "pull_tool_markers": [
        "mcp__peer-finance__", "alpaca", "snaptrade", "quiver",
        "trader/", "cfo/", "get_account", "get_positions", "portfolio",
    ],
}


def _load_cfg():
    cfg = {k: list(v) for k, v in _FALLBACK.items()}
    try:
        with open(_IDENTITY_FILE) as f:
            data = json.load(f)
        ff = data.get("financial_figure_gate", {}) or {}
        for key in ("account_keywords", "pull_tool_markers"):
            extra = ff.get(key)
            if isinstance(extra, list):
                for x in extra:
                    if x not in cfg[key]:
                        cfg[key].append(x)
    except Exception:
        pass
    return cfg


_CFG = _load_cfg()
# WORD-BOUNDED (2026-07-27). These were joined as bare alternates, so every keyword matched as a
# SUBSTRING. "era" — a brokerage name in the list — matched inside "gen(era)ted", "sev(era)l",
# "lit(era)l", "op(era)tion". Any sentence containing one of those plus a percentage plus an
# assertion verb was flagged as an unsourced account claim; it fired on a hook-tuning discussion
# containing ">20%" and "generated", with no financial content whatsoever.
#
# \b on both sides. Multi-word keys ("buying power", "cash balance") are unaffected — \b sits at the
# outer edges only, so the internal space still matches normally.
_ACCOUNT_RX = re.compile(
    "|".join(r"\b" + re.escape(k) + r"\b" for k in _CFG["account_keywords"]), re.IGNORECASE)
_PULL_RX = re.compile("|".join(re.escape(k) for k in _CFG["pull_tool_markers"]), re.IGNORECASE)

# A concrete monetary / quantitative figure: $1,234.56 · 1,234 shares · +3.2% · 12.5% up
_FIGURE_RX = re.compile(
    r"(?:\$\s?\d[\d,]*(?:\.\d+)?[kKmMbB]?)|"           # $1,234.56 / $5k
    r"(?:\b\d[\d,]*(?:\.\d+)?\s*(?:shares?|units?|contracts?)\b)|"  # 100 shares
    r"(?:[-+]?\d+(?:\.\d+)?\s?%)",                      # +3.2% / 12%
)
# An assertion frame near the figure — "is/are/at/now/currently/sitting at/up/down"
_ASSERT_RX = re.compile(
    r"\b(?:is|are|was|were|at|now|currently|sitting|holds?|holding|worth|"
    r"up|down|gained|lost|balance|position|value|totals?|stands?)\b",
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
    """Shared mention discriminators (lib/claimtext.py) — see that file's header. Fail-open."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import claimtext
        return claimtext
    except Exception:
        return None


def detect(text):
    """Sentences asserting a specific account figure. All three of account-keyword + figure +
    assertion-frame must co-occur in ONE sentence."""
    ct = _claimtext()
    hits = []
    for sent in _sentences(text or ""):
        if not (_ACCOUNT_RX.search(sent) and _ASSERT_RX.search(sent)):
            continue
        # EVERY figure in the sentence, not just the first. Codex review 2026-07-27: searching once
        # meant "The account is not at $100, but its balance is $50,000 now" was dismissed on the
        # strength of the negated $100 while the asserted $50,000 was never looked at — a bypass
        # you could build by putting a decoy figure ahead of the real one. One unquoted, unnegated
        # figure in an asserting sentence is enough to fire.
        spans = ct.quoted_spans(sent) if ct else None
        if any(not (ct and ct.is_mention(sent, m.start(), m.end(), spans))
               for m in _FIGURE_RX.finditer(sent)):
            hits.append(sent.strip()[:100])
            if len(hits) >= 2:
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
        import hooklog as _hl; _hl.invoked("financial-figure-gate", "Stop")
    except Exception:
        pass
    if os.environ.get("FINANCE_FIGURE_GATE") == "0":
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
    if not last_text or len(last_text.strip()) < 40:
        return 0

    # Fire only on a sentence that has ALL THREE: account keyword + figure + assertion frame.
    hits = detect(last_text)
    if not hits:
        return 0

    live_pull = any(_PULL_RX.search(b) for b in turn_tools)
    if live_pull:
        return 0

    if hooklog is not None:
        try:
            hooklog.log("financial-figure-gate", "Stop", verdict="block",
                        trigger=hits[0][:60], session=_SESSION)
        except Exception:
            pass

    hits_str = "; ".join(f"\"{h}\"" for h in hits[:2])
    reason = (
        f"FINANCIAL-FIGURE GATE — your response states a live account figure "
        f"({hits_str}) but no live account-pull ran this turn (peer-finance MCP read, "
        "or an Alpaca/SnapTrade pull). Balances and positions move — quoting a remembered "
        "number as current is a state-claim on a moving target. Append a footnote: either "
        "(a) pull the live account and cite the value + timestamp, or (b) mark it stale "
        "(\"as of the last pull on <date> — not re-checked this turn\"). Keep the prior reply intact."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
