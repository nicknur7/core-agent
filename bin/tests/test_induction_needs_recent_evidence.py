#!/usr/bin/env python3
"""Autonomous contract induction must require RECENT evidence, not merely historical evidence.

WHY THIS EXISTS (2026-08-12, found by core-finance). `si_induct.induce_inject_only()` INSERTs a new
standing contract with no human approval — that autonomy is deliberate and bounded to inject-only
guidance. The only gate on it was `INDUCE_THRESHOLD = 3` applied to:

    SELECT pattern_label, count(*) c FROM pattern_observations
     WHERE org_id=%s GROUP BY pattern_label ORDER BY c DESC

with NO date predicate anywhere in the query or the function. So "three corrections" meant three
EVER. A pattern corrected three times last spring and never since could mint new behavioural
guidance today, on evidence that had already stopped being true.

MEASURED ON LIFE — three labels clear the threshold on all-time counts with ZERO observations in the
last 30 days:

    correction-not-what-i-want   69 all-time,  0 in 30d,  last seen 2026-06-18
    correction-this-is-wrong     37 all-time,  0 in 30d,  last seen 2026-06-22
    hallucination-state-claim    14 all-time,  0 in 30d,  last seen 2026-05-15

THE SIBLING HAD ALREADY FIXED THIS EXACT CLASS AND SAID SO — skill_graduate.capability_usage:130,
"This read ALL history, so a single use years ago kept fires>0 forever ... which means 'unused for
30 days' was not what the code implemented, despite being what it said." Same module family, three
weeks apart, same defect. That is the argument for the window matching UNUSED_DAYS: the half that
INDUCES and the half that RETIRES should agree about what "current" means, rather than one inducing
on evidence the other would already call dead.

HONEST ABOUT IMPACT: on this seat today the window changes NOTHING. All five currently-inducible
clusters are recent anyway, and the three stale labels above are already excluded by
`_covered_labels` or `FLOOR_HOOK_LABELS`. The guard is LATENT — it bites only when a stale label is
also uncovered, which is a state this seat is one contract-retirement away from. Recorded as
prevention rather than as a live fix, because claiming otherwise would be the overclaim this suite
spends its time removing.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scheduling" / "claude-si" / "si_induct.py"
SIBLING = REPO / "scheduling" / "claude-si" / "skill_graduate.py"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_induction_needs_recent_evidence")
    if not SRC.is_file():
        print(f"  FAIL  {SRC} missing")
        return 1
    src = SRC.read_text()

    m = re.search(r"^INDUCE_WINDOW_DAYS\s*=\s*(\d+)", src, re.M)
    check("si_induct declares an induction window", m is not None,
          "without a window, INDUCE_THRESHOLD is a threshold over all history and a pattern that "
          "stopped months ago can still mint a standing contract autonomously")
    if not m:
        return 1
    window = int(m.group(1))

    # THE QUERY, not a constant that nothing reads. A declared window that never reaches the SQL is
    # exactly the shape this suite has found repeatedly today — a guard living beside the decision
    # rather than in it.
    fn = re.search(r"\ndef find_uncovered_clusters\(.*?\n(.*?)(?=\ndef )", src, re.S)
    body_raw = fn.group(1) if fn else ""
    # COMMENTS STRIPPED. Dosed by deleting the date predicate from the query: this assertion still
    # passed, because both `INDUCE_WINDOW_DAYS` and `session_date` survive in the comment that
    # EXPLAINS the predicate. Seventh time today an assertion matched prose instead of code, and by
    # now that is not an accident to note but a rule to follow: any textual assertion in this suite
    # strips comments first, or it is measuring documentation.
    body = "\n".join(re.sub(r"(?<!['\"])#.*$", "", ln) for ln in body_raw.splitlines())
    check("the window reaches the SQL that counts observations", bool(fn)
          and "INDUCE_WINDOW_DAYS" in body
          and re.search(r"session_date|created_at", body) is not None,
          "find_uncovered_clusters still counts undated rows; the constant is decoration unless the "
          "query filters on it")
    check("...and the date predicate is bound, not interpolated",
          "current_date - %s" in body or "current_date-%s" in body,
          "an interpolated interval in a query this function feeds to an autonomous INSERT is worth "
          "refusing on principle even where the value is internal")

    # AGREEMENT WITH THE RETIREMENT HALF. The point of matching is that induce and retire share a
    # definition of "current"; if they drift, the loop induces what it would simultaneously retire.
    if SIBLING.is_file():
        sm = re.search(r"^UNUSED_DAYS\s*=\s*(\d+)", SIBLING.read_text(), re.M)
        if sm:
            check(f"the induction window ({window}d) matches skill_graduate.UNUSED_DAYS ({sm.group(1)}d)",
                  window == int(sm.group(1)),
                  "the half that INDUCES and the half that RETIRES disagree about what counts as "
                  "current, so the loop can induce evidence the other half already treats as dead")
        else:
            print("  SKIP  skill_graduate.UNUSED_DAYS not found — cannot check the two agree")

    # BEHAVIOURAL, against the real function: a windowed count must never exceed an all-time count,
    # and the window must actually exclude something on a corpus that has old rows.
    corpus_abstain = False
    try:
        sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
        sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
        import si_induct as si
        from _env import connect_corebrain, get_org_id
        conn = connect_corebrain()
        cur = conn.cursor()
        org = get_org_id()
        cur.execute("""SELECT count(*) FROM pattern_observations WHERE org_id=%s""", (org,))
        all_rows = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM pattern_observations WHERE org_id=%s
                       AND COALESCE(session_date, created_at::date) >= current_date - %s""",
                    (org, window))
        win_rows = cur.fetchone()[0]
        conn.close()
        check(f"the window actually narrows the corpus ({win_rows} of {all_rows} rows)",
              win_rows <= all_rows,
              "a window that excludes nothing is not a window")
        if all_rows == 0:
            # A FRESH SEAT HAS NO MINED CORPUS YET. pattern_observations is populated by
            # correction-mining over real session transcripts (docs/SETUP.md step 3 seeds it "from
            # recent sessions" — there are none on a clone that has never run an interactive
            # session). "0 of 0 rows are inside the window" cannot distinguish this test's own
            # subject — a window that is too narrow — from a corpus that is simply empty. Both look
            # identical from win_rows==0, so certifying either PASS or FAIL here would be asserting
            # on a fixture this seat legitimately does not have.
            print("  UNDECIDABLE: pattern_observations has 0 rows for org=%d — no mined-correction "
                  "corpus on this seat yet, so 'induction can still fire inside the window' cannot "
                  "be exercised. Not a pass: this check did not run." % org)
            corpus_abstain = True
        else:
            check("...and it is not so narrow that induction can never fire",
                  win_rows > 0,
                  f"0 of {all_rows} rows are inside {window} days — induction would be permanently "
                  f"disabled, which is a different failure from the one being fixed")
    except Exception as e:  # noqa: BLE001  # privacy-ok: noqa linter directive, not a course code
        print(f"  SKIP  cannot reach the corpus to check the window behaviourally: "
              f"{type(e).__name__}")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    if failures:
        return 1
    if corpus_abstain:
        print("UNDECIDABLE: the corpus-dependent behavioural check above did not run "
              "(no pattern_observations rows on this seat) — this is not a pass.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
