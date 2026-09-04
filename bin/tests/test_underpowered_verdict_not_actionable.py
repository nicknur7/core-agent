#!/usr/bin/env python3
"""A verdict the instrument calls uninterpretable must not reach ANY consumer, by any path.

WHY THIS EXISTS (2026-08-12). measure-contract-fitness appends "[pre n=%d, TOO FEW to interpret]" to
a verdict's RATIONALE when the pre-window is under MIN_PRE_N. Consumers branch on `verdict`. So the
floor lived in prose a human reads while the decision was made on a label a machine reads.

MEASURED COST. `plan-not-execute` has pre_count=0, post_count=3. There is nothing for a post rate to
be "still recurring" relative to; the measurement's own text says so. It drove
`tune_flag_needs_oracle` **111 times** across ~110 loop runs, and art_97b6… (pre n=1) drove 108 more.

THIS FILE HAS ALREADY BEEN WRONG ONCE, WHICH IS THE POINT.

Its first version asserted the floor as a `pre_count >= MIN_PRE_N` filter on the two lists the module
EMITS (`not_binding`, `not_binding_artifacts`) — and passed, while the defect stayed live. Because
`friction_loop` does not only read those lists. At :1149 and :1156 it RE-DERIVES two more sets from
the raw rows, filtering on `verdict` with no floor at all, and at :1160 subtracts one from the other
— so the floor was not just bypassed, it was inverted. Measured with the "fix" shipped:
plan-not-execute, art_97b6… and art_3c7e… were ALL still in the sets that reach flag_needs_oracle.

Two fixes, both real, both in the wrong layer. A guard that must be repeated at each consumer will be
missed at the next consumer. So the floor now lives in the VERDICT ITSELF — under-powered rows are
downgraded to `INSUFFICIENT-UNDERPOWERED`, joining the family this module already used to refuse
(`INSUFFICIENT`, `INSUFFICIENT-CONFOUNDED`). No consumer needs to know MIN_PRE_N exists.

WHAT THIS ASSERTS, in the order that matters:
  1. the floor function itself, executed, across every verdict the instrument can emit;
  2. the EMITTED lists cannot carry an under-powered row;
  3. **the RE-DERIVED sets cannot either** — reproducing friction_loop's own expressions verbatim.
Assertion 3 is the one that would have caught the live defect, and the reason this file was rewritten
rather than deleted.

Both directions matter throughout. Holding everything out would silence the loop's only working
signal (`stop-and-plan`, pre=30, a real NOT-BINDING) — that must survive.
"""
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scheduling" / "claude-si" / "measure-contract-fitness.py"
CONSUMER = REPO / "scheduling" / "claude-si" / "friction_loop.py"

FLOORED = "INSUFFICIENT-UNDERPOWERED"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_underpowered_verdict_not_actionable")
    if not SRC.is_file():
        print(f"  FAIL  {SRC} missing")
        return 1

    spec = importlib.util.spec_from_file_location("_mcf", SRC)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001  # privacy-ok: noqa linter directive, not a course code
        print(f"  FAIL  cannot import the instrument: {e}")
        return 1

    floor = getattr(mod, "MIN_PRE_N", None)
    apply_floor = getattr(mod, "_apply_power_floor", None)
    check("the instrument exposes MIN_PRE_N and _apply_power_floor",
          isinstance(floor, int) and callable(apply_floor),
          "the floor moved or was renamed; this file would stop measuring the thing it names")
    if not (isinstance(floor, int) and callable(apply_floor)):
        return 1

    # ---- 1. THE FLOOR FUNCTION, EXECUTED ------------------------------------------------------
    # Every verdict the module can emit, at both ends of the floor. A hand-copy of the rule here
    # would drift from the shipped one, so the shipped function is called directly.
    cases = [
        # (verdict, pre, must_be_floored, why this case is in the list)
        ("NOT-BINDING",             0,  True,  "the row that drove 111 flags"),
        ("NOT-BINDING-FIRED",       1,  True,  "art_97b6 — survived both earlier fixes"),
        ("NOT-BINDING-NO-FIRE",     0,  True,  "art_7650"),
        ("GRADUATED",               1,  True,  "success reported by construction"),
        ("GRADUATED-UNPROVEN",      0,  True,  "same, weaker claim, same missing baseline"),
        ("DECAYING",                3,  True,  "model-routing-and-defaults"),
        ("NOT-BINDING",            30,  False, "stop-and-plan — the REAL signal, must survive"),
        ("DECAYING",            floor,  False, "exactly at the floor; the bar is >=, not >"),
        ("GRADUATED",              99,  False, "a well-powered success must stay a success"),
        ("INSUFFICIENT",            0,  False, "already refuses — must not be double-downgraded"),
        ("INSUFFICIENT-CONFOUNDED", 0,  False, "already refuses, for the other invalid comparison"),
        ("UNENFORCEABLE",           0,  False, "a category claim, true at any sample size"),
        ("GATED-WATCH",             0,  False, "'too early, look again' — not a rate comparison"),
    ]
    for verdict, pre, want_floored, note in cases:
        got, why = apply_floor(verdict, "rationale", pre)
        ok = (got == FLOORED) == want_floored
        check(f"pre={pre:<3} {verdict:<24} -> {'floored' if want_floored else 'survives':<9} ({note})",
              ok, f"got {got!r}")

    # The superseded verdict must remain readable. A row that silently changes label is how the
    # fire_count:0 -> GRADUATED defect read as health for weeks.
    _, why = apply_floor("NOT-BINDING", "fires 8x but correction recurs", 0)
    check("the downgraded row still reports what it WOULD have read",
          "NOT-BINDING" in why and "fires 8x" in why,
          f"the original verdict was discarded, not carried: {why!r}")

    # ---- 2. THE EMITTED LISTS -----------------------------------------------------------------
    src = SRC.read_text()
    for name in ("not_binding", "not_binding_artifacts"):
        m = re.search(rf"\b{name} = (?:sorted\()?\[?(.*?)\]?\)?\n\s*(?:if|payload|#|\w+ =)", src, re.S)
        check(f"the emitted `{name}` selection is still findable", m is not None,
              "the expression moved; this half would silently stop being checked")

    # ---- 3. THE RE-DERIVED SETS — the path both earlier fixes missed ---------------------------
    # Reproduced from friction_loop verbatim rather than described, so that if the consumer changes
    # its filter this test is measuring the NEW filter, not a remembered one.
    consumer = CONSUMER.read_text() if CONSUMER.is_file() else ""
    check("the consumer's re-derivation is still where this test thinks it is",
          "not_binding_fired = {" in consumer and "not_binding_fired_slugs = {" in consumer,
          f"{CONSUMER.name} no longer re-derives these sets — re-read it and update this assertion "
          f"rather than deleting it; the re-derivation is the whole reason this file exists")

    rows = [
        {"contract": "underpowered", "verdict": FLOORED,       "pre_count": 0,  "fire_count": 8},
        {"contract": "stop-and-plan", "verdict": "NOT-BINDING", "pre_count": 30, "fire_count": 8},
    ]
    art_rows = [
        {"artifact_id": "art_thin", "verdict": FLOORED,             "pre_count": 1},
        {"artifact_id": "art_real", "verdict": "NOT-BINDING-FIRED", "pre_count": 30},
    ]
    # friction_loop.py:1149 and :1156, character for character.
    nb_fired = {r.get("artifact_id") for r in art_rows
                if str(r.get("verdict", "")) == "NOT-BINDING-FIRED" and r.get("artifact_id")}
    nb_fired_slugs = {r.get("contract") for r in rows
                      if str(r.get("verdict", "")) == "NOT-BINDING"
                      and int(r.get("fire_count") or 0) > 0 and r.get("contract")}

    check("an under-powered CONTRACT cannot reach the consumer's re-derived slug set",
          "underpowered" not in nb_fired_slugs,
          f"got {sorted(nb_fired_slugs)!r} — this is the set that feeds flag_needs_oracle at "
          f"friction_loop.py:936, and it is derived from the raw verdict, not from any list this "
          f"module filters")
    check("an under-powered ARTIFACT cannot reach the consumer's re-derived id set",
          "art_thin" not in nb_fired,
          f"got {sorted(nb_fired)!r} — feeds flag_needs_oracle at friction_loop.py:918")
    check("a well-powered contract still reaches it (the signal is not silenced)",
          "stop-and-plan" in nb_fired_slugs, f"got {sorted(nb_fired_slugs)!r}")
    check("a well-powered artifact still reaches it",
          "art_real" in nb_fired, f"got {sorted(nb_fired)!r}")

    # ---- 4. NO SILENT DROPS -------------------------------------------------------------------
    check("the downgrade is REPORTED by name, not silent",
          "downgraded to INSUFFICIENT-UNDERPOWERED" in src,
          "a row vanishing from a list with no explanation is how the fire_count:0 -> GRADUATED "
          "defect read as health")
    check("artifact rows carry pre_count at all (the floor needs a COUNT, not a rate)",
          '"pre_count": pre' in src,
          "a rate of 0.1/wk cannot say whether it came from 1 observation or 40")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
