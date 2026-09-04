#!/usr/bin/env python3
"""si_snapshot.py — regenerate a Core's learned-contracts.json from ITS OWN DB (unified redesign step ④).

Plan: PART 8 (per-Core self-improvement). The learned-classifier reads a FILE snapshot; historically
that file was a static generic starter identical on every Core, so peers could never self-tune. This
regenerates the snapshot from each Core's own `learned_contracts` rows (org-scoped) — INCLUDING the
`triggers` (data-driven) — so a contract induced from THIS Core's corpus reaches THIS Core's classifier
and fires. Run at close (after mining/induction) so per-Core SI actually accrues.

Output: $CORE_INSTANCE/.claude/state/learned-contracts.json, a dict keyed by situation-key:
  { "<key>": {"required_shape":[...], "forbidden_moves":[...], "triggers":[...]} }
situation-key = the leading token of `situation` before ' — ' (matches the classifier's keys).

Fork-safe (CORE_ORG_ID). Idempotent. Fail-open.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain-pg"))
from _env import connect_corebrain, get_org_id  # noqa: E402

# Base incoming-prompt triggers for the 6 universal starter contracts (regex strings). A Core with
# no triggers stored on these keys inherits these so the shipped starter fires out of the box; a
# Core's own DB triggers (backfilled or induced) override. stop-and-plan / frustration-deescalate
# have NO classifier trigger (stop-signal-gate.py covers them — avoids double-injection).
BASE_TRIGGERS = {
    "plan-not-execute": r"\b(find (all )?(the )?(problems|issues|bugs)|diagnose|don'?t (just )?(go )?fix|bring (me )?a plan|not what i (want|asked|meant)|that'?s not what i (want|asked)|go back|just (research|look)|do (more|a|another) (research|pass))\b",
    "verify-dont-claim": r"\b(that'?s wrong|you (just )?said|you changed|that'?s not right|that'?s incorrect|completely (wrong|off)|you flip)\b",
    "recall-first": r"\b(you should (know|be able to see)|go look in the brain|check (my |the )?(brain|memory)|we (talked|discussed)|like i (said|told you)|remember when|last (night|session|time)|you forgot|already told you)\b",
    "model-routing-and-defaults": r"\b(you should have|should have (done|used|put|run|filter)|on opus|use sonnet)\b",
}


def situation_key(situation: str) -> str:
    return re.split(r"\s+[—-]\s+", situation, maxsplit=1)[0].strip()


def build(org: int) -> dict:
    conn = connect_corebrain()
    out = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT situation, required_shape, forbidden_moves, triggers
                           FROM learned_contracts WHERE org_id=%s AND active
                           ORDER BY created_at""", (org,))
            for situation, req, forb, trig in cur.fetchall():
                key = situation_key(situation)
                triggers = list(trig or [])
                if not triggers and key in BASE_TRIGGERS:
                    triggers = [BASE_TRIGGERS[key]]  # inherit shipped base trigger for a starter contract
                out[key] = {"required_shape": list(req or []),
                            "forbidden_moves": list(forb or []),
                            "triggers": triggers}
        return out
    finally:
        conn.close()


def write_snapshot() -> int:
    org = get_org_id()
    inst = os.environ.get("CORE_INSTANCE")
    if not inst:
        raise SystemExit("CORE_INSTANCE required")
    snap = build(org)
    path = Path(inst) / ".claude" / "state" / "learned-contracts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=2))
    return len(snap)


def main() -> int:
    try:
        n = write_snapshot()
        print(f"si_snapshot: wrote {n} contract(s) → learned-contracts.json (org {get_org_id()})")
        return 0
    except Exception as e:
        print(f"si_snapshot: {e}", file=sys.stderr)
        return 0  # fail-open


if __name__ == "__main__":
    sys.exit(main())
