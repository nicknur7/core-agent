#!/usr/bin/env python3
"""decisions_segment.py — segment decisions-log.md into identified entries (unified redesign, step ②).

Plan: tasks/research/memory-brain-si-unified-redesign-2026-07-18.md PART 7.4 (Codex spec).

Makes each decision a first-class, stably-identified source so decisions become recallable + supersession
-safe (the fix for the 2026-07-10 guard-reversal class). Rules (Codex):
  - Entry boundary = every level-3 heading (`### `). Date context inherits the nearest preceding dated heading.
  - Each entry gets a PERSISTED id: `<!-- core-decision-id: d_<hex> -->` inserted right after its heading.
    Identity = that id (survives edits/renames). content_hash = the entry body (identifies revisions).
  - This pass ONLY inserts id comments — it NEVER rewrites decision prose.
  - Each entry is registered into the ledger as a `decision_entry` source + revision, and a
    'semantically_interpreted' job is enqueued for the (later) assertion extractor to claim.

Idempotent: entries that already carry an id are reused; unchanged bodies create no new revision.
Fork-safe: path from CORE_INSTANCE. Nick-approved 2026-07-18 (decisions-log becomes a brain source;
about-me/relationships/etc. stay private-by-default and are NOT touched here).

Usage:
  CORE_ORG_ID=1 CORE_INSTANCE=... python3 decisions_segment.py --dry-run   # preview, no writes
  CORE_ORG_ID=1 CORE_INSTANCE=... python3 decisions_segment.py             # insert ids + register
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
from _env import connect_corebrain, get_org_id, connect_or_skip  # noqa: E402

DECISION_PROCESSOR_VERSION = "decision-extract/v1"
ID_RE = re.compile(r"<!--\s*core-decision-id:\s*(d_[0-9a-f]+)\s*-->")
# `##` OR `###` (2026-07-28). This matched `^### ` only, which is life's convention — and the
# parser is SHARED code running on every Core. core-business's decisions-log used `##`, so the
# segmenter found 0 entries, enqueued nothing, and printed a cheerful success line on every close
# since that Core existed. Measured the same day: org 2 had 0 assertions against 33 real decisions;
# school 0 against 20; finance 0 against 15; ops 0. `recall_similar` was serving three Cores off an
# assertion layer that was not stale but ABSENT, and the only symptom was the words "0 entries",
# which are indistinguishable from "nothing new."
#
# Life was not clean either: 52 of its own entries use `##` and had never been parsed.
#
# Found by core-business, who fixed their own FILE and then said the fix was the wrong layer and
# handed the parser back. They were right — normalising each Core's markdown is a data fix for a
# parser that assumed a per-Core convention it never verified.
HEAD_RE = re.compile(r"^#{2,3} (.*)$")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _log_path() -> Path:
    inst = os.environ.get("CORE_INSTANCE")
    if not inst:
        raise SystemExit("CORE_INSTANCE required")
    return Path(inst) / "memory" / "decisions-log.md"


def parse_entries(lines: list[str]) -> list[dict]:
    """Return entries: {heading_idx, heading, id (or None), body_start, body_end (exclusive), date}."""
    heads = [i for i, ln in enumerate(lines) if HEAD_RE.match(ln)]
    entries = []
    last_date = None
    for k, hi in enumerate(heads):
        heading = HEAD_RE.match(lines[hi]).group(1)
        m = DATE_RE.search(heading)
        if m:
            last_date = m.group(1)
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        # existing id on the line right after the heading?
        existing = None
        if hi + 1 < end:
            im = ID_RE.search(lines[hi + 1])
            if im:
                existing = im.group(1)
        entries.append({"heading_idx": hi, "heading": heading, "id": existing,
                        "body_start": hi, "body_end": end, "date": last_date})
    return entries


def decision_date_map(lines: list[str] | None = None) -> dict[str, str]:
    """Return {core-decision-id: ISO date} for every identified, dated entry.

    THE ONE SOURCE OF DECISION DATES. `assertions.effective_from` is populated from
    this at ingest, and `backfill_effective_from.py` repairs history from it. Both
    call here rather than re-implementing the heading walk, because a second copy of
    this parse is exactly how the `^### `-vs-`^#{2,3} ` blindness survived unnoticed
    for this Core's entire existence — one pattern got fixed and the other did not.

    Dates inherit the nearest preceding dated heading (same rule as parse_entries),
    so an undated sub-entry is attributed to the decision it sits under.
    """
    if lines is None:
        lines = _log_path().read_text().splitlines()
    return {e["id"]: e["date"] for e in parse_entries(lines) if e["id"] and e["date"]}


def entry_body_hash(lines: list[str], e: dict) -> str:
    """Hash the entry text EXCLUDING any injected id-comment line, so inserting the id is not itself
    a content change."""
    body = [ln for ln in lines[e["body_start"]:e["body_end"]] if not ID_RE.search(ln)]
    return ledger.content_hash_of("".join(body))


def main() -> int:
    dry = "--dry-run" in sys.argv
    path = _log_path()
    lines = path.read_text().splitlines(keepends=True)
    entries = parse_entries(lines)

    # PARSED NOTHING FROM A NON-EMPTY FILE IS A FAILURE, NOT A NO-OP (2026-07-28).
    # The heading-pattern bug above survived for this Core's entire existence because its only
    # symptom was the phrase "0 entries", which reads exactly like "nothing new since last close".
    # A parser that finds no work in a 600-line input is describing ITSELF, not the input. Same
    # class as the exhaust filter fixed the same day: the failure was invisible because the
    # success message and the failure message were the same message. Exit non-zero so a close
    # cannot stroll past it.
    if not entries and any(ln.strip() for ln in lines):
        heads = sum(1 for ln in lines if ln.lstrip().startswith("#"))
        print(f"DECISIONS-SEGMENT: parsed 0 entries from {len(lines)} non-empty lines "
              f"({heads} line(s) begin with '#'). The heading pattern {HEAD_RE.pattern!r} matches "
              f"nothing in {path}.\n"
              f"  This is a PARSER/CONVENTION MISMATCH, not an empty log. Do NOT treat it as "
              f"'nothing new' — the assertion layer for this org is not stale, it is ABSENT, and "
              f"recall_similar is serving without it.", file=sys.stderr)
        return 2

    assigned = 0
    # Insert ids top-down but apply edits bottom-up so indices stay valid.
    inserts = []  # (line_idx_to_insert_after, id)
    for e in entries:
        if e["id"] is None:
            e["id"] = "d_" + uuid.uuid4().hex
            inserts.append((e["heading_idx"], e["id"]))
            assigned += 1
    if not dry and inserts:
        for hi, did in sorted(inserts, key=lambda x: -x[0]):
            indent = ""
            lines.insert(hi + 1, f"<!-- core-decision-id: {did} -->\n")
        path.write_text("".join(lines))
        # re-parse after insertion so body ranges include the id line consistently
        lines = path.read_text().splitlines(keepends=True)
        entries = parse_entries(lines)

    # Register into the ledger.
    reg = {"sources": 0, "new_revisions": 0, "jobs": 0}
    if not dry:
        # Degrade with a named status instead of killing the close chain (see
        # _env.connect_or_skip). A Core with no database must still be able to close.
        conn = connect_or_skip("ASSERTIONS-segment")
        if conn is None:
            return 0
        try:
            for e in entries:
                if not e["id"]:
                    continue
                chash = entry_body_hash(lines, e)
                uri = f"memory/decisions-log.md#{e['id']}"
                sid = ledger.register_source(conn, e["id"], "decision_entry", uri)
                reg["sources"] += 1
                rev_id, seq, is_new = ledger.register_revision(conn, sid, chash, uri, operation="upsert")
                if is_new:
                    reg["new_revisions"] += 1
                    fp = ledger.compute_fingerprint(chash, "semantically_interpreted", DECISION_PROCESSOR_VERSION)
                    _, jnew = ledger.enqueue_job(conn, rev_id, "semantically_interpreted",
                                                 DECISION_PROCESSOR_VERSION, fp)
                    if jnew:
                        reg["jobs"] += 1
                conn.commit()
        finally:
            conn.close()

    tag = "[dry-run] " if dry else ""
    print(f"{tag}decisions-log: {len(entries)} entries, {assigned} new id(s) assigned"
          + ("" if dry else f"; registered {reg['sources']} sources, {reg['new_revisions']} new revisions, "
             f"{reg['jobs']} extraction job(s) enqueued"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
