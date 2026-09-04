#!/usr/bin/env python3
"""A post-window zero only means "the ask stopped" when a non-zero was likely.

WHY THIS EXISTS (2026-08-13). `measure-contract-fitness.py` grades an artifact by comparing the
recurrence of its ask before and after install. A pre-window too thin to support that comparison has
been refused since 2026-08-12 by `_apply_power_floor` -> INSUFFICIENT-UNDERPOWERED. The POST window
had no equivalent guard. `MIN_POST_DAYS = 2` is a floor in the wrong unit — days are not chances.

The asymmetry is the defect, not the missing check. Both windows can be too small to interpret, but
the two failure modes were not symmetric:

    pre-window too thin   ->  INSUFFICIENT-UNDERPOWERED   (admits nothing is known)
    post-window too thin  ->  GRADUATED                   (declares success)

A measurement whose two failure modes are "admit nothing is known" and "declare success" is not a
measurement. Measured on life's 17 trigger-carrying artifacts: E[post] under the null "the ask
recurs at exactly its old rate, nothing was prevented" was below 0.7 for EVERY one, highest 0.65,
and 15 of 17 landed on the GRADUATED branch. The one GRADUATED the shipped matcher actually
reported — art_cf3349715, "ask stopped recurring after firing 4x", the loop's ONLY success verdict
at the time — had p=0.44 of showing zero with nothing prevented at all.

WHAT THIS ASSERTS. Not "GRADUATED is wrong" — the point is that it must remain REACHABLE. A floor
that refuses every success is as useless as one that refuses none, so the first assertion below is
the one that keeps this honest: given real evidence (a common ask, a large post window, and a
genuine zero), GRADUATED must survive. The rest assert it is refused when the zero is what you would
expect anyway.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

import types

# COMPILED FROM SOURCE EVERY RUN, NOT LOADED VIA importlib — deliberately.
#
# `spec_from_file_location` + `exec_module` goes through SourceFileLoader, which will happily serve
# CACHED BYTECODE when the source's (mtime, size) match what the .pyc recorded. On macOS system
# python3.9 that cache is not a local `__pycache__` at all — it is redirected to
# ~/Library/Caches/com.apple.python/<abs-path>.pyc, so `find . -name '*.pyc'` finds nothing and the
# staleness is invisible from inside the repo.
#
# That bit hard while dosing this very file (2026-08-13): a dose changed a documented `179` to
# `150` — SAME BYTE LENGTH — and the restore landed within the same second, so mtime and size both
# matched and Python kept serving the DOSED bytecode. The file on disk read 179, `grep` agreed, and
# the loaded module reported 150. The visible symptom was a test that stayed red after a correct
# restore, which reads exactly like "the dose found a real bug" — the most expensive possible
# misreading, since it argues for changing correct code.
#
# This test asserts that a DOCSTRING agrees with the CODE beside it. Reading either one from a
# cache defeats the entire point, so both come from the same freshly-read bytes.
_SRC = REPO / "scheduling" / "claude-si" / "measure-contract-fitness.py"
mcf = types.ModuleType("mcf")
mcf.__file__ = str(_SRC)
exec(compile(_SRC.read_text(), str(_SRC), "exec"), mcf.__dict__)

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_zero_is_not_evidence_of_absence")

    # ---- THE CHECK MUST BE PASSABLE ------------------------------------------------------------
    # An ask seen in 60 of 300 pre-window observations (20%) that then appears 0 times in 200 more:
    # p = 0.8^200, astronomically small. If this does not survive as GRADUATED, the floor has eaten
    # the verdict it was meant to qualify, and the loop can never report a success again.
    v, why = mcf._apply_zero_floor("GRADUATED", "ask stopped recurring after firing 9x",
                                   pre=60, post=0, pre_n=300, post_n=200)
    check("a genuine zero against a common ask still reads GRADUATED",
          v == "GRADUATED",
          f"real evidence was refused: {v} — {why}\n          "
          "a floor that refuses every success verdict is not a floor, it is a mute button")
    check("the surviving GRADUATED states its own significance",
          "p=" in why,
          f"rationale carries no p-value, so a reader cannot tell a strong zero from a weak one: {why}")

    # ---- AND IT MUST REFUSE THE CASE THAT CAUSED IT ---------------------------------------------
    # art_cf3349715 as measured on 2026-08-13: 5 of 301 before, 49 observations after, zero hits.
    v, why = mcf._apply_zero_floor("GRADUATED", "ask stopped recurring after firing 4x",
                                   pre=5, post=0, pre_n=301, post_n=49)
    check("the real underpowered case is refused (art_cf3349715: 5/301 pre, 49 post)",
          v == "GRADUATED-UNDERPOWERED",
          f"verdict stayed {v} — this is the exact row that read as the loop's only success while "
          "having a ~44% chance of showing zero with nothing prevented")
    check("the refusal carries the verdict it replaced",
          "GRADUATED" in why and "Would otherwise have read" in why,
          f"superseded verdict discarded rather than carried: {why}\n          "
          "a row that silently changes label is how the fire_count:0 -> GRADUATED defect read as "
          "health")

    # ---- BOUNDARY: the floor must key on EXPOSURE, not on the raw count -------------------------
    # Same pre count, same post count, only the post window's SIZE differs. If the verdict does not
    # move, the check is reading something other than power.
    small, _ = mcf._apply_zero_floor("GRADUATED", "x", pre=5, post=0, pre_n=100, post_n=10)
    large, _ = mcf._apply_zero_floor("GRADUATED", "x", pre=5, post=0, pre_n=100, post_n=400)
    check("identical counts in a small vs large post window reach different verdicts",
          small == "GRADUATED-UNDERPOWERED" and large == "GRADUATED",
          f"small window -> {small}, large window -> {large}; the floor is not responding to "
          "exposure, so it is not measuring power")

    # ---- ZERO EXPOSURE IS NOT EVIDENCE ----------------------------------------------------------
    v, _ = mcf._apply_zero_floor("GRADUATED", "x", pre=9, post=0, pre_n=100, post_n=0)
    check("a post window with no observations at all cannot graduate",
          v == "GRADUATED-UNDERPOWERED",
          "an empty post window returned a success verdict — zero out of zero chances is the one "
          "case where absence provably means nothing")
    v, _ = mcf._apply_zero_floor("GRADUATED", "x", pre=0, post=0, pre_n=100, post_n=100)
    check("an ask never seen before does not graduate by continuing not to happen",
          v == "GRADUATED-UNDERPOWERED",
          "pre=0 produced a success verdict — the artifact would be credited for the continued "
          "absence of something that never occurred")

    # ---- IT MUST NOT TOUCH ANYTHING ELSE --------------------------------------------------------
    for verdict in ("NOT-BINDING", "DECAYING", "INSUFFICIENT-UNDERPOWERED", "UNENFORCEABLE",
                    "QUARANTINED"):
        got, _ = mcf._apply_zero_floor(verdict, "x", pre=1, post=0, pre_n=100, post_n=3)
        if got != verdict:
            check(f"{verdict} is left alone", False,
                  f"became {got}; this floor qualifies ONE claim (the ask stopped) and must not "
                  "restate a refusal already made or overwrite a non-comparative verdict")
            break
    else:
        check("non-GRADUATED verdicts pass through untouched", True)

    got, _ = mcf._apply_zero_floor("GRADUATED", "x", pre=5, post=3, pre_n=100, post_n=10)
    check("a NON-zero post count is out of scope",
          got == "GRADUATED",
          f"became {got}; this check is about whether a ZERO is informative. A non-zero post count "
          "is graded by the rate comparison, which is a different question")

    # ---- THE PRE-SIDE FLOOR STILL RUNS FIRST ----------------------------------------------------
    # Ordering matters: a row under MIN_PRE_N must report the PRE deficiency, which is the more
    # fundamental one — there is no rate to compare against at all.
    v1, _ = mcf._apply_power_floor("GRADUATED", "x", pre=1)
    v2, _ = mcf._apply_zero_floor(v1, "x", pre=1, post=0, pre_n=100, post_n=3)
    check("a row failing BOTH floors reports the pre-window deficiency",
          v2 == "INSUFFICIENT-UNDERPOWERED",
          f"got {v2}; with no interpretable pre-window there is no rate for the post window to be "
          "underpowered relative TO, so the pre refusal is the true one")

    # ---- THE DOCUMENTED REQUIRED-EXPOSURE TABLE MUST BE TRUE ------------------------------------
    # _apply_zero_floor's docstring tells a reader how much post-window exposure a significant zero
    # needs, and that table is the ONLY thing standing between "GRADUATED is empty" and someone
    # raising POST_ZERO_ALPHA to refill it. A stale table would be worse than none: it would make a
    # wrong number authoritative at exactly the moment someone is deciding whether to loosen the
    # threshold. So the numbers are asserted against the implementation, not trusted as prose.
    doc = mcf._apply_zero_floor.__doc__ or ""
    rows = re.findall(r"^\s+art_\w+\s+(\d+)/(\d+)\s+(\d+)\s*$", doc, re.M)
    check(f"the docstring's required-exposure table is present and parseable ({len(rows)} rows)",
          len(rows) >= 4,
          "could not parse 'pre/pre_n -> needed' rows out of the docstring; if the table was "
          "reformatted this check silently stops verifying it")
    wrong = []
    for pre_s, pre_n_s, need_s in rows:
        pre, pre_n, need = int(pre_s), int(pre_n_s), int(need_s)
        # at post_n = need the zero must be significant; one observation fewer, it must not be.
        sig_at, _ = mcf._zero_is_uninformative(pre, pre_n, need)
        sig_below, _ = mcf._zero_is_uninformative(pre, pre_n, need - 1)
        if sig_at or not sig_below:
            wrong.append(f"{pre}/{pre_n}: doc says {need}, but significant_at={not sig_at} "
                         f"significant_at_{need - 1}={not sig_below}")
    check("every documented threshold is the ACTUAL boundary in the implementation",
          not wrong,
          "the docstring's numbers no longer match the code:\n          "
          + "\n          ".join(wrong)
          + "\n          This table is what stops POST_ZERO_ALPHA being raised to refill an empty "
            "GRADUATED column; wrong numbers there are worse than none.")

    check("POST_ZERO_ALPHA is still the value the table was computed at",
          abs(mcf.POST_ZERO_ALPHA - 0.05) < 1e-12,
          f"POST_ZERO_ALPHA is {mcf.POST_ZERO_ALPHA}, not 0.05. If that was deliberate the "
          "docstring table must be recomputed — and note that raising it makes flattering verdicts "
          "reachable again on evidence that has not changed, which is the defect it exists to close.")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
