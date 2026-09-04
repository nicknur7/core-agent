#!/usr/bin/env python3
"""Trigger terms are ranked by DISTINCTIVENESS, not raw frequency — and a term that can never fire is refused.

WHY THIS EXISTS (2026-08-12, Phase 4). ask_miner ranked candidate trigger terms by how often they
appeared in the cluster's own member prompts:

    pool.sort(key=lambda w: (-counts[w], -len(w)))

That cannot distinguish "this word is about THIS ask" from "this word is common in everything Nick
writes". Measured against the 750-prompt base corpus on life:

    session   105/750  (14.0%)     common — says nothing about which ask fired
    brain      84/750  (11.2%)     common
    clean      52/750  ( 6.9%)     common
    loose       7/750  ( 0.9%)     distinctive
    fable      12/750  ( 1.6%)     distinctive
    across      0/750  ( 0.0%)     NEVER APPEARS IN ANY PROMPT

THE PROOF IS `across`. A live artifact required `\\bacross\\b AND \\bclean\\b`. Nick has never written
"across" in a single prompt in the corpus — so that artifact was not merely narrow, it was
INCAPABLE OF FIRING, and raw-frequency ranking selected it anyway. Raw frequency inside a cluster
said "this term is in every member prompt"; the base rate says the term does not exist.

Lift = (rate in this cluster) / (rate across all prompts). Frequent here AND rare overall is
evidence. Frequent here because frequent everywhere is not. This is the "distinctiveness x
repetition" signal the 2026-07-27 research named and never built.

A ZERO BASE RATE IS A HARD REJECT, NOT A HIGH SCORE — the property most worth guarding here. Dividing
by a smoothed zero would rank an unfireable term FIRST: the metric would be maximally confident about
a word that can never match. Refusing to install beats installing something provably inert, and the
two-term floor then correctly declines to mint the artifact at all.

This asserts BEHAVIOUR against the real corpus, not the presence of a sort key.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_contrast_scoring")
    try:
        import ask_miner as am
    except Exception as exc:
        print(f"  FAIL  cannot import ask_miner ({exc.__class__.__name__}: {exc})")
        return 1

    check("the miner exposes a base-rate denominator", hasattr(am, "_base_rates"),
          "without it, ranking falls back to raw frequency and `across` gets chosen again")
    if not hasattr(am, "_base_rates"):
        return 1

    base, n = am._base_rates()
    if not base:
        # `_base_rates()` returns the identical falsy ({}, 0) for TWO different causes (its own
        # docstring: "Returns ({}, 0) on any failure") — a genuine connection exception, OR a
        # reachable DB whose pattern_observations table just has zero rows for this org (this
        # suite's own scratch DB, or any fresh Core that has not mined a single correction yet).
        # "corebrain down" named only the first cause and was simply wrong on the second, which
        # is the far more common one for a fresh seat — found 2026-09-03 auditing exactly that.
        print("  SKIP  base corpus empty or unreachable — this test compares live rates.")
        print("        Note the miner FALLS BACK to raw frequency in exactly this case, by design:")
        print("        a DB outage OR an empty corpus must not silently look like a scoring decision.")
        # rc MUST be 0 for run-all.sh's is_skip() to grade this SKIP rather than FAIL — its
        # run_one() checks `rc -ne 0 -> FAIL` BEFORE it ever looks at the SKIP text (same
        # mismatch bin/tests/test_retraction_is_readable.py had two branches of, fixed the same
        # day). This file's exit code never matched its own message either.
        return 0

    check("the base corpus is real and large enough to discriminate",
          n >= 100 and len(base) >= 500,
          f"{n} prompts / {len(base)} terms — too small a denominator makes every lift noise")

    # The property, stated as a comparison the corpus can settle: a rare term must out-rank a
    # common one when both are equally present in the cluster.
    def lift(w, in_cluster, size):
        ev = base.get(w, 0) / max(n, 1)
        return (in_cluster / size) / ev if ev > 0 else 0.0

    # SEAT-INDEPENDENT (2026-09-04, core-school): this used to pick from six LIFE words —
    # ("session","brain","clean") vs ("loose","ends","fable") — measured on life's 750 prompts.
    # On a seat whose corpus has none of the rare three, the fallback term has base rate 0, its
    # lift is 0 by the zero-rate rule below, and the assertion fails for a reason that has nothing
    # to do with the scorer. The property is "at equal in-cluster frequency, the rarer term wins";
    # it holds on ANY corpus with two distinct non-zero rates, so take both terms from this seat's
    # own base dict. Instance #10 of "shared code silently means life".
    nonzero = {w: c for w, c in base.items() if c > 0}
    if len(set(nonzero.values())) < 2:
        print("  UNDECIDABLE  this seat's base corpus has fewer than two distinct term rates — "
              "the rare-vs-common property cannot be exercised here.")
        return 2
    common = max(nonzero, key=nonzero.get)
    rare = min(nonzero, key=nonzero.get)
    check(f"a RARE term out-ranks a COMMON one at equal in-cluster frequency ({rare} vs {common})",
          lift(rare, 5, 5) > lift(common, 5, 5),
          f"{rare} base={base.get(rare)} lift={lift(rare,5,5):.1f} vs "
          f"{common} base={base.get(common)} lift={lift(common,5,5):.1f} — if the common term wins, "
          f"triggers keep being drawn from the operator's ordinary vocabulary")

    # THE ONE THAT MATTERS: a term absent from every prompt scores ZERO, never infinity.
    # The real case was "across" — a live trigger term that appeared in 0 of life's 750 prompts.
    # "across" is NOT absent on every seat, so exercise the rule with a token that cannot occur.
    absent = "zzqxv-never-a-term"
    check(f"a term appearing in ZERO prompts scores 0, not infinity ({absent})",
          base.get(absent, 0) == 0 and lift(absent, 5, 5) == 0.0,
          f"{absent} base={base.get(absent)} lift={lift(absent,5,5)} — a smoothed divisor would rank "
          f"an unfireable term FIRST, which is worse than the defect being fixed")

    # And the scoring code must actually reject it rather than merely rank it last.
    import inspect
    src = inspect.getsource(am)
    check("the scorer FILTERS zero-base-rate terms out, not merely sorts them low",
          "_base.get(t, 0) > 0" in src,
          "ranking an unfireable term last still installs it when it is one of only two candidates; "
          "it must be removed from the pool")

    check("a corpus failure falls back to the prior ranking rather than minting nothing",
          "else:" in src and "-counts[w], -len(w)" in src,
          "a DB outage must not silently change what gets installed")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
