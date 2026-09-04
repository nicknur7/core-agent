#!/usr/bin/env python3
"""THE SPLIT MUST ANCHOR ON WHEN NICK SAID IT, NOT WHEN WE MINED IT. 89% OF ROWS DISAGREE, BY UP TO
35 DAYS.

AUTHORED AND RUN ON core-finance. NOT INSTALLED HERE — finance is a puller and bin/tests/ is
baseline-shared, so per the routing life decided 2026-08-12 the source is returned on the bus and
life commits it. Intended install path: bin/tests/test_recurrence_anchors_on_session_date.py

Fences postmortem 4 of 9 in measure-contract-fitness.py (the `Fix 0` / OBS_FILTER block). Its words:

    "Fix 0 (contract-binding proposal, 2026-06-09) — recurrence counts ONLY real, distinct Nick
     corrections, anchored to when they HAPPENED:
       - window splits on session_date (when said), not created_at (when mined) — batch re-mining
         was dumping old corrections into 'post'
       - hook feedback / tool-result blobs excluded (mined-as-Nick pollution)
       - DISTINCT (text, day) — re-mined duplicates counted once"

Three separate corrections in one comment block, none of them tested.

WHY THE ANCHOR IS THE ONE THAT MATTERS — measured, not assumed, across all five orgs:

    org   rows   session_date <> created_at::date   max gap
    1     1279            1067                      34 days
    2      301             161                      31 days
    3      205             122                      32 days
    4       36              32                      35 days
    5      120              86                      21 days

**89% of finance's rows disagree, by up to 35 days.** Anchoring on `created_at` would relocate a
correction by up to five weeks relative to the split — and always in the same direction, since
mining happens after the fact. A batch re-mine dumps a month of old corrections into "post", which
inflates post_rate, which reads as the contract FAILING. Load-bearing on every seat, not a
theoretical nicety.

WHY THIS IS NOT A GREP. core-life wrote source-text checks against this same file twice on
2026-08-12 and both passed against a neutered predicate, because the string survived in a comment.
Leg 1 captures the SQL **actually issued to a cursor**. A comment cannot reach a cursor.

STRUCTURE
  1. CONTROL — the recurrence query was actually issued (else everything below is vacuous).
  2. ANCHOR (runtime SQL) — the window predicate uses COALESCE(session_date, created_at::date) and
     does NOT split on bare created_at.
  3. POLLUTION FILTER (runtime SQL) — the four documented exclusions are present in the issued SQL.
  4. DEDUPE (runtime SQL) — the count is DISTINCT over (text, day), not a raw row count.
  5. LOAD-BEARING (live DB, read-only) — session_date actually diverges from created_at on this
     seat, so the anchor choice is doing work here rather than being decorative. SKIPS LOUDLY if a
     seat has no divergence; a skip is never counted as a pass.
  6. MUTATION, in-band — swap the anchor to bare created_at in a temp copy and confirm leg 2 flips.

Read-only. No writes, no live state, temp files via TemporaryDirectory.

Run: python3 tasks/si-verification/probes/test_recurrence_anchors_on_session_date.py
"""
import importlib.util
import re
import sys
import tempfile
from datetime import date
from pathlib import Path


def _root() -> Path:
    p = Path(__file__).resolve()
    for cand in p.parents:
        if (cand / "scheduling" / "claude-si").is_dir() and (cand / "bin").is_dir():
            return cand
    raise SystemExit("SKIP - could not locate Core root")


ROOT = _root()
MCF = ROOT / "scheduling" / "claude-si" / "measure-contract-fitness.py"

# The four pollution shapes the postmortem names, as they appear in OBS_FILTER.
POLLUTION = ["<details>", "Stop hook feedback", "Tool result", "⛔"]


class RecordingCursor:
    """Records SQL and returns empty results so the measurement runs but does nothing."""

    def __init__(self):
        self.sql = []

    def execute(self, q, *a, **k):
        self.sql.append(" ".join(str(q).split()))

    def _last(self):
        return self.sql[-1] if self.sql else ""

    def fetchall(self):
        # A contract row is REQUIRED here. v1 returned [] unconditionally, so the per-contract loop
        # never ran and no recurrence query was issued.
        #
        # FIVE-TUPLE, not three (2026-08-31). main()'s learned_contracts SELECT grew
        # `required_shape, checkable` (line 1049) after this fixture was written, and
        # `for situation, labels, created, _req_shape, _checkable in contracts:` (line 1062) then
        # raised ValueError: not enough values to unpack on every run — caught by
        # capture_recurrence_sql's blanket `except Exception: pass`, so main() died silently
        # before ever reaching OBS_FILTER (line ~1113) and every leg past the CONTROL/bounds-query
        # check failed on empty SQL. Same failure SHAPE this file already documents once (the
        # fetchone() 1-tuple/2-tuple mismatch) — the contracts row drifted a second time and this
        # fixture wasn't updated with it. Empty lists for required_shape/checkable are a real,
        # valid contract shape (classify_contract_enforcement treats `required_shape or []` /
        # `checkable or []`), so this is not just "unblock the crash", it's a legitimate stub.
        if "learned_contracts" in self._last():
            return [("probe-contract", ["some-label"], date(2026, 7, 1), [], [])]
        return []

    def fetchone(self):
        # SHAPE MATTERS, not just value. main() unpacks the corpus-bounds query as a PAIR
        # (`obs_min, obs_max = cur.fetchone()`); v2 returned a 1-tuple for everything and main()
        # died with "not enough values to unpack (expected 2, got 1)" immediately after the bounds
        # query — before the contracts loop, so OBS_FILTER was never issued.
        if "min(" in self._last() and "max(" in self._last():
            return (date(2026, 6, 1), date(2026, 8, 1))
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
    spec = importlib.util.spec_from_file_location("mcf_anchor_probe", path or MCF)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def capture_recurrence_sql(module) -> list:
    """Statements main() issues against pattern_observations.

    OBS_FILTER lives in main(), NOT in _measure_si_artifacts — the first version of this probe
    drove the artifacts path and captured nothing. Verified with awk over the source rather than
    assumed the second time.

    dry_run=True skips the OUT_FILE write; OUT_FILE is ALSO redirected to a temp path so a
    behaviour change in that guard cannot make this probe write live state.
    """
    cur = RecordingCursor()
    module.connect_corebrain = lambda *a, **k: FakeConn(cur)
    module._gates = lambda *a, **k: {}
    module._unenforceable = lambda *a, **k: set()
    with tempfile.TemporaryDirectory() as td:
        module.OUT_FILE = Path(td) / "contract-fitness.json"
        try:
            module.main(dry_run=True)
        except Exception:
            pass
    return [q for q in cur.sql if "pattern_observations" in q]


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

    print("=== recurrence must anchor on session_date (when said), not created_at (when mined) ===\n")
    if not MCF.is_file():
        print("  SKIP - measure-contract-fitness.py absent")
        return 0

    m = load()
    stmts = capture_recurrence_sql(m)
    joined = " || ".join(stmts)

    check("CONTROL - a pattern_observations query was actually issued",
          bool(stmts),
          "nothing reached the cursor; every assertion below would pass vacuously")

    # ---- 2. THE ANCHOR ----------------------------------------------------------------------
    check("the window predicate uses COALESCE(session_date, created_at::date)",
          "COALESCE(session_date, created_at::date)" in joined,
          "issued SQL does not coalesce on session_date: %s" % (stmts[:1] or "<none>"))

    # The failure mode is splitting on created_at ALONE. created_at inside the COALESCE is correct,
    # so strip the coalesce before looking for a bare comparison against it.
    stripped = joined.replace("COALESCE(session_date, created_at::date)", "<ANCHOR>")
    bare = re.search(r"created_at\s*(::date)?\s*[<>=]", stripped)
    check("the window does NOT split on bare created_at",
          bare is None,
          "found a bare created_at comparison outside the COALESCE: %r — batch re-mining would "
          "dump old corrections into the post window" % (bare.group(0) if bare else None))

    # ---- 3. POLLUTION FILTER ----------------------------------------------------------------
    missing = [t for t in POLLUTION if t not in joined]
    check("all four documented pollution shapes are excluded in the issued SQL",
          not missing,
          "missing from the filter: %r — these are mined-as-Nick blobs (hook feedback, tool "
          "results) that would count as real corrections" % missing)

    # ---- 4. DEDUPE --------------------------------------------------------------------------
    check("the recurrence count is DISTINCT over (text, day), not raw rows",
          "count(DISTINCT" in joined,
          "no DISTINCT count in the issued SQL — re-mined duplicates would each count as a "
          "separate correction: %s" % (stmts[:1] or "<none>"))

    # ---- 5. LOAD-BEARING, live and read-only ------------------------------------------------
    print("\n--- is the anchor choice doing work on THIS seat? (read-only) ---")
    try:
        sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))
        from _env import connect_corebrain as real_connect  # noqa: E402
        con = real_connect()
        c = con.cursor()
        c.execute("SELECT count(*), "
                  "count(*) FILTER (WHERE session_date IS DISTINCT FROM created_at::date), "
                  "coalesce(max(abs(created_at::date - session_date)),0) "
                  "FROM pattern_observations "
                  "WHERE org_id = current_setting('app.current_org_id', true)::bigint")
        total, differ, gap = c.fetchone()
        con.close()
        if total == 0:
            skip("session_date diverges from created_at on this seat",
                 "no pattern_observations rows for this org — nothing to observe")
        elif differ == 0:
            skip("session_date diverges from created_at on this seat",
                 "all %d rows have session_date == created_at::date, so the anchor choice is "
                 "UNOBSERVABLE here. Measured 2026-08-12: org 1 had 1067 of 1279 diverging with a "
                 "34-day maximum. Not a pass." % total)
        else:
            check("the anchor is load-bearing here (session_date diverges from created_at)",
                  differ > 0,
                  "unreachable")
            print("          %d of %d rows diverge; largest gap %d days — that is how far a "
                  "correction would move across the split under the wrong anchor."
                  % (differ, total, gap))
    except Exception as exc:
        skip("session_date diverges from created_at on this seat",
             "could not read pattern_observations: %s" % str(exc)[:80])

    # ---- 6. MUTATION ------------------------------------------------------------------------
    print("\n--- mutation control: leg 2 must be able to FAIL ---")
    src = MCF.read_text()
    old = "COALESCE(session_date, created_at::date) {op} %s"
    new = "created_at::date {op} %s"
    if old not in src:
        check("mutation site located", False,
              "could not find the anchored window predicate; leg 2's green is unverified")
    else:
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "measure-contract-fitness.py"
            mp.write_text(src.replace(old, new))
            mut_joined = " || ".join(capture_recurrence_sql(load(mp)))
            # Assert the SAME WAY leg 3 does: strip the anchor, then look for a BARE created_at
            # comparison. A blanket "coalesce absent" check could never pass — the corpus-BOUNDS
            # query legitimately contains its own COALESCE and is not the statement being mutated.
            # v1 of this assertion did exactly that and failed while the mutation was working fine.
            mut_stripped = mut_joined.replace("COALESCE(session_date, created_at::date)", "<ANCHOR>")
            mut_bare = re.search(r"created_at\s*(::date)?\s*[<>=]", mut_stripped)
            check("swapping the anchor to bare created_at is DETECTED",
                  mut_bare is not None,
                  "the mutant issued no bare created_at comparison, so leg 3 is not actually "
                  "observing the anchor: %s" % mut_joined[:200])

    print("\n=== Results: %d passed, %d failed, %d skipped ===" % (p, f, s))
    if s:
        print("A SKIP IS NOT A PASS. The skipped assertion was not evaluated on this seat.")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
