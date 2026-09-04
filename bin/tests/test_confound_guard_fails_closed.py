#!/usr/bin/env python3
"""THE CONFOUND GUARD MUST FAIL CLOSED. IT ONCE FAILED OPEN, AND THE ANSWER IT GAVE WAS THE
FLATTERING ONE.

AUTHORED AND RUN ON core-finance. NOT INSTALLED HERE — finance is a puller and bin/tests/ is
baseline-shared, so per the routing life decided 2026-08-12 the source is returned on the bus and
life commits it. Intended install path: bin/tests/test_confound_guard_fails_closed.py

WHY THIS EXISTS, IN THE INCUMBENT'S OWN WORDS
----------------------------------------------
`measure-contract-fitness.py :: _confounded()` carries this postmortem in its except block:

    "FAIL CLOSED. This was `res = False`, and False means NOT-CONFOUNDED means PROCEED — so an
     unreachable database, a query error, or a schema change silently WAIVED the only guard
     standing between per_week's confounded denominator and a published verdict. core-business
     probed it with fake cursors: DB unreachable -> False -> proceeds.

     The guard works when it can RUN (a 40x density skew is correctly refused). The defect is
     entirely in what happens when it cannot, and the answer was the flattering one.

     True here means 'treat as confounded', i.e. REFUSE the rate comparison. A measurement
     that cannot verify its own precondition has not verified it."

**That paragraph is a specification and nothing tested it.** core-life, 2026-08-12: "the postmortem
comments in these files are specifications, and nothing tests them... There are at least four more
documented postmortems in measure-contract-fitness alone." This closes one of them.

The class matters more than the instance. A fix recorded only as prose survives exactly as long as
nobody edits the block — and this one is a single token wide (`True` -> `False`) with no test
underneath it. The GEN regression that forced life's 2026-08-12 retraction was the same shape: the
incumbent wrote "reversible by deleting one clause," and a new instrument deleted the clause.

WHAT IS ASSERTED, AND WHY EACH ONE
-----------------------------------
1. HEALTHY PATH — a comparable pre/post density returns False (not confounded). Without this the
   other assertions pass against a guard hard-wired to True, which would refuse everything and be
   just as broken in the opposite direction.
2. THE REAL SKEW — a 40x density difference returns True. The docstring claims this case works;
   asserted so a future "simplification" of the ratio maths cannot quietly break it.
3. THE POSTMORTEM — a cursor that RAISES returns True. This is the regression the comment describes:
   DB unreachable must REFUSE, never PROCEED.
4. VISIBILITY — the failure is recorded in `_CONFOUND_ERRORS`, so a refusal caused by breakage is
   distinguishable from a refusal caused by real skew. A silent fail-closed is safe but undebuggable.
5. MUTATION CONTROL, in-band — assertion 3 is re-run against a guard whose except-branch has been
   forced back to the old `res = False`. If that does not flip the verdict, assertions 3 and 4 are
   passing vacuously and this file is decoration.

Fake cursors only. No database connection, no writes, no live state touched.

Run: python3 tasks/si-verification/probes/test_confound_guard_fails_closed.py
"""
import importlib.util
import sys
from pathlib import Path


def _root() -> Path:
    p = Path(__file__).resolve()
    for cand in p.parents:
        if (cand / "scheduling" / "claude-si").is_dir() and (cand / "bin").is_dir():
            return cand
    raise SystemExit("SKIP - could not locate Core root")


ROOT = _root()
MCF = ROOT / "scheduling" / "claude-si" / "measure-contract-fitness.py"


def load():
    sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
    sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))
    spec = importlib.util.spec_from_file_location("mcf_confound_probe", MCF)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


class CountCursor:
    """Returns a fixed count per execute(). Stands in for the real activity query."""

    def __init__(self, counts):
        self._counts = list(counts)
        self._i = 0

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        v = self._counts[min(self._i, len(self._counts) - 1)]
        self._i += 1
        return (v,)


class ExplodingCursor:
    """A cursor that cannot answer — the DB-unreachable / schema-change case from the postmortem."""

    def execute(self, *a, **k):
        raise RuntimeError("simulated: database unreachable")

    def fetchone(self):
        raise RuntimeError("simulated: database unreachable")


def main() -> int:
    from datetime import date
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== _confounded() must fail CLOSED when it cannot verify its own precondition ===\n")
    if not MCF.is_file():
        print("  SKIP - measure-contract-fitness.py absent")
        return 0

    m = load()
    lo, mid, hi = date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)

    # 1. HEALTHY — ~equal density either side. Cache is cleared before each call because
    #    _confounded memoises on the split date and would otherwise return a prior verdict.
    m._CONFOUND_CACHE.clear()
    healthy = m._confounded(CountCursor([300, 310]), lo, mid, hi)
    check("CONTROL - comparable densities are NOT confounded",
          healthy is False, "got %r; if this is True the guard refuses everything" % healthy)

    # 2. REAL SKEW — the case the docstring says works.
    m._CONFOUND_CACHE.clear()
    skewed = m._confounded(CountCursor([10, 4000]), lo, mid, hi)
    check("a large density skew IS confounded (the guard works when it can run)",
          skewed is True, "got %r for a ~400x skew against CONFOUND_FACTOR=%s"
          % (skewed, getattr(m, "CONFOUND_FACTOR", "?")))

    # 3. THE POSTMORTEM ITSELF.
    m._CONFOUND_CACHE.clear()
    m._CONFOUND_ERRORS.clear()
    broken = m._confounded(ExplodingCursor(), lo, mid, hi)
    check("a cursor that RAISES returns True (REFUSE), not False (PROCEED)",
          broken is True,
          "got %r. This is the regression the except-block documents: 'False means NOT-CONFOUNDED "
          "means PROCEED — so an unreachable database ... silently WAIVED the only guard standing "
          "between per_week's confounded denominator and a published verdict.'" % broken)

    # 4. VISIBILITY — a refusal-by-breakage must be distinguishable from a refusal-by-skew.
    check("the failure is recorded in _CONFOUND_ERRORS, so the refusal is not silent",
          len(m._CONFOUND_ERRORS) >= 1 and "RuntimeError" in m._CONFOUND_ERRORS[0],
          "_CONFOUND_ERRORS=%r — a silent fail-closed is safe but undebuggable, and cannot be told "
          "apart from a genuine 40x skew" % (m._CONFOUND_ERRORS,))

    # 5. MUTATION CONTROL — restore the OLD behaviour and prove assertion 3 can fail.
    print("\n--- mutation control: assertion 3 must be able to FAIL ---")
    src = MCF.read_text()
    old = "        res = True\n        _CONFOUND_ERRORS.append"
    new = "        res = False\n        _CONFOUND_ERRORS.append"
    if old not in src:
        check("mutation site located in the except block", False,
              "could not find the `res = True` fail-closed line; this probe cannot prove it "
              "discriminates, so treat every green above as unverified")
    else:
        import tempfile
        # TemporaryDirectory, NOT mkdtemp. The first version of this file used mkdtemp() with no
        # cleanup and leaked one directory per run — caught 2026-08-12 by claim-sweeping this
        # probe's own docstring ("no writes, no live state touched") against its behaviour, which
        # is business's doser-vs-claim-sweep distinction applied to the prober. Measured: tmpdir  # privacy-ok: generic engineering vocabulary
        # entry count went 8884 -> 8885 on a single run, on a machine already carrying orphaned
        # test files. A verification probe that litters is the same defect class it exists to find.
        with tempfile.TemporaryDirectory() as td:
            mut_path = Path(td) / "measure-contract-fitness.py"
            mut_path.write_text(src.replace(old, new, 1))
            spec = importlib.util.spec_from_file_location("mcf_mutant", mut_path)
            mut = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mut)
            except SystemExit:
                pass
            mut._CONFOUND_CACHE.clear()
            regressed = mut._confounded(ExplodingCursor(), lo, mid, hi)
            check("reverting to `res = False` makes the broken cursor PROCEED",
                  regressed is False,
                  "mutant returned %r — assertion 3 is not actually testing the except branch"
                  % regressed)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
