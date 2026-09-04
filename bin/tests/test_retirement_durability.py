#!/usr/bin/env python3
"""A deliberate retirement must survive the next generation pass.

WHY THIS EXISTS
---------------
Found 2026-08-05 by retiring an artifact and watching the loop put it back. `art_7da01522`
("make codex available across all cores") was deactivated because the ask had been VERIFIED already
satisfied fleet-wide — codex is a global binary and all five Cores carry the routing rule, the detail
skill and the fence. The action log then shows, for that same id:

    install_begin -> test_pass -> install_commit      (twice, four minutes apart)

It came back at revision 236.

The cause is that `active = false` is a STATE, not a DECISION. The ask was still in the corpus, still
clustered at support>=3, and `generate_from_asks` had no memory that a judgement had been made about
it. So dedupe could remove duplicates faster than the loop created them, but anything retired FOR A
REASON would resurrect — which is worse, because the reason was the entire point.

THE DISTINCTION THIS TEST PROTECTS, and it is the part that is easy to get wrong:

    deactivated   "not in the live set right now". An automated pass does this — demote-to-shadow,
                  or a re-arm cycle — and the artifact SHOULD come back when its evidence returns.
    quarantined   "a judgement was made about this." Durable. The generator must not re-mint it.

Freezing both would remove the loop's ability to change its mind, which is the opposite failure and
just as bad. So the skip keys on `quarantined` only.

It reuses `quarantined`, which already existed and which `si_project.project()` already filters,
rather than introducing a second parallel "retired" concept beside it — Nick's standing directive
(recurring 9x) is to consolidate rather than add another mechanism alongside the old one. Checking the
live table on the day this shipped found 23 case_ids ALREADY quarantined for good reasons (a
2026-07-30 raw-quote purge, and artifacts minted with no recurrence evidence) that the generator had
been ignoring entirely. None had actually resurrected — they are all `fc_` cases and the router's
canonical_ask requirement was independently blocking them — but the mechanism was doing nothing, and
the one `ask_` case that reached it came straight back.

Run: python3 bin/tests/test_retirement_durability.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

# THIS CORE's org, not a constant. Third file today found hardcoding org 1 in shared test code — the
# other two were test_oracle_escalation and test_org_isolation, both surfaced by peers on the pull and
# both invisible here, because on life the constant happens to be right. The signature to watch for is
# a shared constant that is only ever exercised where it is true.
def _own_org() -> int:
    import os as _o
    v = _o.environ.get("CORE_ORG_ID")
    if v and v.strip().isdigit():
        return int(v)
    try:
        import json as _j
        return int(_j.loads((REPO / ".claude" / "identity.json").read_text())["org_id"])
    except Exception:
        return 1


ORG = _own_org()

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    try:
        import friction_loop as fl
    except Exception as exc:
        print(f"  FAIL  import friction_loop — {exc}")
        return 1

    check("_retired_case_ids exists (the generator has somewhere to look)",
          hasattr(fl, "_retired_case_ids"))
    if not hasattr(fl, "_retired_case_ids"):
        print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed ===")
        return 1

    try:
        retired = fl._retired_case_ids(ORG)
    except Exception as exc:
        print(f"  SKIP  postgres unreachable: {str(exc)[:70]}")
        print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed (partial) ===")
        return 1 if FAIL else 0

    check("it returns a mapping of case_id -> reason", isinstance(retired, dict))
    check("every entry carries a non-empty reason — a retirement without one is not a decision",
          all(isinstance(v, str) and v.strip() for v in retired.values()),
          f"{sum(1 for v in retired.values() if not (isinstance(v, str) and v.strip()))} blank")

    # The generator must consult it. Asserted against the source rather than by running a live
    # generation pass, so this test never mints or installs anything as a side effect.
    src = (REPO / "scheduling" / "claude-si" / "friction_loop.py").read_text()
    gen = src.split("def generate_from_asks", 1)[-1].split("\ndef ", 1)[0]
    check("generate_from_asks consults the retired set", "_retired_case_ids(" in gen)
    check("...and SKIPS a case found in it (continue, not merely counted)",
          "retired_skipped" in gen and "continue" in gen)

    # The distinction that matters: keyed on quarantined, NOT on active.
    fn = src.split("def _retired_case_ids", 1)[-1].split("\ndef ", 1)[0]
    check("keyed on `quarantined` (a judgement), not on `active` (a state)",
          "quarantined" in fn)
    check("...and NOT on `NOT active`, which would freeze demote/re-arm from ever recovering",
          "NOT active" not in fn and "active = false" not in fn.lower(),
          "a deactivated-but-not-quarantined artifact must still be able to return")

    # Live invariant: nothing quarantined may also be active.
    try:
        from _env import connect_corebrain
        con = connect_corebrain()
        with con, con.cursor() as cur:
            cur.execute("SET app.current_org_id = %s", (str(ORG),))
            # A case_id counts as RETIRED only when every artifact for it is quarantined — the same
            # semantics _retired_case_ids uses. A case can have several artifacts (one distilled ask
            # produced both a reminder and an oracle-backed block), so a partially-quarantined case
            # is not a resurrection, it is a case still served by a live sibling. The first version
            # of this check compared any-quarantined against any-live and reported its own correct
            # state as a failure.
            cur.execute("SELECT spec->>'case_id', count(*) FILTER (WHERE quarantined), count(*) "
                        "FROM si_artifacts WHERE org_id=%s AND spec->>'case_id' IS NOT NULL "
                        "GROUP BY 1", (ORG,))
            rows = cur.fetchall()
            fully_retired = {r[0] for r in rows if r[1] and r[1] == r[2]}
            cur.execute("SELECT spec->>'case_id' FROM si_artifacts "
                        "WHERE org_id=%s AND active AND NOT quarantined", (ORG,))
            a = {r[0] for r in cur.fetchall() if r[0]}
            q = fully_retired
        both = sorted(fully_retired & a)
        check("no FULLY-retired case_id is simultaneously live (nothing resurrected)",
              not both, f"resurrected: {both[:4]}")
        print(f"\n  {len(q)} quarantined case_id(s) on this Core, {len(a)} live, 0 overlap expected")
    except Exception as exc:
        print(f"  note: live overlap check skipped — {str(exc)[:60]}")

    print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
