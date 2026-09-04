#!/usr/bin/env python3
"""grade-intent.py — compare what a gate DOES against what it was BUILT to do.

WHY THIS IS NOT grade-gate.py
-----------------------------
grade-gate measures a RATE: how often a gate fires across the real corpus. That answers
"is this noisy" and cannot answer "is this still doing its job", because a rate has no
notion of purpose. Two gates can both fire at 5% for opposite reasons — one catching
exactly the right thing, which happens to be common; the other matching a character pattern
unrelated to what it guards. Narrowing both by the same amount improves one and breaks the
other.

The operator, 2026-07-27: narrowing a rule isn't enough — it should tune itself against what
it's supposed to be doing versus what it's actually doing, and whether that's even the right
thing to be doing.

That is a three-way comparison and this is its first leg:

  1. WHAT IT WAS BUILT TO CATCH   the intent record (bin/hook-intent.py)
  2. WHAT IT ACTUALLY CATCHES     replay against the real corpus
  3. IS THAT STILL RIGHT          does the guarded behaviour still occur (later stage)

THE CONTRACT SIDE ALREADY HAS THIS
----------------------------------
scheduling/claude-si/measure-contract-fitness.py measures recurrence from a contract's
gate-since date and marks it UNENFORCEABLE when recurrence is not declining. Same question,
asked of generated contracts. Hooks had no equivalent, so a hand-written gate could rot or
drift indefinitely with nothing looking. This closes that asymmetry.

DELIBERATELY DETERMINISTIC
--------------------------
This checks only what is exactly decidable: does the gate still catch every positive in its
own record, and still reject every negative? No similarity scoring, no classifier, no judge.

That is a finding, not a simplification. tasks/research/kind-check-research-2026-07-27.md
measured lexical "same-kind" scoring against a real labelled set of 98 right-kind and 126
wrong-kind catches and got AUC 0.54 — a coin flip — with the local-window variant at 0.48,
below chance. Published work explains why the whole similarity family fails here: a claim
and a quoted mention of that claim contain the same words. So the cheap version was deleted
rather than shipped weak.

What remains is the part that decides exactly, and it is not a small part: it would have
caught financial-figure-gate matching "era" inside "generated" the moment it appeared,
because "generated" would have been one of its negatives.

  python3 bin/grade-intent.py                # every gate with an intent record
  python3 bin/grade-intent.py <hook>         # one
  python3 bin/grade-intent.py --write        # record verdicts for estate-sweep to consume
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import io
import json
import sys
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
REGISTRY = REPO / "bin" / "hook-registry.json"
OUT = REPO / ".claude" / "state" / "intent-grades.json"

EVIDENCE = REPO / ".claude" / "state" / "hook-intent-evidence.json"


def _evidence(name: str) -> dict:
    """Corpus examples for one gate, from LOCAL state.

    They live outside bin/hook-registry.json on purpose. The registry is shared with every
    Core and with external forks; these examples are verbatim text from this Core's own
    sessions. Putting them in the registry — which I did first — would have pushed real
    prompts, a filename naming a third party, and assistant replies to the baseline. That is
    the same leak that already reached it once (5ab58a6, 296KB of verbatim corrections).

    Purpose is universal and belongs in the registry. Evidence is per-Core and belongs here.
    """
    try:
        import json as _j
        return _j.loads(EVIDENCE.read_text()).get("gates", {}).get(name, {})
    except Exception:
        return {}


def _gg():
    spec = importlib.util.spec_from_file_location("gg", REPO / "bin" / "grade-gate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fires(mod, text: str) -> bool:
    det = getattr(mod, "detect", None)
    if det is None:
        return False
    try:
        r = det(text)
        return bool(r[0] if isinstance(r, tuple) else r)
    except Exception:
        return False


def grade(name: str, intent: dict, gg) -> dict:
    saved = sys.stdin
    try:
        # A hook that reads stdin at import blocks forever; one that calls sys.exit() at
        # import kills the process. Both were hit building the rate sweep.
        sys.stdin = io.StringIO("")
        mod = gg.load_hook(name)
    except BaseException:
        return {"verdict": "ungradeable", "why": "hook failed to load"}
    finally:
        sys.stdin = saved
    if mod is None or not hasattr(mod, "detect"):
        return {"verdict": "ungradeable", "why": "no detect()"}

    positives = intent.get("positives") or []
    negatives = intent.get("negatives") or []

    missed = [p for p in positives if not _fires(mod, p)]
    caught_neg = [n for n in negatives if _fires(mod, n)]

    # ROT is checked first and outranks imprecision. A gate that no longer catches its own
    # founding examples has stopped doing its job entirely; that is categorically worse than
    # firing somewhat too widely, and it must not be masked by a clean negative set.
    if missed:
        verdict, why = "rotted", (
            f"misses {len(missed)}/{len(positives)} of its own positive examples — it no "
            f"longer catches what it was built for")
    elif caught_neg:
        verdict, why = "imprecise", (
            f"fires on {len(caught_neg)}/{len(negatives)} of its own negative examples — "
            f"the trigger reaches past its purpose")
    elif not positives:
        verdict, why = "unproven", "intent record has no positive examples to check against"
    else:
        verdict, why = "holds", (
            f"catches all {len(positives)} positives, rejects all {len(negatives)} negatives")

    return {
        "verdict": verdict, "why": why,
        "positives": len(positives), "positives_missed": len(missed),
        "negatives": len(negatives), "negatives_caught": len(caught_neg),
        "guards": intent.get("guards", ""),
        # One concrete failing example. "Misses 2 of 4" does not tell anyone WHICH behaviour
        # broke, and a verdict nobody can act on is a verdict nobody acts on.
        "example_failure": (missed or caught_neg or [""])[0][:220],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hook", nargs="?")
    ap.add_argument("--write", action="store_true",
                    help="write verdicts to .claude/state/intent-grades.json")
    a = ap.parse_args()

    reg = json.loads(REGISTRY.read_text())
    have = {}
    for h in reg["hooks"]:
        if not h.get("intent") or h.get("retired"):
            continue
        merged = dict(h["intent"])
        merged.update(_evidence(h["name"]))   # examples come from local state, not the registry
        have[h["name"]] = merged
    if a.hook:
        have = {k: v for k, v in have.items() if k == a.hook}
    if not have:
        print("no gate has an intent record yet — run bin/hook-intent.py first")
        return 1

    gg = _gg()
    results = {name: grade(name, intent, gg) for name, intent in sorted(have.items())}

    tally: dict = {}
    for r in results.values():
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print(f"[grade-intent] {len(results)} gate(s): " +
          ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    for name, r in sorted(results.items()):
        if r["verdict"] != "holds":
            print(f"  {r['verdict']:11} {name:28} {r['why']}")
            # .get, not [] — the ungradeable/early-return branches build a two-key dict with no
            # example_failure, and this crashed the whole run the first time a hook without a
            # detect() carried an intent record. That happened on 2026-07-30, when Phase 2.1
            # backfilled records onto 26 hooks (several of which are shims and side-effect
            # writers with nothing to grade). A reporting tool must never be the thing that
            # fails; it is the last line of defence and its job is to still print.
            if r.get("example_failure"):
                print(f"              e.g. {r['example_failure'][:110]}")

    if a.write:
        payload = {
            "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "gates": results,
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".json.partial")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(OUT)
        print(f"[grade-intent] wrote {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
