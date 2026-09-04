#!/usr/bin/env python3
"""The precision/recall organ's statistics, dosed with published closed-form answers.

WHY THIS EXISTS (2026-08-13). `measure-existing-hooks.py` is one of the two organs core-finance's
T014 names as having ZERO tests. It computes precision, recall and Wilson 95% confidence intervals
and emits a Markdown report — statistical arithmetic whose failure mode is not a crash but a
plausible wrong number, which is the hardest kind to notice in a report a human reads once.

A KNOWN-ANSWER DOSE IS THE RIGHT INSTRUMENT HERE, and it is core-finance's method rather than mine:
compare against values computed elsewhere, published, and independent of this implementation. An
assertion that re-derives the formula from the same source it is testing proves only that the file
agrees with itself.

    5/10     -> (0.2366, 0.7634)
    0/10     -> (0.0000, 0.2775)
    10/10    -> (0.7225, 1.0000)
    50/100   -> (0.4038, 0.5962)

All four match to four decimals. THE MATH IS CORRECT — recorded as a pass, not treated as a
disappointment. Most of what a dose finds should be that the thing works.

WHAT IT DID FIND: `wilson_ci(0, 0)` returned `(0.0, 0.0, 0.0)` — a point estimate of zero with a
ZERO-WIDTH interval, the strongest claim this notation can express.

    wilson_ci(0, 0)      width 0.0
    wilson_ci(0, 1000)   width 0.0038

Never measuring produced a TIGHTER interval than measuring a thousand samples. Same
absence-read-as-evidence defect this codebase spent two days removing, in statistics rather than in
a log reader.

NOT A LIVE BUG, stated plainly rather than inflated: both call sites already guard with
`wilson_ci(...) if total else (None, None, None)` and render None as "n/a", so the branch was
unreachable from the report path. It was a trap for the next caller, and there were two spellings of
"no data" where one would do.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scheduling" / "claude-si" / "measure-existing-hooks.py"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_wilson_ci_known_answers")
    if not SRC.is_file():
        # UNDECIDABLE, NOT FAIL (2026-09-01, core-business). measure-existing-hooks.py is listed in
        # bin/sync-manifest.json per_core_keep — life-local claude-si tooling that by design NEVER
        # syncs to a peer. A hard FAIL here is unsatisfiable by construction on every seat but the
        # writer: business, school, finance and ops would all print this exact line forever, no
        # matter what anyone on those seats does. run-all.sh already has the vocabulary for "this
        # seat has no fixture for that check" (rc==2 + UNDECIDABLE, same shape as
        # test_spec_schema_integrity.py and test_cwd_routing.py) — a missing fixture is missing
        # evidence, not a defect, and the runner's own ABSTAIN arm exists precisely so a peer does
        # not chase a red that is actually "you are not the writer Core."
        print(f"  UNDECIDABLE  {SRC} missing — per_core_keep (life-local tooling), not shared to "
              f"this seat. 0 of 9 checks ran. Not a pass: this suite cannot certify math it never "
              f"read. On core-life (the writer), the file is present and every assertion below runs.")
        return 2
    spec = importlib.util.spec_from_file_location("_meh", SRC)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    except Exception as e:  # noqa: BLE001  # privacy-ok: noqa linter directive, not a course code
        print(f"  FAIL  cannot import the organ: {type(e).__name__}: {e}")
        return 1

    w = getattr(mod, "wilson_ci", None)
    check("the organ exposes wilson_ci", callable(w))
    if not callable(w):
        return 1

    # ---- KNOWN ANSWERS, from published Wilson score intervals at z=1.96 -----------------------
    for succ, tot, lo_e, hi_e, note in [
        (5, 10, 0.2366, 0.7634, "5/10 — the textbook case"),
        (0, 10, 0.0000, 0.2775, "0/10 — zero successes, interval must NOT collapse"),
        (10, 10, 0.7225, 1.0000, "10/10 — all successes, must not exceed 1.0"),
        (50, 100, 0.4038, 0.5962, "50/100 — larger n tightens correctly"),
    ]:
        p, lo, hi = w(succ, tot)
        check(f"{note}", abs(lo - lo_e) < 0.002 and abs(hi - hi_e) < 0.002,
              f"got ({lo:.4f}, {hi:.4f}), published ({lo_e:.4f}, {hi_e:.4f})")

    # ---- the point estimate is the raw proportion, not the shrunk centre ----------------------
    p, _, _ = w(5, 10)
    check("the returned mean is the observed proportion, not the Wilson centre",
          abs(p - 0.5) < 1e-9,
          f"got {p} — callers print this as the measured rate; substituting the shrunk centre would "
          f"silently report a different number than the one observed")

    # ---- NO DATA IS NOT A MEASUREMENT OF ZERO ------------------------------------------------
    got = w(0, 0)
    check("no data returns the same 'unknown' both call sites already construct",
          got == (None, None, None),
          f"got {got}. (0.0, 0.0, 0.0) is a point estimate of zero with a ZERO-WIDTH interval — a "
          f"stronger claim than measuring 1000 samples, which yields width 0.0038. Two spellings of "
          f"'no data' also invite a caller to handle one and forget the other.")

    # And the direction that makes that assertion meaningful: a REAL zero must still be reportable
    # and must carry uncertainty.
    p0, lo0, hi0 = w(0, 1000)
    check("a genuine 0/1000 still reports a rate of 0 with a NON-zero interval",
          p0 == 0.0 and hi0 > 0,
          f"got mean={p0} ci=({lo0}, {hi0}) — if this collapsed too, the organ could not distinguish "
          f"'measured none' from 'measured nothing' at all")

    # ---- monotonicity: more evidence must never widen the interval ---------------------------
    _, lo_small, hi_small = w(5, 10)
    _, lo_big, hi_big = w(500, 1000)
    check("more samples at the same rate narrow the interval",
          (hi_big - lo_big) < (hi_small - lo_small),
          f"10 samples -> width {hi_small-lo_small:.4f}; 1000 samples -> width {hi_big-lo_big:.4f}. "
          f"If more evidence widened it, the CI would be reporting something other than uncertainty.")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
