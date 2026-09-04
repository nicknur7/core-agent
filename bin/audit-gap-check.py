#!/usr/bin/env python3
"""Every baseline apply must have an audit entry. Report the ones that do not.

WHY THIS EXISTS (2026-08-12). core-finance and core-business established, from their own records,
that a `!` command never becomes a Claude tool call — so PreToolUse never fires, `pretooluse-guard`
never runs, and `sentinel-approve.sh` never writes its `APPROVE | Bash | …` line to access-log.md.

That resolved a live question the right way: Nick's hand-run path EXISTS and is not blocked, so the
trust-root finding stays "a peer can ALSO approve" rather than "only peers can, and the human
cannot." Good news.

It arrives with a price, which finance stated in the same breath: THE SANCTIONED ROUTE FOR THE
HIGHEST-RISK CLASS OF CHANGE IS THE ONE THAT LEAVES NO AUDIT RECORD. Nick's `!` push is the one
confirmation Core cannot forge, and nothing writes it down. finance's entry for the 2026-08-06 apply
was reconstructed after the fact and is marked as such.

Nothing can log it AT THE MOMENT it happens — there is no hook to hang on, which is precisely why
the path is unforgeable. But the apply leaves a trace either way: `.last-baseline-sync` gets a line.
So the gap is detectable AFTERWARD by reconciling two records this seat already keeps:

    .last-baseline-sync   every apply, with a timezone-qualified timestamp
    memory/access-log.md  every gated action, in UTC

An apply with no audit entry near it was run outside the tool path. That is not an accusation — on a
puller it is the EXPECTED shape, because Nick applies by hand there. What it gives is a record that
the unrecorded thing happened, and when.

NOT FLEET-UNIFORM, and assuming it was would have been wrong. Measured on life 2026-08-12: all three
2026-08-06 applies have matching entries (18:59/18:59, 21:12/21:11, 00:48/00:48 UTC), because life
is the WRITER and Core runs its pushes through the tool path. The gap appears on the PULLER seats,
where Nick acts directly. Each Core must therefore run this against its OWN records — which is the
point: a seat cannot audit another seat's hand-run actions.

SCOPE, stated because an over-broad instrument that reads as a finding is the defect this file's
neighbours exist to catch: this reports applies WITHOUT a nearby audit entry. It does not prove who
ran them, and it cannot see a `!` command that changed nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scheduling" / "brain-pg"))

# The approve is written BEFORE the action runs, so an entry may precede its apply. Observed on life:
# 21:11 approve -> 21:12 apply. Widened to 30 min because a sync clones the baseline over the network
# first, and a slow clone stretches that gap. The window is deliberately asymmetric: an audit entry
# AFTER an apply is not evidence for it.
LEAD = dt.timedelta(minutes=30)
LAG = dt.timedelta(minutes=5)

_APPLY = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})([+-]\d{2}:?\d{2})")
_AUDIT = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+UTC\s*\|")
_SYNC = re.compile(r"sync-(to|from)-baseline", re.I)


def _seat_root() -> Path:
    """This Core's root. CLAUDE_PROJECT_DIR when set, else the repo containing this file."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parent)


def _parse_applies(path: Path) -> list[tuple[dt.datetime, str]]:
    out: list[tuple[dt.datetime, str]] = []
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        m = _APPLY.match(line.strip())
        if not m:
            continue
        day, clock, off = m.group(1), m.group(2), m.group(3).replace(":", "")
        sign = 1 if off[0] == "+" else -1
        local = dt.datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S")
        utc = local - sign * dt.timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
        out.append((utc, line.strip()))
    return out


def _parse_audit(path: Path) -> list[dt.datetime]:
    """UTC timestamps of gated actions that name a baseline sync.

    Only sync lines count. Matching an apply against ANY audit entry would let an unrelated
    approved action three minutes earlier vouch for a hand-run trust-root apply — a proxy trusted
    as the thing, which is the shape this suite exists to refuse.
    """
    out: list[dt.datetime] = []
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        m = _AUDIT.match(line.strip())
        if not m or not _SYNC.search(line):
            continue
        out.append(dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="", help="only applies on/after this date (YYYY-MM-DD)")
    ap.add_argument("--root", default="", help="seat root to audit (default: this Core)")
    ap.add_argument("--quiet", action="store_true", help="print only the summary line")
    a = ap.parse_args()

    root = Path(a.root) if a.root else _seat_root()
    applies = _parse_applies(root / ".claude" / "state" / ".last-baseline-sync")
    audits = _parse_audit(root / "memory" / "access-log.md")

    if a.since:
        cut = dt.datetime.strptime(a.since, "%Y-%m-%d")
        applies = [(t, s) for t, s in applies if t >= cut]

    if not applies:
        print(f"audit-gap-check: NO APPLIES FOUND under {root}")
        print("  Not a clean result — either this seat has never applied a baseline, or")
        print("  .last-baseline-sync is missing. A zero here must not read as 'no gaps'.")
        return 2

    # TWO SOURCES, NOT ONE — core-business, from its own seat, and it corrected a real defect here.
    # This tool originally closed with "The apply is real and authorised", which is true of a
    # hand-run and UNESTABLISHED for the other source: settings.json registers sync-from-baseline.sh
    # --quiet as a SessionStart hook, and a HOOK-RUN SCRIPT never passes PreToolUse either. Same
    # invisibility, different cause. business named two of its eleven — one at 39s after
    # .session-start, one that "synced itself via the SessionStart RE-FIRE on a context compaction:
    # NOBODY CHOSE THE MOMENT".
    #
    # So a reassuring sentence was absorbing the case that actually needs attention. An automatic
    # pull has no authoriser at all, and business has carried that as an open Nick decision since
    # 2026-08-05.
    #
    # Discriminated on OBSERVABLE data only, keeping the rule that this tool never names a cause it
    # cannot see: `via=` is written into every line (a pull is a pull, recorded, not inferred), and
    # session-start proximity is a stamp on disk. Everything else stays "unattributed", which is the
    # honest label for "could be Nick's `!`, could be something else."
    # THE SECOND SIGNAL, AND IT CANNOT BE ABSENT — core-business's option 3, and the same "both, or
    # neither" shape shared-write-guard.py already uses. `via=` travels only on the push path, so it
    # is missing exactly where it is most needed. A seat's DECLARED ROLE is always present, and a
    # puller cannot have pushed by construction.
    role = ""
    ident = root / ".claude" / "identity.json"
    if ident.is_file():
        try:
            import json as _json
            role = str((_json.loads(ident.read_text()).get("hook_profile") or {}).get("role", ""))
        except Exception:
            role = ""

    starts = []
    for f in (".session-start", ".last-session-start"):
        p = root / ".claude" / "state" / f
        if p.is_file():
            for tok in re.findall(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", p.read_text(errors="replace")):
                try:
                    starts.append(dt.datetime.strptime(tok.replace("T", " "), "%Y-%m-%d %H:%M"))
                except ValueError:
                    pass

    gaps = []
    for when, raw in applies:
        if any(-LAG <= (when - e) <= LEAD for e in audits):
            continue
        near_start = any(abs((when - s).total_seconds()) <= 90 for s in starts)
        if "sync-from-baseline" in raw:
            direction = "PULL"
        elif "sync-to-baseline" in raw:
            direction = "PUSH"
        elif role == "puller":
            # A PULLER CANNOT HAVE PUSHED. `via=` is written only by sync-to-baseline.sh, so on a
            # pull-only seat NO line carries it and every one hit the old default — which was PUSH.
            # That made the tool assert that business pushed to the baseline eleven times unrecorded:
            # not an unlogged event, an event that DID NOT HAPPEN, and one that reads as an incident.
            # Wrong reassurance goes unchecked; a wrong ALARM burns the checker and makes the next
            # real one read more slowly. Both directions cost.
            direction = "PULL"
        else:
            # Writer seat, no via=: the line predates the field (3 of life's 140). Not evidence of a
            # direction. Saying so beats guessing — core-business's option 1, kept for exactly the
            # case where its option 3 cannot decide.
            direction = "DIRECTION UNKNOWN (line predates via=)"

        if direction == "PULL" and near_start:
            why = "AUTO-PULL — within 90s of a session start; the SessionStart hook ran it, nobody chose it"
        else:
            why = f"{direction}, unattributed" if "UNKNOWN" not in direction else direction
        gaps.append((when, raw, why))

    if not a.quiet:
        print(f"audit-gap-check — seat {root.name}")
        print(f"  applies examined : {len(applies)}")
        print(f"  sync audit lines : {len(audits)}")
        print(f"  window           : entry may precede apply by {LEAD}, follow by {LAG}")
        print()
        for when, raw, why in gaps:
            print(f"  NO AUDIT ENTRY   {when:%Y-%m-%d %H:%M} UTC   [{why}]")
            print(f"                   {raw[:110]}")

    if gaps:
        auto = sum(1 for _, _, w in gaps if w.startswith("AUTO-PULL"))
        print(f"\n  {len(gaps)} of {len(applies)} applies have NO audit entry"
              + (f", {auto} of them AUTO-PULLS." if auto else "."))
        print()
        print("  UNATTRIBUTED means unattributed. It is consistent with the operator running the")
        print("  command directly via `!` — which never becomes a tool call, so no hook fires — and")
        print("  that")
        print("  apply would be real and authorised, with only the RECORD missing. It is equally")
        print("  consistent with something else. This tool cannot tell, and says so rather than")
        print("  offering the reassuring reading.")
        if auto:
            print()
            print(f"  THE {auto} AUTO-PULL(S) ARE THE ONES THAT MATTER. A SessionStart hook ran")
            print("  sync-from-baseline.sh --quiet, and a hook-run script bypasses PreToolUse for the")
            print("  same reason a `!` command does. But nobody CHOSE it — there is no authoriser at")
            print("  all, not merely an unrecorded one. core-business has carried that as an open")
            print("  decision for the operator since 2026-08-05.")
        return 1

    print(f"\n  all {len(applies)} applies have an audit entry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
