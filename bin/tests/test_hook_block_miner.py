#!/usr/bin/env python3
"""hook_block_miner.py HAD ZERO TEST COVERAGE — zero grep hits across the whole suite.

AUTHORED AND RUN ON core-finance. NOT INSTALLED HERE — finance is a puller and bin/tests/ is
baseline-shared, so per the routing life decided 2026-08-12 the source is returned on the bus and
life commits it. Intended install path: bin/tests/test_hook_block_miner.py

This closes HALF of T014. The other named organ, `measure-existing-hooks.py`, does not exist on
finance — a fleet-wide find puts it only on core-life — so that half is unreachable from this seat
and is reported rather than faked.

WHAT IS BEING PROTECTED, and why each assertion is here rather than being a tidy round number.

1. DEDUPE. `_rows()` documents its own prior failure: the log writes TWO rows per block (an excerpt
   row and a message row) and counting rows instead of distinct (hook, timestamp) pairs "inflated
   every figure — I reported '13 blocks this session' when the real count was 6." The fixture below
   therefore writes two of its time-claim-gate blocks as duplicate pairs: 8 physical log lines that
   must tally as 6 blocks. A regression here does not crash, it inflates — the failure mode that
   already happened once.

2. THE SECURITY TIER. `SECURITY_TIER = {pretooluse-guard, approval-gate}` is a POLICY boundary, not
   a tuning parameter. The module's own restraint clause: pretooluse-guard's blocks are "144 stopped
   outward actions", and approval-gate "enforces Nick's authority over the loop itself — a loop
   narrowing it edits its own permission to act." A block count cannot distinguish "the behaviour
   was learned" from "the boundary moved," so these two must never reach a tuning case. This is the
   assertion with real consequences: silently dropping a name from that set would let the loop
   propose narrowing its own permission gate, and nothing else in the suite checks it.

3. MEASURE vs ACT — the distinction that makes the design honest. `report()` COUNTS the security
   tier and surfaces it under `security_tier_excluded`; only `cases()` excludes it. That is correct:
   the measurement stays complete and visible, the actuator is restrained. Both halves are asserted,
   because "excludes entirely" read loosely would justify dropping them from the count too, which
   would hide 144 blocks from the one report that can see them.

4. THRESHOLDS. MIN_BLOCKS=5 / MIN_SESSIONS=3 keep a one-off out of the tuning queue.

DOSE RESULT when authored (2026-08-12, core-finance): 7 PASS / 0 FAIL, then mutation-verified —
removing `pretooluse-guard` from SECURITY_TIER makes it appear as a tuning case, so the assertions
discriminate rather than passing vacuously. An all-green run that has never been watched go red is
not evidence.

Run: python3 tasks/si-verification/probes/test_hook_block_miner.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path


def _root() -> Path:
    p = Path(__file__).resolve()
    for cand in p.parents:
        if (cand / "scheduling" / "claude-si").is_dir() and (cand / "bin").is_dir():
            return cand
    raise SystemExit("SKIP - could not locate Core root")


ROOT = _root()
HBM = ROOT / "scheduling" / "claude-si" / "hook_block_miner.py"


def load(log_path):
    sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
    sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))
    spec = importlib.util.spec_from_file_location("hbm_probe", HBM)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    m.HOOK_EVENTS = log_path
    return m


def _fixture(path: Path):
    """Hand-built log with a KNOWN answer, derived from the DOCUMENTED rules, not from the counter.

        time-claim-gate   6 blocks / 3 sessions, two written as duplicate pairs -> 8 lines, 6 blocks
        pretooluse-guard  7 blocks / 4 sessions -> SECURITY_TIER
        approval-gate     6 blocks / 3 sessions -> SECURITY_TIER
        recall-gate       3 blocks / 2 sessions -> under MIN_BLOCKS
        one verdict=inject row                  -> not a block at all
    """
    lines = []

    def blk(hook, day, hh, sess, dup=False):
        ts = f"2026-08-{day:02d}T{hh:02d}:00:00.000Z"
        lines.append(f"{ts} | hook={hook} | event=Stop | verdict=block | session={sess} | excerpt=x")
        if dup:
            lines.append(f"{ts} | hook={hook} | event=Stop | verdict=block | session={sess} | detail=y")

    for i, (d, h, s) in enumerate([(10, 1, "s1"), (10, 2, "s1"), (11, 3, "s2"),
                                   (11, 4, "s2"), (12, 5, "s3"), (12, 6, "s3")]):
        blk("time-claim-gate", d, h, s, dup=(i < 2))
    for d, h, s in [(10, 1, "a"), (10, 2, "a"), (11, 3, "b"), (11, 4, "c"),
                    (12, 5, "d"), (12, 6, "d"), (12, 7, "d")]:
        blk("pretooluse-guard", d, h, s)
    for d, h, s in [(10, 1, "p"), (10, 2, "p"), (11, 3, "q"), (11, 4, "q"), (12, 5, "r"), (12, 6, "r")]:
        blk("approval-gate", d, h, s)
    for d, h, s in [(10, 1, "z1"), (11, 2, "z2"), (12, 3, "z2")]:
        blk("recall-gate", d, h, s)
    lines.append("2026-08-12T09:00:00.000Z | hook=time-claim-gate | event=Stop | verdict=inject "
                 "| session=s9 | excerpt=x")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== hook_block_miner: dedupe, thresholds, and the security-tier boundary ===\n")
    if not HBM.is_file():
        print("  SKIP - hook_block_miner.py absent")
        return 0

    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "hook-events.log"
        _fixture(log)
        m = load(log)
        # days is large so the fixture's fixed dates never age out of the window and turn this
        # test into a calendar bomb that starts SKIPPING silently in a month.
        r = m.report(days=3650)
        case_hooks = {c.get("hook") for c in m.cases(days=3650)}

        check("dedupe: 8 log rows for time-claim-gate tally as 6 blocks",
              r["by_hook"].get("time-claim-gate") == 6,
              "got %s — the excerpt row and message row are ONE block" % r["by_hook"].get("time-claim-gate"))
        check("a non-block verdict is not counted",
              r["total_blocks"] == 22, "total_blocks=%s expected 22" % r["total_blocks"])
        check("report() SURFACES the security tier rather than hiding it",
              r["security_tier_excluded"] == {"pretooluse-guard": 7, "approval-gate": 6},
              "got %s — measurement must stay complete even where the actuator is restrained"
              % r["security_tier_excluded"])
        check("cases() EXCLUDES pretooluse-guard",
              "pretooluse-guard" not in case_hooks,
              "a tuning case for a security gate would let the loop narrow 144 stopped outward "
              "actions: %s" % sorted(case_hooks))
        check("cases() EXCLUDES approval-gate",
              "approval-gate" not in case_hooks,
              "approval-gate enforces Nick's authority over the loop; a case here is the loop "
              "editing its own permission to act: %s" % sorted(case_hooks))
        check("cases() includes time-claim-gate (6 blocks / 3 sessions clears the bar)",
              "time-claim-gate" in case_hooks, sorted(case_hooks))
        check("cases() excludes recall-gate (3 blocks < MIN_BLOCKS=5)",
              "recall-gate" not in case_hooks, sorted(case_hooks))

        # THE CONTROL THAT MAKES THE GREEN MEAN SOMETHING. Without it, assertions 4 and 5 would
        # also pass against a cases() that returned nothing at all, for any reason.
        print("\n--- mutation control: the security-tier assertions must be able to FAIL ---")
        m.SECURITY_TIER = frozenset({"approval-gate"})
        mutated = {c.get("hook") for c in m.cases(days=3650)}
        check("dropping pretooluse-guard from SECURITY_TIER makes it appear as a tuning case",
              "pretooluse-guard" in mutated,
              "the exclusion assertions pass vacuously — cases() returned %s under mutation, so "
              "they are not actually testing the boundary" % sorted(mutated))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
