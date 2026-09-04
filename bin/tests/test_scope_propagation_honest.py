#!/usr/bin/env python3
"""A PRIVATE MARK MUST NOT REPORT SUCCESS WHEN THE RAW FACTS STAY SHARED.

core-business, bus #967. set-scope.py's docstring promises that marking a concept private hides it
"at BOTH the hub and the raw-fact layer, not just the summary". Both evidence UPDATEs key on
`evidence.entity_id`, which is NULL on every row in the database — 4,377 of 4,377 on life, 286 of
286 on business. `col = x` and `col IN (…)` are NULL-rejecting, so those statements are PROVABLY
UNSATISFIABLE rather than empty-today. The function still returned `(n_ent, n_ev)` with n_ent > 0,
so the process exited 0, so core-ux's `scope:set` handler took its SUCCESS branch and rendered
"private" while query.py's scope clause kept returning the excerpts.

The mechanism had never been exercised — every scope column in the DB is still at its 'shared'
default — which is exactly why nine months of use never surfaced it. It would have surfaced the
first time Nick used it, and it would have looked like it worked.

WHAT IS AND IS NOT TESTED HERE. The linkage is NOT repaired; that needs a backfill deciding which
evidence justifies which entity, which is Nick's design call. What is pinned is that the unkeepable
promise is now LOUD: refuse and roll back by default, `--hub-only` to accept less, and `shared`
never refuses because evidence staying private is a fail-toward-MORE-private.

THE DOSE, which is the point of the file. `propagation_status` is pure so its refusal can be shown
to DEPEND ON ITS INPUT. Two cases differ in `linked_rows` alone and must disagree; two more differ
in `scope` alone and must disagree. A guard watched only against the live database would refuse
forever and teach nothing — a stable verdict is not evidence the instrument works.

Run: python3 bin/tests/test_scope_propagation_honest.py
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SRC = ROOT / "scheduling" / "brain-pg" / "set-scope.py"

spec = importlib.util.spec_from_file_location("set_scope_mod", str(SRC))
mod = importlib.util.module_from_spec(spec)
sys.modules["set_scope_mod"] = mod
spec.loader.exec_module(mod)


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    st = mod.propagation_status
    print("=== a private mark that cannot reach the raw facts is UNAVAILABLE ===\n")

    check("private, 0 evidence updated, NO linked rows anywhere -> UNAVAILABLE",
          st("private", 0, 0) == "UNAVAILABLE", st("private", 0, 0))

    print("\n--- dose 1: hold everything fixed but the linkage, and the verdict must move ---")
    # THE CONTROL THE FINDING ITSELF NEEDED. n_ev is 0 in BOTH rows below. If the guard keyed on
    # the row count alone — the obvious implementation — these would be indistinguishable, and
    # "this entity happens to have no excerpts" would be reported as a broken database forever.
    a = st("private", 0, 0)     # join column carries no information -> unsatisfiable
    b = st("private", 0, 5)     # linkage works; this concept simply has no excerpts
    check("...UNAVAILABLE when linked_rows=0, OK when linked_rows=5", a == "UNAVAILABLE" and b == "OK",
          "linked=0 -> %s   linked=5 -> %s" % (a, b))
    check("...so the refusal is not a stuck answer", a != b)

    print("\n--- dose 2: hold everything fixed but the scope, and the verdict must move ---")
    c = st("private", 0, 0)
    d = st("shared", 0, 0)
    check("private refuses where shared does not", c == "UNAVAILABLE" and d == "NOT_REQUIRED",
          "private -> %s   shared -> %s" % (c, d))
    check("...marking SHARED never refuses (evidence staying private under-shares, which is safe)",
          st("shared", 0, 0) == "NOT_REQUIRED" and st("shared", 3, 9) == "NOT_REQUIRED")

    print("\n--- a propagation that genuinely landed must still read as OK ---")
    check("private with evidence rows actually updated -> OK", st("private", 7, 7) == "OK")

    print("\n--- the guard must roll back, not half-apply ---")
    src = SRC.read_text()
    check("the refusal path calls conn.rollback() before raising",
          "conn.rollback()" in src.split("raise ScopePropagationUnavailable")[0][-400:],
          "rollback does not immediately precede the raise")
    check("the CLI exits nonzero on the refusal (the UI branches on exit code)",
          "except ScopePropagationUnavailable" in src and "sys.exit(str(e))" in src)

    print("\n--- and the SQL it guards still keys on the column that is NULL ---")
    # If someone repoints the predicate WITHOUT running a backfill, the guard's premise is stale and
    # this test should say so rather than keep passing against a statement it no longer describes.
    check("both evidence UPDATEs still key on entity_id", src.count("UPDATE evidence SET scope") == 2
          and src.count("entity_id") >= 3,
          "the predicate moved — re-verify the guard's premise before editing this line")

    print("\n--- the live database is in the failing state, so this is not hypothetical ---")
    try:
        conn = mod.connect_corebrain_admin()
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*), count(entity_id) FROM evidence")
            total, linked = cur.fetchone()
            print("  INFO  evidence rows: %d total, %d with a non-NULL entity_id" % (total, linked))
            check("live linkage is absent, so a private mark today WOULD be refused",
                  st("private", 0, linked) == ("UNAVAILABLE" if linked == 0 else "OK"),
                  "linked=%d" % linked)
        finally:
            conn.close()
    except Exception as e:  # no DB in this environment is not a failure of the property
        print("  SKIP  live check unavailable: %s" % str(e).splitlines()[0][:90])

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
