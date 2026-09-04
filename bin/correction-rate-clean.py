#!/usr/bin/env python3
"""The defensible correction-rate number: one detector generation, exposure denominator.

WHY THIS EXISTS. An external document's headline — "82% correction reduction" — does not
survive a generation-consistent recount. Established 2026-08-09 in
tasks/research/correction-rate-reconciliation-2026-08-09.md:

  - detector `v1` produced its last row 2026-06-04; the contracts were created 2026-06-05, so the
    pre-window was counted by TWO detectors and the post-window by ONE
  - both detectors were alive 05-15..06-04 with 19 of 22 days double-counted
  - unfiltered the change is -71.2%; filtered to the live detector it is +31.2%. The sign flips.
  - and the per_calendar_week denominator fails core-business's wall-clock invariance fixture by
    30x on corpora with identical behaviour

Neither number was sound, so the honest verdict was UNDECIDABLE. THIS SCRIPT MAKES IT DECIDABLE,
by removing both confounds instead of arguing about them:

  1. ONE GENERATION. Window starts 2026-06-05, after `v1` stopped. No detector swap inside it.
  2. EXPOSURE, NOT CALENDAR TIME. The denominator is assistant turns actually taken, counted from
     the session transcripts — not days elapsed. Ten corrections in 100 turns is the same rate
     whether those turns happened in one day or thirty, which is exactly what the calendar-week
     denominator got wrong.

WHAT IT DELIBERATELY DOES NOT DO: pick a flattering split. The split is the MIDPOINT OF EXPOSURE
(equal turns each side), not a date chosen after seeing the answer, and `--split` prints the
sensitivity across several alternatives so a reader can see whether the result depends on it.

    python3 bin/correction-rate-clean.py
    python3 bin/correction-rate-clean.py --json
"""
import argparse
import collections
import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scheduling" / "brain-pg"))
sys.path.insert(0, str(HERE))          # core_seat lives beside this file
import core_seat as _core_seat  # noqa: E402  — the canonical seat/slug resolver
from _env import load_secrets, connect_corebrain, core_root  # noqa: E402

# The live detector and the first date on which it is the ONLY one producing rows.
LIVE_DETECTOR = "learned-miner-v1"
CLEAN_START = date(2026, 6, 5)


def transcript_dir(root: Path) -> Path:
    """Delegates to core_seat — the canonical slug, not a local re-derivation.

    FIFTH ESCAPE OF ONE CONSOLIDATION (2026-08-12, core-finance DOSE 37; business found the fourth).
    This computed `str(root).replace("/", "-").replace(" ", "-")`, which handles exactly the two
    characters its author thought of. Claude Code replaces EVERY non-alphanumeric:

        canonical (core_seat)   -Users-x-AI-Projects-core-v1-5
        this file, before       -Users-x-AI-Projects-core-v1.5

    They agree on life's path because it contains no other punctuation, which is why nothing here is
    broken today and why the divergence stayed invisible.

    THE BLAST RADIUS IS LOUD, NOT SILENT — worth stating because finance predicted the opposite and
    then disproved themselves by RUNNING the tool rather than asserting the theory. A bad slug yields
    a nonexistent directory, and this file exits non-zero naming the path, with a different sentence
    from the thin-corpus verdict. So "I looked in the wrong place" was never going to be mistaken for
    "there was not enough data" — which was the scenario that would have made a T004 UNDERPOWERED
    answer unfalsifiable.

    Fixed because it has escaped twice, not because anything is currently wrong.
    """
    return _core_seat.transcripts_dir(root)


def exposure_by_day(tdir: Path):
    """Assistant turns per calendar day, from the session transcripts.

    A TURN is a group of assistant messages between user prompts, not one assistant message.
    core-business measured why this matters: of 13,543 assistant messages on its Core, 3,634 carry
    text and 5,966 carry tool_use and THEY ARE ALMOST NEVER THE SAME MESSAGE. Counting messages
    would inflate exposure by roughly the tool-call rate, which is itself not constant over time —
    a moving inflation factor inside the denominator is precisely the confound this script exists
    to remove.
    """
    per_day = collections.Counter()
    files = sorted(tdir.glob("*.jsonl")) if tdir.is_dir() else []
    for f in files:
        in_turn = False
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    role = rec.get("type") or rec.get("role")
                    ts = rec.get("timestamp") or ""
                    if role == "user":
                        # A TOOL RESULT IS DELIVERED AS type="user" AND IS NOT A PROMPT.
                        # Codex found this 2026-08-09 and it is severe: in the newest transcript
                        # 788 of 879 "user" records are tool results, so treating every one as a
                        # turn boundary counted TOOL-CALL DEPTH, not work — 9.7x inflation here.
                        # Worse than a constant factor: depth is not stable over time, so a period
                        # where Core used more tools per prompt gets a larger denominator and a
                        # lower correction RATE. That manufactures exactly the downward trend this
                        # script was written to measure honestly.
                        content = (rec.get("message") or {}).get("content")
                        is_tool_result = isinstance(content, list) and any(
                            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                        if not is_tool_result:
                            in_turn = False
                        continue
                    if role != "assistant":
                        continue
                    if in_turn:
                        continue          # same turn, already counted
                    in_turn = True
                    try:
                        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
                    except Exception:
                        continue
                    per_day[d] += 1
        except OSError:
            continue
    return per_day, len(files)


def corrections_by_day(cur, org):
    cur.execute(
        """SELECT session_date, COUNT(*) FROM pattern_observations
           WHERE org_id = %s AND detector_version = %s AND session_date >= %s
           GROUP BY 1 ORDER BY 1""",
        (org, LIVE_DETECTOR, CLEAN_START))
    out = collections.Counter()
    for d, n in cur.fetchall():
        out[d if isinstance(d, date) else datetime.strptime(str(d), "%Y-%m-%d").date()] = int(n)
    return out


def rate(corr, exp):
    """Corrections per 100 assistant turns. UNDECIDABLE (None) when exposure is too thin.

    A rate over a handful of turns is noise wearing a decimal point, and reporting it would be the
    same fail-toward-PASS this whole exercise exists to correct.
    """
    return None if exp < 100 else (corr / exp * 100.0)


def window(days, corr, exp, lo, hi):
    c = sum(corr.get(d, 0) for d in days if lo <= d < hi)
    e = sum(exp.get(d, 0) for d in days if lo <= d < hi)
    return c, e, rate(c, e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    # UNRUNNABLE IS UNVERIFIABLE. core-business ran this from a review checkout and got
    # "no transcripts found ... cannot denominate" — so no fresh clone, no peer Core and no
    # reviewer could reproduce the figure. Only whoever sits at the live instance could. For a
    # number that was headed to a third party, "check my work" was not actually available.
    ap.add_argument("--transcripts-dir",
                    help="explicit transcript directory, so a reviewer on another checkout can "
                         "reproduce the figure instead of taking it on trust")
    ap.add_argument("--as-of", dest="as_of",
                    help="bound the post-window at this date (YYYY-MM-DD) so an earlier figure "
                         "is reproducible; default is the last day with data")
    a = ap.parse_args()
    args_as_of = None
    if a.as_of:
        args_as_of = datetime.strptime(a.as_of, "%Y-%m-%d").date()

    load_secrets()
    root = core_root(Path(__file__))
    org = json.loads((root / ".claude" / "identity.json").read_text()).get("org_id")
    cur = connect_corebrain().cursor()

    corr = corrections_by_day(cur, org)
    tdir = Path(a.transcripts_dir).expanduser() if a.transcripts_dir else transcript_dir(root)
    exp, nfiles = exposure_by_day(tdir)
    if not exp:
        sys.exit("no transcripts found under %s — cannot denominate by exposure.\n"
                 "Pass --transcripts-dir to point at a real transcript set." % tdir)

    days = sorted(d for d in set(list(corr) + list(exp)) if d >= CLEAN_START)
    if not days:
        sys.exit("no data inside the clean window")
    lo, hi = days[0], days[-1]

    # SPLIT AT THE MIDPOINT OF EXPOSURE, not of the calendar — equal turns each side, so neither
    # window is a thin baseline. Chosen before looking at the answer.
    total_exp = sum(exp.get(d, 0) for d in days)
    run, split = 0, days[len(days) // 2]
    for d in days:
        run += exp.get(d, 0)
        if run >= total_exp / 2:
            split = d
            break

    # THE POST-WINDOW IS BOUNDED AND STAMPED. It ran split -> date(9999,1,1), so it grew every
    # day the tool was run: exposure accrues while corrections may not, and THE NUMBER IMPROVES BY
    # WAITING. core-business caught it and named it correctly — a wall-clock invariance failure, in
    # the metric I built to REPLACE one that failed wall-clock invariance.
    #
    # A figure quoted to anyone must be a claim about a STATED PERIOD, not a live readout that is
    # different tomorrow with nothing having changed. `as_of` is recorded in the output so any
    # citation carries its own window, and --as-of makes an earlier figure reproducible.
    as_of = args_as_of or hi
    pre_c, pre_e, pre_r = window(days, corr, exp, lo, split)
    post_c, post_e, post_r = window(days, corr, exp, split, as_of + timedelta(days=1))

    change = None
    if pre_r and post_r is not None and pre_r > 0:
        change = (post_r - pre_r) / pre_r * 100.0

    # SENSITIVITY: does the answer depend on where the split lands? If it flips across nearby
    # splits, the headline is an artifact of the split and must not be published as a result.
    sens = []
    for frac in (0.35, 0.45, 0.5, 0.55, 0.65):
        run, s = 0, days[0]
        for d in days:
            run += exp.get(d, 0)
            if run >= total_exp * frac:
                s = d
                break
        _, _, r1 = window(days, corr, exp, lo, s)
        _, _, r2 = window(days, corr, exp, s, as_of + timedelta(days=1))
        sens.append({"frac": frac, "split": str(s), "pre": r1, "post": r2,
                     "change_pct": ((r2 - r1) / r1 * 100.0) if (r1 and r2 is not None) else None})

    result = {
        "org_id": org, "detector": LIVE_DETECTOR,
        "window": [str(lo), str(as_of)], "split": str(split),
        "as_of": str(as_of),
        "citation": "%s%% over %s..%s, split %s, one detector generation, exposure denominator"
                    % ("n/a" if change is None else "%+.1f" % change, lo, as_of, split),
        "transcripts": nfiles, "total_turns": total_exp,
        "pre": {"corrections": pre_c, "turns": pre_e, "per_100_turns": pre_r},
        "post": {"corrections": post_c, "turns": post_e, "per_100_turns": post_r},
        "change_pct": change,
        "sensitivity": sens,
    }

    if a.json:
        print(json.dumps(result, indent=2))
        return 0

    print("\n  CORRECTION RATE — one detector generation, exposure denominator\n")
    print("  org %s · detector %s · %s .. %s · %d transcripts, %d assistant turns"
          % (org, LIVE_DETECTOR, lo, as_of, nfiles, total_exp))
    print("  AS OF %s — quoting this figure without that date makes it a moving number." % as_of)
    print("  split at the EXPOSURE midpoint: %s\n" % split)
    print("  %-8s %12s %10s %16s" % ("window", "corrections", "turns", "per 100 turns"))
    for lbl, c, e, r in (("pre", pre_c, pre_e, pre_r), ("post", post_c, post_e, post_r)):
        print("  %-8s %12d %10d %16s" % (lbl, c, e, "UNDECIDABLE" if r is None else "%.2f" % r))
    print("\n  change: %s" % ("UNDECIDABLE — exposure too thin on one side" if change is None
                              else "%+.1f%%" % change))
    print("\n  SENSITIVITY TO THE SPLIT (if the sign moves here, there is no result):")
    for s in sens:
        ch = "n/a" if s["change_pct"] is None else "%+.1f%%" % s["change_pct"]
        print("    %.0f%% of exposure  split %s   pre %s  post %s   %s"
              % (s["frac"] * 100, s["split"],
                 "n/a" if s["pre"] is None else "%.2f" % s["pre"],
                 "n/a" if s["post"] is None else "%.2f" % s["post"], ch))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
