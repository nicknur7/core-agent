#!/usr/bin/env python3
"""THE SPLIT DATE MUST BE THE IMMUTABLE ONE. WHEN IT WAS NOT, THE FILE REPORTED SUCCESS BY
CONSTRUCTION.

AUTHORED AND RUN ON core-finance. NOT INSTALLED HERE — finance is a puller and bin/tests/ is
baseline-shared, so per the routing life decided 2026-08-12 the source is returned on the bus and
life commits it. Intended install path: bin/tests/test_split_uses_installed_at.py

Fences postmortem 3 of 9 in measure-contract-fitness.py. Its own words:

    "`installed_at`, NOT `updated_at`. THIS SPLIT IS THE WHOLE MEASUREMENT.
     Fable found this 2026-08-05 and it invalidated every si_artifact verdict in the file.
     `updated_at` is set to now() by si_project.upsert()'s ON CONFLICT clause, and the pipeline
     re-installs every live artifact at every close ... So splitting at `updated_at` reset the
     'after it shipped' window to ~zero on every close, `post` came back 0.0 for everything, and
     the verdict cascade below reads `post == 0 and fires > 0` as GRADUATED. The file therefore
     reported success BY CONSTRUCTION: 6 GRADUATED, 5 GRADUATED-UNPROVEN, 0 NOT-BINDING."

The highest-blast-radius entry in the file: not a skewed number, a measurement that could only ever
return success.

WHY THIS IS NOT A GREP, WHICH MATTERS HERE MORE THAN USUAL
-----------------------------------------------------------
core-life wrote a source-text check for this same file TWICE on 2026-08-12 and both passed against a
neutered predicate, because the string survived in a comment. A substring test cannot distinguish an
APPLIED clause from a MENTIONED one.

So leg 1 captures the SQL **actually issued to the cursor at runtime**. A comment cannot reach a
cursor. That is strictly stronger than reading the file, and it is the specific weakness that beat
life twice tonight.

TWO LEGS, DELIBERATELY DIFFERENT IN KIND
-----------------------------------------
1. WHICH COLUMN IS ASKED FOR — runtime SQL capture via a fake connection. Portable to every seat.
2. IS THAT COLUMN ACTUALLY IMMUTABLE — read-only assertion against the live DB, that rows which
   have been revised carry installed_at EARLIER than updated_at. Leg 1 proves the code reads the
   intended column; leg 2 proves the intended column still has the property the fix depended on.
   Neither implies the other: the column could be correct and the schema could have started
   churning it, and leg 1 would still be green.

LEG 2 CAN SKIP, AND SKIPS LOUDLY WHEN IT DOES. Measured 2026-08-12 at TIMESTAMP precision:

    org              rows   installed<updated   installed>updated   equal
    1 (life)           61          61                   0             0
    2 (business)       30          27                   0             3
    3 (school)          8           6                   0             2
    4 (finance)         4           4                   0             0

Observable on every seat, and `installed_at > updated_at` is zero fleet-wide — the immutability the
fix depends on holds everywhere it can be checked. Leg 2 still carries a SKIP branch for an org with
no rows or no revised rows; a skip is reported as its own outcome and never counted as a pass,
because life's `test_trajectory_gate.sh` had NEVER run while being counted green.

**A CORRECTION TO THIS DOCSTRING'S OWN FIRST DRAFT, kept rather than quietly edited.** It claimed
"on finance the property is UNOBSERVABLE — installed_at == updated_at on every row," and predicted
leg 2 would SKIP here. It does not; it passes. The error was measuring `installed_at::date <>
updated_at::date` and generalising a DATE-precision result to a claim about immutability at any
precision. Finance's four rows differ by fifteen seconds — 22:38:57 against 22:39:12 — which
`::date` cannot see.

That is the same shape as the postmortem this file fences: a measurement taken at the wrong
resolution, reported as a property. Caught by running leg 2 and noticing it passed where the
docstring said it would skip — i.e. by execution disagreeing with prose, which is the only thing
that has caught anything tonight.

Read-only. No writes, no live state touched, temp files via TemporaryDirectory (a previous probe of
mine leaked one dir per run via mkdtemp — caught by claim-sweeping my own docstring, 2026-08-12).

Run: python3 tasks/si-verification/probes/test_split_uses_installed_at.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path


def _root() -> Path:
    p = Path(__file__).resolve()
    for cand in p.parents:
        if (cand / "scheduling" / "claude-si").is_dir() and (cand / "bin").is_dir():
            return cand
    raise SystemExit("SKIP - could not locate Core root")


ROOT = _root()
MCF = ROOT / "scheduling" / "claude-si" / "measure-contract-fitness.py"


class RecordingCursor:
    """Records every SQL string executed and returns empty result sets.

    Empty results make _measure_si_artifacts fall straight out of its per-artifact loop, so the
    function runs its real query-building code and then does nothing. The point is the SQL, not
    the rows.
    """

    def __init__(self):
        self.sql = []

    def execute(self, q, *a, **k):
        self.sql.append(" ".join(str(q).split()))

    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)

    def close(self):
        pass


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


def load(path=None):
    sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
    sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))
    spec = importlib.util.spec_from_file_location("mcf_split_probe", path or MCF)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def capture_artifact_sql(module) -> list:
    """Run the si_artifacts measurement against a fake connection; return the SQL it issued."""
    from datetime import date
    cur = RecordingCursor()
    module.connect_corebrain = lambda *a, **k: FakeConn(cur)
    try:
        module._measure_si_artifacts(date(2026, 6, 1), date(2026, 8, 1))
    except Exception:
        pass  # downstream work is irrelevant; we only need the statements it issued
    return [q for q in cur.sql if "si_artifacts" in q]


def main() -> int:
    p = f = s = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    def skip(label, why):
        nonlocal s
        s += 1
        print("  SKIP  " + label + "\n          " + why)

    print("=== the si_artifacts split must use installed_at, not the churned updated_at ===\n")
    if not MCF.is_file():
        print("  SKIP - measure-contract-fitness.py absent")
        return 0

    # ---- LEG 1: which column does it actually ASK the database for? -------------------------
    m = load()
    stmts = capture_artifact_sql(m)
    joined = " || ".join(stmts)

    check("CONTROL - the si_artifacts query was actually issued",
          bool(stmts), "no statement mentioning si_artifacts reached the cursor; leg 1 proves "
                       "nothing and every result below is vacuous")
    check("the issued SQL selects installed_at",
          "installed_at" in joined, "issued: %s" % (stmts or "<none>"))
    check("the issued SQL does NOT split on updated_at",
          "updated_at" not in joined,
          "issued SQL references updated_at, which si_project.upsert() churns to now() on every "
          "close — the exact defect Fable found 2026-08-05: %s" % (stmts or "<none>"))

    # ---- LEG 2: is installed_at actually immutable on this seat's data? ---------------------
    print("\n--- leg 2: is installed_at genuinely immutable here? (read-only, live DB) ---")
    churned = None
    try:
        sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))
        from _env import connect_corebrain as real_connect  # noqa: E402
        con = real_connect()
        c = con.cursor()
        c.execute("SELECT count(*) FILTER (WHERE installed_at < updated_at), "
                  "count(*) FILTER (WHERE installed_at > updated_at), count(*) "
                  "FROM si_artifacts WHERE org_id = current_setting('app.current_org_id', true)::bigint")
        churned = c.fetchone()
        con.close()
    except Exception as exc:
        churned = None
        skip("installed_at precedes updated_at on revised rows",
             "could not read si_artifacts: %s" % str(exc)[:80])

    if churned is not None:
        earlier, later, total = churned
        if total == 0:
            skip("installed_at precedes updated_at on revised rows",
                 "this org has no si_artifacts rows — nothing to observe")
        elif earlier == 0 and later == 0:
            skip("installed_at precedes updated_at on revised rows",
                 "all %d rows on this seat have installed_at == updated_at (no artifact has been "
                 "revised since install), so immutability is UNOBSERVABLE here. Measured "
                 "2026-08-12: org 1 has 42 of 61 churned at revision 525 and IS a valid seat for "
                 "this leg; org 4 has 0 of 4. Not a pass." % total)
        else:
            check("installed_at is never LATER than updated_at (it does not churn)",
                  later == 0,
                  "%d row(s) have installed_at AFTER updated_at — installed_at is being written "
                  "post-install, which breaks the immutability the fix depends on" % later)

    # ---- MUTATION CONTROL: leg 1 must be able to fail ---------------------------------------
    print("\n--- mutation control: leg 1 must be able to FAIL ---")
    src = MCF.read_text()
    # ANCHOR ON THE COLUMN, NOT THE WHOLE SELECT (2026-08-12). This matched the entire statement as
    # one literal, so adding a column to it — `COALESCE(quarantined, false)`, which also wrapped the
    # line — made the mutation site unfindable and the control unverifiable. The control failed
    # loudly rather than passing silently, which is the behaviour it was built for; but the anchor
    # was brittle for no benefit. `installed_at::date` is the thing leg 1 actually observes, and it
    # survives any reformatting or extra column that does not touch the split's own source.
    old = "installed_at::date"
    new = "updated_at::date"
    if old not in src:
        check("mutation site located", False,
              "could not find the si_artifacts SELECT; leg 1's greens are unverified")
    else:
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "measure-contract-fitness.py"
            mp.write_text(src.replace(old, new, 1))
            mut_stmts = capture_artifact_sql(load(mp))
            mut_joined = " || ".join(mut_stmts)
            check("swapping the SELECT to updated_at is DETECTED by leg 1",
                  "updated_at" in mut_joined and "installed_at" not in mut_joined,
                  "mutant issued %s — leg 1 is not actually observing the column choice"
                  % (mut_stmts or "<none>"))

    print("\n=== Results: %d passed, %d failed, %d skipped ===" % (p, f, s))
    if s:
        print("A SKIP IS NOT A PASS. The skipped assertion was not evaluated on this seat.")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
