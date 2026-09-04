#!/usr/bin/env python3
"""The three trigger shapes route to three different artifact kinds, and cadence never installs.

The operator, 2026-07-30: skill-tracking should catch not just frustration but actual things that
should become skills — and workflow automation should just get added now if it's needed.

The answer is that the SHAPE of an ask's trigger decides what it becomes, and there are three:

  prompt-shaped   fires on something Nick types             -> hooked_skill
  work-moment     fires when a KIND OF WORK is about to     -> hooked_skill @ PreToolUse
                  happen ("before making changes")
  cadence         has its own clock, waits for nothing      -> scheduled_job PROPOSAL

The first two existed. Cadence is the gap this closes, and it closes it as detection plus a
proposal rather than as a scheduler — on the evidence, and on a hard rule:

  EVIDENCE. A loose pattern matched 16 of 287 distilled asks, but reading them, almost every one
  was "every CORE" / "every element" / "every time" — `every` as a SCOPE quantifier, not a
  schedule. Requiring a real time or lifecycle anchor leaves exactly ONE: "always run the full
  close-reconciler at session close". The work-moment surface beside it was justified at 3 of 29
  and its own comment says surfaces must have "real consumers rather than being widened
  speculatively". 1 of 287 does not clear that bar, so no scheduler was built.

  HARD RULE. Even with consumers, this surface would stay proposal-only. A scheduled job runs
  unattended and can spend money or take an outward action, and both are things Core may never do
  without Nick. Proposal is the ceiling by policy, not by immaturity — which is why the test
  below asserts it rather than leaving it to a comment.

Run: python3 bin/tests/test_trigger_shape_routing.py
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
spec = importlib.util.spec_from_file_location(
    "artifact_typer", ROOT / "scheduling" / "claude-si" / "artifact_typer.py")
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)

CADENCE = [
    ("lifecycle anchor", "always run the full close-reconciler at session close"),
    ("clock anchor",     "check the deploy status every morning"),
    ("frequency anchor", "run a brain integrity sweep nightly"),
    ("post-work anchor", "reconcile memory files after every session"),
]

# `every` as a SCOPE quantifier. These are the 15 of 16 the loose pattern got wrong, and if a
# later widening re-admits them the cadence surface fills with asks that have no clock at all.
NOT_CADENCE = [
    ("every = scope, not schedule", "audit every core to verify closes are distinguished"),
    ("every = scope, not schedule", "run a full usability test clicking through every element"),
    ("every time = reliability",    "make brain-lint work reliably every time"),
    ("automatically != scheduled",  "keep the ui in sync with underlying system changes automatically"),
]


def main() -> int:
    p = f = 0
    print("=== trigger shape decides the artifact kind ===\n")
    print("--- CADENCE → scheduled_job_proposal ---")
    for label, ask in CADENCE:
        r = at.route_type(ask, ask_type="procedure", steps=3)
        if r["type"] == "scheduled_job_proposal":
            print(f"  PASS  {label:26} → proposal (anchor {r.get('cadence')!r})")
            p += 1
        else:
            print(f"  FAIL  {label:26} → {r['type']}: {ask}")
            f += 1

    print("\n--- NOT cadence ('every' as scope) must not reach the schedule surface ---")
    for label, ask in NOT_CADENCE:
        r = at.route_type(ask, ask_type="procedure", steps=3)
        if r["type"] != "scheduled_job_proposal":
            print(f"  PASS  {label:26} → {r['type']}")
            p += 1
        else:
            print(f"  FAIL  {label:26} → wrongly routed to a schedule: {ask}")
            f += 1

    print("\n--- a cadence route is a PROPOSAL and can never carry an install action ---")
    r = at.route_type("always run the full close-reconciler at session close",
                      ask_type="procedure", steps=3)
    if r["type"].endswith("_proposal") and "install" not in r["type"]:
        print("  PASS  the type name itself encodes proposal-only")
        p += 1
    else:
        print(f"  FAIL  cadence type is {r['type']!r}")
        f += 1

    # The loop's own dispatch must handle it explicitly, not let it fall into a generic bucket:
    # both outcomes install nothing, but only an explicit branch leaves a RECORD, and a proposal
    # that is silently skipped is indistinguishable from one that was never detected.
    loop_src = (ROOT / "scheduling" / "claude-si" / "friction_loop.py").read_text()
    if 'if t == "scheduled_job_proposal"' in loop_src and "scheduled_proposals" in loop_src:
        print("  PASS  friction_loop records the proposal instead of silently skipping it")
        p += 1
    else:
        print("  FAIL  friction_loop has no explicit branch for scheduled_job_proposal")
        f += 1

    # Check for CALLS, not for the substring "install" — the branch's own comment explains why
    # it never installs, and matching that comment made this assertion fail on correct code. A
    # test that greps prose instead of behaviour is measuring the wrong thing, which is the same
    # error class as everything else found today.
    branch = loop_src.split('if t == "scheduled_job_proposal"')[1].split("continue")[0]
    # STRIP COMMENTS FIRST. Two earlier versions of this assertion failed on correct code because
    # they matched the branch's own explanatory comment — first on the word "install", then on
    # "ag.generate" in the sentence explaining why ag.generate is NOT called. Checking behaviour
    # means checking code, and a comment is not code. Same error class as everything else this
    # session turned up: the check and the claim were about different things.
    code = "\n".join(ln.split("#", 1)[0] for ln in branch.splitlines())
    calls = [c for c in ("inst.install", "ag.generate", "install_shadow_block",
                         "install_hooked_skill") if c in code]
    if not calls:
        print("  PASS  the proposal branch makes no install/generate call")
        p += 1
    else:
        print(f"  FAIL  the proposal branch calls {calls}")
        f += 1

    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
