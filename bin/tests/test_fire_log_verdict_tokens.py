#!/usr/bin/env python3
"""`block` IS NOT A NEAR-FIRE. THE COUNTER FILES IT AS ONE, SO THE HARDEST-ENFORCING CONTRACT
READS AS THE DEADEST.

AUTHORED AND RUN ON core-finance. NOT INSTALLED HERE — finance is a puller and bin/tests/ is
baseline-shared, so per the routing life decided on 2026-08-12 the source is returned on the bus and
life commits it. Intended install path: bin/tests/test_fire_log_verdict_tokens.py

WHAT THIS EXTENDS. test_fire_log_two_formats.py fixed and fenced the column bug — old-format lines
put the contract in col 2, new-format in col 1, and a HOOK NAME must never be counted as a contract.
That test is 7/7 green. It is blind to this, in the same column.

life's 2026-08-12 fix then split the overloaded column:

    _FIRE_RECORD_KINDS = frozenset({"shadow", "block"})
    ... if name in _FIRE_RECORD_KINDS: records[name] += 1   else: c[name] += 1

That is RIGHT about `shadow` and WRONG about `block`, and the difference is visible in the writer:

    learned-recallguard.py:118-125   shadow -> the recall demand matched AND a brain read had
                                     already cleared it, so the guard returns 0 and does NOT
                                     intervene. A genuine near-fire. Correctly excluded.

    learned-recallguard.py:127-140   block  -> writes "recallguard\tblock", then emits
                                       print(json.dumps({"decision": "block", "reason":
                                         "LEARNED CONTRACT - recall-first: ..."}))
                                     That is the recall-first contract FIRING AND ENFORCING. It is
                                     the contract working at full strength.

WHAT IT COSTS. measure-contract-fitness.py:425 does `fc = fires.get(short, 0)` and feeds the cascade
at :458; the comment at :262 records that the cascade reads `post == 0 and fires > 0` as GRADUATED.
A contract whose enforcement arrives only through a guard therefore reads `fires == 0` and grades
NOT-BINDING-NO-FIRE — "its trigger never matched" — when the trigger matched and blocked every time.
The harder a contract enforces through this path, the deader the instrument reports it. That is the
same shape as the original column bug: a confident, actionable, wrong zero.

Distinguish the two claims, because only the second is a live defect:
  - phantom KEY in the returned dict: life fixed it, and per-contract attribution was never affected
    (the call site is a keyed lookup, so unmatched keys are skipped). Not asserted here.
  - `block` MIS-CLASSIFIED as near-fire: asserted here. Survives the fix.

STATUS: GREEN FENCE as installed (life 2026-08-12). It was authored RED-BY-DESIGN on core-finance to
record a live defect, and it did its job: `block` was being filed as near-fire telemetry when it is a
contract ENFORCING. life fixed that in be31e67 and installs this file in the SAME push as the fix, so
it lands green and stays a regression fence.

The status line matters because run-all.sh auto-discovers bin/tests/test_*.py by glob and has no
xfail mechanism — a permanently-red row here would make the fleet's suite report NOT GREEN forever,
and a health signal with a known-acceptable failure in it is not a health signal. core-business
raised exactly that on review, reading this docstring; the docstring was stale, not the file. Left
uncorrected it would have been a shipped file lying about itself, which is the defect class this
whole effort keeps finding.
Do NOT make it green by weakening the assertion — the fix is to re-attribute `block` to the contract
the emitting guard names, keeping `shadow` in FIRE_RECORDS where life correctly put it.

Run: python3 tasks/si-verification/probes/test_fire_log_verdict_tokens.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

# Anchored to the repo root so this runs identically from the finance probe dir and from
# bin/tests/ after life installs it.
def _root() -> Path:
    p = Path(__file__).resolve()
    for cand in p.parents:
        if (cand / "scheduling" / "claude-si").is_dir() and (cand / "bin").is_dir():
            return cand
    raise SystemExit("SKIP - could not locate Core root from %s" % p)


ROOT = _root()
MCF = ROOT / "scheduling" / "claude-si" / "measure-contract-fitness.py"

# The verdict vocabulary the guards write into the contract column.
NEAR_FIRE = "shadow"      # correctly excluded: matched, but a brain read already cleared it
ENFORCED = "block"        # NOT a near-fire: matched and returned decision=block


def load(fires_log):
    """Repoint FIRES_LOG at a temp file. Same loader shape as test_fire_log_two_formats.py.

    Reused rather than reinvented (the brief says reuse the two existing templates), which also
    preserves the isolation property: nothing here writes to live .claude/state.
    """
    sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
    sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))
    spec = importlib.util.spec_from_file_location("mcf_verdict_probe", MCF)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    m.FIRES_LOG = fires_log
    return m


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== `block` is recall-first enforcing, not near-fire telemetry ===\n")

    if not MCF.is_file():
        print("  SKIP - measure-contract-fitness.py absent")
        return 0

    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "learned-fires.log"

        # KNOWN-ANSWER DOSE. The expected values below are derived by READING THESE SIX LINES as a
        # human, not by running the parser. That separation is the point: a fixture whose oracle is
        # produced by the code under test cannot falsify that code — the anti-pattern that let a
        # screenshot filter stay 16/16 green while discarding every screenshot turn.
        #
        #   3 classifier rows crediting recall-first  (new-format, old-format, and one CSV pair)
        #   1 classifier row crediting verify-dont-claim
        #   1 recallguard `shadow`  -> near-fire, correctly NOT a recall-first fire
        #   1 recallguard `block`   -> recall-first ENFORCED; this IS a recall-first fire
        #
        # Correct recall-first fire count, counted by hand:  3 + 1(block) = 4
        # Correct count for a contract literally named `shadow` or `block`: there is none. 0.
        log.write_text(
            "classifier\trecall-first\tyou should know this\n"
            "2026-08-06T18:30:25+00:00\tclassifier\trecall-first\tcheck my brain\n"
            "2026-08-09T01:07:04+00:00\tclassifier\tverify-dont-claim,recall-first\tlast session\n"
            "recallguard\tshadow\tyou should be able to see it\n"
            "recallguard\tblock\tdon't you remember\n"
            "classifier\tplan-not-execute\tstop and plan\n"
        )

        m = load(log)
        c = m.fire_counts()
        records = dict(getattr(m, "FIRE_RECORDS", {}) or {})

        # CONTROLS. If these fail, the probe is broken rather than the counter, and every result
        # below is uninterpretable.
        check("CONTROL - classifier rows still attribute correctly (verify-dont-claim = 1)",
              c.get("verify-dont-claim", 0) == 1,
              "got %d" % c.get("verify-dont-claim", 0))
        check("CONTROL - a hook name is still never a contract",
              c.get("classifier", 0) == 0 and c.get("recallguard", 0) == 0,
              "classifier=%d recallguard=%d" % (c.get("classifier", 0), c.get("recallguard", 0)))

        # life's fix, which this probe AGREES with and pins so a later change cannot undo it.
        check("`shadow` is not a contract (life's 2026-08-12 split - asserted so it stays fixed)",
              c.get(NEAR_FIRE, 0) == 0,
              "`shadow` is back in the contract tally at %d" % c.get(NEAR_FIRE, 0))
        check("`block` is not a contract name either",
              c.get(ENFORCED, 0) == 0,
              "`block` counted as a contract %dx" % c.get(ENFORCED, 0))

        # THE DEFECT.
        check("recallguard `block` is credited to recall-first (expected 4: 3 classifier + 1 block)",
              c.get("recall-first", 0) == 4,
              "recall-first counted %d, expected 4.\n"
              "          learned-recallguard.py:139 emits {\"decision\":\"block\",\"reason\":\n"
              "          \"LEARNED CONTRACT - recall-first: ...\"} - the contract ENFORCING.\n"
              "          It is filed under FIRE_RECORDS=%s instead of its own contract.\n"
              "          A contract enforcing only via this path reads fires==0 and grades\n"
              "          NOT-BINDING-NO-FIRE ('trigger never matched') while it blocked every time."
              % (c.get("recall-first", 0), records or "{}"))

        check("`shadow` is still retained as near-fire telemetry, not discarded",
              records.get(NEAR_FIRE, 0) == 1,
              "FIRE_RECORDS=%s - the near-fire denominator must survive any re-attribution fix; "
              "dropping it to tidy a total would destroy real signal (life's own rationale)."
              % (records or "{}"))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    if f:
        print("\nFAILING means the `block` re-attribution regressed: a contract that enforces only\n"
              "via the block path would read fires==0 and grade NOT-BINDING-NO-FIRE while it blocked\n"
              "every time. Re-attribute `block` to the contract its emitting guard names, and keep\n"
              "`shadow` in FIRE_RECORDS — it is the near-fire denominator, not noise.")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
