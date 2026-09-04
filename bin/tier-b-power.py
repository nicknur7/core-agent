#!/usr/bin/env python3
"""CAN TIER B'S PAIRED RUN DECIDE ANYTHING, AND AT WHAT COST — computed, not recited.

`bin/gate_tier_b.py` carried "NOT ESTABLISHED: that this tier can DECIDE anything" and the open item
was to settle it by spending ~$8 on a live paired run. **That run cannot produce information, and
this file is why** — the answer is arithmetic over the gate's own decision rule, so it costs nothing.

THE FLOOR, WHICH BINDS BEFORE ANY CANDIDATE EFFECT. `verdict()` refuses unless BOTH arms yield at
least `min_ran` scoreable trials; fewer is UNDECIDABLE, which returns REVERT. With a per-trial
observe rate q, an arm reaches the floor with P(Binom(n, q) >= min_ran), and the run needs BOTH.
At the 2-trial floor that is 0.2%-6.2% across every plausible q. **So the cheapest run is at least
94% guaranteed undecidable before the candidate is considered at all**, and a REVERT produced that
way carries no information about the candidate — it is the absence of a measurement wearing a
verdict's clothes.

WHY A RANGE OF q AND NOT ONE NUMBER. gate_tier_b.py states "an arm observes with p=0.20", and its
own evidence disagrees: 10 pairs = 20 arms, of which 6 neither / 2 both / 2 exactly-one gives 6
observing arms of 20 = 0.30. Its "a pair differs by chance 32%" implies 2p(1-p)=0.32, i.e. p=0.20;
at q=0.30 that figure is 0.42. The constant and the data it was drawn from cannot both be right, so
this sweeps q instead of inheriting either. The conclusion does not depend on resolving it — every
column says the same thing at 2 trials.

WHAT IT WOULD TAKE. Reliable decidability needs 10-20 trials per arm (~$40-$80, 2-4h). Against even
a huge effect (pass rate 0.10 -> 0.90) power is 71.5% at 10 trials and 98.5% at 20, at the q=0.30
this section quotes. A realistic effect needs more. That is the number to put in front of Nick — not
$8.

    CORRECTED 2026-08-12, found by core-finance dosing this file. The sentence above used to read
    "reaches ~86% only at 20 trials" while naming q=0.30 — but 86% is the q=0.20 figure (85.7%
    simulated, 86.6% floor). Its own table twelve lines down printed 98.5% for q=0.30 at 20 trials.
    A header that misquotes the output directly beneath it by 12.7 points, in the direction that
    makes the cancelled experiment look weaker than it was. Everything COMPUTED here was and is
    correct — finance dosed it with four closed-form known answers, including the non-obvious null
    P(false PASS) = (1 - C(20,10)/4^10)/2 = 0.41190 at r_base=r_cand=0.5, n=10, q=1, which this
    returns exactly (a simulator that counted ties as wins would return 0.5). The defect was only
    ever in the prose, which is the part a reader reads.

    THE CLEANER STATEMENT OF THIS FILE'S OWN CASE, which it did not make: at n=20, power and the
    both-arms-scoreable floor are the same number at every q — 85.7/86.6, 98.4/98.5, 99.8/99.9,
    100.0/100.0. Against an effect this large power IS decidability; the only binding constraint is
    whether both arms become scoreable at all. So no trial count buys power without also buying the
    false-PASS problem, and the conclusion still rests where it did — on the false-PASS arm, 31%
    from pure noise at 10 trials.

Run: python3 bin/tier-b-power.py [--runs N]
"""
import importlib.util
import random
import sys
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core_seat import seat_root  # noqa: E402

ROOT = seat_root()
COST_PER_TRIAL = 2.00      # measured per arm, gate_tier_b.py header
SECS_PER_TRIAL = 390       # same source

QS = (0.20, 0.30, 0.40, 0.50)
TRIALS = (2, 3, 5, 10, 20, 40)


def load_gate():
    """The REAL verdict(), so the simulation is scored by the shipped decision rule."""
    p = ROOT / "bin" / "gate_tier_b.py"
    spec = importlib.util.spec_from_file_location("gate_tier_b_probe", str(p))
    m = importlib.util.module_from_spec(spec)
    sys.modules["gate_tier_b_probe"] = m
    spec.loader.exec_module(m)
    return m


def p_reaches(n, k, q):
    return sum(comb(n, i) * q ** i * (1 - q) ** (n - i) for i in range(k, n + 1))


def simulate(verdict, trials, q, r_base, r_cand, runs, seed=20260810):
    """P(verdict says PASS). r = P(pass | scoreable); q = P(scoreable)."""
    rng = random.Random(seed)
    hits = 0
    for _ in range(runs):
        bp = br = cp = cr = 0
        for _ in range(trials):
            if rng.random() < q:
                br += 1
                if rng.random() < r_base:
                    bp += 1
            if rng.random() < q:
                cr += 1
                if rng.random() < r_cand:
                    cp += 1
        ok, _ = verdict([{"id": "T", "trials": trials, "base_pass": bp, "base_ran": br,
                          "cand_pass": cp, "cand_ran": cr, "errors": []}])
        hits += bool(ok)
    return hits / runs


def main() -> int:
    runs = 20000
    if "--runs" in sys.argv:
        runs = int(sys.argv[sys.argv.index("--runs") + 1])
    g = load_gate()

    print("=== Tier B: can the paired run decide anything? ===\n")
    print("  scored by the SHIPPED verdict(), %d simulated runs per cell\n" % runs)

    print("  P(BOTH arms reach min_ran=2) — the floor, before any candidate effect")
    print("    %-7s %s" % ("trials", "  ".join("q=%.2f" % q for q in QS)))
    for n in TRIALS:
        row = "  ".join("%5.1f%%" % (100 * p_reaches(n, 2, q) ** 2) for q in QS)
        print("    %-7d %s   ~$%-4d ~%.1fh" % (n, row, n * 2 * COST_PER_TRIAL,
                                               n * 2 * SECS_PER_TRIAL / 3600))

    print("\n  FALSE PASS under the null (r_base == r_cand) — noise declaring an improvement")
    for r in (0.25, 0.50):
        row = "  ".join("%5.2f%%" % (100 * simulate(g.verdict, n, 0.30, r, r, runs)) for n in (2, 3, 5, 10))
        print("    r=%.2f  trials 2/3/5/10: %s" % (r, row))

    print("\n  POWER against a LARGE real effect (0.10 -> 0.90), q=0.30")
    for n in (2, 3, 5, 10, 20):
        print("    trials=%-3d PASS %5.1f%%   ~$%-4d" % (n, 100 * simulate(g.verdict, n, 0.30, 0.10, 0.90, runs),
                                                        n * 2 * COST_PER_TRIAL))

    floor2 = max(p_reaches(2, 2, q) ** 2 for q in QS)
    fp10 = simulate(g.verdict, 10, 0.30, 0.50, 0.50, runs)
    print("\n=== VERDICT: THERE IS NO GOOD OPERATING POINT ===")
    print("  This is the answer to the open item, and it is stronger than 'unproven'.")
    print()
    print("  TOO FEW TRIALS -> no information. At 2 trials the run is at least %.1f%% certain to be"
          % (100 * (1 - floor2)))
    print("  UNDECIDABLE, for every q above. A REVERT produced that way says nothing about the")
    print("  candidate; spending $8 to learn nothing is worse than not spending it, because the")
    print("  REVERT looks like a result.")
    print()
    print("  ENOUGH TRIALS -> too much noise. At 10 trials the run is decidable ~72% of the time")
    print("  (q=0.30) but declares an improvement FROM PURE NOISE %.0f%% of the time. Raising the"
          % (100 * fp10))
    print("  trial count buys decidability and false positives together, because `verdict()` passes")
    print("  on ANY margin — `if c > b` — with no test that the margin exceeds chance.")
    print()
    print("  So the blocker is not the spend. It is that the decision rule cannot separate signal")
    print("  from variance at any cost, and a bigger budget makes the false-PASS rate worse rather")
    print("  than better. Fixing it means replacing `c > b` with a test that accounts for n --")
    print("  a design change to gate_tier_b.py, not an experiment. That is the thing to put in")
    print("  front of the operator, instead of a $8 run that was never going to decide anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
