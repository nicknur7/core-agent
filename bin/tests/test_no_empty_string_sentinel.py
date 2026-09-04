#!/usr/bin/env python3
"""An empty string must never be a marker. It reads as PRESENT and means ABSENT.

WHY THIS EXISTS (2026-08-12, T021). ask_miner wrote `canonical_ask = ''` when the extractor found no
ask in a correction. The INTENT was right — stop re-offering a row already examined, so the LLM does
not pay for it every run. The ENCODING failed in three compounding ways:

  1. STRANDED. The miner's work queue selects `canonical_ask IS NULL`. An empty string is not NULL,
     so the row is never offered again — not to this extractor and not to a better one later.
     239 rows fleet-wide sat in that state (life 133, business 52, school 39, finance 10, ops 5),
     permanently unlabellable AND permanently unusable.

  2. READS AS LABELLED. `canonical_ask IS NOT NULL` is TRUE for ''. Three consumers happen to guard
     with `<> ''`; any new reader doing the obvious thing is silently wrong. One did — bin/
     corpus-readiness.py reported "life: 0 unlabelled" when the true figure was 133, and that number
     was reported to Nick and to two peers before this was found.

  3. NO REASON, NO AUDIT. '' records that something was skipped and nothing about why or when.

FIXED AT THE SOURCE: the miner now writes an explicit `excluded_reason` and leaves canonical_ask
NULL. Same skip, same cost saving, but the rows become a QUERYABLE SET a future extractor can
deliberately re-offer, and no reader can mistake them for labelled.

THIS TEST GUARDS THE SHAPE, NOT THE INSTANCE. A sentinel that is indistinguishable from a legitimate
value by the language's own truth test will be misread again; the only durable fix is that nothing
writes one. So it asserts the miner does not write '' at all, rather than counting today's rows.

Row counts are NOT asserted, deliberately: writes are RLS-scoped to app.current_org_id, so life
cannot backfill a peer's rows and a peer's corpus will legitimately still hold empty strings until
that seat runs its own. Failing here for another seat's un-migrated data would be a wrong alarm —
the class this suite spent the night learning to refuse.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MINER = REPO / "scheduling" / "claude-si" / "ask_miner.py"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _code_only(text: str) -> str:
    """Source with comment lines removed.

    THE FIRST VERSION OF THIS FILE FAILED ITSELF ON ITS OWN PROSE. Both of its textual checks matched
    the COMMENT that documents the bug — ask_miner:183 contains the literal `canonical_ask IS NOT
    NULL` while explaining why an unguarded one is wrong, and the no-ask branch's comment names
    canonical_ask six times while the code beneath it sets excluded_reason.

    That is exactly the defect documented in bin/tests/test_instruments_agree_on_corpus.py — "a test
    that reads source text cannot tell an APPLIED clause from a MENTIONED one" — committed inside a
    file written to catch a related one, minutes after writing the comment it then tripped over.

    Comment-stripping is the minimum honest fix; a `#` inside a string literal would still be
    dropped, which is acceptable here because the SQL under test contains none. The real assertion of
    BEHAVIOUR lives in the row counts the miner produces, not in this file.
    """
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())


def main() -> int:
    print("test_no_empty_string_sentinel")
    if not MINER.is_file():
        print(f"  FAIL  {MINER} missing")
        return 1
    src = _code_only(MINER.read_text())

    # --- the source must not write '' as a value -------------------------------------------
    check("ask_miner no longer writes `ask if ask else \"\"`",
          "ask if ask else \"\"" not in src and "ask if ask else ''" not in src,
          "the empty-string sentinel is back. It reads as PRESENT to `IS NOT NULL` and means "
          "ABSENT, and it strands the row: the work queue selects `canonical_ask IS NULL`, which "
          "an empty string does not satisfy, so nothing ever offers it again.")

    check("ask_miner declares an explicit exclusion reason instead",
          re.search(r"^NO_ASK_REASON\s*=\s*[\"'].+[\"']", src, re.M) is not None,
          "the no-ask case needs a named, human-readable marker — excluded_reason is read by "
          "whoever reviews what the corpus dropped")

    # --- THE DISTINCTION THAT MATTERS, and the one the first version of this file missed ------
    #
    # `type='none'` is a SUCCESSFUL extraction: the extractor looked and reported no durable
    # instruction, which is the correct answer for most corrections and the designed majority.
    # `type IS NULL` with no ask is the genuine failure. Only the second is a stranding.
    #
    # The first T021 fix conflated them and rewrote 624 of life's rows — 587 of them typed 'none' —
    # as miner artifacts. core-business refused the same migration on its own 52 and quoted my own
    # earlier message back: "'' does not mean junk. It means EXTRACTED." Fleet-wide the real defect
    # is 41 rows (37 life, 4 finance), not the 239 first claimed. The other 198 were correct.
    typed = re.search(r'if not ask and t == "none":(.*?)elif not ask:', src, re.S)
    typed_branch = typed.group(1) if typed else ""
    check("a type='none' extraction is treated as a SUCCESS, not a stranding",
          bool(typed) and "excluded_reason" not in typed_branch,
          "the extractor reporting `none` is the designed majority answer. Excluding it attributes "
          "to the miner a failure the miner did not have, and destroys the distinction between "
          "'examined, nothing durable' and 'never examined'.")

    m = re.search(r"elif not ask:(.*?)else:", src, re.S)
    branch = m.group(1) if m else ""
    check("the untyped no-ask branch writes excluded_reason",
          "excluded_reason" in branch and "NO_ASK_REASON" in branch,
          "without it the row is either re-offered forever (LLM cost) or stranded (the old bug)")
    check("...and does NOT set canonical_ask in that branch",
          "canonical_ask" not in branch,
          "setting canonical_ask on the genuine-failure path is how the sentinel came back last time")
    check("...and is idempotent — it will not overwrite an existing exclusion reason",
          "excluded_reason IS NULL" in branch,
          "re-running the miner must not relabel a row already excluded for a DIFFERENT reason "
          "(31 of life's rows are excluded as machine-generated and must keep that reason)")

    # --- EVERY reader must guard, and the readers are DISCOVERED, not listed -------------------
    #
    # This enumerated two files and asserted "every occurrence". It covered 2 of the 5 files that
    # actually use the pattern, and on 2026-08-13 the gap was live: `bin/corpus-readiness.py` had
    # one guarded query and one UNGUARDED SIBLING in the same file, so its by_ask breakdown
    # returned 587 empty-string rows as the single largest "ask" — 14x the biggest real one.
    #
    # That file's own comment records the T021 incident this guard exists for. The fix was applied
    # to the query that got caught and not to the one beside it, and a two-name list could not see
    # the difference. An enumeration cannot find the file nobody added to it.
    #
    # BOTH PAIRING FORMS ARE ACCEPTED. `bin/inject-efficacy.py` guards with
    # `btrim(canonical_ask) <> ''`, which is stricter than `<> ''` (it also excludes whitespace-only
    # asks). A checker that demanded the exact literal form would report the stricter file as the
    # defective one — I made precisely that mistake with the first version of this sweep and read
    # the survivors before believing it.
    #
    # DELIBERATE NON-OFFENDER: a query may name the empty string on purpose. bin/core-si-close.py
    # COUNTS empty asks (`btrim(COALESCE(canonical_ask,'')) = '' AND canonical_ask IS NOT NULL`) —
    # pairing that with a `<> ''` guard would break the measurement it exists to take. It is
    # recognised by the `= ''` on the same statement rather than exempted by filename.
    PAIRED = r"canonical_ask IS NOT NULL(?!\s+AND\s+(?:btrim\()?canonical_ask\)?\s*<>\s*'')"
    readers, offenders = [], []
    for src in sorted(REPO.rglob("*.py")):
        rel = src.relative_to(REPO)
        if any(x in src.parts for x in (".git", "archive", "_archive", "tests")):
            continue
        try:
            raw = src.read_text(errors="ignore")
        except Exception:
            continue
        if "canonical_ask IS NOT NULL" not in raw:
            continue
        readers.append(str(rel))
        # JOIN ADJACENT STRING LITERALS BEFORE MATCHING. A SQL statement in this repo is routinely
        # split across concatenated Python literals, so `... IS NOT NULL "` / newline / `"AND btrim`
        # puts a quote-newline-quote between the clause and its guard. The lookahead below expects
        # whitespace, sees `"`, and reports a CORRECTLY GUARDED query as unguarded — which it did,
        # on the very file this check had just been extended to catch.
        #
        # Collapsing quote-whitespace-quote to a single space reconstructs the statement the
        # database actually receives, which is the thing the assertion is about.
        text = re.sub(r"[\"']\s*\n\s*[\"']", " ", _code_only(raw))
        for m in re.finditer(PAIRED, text):
            window = text[max(0, m.start() - 220):m.end() + 220]
            if "= ''" in window or "= ''" in window.replace('"', "'"):
                continue          # deliberately counting the empty-ask population
            offenders.append(f"{rel}: ...{text[max(0, m.start()-70):m.end()][-90:]}")

    check(f"the sweep found the readers at all ({len(readers)} files use the pattern)",
          len(readers) >= 2,
          "fewer than two files matched — the column was renamed or the query style changed, and "
          "this assertion is now measuring an empty set")
    check("EVERY reader pairs `canonical_ask IS NOT NULL` with an empty-string guard",
          not offenders,
          f"{len(offenders)} unguarded occurrence(s). Old rows carrying '' still exist on any seat "
          f"that has not backfilled — writes are RLS-scoped, so each Core must do its own — and an "
          f"unguarded reader counts them as labelled:\n          "
          + "\n          ".join(offenders[:4]))

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
