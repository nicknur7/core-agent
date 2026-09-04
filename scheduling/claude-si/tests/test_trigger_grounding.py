#!/usr/bin/env python3
"""test_trigger_grounding.py — locks the GAP C fix (2026-08-31): fc_-case trigger derivation
grounded against the ask's own real siblings, not friction_miner's pattern_label bucket.

Root-cause measurement this fix responds to (org 1, live DB, 2026-08-31): 33 of 36 all-time
friction_cases denials were `no_trigger_terms`, because friction_router.route() was grounding
candidate words against `support["members"]` — friction_miner.compute_support's grouping by
`cluster_key` = pattern_label (the correction TYPE), not the ask. fc_16358fc0's own ask
("verify state against the live source before claiming") has 46 sibling ROWS sharing its exact
canonical_ask in pattern_observations, none of which that grouping ever consulted.

Read-only throughout — every DB call here is a SELECT (ask_miner.sibling_moments_for_ask,
ask_miner._base_rates, artifact_typer.route_type's filesystem reads). Nothing in this file writes
to friction_cases, si_artifacts, or any other corebrain table.

  CORE_ORG_ID=1 python3 tests/test_trigger_grounding.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "brain-pg"))
from _env import connect_corebrain, get_org_id  # noqa: E402

_ORG = get_org_id()  # identity decides, never a hardcoded org (tasks/lessons.md 2026-07-30)

import ask_miner as am     # noqa: E402
import friction_router as fr  # noqa: E402

_fails = []
_abstains = []
def check(name, cond, d=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {d}" if d and not cond else ""))
    if not cond: _fails.append(name)


def abstain(name, reason):
    """Record a check this seat's data cannot settle — UNDECIDABLE, not FAIL, matching
    run-all.sh's rc==2 + "UNDECIDABLE" contract (bin/tests/test_wilson_ci_known_answers.py,
    bin/tests/run-all.sh declared_to_certify()). A precondition genuinely absent from a seat's
    corpus is missing evidence, not a defect; asserting through it either way would be the exact
    dishonesty the ratchet exists to catch."""
    print(f"  UNDECIDABLE  {name} — {reason}")
    _abstains.append(name)


def test_need_cap_is_noop_at_or_below_six_prompts():
    """Judge-required change 4: `need = max(1, min(len(prompts)//2, 3))` must be BYTE-IDENTICAL
    in behaviour to the pre-fix `need = max(1, len(prompts)//2)` for every input the EXISTING
    caller (ask_cases -> _member_prompts, limit=6) can ever produce — n in [0, 6]. This is the
    "no-op for the existing ask lane" claim the judge's decision rests on; if it ever stops
    holding, the cap has silently started changing ask_cases()'s own behaviour, not just the new
    fc_-lane caller's."""
    for n in range(0, 7):
        old = max(1, n // 2)
        new = max(1, min(n // 2, 3))
        check(f"need cap no-op at n={n} prompts", old == new, f"old={old} new={new}")


def test_need_cap_diverges_above_six_prompts():
    """The cap has to DO something once a caller passes more than 6 prompts, or "the fix" is
    inert and the 92%-of-funnel no_trigger_terms measurement stays unexplained. fc_16358fc0's own
    ask supplies 37 real siblings once deduped to moments — the exact shape the fc_ lane hands
    this function that ask_cases() never could."""
    for n in (10, 20, 37):
        old = max(1, n // 2)
        new = max(1, min(n // 2, 3))
        check(f"need cap diverges above six prompts at n={n}", new < old, f"old={old} new={new}")


def test_rank_ask_terms_and_wrapper_agree():
    """`_trigger_from_ask` must be a THIN wrapper over `_rank_ask_terms` — same words, just
    `\\b...\\b`-wrapped — so friction_router's fc_ lane (which needs bare words for its own
    `_word_re`/`_occurs` machinery) and ask_cases (which needs ready-to-use regexes) are
    guaranteed to agree on WHICH terms a given ask grounds to. Base rates are pinned to a
    controlled fixture rather than the live corpus so this test's verdict does not drift as
    Nick's corpus grows."""
    old_cache = am._BASE_CACHE
    try:
        # apple/banana rare+equal (lift ties broken by length, both >0 base rate); widget absent
        # from every prompt below so it can never reach the count threshold.
        am._BASE_CACHE = ({"apple": 4, "banana": 4}, 200)
        ask = "apple banana widget"
        prompts = ["please apple banana now", "apple banana again", "need apple banana today",
                   "apple banana please respond", "apple banana over here", "final apple banana word"]
        top = am._rank_ask_terms(ask, prompts)
        wrapped = am._trigger_from_ask(ask, prompts)
        check("both terms grounded (apple, banana both meet need=3 of 6)", len(top) == 2, top)
        check("wrapper == regex-wrapped ranker output",
              wrapped == [r"\b" + re.escape(w) + r"\b" for w in top], (wrapped, top))
        check("widget excluded (0 occurrences in prompts)", "widget" not in top, top)
    finally:
        am._BASE_CACHE = old_cache  # never leave the process-wide cache pinned for later tests


def test_sibling_moments_dedupes_rows_to_moments():
    """The fc_-lane equivalent of ask_cases()'s own moment-dedupe (judge-required change 1):
    counting ROWS lets one bad afternoon, re-extracted or re-mined, fake N-of-siblings term
    support. Verified against fc_16358fc0's real ask, measured live 2026-08-31 at 46 rows / 37
    moments — asserted here as a RELATIVE property (deduped < raw rows) so the test does not need
    updating as more corrections accrue, plus the live count as evidence.

    THE FIXTURE IS THE ASK'S OWN RE-EXTRACTION DUPLICATES, and that is per-Core data, not code.
    core-business, 2026-09-01: this ask has 8 raw rows on business vs. life's 46, and all 8 are
    genuinely distinct (correction_text, date) pairs — zero re-extraction duplicates, so
    `len(moments) < raw_rows` is unsatisfiable BY THE DATA, not by a broken dedup. Verified
    directly: `GROUP BY (COALESCE(correction_text,prompt_text), COALESCE(session_date,
    created_at::date)) HAVING COUNT(*) > 1` over business's 8 rows returns zero groups. The dedup
    mechanism (DISTINCT ON the same pair, in ask_miner.sibling_moments_for_ask) is unchanged and
    still asserted in full when a seat's corpus actually contains a duplicate to collapse — this
    only tells the difference between "nothing to dedupe" and "dedupe is broken," which the raw
    count alone cannot."""
    ask = "verify state against the live source before claiming"
    con = connect_corebrain()
    try:
        cur = con.cursor()
        # COUNT THE POPULATION THE DEDUP ACTUALLY SEES — excluded_reason IS NULL.
        #
        # This counted ALL rows, while sibling_moments_for_ask (ask_miner.py) selects
        # `DISTINCT ON (...) WHERE ... AND excluded_reason IS NULL`. So `len(moments) < raw_rows`
        # could be satisfied purely by EXCLUDED rows being dropped, with the dedup collapsing
        # nothing at all — an assertion passing for a reason unrelated to the property it claims.
        #
        # Reachable and live, not theoretical: measured on THIS ask, org 1 holds 46 rows of which 3
        # are excluded, so `moments` could never exceed 43 and the comparison was true by
        # construction. Counting the same filtered population makes the strict-reduction claim mean
        # only what it says — the dedup collapsed at least one duplicate MOMENT.
        #
        # Same shape the brain already records for learned-corpus-miner.py:263, which counted
        # pattern_observations with no org_id filter and subtracted a filtered number from it.
        #
        # Found chasing a Codex lead that was itself wrong: it argued dup_groups could abstain while
        # a real duplicate existed, which is refuted because sibling_moments_for_ask filters
        # excluded_reason exactly as the dup_groups query does. The genuine asymmetry was one query
        # above the one it named.
        cur.execute("SELECT count(*) FROM pattern_observations "
                    "WHERE org_id=%s AND canonical_ask=%s AND excluded_reason IS NULL",
                    (_ORG, ask))
        raw_rows = cur.fetchone()[0]
        # Does this seat's corpus for THIS ask actually contain a duplicate moment to collapse?
        # Same (correction_text/prompt_text, date) pair sibling_moments_for_ask dedupes on.
        cur.execute(
            "SELECT count(*) FROM ("
            "  SELECT 1 FROM pattern_observations"
            "   WHERE org_id=%s AND canonical_ask=%s AND excluded_reason IS NULL"
            "     AND COALESCE(correction_text, prompt_text) IS NOT NULL"
            "   GROUP BY COALESCE(correction_text, prompt_text), "
            "            COALESCE(session_date, created_at::date)"
            "  HAVING count(*) > 1"
            ") dup_groups", (_ORG, ask))
        dup_groups = cur.fetchone()[0]
    finally:
        con.close()
    moments = am.sibling_moments_for_ask(_ORG, ask, limit=1000)
    if raw_rows == 0:
        # THE FIXTURE IS THIS SEAT'S OWN CORPUS FOR THIS EXACT HARDCODED ASK. This function's own
        # module docstring measures it live on org 1: "46 sibling ROWS sharing its exact
        # canonical_ask" — that is this operator's mined-correction history, not a shared fixture.
        # A fresh seat (a fork, a clean clone, this suite's own audit tooling) has zero
        # pattern_observations rows and therefore zero for this ask by construction. An empty
        # `moments` list is then the CORRECT return, not evidence sibling_moments_for_ask is
        # broken — asserting len(moments) > 0 here cannot distinguish "works, nothing to find"
        # from "broken, finds nothing it should have". Same shape as the dup_groups==0 abstain
        # three lines below, just one level up: no raw rows means no dup_groups either.
        check("isinstance(moments, list) at least (even on an empty corpus)",
              isinstance(moments, list), moments)
        abstain(f"sibling_moments_for_ask returns actual siblings for this ask (raw rows=0)",
                "this seat's pattern_observations has zero rows for this hardcoded ask — nothing "
                "to return siblings from, not a defect. Real on any seat with mined corrections.")
    else:
        check(f"sibling_moments_for_ask exists and returns a list (raw rows={raw_rows})",
              isinstance(moments, list) and len(moments) > 0, moments[:1])
    if dup_groups == 0:
        # No re-extraction duplicate exists in this seat's corpus for this ask -- the property
        # this check exists to prove (dedup COLLAPSES a real duplicate) has nothing to act on
        # here. Asserting len(moments) < raw_rows anyway would be asserting through a coin flip
        # that happens to come up "no duplicates," not testing the mechanism.
        abstain(f"moment-dedupe strictly reduces count (rows={raw_rows}, moments={len(moments)})",
                f"this seat's corpus has 0 duplicate-moment groups for this ask (rows={raw_rows} "
                f"are all distinct correction_text+date pairs) -- nothing to dedupe here, not a "
                f"defect. Re-checked whenever this test runs; asserts in full the moment a real "
                f"duplicate lands.")
    else:
        check(f"moment-dedupe strictly reduces count (rows={raw_rows}, moments={len(moments)}, "
              f"{dup_groups} real duplicate group(s) present)",
              len(moments) < raw_rows, f"rows={raw_rows} moments={len(moments)}")


def _fc_case(canonical_ask: str, case_id: str) -> dict:
    """Minimal fc_-case-shaped dict — just enough of build_case()'s shape for route() to run
    against, without touching a transcript. quality/support are set past the gates BEFORE the
    one under test in each function below, so a failure there cannot be mistaken for the gate this
    test actually targets."""
    return {
        "case_id": case_id, "org_id": _ORG,
        "quality": {"eligible_for_routing": True},
        "canonical_ask": canonical_ask,
        "user_wanted": canonical_ask,
        "moment": {"correction": canonical_ask},
        "support": {"cluster_key": "test-cluster", "distinct_sessions": 2,
                    "positive_case_ids": ["fc_sibling_test"], "members": []},
    }


def test_route_type_precheck_diffuse_directive():
    """Judge-required change 3: a diffuse standing preference must be reclassified BEFORE this
    lane tries (and fails) to derive a lexical trigger for it — it is already served by
    generate_from_asks() -> artifact_typer.route_type() == claude_md_directive, via the ask lane,
    off the SAME canonical_ask. Phrase chosen to hit DIRECTIVE_SIGNALS without also matching any
    COVERED_CONCEPTS / ORACLE_CATALOG / cadence signal first (route_type's own precedence order)."""
    case = _fc_case("consolidate patched, redundant subsystems into one clean, efficient design",
                     "test_directive_case")
    spec = fr.route(case)
    check("directive ask refused (no spec)", spec is None, spec)
    check("drop reason names the ask-lane terminal, not no_trigger_terms",
          case.get("_drop_reason") == "served_by_ask_lane:claude_md_directive",
          case.get("_drop_reason"))


def test_route_type_precheck_cadence():
    """Same defect, the other route_type terminal: a cadence ask needs a clock, not a
    prompt_regex — scheduled_job_proposal, also already served by the ask lane."""
    case = _fc_case("run a full data backup every session", "test_cadence_case")
    spec = fr.route(case)
    check("cadence ask refused (no spec)", spec is None, spec)
    check("drop reason names the ask-lane terminal, not no_trigger_terms",
          case.get("_drop_reason") == "served_by_ask_lane:scheduled_job_proposal",
          case.get("_drop_reason"))


def test_route_still_refuses_ungroundable_ask():
    """Fail-toward-silence is unchanged: an ask that is not diffuse/cadence AND cannot ground two
    real co-occurring terms in its own siblings still refuses, honestly, as no_trigger_terms — the
    fix recovers asks with real grounded evidence, not every ask."""
    case = _fc_case("xyzqfoo wibblesnarf zzqorbit made up nonsense phrase", "test_ungroundable_case")
    spec = fr.route(case)
    check("ungroundable ask still refused (no invented trigger)", spec is None, spec)
    check("refused for lack of terms, not misrouted",
          case.get("_drop_reason") == "no_trigger_terms", case.get("_drop_reason"))


if __name__ == "__main__":
    for fn in [test_need_cap_is_noop_at_or_below_six_prompts, test_need_cap_diverges_above_six_prompts,
               test_rank_ask_terms_and_wrapper_agree, test_sibling_moments_dedupes_rows_to_moments,
               test_route_type_precheck_diffuse_directive, test_route_type_precheck_cadence,
               test_route_still_refuses_ungroundable_ask]:
        print(fn.__name__); fn()
    if _fails:
        print(f"\nFAILURES: {', '.join(_fails)}")
        sys.exit(1)
    if _abstains:
        # rc==2 + "UNDECIDABLE" -- run-all.sh's declared_to_certify() and
        # test_tests_do_not_write_live_state.py's DOSE loop both grade this as "declined to
        # certify here, no fixture, not a defect" rather than a failure. Every OTHER check in
        # this file still ran and passed; only the specific fixture (a real duplicate moment for
        # this ask) is seat-dependent.
        print(f"\nUNDECIDABLE  {len(_abstains)} check(s) had no fixture on this seat "
              f"({'; '.join(_abstains)}); every other check passed. Not a pass: this run cannot "
              f"certify the property it abstained on.")
        sys.exit(2)
    print("\nALL PASS")
    sys.exit(0)
