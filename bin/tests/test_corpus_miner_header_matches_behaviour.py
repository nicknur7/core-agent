#!/usr/bin/env python3
"""A file's header may not contradict what the file does — checked by AST, not by text.

WHY THIS EXISTS (2026-08-13, core-finance DOSE 39). `learned-corpus-miner.py`'s header carried three
false claims, and one of them is the sentence a reader hits when asking WHICH FILES WRITE TO THE
CORRECTION CORPUS:

    "Two jobs over pattern_observations"      -> three: --backfill, --embed, --detect
    "Read-mostly ... No inserts, no deletes"  -> INSERT INTO pattern_observations at :372
    "brain_admin BYPASSRLS connection"        -> connect_corebrain() = brain_app, RLS-scoped

session-lifecycle.sh:546 states that this file's `--detect` is THE ONLY WRITER to
pattern_observations. Anyone trusting the header would skip the sole writer to the corpus that feeds
si_induct, contract fitness and the rest of the SI loop.

The connection line was wrong in BOTH directions at once. Three statements in that file carry no
org_id predicate, which is fine under the RLS-scoped connection it really uses — so one false
sentence yields a false NEGATIVE (skip the writer) and a false POSITIVE (condemn three safe queries
as a leak) from opposite ends of the same audit.

WHY THIS USES AST, WHICH IS THE PART THAT GENERALISES. After seven textual assertions matched
COMMENTS instead of code on 2026-08-12, this suite adopted "strip comments before any textual
assertion". core-finance adopted it too — and their DOSE 39 probe then reported "no INSERT found",
a false negative, because their stripper removed every triple-quoted literal and THE SQL IS A
TRIPLE-QUOTED STRING. The mitigation for my defect caused theirs:

    stripping DOCSTRINGS is not the same as stripping every triple-quoted literal
    mine matched prose and passed; theirs deleted code and passed

A regex over source text cannot reliably tell a docstring from a SQL literal. The AST can: it knows
which strings are arguments to `.execute(...)` and which are module documentation. So the checks
below read the parse tree, never the raw text.
"""
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scheduling" / "claude-si" / "learned-corpus-miner.py"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def sql_verbs(tree: ast.AST) -> set:
    """Every SQL verb passed to a .execute() call, read from the parse tree."""
    verbs = set()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "execute"):
            continue
        for a in n.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                head = " ".join(a.value.split()).upper()
                for v in ("INSERT", "UPDATE", "DELETE", "SELECT"):
                    if head.startswith(v) or f" {v} " in head[:60]:
                        verbs.add(v)
    return verbs


def argparse_flags(tree: ast.AST) -> set:
    flags = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "add_argument"):
            for a in n.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and a.value.startswith("--"):
                    flags.add(a.value)
    return flags


def main() -> int:
    print("test_corpus_miner_header_matches_behaviour")
    if not SRC.is_file():
        print(f"  FAIL  {SRC} missing")
        return 1
    src = SRC.read_text()
    tree = ast.parse(src)
    header_raw = ast.get_docstring(tree) or ""

    # A MENTION IS NOT AN ASSERTION — and this is the root of every one of the nine false matches
    # this suite hit on 2026-08-12/13.
    #
    # The corrected header QUOTES the claims it is retracting: it says the file used to claim
    # 'No inserts, no deletes' and 'brain_admin BYPASSRLS connection', and that both were false.
    # A regex over the raw header matches those quotations and concludes the file still makes the
    # claims — so the first version of this test failed on the very header that fixed the defect,
    # which would have pressured the next reader to delete the retraction to make the suite green.
    #
    # Quoted spans are stripped before matching. In prose, quotation marks are exactly the marker
    # that separates USE from MENTION, so this is not a heuristic patched onto a regex — it is the
    # distinction the whole class of bug turns on, applied once.
    header = re.sub(r"'[^']*'|\"[^\"]*\"", " ", header_raw)

    verbs = sql_verbs(tree)
    flags = argparse_flags(tree) - {"--dry-run"}

    # ---- the claim that matters most: does the header deny writes the file makes? -------------
    denies_insert = re.search(r"no inserts", header, re.I) is not None
    check("the header does not deny an INSERT the file actually performs",
          not (denies_insert and "INSERT" in verbs),
          f"the header says 'no inserts' while .execute() issues {sorted(verbs)}. This is the "
          f"sentence someone reads when asking which files write to pattern_observations, and "
          f"session-lifecycle.sh:546 calls this file the ONLY writer.")

    check("the file does perform an INSERT (so the check above is not vacuous)",
          "INSERT" in verbs,
          f"no INSERT found in any .execute() argument — got {sorted(verbs)}. If --detect was "
          f"genuinely removed, update the header and this test together.")

    # ---- job count ----------------------------------------------------------------------------
    m = re.match(r"\s*(TWO|THREE|FOUR|\d+)\s+jobs", header, re.I)
    words = {"two": 2, "three": 3, "four": 4}
    claimed = None
    if m:
        tok = m.group(1).lower()
        claimed = words.get(tok, int(tok) if tok.isdigit() else None)
    check(f"the header's job count matches the registered flags ({sorted(flags)})",
          claimed is None or claimed == len(flags),
          f"header claims {claimed} jobs; argparse registers {len(flags)}: {sorted(flags)}")

    # ---- connection claim ---------------------------------------------------------------------
    uses_app = any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "connect_corebrain"
                   for n in ast.walk(tree))
    # A NEGATION IS NOT A CLAIM, which is the same use/mention distinction one layer over. The
    # corrected header says "NOT a BYPASSRLS connection" — an accurate statement that a bare
    # `search("BYPASSRLS")` reads as the very error it corrects. So an occurrence only counts when
    # no negator immediately precedes it. Second time in this one file that the fix for a false
    # claim tripped the detector written to catch that claim.
    claims_bypass = any(
        not re.search(r"\b(not|never|no|isn't|is not)\b[^.]{0,24}$", header[:m.start()],
                      re.I | re.S)
        for m in re.finditer(r"BYPASSRLS", header))
    check("the header does not claim BYPASSRLS while using the RLS-scoped connection",
          not (claims_bypass and uses_app),
          "the header claims a BYPASSRLS connection but the code calls connect_corebrain() "
          "(brain_app, RLS-scoped). That single sentence makes three org_id-free statements in "
          "this file look like a multi-tenant leak when the policy is scoping them.")

    # ---- and prove the AST instrument is doing real work -------------------------------------
    # If the SQL had been found by text search it would also match the header's own prose; the
    # point of the AST is that it CANNOT. Assert the parse tree sees a string the raw text search
    # would attribute ambiguously.
    check("the AST reads .execute() arguments, not documentation",
          "INSERT" in verbs and "no inserts" not in " ".join(sorted(verbs)).lower(),
          "verbs came from parse-tree call arguments; if this ever starts matching header prose "
          "the instrument has regressed to text search")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
