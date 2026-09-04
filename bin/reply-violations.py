#!/usr/bin/env python3
"""How often does each mistake ACTUALLY reach Nick — measured, not inferred from blocks.

WHY THIS NUMBER DID NOT EXIST BEFORE
------------------------------------
Every previous count of these behaviours came from one of two bad instruments:

  · BLOCKS. A Stop gate fires, the turn is redone, and the event is logged as "prevented" whether or
    not the model would actually have shipped the mistake. A false positive is indistinguishable from
    a catch. The time-claim gate's 11 blocks in a single session included at least one false positive,
    found only because it fired on a reply that QUOTED its own trigger words.
  · CORRECTIONS NICK TYPED. That is a measure of what annoyed him enough to say something, filtered
    through whether he had the patience. `inject-efficacy.py` had to work from this and said so.

`reply-observer.py` (MessageDisplay) reads the real, final, unretried text at zero cost and cannot
interrupt anything — verified 2026-08-06, a probe returning exit 2 there was ignored. So for the first
time the denominator is honest: violations per reply, on the text Nick actually received.

WHAT TO DO WITH IT
------------------
This is the number that tells you whether SUPPLY worked. The plan replaces post-reply gates with
supply (make the mistake impossible) and pre-emption (nudge before the model writes). If those work,
these counts fall toward zero WITHOUT anything ever blocking. If they do not fall, the supply is not
reaching the model and no amount of gate-tuning will fix it.

`sourced` is the column that matters most. An unsourced duration claim is a real violation. A sourced
one means the turn DID call the clock and the claim was legitimate — those are not errors and must not
be counted as such, which is precisely the distinction a blocking gate cannot draw about itself.

    python3 bin/reply-violations.py [--days 7]
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
import os
from pathlib import Path

def _core_root() -> Path:
    """This Core's root — env first, then git, then the file's own location.

    Anchoring on __file__ ALONE resolves the wrong Core the moment a peer runs this file from its
    own seat, which is exactly how core-business's clean enforcement-audit result turned out to be
    a second read of life. Correct in normal use, wrong during cross-Core review — and cross-Core
    review is the operation the fleet's autonomy depends on.
    """
    env = os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        p = Path(env).expanduser()
        if (p / ".claude").is_dir():
            return p.resolve()
    try:
        import subprocess as _sp
        top = _sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, timeout=10).stdout.strip()
        if top:
            return Path(top).resolve()
    except Exception:
        pass
    return Path(__file__).resolve().parents[1]


REPO = _core_root()
LOG = REPO / ".claude" / "state" / "reply-observations.jsonl"

LABEL = {
    "duration_claim": "elapsed/duration claim",
    "state_claim": "system-state assertion",
    "cross_core_claim": "fleet-wide / all-Cores claim",
    "decision_attribution": "decision attributed to the operator",
    "say_do_gap": "promised a write, made none",
    "financial_figure": "account figure without a live pull",
    "deliverable_format": "pasted what should have been a file",
}
REPLACES = {
    "duration_claim": "time-claim-gate (Stop)",
    "state_claim": "state-claim-gate (Stop)",
    "cross_core_claim": "cross-core-completion-gate (Stop)",
    "decision_attribution": "decision-attribution-gate (Stop)",
    "say_do_gap": "say-do-gap (Stop)",
    "financial_figure": "financial-figure-gate (Stop)",
    "deliverable_format": "deliverable-format-gate (Stop)",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    if not LOG.is_file():
        print("no observations yet — reply-observer.py has not recorded anything.")
        print("That is either a clean run or an unregistered hook. Check:")
        print("  python3 -c \"import json;d=json.load(open('.claude/settings.json'));"
              "print([h['command'] for g in d['hooks'].get('MessageDisplay',[]) for h in g['hooks']])\"")
        return 0

    cutoff = int(time.time()) - args.days * 86400
    rows = []
    for line in LOG.read_text(errors="ignore").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if int(r.get("ts", 0)) >= cutoff:
            rows.append(r)

    if not rows:
        print(f"no observations in the last {args.days}d.")
        return 0

    turns = {r.get("turn") for r in rows if r.get("turn")}
    by_kind = defaultdict(lambda: {"total": 0, "unsourced": 0})
    for r in rows:
        k = by_kind[r.get("kind", "?")]
        k["total"] += 1
        if not r.get("sourced"):
            k["unsourced"] += 1

    print(f"reply observations — last {args.days}d, {len(rows)} hit(s) across "
          f"{len(turns)} distinct turn(s)\n")
    print(f"{'behaviour':<32}{'seen':>6}{'UNSOURCED':>11}   replaces")
    print("-" * 92)
    for kind, v in sorted(by_kind.items(), key=lambda kv: -kv[1]["unsourced"]):
        print(f"{LABEL.get(kind, kind):<32}{v['total']:>6}{v['unsourced']:>11}   "
              f"{REPLACES.get(kind, '')}")

    tot_unsourced = sum(v["unsourced"] for v in by_kind.values())
    print()
    if tot_unsourced == 0:
        print("  0 unsourced claims reached the operator. Every hit was backed by a tool call in its own turn.")
        print("  This is the state where the corresponding Stop gates are provably redundant — they")
        print("  would have had nothing to catch.")
    else:
        print(f"  {tot_unsourced} unsourced claim(s) reached the operator. These are the real violations, and")
        print("  the ones a Stop gate would have blocked AFTER they read them. Fix by SUPPLY (make the")
        print("  evidence present before the model needs it), not by blocking later.")

    # Most frequent exact matches — what to supply, concretely.
    common = Counter((r.get("kind"), r.get("matched")) for r in rows if not r.get("sourced"))
    if common:
        print("\n  most frequent unsourced phrasings (what supply must cover):")
        for (kind, matched), n in common.most_common(8):
            print(f"    {n:>3}x  [{kind}]  {matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
