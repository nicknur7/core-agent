#!/usr/bin/env python3
"""learned-classifier.py — UserPromptSubmit hook. The INJECT (preventive) half of the
learned-workflow layer.

NO embedding (Nick's call, 2026-06-05): high-precision keyword triggers map the
incoming prompt to the relevant learned_contract(s) and inject that contract's
required_shape / forbidden_moves as guidance BEFORE Core responds — so the response
is shaped right rather than corrected after. Zero network/DB/latency per prompt.

Never blocks (UserPromptSubmit can only inject). Fail-open: any error → no injection.
Kill-switch: LEARNED_LAYER=0. Reads contract bodies from the snapshot written by
learned-contracts-seed.py (.claude/state/learned-contracts.json).

Complements stop-signal-gate.py (generic halt directive) with the SPECIFIC contract,
and is the preventive counterpart to learned-validator.py (Stop, blocking).
"""
import json
import os
import re
import sys
from pathlib import Path

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
SNAPSHOT = INSTANCE / ".claude" / "state" / "learned-contracts.json"
FIRE_LOG = INSTANCE / ".claude" / "state" / "learned-fires.log"
# HOOK_PREFIXES lived here in FOUR byte-identical copies (sha a69e7ba31ca1) and none
# of them listed `<task-notification>` — 72% of this seat's prompt-stage traffic. One
# definition now: .claude/hooks/_prompt_source.py. Fails toward firing on import error.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _prompt_source import is_user_text as _is_user_text
except Exception:  # pragma: no cover
    def _is_user_text(_p):
        return True

# situation-key -> high-precision trigger on Nick's incoming prompt. Keep precise:
# over-firing desensitizes (boy-who-cried-wolf). Keys match the snapshot.
TRIGGERS = {
    # 2026-06-23 (L4 dedup): 'stop-and-plan' + 'frustration-deescalate' removed —
    # stop-signal-gate.py already injects a halt directive on those same signals
    # (was double-injection, 40/73 fires). Kept: contracts with NO other coverage.
    "plan-not-execute": re.compile(
        r"\b(find (all )?(the )?(problems|issues|bugs)|diagnose|don'?t (just )?(go )?fix|"
        r"bring (me )?a plan|not what i (want|asked|meant)|that'?s not what i (want|asked)|"
        r"go back|just (research|look)|do (more|a|another) (research|pass))\b", re.I),
    "verify-dont-claim": re.compile(
        r"\b(that'?s wrong|you (just )?said|you changed|that'?s not right|that'?s incorrect|"
        r"completely (wrong|off)|you flip)\b", re.I),
    "recall-first": re.compile(
        r"\b(you should (know|be able to see)|go look in the brain|check (my |the )?(brain|memory)|"
        r"we (talked|discussed)|like i (said|told you)|remember when|last (night|session|time)|"
        r"you forgot|already told you)\b", re.I),
    "model-routing-and-defaults": re.compile(
        r"\b(you should have|should have (done|used|put|run|filter)|on opus|use sonnet)\b", re.I),
}



def _hooklog():
    """hooklog.emit prints the payload AND records tokens_injected. Fail-open to a bare print."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog
        return hooklog
    except Exception:
        return None

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
        import hooklog as _hl; _hl.invoked("learned-classifier", "UserPromptSubmit")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if os.environ.get("LEARNED_LAYER", "1") == "0":
        return
    prompt = data.get("prompt") or data.get("user_message") or ""
    if not prompt or len(prompt) < 3 or not _is_user_text(prompt):
        return
    try:
        contracts = json.loads(SNAPSHOT.read_text())
    except Exception:
        return

    # Data-driven triggers (2026-07-18): prefer each contract's OWN `triggers` from the per-Core
    # snapshot (this is what lets an INDUCED contract fire, and lets each Core have its own SI);
    # fall back to the hardcoded TRIGGERS for any legacy key without stored triggers. Compile each
    # pattern safely — a malformed induced regex must never break the hook (fail-open per-pattern).
    matched = []
    for k, c in contracts.items():
        if not isinstance(c, dict):
            continue
        rxs = []
        for p in (c.get("triggers") or []):
            try:
                rxs.append(re.compile(p, re.I))
            except Exception:
                continue
        if not rxs and k in TRIGGERS:
            rxs = [TRIGGERS[k]]
        if any(rx.search(prompt) for rx in rxs):
            matched.append(k)
    if not matched:
        return

    lines = ["📋 LEARNED CONTRACT matched for this prompt — shape your response accordingly:"]
    for k in matched[:2]:  # cap to 2 — avoid context noise
        c = contracts[k]
        lines.append(f"\n[{k}]")
        for s in c.get("required_shape", [])[:3]:
            lines.append(f"  DO: {s}")
        for s in c.get("forbidden_moves", [])[:2]:
            lines.append(f"  DON'T: {s}")

    try:
        import datetime as _dt
        _ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")  # L3 fix 2026-06-23: timestamp so recurrence/graduation is measurable
        FIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FIRE_LOG.open("a") as f:
            f.write(_ts + "\tclassifier\t" + ",".join(matched[:2]) + "\t" + prompt[:60].replace("\n", " ") + "\n")
    except Exception:
        pass

    # session id from the payload, not "". The emit migration hardcoded str(""), leaving every
    # row in the ledger with no session attribution — so per-session cost could not be computed
    # for this hook at all. (Fable, blast-radius review.)
    ctx = "\n".join(lines)
    _sess = str((data or {}).get("session_id") or "")
    _hl = _hooklog()
    if _hl:
        _hl.emit("learned-classifier", "UserPromptSubmit", ctx, session=_sess)
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": ctx}}))


if __name__ == "__main__":
    main()
