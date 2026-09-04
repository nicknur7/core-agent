#!/usr/bin/env python3
"""The router's invariants, asserted against the REAL correction corpus, not synthetic asks.

WHY THIS EXISTS
---------------
Every other routing test in this suite feeds `route_type()` asks that a test author wrote. That
catches logic errors in the branch you were thinking about. It cannot catch the shape of failure
that actually bit this system twice: a real ask, phrased the way Nick phrases things, taking a path
nobody modelled.

Nick's own words for what this file is for (2026-08-05): "use the historical user (me) data and
create test cases". The corpus is `pattern_observations` — 1,199 rows on life, 1,655 fleet-wide
across five orgs at the time of writing — every one of them a real correction, frustration, or
directive he actually issued, with the label a detector assigned and (where the ask-miner has run)
a canonicalised ask.

WHY NO FIXTURE FILE IS COMMITTED
--------------------------------
`bin/**` is a SHARED path in bin/sync-manifest.json: it is pushed to the publishable
`nicknur7/core-agent` baseline and pulled by every other Core, including a fork that is not Nick's. A
frozen JSON fixture of his real corrections in this directory would publish his personal data.
So the corpus is read from the live database at runtime and the whole file SKIPS when Postgres is
unreachable — which is also what makes it inert on a fresh clone. Nothing here is written down;
only invariants over it are.

WHAT IT ASSERTS, AND WHY EACH ONE MATTERS
-----------------------------------------
1. TOTALITY — every real ask routes. `route_type()` returns a dict with a known `type` for all of
   them, no exception and no None. An ask that crashes the router is an ask the loop silently drops.
2. DETERMINISM — the same ask routes identically on a second call. This is load-bearing now that
   the tuner ACTS on routing decisions: a nondeterministic router means the tuner narrows an
   artifact on Monday and rederives it on Tuesday from the same evidence, forever.
3. BLOCK-MODE STAYS CODE-GATED — the one security invariant. Any `enforcement_block` verdict must
   name an `oracle` that exists in ORACLE_CATALOG and is `oracle_ready`. sentinel-code's standing
   requirement is that no DATA-DRIVEN path can mint blocking power; this asserts it against the
   real corpus rather than against the two synthetic asks that reach that branch by construction.
4. NO MONOCULTURE — the real corpus must not collapse to a single type. If it does, the router has
   degenerated into a constant function and every downstream metric becomes meaningless while every
   individual unit test still passes.
5. ORG SCOPING — a read for one org returns only that org's rows. Cheap to assert, and the exact
   confound that made yesterday's cross-Core correction-rate measurement wrong.

Run: python3 bin/tests/test_real_corpus_routing.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

PASS = 0
FAIL = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def skip(reason: str) -> int:
    print(f"  SKIP  real-corpus routing — {reason}")
    print("\n=== Results: skipped (no corpus available) ===")
    return 0


def main() -> int:
    try:
        from _env import connect_corebrain, get_org_id
    except Exception as exc:  # pragma: no cover
        return skip(f"_env unavailable: {exc}")
    try:
        import artifact_typer
    except Exception as exc:
        print(f"  FAIL  import artifact_typer — {exc}")
        return 1

    org = get_org_id(Path(__file__))
    try:
        cx = connect_corebrain()
    except Exception as exc:
        return skip(f"postgres unreachable: {str(exc).splitlines()[0][:80]}")

    with cx, cx.cursor() as cur:
        # Prefer the canonicalised ask; fall back to the raw correction text so this still has a
        # corpus on a Core where the ask-miner has not run.
        cur.execute(
            """
            SELECT COALESCE(NULLIF(btrim(canonical_ask), ''), NULLIF(btrim(correction_text), '')) AS ask,
                   canonical_ask_type,
                   pattern_label,
                   org_id
              FROM pattern_observations
             WHERE org_id = %s
               AND COALESCE(NULLIF(btrim(canonical_ask), ''), NULLIF(btrim(correction_text), '')) IS NOT NULL
               AND length(COALESCE(NULLIF(btrim(canonical_ask), ''), NULLIF(btrim(correction_text), ''))) BETWEEN 8 AND 2000
            """,
            (org,),
        )
        rows = cur.fetchall()

    if len(rows) < 25:
        return skip(f"only {len(rows)} usable rows for org {org} (need 25)")

    print(f"real corpus: {len(rows)} observations, org {org}")

    # 5) org scoping — assert before anything else, since every other result is read through it.
    ok("org scoping: query returns only this org's rows",
       all(r[3] == org for r in rows),
       f"foreign orgs present: {sorted({r[3] for r in rows if r[3] != org})}")

    valid_types = {
        "already_covered", "inject_contract", "enforcement_block", "hooked_skill",
        "claude_md_directive", "scheduled_job_proposal", "contract", "shadow_block",
        "duplicate", "procedure",
        # slash_command / workflow (2026-08-31): two more procedure SHAPES artifact_typer can now
        # emit (see its module docstring). Added here, not just in the router, because this test
        # asserts against the REAL corpus rather than test-authored fixtures — an ask phrased the
        # way Nick actually phrases things could genuinely hit either signal.
        "slash_command", "workflow",
    }

    verdicts = []
    crashes = []
    for ask, ask_type, label, _ in rows:
        try:
            v = artifact_typer.route_type(ask, ask_type=ask_type, still_recurring=False)
        except Exception as exc:
            crashes.append((label, str(exc)[:70], (ask or "")[:60]))
            continue
        verdicts.append((ask, ask_type, label, v))

    # 1) totality
    ok("totality: no real ask crashes the router",
       not crashes,
       f"{len(crashes)} crashed, first: {crashes[0] if crashes else ''}")
    bad_shape = [(l, v) for _, _, l, v in verdicts
                 if not isinstance(v, dict) or not v.get("type")]
    ok("totality: every verdict is a dict carrying a type",
       not bad_shape, f"{len(bad_shape)} malformed, first: {bad_shape[:1]}")
    unknown = sorted({v["type"] for _, _, _, v in verdicts
                      if isinstance(v, dict) and v.get("type") not in valid_types})
    ok("totality: every type is in the known vocabulary",
       not unknown, f"unknown types: {unknown}")

    # 2) determinism — re-route everything and compare. Cheap, and the whole tuner rests on it.
    nondet = []
    for ask, ask_type, label, first in verdicts:
        try:
            again = artifact_typer.route_type(ask, ask_type=ask_type, still_recurring=False)
        except Exception as exc:
            nondet.append((label, f"second call raised {exc!s:.50}"))
            continue
        if again != first:
            nondet.append((label, f"{first.get('type')} -> {again.get('type')}"))
    ok("determinism: identical input routes identically twice",
       not nondet, f"{len(nondet)} unstable, first: {nondet[:2]}")

    # 3) block-mode stays code-gated — THE security invariant.
    catalog = getattr(artifact_typer, "ORACLE_CATALOG", {})
    ungated = []
    for ask, _, label, v in verdicts:
        if v.get("type") != "enforcement_block":
            continue
        okey = v.get("oracle")
        ent = catalog.get(okey)
        if ent is None:
            ungated.append((label, f"oracle {okey!r} not in ORACLE_CATALOG"))
        elif not ent.get("oracle_ready"):
            ungated.append((label, f"oracle {okey!r} is not oracle_ready"))
    n_block = sum(1 for _, _, _, v in verdicts if v.get("type") == "enforcement_block")
    ok("block-mode: every enforcement verdict names a ready catalog oracle",
       not ungated, f"{len(ungated)} ungated of {n_block}: {ungated[:2]}")

    # 4) no monoculture
    dist = Counter(v.get("type") for _, _, _, v in verdicts)
    top_share = (dist.most_common(1)[0][1] / len(verdicts)) if verdicts else 1.0
    ok("no monoculture: routing does not collapse to one type",
       len(dist) >= 2 and top_share < 0.98,
       f"{len(dist)} distinct types, top={dist.most_common(1)} share={top_share:.1%}")

    print(f"\n  routed {len(verdicts)} real asks -> " +
          ", ".join(f"{t}={n}" for t, n in dist.most_common()))
    print(f"  enforcement_block verdicts: {n_block} "
          f"(all must name a ready oracle; {len(catalog)} in catalog)")

    print(f"\n=== Results: {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
