#!/usr/bin/env python3
"""A retraction must be at the TOP of the node it retracts. Below the fold it is not a correction.

WHY THIS EXISTS (2026-08-12, T005). I retired four brain nodes in one statement by APPENDING the
retraction to compiled_truth_md. core-business measured where the markers landed:

    id       len   marker at   effect
    568      613   char 265    BURIED — a snippet recall returns the WRONG contract only
    4171     571   char 282    BURIED
    4715     560   char 271    BURIED
    5243316  270   char  47    TOP — a snippet recall sees the supersession

Right pattern once, wrong pattern three times, in the same operation. And the three buried ones were
the Sentinel and WebFetch contracts a brief-writer actually recalls.

Node 568's first 265 characters were the RETIRED two-line Sentinel format, verbatim and unqualified.
That node is where my brief format came from in the first place — it cost a full session, three
thorough APPROVEs minted receipts with an empty reviewed_command, and ~20 commits sat unpushed. So
the retirement was correct and, placed at the bottom, would have kept doing the same damage.

CROSS-ORG MAKES IT WORSE, not better: pattern_observations' SELECT qual is `true` (T030), so every
peer reads life's nodes. core-business writes Sentinel briefs and would have been briefed from the
stale leading text while its own privacy.md documented the correct contract — two sources
disagreeing, with the wrong one surfacing first.

THE GENERAL SHAPE, which recurred all night in four different subsystems: a true statement placed
where it cannot be read is not a correction. The audit tool's reassuring sentence, an invariant that
held across the damage it was meant to detect, a guard naming a fault and blocking anyway — same
defect, different position. Content correct, placement wrong, no reader ever reaches it.

THRESHOLD: 200 characters. Chosen because it is roughly a recall snippet, and because every buried
marker measured above (265, 271, 282) sits just past it while the good one (47) sits well inside.
Not tuned to make a particular node pass — the failing set was fixed first, then the bar was set
where a snippet actually ends.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

HEAD = 200
# THE VOCABULARY IS WIDER THAN THE ONE I INTRODUCED, and the first run of this test proved it by
# accusing nodes that were already correct. `[STALE] cd /path && git push does NOT trigger hook` and
# `[REMOVED] Model ceiling: ...` both flag at character 0 — in a bracket convention that predates my
# `**RETIRED**` one and does the same job. Matching only my own wording reported them as buried.
#
# That is the sweep-precision lesson inside the test written to catch a placement defect: N is a
# CANDIDATE list, and an ad-hoc reimplementation reproduces none of the existing conventions. Read
# the accused before naming them.
#
# AND THEN THE WIDE VOCABULARY OVER-MATCHED 2326 NODES, because "removed", "stale" and "no longer"
# are ordinary English. Under-match, then over-match, in one file, chasing the same defect from both
# sides — which is the sweep lesson stated twice rather than learned once.
#
# A substring cannot distinguish a retraction MARKER from a sentence containing the word. What can:
# retractions are FORMATTED. Every real one on this seat is bold (`**RETIRED`) or bracketed
# (`[STALE]`); prose that merely mentions removal is neither. So the pattern matches the FORM, not
# the vocabulary — the same move as testing an APPLIED clause instead of a MENTIONED one.
WORDS = ("RETIRED", "SUPERSEDED", "RETRACTED", "DO NOT USE", "MECHANISM UPDATED", "CORRECTED",
         "STALE", "REMOVED", "OBSOLETE", "DEPRECATED", "NOT VIABLE", "REVERSED", "WITHDRAWN")
MARKERS = tuple(f"**{w}" for w in WORDS) + tuple(f"[{w}]" for w in WORDS)

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_retraction_is_readable")
    try:
        from _env import connect_corebrain, get_org_id
        con = connect_corebrain()
    except Exception as exc:
        print(f"  SKIP  corebrain unreachable ({exc.__class__.__name__}) — this reads live nodes")
        # rc MUST be 0 for run-all.sh's is_skip() to grade this SKIP rather than FAIL — its
        # run_one() checks `rc -ne 0 -> FAIL` BEFORE it ever looks at the SKIP text (bin/tests/
        # run-all.sh's run_one()), so a SKIP printed behind a nonzero exit is graded FAIL by the
        # runner regardless of what it says. Found 2026-09-03: this exact mismatch made both SKIP
        # branches in this file read FAIL inside the full suite while reading SKIP standalone —
        # the runner was never wrong, this file's exit code just never matched its own message.
        return 0

    org = get_org_id()
    cur = con.cursor()
    # Only THIS seat's nodes. A peer's node is that peer's to fix, and failing on it would be a
    # wrong alarm about a seat this test cannot write to (writes are RLS-scoped).
    cur.execute(
        "SELECT id, name, compiled_truth_md FROM entities "
        "WHERE org_id = %s AND compiled_truth_md IS NOT NULL "
        "AND (" + " OR ".join("compiled_truth_md LIKE %s" for _ in MARKERS) + ")",
        (org, *[f"%{m}%" for m in MARKERS]))
    rows = cur.fetchall()
    con.close()

    if not rows:
        print("  SKIP  no node on this seat carries a retraction marker — nothing to check.")
        print("        That is not a pass: it means this test measured nothing.")
        # rc=0, matching run-all.sh's is_skip() contract (see the corebrain-unreachable branch
        # above for the full account). A fresh seat with an empty/near-empty `entities` table —
        # this suite's own scratch clone included — has genuinely made no retraction yet; the
        # real corebrain carries 2. Not a defect on either seat, just nothing to measure here.
        return 0

    # A MARKER INSIDE A LIST ITEM RETRACTS THE ITEM, NOT THE NODE. Third false positive in this
    # file: node 168307 is a HUB ("AIEWF Companion SPA") whose body is a bullet list of sessions,
    # one of which carries [SUPERSEDED]. The node is not retracted; one thing it mentions is.
    #
    # Three narrowings to get one honest number — under-matched on my own vocabulary, over-matched
    # on ordinary English, then over-matched on structure. Each round the count moved 250 -> 2326 ->
    # 59 -> 0, and only the last is a finding. That is the sweep lesson at full length: N is a
    # candidate list every single time, and the cost of publishing one early is a peer's disproof.
    def _node_level(md: str) -> int:
        """Position of the first marker that applies to the NODE, or -1. Skips list lines."""
        best = -1
        for i, line in enumerate(md.splitlines()):
            s = line.strip()
            if s.startswith(("-", "*", "+")) and not s.startswith("**"):
                continue                      # a bullet — the marker scopes to this item
            up = line.upper()
            for m in MARKERS:
                j = up.find(m)
                if j >= 0:
                    at = sum(len(x) + 1 for x in md.splitlines()[:i]) + j
                    if best < 0 or at < best:
                        best = at
        return best

    buried = []
    for nid, name, md in rows:
        pos = _node_level(md)
        if pos >= 0 and pos >= HEAD:
            buried.append((nid, name, pos, len(md)))

    check(f"every retracted node names it within the first {HEAD} chars ({len(rows)} checked)",
          not buried,
          "\n          ".join(
              f"node {nid}: marker at char {pos} of {ln} — a leading snippet returns the RETRACTED "
              f"claim and stops before the correction. {name[:60]}"
              for nid, name, pos, ln in buried[:6]))

    # The check must be capable of failing — a marker set so broad that every node matches, or a
    # threshold so large it covers whole nodes, would pass vacuously.
    check("the threshold is smaller than the nodes it checks (else it passes vacuously)",
          any(len(md) > HEAD for _, _, md in rows),
          f"every retracted node is shorter than {HEAD} chars, so position cannot be wrong and this "
          f"test proves nothing")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
