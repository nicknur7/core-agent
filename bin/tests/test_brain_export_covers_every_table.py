#!/usr/bin/env python3
"""Every table in the brain database must be classified. An unclassified table is un-backed-up.

The failure this guards is not a broken export — a broken export is loud. It is a table added six
months from now that nobody thinks about, which falls outside all three lists, is silently never
exported, and is discovered missing only at the moment someone needs to restore it.

That is the same shape as the defects this system kept producing in August: a mechanism that looks
live and covers less than assumed. `steering-compress` could move 1% of what it existed to manage.
`lessons-evict` covered a quarter of the always-loaded prose. `contract-fitness` froze for nine days
because nothing measured its age. In each case the mechanism worked and its COVERAGE was the thing
nobody checked.

So the check here is coverage, not correctness: does the classification account for every table
that actually exists, right now, on this machine.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("bx", ROOT / "bin" / "brain-export-si-layer.py")
bx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bx)

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    # The three lists must be disjoint — a table in two lists means the intent is ambiguous, and an
    # ambiguous intent about a backup is the kind of thing that reads fine and protects nothing.
    i, r, e = set(bx.IRREPLACEABLE), set(bx.REGENERABLE), set(bx.EXCLUDED)
    check("IRREPLACEABLE and REGENERABLE do not overlap", not (i & r), str(i & r))
    check("IRREPLACEABLE and EXCLUDED do not overlap", not (i & e), str(i & e))
    check("REGENERABLE and EXCLUDED do not overlap", not (r & e), str(r & e))

    probe = subprocess.run(["psql", "-d", bx.DB, "-tAc", "select 1"],
                           capture_output=True, text=True, timeout=60)
    if probe.returncode != 0:
        # ABSTAIN, not pass. run-all.sh grades exit 2 + UNDECIDABLE as "declined to certify here;
        # no fixture, not a defect" — the honest verdict when the thing under test is unreachable.
        # Reporting green here would be the exact failure this file is about.
        print("UNDECIDABLE: no Postgres reachable, cannot enumerate live tables")
        print("  Run with an interpreter and environment that can reach the corebrain database.")
        return 2

    live = set(bx.live_tables())
    check("the database has tables to classify", len(live) >= 10, f"found {len(live)}")

    unclassified = sorted(live - i - r - e)
    check("EVERY live table is classified", not unclassified,
          f"unclassified (silently NOT backed up): {unclassified}")

    # A name in a list that no longer exists is stale rather than dangerous, but it hides a rename:
    # the old name lingers, the new one is unclassified, and the count still looks plausible.
    #
    # EXEMPT: ADHOC_SEAT_LOCAL entries are one-off, hand-created tables from a specific incident on
    # one seat's live DB — not created by schema.sql or any migration, so a fresh Core (or any seat
    # that never hit that incident) legitimately never has them. Requiring them to be live fails
    # every fresh clone unfixably; that is not the rename this check exists to catch.
    adhoc = set(getattr(bx, "ADHOC_SEAT_LOCAL", ()))
    stale = sorted((i | e) - live - adhoc)
    check("no IRREPLACEABLE/EXCLUDED entry names a table that is gone (excluding known seat-local adhoc backups)",
          not stale, f"stale entries: {stale}")

    # POSITIVE CONTROL. A coverage check that cannot fail is decoration. Prove it detects a table
    # that exists and is in none of the lists.
    saved = list(bx.IRREPLACEABLE)
    try:
        bx.IRREPLACEABLE = [t for t in bx.IRREPLACEABLE if t != "si_artifacts"]
        would_flag = sorted(live - set(bx.IRREPLACEABLE) - r - e)
        check("control: dropping a table from the lists IS detected",
              "si_artifacts" in would_flag, f"got {would_flag}")
    finally:
        bx.IRREPLACEABLE = saved

    print("\n" + ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "ALL PASS"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
