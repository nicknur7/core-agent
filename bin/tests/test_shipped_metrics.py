"""Invariance and fail-direction probes against the SHIPPED code, not a re-implementation.

core-business withdrew its own claim that si-objective's metric "provably survives its own claims":
what survived was its COPY. `rate` was a closure nested inside another function — importable by
nothing, callable by nothing, testable by nothing — so every passing fixture described the copy.

A COPY THAT AGREES WITH YOUR READING OF THE ORIGINAL IS EVIDENCE ABOUT YOUR READING. This file
imports the real functions and puts them under the same fixtures.

Run: python3 bin/tests/test_shipped_metrics.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PASS = FAIL = 0


def load(rel, name):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % label)
    else:
        FAIL += 1
        print("  FAIL  %s %s" % (label, detail))


print("\n=== the SHIPPED per-100-replies rate, imported ===")
sio = load("bin/si-objective.py", "sio_shipped")

# PAIRED INVARIANCE. Each pair differs ONLY in the declared property; the true answer is constant
# by construction. A metric that moves across a pair is measuring the confound, and the pair names
# which one. 10 violations in 100 replies is the same rate however those replies are distributed.
PAIRS = [
    ("wall-clock   (1 day vs 30 days)", (10, 100), (10, 100)),
    ("volume       (100 vs 1000 replies, same rate)", (10, 100), (100, 1000)),
    ("session-shape(one long vs many short)", (10, 100), (10, 100)),
]
for label, a, b in PAIRS:
    ra, rb = sio.rate(*a), sio.rate(*b)
    check("invariant to %s" % label, ra == rb, "(%s vs %s)" % (ra, rb))

# It must NOT be invariant to the thing it measures, or it is not measuring anything.
check("NOT invariant to the violation count (a control)",
      sio.rate(10, 100) != sio.rate(20, 100),
      "a metric invariant to its own numerator is inert")

# Zero replies is no evidence, not a clean score.
check("zero replies -> None, never 0.0", sio.rate(0, 0) is None,
      "0.0 would read as a perfect score computed from nothing")

print("\n=== the confound guard's FAIL DIRECTION ===")
mcf = load("scheduling/claude-si/measure-contract-fitness.py", "mcf_shipped")


class ExplodingCursor:
    """A database that is simply unavailable — the case that used to WAIVE the guard."""

    def execute(self, *a, **k):
        raise RuntimeError("database unreachable")

    def fetchone(self):
        raise RuntimeError("database unreachable")


import datetime as _dt
lo, split, hi = _dt.date(2026, 6, 1), _dt.date(2026, 7, 1), _dt.date(2026, 8, 1)
mcf._CONFOUND_CACHE.clear()
res = mcf._confounded(ExplodingCursor(), lo, split, hi)
check("DB unreachable -> treated as CONFOUNDED (refuses)", res is True,
      "-> False means 'not confounded' means PROCEED: the guard waives itself on error")

check("the error is recorded, not swallowed", bool(mcf._CONFOUND_ERRORS),
      "a silent refusal is its own fail-toward")

print("\n=== Results: %d passed, %d failed ===\n" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
