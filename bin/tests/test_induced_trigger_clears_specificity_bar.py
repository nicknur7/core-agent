#!/usr/bin/env python3
"""An induced contract's trigger must clear the SAME specificity bar every other artifact clears.

WHY THIS EXISTS (2026-08-18, found by core-business).

`friction_test_gate.gate()` refuses any artifact firing on more than `OVERBROAD_RATE` of real corpus
prompts. It is reachable only through `friction_installer`. `si_induct.induce_inject_only()` writes
to `learned_contracts` in raw SQL and never passes it — so a trigger built from the six most frequent
content words of forty sample prompts went straight into a live contract, untested by a bar that
already existed.

business measured the cost with the gate's own test, each seat against its own corpus:

    cid  key                          fires   rate        (bar: 3%)
     43  instruction directive          185   60.1%
     52  instruction emphatic           189   61.4%
     46  instruction tooling            104   33.8%
     44  instruction preference          78   25.3%
    every hand-authored starter contract: 0.3% - 0.6%

Nine generated triggers over the bar across three seats, eight of them `instruction-*`.

**Life reproduces it too, which business predicted it could not.** Life has no induced contracts, but
it has the same `instruction-*` observations, and the pre-fix generator run against life's own corpus
yields 31%-54% for five labels. The seat that owns the fix could see the defect after all; what it
lacked was the artifact, not the data.

Two harms, and the second is the one that is not merely noise: `learned-classifier.py:111` caps the
injected block at `matched[:2]`, so two over-broad contracts consume both slots and a precise
contract never reaches the prompt at all.

WHAT THIS ASSERTS.
  1. `_generate_trigger` measures its candidate against the corpus and refuses or narrows — a
     generated trigger over the bar is never returned.
  2. It uses the gate's own constants rather than a second copy of the number.
  3. It fails CLOSED: no corpus, or a corpus under `MIN_CORPUS`, yields no trigger. The gate calls
     that undecidable, and undecidable must not install.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

import friction_test_gate as tg  # noqa: E402
import si_induct as SI  # noqa: E402

checks = 0


class FakeCur:
    """Cursor returning cluster prompts first, then the corpus sample."""

    def __init__(self, cluster, corpus):
        self.cluster, self.corpus, self.mode = cluster, corpus, None

    def execute(self, q, params=None):
        self.mode = "cluster" if "pattern_label=%s" in q else "corpus"

    def fetchall(self):
        return [(t,) for t in (self.cluster if self.mode == "cluster" else self.corpus)]


# A cluster whose top words are ubiquitous in the corpus -> pre-fix generator returns a broad regex.
CLUSTER = ["please review the plan before you continue",
           "review the plan and then continue please",
           "continue with the plan review now"] * 4
CORPUS = ["please review the plan " + str(i) for i in range(100)]

out = SI._generate_trigger(FakeCur(CLUSTER, CORPUS), 1, "instruction-test")
assert out == [], "a trigger firing on ~100%% of the corpus was returned: %r" % (out,)
checks += 1

# Same cluster, a corpus it is genuinely rare in -> a trigger IS produced, and it clears the bar.
RARE = ["unrelated prompt about %d widgets and shipping" % i for i in range(100)]
RARE[0] = "please review the plan"
out2 = SI._generate_trigger(FakeCur(CLUSTER, RARE), 1, "instruction-test")
assert out2, "no trigger produced for a genuinely specific cluster — the gate is refusing everything"
checks += 1
rx = re.compile(out2[0], re.I)  # the matcher learned-classifier.py:99 actually runs
rate = sum(1 for t in RARE if rx.search(t)) / len(RARE)
assert rate <= tg.OVERBROAD_RATE, "returned trigger fires on %.0f%% > bar %.0f%%" % (
    100 * rate, 100 * tg.OVERBROAD_RATE)
checks += 1

# Fails closed on a corpus too small to prove anything (finance sits at 33, under MIN_CORPUS).
small = SI._generate_trigger(FakeCur(CLUSTER, RARE[: tg.MIN_CORPUS - 1]), 1, "instruction-test")
assert small == [], "a trigger was installed against a corpus too small to prove specificity: %r" % (small,)
checks += 1

# The bar is imported, not re-declared — a second copy of the number is how the two drift apart.
src = (REPO / "scheduling" / "claude-si" / "si_induct.py").read_text()
assert "friction_test_gate" in src, "si_induct no longer imports the gate — the bar has been copied"
checks += 1
assert not re.search(r"^\s*OVERBROAD_RATE\s*=", src, re.M), \
    "si_induct declares its own OVERBROAD_RATE — it must use the gate's"
checks += 1

print("ok — %d checks: bar %.0f%% imported from friction_test_gate, MIN_CORPUS %d, fails closed"
      % (checks, 100 * tg.OVERBROAD_RATE, tg.MIN_CORPUS))
