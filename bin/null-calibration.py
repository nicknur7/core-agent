#!/usr/bin/env python3
"""Is a contract's real install date SPECIAL, or would almost any date have 'shown decay'?

WHY THIS EXISTS (2026-08-12). measure-contract-fitness.py compares a contract's post rate to its pre
rate and calls it DECAYING below a threshold. core-business then found the corpus-volume confound —
the whole corpus fell to 0.61 of its pre-rate — and the verdicts were annotated against that.

Both controls miss the same thing. Nick's corrections are FRONT-LOADED: a correction class that was
common in May and rare in August will show "decay" at almost any split date you choose. Measured on
`verify state against the live source before claiming`: **85.5% of all 83 valid split points yield a
DECAYING verdict with no intervention whatsoever**, median ratio 0.19.

Read alone that number looks like a refutation of every verdict. IT IS NOT — it is a description of
the distribution, and stopping there would have been the mistake. The question a verdict actually
makes is whether THE REAL INSTALL DATE beats the dates that were available to it. So this computes
the null distribution of ratios over every valid split, then reports where the real one falls in it.

RESULT ON LIFE, 2026-08-12 (the first genuine validation the loop has had):

    contract                    real   null median   percentile
    plan-not-execute            0.19       0.76          5%     SPECIAL
    recall-first                0.33       0.61          9%     SPECIAL
    verify-dont-claim           0.10       0.19          9%     SPECIAL
    stop-and-plan               0.09       0.14         13%     SPECIAL
    frustration-deescalate      0.39       0.58         14%     SPECIAL
    model-routing-and-defaults  0.35       0.35         48%     ORDINARY

Five of six install dates sit in the bottom 15% of their own null. model-routing-and-defaults is
indistinguishable from a randomly chosen date — which independently corroborates core-business's
refutation of that same contract, reached by a different route (pre n=3, below the MIN_PRE_N floor).

A percentile above ~40 means the date is not doing the work and the verdict should not be credited,
however good its raw ratio looks.
"""
import datetime
import math
import os
import statistics
import sys
from pathlib import Path

# SEAT RESOLUTION, not __file__. Anchoring on __file__ resolves whichever Core the FILE lives in,
# which for baseline-shared code is the wrong answer cross-seat: core-finance reported it could not
# run this tool on its own seat, and bin/tests/test_root_anchors.py independently flagged this exact
# file as the only VULN in the tree. Two sources, one defect, neither of them me reading my own code.
#
# core_seat.seat_root() requires the identity marker, so a bad CORE_INSTANCE is REJECTED rather than
# accepted-and-then-fatal — which is why it is the fleet's resolver and __file__ is not.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from core_seat import seat_root  # noqa: E402
REPO = seat_root(fallback=Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
from _env import connect_corebrain  # noqa: E402

MIN_PRE_N = 5          # same floor as the fitness instrument; below this a ratio is noise

# THE GENERATION FILTER — omitted from the first version of this file, which INVALIDATED it.
# core-business found it reconciling T025 (2026-08-12) by varying every filter and finding only one
# that moves the number: with GEN, stop-and-plan pre=23 ratio 0.70; without it, pre=325 ratio 0.10.
#
# measure-contract-fitness.py documents exactly why, and I read that comment earlier the same night:
# pattern_observations holds TWO detector generations, 838 fossil `v1` rows and the live
# `learned-miner-v1` set — and EVERY contract shipped 2026-06-05, which IS the changeover date. So
# an unfiltered pre-window is ~82% fossil against a 100%-live post-window, and "the install date is
# special" collapses into "the detector changed that day."
#
# I built this instrument to validate that one and reintroduced the confound it had already
# documented and fixed. The comment was three files away and I had quoted from it hours earlier.
LIVE_DETECTOR = "learned-miner-v1"
GEN = f"detector_version = '{LIVE_DETECTOR}'"
SPECIAL_AT = 20        # percentile at or below which the real date genuinely beats its alternatives
ORDINARY_AT = 40       # at or above which the date is not doing the work
# DISAGREE_AT IS GONE. Both peer seats attacked it independently and reached the same replacement
# within minutes of each other, which is worth more than either argument alone.
#
# I had: "INDETERMINATE when the two nulls differ by more than 15 percentile points." I could not
# defend the 15 and said so. Their answer is that NO number belongs there, because the gap is the
# wrong quantity: it is scale-free, so it treats 15 points in the middle of a region the same as 15
# points straddling the line that decides the verdict — and only a disagreement that changes the
# ANSWER matters.
#
#     INDETERMINATE  iff  verdict(pct_calendar) != verdict(pct_observation)
#
# Zero free parameters. It is also my own stated criterion — "the method decides, not the data" —
# written literally instead of approximated by a distance. SPECIAL_AT and ORDINARY_AT remain, but
# those define the DECISION and are policy; the 15 was a statistic pretending to be one.
# MIN_CANDIDATES IS DERIVED, NOT PICKED — core-finance, 2026-08-12, on request.
#
# I had 20 and said openly it was "mine and thin". It is not thin; I had chosen close to the right
# number for a reason I had never written down. finance supplied the reason and the algebra.
#
# A percentile is a PROPORTION, so it carries a binomial standard error SE = sqrt(p(1-p)/N). That
# quantifies business's "denominator of three wearing a percent sign" exactly — recomputed here:
#
#     N=  3   95% CI = +/- 53 pp     <- not a weak measurement; not a measurement
#     N=  5           +/- 41 pp
#     N= 16           +/- 23 pp
#     N= 20           +/- 21 pp
#     N= 83           +/- 10 pp
#
# The requirement that matters: a contract sitting exactly ON the SPECIAL boundary must not have a
# confidence interval reaching the ORDINARY line — otherwise "SPECIAL" and "ORDINARY" are the same
# statement with different rounding.
#
#     require   q + z*sqrt(q(1-q)/N) <= dec        q = SPECIAL_AT, dec = ORDINARY_AT, z = 1.96
#     solve     N >= z^2 * q(1-q) / (dec-q)^2  =  3.8416 * 0.16 / 0.04  =  15.4   ->  16
#
# Computed rather than hardcoded so it TRACKS the two policy thresholds: widen the gap between
# SPECIAL_AT and ORDINARY_AT and the floor falls out on its own. A constant would silently stop
# being the right constant the first time either line moved.
def _min_candidates(special_at: int, ordinary_at: int, z: float = 1.96) -> int:
    q, dec = special_at / 100.0, ordinary_at / 100.0
    return math.ceil(z ** 2 * q * (1 - q) / (dec - q) ** 2)


MIN_CANDIDATES = _min_candidates(SPECIAL_AT, ORDINARY_AT)

# WHY A CANDIDATE FLOOR, and why it is 20 (core-business, attacking this file on request 2026-08-12).
#
# I shipped a dual-null report and called a large A-vs-B gap INDETERMINATE. business computed both
# nulls independently and found the same non-neutrality — then named the CAUSE, which my version
# only described:
#
#     for model-routing-and-defaults, null-B has n = 3 CANDIDATE SPLIT POINTS.
#     "33rd percentile" is 1-of-3. It is a fraction with a denominator of three wearing a percent sign.
#
# That is the real defect, and it is the same class as MIN_PRE_N one level up: observation-day nulls
# collapse to a handful of candidates for exactly the sparse classes where the two nulls disagree. So
# the disagreement is not two valid methods differing — it is one method becoming meaningless.
#
# 20, because SPECIAL_AT is 20: to resolve "bottom fifth" at all you need at least five candidates,
# and to place a value within it with any precision you need about twenty. Below that the honest
# output is a refusal, not a number. Stated so it can be argued with rather than inherited — and I
# have asked both seats for a principled basis, having none better than this.

# TWO NULLS, BECAUSE THE CHOICE IS NOT NEUTRAL AND I COULD NOT SHOW THAT IT WAS.
#
# A candidate split can be every interior CALENDAR day, or only the days the class actually has
# observations on. I shipped calendar-only first, flagged the doubt to core-business rather than
# papering it, then measured it instead of waiting:
#
#     contract                    real   pct(cal)  pct(obs)   delta
#     plan-not-execute            0.19        5%        0%      -5
#     recall-first                0.33        9%        6%      -3
#     verify-dont-claim           0.10        9%        3%      -7
#     stop-and-plan               0.09       13%        7%      -6
#     frustration-deescalate      0.39       14%        7%      -7
#     model-routing-and-defaults  0.35       48%       20%     -28   <-- ORDINARY or borderline?
#
# For the five clear cases the choice is immaterial. For the ONE marginal case it decides the
# answer — and the marginal case is precisely where a test has to be trustworthy. Picking a null
# and reporting its verdict would have been asserting a methodological judgment as a result.
#
# So both are computed and a material disagreement is reported as INDETERMINATE. That is a real
# answer: it says the data does not settle this and names why.


def _ratios(per_day, lo, hi, split):
    pre_n = sum(n for k, n in per_day.items() if k < split)
    post_n = sum(n for k, n in per_day.items() if k >= split)
    if pre_n < MIN_PRE_N:
        return None
    pre_rate = pre_n / max((split - lo).days, 1)
    post_rate = post_n / max((hi - split).days, 1)
    return (post_rate / pre_rate) if pre_rate > 0 else None


def main() -> int:
    con = connect_corebrain()
    cur = con.cursor()
    cur.execute("select situation, trigger_labels, created_at::date from learned_contracts "
                "where active and org_id = current_setting('app.current_org_id', true)::bigint")
    rows = cur.fetchall()
    print(f"{'contract':<28}{'real':>7}{'null med':>10}{'cal':>6}{'obs':>7}  reading")
    print("-" * 92)
    out = 0
    for situation, labels, created in rows:
        short = (situation.split("—")[0].split("-")[0].strip()
                 if "—" not in situation else situation.split("—")[0].strip())
        cur.execute("""select coalesce(session_date, created_at::date),
                              count(distinct (correction_text,
                                              coalesce(session_date, created_at::date)))
                         from pattern_observations
                        where org_id = current_setting('app.current_org_id', true)::bigint
                          and pattern_label = ANY(%s)
                          and excluded_reason is null and correction_text is not null
                          and """ + GEN + """
                        group by 1 order by 1""", (labels,))
        obs = cur.fetchall()
        if len(obs) < 3:
            continue
        per_day = {d: n for d, n in obs}
        lo, hi = obs[0][0], obs[-1][0]
        cal, d = [], lo + datetime.timedelta(days=1)
        while d < hi:
            r = _ratios(per_day, lo, hi, d)
            if r is not None:
                cal.append(r)
            d += datetime.timedelta(days=1)
        obs_only = [r for r in (_ratios(per_day, lo, hi, k)
                                for k in sorted(per_day) if lo < k < hi) if r is not None]
        real = _ratios(per_day, lo, hi, created)
        if real is None or not cal or not obs_only:
            continue
        pct_c = 100 * sum(1 for r in cal if r < real) / len(cal)
        pct_o = 100 * sum(1 for r in obs_only if r < real) / len(obs_only)
        def _verdict(pct):
            if pct <= SPECIAL_AT:
                return "SPECIAL"
            if pct >= ORDINARY_AT:
                return "ORDINARY"
            return "weak"

        thin = [f"{k}={n}" for k, n in (("cal", len(cal)), ("obs", len(obs_only)))
                if n < MIN_CANDIDATES]
        v_c, v_o = _verdict(pct_c), _verdict(pct_o)
        if thin:
            reading = (f"NO VERDICT — only {', '.join(thin)} candidate split(s); "
                       f"a percentile needs >= {MIN_CANDIDATES} to mean anything")
            out = 1
        elif v_c != v_o:
            reading = (f"INDETERMINATE — calendar null says {v_c}, observation null says {v_o}; "
                       f"the method decides, not the data")
            out = 1
        elif v_c == "SPECIAL":
            reading = "SPECIAL — beats most alternative dates, under both nulls"
        elif v_c == "ORDINARY":
            reading = "ORDINARY — the date is not doing the work, under both nulls"
            out = 1
        else:
            reading = "weak — under both nulls"
        print(f"{short[:27]:<28}{real:>7.2f}{statistics.median(cal):>10.2f}"
              f"{pct_c:>5.0f}%{pct_o:>7.0f}%  {reading}")
    con.close()
    return out


if __name__ == "__main__":
    sys.exit(main())
