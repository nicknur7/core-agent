#!/usr/bin/env python3
"""A CHECK NOBODY RUNS IS A CHECK THAT DOES NOT EXIST.

`scheduling/brain-pg/verify-schema-checks.py` compares the CHECK-constraint allow-lists in
`schema.sql` against the live `corebrain` constraints. Its docstring states the stake plainly: *"a
fresh Core provisioned from schema.sql must"* accept what the live one accepts.

It was RED for five days and nobody saw it. Migration `2026-08-05-workflow-steps.sql` widened
`entities.kind` to include `Workflow` on the live database and never updated `schema.sql`, so a Core
built from that file would have REJECTED the 12 rows that already carry it. `entity_edges.edge_type`
had the same gap for `next_step`.

The drift is not the interesting part — the checker caught it exactly as designed, the moment it was
run. **Nothing ran it.** `grep -rl verify-schema-checks` across every shell script, hook, settings
file and JSON config returned only the file itself. It was wired to nothing, so its verdict went
nowhere, so being correct bought nothing.

That is the same shape as the SKIP found in `test_trajectory_gate.sh` an hour earlier, one level
further out: there a check ran and its refusal was miscounted as a pass; here a check never ran at
all and its silence was never counted as anything. Both were invisible for the same reason — no
surface reported them.

ON THE SKIP PATH. If the database is unreachable this reports SKIP rather than passing. `run-all.sh`
treats SKIP as not-green on purpose, which is right here: an unverifiable schema is not a verified
one. On a fork with no brain database that reads as "did not run", which is the honest answer, not a
failure claim.

Run: python3 bin/tests/test_schema_matches_live.py
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
CHECKER = ROOT / "scheduling" / "brain-pg" / "verify-schema-checks.py"


def main() -> int:
    if not CHECKER.is_file():
        print("  SKIP — %s not present on this Core" % CHECKER.name)
        return 0

    r = subprocess.run([sys.executable, str(CHECKER)], capture_output=True, text=True, timeout=180)
    out = (r.stdout or "") + (r.stderr or "")

    # An unreachable database is an environment fact, not a schema verdict. Distinguishing the two
    # matters: reporting "schema drift" when psql simply is not running would send the next reader
    # to edit schema.sql over a connection problem.
    if any(s in out for s in ("could not connect", "Connection refused", "does not exist",
                              "OperationalError", "psql: error")):
        print("  SKIP — corebrain unreachable; the schema could not be verified")
        print("         (not a pass: an unverifiable schema is not a verified one)")
        print(out.strip()[:300])
        return 0

    print(out.rstrip())
    if r.returncode == 0:
        print("\n  PASS  schema.sql's CHECK allow-lists match the live corebrain constraints")
        print("  ok    and this check now RUNS — it was wired to nothing until 2026-08-10")
        return 0

    print("\n  FAIL  schema.sql has drifted from the live database.")
    print("        A Core provisioned from schema.sql would reject rows the live one accepts.")
    print("        Fix schema.sql to match the migration that widened the live constraint —")
    print("        do NOT narrow the live database to match a stale file.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
