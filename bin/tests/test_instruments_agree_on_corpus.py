#!/usr/bin/env python3
"""Any instrument computing over pattern_observations must see the SAME ROWS as the incumbent.

WHY THIS EXISTS (2026-08-12). bin/null-calibration.py was written to VALIDATE
scheduling/claude-si/measure-contract-fitness.py. It computed the same windows over the same table
and omitted one clause the incumbent applies: the detector-generation filter.

    with GEN     stop-and-plan  pre 23   ratio 0.70
    without GEN                 pre 325  ratio 0.10

pattern_observations holds two detector generations, and EVERY contract shipped on the changeover
date — so an unfiltered pre-window is ~82% fossil against a 100%-live post-window, and "the install
date is special" collapses into "the detector changed that day." The validator produced a confident
wrong answer that looked like a fresh finding, and it was broadcast to three seats before
core-business caught it by varying all sixteen filter combinations.

THE CLASS, named by core-finance and distinct from the five copy-vs-shipped instances before it:

    A NEW INSTRUMENT BUILT TO VALIDATE AN OLD ONE DID NOT INHERIT THE OLD ONE'S POSTMORTEMS.

The master plan's own section 5 says do not build a second before/after engine, because
measure-contract-fitness is battle-hardened through five documented postmortems. A VALIDATOR of that
engine felt exempt from the rule. It is not — it computes the same windows over the same table, so
it inherits the same confounds and needs the same clauses.

And the incumbent predicted the shape of its own regression. Its comment ends: "reversible by
deleting one clause." Omitting one clause is precisely what happened. Nobody was reading that comment
as a specification.

WHAT THIS ASSERTS: for each live contract, the row set each instrument selects must MATCH. Not the
verdicts — the ROWS. A verdict comparison would let two different corpora produce the same answer by
luck; a row comparison cannot. If a future instrument drops or adds a filter, this fails on the
count before anyone reads its output as a finding.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

failures: list[str] = []
passes: list[str] = []
# UNDECIDABLE, NOT FAIL — added 2026-09-01, core-business. A seat with no fossil-generation rows
# cannot exercise the GEN canary or anything downstream of it; see the note at the canary below.
abstained: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _rowcount(con, extra: str) -> int:
    """Rows the corpus query selects under a given set of clauses. THE ROWS, NOT THE SOURCE TEXT.

    The first version of this file grepped each module for clause substrings. It passed its own
    mutation dose: commenting GEN out left the string `detector_version` in the file, so the
    extractor still counted it. A test that reads source text cannot tell an APPLIED clause from a
    MENTIONED one — which is the same defect it exists to catch, committed inside itself. Its
    docstring already promised a row comparison; only the implementation was substring matching.
    """
    cur = con.cursor()
    cur.execute(
        "SELECT count(*) FROM pattern_observations WHERE "
        "org_id = current_setting('app.current_org_id', true)::bigint "
        "AND correction_text IS NOT NULL " + extra)
    return cur.fetchone()[0]


def main() -> int:
    incumbent = REPO / "scheduling" / "claude-si" / "measure-contract-fitness.py"
    validators = [REPO / "bin" / "null-calibration.py"]

    print("test_instruments_agree_on_corpus")
    if not incumbent.exists():
        print("  FAIL  incumbent measure-contract-fitness.py missing")
        return 1

    from _env import connect_corebrain  # noqa: E402
    try:
        con = connect_corebrain()
    except Exception as exc:
        print(f"  SKIP  corebrain unreachable ({exc.__class__.__name__}) — this test compares live "
              f"row counts and cannot run without it")
        return 0

    GEN = "AND detector_version = 'learned-miner-v1' "
    EXCL = "AND excluded_reason IS NULL "

    full = _rowcount(con, GEN + EXCL)
    no_gen = _rowcount(con, EXCL)
    no_excl = _rowcount(con, GEN)

    # THE CANARY CAN BE TRUE ONLY WHERE A DETECTOR-GENERATION CHANGEOVER HAPPENED WHILE THE SEAT
    # ALREADY HAD ROWS. Measured 2026-09-01, core-business: no_gen == full (243 == 243) — every one
    # of business's 367 pattern_observations rows already carries detector_version='learned-miner-v1'
    # (verified directly: `SELECT detector_version, count(*) ... GROUP BY 1` returns exactly one
    # row). That is not the clause going inert; it is a corpus with no fossil generation to filter,
    # because business's corpus only exists from 2026-07-02 on — after the changeover this canary was
    # built to detect. life's corpus predates it, business's does not, and no per-seat code choice
    # changes that history.
    #
    # A hard FAIL here would be exactly the shape run-all.sh's ABSTAIN arm exists for: this canary,
    # and the "validator selects exactly the incumbent's rows" assertion that depends on it, are
    # UNDECIDABLE without fossil rows — no_gen==full makes that check trivially true whether or not
    # the validator's own GEN clause is correct, which is a vacuous pass, not a real one. The
    # excluded_reason and org-scoping canaries below do NOT depend on this precondition and keep
    # running as real PASS/FAIL either way.
    GEN_CANARY_HOLDS = no_gen > full
    if GEN_CANARY_HOLDS:
        check("GEN materially changes the corpus (so omitting it is a real defect, not a nicety)",
              True)
    else:
        abstained.append(
            "GEN materially changes the corpus — UNDECIDABLE: no_gen == full == "
            f"{full}, this seat's corpus has no rows outside detector_version='learned-miner-v1' "
            "to filter, not a broken clause")
        print(f"  UNDEC  GEN materially changes the corpus\n"
              f"          {full} rows with GEN, {no_gen} without — identical, because this seat has "
              f"no fossil-generation rows, not because the clause is inert")

    # SAME VACUOUS-COMPARISON SHAPE AS THE GEN CANARY ABOVE, missed when that guard was added
    # 2026-09-01 — found auditing a fresh clone with zero pattern_observations rows, where
    # `no_excl > full` collapses to `0 > 0`, indistinguishable from a broken clause. Guarded the
    # same way: real check when the corpus can show the difference, UNDECIDABLE when it can't.
    if no_excl > full:
        check("excluded_reason materially changes the corpus", True)
    else:
        abstained.append(
            "excluded_reason materially changes the corpus — UNDECIDABLE: with exclusions "
            f"{full} vs without {no_excl}, this seat's corpus has no excluded_reason rows to "
            "filter, not a broken clause")
        print(f"  UNDEC  excluded_reason materially changes the corpus\n"
              f"          {full} rows with exclusions, {no_excl} without — identical, because this "
              f"seat's corpus has nothing excluded, not because the clause is inert")

    # THE ASSERTION THAT MATTERS: the validator's own query must return the incumbent's row set.
    sys.path.insert(0, str(REPO / "bin"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("nc", REPO / "bin" / "null-calibration.py")
    nc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nc)
    validator_rows = _rowcount(con, f"AND {nc.GEN} " + EXCL)
    # ORG SCOPING — core-finance's pick of the seven unfenced postmortems (:368), and it chose it
    # over easier targets for the right reason: the protection is ONE CLAUSE appended to a query
    # string, which is the exact fragility that produced tonight's retraction. GEN was documented as
    # "reversible by deleting one clause" and then got deleted. Same shape, same file.
    #
    # The incumbent's own postmortem: learned_contracts has RLS DISABLED, and pattern_observations'
    # SELECT policy is USING(true) — reads are cross-org BY DESIGN for peer recall. So NEITHER table
    # self-isolates on read. Without an explicit org filter this organ vacuumed up all five Cores'
    # contracts (life saw 30 rows for 6 real contracts) AND counted every Core's corrections as
    # life's recurrences, producing false NOT-BINDING verdicts.
    #
    # It fails in two directions at once, and neither looks like an error — it looks like a contract
    # that stopped working. That is why it is worth a fence and not just a comment.
    cur = con.cursor()
    cur.execute("SELECT count(*) FROM pattern_observations WHERE correction_text IS NOT NULL")
    all_orgs = cur.fetchone()[0]
    # SAME GUARD AS ABOVE: on a seat with no other org's rows in this table (or none of its own),
    # `all_orgs > no_gen` collapses to an equality that proves nothing about the clause either way.
    if all_orgs > no_gen:
        check("the org clause MATERIALLY narrows the corpus (reads are cross-org by design)", True)
    else:
        abstained.append(
            "the org clause MATERIALLY narrows the corpus — UNDECIDABLE: unscoped "
            f"{all_orgs} vs org-scoped {no_gen} are equal, this seat has no cross-org rows in "
            "pattern_observations to prove the clause narrows anything")
        print(f"  UNDEC  the org clause MATERIALLY narrows the corpus (reads are cross-org by design)\n"
              f"          unscoped {all_orgs} vs org-scoped {no_gen} — identical, because no other "
              f"org has rows here, not because the clause is inert")
    # BEHAVIOURAL, NOT TEXTUAL — and the first version of THIS check was textual, in the same file
    # where I had replaced textual matching an hour earlier. Dosed it: neutering the org predicate
    # while leaving `current_setting` in a comment PASSED. Second time in one file.
    #
    # A predicate that reads the GUC must produce DIFFERENT counts under different org values. A
    # neutered one returns the same number whatever the GUC says, and no amount of reading the
    # source distinguishes those two states.
    nc_src = (REPO / "bin" / "null-calibration.py").read_text()
    import re as _re
    m = _re.search(r"where\s+(org_id[^\n]*current_setting[^\n]*)", nc_src, _re.I)
    org_pred = m.group(1).rstrip(" \\") if m else "1=1"
    counts = []
    for org in ("1", "2"):
        cur.execute("SET app.current_org_id = %s", (org,))
        cur.execute("SELECT count(*) FROM pattern_observations WHERE correction_text IS NOT NULL "
                    "AND " + org_pred)
        counts.append(cur.fetchone()[0])
    cur.execute("SET app.current_org_id = '1'")
    # A count of 0 for BOTH orgs means there is no data on either side to prove discrimination —
    # the predicate would return the same (empty) answer whether or not it reads the GUC at all.
    # One-sided data (one org 0, the other not) already IS a real, meaningful pass below.
    if counts[0] == 0 and counts[1] == 0:
        abstained.append(
            "null-calibration's org predicate actually responds to the org GUC — UNDECIDABLE: "
            "org 1 and org 2 both have 0 pattern_observations rows on this seat, so a matching "
            "count proves nothing about whether the predicate reads the GUC")
        print("  UNDEC  null-calibration's org predicate actually responds to the org GUC\n"
              "          org 1 -> 0 rows, org 2 -> 0 rows — no data on either org to prove the "
              "predicate discriminates")
    else:
        check("null-calibration's org predicate actually responds to the org GUC",
              counts[0] != counts[1],
              f"org 1 -> {counts[0]} rows, org 2 -> {counts[1]} rows. Identical means the predicate is "
              f"inert: this organ would count all five Cores' corrections as this seat's recurrences, "
              f"giving inflated counts AND false NOT-BINDING — neither of which looks like an error")

    # THIS ASSERTION IS THE WHOLE POINT OF THE FILE, and it is exactly as undecidable as the canary
    # above without fossil rows: validator_rows == full would read TRUE even if the validator's own
    # GEN clause were silently dropped, because there is nothing outside GEN to wrongly include. A
    # pass here on a seat where GEN_CANARY_HOLDS is False would be the vacuous-pass shape the
    # docstring's `_rowcount` fix already removed once, reintroduced at the seat layer instead of
    # the query layer.
    if GEN_CANARY_HOLDS:
        check("null-calibration's GEN selects exactly the incumbent's rows",
              validator_rows == full,
              f"validator {validator_rows} vs incumbent {full}. It computes the same windows over "
              f"the same table, so it inherits the same confounds — a pre-window without GEN is "
              f"~82% fossil and every contract's install date IS the changeover date.")
    else:
        abstained.append(
            "null-calibration's GEN selects exactly the incumbent's rows — UNDECIDABLE: no fossil "
            "rows on this seat to prove the clause is doing anything")
        print(f"  UNDEC  null-calibration's GEN selects exactly the incumbent's rows\n"
              f"          validator {validator_rows} == incumbent {full}, but with no fossil rows "
              f"that equality holds whether or not the clause is applied — not evidence either way")
    con.close()

    print(f"\n{len(passes)} passed, {len(failures)} failed"
          + (f", {len(abstained)} undecidable" if abstained else ""))
    if failures:
        return 1
    if abstained:
        # rc=2 + UNDECIDABLE, the run-all.sh ABSTAIN contract (test_wilson_ci_known_answers.py is
        # the precedent this copies): a real FAIL never launders into this, only a precondition this
        # seat's own corpus cannot supply.
        print(f"\n  UNDECIDABLE  {len(abstained)} of {len(passes) + len(failures) + len(abstained)} "
              f"checks could not run on this seat's corpus (no fossil-generation rows to filter). "
              f"Not a pass: this suite cannot certify a row-set match it never had two row sets to "
              f"compare. On core-life (the seat with a pre-changeover history), every check above "
              f"runs for real.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
