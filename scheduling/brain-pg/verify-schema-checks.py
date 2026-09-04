#!/usr/bin/env python3
"""B3 CI guard (federated-brain-plan-2026-07-07 §9).

Assert that the CHECK-constraint allowed-value sets in schema.sql match the LIVE
corebrain pg_constraint definitions. Prevents schema.sql from silently drifting
behind an applied migration again — a fresh Core provisioned from schema.sql must
match the running DB, or its first heavy build crashes on an unknown kind/edge_type.

Covers the constraints migrations touch. Extend CHECKS as new enum-like CHECKs
are added (Phase 2 same_as edge_type, Phase 3 scope, ...).

Exit 0 = all match. Exit 1 = drift (prints the diff). Exit 2 = parse/DB error.
"""
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent / "schema.sql"

# live constraint name -> (table, column) whose CHECK is an ARRAY[...] allow-list
CHECKS = {
    "entities_kind_check": ("entities", "kind"),
    "entity_edges_edge_type_check": ("entity_edges", "edge_type"),
}


def _vals(text: str) -> set:
    return set(re.findall(r"'([^']+)'", text))


def schema_vals(column: str):
    for line in SCHEMA.read_text().splitlines():
        if re.search(rf"\b{column}\b\s+TEXT", line) and "CHECK" in line and "ARRAY[" in line:
            m = re.search(r"ARRAY\[(.*?)\]", line)
            if m:
                return _vals(m.group(1))
    return None


def live_vals(conname: str):
    out = subprocess.run(
        ["psql", "-d", "corebrain", "-tAc",
         f"SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='{conname}'"],
        capture_output=True, text=True)
    if out.returncode != 0:
        print(f"DB error for {conname}: {out.stderr.strip()}", file=sys.stderr)
        return None
    return _vals(out.stdout)


def main() -> int:
    ok = True
    for conname, (table, col) in CHECKS.items():
        sv = schema_vals(col)
        lv = live_vals(conname)
        if sv is None or lv is None:
            print(f"FAIL {conname}: could not read {'schema.sql' if sv is None else 'live DB'} for {table}.{col}")
            return 2
        if sv != lv:
            ok = False
            print(f"DRIFT {conname} ({table}.{col}):")
            print(f"  schema.sql only: {sorted(sv - lv)}")
            print(f"  live DB only:    {sorted(lv - sv)}")
        else:
            print(f"OK   {conname}: {len(sv)} values match ({', '.join(sorted(sv))})")
    return 0 if ok else 1


if __name__ == "__main__":
    # SEAT-LOCKED: REFUSE AN ARGUMENT RATHER THAN DISCARD IT. core-business, bus #971. It aimed a
    # seat-locked scanner at life's tree; Python accepted the stray argv silently, the tool scanned
    # its OWN seat, and the two runs "converged" — which reads as a regressed fix rather than as a
    # tool that never moved. A WRONG NUMBER, not no number. That is the whole defect: not the
    # seat-locking, which is fine, but claiming to honour a tree it discards.
    if len(sys.argv) > 1:
        sys.exit("REFUSING: %s is seat-locked and cannot scan %r.\n"
                 "It resolves its own Core from __file__. To measure another Core, run it from "
                 "that Core's tree or use seat_matrix staging." % (Path(__file__).name, sys.argv[1]))
    sys.exit(main())
