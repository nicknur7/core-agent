#!/usr/bin/env python3
"""Can THIS seat's own corpus support the instruments that read it? Answer per threshold.

WHY THIS EXISTS (2026-08-12, T017). Nick's ask: "each Core can build stuff and optimize from the
data it already has." The organs were already capable of it — a sweep for hardcoded org/seat values
across scheduling/claude-si/*.py and bin/*.py found none; everything resolves through get_org_id().
So the blocker was never the code. It is whether a given seat HAS enough corpus for its instruments
to say anything, and nobody had measured that per seat.

Measured 2026-08-12, and it inverted the assumption that life is the reference seat:

    seat       usable obs   labelled   obs days   artifacts*   cases
    life              547        547         66          21      195
    business          178        178         35          30      253
    school            191        168         46           8       81
    finance            35         35         12           4        4
    ops              107         52         16           0       61

business produces MORE live artifacts than life from a THIRD of the corpus, and school and ops hold
real corpora nobody had counted. No fleet-wide total would surface any of that: life's 547 dominates
every sum, which is precisely why Nick's ask is per-seat.

*THE ARTIFACT COLUMN CANNOT BE REPRODUCED BY THIS TOOL, and the reason is the point. si_artifacts is
org-scoped (si_artifacts_org_isolation, ALL commands) while pattern_observations is NOT
(SELECT qual=true) — the asymmetry that is all of T030. Those artifact numbers came from a superuser
psql session that bypasses RLS. Connecting as brain_app, a foreign seat's count returns 0 BY
CONSTRUCTION, and the first run of this tool duly reported "business: 178 observations, ZERO live
artifacts, loop not running." False, and the same wrong-ALARM shape core-business had caught in
audit-gap-check twenty minutes earlier. So --fleet now refuses to print the number for a foreign seat
rather than printing a zero that means "RLS refused".

WHAT THIS REFUSES TO DO. It does not compute a verdict, and it does not tell a thin seat to collect
more data. It reports each instrument's own stated floor against this seat's actual counts, so a
seat learns BEFORE running an instrument whether that instrument can speak here. The floors are read
from the instruments rather than restated: MIN_PRE_N and DECAY from measure-contract-fitness,
MIN_CANDIDATES from null-calibration, which derives it rather than picking it.

A zero here means "this seat cannot support that instrument", never "this seat is clean" — the
distinction the whole suite exists to keep.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

GEN = "learned-miner-v1"
SEATS = {1: "life", 2: "business", 3: "school", 4: "finance", 5: "ops"}


def _thresholds() -> dict:
    """Read the floors FROM the instruments. Restating them here would let this tool drift into
    reporting a bar nothing enforces — the copy-vs-shipped defect, in the file that measures it."""
    out = {}
    mcf = (REPO / "scheduling" / "claude-si" / "measure-contract-fitness.py").read_text()
    m = re.search(r"^MIN_PRE_N\s*=\s*(\d+)", mcf, re.M)
    out["MIN_PRE_N"] = int(m.group(1)) if m else None
    m = re.search(r"^DECAY\s*=\s*([\d.]+)", mcf, re.M)
    out["DECAY"] = float(m.group(1)) if m else None
    try:
        spec = importlib.util.spec_from_file_location("nc", REPO / "bin" / "null-calibration.py")
        nc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(nc)
        out["MIN_CANDIDATES"] = getattr(nc, "MIN_CANDIDATES", None)
    except Exception:
        out["MIN_CANDIDATES"] = None
    return out


def _counts(cur, org: int, own_org: int) -> dict:
    q = {}
    cur.execute("SELECT count(*) FROM pattern_observations WHERE org_id=%s "
                "AND detector_version=%s AND excluded_reason IS NULL", (org, GEN))
    q["usable"] = cur.fetchone()[0]
    # EMPTY STRING COUNTS AS UNLABELLED. `canonical_ask IS NOT NULL` is TRUE for '', so the obvious
    # test is wrong — and this file did the obvious thing and reported "life: 0 unlabelled" when 133
    # of its rows carried an empty ask (T021). The consumers all guard with `<> ''`
    # (ask_miner:113/:212, measure-contract-fitness:465); every NEW reader has to remember, and the
    # first new reader did not. Fixed at the source too — the miner now writes an explicit
    # excluded_reason instead of '' — but this test stays, because the old rows exist on every seat
    # until that seat backfills its own (writes are RLS-scoped, so life cannot do it for them).
    cur.execute("SELECT count(*) FROM pattern_observations WHERE org_id=%s "
                "AND detector_version=%s AND excluded_reason IS NULL "
                "AND (canonical_ask IS NULL OR canonical_ask = '')",
                (org, GEN))
    q["unlabelled"] = cur.fetchone()[0]
    # SI_ARTIFACTS IS ORG-SCOPED AND pattern_observations IS NOT. That asymmetry is the whole of
    # T030: pattern_observations_select has qual=true so reads cross orgs, while
    # si_artifacts_org_isolation scopes ALL commands on org_id. Connecting as brain_app, a foreign
    # seat's artifact count therefore comes back 0 BY CONSTRUCTION — not because the loop is idle.
    #
    # I nearly shipped that as a finding: "business has 178 observations and ZERO live artifacts,
    # loop not running." False, and in the same shape core-business had just caught in
    # audit-gap-check — a wrong ALARM, which costs a checker's time and slows the next real one.
    # The earlier count that said business had 30 came from a superuser psql session that bypasses
    # RLS; the tool does not have that and must not pretend the zero means anything.
    q["artifacts_readable"] = (org == own_org)
    if q["artifacts_readable"]:
        cur.execute("SELECT count(*) FROM si_artifacts WHERE org_id=%s AND active AND NOT quarantined",
                    (org,))
        q["artifacts"] = cur.fetchone()[0]
    else:
        q["artifacts"] = None
    # THE INCUMBENT'S ANCHOR, NOT A NEW ONE. measure-contract-fitness.py:150 counts against
    # COALESCE(session_date, created_at::date), and core-finance's probe 7 fences exactly that:
    # 1067 of 1279 rows diverge between the two anchors, largest gap 34 days. A readiness tool that
    # counted bare created_at would report a different corpus than the instrument it advises about —
    # the copy-vs-shipped defect, in a file whose whole job is to say what the instruments can see.
    cur.execute("SELECT count(DISTINCT COALESCE(session_date, created_at::date)) "
                "FROM pattern_observations "
                "WHERE org_id=%s AND detector_version=%s AND excluded_reason IS NULL", (org, GEN))
    q["obs_days"] = cur.fetchone()[0]
    # THE SIBLING OF THE QUERY THIS FILE ALREADY FIXED. The comment ~40 lines above records that
    # `canonical_ask IS NOT NULL` is TRUE for '', that this file "did the obvious thing" and
    # reported 0 unlabelled while 133 rows carried an empty ask (T021) — and the guard was added to
    # the unlabelled COUNT and not to this GROUP BY, in the same file.
    #
    # Measured on life 2026-08-13 before fixing: 587 empty-string rows, which came back as the
    # single largest "ask" in the breakdown at 14x the biggest real one (41). A reader of by_ask saw
    # a blank string reported as the most recurring ask on the seat.
    #
    # btrim, not `<> ''`, so a whitespace-only ask is excluded too — it is unlabelled by the same
    # argument and the `<> ''` form would admit it.
    cur.execute("SELECT canonical_ask, count(*) FROM pattern_observations WHERE org_id=%s "
                "AND detector_version=%s AND excluded_reason IS NULL AND canonical_ask IS NOT NULL "
                "AND btrim(canonical_ask) <> '' "
                "GROUP BY 1 ORDER BY 2 DESC", (org, GEN))
    q["by_ask"] = cur.fetchall()
    return q


def _report(seat: str, c: dict, t: dict) -> int:
    print(f"\ncorpus-readiness — {seat}")
    print(f"  usable observations : {c['usable']}   ({c['unlabelled']} unlabelled)")
    print(f"  distinct obs days   : {c['obs_days']}")
    if c["artifacts_readable"]:
        print(f"  live artifacts      : {c['artifacts']}")
    else:
        print("  live artifacts      : NOT READABLE FROM HERE — si_artifacts is org-scoped by RLS.")
        print("                        A zero here would mean 'RLS refused', never 'none exist'.")
        print("                        Run this ON that seat for a real number.")
    print(f"  distinct asks       : {len(c['by_ask'])}")

    blocked = []
    print("\n  INSTRUMENT READINESS (floors read from the instruments, not restated here)")

    n = t.get("MIN_PRE_N")
    if n is None:
        print("    contract-fitness   ? could not read MIN_PRE_N — treat as unknown, not as ready")
        blocked.append("MIN_PRE_N unreadable")
    else:
        ok = [a for a, k in c["by_ask"] if k >= n]
        print(f"    contract-fitness   needs >={n} observations per ask to interpret a rate")
        print(f"                       {len(ok)} of {len(c['by_ask'])} asks clear it")
        if not ok:
            blocked.append("no ask clears MIN_PRE_N — every verdict would read 'TOO FEW'")

    m = t.get("MIN_CANDIDATES")
    if m is None:
        print("    null-calibration   ? could not import MIN_CANDIDATES — unknown, not ready")
        blocked.append("MIN_CANDIDATES unreadable")
    else:
        print(f"    null-calibration   needs >={m} candidate split days for a percentile to mean anything")
        print(f"                       this seat has {c['obs_days']} distinct observation days")
        if c["obs_days"] < m:
            blocked.append(f"only {c['obs_days']} obs days < {m} — percentiles are NO VERDICT here")

    if c["unlabelled"]:
        pct = 100.0 * c["unlabelled"] / max(c["usable"], 1)
        print(f"\n  {c['unlabelled']} observations ({pct:.0f}%) carry no canonical_ask. They are corpus this")
        print("  seat HAS and cannot mine — labelling is the cheapest capacity available to it.")

    if c["usable"] and c["artifacts_readable"] and not c["artifacts"]:
        print(f"\n  *** {c['usable']} usable observations and ZERO live artifacts. This seat has a corpus")
        print("  and produces nothing from it — the loop is not running here. That is invisible in any")
        print("  fleet total, because one large seat dominates the sum.")
        blocked.append("corpus present, no artifacts — loop not running")

    print()
    if blocked:
        for b in blocked:
            print(f"  NOT READY: {b}")
        return 1
    print("  READY — every instrument's own floor is cleared by this seat's own corpus.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fleet", action="store_true",
                    help="report every seat (needs the cross-org SELECT that T030 may close)")
    a = ap.parse_args()

    from _env import connect_corebrain, describe_db_failure, get_org_id  # noqa: E402
    t = _thresholds()
    # GUARDED, NOT BARE. This call used to be unguarded and a missing/unreachable corebrain (every
    # fresh clone, before `make setup-brain`) crashed with a raw psycopg2 traceback instead of the
    # readiness report this tool exists to print. connect_or_skip() is for a close-path component
    # that can be silently skipped; this tool's whole job is to REPORT on readiness, so it still says
    # so on stdout (test_corpus_readiness.py's SKIP detection reads for "could not"+"corebrain" here)
    # rather than going silent.
    try:
        con = connect_corebrain()
    except Exception as exc:
        print(f"corpus-readiness: could not reach corebrain — {describe_db_failure(exc)}")
        return 1
    try:
        cur = con.cursor()
        own = get_org_id()
        if a.fleet:
            rc = 0
            for org, seat in SEATS.items():
                rc |= _report(seat, _counts(cur, org, own), t)
            print("\n  --fleet read every seat's rows. That works only while pattern_observations'")
            print("  SELECT policy is USING(true) — the open decision on T030. If it is scoped, this")
            print("  flag stops working and each seat must run this itself, which is the intent anyway.")
            return rc
        return _report(SEATS.get(own, f"org {own}"), _counts(cur, own, own), t)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
