#!/usr/bin/env python3
"""RE-DERIVES THE OBSERVATION-LOG FIGURES I QUOTED, so they stop being citations to a conversation.

core-business, bus #978, turning my own point back on me: *"a number whose query lives nowhere is a
citation to a conversation."* It had quoted four provenance figures in a research artifact next to
the words "the 2,812 provenance figure is exact" — every one produced by a one-liner typed into a
shell and then discarded.

Mine were worse in one respect: I put them in COMMIT MESSAGES, which read as more durable than a
research note and are harder to correct. `duration_claim 242 raw / 151 distinct = 1.60x`, `sourced
17.3% at index 0 against 21.7% later`, `32 argv-blind tools` — all from heredocs, none re-runnable.

WHY THIS COMPARES INSTEAD OF ASSERTING. The counts move every time the observer records a turn, so
pinning 1.60x would make this red by tomorrow for no reason. What the conclusions actually rest on
is structural, and that is what gets asserted:

    inflation > 1.0                 raw rows exceed distinct claims, so collapsing them was not a
                                    no-op — if this reaches 1.0 the dedupe is either fixed upstream
                                    or has stopped working, and either way the commit messages
                                    citing it need re-reading
    |gap after| < |gap before|      tally_distinct actually neutralises the position bias, which is
                                    the claim bin/tests/test_metric_position_invariance.py pins as a
                                    property and this measures on real data
    the log is non-empty            an empty log makes every rate above 0/0, and a probe that
                                    reports health on no data is the defect this file exists to end

Run: python3 bin/observation-probe.py [--json]
"""
import collections
import json
import math
import os
import sys
from pathlib import Path

# How many standard errors the change in |gap| must clear before claim 2 renders a verdict at all.
# 1.96 is the conventional two-sided 95% mark. NOT tuned to make the live result flip: at the value
# that prompted this (0.045 sigma) any threshold above ~0.05 gives the same answer, and the number
# is quoted in the output so a reader can judge it rather than inherit it.
CLAIM2_MIN_SIGMA = 1.96

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core_seat import seat_root  # noqa: E402

ROOT = seat_root()

# HONOUR A --log ARGUMENT, because a probe nothing can aim is a probe nothing can check. Its three
# structural claims all PASS on the live log, and a check that has only ever been seen passing is
# indistinguishable from one that cannot fail. Pointing it at a planted log is how the control in
# bin/tests/test_observation_probe.py makes each claim go red on demand.
def _log_path():
    for i, a in enumerate(sys.argv):
        if a == "--log" and i + 1 < len(sys.argv):
            p = Path(sys.argv[i + 1]).expanduser()
            if not p.is_file():
                sys.exit("REFUSING: --log %s is not a file. Not falling back to the live log — "
                         "that would report a number for a target you did not ask about." % p)
            return p
    return ROOT / ".claude" / "state" / "reply-observations.jsonl"


LOG = _log_path()


def load():
    rows = []
    if not LOG.is_file():
        return rows
    for line in LOG.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue          # a truncated tail is not a reason to report nothing
    return rows


def collapse(rows):
    """Exactly tally_distinct's key: one claim per (turn, kind, matched), sourced if ANY row was."""
    claims = {}
    for r in rows:
        k = (r.get("turn"), r.get("kind", "?"), str(r.get("matched")))
        c = claims.setdefault(k, {"idxs": [], "sourced": False, "kind": r.get("kind", "?")})
        c["idxs"].append(r.get("index", 0))
        if r.get("sourced"):
            c["sourced"] = True
    return claims


def rate(items, pred):
    sel = [x for x in items if pred(x)]
    if not sel:
        return None, 0
    n = sum(1 for x in sel if x["sourced"])
    return 100.0 * n / len(sel), len(sel)


def main() -> int:
    rows = load()
    claims = collapse(rows)
    out = {"rows": len(rows), "claims": len(claims), "log": str(LOG)}

    print("=== observation log — re-derived, not recalled ===\n")
    print("  log   : %s" % LOG)
    print("  rows  : %d raw   claims: %d distinct" % (len(rows), len(claims)))

    if not rows:
        print("\n  REFUSING: the log is empty. Every rate below would be 0/0, and a probe that")
        print("  reports health on no data is the defect this file exists to end.")
        return 2

    raw_k = collections.Counter(r.get("kind", "?") for r in rows)
    dis_k = collections.Counter(c["kind"] for c in claims.values())
    print("\n  inflation by class (raw rows per distinct claim)")
    infl = {}
    for k in sorted(raw_k, key=lambda x: -raw_k[x]):
        f = raw_k[k] / dis_k[k]
        infl[k] = round(f, 3)
        print("    %-22s raw %4d  distinct %4d  = %.2fx" % (k, raw_k[k], dis_k[k], f))
    out["inflation"] = infl

    overall = len(rows) / len(claims)
    out["inflation_overall"] = round(overall, 3)

    # the position bias, before and after collapsing — the claim the commit messages rest on
    print("\n  position bias, duration_claim")
    dur_rows = [r for r in rows if r.get("kind") == "duration_claim"]
    b0, n0 = rate([{"sourced": r.get("sourced")} for r in dur_rows if r.get("index") == 0], lambda x: True)
    b1, n1 = rate([{"sourced": r.get("sourced")} for r in dur_rows if r.get("index", 0) > 0], lambda x: True)
    a0, m0 = rate([c for c in claims.values() if c["kind"] == "duration_claim"
                   and max(c["idxs"]) == 0], lambda x: True)
    a1, m1 = rate([c for c in claims.values() if c["kind"] == "duration_claim"
                   and max(c["idxs"]) > 0], lambda x: True)
    before = None if (b0 is None or b1 is None) else b1 - b0
    after = None if (a0 is None or a1 is None) else a1 - a0
    print("    RAW rows       index=0 %s (n=%d)   index>0 %s (n=%d)   gap %s"
          % (_p(b0), n0, _p(b1), n1, _pp(before)))
    print("    AFTER collapse only-0 %s (n=%d)   spans>0 %s (n=%d)   gap %s"
          % (_p(a0), m0, _p(a1), m1, _pp(after)))
    out["gap_raw"] = None if before is None else round(before, 2)
    out["gap_collapsed"] = None if after is None else round(after, 2)

    print("\n=== structural claims the write-ups rest on ===")
    ok = True

    c1 = overall > 1.0
    print("  %s inflation > 1.0 (%.2fx) — collapsing chunks was not a no-op"
          % ("PASS " if c1 else "FAIL ", overall))
    ok &= c1

    if before is None or after is None:
        print("  SKIP  position bias — not enough duration_claim rows in both arms to compare")
    else:
        # POWER FLOOR ON CLAIM 2 (2026-08-12, found by core-finance's DOSE 29).
        #
        # This was `abs(after) < abs(before)`: two differences-of-proportions compared with a bare
        # `<`, no n, no standard error, no refusal at small n. That is precisely the rule
        # bin/tier-b-power.py exists to condemn in gate_tier_b — "passes on ANY margin, with no
        # test that the margin exceeds chance" — committed one directory away, by the tool whose
        # job is judging whether our other measurements can be trusted.
        #
        # It was live-RED off noise. Measured on finance's log: raw gap +14.9pp (n=25/67),
        # collapsed +15.6pp (n=8/57), so `abs(15.6) < abs(14.9)` is False and the probe printed
        # "FAIL — a quoted conclusion needs re-reading". The two gaps differ by 0.7pp against a
        # combined SE of 15.6pp: 0.045 sigma. Demonstrated, not merely computed — planted logs
        # identical but for ONE row's `sourced` flag flipped the verdict PASS/FAIL (rc 0/1). The
        # conclusion was a property of one observation.
        #
        # SAME SHAPE AS THE MIN_PRE_N FIX in measure-contract-fitness the same day, and answered
        # the same way: a comparison too weak to support a verdict returns INDETERMINATE rather
        # than a verdict. INDETERMINATE does NOT fail the run — a FAIL here means "a quoted
        # conclusion needs re-reading", a real alarm, and firing it from noise is how an alarm
        # stops being read. Silent is not an option either, so it prints the sigma it could not
        # reach.
        #
        # SEs treat the two gaps as independent, which they are not (the collapsed arm is built
        # from the same rows). That INFLATES the combined SE and so refuses MORE often — the
        # conservative direction for a refusal test, and stated here rather than left as a hidden
        # assumption.
        def _se(p, n):          # SE of a proportion expressed in percentage points
            if not n or p is None:
                return None
            q = p / 100.0
            return 100.0 * math.sqrt(max(q * (1 - q), 0.0) / n)

        se_b = _se(b0, n0), _se(b1, n1)
        se_a = _se(a0, m0), _se(a1, m1)
        if None in se_b or None in se_a:
            print("  SKIP  position bias — an arm has no rows, so no SE is computable")
        else:
            se_before = math.hypot(*se_b)
            se_after = math.hypot(*se_a)
            se_diff = math.hypot(se_before, se_after)
            margin = abs(before) - abs(after)          # positive = collapsing shrank the gap
            sigma = abs(margin) / se_diff if se_diff else float("inf")
            if sigma < CLAIM2_MIN_SIGMA:
                print("  INDET position bias — |gap| moved %.1fpp -> %.1fpp, a %.1fpp margin "
                      "against a combined SE of %.1fpp (%.2f sigma, need %.1f). Too few rows "
                      "(n=%d/%d raw, %d/%d collapsed) to say whether tally_distinct neutralises "
                      "it. NOT counted as pass or fail."
                      % (before, after, margin, se_diff, sigma, CLAIM2_MIN_SIGMA,
                         n0, n1, m0, m1))
                out["claim2"] = "indeterminate"
                out["claim2_sigma"] = round(sigma, 3)
            else:
                c2 = abs(after) < abs(before)
                print("  %s |gap after| < |gap before|  (%.1fpp -> %.1fpp, %.2f sigma) — "
                      "tally_distinct neutralises it"
                      % ("PASS " if c2 else "FAIL ", before, after, sigma))
                out["claim2"] = "pass" if c2 else "fail"
                out["claim2_sigma"] = round(sigma, 3)
                ok &= c2

    c3 = len(claims) > 0
    print("  %s the log carries at least one distinct claim" % ("PASS " if c3 else "FAIL "))
    ok &= c3

    if "--json" in sys.argv:
        print("\n" + json.dumps(out, indent=2))

    print("\n=== %s ===" % ("PASS" if ok else "FAIL — a quoted conclusion needs re-reading"))
    return 0 if ok else 1


def _p(v):
    return "  n/a " if v is None else "%5.1f%%" % v


def _pp(v):
    return " n/a" if v is None else "%+.1fpp" % v


if __name__ == "__main__":
    sys.exit(main())
