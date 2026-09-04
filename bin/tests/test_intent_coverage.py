#!/usr/bin/env python3
"""Every registered hook must carry an intent record. CI fails if one does not.

WHY. The ledger the master plan builds in Phase 2 answers, per component: what it was built to
catch · what it actually catches · whether that is still happening · what it costs. The first
leg is the intent record, and on 2026-07-30 exactly HALF the registered entries (26 of 52) had
none — so half the steering surface was structurally unmeasurable. Not underperforming.
Unmeasurable, which is worse, because it looks the same as fine.

The rule this enforces is the one that makes the ledger possible at all: a component that cannot
say what it was for cannot be judged on whether it still does it, and therefore can never be
retired on evidence. It just accumulates.

WHAT COUNTS AS AN INTENT RECORD. `guards` must name the FAILURE the hook prevents, not describe
what the hook does — "logs skill invocations" tells the ledger nothing, "a generated capability
cannot be retired because nothing counted its uses" names something whose recurrence can be
checked. The length floor is a crude proxy for that; the real check is review at authoring time.

`effect` classifies what the hook does when it succeeds, and it is load-bearing rather than
decorative: steering_liveness uses exactly this distinction to tell "no effect" from "effect the
fire counter cannot see", which is what stopped Phase 0.7 retiring four working hooks whose whole
job is a side effect.

Run: python3 bin/tests/test_intent_coverage.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
REG = ROOT / "bin" / "hook-registry.json"

VALID_EFFECTS = {"inject", "block", "log-only", "side-effect"}
MIN_GUARDS_CHARS = 40

# Description verbs. A guards field opening with one of these is almost always describing the
# mechanism instead of naming the failure, which is the failure mode this check exists to stop.
DESCRIPTIVE_OPENERS = ("logs ", "records ", "runs ", "checks ", "fires ", "injects ", "writes ")

# Literal template text left in place. Three entries carried "ONE SENTENCE — what behaviour this
# prevents. WRITE THIS BY HAND." and this check PASSED them: long enough, no descriptive opener.
# So "26 authored, CI enforces it" had a hole exactly one grep wide — a coverage test that counts
# non-empty strings measures non-emptiness, not coverage. (Fable, re-review.)
# Case-SENSITIVE on purpose. The first version lowercased both sides, so "todo" matched inside
# tracking-orphan-guard's legitimate list of tracker filenames it blocks ("backlog, queue,
# dashboard, registry, todo") and failed a perfectly good record. A placeholder is SHOUTED; the
# same letters in ordinary prose are not a placeholder. Caught by running it, one edit after
# writing it.
TEMPLATE_MARKERS = ("WRITE THIS BY HAND", "ONE SENTENCE —", "ONE SENTENCE -", "TODO:", "FIXME",
                    "<describe", "XXX")


def main() -> int:
    if not REG.exists():
        print(f"  SKIP — no registry at {REG}")
        return 0
    hooks = json.loads(REG.read_text()).get("hooks", [])
    live = [h for h in hooks if not h.get("retired")]

    missing, weak, bad_effect = [], [], []
    for h in live:
        who = f"{h.get('name')}@{h.get('event')}"
        intent = h.get("intent") or {}
        guards = (intent.get("guards") or "").strip()
        effect = (intent.get("effect") or "").strip()
        if not guards:
            missing.append(who)
            continue
        if (len(guards) < MIN_GUARDS_CHARS or guards.lower().startswith(DESCRIPTIVE_OPENERS)
                or any(m in guards for m in TEMPLATE_MARKERS)):
            weak.append((who, guards[:70]))
        if effect and effect not in VALID_EFFECTS:
            bad_effect.append((who, effect))

    print(f"=== intent coverage — {len(live)} live registered entries ===\n")
    ok = True
    if missing:
        ok = False
        print(f"  FAIL  {len(missing)} entries with NO intent.guards:")
        for w in missing:
            print(f"          {w}")
    else:
        print(f"  PASS  every live entry carries intent.guards")

    if weak:
        ok = False
        print(f"\n  FAIL  {len(weak)} entries whose guards describes the mechanism "
              f"instead of naming the failure:")
        for w, g in weak:
            print(f"          {w}: {g}")
    else:
        print(f"  PASS  no guards field reads as a description of the mechanism")

    if bad_effect:
        ok = False
        print(f"\n  FAIL  {len(bad_effect)} entries with an unknown effect "
              f"(valid: {sorted(VALID_EFFECTS)}):")
        for w, e in bad_effect:
            print(f"          {w}: {e!r}")
    else:
        n_eff = sum(1 for h in live if (h.get("intent") or {}).get("effect"))
        print(f"  PASS  effect classification valid ({n_eff}/{len(live)} classified)")

    print(f"\n=== {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
