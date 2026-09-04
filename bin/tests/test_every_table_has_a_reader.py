#!/usr/bin/env python3
"""A table with rows and no reader is a write into a void — declare it or fix it.

WHY THIS EXISTS (2026-08-13). Phase 4's "make matched-but-did-not-fire a first-class CONSUMED
state" asks for a sweep of signals that are written and never read. The `.claude/state/` half found
ten files. This is the database half, and it found one:

    detector_runs   14 rows   org 1   2026-05-21 .. 2026-06-05   NO READER, EVER

Its stated purpose was resumption — "lets the script resume from last window_end". That was never
implemented. `detect-patterns.py:608` computed its window as `now - timedelta(days=args.since)`, a
CLI flag with a default, and never consulted `window_end`. The bookkeeping was write-only from the
first commit rather than something that decayed, and the table's own COMMENT asserted otherwise for
84 days. Its only writer was archived 2026-06-05.

WHAT THIS ASSERTS. Not "every table must be read" — a table can be legitimately dormant, and a DROP
against the corebrain that five Cores share is a blast-radius action nobody should be pressured into
by a red test. The assertion is that a populated table with no reader must be **declared** here,
with a reason. Declaring costs one line; the point is that it stops being invisible.

THE INSTRUMENT IS DELIBERATELY CONSERVATIVE, because its false-positive mode is expensive. It only
counts a table as unread when the name appears in NO read-shaped context anywhere — and a reader
reached through a helper, a view, or a dynamically-built query string will not be seen. So a finding
here is a CANDIDATE to verify by reading the references, exactly as the real audit did: 30 tables ->
1 candidate -> confirmed by listing every reference by hand. It will not catch every dead table. It
will catch the shape that has already happened once.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

failures: list[str] = []
passes: list[str] = []

# Tables known to have rows and no reader, with the reason. A line here is a CLAIM, and the test
# below checks it is still true — an entry that starts being read again is itself reported, so this
# list cannot quietly become a permanent exemption.
KNOWN_DEAD = {
    "detector_runs": "audited 2026-08-13: 14 rows, writer archived 2026-06-05, resume never "
                     "implemented (detect-patterns.py:608 took its window from --since). DROP is "
                     "a shared-corebrain migration, so it is surfaced rather than done.",
    "merge_journal_20260828": "audited 2026-08-31: write-only restore journal for the "
                     "2026-08-28 canonical-merge run (bin/canonical-merge-apply.py:53 — "
                     "full-row JOURNAL, `to_jsonb(row)` BEFORE every change, precisely so a bad "
                     "merge can be undone). Its reader is a human running a manual psql restore "
                     "against `old_row` IF that merge ever needs reverting, not a script this "
                     "checker can see — that is the table's whole design, not an oversight. Not "
                     "resurrected: this checker would flag it the day a script starts reading it.",
    "entities_bak_orphans": "audited 2026-08-31: T018 preserve-then-drop backup (277 orphan "
                     "entities that existed nowhere else, copied out of entities_bak_pre_origin "
                     "before it was dropped — decisions-log ~4911). Only bin/brain-export-si-layer.py "
                     "names it (excluded above, NOT_A_READER — enumerating a backup is not reading "
                     "it) and bin/tests/test_org_isolation.py's own prose, which the OLD "
                     "proximity-window has_reader() misread as a query. Restoring from a backup is "
                     "a human/admin action, not a live code path.",
    "entity_edges_bak_orphans": "audited 2026-08-31: same T018 backup as entities_bak_orphans "
                     "above (495 orphan edges), same reasoning.",
    "artifact_outcome": "audited 2026-08-31: the only INSERT is "
                     "scheduling/archive/claude-si-behavior-loop/measure-rule-fitness.py — "
                     "archived, not on any live path — and nothing SELECTs from it; "
                     "schema-phase2.sql's own comment ('Refreshed by Phase 3 job from "
                     "artifact_event + artifact_outcome') is what the OLD proximity-window "
                     "has_reader() misread as a reader: the word 'from' two tables earlier in the "
                     "same sentence, not a query naming this table. Phase 3 never shipped a "
                     "refresher. 22 rows are pre-archival history.",
}

# Tables whose ONLY known reader lives in a `per_core_keep` file (bin/sync-manifest.json) — carried
# on the writer (life) by design and deliberately never synced to a pull seat. KNOWN_DEAD does not
# fit this shape: it is one flat boolean shared by every seat's run of this SAME test file, so
# declaring a table dead there makes life's own run trip the "nothing in KNOWN_DEAD has quietly
# acquired a reader" check the moment life's copy of the reader is on disk and read from — which it
# always is, life being where the file lives.
#
# FOUND core-business, 2026-09-01: `artifacts` (org 1 only, 16 rows: 5 hook + 10 rule + 1
# verification_trigger) is read by scheduling/claude-si/measure-existing-hooks.py
# ("SELECT label, id FROM artifacts WHERE org_id = %s AND kind = 'hook'"), and that file is
# per_core_keep — present on life, absent everywhere else on purpose (brain: "measure-existing-
# hooks.py is personal/per-core; not generic enough for shared baseline"). A pull seat's own-tree
# scan correctly finds no reader; that is the seat not carrying a file it was never meant to
# carry, not the table going dead.
PER_CORE_READER = {
    "artifacts": "scheduling/claude-si/measure-existing-hooks.py — per_core_keep "
                 "(bin/sync-manifest.json), life-only by design. audited 2026-09-01 (surfaced by "
                 "core-business running this shared test): populated only by org 1 (life), 16 rows; "
                 "measure-existing-hooks.py:133 reads `FROM artifacts`.",
}


def _per_core_reader_status(table):
    # type: (str) -> "bool | None"  — no `from __future__ import annotations` here; this repo's
    # tests run under Python 3.9, and `bool | None` is not a valid RUNTIME annotation before 3.10.
    """True/False if PER_CORE_READER's file is present on THIS seat and does/doesn't still read
    the table (verifiable here — usually life, the seat that carries the file). None if the file
    is simply absent on this seat, which is the expected, unverifiable-from-here, normal case on
    every pull seat and is not itself a failure.
    """
    entry = PER_CORE_READER.get(table)
    if entry is None:
        return None
    reader_rel = entry.split(" — ", 1)[0].strip()
    p = REPO / reader_rel
    if not p.is_file():
        return None  # not on this seat — the exemption is trusted, not verifiable here
    rx = re.compile(r"\b(?:from|join)\s+\"?" + re.escape(table) + r"\"?\b", re.I)
    return bool(rx.search(p.read_text(errors="ignore")))


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_every_table_has_a_reader")
    try:
        from _env import connect_corebrain
        con = connect_corebrain()
    except Exception as exc:
        print(f"  note: CHECK DID NOT RUN — corebrain unreachable: {str(exc)[:80]}")
        print("\n0 passed, 0 failed")
        return 0

    try:
        cur = con.cursor()
        cur.execute("SET app.current_org_id = '1'")
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
        tables = [r[0] for r in cur.fetchall()]
        populated = []
        for t in tables:
            try:
                cur.execute(f'SELECT 1 FROM "{t}" LIMIT 1')
                if cur.fetchone():
                    populated.append(t)
            except Exception:
                con.rollback()
    finally:
        con.close()

    check(f"the schema was readable ({len(tables)} tables, {len(populated)} populated)",
          bool(tables),
          "no tables enumerated — the query changed or the DB is empty, and every assertion "
          "below is vacuous")

    # Scan the codebase once.
    #
    # A BACKUP IS NOT A READER (2026-08-28). bin/brain-export-si-layer.py enumerates EVERY
    # irreplaceable table in one list and emits `select ... from <t>` for each. To this detector
    # that is a reader — for all nineteen of them at once. So the check that is supposed to find a
    # table nothing consumes was blind across the entire SI layer, and had been since the exporter
    # landed (3846d4d).
    #
    # It surfaced only because `detector_runs` is ALSO in KNOWN_DEAD, so the opposite assertion —
    # "nothing declared dead has quietly acquired a reader" — fired. One table happened to be
    # covered by both checks; the other eighteen were silently exempt with nothing to notice it.
    # The failing check was right that something read the table, and wrong about what that meant.
    #
    # Excluded by PATH rather than by pattern: the exporter's whole job is to name every table, so
    # any content heuristic would have to encode "unless it is the backup", which is the same
    # exemption written less legibly. Backing data up is not consuming it, and a table that only
    # the backup mentions is exactly as dead as one nothing mentions at all.
    #
    # THIS FILE ITSELF is not a reader either (2026-08-31), for the same reason: its whole job is
    # to NAME every dead table in KNOWN_DEAD prose. Found live: adding `merge_journal_20260828`
    # right after the existing `detector_runs` entry put that entry's own "took its window from
    # --since" inside THIS table's -160-char lookback window — the checker's own documentation of
    # one dead table supplied the word "from" that made a different dead table look read. A
    # detector that reads its own explanatory prose as evidence will keep colliding as KNOWN_DEAD
    # grows; excluding this file's source from corpus removes the false signal at its root instead
    # of reshuffling prose to dodge the regex each time a new entry lands near an old one.
    NOT_A_READER = ("bin/brain-export-si-layer.py", "bin/tests/test_every_table_has_a_reader.py")
    corpus = []
    for ext in ("*.py", "*.sh", "*.sql"):
        for p in REPO.rglob(ext):
            s = str(p)
            if "/.git/" in s or "/node_modules/" in s:
                continue
            if any(s.endswith(x) for x in NOT_A_READER):
                continue
            try:
                corpus.append(p.read_text(errors="ignore"))
            except Exception:
                pass
    check(f"the codebase was scanned ({len(corpus)} files)", len(corpus) > 50,
          "too few files read to conclude anything about readers")

    def has_reader(table: str) -> bool:
        # FROM/JOIN must directly GOVERN this table token (optionally quoted), not merely
        # appear somewhere in a loose window around it (2026-08-31). The window version —
        # "from" or "join" anywhere in the 160 chars before / 60 after the table name — read
        # `REVOKE ... ON public.merge_journal_20260828 FROM brain_app` as a SELECT read of that
        # table, because SQL's REVOKE grammar also uses the word FROM, just to name who LOSES a
        # grant, not what a query reads. It separately read a KNOWN_DEAD entry's own prose
        # ("...took its window from --since") as a reader of the NEXT dict entry purely by
        # proximity. Anchoring the keyword immediately before the identifier is what "reads
        # this table" actually looks like in SQL and stops matching prose or a different
        # clause that happens to share a paragraph with the name.
        rx = re.compile(r"\b(?:from|join)\s+\"?" + re.escape(table) + r"\"?\b", re.I)
        return any(rx.search(txt) for txt in corpus)

    unread = [t for t in populated if not has_reader(t)]
    # A PER_CORE_READER entry excuses `t` from `undeclared` UNLESS its declared file is present on
    # THIS seat and demonstrably no longer reads the table (status is False, not just falsy/None) —
    # that is the one case this checker CAN verify, and a stale declaration must still fail loud.
    pc_status = {t: _per_core_reader_status(t) for t in unread if t in PER_CORE_READER}
    pc_exempt = lambda t: t in PER_CORE_READER and pc_status.get(t) is not False
    undeclared = [t for t in unread if t not in KNOWN_DEAD and not pc_exempt(t)]

    check("every populated table either has a reader or is declared dead here",
          not undeclared,
          "these hold rows and nothing reads them, and they are not in KNOWN_DEAD:\n          "
          + "\n          ".join(undeclared)
          + "\n          Verify by listing every reference before believing it — a reader reached "
            "through a helper or a view is invisible to this check. If it is genuinely dead, add "
            "it to KNOWN_DEAD with the evidence.")

    # A PER_CORE_READER declaration whose file IS on this seat (usually life) must still actually
    # read the table — the one direction this checker can verify locally. A False here means the
    # exemption has gone stale: either fix the reader, or move the table to KNOWN_DEAD.
    pc_stale = [t for t in PER_CORE_READER if pc_status.get(t) is False]
    check("no PER_CORE_READER declaration is stale on a seat that carries its file",
          not pc_stale,
          f"these declare a per-core reader whose file is present on THIS seat but no longer reads "
          f"the table: {pc_stale}")

    # The exclusion above is load-bearing: if the exporter ever stops being excluded, nineteen
    # tables become unfalsifiable at once and this file reports ALL GREEN while checking nothing.
    _exporter = REPO / "bin" / "brain-export-si-layer.py"
    check("the backup is excluded from reader evidence, and still exists to be excluded",
          _exporter.is_file() and "IRREPLACEABLE" in _exporter.read_text(errors="ignore"),
          "brain-export-si-layer.py moved or changed shape — re-derive NOT_A_READER, or this "
          "check is exempting a file that no longer does what the exemption assumes")

    # The other direction: a KNOWN_DEAD entry that came back to life is a stale claim.
    resurrected = [t for t in KNOWN_DEAD if t in populated and has_reader(t)]
    check("nothing in KNOWN_DEAD has quietly acquired a reader",
          not resurrected,
          f"these are declared dead but are now read: {resurrected}. Remove them from KNOWN_DEAD — "
          "a stale exemption is how a real finding gets suppressed later.")

    # And the check must be capable of failing.
    check("the reader-detector would notice a table nothing mentions",
          not has_reader("definitely_not_a_real_table_xyz"),
          "has_reader() returns True for a table that appears nowhere, so the assertion above "
          "passes regardless of the schema")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
