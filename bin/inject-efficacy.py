#!/usr/bin/env python3
"""Does an injected reminder actually change behaviour? The one number the loop never measured.

WHY THIS EXISTS
---------------
19 of the loop's artifacts are `inject` mode: they add text to the model's context at a hook event.
The project's founding premise is that ~10,800 tokens of standing prose rules demonstrably FAIL to
graduate — the same correction keeps recurring after the rule exists. Fable put the obvious question
in the sharpest possible form during the 2026-08-05 design review:

    Is a context injection at the right moment materially different from a rule in a file, or is
    this a category error that the shadow-only state is currently hiding?

Nothing in the pipeline could answer that. `correction-rate.py` measures the top line, which at ~17%
artifact coverage cannot resolve the intervention's effect against its own noise margins. Per-artifact
fitness compares recurrence before vs after install, which answers "did the correction stop" but not
"did the reminder do it".

THE MEASUREMENT
---------------
The discriminating question is conditional, not aggregate:

    When a reminder FIRED in a session, did the correction it guards still happen in that same
    session, AFTER it fired?

A correction arriving after its own reminder fired is direct evidence the injection did not bind on
that occasion. Compare against corrections in the same covered class where no reminder fired.

    covered class   a correction whose canonical_ask matches a live artifact's ask
    fired-first     that artifact logged a fire_inject in the same session, at or before the
                    correction's timestamp
    silent          the artifact never fired in that session

If fired-first corrections are as common as silent ones, timing and selectivity are buying nothing
and the inject portfolio is prose rules with extra steps. If fired-first is markedly rarer, the
mechanism is doing work.

WHAT THIS CANNOT DO, STATED UP FRONT
------------------------------------
This is observational, not an experiment. A reminder fires BECAUSE its trigger matched, and the
trigger matched because the session looked like one where the mistake was likely — so fired-first
sessions are selected for elevated risk. That biases AGAINST injection looking good. A clean answer
needs randomised withholding (fire on a coin flip, compare), which is a design change for Nick to
approve, not something to sneak in. So read a bad result as "no evidence it binds" rather than proof
it cannot, and read a good result as genuinely encouraging.

It also reports its own statistical power, because at these volumes the honest answer is often
"not enough data yet" and that must not be dressed up as a finding.

    python3 bin/inject-efficacy.py [--days 28]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
import os
from pathlib import Path

def _core_root() -> Path:
    """This Core's root — env first, then git, then the file's own location.

    Anchoring on __file__ ALONE resolves the wrong Core the moment a peer runs this file from its
    own seat, which is exactly how core-business's clean enforcement-audit result turned out to be
    a second read of life. Correct in normal use, wrong during cross-Core review — and cross-Core
    review is the operation the fleet's autonomy depends on.
    """
    env = os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        p = Path(env).expanduser()
        if (p / ".claude").is_dir():
            return p.resolve()
    try:
        import subprocess as _sp
        top = _sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, timeout=10).stdout.strip()
        if top:
            return Path(top).resolve()
    except Exception:
        pass
    return Path(__file__).resolve().parents[1]


REPO = _core_root()
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

_PREFIX_RX = re.compile(r"^\s*recurring\s+(?:ask|expectation)\s*\([^)]*\)\s*:\s*", re.I)


def norm(msg: str) -> str:
    m = _PREFIX_RX.sub("", str(msg or ""))
    return re.sub(r"\s+", " ", m).strip().lower().rstrip(".")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--org", type=int, default=1)
    args = ap.parse_args()

    try:
        from _env import connect_corebrain, describe_db_failure
    except Exception as exc:
        print(f"cannot import _env: {exc}")
        return 1
    try:
        con = connect_corebrain()
    except Exception as exc:
        print(f"SKIP — {describe_db_failure(exc)}")
        return 0

    # ── live artifacts and the ask each one guards
    active = REPO / ".claude" / "state" / "friction-artifacts" / "active.json"
    if not active.is_file():
        print("SKIP — no active.json; nothing is installed on this Core")
        return 0
    raw = json.loads(active.read_text())
    arts = raw if isinstance(raw, list) else raw.get("artifacts", [])
    guard = {}
    for a in arts:
        if (a.get("effect") or {}).get("mode") != "inject":
            continue
        k = norm((a.get("effect") or {}).get("message"))
        if len(k) >= 20:
            guard[a["artifact_id"]] = k
    if not guard:
        print("SKIP — no inject-mode artifacts installed")
        return 0

    # ── fires per (artifact, session), earliest first
    log = REPO / ".claude" / "state" / "friction-action-log.jsonl"
    fires: dict[tuple[str, str], int] = {}
    if log.is_file():
        for line in log.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("action") not in ("fire_inject", "fire_block"):
                continue
            sid, aid = str(r.get("session_id") or ""), str(r.get("artifact_id") or "")
            try:
                ts = int(r.get("ts") or 0)
            except Exception:
                continue
            if not (sid and aid and ts):
                continue
            k = (aid, sid)
            fires[k] = min(fires.get(k, ts), ts)

    # ── corrections in the window, with their session and epoch
    with con, con.cursor() as cur:
        cur.execute(
            """SELECT canonical_ask, source_file, extract(epoch FROM created_at)::bigint
                 FROM pattern_observations
                WHERE org_id = %s
                  AND canonical_ask IS NOT NULL AND btrim(canonical_ask) <> ''
                  AND COALESCE(session_date, created_at::date) >= (CURRENT_DATE - %s)""",
            (args.org, args.days))
        rows = cur.fetchall()

    total = len(rows)
    covered = 0
    fired_first = 0
    silent = 0
    per_art = defaultdict(lambda: {"fired_first": 0, "silent": 0})

    for ask, src, epoch in rows:
        a_norm = norm(ask)
        sid = Path(str(src or "")).stem
        # A correction is "covered" when a live artifact's ask contains it or vice versa — the
        # loosest of the three identity relations in the codebase, chosen deliberately so coverage
        # is not undercounted and the result is not flattered.
        hits = [aid for aid, g in guard.items() if a_norm and (a_norm in g or g in a_norm)]
        if not hits:
            continue
        covered += 1
        got_fire = False
        for aid in hits:
            f = fires.get((aid, sid))
            if f is not None and f <= epoch:
                got_fire = True
                per_art[aid]["fired_first"] += 1
                break
        if got_fire:
            fired_first += 1
        else:
            silent += 1
            for aid in hits:
                per_art[aid]["silent"] += 1

    print(f"inject efficacy — last {args.days}d, org {args.org}")
    print(f"  inject artifacts live        {len(guard)}")
    print(f"  corrections with an ask      {total}")
    print(f"  of those, COVERED by one     {covered}"
          + (f"  ({100*covered/total:.0f}%)" if total else ""))
    print()
    if covered == 0:
        print("  NO covered corrections in the window. Nothing to conclude: either the artifacts")
        print("  guard asks that did not recur (good) or coverage is too narrow to measure (unknown).")
        print("  These are different and this measurement cannot separate them.")
        return 0

    print(f"  reminder FIRED, correction still happened   {fired_first}"
          f"  ({100*fired_first/covered:.0f}% of covered)")
    print(f"  reminder silent, correction happened        {silent}"
          f"  ({100*silent/covered:.0f}% of covered)")
    print()
    # Honest power statement. With n this small a difference of a few is noise, and saying so is the
    # whole point — the failure mode being guarded against here is a confident number over n=3.
    if covered < 20:
        print(f"  VERDICT: UNDERPOWERED. n={covered} covered corrections is too few to distinguish")
        print("  'injection does not bind' from chance. Do not act on the ratio above. What would")
        print("  make it decisive: more coverage, a longer window, or randomised withholding.")
    elif fired_first == 0 and silent > 0:
        # THE TRAP THIS BRANCH EXISTS TO AVOID. The first version of this script read
        # `fired_first == 0` as "no correction ever survived its reminder — consistent with the
        # injections binding", and printed that against silent=29. It is the opposite. Zero
        # fired-first with a large silent count means the reminder was ABSENT every single time its
        # own mistake occurred: the artifact never fired in any of those sessions. That is a
        # targeting failure, and reporting it as success would have been exactly the
        # measure-your-own-homework pathology the rest of this codebase's comments hunt.
        #
        # "Zero fired-first" is only good news when `silent` is ALSO near zero — i.e. the correction
        # stopped happening at all. The two cases are distinguished by `silent`, never by
        # `fired_first` alone.
        print(f"  VERDICT: TARGETING FAILURE, not evidence of binding. All {silent} covered")
        print("  corrections happened in sessions where the guarding artifact NEVER FIRED. The")
        print("  reminder is absent precisely when its own mistake occurs, so this measures nothing")
        print("  about whether injection binds — it shows the triggers do not match the situations")
        print("  the corrections arise in. Fix targeting before drawing any conclusion about")
        print("  efficacy. (fitness calls this NOT-BINDING-NO-FIRE; here it is the whole portfolio.)")
    elif fired_first == 0:
        print("  VERDICT: no covered correction recurred at all in this window. Consistent with the")
        print("  injections binding — though selection bias runs the other way, so this is")
        print("  encouraging rather than conclusive.")
    elif fired_first >= silent:
        print("  VERDICT: corrections are AT LEAST AS common after the reminder fired as when it")
        print("  stayed silent. No evidence the injection binds. If this holds as n grows, the")
        print("  inject portfolio is prose rules with a keyword filter, and effort belongs in")
        print("  oracle-backed detection instead.")
    else:
        print("  VERDICT: corrections are rarer when the reminder fired. Weak positive evidence,")
        print("  against a selection bias that works in the opposite direction.")

    live = {a: v for a, v in per_art.items() if v["fired_first"] or v["silent"]}
    if live:
        print("\n  per artifact (fired-first / silent):")
        for aid, v in sorted(live.items(), key=lambda kv: -(kv[1]["fired_first"] + kv[1]["silent"])):
            print(f"    {aid}  {v['fired_first']:>3} / {v['silent']:>3}   {guard.get(aid,'')[:58]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
