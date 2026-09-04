#!/usr/bin/env python3
"""A CLASS RATE MUST NOT DEPEND ON WHERE IN THE REPLY THE CLAIM APPEARED.

`reply-observer.py` logs one row per streamed CHUNK, each carrying an `index`. Detection runs on the
ACCUMULATED text, so a claim first seen at index 5 was judged against chunks 0-5 while one at index 0
was judged against a single chunk. Measured on the live log before collapsing: duration_claim sourced
at 17.3% at index 0 against 21.7% later. **That is the instrument varying with position, not the
behaviour varying.** A rate that moves with reply length is not a rate.

`tally_distinct` fixes it as a side effect of fixing something else — it keys on
`(turn, kind, matched)` and marks a claim sourced if ANY row for it was, so every chunk of one claim
collapses to one entry and the position it was first noticed at stops mattering. Measured after
collapsing: the duration_claim gap falls to -1.8pp at n=25, which is noise.

WHY THIS FILE EXISTS ANYWAY. That neutralisation is INCIDENTAL. Nothing states it, nothing tests it,
and the obvious "improvement" to tally_distinct — keying on index too, so two chunks of one claim are
not conflated — reintroduces the bias in full while looking like a bug fix. This pins the property
rather than the implementation, so a rewrite that preserves the behaviour stays green and one that
loses it goes red.

Run: python3 bin/tests/test_metric_position_invariance.py
"""
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
spec = importlib.util.spec_from_file_location("si_objective", str(ROOT / "bin" / "si-objective.py"))
si = importlib.util.module_from_spec(spec)
sys.modules["si_objective"] = si
spec.loader.exec_module(si)


def row(turn, kind, matched, index, sourced):
    return {"turn": turn, "kind": kind, "matched": matched, "index": index,
            "sourced": sourced, "final": True, "session": "s"}


def build(n_claims, chunks_per_claim, sourced_at):
    """One claim per turn, streamed over `chunks_per_claim` chunks.

    `sourced_at` picks WHICH chunk carries the sourced flag. The claim is identical in every case —
    only the position of the evidence moves. A position-invariant metric must return the same rate
    for every value of it.
    """
    out = []
    for c in range(n_claims):
        for i in range(chunks_per_claim):
            out.append(row("turn-%d" % c, "duration_claim", "about 3 hours", i, i == sourced_at))
    return out


def unsourced_rate(tally):
    d = tally.get("duration_claim")
    if not d or not d["total"]:
        return None
    return d["unsourced"] / d["total"]


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== the rate must not move when the evidence moves ===\n")

    rates = {}
    for at in range(4):
        t = si.tally_distinct(build(10, 4, sourced_at=at))
        rates[at] = unsourced_rate(t)
    print("  unsourced rate by which chunk carried the evidence: %s" % rates)
    check("identical for evidence at chunk 0, 1, 2 and 3", len(set(rates.values())) == 1,
          "rates differ by position: %s" % rates)
    check("...and the rate is the real one (all 10 claims sourced -> 0.0)", rates[0] == 0.0,
          "got %r" % rates[0])

    print("\n--- reply LENGTH must not move it either ---")
    lens = {}
    for n_chunks in (1, 2, 5, 9):
        t = si.tally_distinct(build(10, n_chunks, sourced_at=0))
        lens[n_chunks] = unsourced_rate(t)
    print("  unsourced rate by chunks-per-reply: %s" % lens)
    check("a 1-chunk reply and a 9-chunk reply score the same", len(set(lens.values())) == 1,
          "rates differ by length: %s" % lens)

    print("\n--- THE CONTROL: the naive 'fix' must visibly break it ---")
    # Keying on index as well is the plausible-looking change — "don't conflate two chunks of one
    # claim". It is exactly what reintroduces the defect, so the check above must be able to SEE it.
    def tally_by_index(rows):
        d = defaultdict(lambda: {"total": 0, "unsourced": 0})
        seen = {}
        for r in rows:
            key = (r.get("turn"), r.get("kind"), str(r.get("matched")), r.get("index"))
            seen[key] = seen.get(key, False) or bool(r.get("sourced"))
        for (t_, kind, _m, _i), src in seen.items():
            d[kind]["total"] += 1
            if not src:
                d[kind]["unsourced"] += 1
        return d

    # WHAT THIS CONTROL ORIGINALLY CLAIMED, AND WHY THAT WAS WRONG. It asserted the naive tally makes
    # the rate "position-dependent", and passed — on an `or` fallback, not on the stated property.
    # The printed evidence said the opposite: {0: 0.75, 1: 0.75, 2: 0.75, 3: 0.75}. Moving WHICH chunk
    # carries the evidence changes nothing under index-keying, because every claim in that fixture has
    # the same shape. The naive tally's defect is not sensitivity to position; it is sensitivity to
    # LENGTH — n chunks become n keys, of which exactly one is sourced, so the rate is (n-1)/n and
    # climbs toward 1.0 as replies get longer. A check whose label names a property its own output
    # contradicts is the defect this suite exists to catch, so it is stated as measured.
    by_pos = {at: unsourced_rate(tally_by_index(build(10, 4, sourced_at=at))) for at in range(4)}
    by_len = {n: unsourced_rate(tally_by_index(build(10, n, sourced_at=0))) for n in (1, 2, 5, 9)}
    print("  index-keyed rate by position: %s   (flat — NOT the failure mode)" % by_pos)
    print("  index-keyed rate by length  : %s   (climbs — this is the failure mode)" % by_len)
    check("keying on index makes the rate depend on REPLY LENGTH (so the test has teeth)",
          len(set(by_len.values())) > 1,
          "the control did not break the property — this test cannot detect a regression")
    check("...and it climbs toward 1.0 as replies lengthen, which is the (n-1)/n signature",
          by_len[1] < by_len[2] < by_len[5] < by_len[9], str(by_len))
    check("...while the SHIPPED tally stays flat across those same lengths",
          len(set(lens.values())) == 1, str(lens))

    print("\n--- and one claim is still ONE violation, however many chunks carried it ---")
    t = si.tally_distinct(build(10, 6, sourced_at=0))
    check("10 claims over 6 chunks each tally as 10, not 60",
          t["duration_claim"]["total"] == 10, "got %d" % t["duration_claim"]["total"])

    print("\n--- a claim NO row sourced is still counted unsourced ---")
    # The optimistic any-row rule must not become an always-sourced rule.
    none_sourced = [row("t%d" % c, "duration_claim", "about 3 hours", i, False)
                    for c in range(10) for i in range(4)]
    t = si.tally_distinct(none_sourced)
    check("all 10 unsourced -> rate 1.0", unsourced_rate(t) == 1.0, "got %r" % unsourced_rate(t))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
