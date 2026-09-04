#!/usr/bin/env python3
"""The always-loaded steering surface has a token budget, and CI enforces it.

Master plan Phase 4.1. Every file below is read on EVERY prompt, forever. That makes each one a
recurring tax rather than a one-time cost, and nothing was measuring the total — it reached
16,557 tokens per prompt without anyone deciding it should.

THE POINT OF A CEILING is not the number. It is that adding prose now requires deleting prose.
Without it, every good intention that ever gets written down is permanently free to its author
and permanently expensive to every future turn, which is exactly how this surface grew.

TWO NUMBERS, AND THE DIFFERENCE MATTERS
---------------------------------------
  CEILING  what CI actually fails on. Set just above the current measured total, so the file set
           cannot grow. Ratchets DOWN as cuts land; it must never be raised.
  TARGET   4,000 tokens, from the master plan. Not yet met and not pretended to be met.

Reporting the target as if it were achieved is the exact dishonesty this session has spent all
day removing from other metrics, so the gap is printed on every run.

WHAT IS ALREADY CUT, AND WHAT IS NOT
------------------------------------
  tasks/lessons.md   6,768 -> 1,287   each entry's heading IS the rule; the story moved to
                                      lessons-archive.md verbatim, nothing dropped.
  rot-check ABA      8,198 ->   307   it was re-injecting CLAUDE.md and every rules file on top
                                      of context that already contained them (Phase 0.4).

Still over budget, and deliberately untouched here: the four .claude/rules/ files and
CLAUDE.base.md are BASELINE-SHARED — editing them changes every Core in the fleet. The plan
requires an adversarial review pass before a blast-radius change, so they are named as targets
rather than trimmed in the same commit that adds the ceiling.

Run: python3 bin/tests/test_steering_budget.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])

# THE LIST LIVES IN bin/steering_load.py, NOT HERE.
#
# It used to live here, and artifact_generator.py's promote gate kept its own idea of what to
# measure — the nine-file ceiling this test records, minus CLAUDE.md alone. That gate reported
# 15,669 tok free on a seat 7,441 tok in breach (business), 12,166 on one 3,739 over (school),
# 11,878 on one 2,674 over (ops), 9,871 on one 2,747 over (finance), and ~9,500 on life, whose
# true headroom was 23. Two copies of "what is always loaded" is what made that possible, so
# there is now one and both callers import it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import steering_load as _sl  # noqa: E402

ALWAYS_LOADED = _sl.ALWAYS_LOADED

# A PER-CORE RATCHET, not a global constant — and that correction came from actually testing a
# pull. The first version hardcoded CEILING = 11_500, calibrated on core-life AFTER its lessons
# compression. Run on core-business it failed instantly by 7,217 tokens, because CLAUDE.md and
# lessons.md are per_core_keep: every Core has its own, none of the others have been compressed,
# and a number measured on one Core is meaningless on the next.
#
# That is the same fleet-propagation error Fable caught in the PRE_EMPTED verdict, wearing
# different clothes: I calibrated a gate on life's state and shipped it to five Cores.
#
# The ratchet fixes it without needing a number per Core. Each Core records its own first
# measurement and may never exceed it. That enforces the thing the ceiling was FOR — adding
# steering prose requires deleting steering prose — on any Core, at any starting size, with no
# configuration. TARGET stays as the shared direction of travel, reported and never enforced.
TARGET = 4_000       # master plan Phase 4.1 — informational, fleet-wide
TOLERANCE = 200      # absorbs a lesson being appended mid-session; a real regression dwarfs it

# Named so a future cut has somewhere to start, and so the reason each is still here is on record.
KNOWN_TARGETS = {
    ".claude/rules/subagents.md": "states model routing four separate times",
    ".claude/rules/memory.md": "enforcement-history narratives that could be pointers",
    ".claude/rules/codex-routing.md": "inert where the codex CLI is absent; 92% baseline-resident",
    ".claude/CLAUDE.base.md": "overlaps the rules files it points at",
}


def _baseline_path() -> Path:
    return ROOT / ".claude" / "state" / ".steering-budget-baseline.json"


def _load_baseline():
    try:
        return int(json.loads(_baseline_path().read_text())["ceiling"])
    except Exception:
        return None


def _record_baseline(total: int) -> None:
    try:
        p = _baseline_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ceiling": total, "recorded": "first measurement on this Core"},
                                indent=2))
    except Exception:
        pass


def main() -> int:
    rows, total = _sl.measure(ROOT)   # same function the promote gate calls

    print(f"=== always-loaded steering budget — {len(rows)} files ===\n")
    for rel, n in sorted(rows, key=lambda r: -r[1]):
        flag = "  ←" + KNOWN_TARGETS[rel][:52] if rel in KNOWN_TARGETS else ""
        print(f"  {n:6,} tok  {rel}{flag}")
    print(f"  {'-' * 60}")
    print(f"  {total:6,} tok  TOTAL per prompt")
    baseline = _load_baseline()
    if baseline is None:
        _record_baseline(total)
        print(f"  {total:6,} tok  ceiling — FIRST measurement on this Core, recorded as its ratchet")
        print(f"  {TARGET:6,} tok  target (fleet-wide direction of travel)\n")
        print(f"  PASS  baseline recorded. From now on this Core may not exceed "
              f"{total:,} (+{TOLERANCE} tolerance).")
        ok = True
    else:
        ceiling = baseline + TOLERANCE
        print(f"  {baseline:6,} tok  this Core's recorded ratchet (+{TOLERANCE} tolerance)")
        print(f"  {TARGET:6,} tok  target — gap {max(0, total - TARGET):,}\n")
        ok = True
        if total > ceiling:
            ok = False
            print(f"  FAIL  grew {total - baseline:,} tok past this Core's own baseline. Adding "
                  f"steering prose requires deleting steering prose.")
        else:
            print(f"  PASS  within this Core's ratchet ({ceiling - total:,} tok of headroom)")
            if total < baseline:
                _record_baseline(total)
                print(f"  ↓     ratchet TIGHTENED to {total:,} — a cut is permanent, "
                      f"the budget never drifts back up")
        if total > TARGET:
            print(f"  NOTE  {total - TARGET:,} tok above the fleet target. On a peer Core the "
                  f"biggest remaining items are per_core_keep (CLAUDE.md, lessons.md) and are "
                  f"that Core's own to compress; the shared rules files need an adversarial pass.")

    ok = _assert_ratchet_cannot_rise() and ok

    print(f"\n=== {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


def _assert_ratchet_cannot_rise() -> bool:
    """THE CEILING MUST ONLY EVER FALL, or a regression decays into the baseline.

    core-business, bus #976: its drift check compared each run against the LAST run and rewrote the
    snapshot unconditionally, so a degradation was visible for exactly ONE run and then became the
    new normal. `a.py: FINDINGS -> CLEAN` on run 1; `No state changes` on run 2 and forever after.
    A detector that silently stopped detecting was self-erasing.

    This ratchet is immune BY CONSTRUCTION rather than by luck: `_record_baseline` is called only
    inside `if total < baseline`, so an overshoot never gets written down. Verified by running, not
    by reading — 300 tok of prose planted into lessons.md produced `FAIL … grew 956 tok` on three
    consecutive runs with the ceiling unmoved, then PASS on revert.

    That immunity is one edit away from being lost, and the edit would look like a fix ("record the
    new total so the message is accurate"). So the guard is checked structurally: every call site
    must sit under a condition that mentions `baseline`.
    """
    import ast
    src = Path(__file__).read_text()
    tree = ast.parse(src)

    def guarded_calls(mod):
        """Distinct call sites, and how many sit under a baseline-comparing condition.

        Counted PER CALL NODE, not per enclosing `if`. The first version walked every `If` and
        incremented for each call inside it, so one call nested two levels deep counted twice and
        the check printed "2 calls, 4 guarded" — a guarded count larger than the total. The control
        still fired, so it PASSED while printing a number that cannot be true. Same defect the
        commit one hour earlier called out in my own position-invariance control: a check is not
        allowed to report a figure its own arithmetic contradicts.
        """
        guarded_nodes = set()
        for node in ast.walk(mod):
            if not isinstance(node, ast.If):
                continue
            # THE CONDITION MUST CONSTRAIN THE DIRECTION, not merely mention the baseline. The
            # first version accepted any enclosing `if` whose test named baseline or ceiling — which
            # includes the outer `if total > ceiling: … else:` that says nothing about whether the
            # new value is LOWER. Under that reading the control (`if True:`) left the call still
            # "guarded" by the outer branch and the check reported no teeth, correctly. What makes
            # the ceiling monotonic is specifically `total < baseline`, so that is what is required.
            cond = ast.dump(node.test)
            is_strict_less = (isinstance(node.test, ast.Compare)
                              and any(isinstance(o, ast.Lt) for o in node.test.ops)
                              and "baseline" in cond)
            if not is_strict_less:
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                        and inner.func.id == "_record_baseline"):
                    guarded_nodes.add(id(inner))
        all_nodes = {id(n) for n in ast.walk(mod)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "_record_baseline"}
        return len(guarded_nodes), len(all_nodes)

    guarded, all_calls = guarded_calls(tree)
    # The first-measurement path records unconditionally and must — there is no baseline to compare
    # against yet. That is why this counts calls reachable under a baseline-comparing condition
    # rather than demanding every call be guarded.
    print("\n  ratchet: %d _record_baseline call(s), %d under a strict `total < baseline` "
          "guard (the other is the first-measurement path, correctly unconditional)"
          % (all_calls, guarded))
    if guarded < 1:
        print("  FAIL  no _record_baseline call sits under a strict less-than guard — the ceiling can "
              "now rise, and a regression will decay into it exactly as core-business's did")
        return False

    # THE CONTROL. Without it this passes on a file where the AST walk simply found nothing.
    broken = src.replace("            if total < baseline:\n                _record_baseline(total)",
                         "            if True:\n                _record_baseline(total)", 1)
    if broken == src:
        print("  FAIL  could not locate the guarded call to build a control — re-point this check")
        return False
    g2, _ = guarded_calls(ast.parse(broken))
    if g2 >= guarded:
        print("  FAIL  the control did not reduce the guarded count (%d -> %d); this check has no "
              "teeth" % (guarded, g2))
        return False
    print("  PASS  ceiling can only fall, and an unguarded rewrite would be detected (%d -> %d)"
          % (guarded, g2))
    return True


if __name__ == "__main__":
    sys.exit(main())
