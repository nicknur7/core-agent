#!/usr/bin/env python3
"""db_absent() draws the one line a test may SKIP on: "there is no database to talk to". Every
other failure — a schema error, a cancelled statement, a REFUSED login — comes from a reachable
database and is a defect. Codex review of the P0 repair (2026-09-04, pass 2) found the first cut
put every OperationalError on the absent side, so "password authentication failed" SKIPped.

Synthetic exception classes: the predicate reads the class NAME and message, so this runs on a
seat with no psycopg2 installed, which is exactly where it matters.
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
from _env import db_absent, describe_db_failure  # noqa: E402

p = f = 0


def check(label, cond, detail=""):
    global p, f
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
    p, f = (p + 1, f) if cond else (p, f + 1)


def exc(cls_name: str, msg: str) -> Exception:
    return type(cls_name, (Exception,), {})(msg)


def main() -> int:
    print("test_db_absent_predicate")
    absent = [
        exc("OperationalError", 'connection to server at "localhost" (::1), port 5432 failed: Connection refused'),
        exc("OperationalError", 'FATAL:  database "no_such_db_zz" does not exist'),
        exc("OperationalError", "could not connect to server: No such file or directory"),
        ModuleNotFoundError("No module named 'psycopg2'", name="psycopg2"),
        ModuleNotFoundError("No module named 'psycopg2._psycopg'", name="psycopg2._psycopg"),
    ]
    present = [
        exc("OperationalError", 'FATAL:  password authentication failed for user "core_life"'),
        exc("OperationalError", 'FATAL:  no pg_hba.conf entry for host "10.0.0.5", user "x", database "corebrain"'),
        exc("OperationalError", 'FATAL:  permission denied for database "corebrain"'),
        exc("UndefinedTable", 'relation "si_artifacts" does not exist'),
        exc("QueryCanceled", "canceling statement due to statement timeout"),
        exc("InsufficientPrivilege", "permission denied for table tenants"),
        exc("ProgrammingError", "syntax error at or near"),
        ModuleNotFoundError("No module named 'si_project'", name="si_project"),
        KeyError("org_id"),
    ]
    for e in absent:
        check(f"ABSENT: {e.__class__.__name__}: {str(e)[:60]}", db_absent(e) is True)
    for e in present:
        check(f"NOT absent (a defect): {e.__class__.__name__}: {str(e)[:60]}", db_absent(e) is False)
    # The description printed beside the verdict must not contradict it (Fable, pass 3): a refused
    # login used to describe as "corebrain unreachable — check brew services".
    for e in present[:3]:
        d = describe_db_failure(e)
        check(f"description agrees: refused login is not 'unreachable': {str(e)[8:48]}",
              "unreachable" not in d and "REFUSED the login" in d, d)
    d = describe_db_failure(absent[0])
    check("description agrees: connection refused IS 'unreachable'", "unreachable" in d, d)
    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
