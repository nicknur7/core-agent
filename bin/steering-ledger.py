#!/usr/bin/env python3
"""steering-ledger — is each steering component earning its keep?

THE FITNESS FUNCTION, in Nick's words (2026-07-30): "the cost of token injecting should be lower
than the cost of me re-explaining what that contract would have and the back and forth when you
could have known what I wanted for way less tokens."

    value(component) = (tokens Nick would spend re-explaining
                        + tokens of the wasted turns that follow)
                     −  tokens the component costs to inject

This is the fourth leg of the measurement stack and the one that ties the others together:

    bin/grade-gate.py       a RATE          — how often does it fire on the real corpus
    bin/grade-intent.py     PURPOSE         — does it still catch what it was built to catch
    steering_liveness.py    DISPATCH        — does it run at all, and can its effect be seen
    THIS                    VALUE           — is the firing worth what it costs

WHAT IS MEASURED HONESTLY, AND WHAT IS NOT
------------------------------------------
COST is measured, not estimated: hooklog.emit records tokens_injected on every emission, so the
per-fire and per-session cost of a component is a fact.

BENEFIT is NOT directly measurable and this tool does not pretend otherwise. The counterfactual —
"what would Nick have had to re-explain if this had not fired" — is unobservable. Reporting a
made-up number for it would be worse than reporting none, because a fabricated benefit column is
exactly what would justify keeping everything.

So the benefit side is reported as the two things that ARE observable, and the verdict is drawn
from their relationship rather than from a single invented score:

  · RECURRENCE TREND — is the guarded behaviour still happening, and is it declining? A gate
    whose behaviour has stopped occurring is either working or obsolete, and the distinction is
    whether it still fires. A gate that fires constantly AND whose behaviour is not declining is
    not binding — that is the NOT-BINDING verdict measure-contract-fitness already uses.
  · SEVERITY VIA RARITY — rarity is not worthlessness. cross-core-completion-gate fires 3 times
    in 74 sessions and caught a real overclaim. The term is rarity x severity, and severity is a
    judgement recorded in the intent record, not something a counter can derive.

VERDICTS
  COST-BLIND     fires, but its cost predates instrumentation — NOT a pass, just unknown
  EXPENSIVE      high cost per session, behaviour not declining — pays a lot, changes little
  PRE-EMPTED     the failure is now impossible by construction (the clock), so any firing is
                 pure cost — retire the gate, keep the supply
  LOW-YIELD      >=20 runs and under 10% of them act — runs constantly, catches almost nothing.
                 Distinct from RARE-VALUABLE by the DENOMINATOR, which is why it needed universal
                 invoke instrumentation before it could exist.
  EARNING        fires, cost is modest, behaviour is declining
  RARE-VALUABLE  fires seldom; kept on severity, which the intent record has to justify
  UNMEASURED     effect is a side effect the fire counter cannot see (see Phase 0.7)
  NO-DATA        registered, no telemetry yet

  python3 bin/steering-ledger.py [--days 14] [--json]
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[1])
LOG = ROOT / ".claude" / "state" / "hook-events.log"

def _rotated_text(_p):
    """Delegates to core_paths.read_rotated — the canonical rotation resolver.

    This file carried its own copy of the rotation rule. So did steering-ledger.py and
    refresh-hook-dispositions.py, all three byte-identical, and core_paths.rotated_set was written
    on 2026-08-12 to BE the single resolver for exactly these readers — its docstring names them by
    line number. The consolidation was stated and the copies shipped anyway.

    Verified equivalent before delegating, on a fixture covering the cases the local copy called out:
    numeric suffix ordering (.10 is OLDER than .2, where a lexical sort gets it backwards) and
    non-numeric siblings (.log.gz) ignored rather than guessed at. Same output, same order.

    Same argument as core_paths' own: "four copies of a rotation rule is four places for the next
    generation suffix to be missed".
    """
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    from core_paths import read_rotated as _rr
    return _rr(_P(_p))

REG = ROOT / "bin" / "hook-registry.json"

ROW = re.compile(
    r"^(?P<ts>\S+) \| hook=(?P<hook>[^|]*?) \| event=(?P<event>[^|]*?) \| verdict=(?P<verdict>[^|]*?) "
    r"\| session=(?P<session>[^|]*?)(?: \| detail=(?P<detail>[^|]*?))?"
    r"(?: \| tokens_injected=(?P<tok>\d+))? \| excerpt=(?P<ex>.*)$")

# A component whose failure has been made impossible by construction. Firing is then pure cost
# regardless of how correct the firing is — the Phase 1 "supply beats policing" outcome.
# EMPTY ON PURPOSE, and the reason is a correction to my own earlier entry.
#
# time-claim-gate was listed here as PRE-EMPTED, on the reasoning that the injected clock makes
# its failure impossible. That is true of the TIME-OF-DAY half and false of the duration and
# session-start half — the clock gives NOW and says nothing about when something started, which
# is exactly what the allowlist fix in time-claim-gate.py establishes. So the verdict said
# "retire the gate" about a hook that is still the only thing gating duration claims, which
# CLAUDE.base.md documents as enforced.
#
# It is worse than a wrong verdict, because bin/hook-registry.json is BASELINE-SHARED: a
# tombstone written from life's local telemetry propagates to all four peers on the next push,
# retiring a hook fleet-wide on one Core's reading. (Fable, blast-radius review.)
#
# An entry here must mean the failure is impossible in FULL, not in part. A partially pre-empted
# gate is a payload-reduction candidate, not a retirement candidate.
PRE_EMPTED: dict = {}


def load_rows(days: int):
    if not LOG.exists():
        return []
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    out = []
    for line in _rotated_text(LOG).splitlines():
        if line.startswith("#"):
            continue
        m = ROW.match(line.strip())
        if not m:
            continue
        d = m.groupdict()
        try:
            ts = datetime.datetime.fromisoformat(d["ts"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        d["_ts"] = ts
        d["tok"] = int(d.get("tok") or 0)
        out.append(d)
    return out


def registry():
    try:
        return {h["name"]: h for h in json.loads(REG.read_text()).get("hooks", [])
                if not h.get("retired")}
    except Exception:
        return {}



def registered_now() -> set:
    """Hooks ACTUALLY registered in this Core's settings.json right now.

    THE LEDGER SCORED COMPONENTS THAT CANNOT RUN. bin/hook-registry.json lists what EXISTS;
    settings.json decides what RUNS, and it is per_core_keep so it differs per Core. On core-life
    `learned-classifier` carries 30 fires and is absent from settings.json — those fires are
    historical, from before the SI-spine cutover — yet it still received a confident EXPENSIVE
    verdict, which is a RETIRABLE verdict feeding the retirement actuator.

    core-business found the same component at streak=2 there and registered=1: on its Core it is
    genuinely live. So the identical word, from the identical code, meant "expensive" on one Core
    and "expensive once, before we turned it off" on another — and drove the same actuator.

    That is the on_missing defect in verdict form: a conclusion computed over a window the
    instrument could not observe. A component that was not registered during the window has no
    measurable cost or benefit, and the honest verdict is UNREGISTERED, not EXPENSIVE.
    """
    try:
        import re as _re
        s = (ROOT / ".claude" / "settings.json").read_text()
        return set(_re.findall(r"hooks/([a-z0-9_-]+)\.(?:py|sh)", s))
    except Exception:
        return set()          # unknown -> do NOT claim anything is unregistered


# THE SORT RANK IS PART OF THE VERDICT SET, so it lives beside it and is asserted against it.
# LOW-YIELD and UNREGISTERED were both emitted by build() and both absent from this map, so they
# fell to the default and printed BELOW EARNING — the two verdicts that most need a reader's
# attention, rendered as the least urgent rows on the page. Nothing was wrong with the analysis;
# the presentation inverted it. test_steering_ledger.py now fails if a new verdict is added
# without a rank.
DISPLAY_ORDER = {
    "UNREGISTERED": 0,   # cannot be concluded about at all — read this before any cost verdict
    "EXPENSIVE": 1,
    "LOW-YIELD": 2,      # runs constantly, acts almost never — the other retirement candidate
    "PRE-EMPTED": 3,
    "COST-BLIND": 4,
    "NO-DATA": 5,
    "UNMEASURED": 6,
    "RARE-VALUABLE": 7,
    "EARNING": 8,
}


def build(days: int):
    rows = load_rows(days)
    reg = registry()
    live = registered_now()
    sessions = {r["session"] for r in rows if r["session"]}
    n_sessions = max(1, len(sessions))

    fires = collections.Counter()
    invokes = collections.Counter()
    cost = collections.Counter()
    per_session = collections.defaultdict(set)
    halves = collections.defaultdict(lambda: [0, 0])   # [older half, newer half]
    if rows:
        mid = min(r["_ts"] for r in rows) + (max(r["_ts"] for r in rows)
                                             - min(r["_ts"] for r in rows)) / 2
    for r in rows:
        h = r["hook"]
        if r["verdict"] == "invoke":
            invokes[h] += 1
            continue
        fires[h] += 1
        cost[h] += r["tok"]
        if r["session"]:
            per_session[h].add(r["session"])
        halves[h][1 if r["_ts"] >= mid else 0] += 1

    out = []
    for name, entry in sorted(reg.items()):
        intent = entry.get("intent") or {}
        effect = intent.get("effect") or ""
        f, c = fires[name], cost[name]
        seen_sessions = len(per_session[name]) or 0
        older, newer = halves[name]
        # Declining means the guarded behaviour is occurring less. Needs both halves populated
        # to mean anything; with one data point it is noise wearing a trend's clothes.
        declining = None
        if older + newer >= 6:
            declining = newer < older * 0.6
        tok_per_session = round(c / n_sessions, 1)

        # COST-BLIND comes before every cost-based verdict, and that ordering is the point.
        # tokens_injected only started being recorded on 2026-07-30 (hooklog.emit), so every
        # row before that carries 0. Scoring a component "EARNING at 0.0 tok/session" on that
        # basis would be a verdict standing in for missing data — which is precisely the error
        # this session found in contract-fitness (verdicts measuring a detector swap) and in
        # steering_liveness (zero fires read as zero work). The ledger has to be the one place
        # that does not do it.
        # UNREGISTERED OUTRANKS EVERY COST VERDICT, for the same reason COST-BLIND outranks them:
        # it is the case where the data cannot support any conclusion. Guarded on `live` being
        # non-empty so an unreadable settings.json cannot mass-relabel the estate.
        if live and name not in live:
            verdict, why = "UNREGISTERED", (
                f"not in settings.json — {f} fire(s) in window are historical, from before this "
                f"component was unregistered. No cost or benefit is measurable, so no retirable "
                f"verdict is admissible.")
        elif name in PRE_EMPTED and f:
            verdict, why = "PRE-EMPTED", PRE_EMPTED[name]
        elif f > 0 and c == 0 and effect in ("inject", "block"):
            verdict, why = "COST-BLIND", (f"fires {f}x, cost not yet recorded — telemetry "
                                          f"predates hooklog.emit (2026-07-30)")
        elif effect in ("side-effect", "log-only") and f == 0:
            verdict, why = "UNMEASURED", "effect is not verdict-shaped; the fire counter cannot see it"
        elif f == 0 and invokes[name] == 0:
            verdict, why = "NO-DATA", "registered, no telemetry in window"
        elif f == 0:
            verdict, why = "RARE-VALUABLE", f"ran {invokes[name]}x, never had to act"
        elif tok_per_session > 500 and declining is False:
            verdict, why = "EXPENSIVE", f"{tok_per_session} tok/session and behaviour not declining"
        elif declining:
            verdict, why = "EARNING", f"fires {f}x, behaviour declining ({older}->{newer})"
        elif (invokes[name] >= 20 and f > 0 and (f / (f + invokes[name])) < 0.10):
            # LOW-YIELD (2026-07-30). The gap Nick's Stop-layer question exposed: both retirement
            # verdicts were COST-based, so a component that fires constantly, catches nothing and
            # costs little was immortal. Yield is the missing term.
            #
            # It could not be computed until today because only 5 of 9 Stop gates called
            # hooklog.invoked() — decision-attribution, financial-figure and cross-core-completion
            # logged fires but never invocations, so their yield read 100% BY CONSTRUCTION. All
            # eleven instrumented hooks now record invocation, which is what makes this honest.
            #
            # The 20-invocation floor is what separates this from RARE-VALUABLE:
            # cross-core-completion-gate fires 4 times out of 4 and caught a real overclaim;
            # learned-validator fired once in 26 runs against a table frozen since June. Same small
            # absolute number, opposite meaning, and only the denominator tells them apart.
            verdict, why = "LOW-YIELD", (f"{f} fires in {f + invokes[name]} runs "
                                         f"({f / (f + invokes[name]):.0%}) — runs often, acts almost never")
        elif f <= 3:
            verdict, why = "RARE-VALUABLE", f"{f} fires — kept on severity, see intent.guards"
        else:
            verdict, why = "EARNING", f"fires {f}x at {tok_per_session} tok/session"

        out.append({"hook": name, "event": entry.get("event"), "effect": effect,
                    "fires": f, "invocations": invokes[name], "sessions": seen_sessions,
                    "tokens": c, "tok_per_session": tok_per_session,
                    "trend": (None if declining is None else ("declining" if declining else "flat/rising")),
                    "verdict": verdict, "why": why,
                    "guards": (intent.get("guards") or "")[:120]})
    return {"days": days, "sessions": n_sessions, "rows": len(rows), "components": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    led = build(a.days)
    if a.json:
        print(json.dumps(led, indent=2))
        return 0

    print(f"steering ledger — last {led['days']}d · {led['rows']} telemetry rows · "
          f"{led['sessions']} sessions\n")
    comps = sorted(led["components"],
                   key=lambda c: (DISPLAY_ORDER.get(c["verdict"], 99), -c["tokens"]))
    print(f"{'hook':30} {'fires':>6} {'tok':>7} {'tok/sess':>9}  verdict")
    print("-" * 96)
    for c in comps:
        print(f"{c['hook']:30} {c['fires']:6} {c['tokens']:7} {c['tok_per_session']:9}  "
              f"{c['verdict']:14} {c['why'][:40]}")

    total = sum(c["tokens"] for c in comps)
    print(f"\ntotal injected in window: {total:,} tok "
          f"({round(total / max(1, led['sessions'])):,} tok/session)")
    flagged = [c for c in comps if c["verdict"] in ("EXPENSIVE", "PRE-EMPTED")]
    if flagged:
        print(f"\nACTION ({len(flagged)}):")
        for c in flagged:
            print(f"  {c['hook']:28} {c['verdict']:12} {c['why']}")
    print("\nNOTE: cost is measured. Benefit is NOT — the counterfactual is unobservable, so "
          "this reports\n      recurrence trend and rarity instead of inventing a benefit number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
