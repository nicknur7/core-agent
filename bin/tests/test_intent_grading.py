#!/usr/bin/env python3
"""test_intent_grading.py — the intent check must DISCRIMINATE, not just say "holds".

WHY THIS EXISTS
---------------
On its first run, bin/grade-intent.py reported all nine gates as "rotted". That was not
nine simultaneous regressions — it was a broken instrument: bin/hook-intent.py had stored
each positive example as a 200-character PREFIX, so whatever actually made the gate fire
usually sat past the cut and the stored example could not reproduce its own catch.

After the fix, all nine report "holds". Which is the correct answer, and is also exactly
what a check that had silently stopped working would report. A green light means nothing
unless you can show the instrument still goes red.

So this proves the three verdicts on synthetic gates whose behaviour is known exactly:
  holds      catches every positive, rejects every negative
  rotted     no longer catches a founding example — the serious one
  imprecise  fires on its own negative — the trigger reaches past its purpose

And it pins the precedence: ROT OUTRANKS IMPRECISION. A gate that has stopped doing its job
must not be reported as merely noisy because its negatives happen to be clean.

Run: python3 bin/tests/test_intent_grading.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gi", REPO / "bin" / "grade-intent.py")
gi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gi)

_fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


class _FakeGG:
    """Stands in for grade-gate's loader so a synthetic gate can be graded without a file."""

    def __init__(self, mod):
        self._mod = mod

    def load_hook(self, name):
        return self._mod


def _gate(trigger: str):
    """A gate that fires when `trigger` appears. Deliberately trivial — the point is that its
    behaviour is known exactly, so a wrong verdict is unambiguous."""
    class M:
        @staticmethod
        def detect(text):
            return [trigger] if trigger in (text or "") else []
    return M


INTENT = {
    "guards": "synthetic",
    "positives": ["the widget is broken", "that widget is broken too"],
    "negatives": ["nothing to report here", "all systems nominal"],
}

print("=== the three verdicts ===")

r = gi.grade("x", INTENT, _FakeGG(_gate("widget is broken")))
check("a working gate holds", r["verdict"] == "holds", r["verdict"])
check("holds reports both counts", r["positives"] == 2 and r["negatives"] == 2)

# Stops catching what it was built for — the serious failure.
r = gi.grade("x", INTENT, _FakeGG(_gate("SOMETHING ELSE ENTIRELY")))
check("a gate that misses its positives is ROTTED", r["verdict"] == "rotted", r["verdict"])
check("rotted counts the misses", r["positives_missed"] == 2, str(r.get("positives_missed")))
check("rotted names a concrete failing example", "widget" in r["example_failure"],
      r["example_failure"][:60])

# Fires on its own negative — right idea, trigger too wide.
r = gi.grade("x", INTENT, _FakeGG(_gate("e")))     # matches everything
check("a gate that fires on its negatives is IMPRECISE", r["verdict"] == "imprecise", r["verdict"])
check("imprecise counts the false catches", r["negatives_caught"] == 2,
      str(r.get("negatives_caught")))

print("\n=== precedence: rot outranks imprecision ===")
# A gate that BOTH misses positives and catches negatives must report rotted, not imprecise.
# Reporting "noisy" for something that has stopped working entirely would send the tune path
# to narrow a gate that needs re-deriving.
both = {"guards": "synthetic",
        "positives": ["alpha signal"],          # will be missed
        "negatives": ["beta noise"]}            # will be caught
r = gi.grade("x", both, _FakeGG(_gate("beta")))
check("misses-a-positive AND catches-a-negative reports rotted", r["verdict"] == "rotted",
      r["verdict"])

print("\n=== degenerate inputs ===")
r = gi.grade("x", {"guards": "s", "positives": [], "negatives": []}, _FakeGG(_gate("z")))
check("no positives means UNPROVEN, never 'holds'", r["verdict"] == "unproven", r["verdict"])

class _NoDetect:
    pass
r = gi.grade("x", INTENT, _FakeGG(_NoDetect()))
check("a hook without detect() is ungradeable, not passing", r["verdict"] == "ungradeable",
      r["verdict"])

class _Explodes:
    @staticmethod
    def detect(text):
        raise RuntimeError("boom")
r = gi.grade("x", INTENT, _FakeGG(_Explodes()))
check("a detect() that raises does not crash the grader", r["verdict"] in ("rotted", "ungradeable"),
      r["verdict"])

print("\n=== the live registry's own records still hold ===")
# The regression this whole file exists for: stored positives must reproduce their own catch.
# If a future change to hook-intent.py truncates or normalizes them lossily again, this goes red.
import json
reg = json.loads((REPO / "bin" / "hook-registry.json").read_text())
recorded = [h for h in reg["hooks"] if h.get("intent") and not h.get("retired")]
check("at least one gate has an intent record", len(recorded) > 0, str(len(recorded)))
for h in recorded:
    it = h["intent"]
    pos = it.get("positives") or []
    check(f"{h['name']}: every stored positive is non-empty",
          all(p.strip() for p in pos), f"{sum(1 for p in pos if not p.strip())} empty")

print()
if _fails:
    print(f"FAILURES ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL PASS")
