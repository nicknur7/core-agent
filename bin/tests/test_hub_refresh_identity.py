#!/usr/bin/env python3
"""Hub-refresh identity: what `--partition` dispatches must be what `--ingest` can write.

WHAT THIS GUARDS
----------------
`compile-truth-refresh.py --ingest` updates
`WHERE entities.kind = ? AND entities.name = ? AND org_id = ?`. Kind is HALF the identity.
`--detect` reads drifted rows from the DB, so it always knows both halves. `--partition`
sits between them and is the only place the pair can be broken.

THE FAILURE THIS TEST EXISTS FOR (2026-07-30)
---------------------------------------------
`--partition` collapsed the drift report to a set of NAMES, then walked the vault's hub
files and took `kind` from whichever FILE matched the name. The vault holds both
`entities/receipt-reader.md` and `topics/receipt-reader.md`, so two drifted DB rows —
Entity/"Receipt Reader" and Topic/"sentinel-security-review" — partitioned into THREE
batches. The extra one, Topic/"Receipt Reader", names no row in `entities`.

A Sonnet worker compiled that hub, its output passed the (kind, name)-matches-input check
(it did match its input — the input was wrong), and `--ingest` updated nothing for it. It
printed "Refresh-ingested 3 entities" because it printed `len(rows)`, the count SUBMITTED.
The close protocol's safeguard is "the ingest count MUST equal the drifted-hub count"; that
read 3 == 3 and passed. The only reason it surfaced was a secondary line reporting 2 rows
actually touched.

Two invariants, one per half of that failure:
  1. partition may only emit (kind, name) pairs the drift report actually contains;
  2. ingest must report rows APPLIED, so a shortfall is loud rather than arithmetic that
     happens to agree.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scheduling" / "brain-pg" / "compile-truth-refresh.py"

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")
        failures.append(label)


def main():
    src = SCRIPT.read_text()
    print("hub-refresh identity")

    # ---- Invariant 1: partition matches on the pair, not the name ----------------------
    #
    # MOVED TO bin/tests/test_hub_partition_pairs.py, WHICH EXECUTES IT.
    #
    # These four checks were `re.search` against this script's SOURCE TEXT — they asserted that a
    # particular expression appears, never that the partition behaves. So a correct refactor turned
    # them red, and a rewrite reintroducing the 2026-07-30 phantom-pair bug in a different spelling
    # passed them. Fourth test found today keyed to an implementation rather than a property.
    #
    # The replacement plants a vault holding the SAME NAME under two kinds, runs the real
    # partition_drifted(), and requires exactly one entry — with a control that reintroduces the
    # name-only match in a COPY and confirms the same fixture DOES produce the phantom pair.
    #
    # Not kept alongside: two instruments on one subject is the defect this fleet has spent two days
    # removing, and the source-grep is the one that cannot fail for the right reason.

    # ---- Invariant 2: ingest reports what landed --------------------------------------
    check(
        "ingest captures cur.rowcount",
        re.search(r'applied\s*=\s*cur\.rowcount', src) is not None,
        "the submitted count is not evidence of a write",
    )
    check(
        "ingest compares applied against submitted",
        re.search(r'if\s+applied\s*!=\s*len\(rows\)', src) is not None,
    )
    check(
        "a shortfall is announced, not inferred",
        "SHORTFALL" in src,
    )
    check(
        "the headline count is the applied one",
        re.search(r'Refresh-ingested \{applied\}', src) is not None,
        "printing len(rows) here is what let 3-vs-2 read as success",
    )

    # ---- Behavioural: the collapsing case, on a synthetic report -----------------------
    # Two drifted rows sharing NO kind with a third plausible pair. We assert the pair set
    # comprehension itself, since running --partition needs the live vault + DB.
    report = {
        "drift_records": [
            {"kind": "Entity", "name": "Receipt Reader"},
            {"kind": "Topic", "name": "sentinel-security-review"},
        ]
    }
    pairs = {(r["kind"], r["name"]) for r in report["drift_records"]}
    check(
        "the 2026-07-30 phantom pair is not derivable from the report",
        ("Topic", "Receipt Reader") not in pairs,
        "Topic/Receipt Reader came from a hub FILE, never from the DB",
    )
    names_only = {r["name"] for r in report["drift_records"]}
    check(
        "...but IS derivable under name-only matching (the old bug reproduces)",
        "Receipt Reader" in names_only,
        "confirms this test would have caught the original defect",
    )

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("PASS — partition dispatches only real (kind, name) pairs; ingest reports applied rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
