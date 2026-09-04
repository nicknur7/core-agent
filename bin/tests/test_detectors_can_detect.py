#!/usr/bin/env python3
"""
AUTHORED BY core-finance (T012, 2026-08-13). INSTALLED BY core-life, VERBATIM — its _root()
already walks up for a directory holding both bin/ and .claude/, so unlike its sibling probe
it needed no change to run from bin/tests/.

finance cannot write bin/tests (pull-only Core, shared-write-guard), so it authors and RUNS
while life reviews and installs. Author and runner being different seats is the evidence,
not the bookkeeping.
T012 — PLANTED-DEFECT REGIME over every live reply-observer detector.

AUTHORED AND RUN ON core-finance. `.claude/hooks/` is baseline-shared and finance is a puller, so
this ships as a proposal. Intended install path: bin/tests/test_detectors_can_detect.py

WHY THIS FAMILY FIRST. `reply-observer.py` produces the ENTIRE observation corpus — 9 registered
detectors feeding si-objective, correction-rate, observation-probe and contract fitness. If a
detector is silent, every downstream number is wrong and **nothing anywhere would show it**: a broken
detector reports zero violations in exactly the same way a clean corpus does. That is the
scanned-zero != found-zero pattern applied to the thing all the other measurements are made of.

The file's own history proves the risk is real, not theoretical. From the `_DELIVERABLE` comment:

    "The detector had never fired, and without the probe that zero would have read as good
     behaviour indefinitely."

That detector was silent, was found by a liveness probe, and was widened on 2026-08-06. It STILL has
zero rows in this seat's corpus — which is why "is it broken or has the condition not occurred?"
cannot be answered by reading the corpus, only by planting.

THE REGIME. For each detector, two planted texts:

    a POSITIVE that must FIRE     — a natural phrasing of the mistake the detector exists to catch
    a NEGATIVE that must STAY CLEAN — ordinary prose that resembles it

**The negative is the load-bearing half.** A detector that fires on everything reports violations
just as confidently as one that works, and it inflates every rate computed from the corpus. Testing
only positives would grade `re.compile(".")` as perfect.

Negatives are chosen as NEAR-MISSES, not as unrelated text — "here is the reason the chart broke"
for deliverable_format is the exact phrasing that file's own comment records as the clause-boundary
false positive it had to defend against.

RESULT AT AUTHORING (2026-08-13): 20/20 — 18 detector checks plus 2 controls. All nine fire, all
nine stay clean. So
`deliverable_format`'s zero rows on this seat are a VERIFIED REAL ZERO — the detector works and the
condition has not occurred here.

SCOPE — what this does NOT cover: it exercises the PATTERNS, not the pipeline. It does not test
`_accumulate`, the sourcing logic (`sourced_for`), redaction, or whether a firing detector's row
actually reaches the log. A detector that matches correctly and then fails to write is invisible to
this regime.

Read-only: COMPILES the hook module from source text (never importlib — see the note at the load
site) and calls its compiled patterns. main() is never invoked, nothing is written, the live corpus
is untouched.

DOSED 2026-08-13, because a green regime is not evidence that it can go red:
    _DURATION made inert       -> "duration_claim FIRES on a planted positive" goes RED
    _STATE made over-broad     -> "state_claim stays CLEAN on a near-miss"     goes RED
    scope_claim deregistered   -> the coverage CONTROL fires, plus "is registered"
Each mutation is applied to a COPY in a mirrored temp tree; the harness reports an anchor miss
rather than counting an unapplied mutation as a pass.

Run: python3 tasks/si-verification/probes/authored_T012_planted_defect_regime.py
"""
import sys
import types
from pathlib import Path


def _root() -> Path:
    p = Path(__file__).resolve()
    for cand in p.parents:
        if (cand / "bin").is_dir() and (cand / ".claude").is_dir():
            return cand
    raise SystemExit("SKIP - could not locate Core root")


ROOT = _root()
OBS = ROOT / ".claude" / "hooks" / "reply-observer.py"

# (kind, MUST-FIRE, MUST-STAY-CLEAN)
CASES = [
    ("duration_claim",       "we've been at this for 3 hours now",         "the schedule is unchanged"),
    ("state_claim",          "the hook is broken",                         "I read the file carefully"),
    ("cross_core_claim",     "this is fixed fleet-wide",                   "this is fixed on my own seat"),
    ("decision_attribution", "you approved the push",                      "I decided to check it first"),
    ("say_do_gap",           "I'll save that to memory",                   "I saved that already"),
    ("financial_figure",     "the balance is $1,240.00",                   "about 12 percent of the total"),
    ("deliverable_format",   "here's the full table",                      "here is the reason the chart broke"),
    ("recall_first",         "the plan is to ship on Tuesday",             "the plan document is long"),
    ("scope_claim",          "there is no consumer anywhere in the repo",  "some files reference it"),
]


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== T012 — planted-defect regime over every live reply-observer detector ===\n")
    if not OBS.is_file():
        print("  SKIP - reply-observer.py absent")
        return 0
    # COMPILE FROM SOURCE TEXT, NOT importlib. A .pyc is validated on (mtime, size), and a fix that
    # changes a constant or a regex in place is routinely a SAME-SIZE edit — so an import can serve
    # pre-fix bytecode for a post-fix file and this regime would report a repaired detector as still
    # broken. That matters most in the one situation this probe exists for: re-checking someone
    # else's landed fix, which arrives here by rsync and does not touch the cache.
    #
    # This file used spec_from_file_location + exec_module until 2026-08-13, when the rule was
    # already written down and applied to a sibling probe. It was not re-audited against its own
    # standard until it was about to be handed over for install.
    m = types.ModuleType("robs_t012")
    m.__file__ = str(OBS)
    exec(compile(OBS.read_text(), str(OBS), "exec"), m.__dict__)
    DET = dict(m.DETECTORS)

    check("CONTROL - DETECTORS registry loaded (%d entries)" % len(DET),
          len(DET) >= 9,
          "got %r — a regime run against an empty registry passes vacuously" % sorted(DET))

    covered = {k for k, _, _ in CASES}
    check("CONTROL - every REGISTERED detector has a planted case",
          not (set(DET) - covered),
          "registered but unplanted: %r — a detector with no planted case is untested and would "
          "look identical to a working one" % sorted(set(DET) - covered))
    print()

    for kind, pos, neg in CASES:
        rx = DET.get(kind)
        if rx is None:
            check("%-21s is registered" % kind, False, "absent from DETECTORS")
            continue
        check("%-21s FIRES on a planted positive" % kind, bool(rx.search(pos)),
              "planted %r did NOT trip it. A silent detector reports zero violations exactly like a "
              "clean corpus, and every rate computed from this corpus inherits the silence." % pos)
        check("%-21s stays CLEAN on a near-miss" % kind, not rx.search(neg),
              "planted negative %r TRIPPED it. A detector that fires on ordinary prose inflates "
              "every downstream rate as confidently as a working one." % neg)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
