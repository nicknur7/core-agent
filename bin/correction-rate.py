#!/usr/bin/env python3
"""Corrections per 100 prompts — the master plan's criterion #1, made reproducible.

WHY THIS EXISTS. `tasks/specs/MASTER-PLAN-core-fitness-2026-07-30.md` §1 names three
top-line metrics and puts this one first: *corrections per 100 prompts must fall*. It
recorded "June 22.3 -> July 30.2" as measured. No script produced those numbers — they
were computed by hand in-session and typed into the plan, so nothing could re-run them,
and the criterion the whole plan is judged by had no instrument. That is the same defect
the plan spends its length removing, sitting in its own first section.

WHAT IT MEASURES.

    numerator    pattern_observations rows from the LIVE detector (learned-miner-v1),
                 org-scoped. This is the system's own correction detector; per the
                 2026-07-28 lesson, the detector's output IS the measurement and an
                 ad-hoc reimplementation silently answers a different question.
    denominator  substantive external prompts from this Core's own transcripts, using
                 the same filter as bin/grade-gate.py:corpus() so the two agree.

WHY THERE IS A COVERAGE COLUMN. Claude Code rotates JSONL transcripts, so the
denominator ages out while the numerator (in Postgres) does not. A period whose
transcripts are half gone yields a correction rate that looks alarming and means
nothing. Rather than print a clean number over partial data, every bucket is checked:
if the DB holds corrections on days with no surviving transcript, that bucket is marked
PARTIAL and its rate is suppressed. A rate you cannot trust is worse than no rate,
because it will be quoted.

This is why the plan's June baseline cannot be reproduced, and why this script says so
instead of inventing a comparable figure.

READ THE TREND, NOT THE LEVEL — AND NEVER RANK CORES BY IT.

This paragraph used to claim the >80-char filter was "a constant multiplier applied identically
to every bucket, so week-over-week movement is comparable." That was false in both directions and
four Cores measured it on 2026-08-05, each on its own corpus:

  - Week to week: the share of corrections with no denominator behind them ran 12.5-31.0% on org
    1, 25-70% on org 3 (a 2.8x swing), 36.6-57.3% on org 5 — and on org 5 the two highest-share
    weeks were the two highest-rate weeks, the direction that manufactures a decline.
  - Core to core: org 3 discards 57-71% of its prompts where org 1 discards 12.5-31%, so org 3's
    per-100 is inflated roughly 2x against org 1's. A LEVEL comparison across Cores is therefore
    meaningless — it partly measures whose prompts are wordier. Criterion #1 is a per-Core trend
    and survives; a fleet ranking does not. Do not build one.

Since 2026-08-05 both sides share one predicate (`qualifies()`), which removes the mismatch but
NOT the level bias: short prompts are still excluded from both, so the rate remains higher than a
true per-prompt rate. It is an index, not a percentage.

Two further biases, both landing on the newest bucket, both now handled: an unfinished week reads
as `full` (excluded from trend and streak since 2026-08-05), and corrections are mined at close
while prompts land immediately, so the newest bucket is always under-mined.

WHY WEEKS. Criterion #1 in the plan's §5 is stated as "falling for 3 consecutive
WEEKS", so `--by week` is the grain that actually answers it; months are the grain the
plan's own §1 baseline was quoted in. Both are offered because they answer different
questions, and neither is a default-and-forget.

    python3 bin/correction-rate.py                # by month
    python3 bin/correction-rate.py --by week      # the §5 criterion's own grain
    python3 bin/correction-rate.py --since 2026-06
    python3 bin/correction-rate.py --json
"""
import argparse
import collections
import json
import math
import os
import sys
from datetime import date as _date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scheduling" / "brain-pg"))
from _env import load_secrets, connect_corebrain, get_org_id, core_root  # noqa: E402

load_secrets()

LIVE_DETECTOR = "learned-miner-v1"

# Same shape as bin/grade-gate.py:corpus(kind="prompts"). Kept deliberately identical:
# two different definitions of "a prompt" across two tools is how a metric quietly stops
# being comparable to the one beside it.
MIN_PROMPT_CHARS = 80
SKIP_PREFIXES = ("<", "[SYSTEM", "Stop hook")


def repo() -> Path:
    """This Core's root — anchored to this file, NOT to CORE_INSTANCE.

    core-school, 2026-08-05: fixing only the numerator's identity made things WORSE, not better.
    `CORE_INSTANCE` leaks across a `cd` for exactly the same reason `CORE_ORG_ID` does — same
    export, same shells — so an identity-anchored org over an env-anchored transcript directory
    prints one Core's corrections against another Core's prompts with a header that now looks
    correct. The old failure at least announced itself by exceeding 100. "The loud failure WAS
    the safety property and you removed it from one side." Both sides now share one anchor.
    """
    root = core_root(Path(__file__))
    env = os.environ.get("CORE_INSTANCE")
    if env and Path(env).resolve() != root:
        print(f"[correction-rate] NOTE: CORE_INSTANCE={env} but this script lives in {root} — "
              f"using {root}. A session env var leaked across a `cd` is the usual cause.",
              file=sys.stderr)
    return root


def bucket_of(day: str, by: str) -> str:
    """'YYYY-MM-DD' -> its month or ISO week label. ISO week so a week is a real calendar
    week on both sides of a month boundary — the criterion says consecutive weeks, and a
    sliding 'last 7 days' window never sits still long enough to be consecutive."""
    if by == "month":
        return day[:7]
    iso = _date(*(int(x) for x in day.split("-"))).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def qualifies(text: str) -> bool:
    """The ONE definition of "an utterance this metric counts". Both sides call it.

    The whole 2026-08-05 fleet audit reduces to this function not existing. The denominator
    applied `>MIN_PROMPT_CHARS` and `SKIP_PREFIXES`; the numerator applied NOTHING, so a rate
    was formed from two different populations and called a percentage. core-ops measured 52 of
    its 97 rows (53.6%) as numerator with no denominator behind it, and showed that its entire
    3/3 "MET" existed only under the mismatch — self-consistent in EITHER direction it is 1/3.
    core-school measured its own numerator's sub-80 share swinging 25%-70% week to week.

    A shared predicate is the fix, not a matched pair of filters maintained in two places.
    """
    t = (text or "").strip()
    if len(t) <= MIN_PROMPT_CHARS:
        return False
    if t.lstrip().startswith(SKIP_PREFIXES):
        return False
    # Compaction continuation summaries are system-injected, arrive as user-role text, and scale
    # with how often a Core compacts rather than with anything Nick did (core-school: 6.9% of its
    # numerator, 2000 chars each). They are not corrections.
    if t.lstrip().startswith("This session is being continued from a previous conversation"):
        return False
    return True


def prompt_text_of(record: dict) -> str:
    """Text Nick actually TYPED, out of a JSONL user record. Empty string for anything else.

    Three populations arrive as `type=user, userType=external` and only one of them is a prompt:

    1. tool_result envelopes (list-form). Plumbing. On core-life 10,888 of 11,027 list-form
       messages are these; on core-business 84.5%. Correctly dropped — core-business, 2026-08-05:
       "a naive isinstance fix turns 5384 tool results on my Core into denominator rows and
       drives every rate toward zero."
    2. skill bodies and slash-command expansions. Machine-inserted template text — the
       `/close-core` body, the claude-brain skill, `/deep-plan`. Not typed by anyone. These carry
       `isMeta: true`, which is a structural marker rather than a text heuristic, so it does not
       rot the way a prefix list does.
    3. what Nick typed.

    The old `c if isinstance(c, str) else ""` got (1) right by accident and (2) WRONG in a way
    nobody was auditing: 233 of life's string-form messages also carry `isMeta: true` and have
    been inflating the denominator since the metric shipped. The list-form half of (2) is 74 more.
    Excluding both and admitting the 6 genuinely-typed list-form prompts is a net SHRINK of the
    denominator, which pushes rates up — the honest direction, since the instrument had been
    dividing by template text.
    """
    if record.get("isMeta"):
        return ""
    c = (record.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        return "".join(b.get("text", "") for b in c
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def prompt_days() -> collections.Counter:
    """{'YYYY-MM-DD': count} of substantive external prompts."""
    base = Path.home() / ".claude" / "projects"
    out = collections.Counter()
    for d in base.glob(f"*{repo().name}*"):
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                text = f.read_text(errors="ignore")
            except Exception:
                continue
            for line in text.splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") != "user" or r.get("userType") != "external":
                    continue
                ts = r.get("timestamp") or ""
                if len(ts) < 10:
                    continue
                if not qualifies(prompt_text_of(r)):
                    continue
                out[ts[:10]] += 1
    return out


def correction_days(conn, org_id: int) -> tuple:
    """({'YYYY-MM-DD': count}, excluded_count) from the live detector, SAME filter as prompts.

    Also de-duplicates: core-school found 11 of 175 rows (6.3%) were exact same-day repeats of
    one another, one correction stored three times. A duplicated numerator inflates the rate of
    whichever week happened to double-mine.
    """
    out = collections.Counter()
    excluded = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_date, correction_text FROM pattern_observations "
            "WHERE org_id = %s AND detector_version = %s",
            (org_id, LIVE_DETECTOR),
        )
        seen = set()
        for day, ctext in cur.fetchall():
            d = day.strftime("%Y-%m-%d")
            if not qualifies(ctext):
                excluded += 1
                continue
            key = (d, (ctext or "").strip())
            if key in seen:
                excluded += 1
                continue
            seen.add(key)
            out[d] += 1
    return out, excluded


# A rate over a handful of prompts is not a measurement. core-ops's 2026-W32 bucket held FOUR
# prompts, where one correction moves the rate 25 points — enough to manufacture or erase a
# "falling streak" out of noise. Buckets under this floor still PRINT their rate (hiding real data
# would be its own dishonesty) but may not support a Criterion #1 verdict, and the verdict is
# REFUSED rather than printed with a caveat: a number with a caveat gets quoted without one.
# 30 is the conventional floor where a proportion's normal approximation starts behaving; it is a
# convention, not a discovery, and the printed margin below is what actually carries the argument.
MIN_BUCKET_PROMPTS = 30


def _wilson_halfwidth(k: int, n: int) -> float:
    """Half-width of the 95% Wilson score interval for k/n, in per-100 units.

    Indicative only, and deliberately labelled as such wherever it prints. Wilson assumes k/n is a
    proportion in [0,1], but this metric's numerator and denominator do not come from the same
    filter — the denominator inherits grade-gate's >80-char rule and the numerator does not, so
    k > n is possible and observed. p is clamped for the arithmetic; when it clamps, the interval
    understates the true uncertainty rather than overstating it, which is the safe direction.
    """
    if n <= 0:
        return float("inf")
    z = 1.96
    p = min(1.0, max(0.0, k / n))
    denom = 1.0 + z * z / n
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return 100.0 * margin


def _current_bucket(by: str) -> str:
    """The bucket TODAY falls in — i.e. the one that has not finished happening yet.

    core-business, 2026-08-05: "should an unfinished bucket be in the series at all." It never
    should have been. The coverage guard only flags correction-days with no surviving transcript,
    and a week that is 43% elapsed has no missing days and nonzero prompts, so it reads `full`
    and then terminates both the trend direction and the streak counter — the two things the
    conclusion rests on. Compounding it, core-school: corrections are mined at close while
    prompts land in the JSONL immediately, so the newest bucket is ALSO systematically
    under-mined. Both errors push the newest bucket the same way, and the newest bucket is
    exactly the one a "falling streak" leans on hardest.
    """
    return bucket_of(_date.today().isoformat(), by)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", choices=["month", "week"], default="month")
    ap.add_argument("--since", help="earliest bucket label, e.g. 2026-06 or 2026-W27")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # ONE resolver, anchored to this script's own location — not two that can disagree.
    # (core-ops, 2026-08-05: "Two resolvers that can disagree is the next version of this bug.")
    # Both the org (numerator) and repo() (denominator) now come from the same on-disk anchor,
    # so the pair cannot be mismatched by a leaked session env var.
    org_id = get_org_id(Path(__file__))
    conn = connect_corebrain()
    try:
        corr_d, corr_excluded = correction_days(conn, org_id)
    finally:
        conn.close()
    prom_d = prompt_days()

    corr, prom = collections.Counter(), collections.Counter()
    corr_dates = collections.defaultdict(set)
    for day, n in corr_d.items():
        b = bucket_of(day, args.by)
        corr[b] += n
        corr_dates[b].add(day)
    for day, n in prom_d.items():
        prom[bucket_of(day, args.by)] += n

    buckets = sorted(set(corr) | set(prom))
    if args.since:
        buckets = [b for b in buckets if b >= args.since]

    rows = []
    for b in buckets:
        corrections, prompts = corr.get(b, 0), prom.get(b, 0)
        # A day the DB knows about but the transcripts do not is a rotated-away day: its
        # corrections sit in the numerator with no denominator behind them.
        missing = sorted(d for d in corr_dates.get(b, ()) if d not in prom_d)
        partial = bool(missing) or prompts == 0
        rows.append({
            "bucket": b,
            "corrections": corrections,
            "prompts": prompts,
            "per_100": None if partial else round(100.0 * corrections / prompts, 1),
            "coverage": "PARTIAL" if partial else "full",
            "days_missing_transcripts": missing,
            # "thin" = the rate is computable but the sample cannot support a verdict.
            "thin": (not partial) and prompts < MIN_BUCKET_PROMPTS,
            "margin_95": None if partial else round(_wilson_halfwidth(corrections, prompts), 1),
            "in_flight": b == _current_bucket(args.by),
        })

    if args.json:
        print(json.dumps({"org_id": org_id, "detector": LIVE_DETECTOR,
                          "by": args.by, "buckets": rows}, indent=2))
        return 0

    print(f"Corrections per 100 prompts — org {org_id}, detector {LIVE_DETECTOR}, by {args.by}")
    print(f"{'bucket':10} {'corrections':>12} {'prompts':>9} {'per 100':>9} {'±95%':>7}  coverage")
    for r in rows:
        rate = "—" if r["per_100"] is None else f"{r['per_100']:.1f}"
        marg = "—" if r["margin_95"] is None else f"{r['margin_95']:.1f}"
        note = ""
        if r["coverage"] == "PARTIAL":
            n = len(r["days_missing_transcripts"])
            note = f"  ({n} day(s) with corrections but no surviving transcript)" if n else "  (no prompts)"
        cov = r["coverage"] + ("·THIN" if r["thin"] else "") + ("·IN-FLIGHT" if r["in_flight"] else "")
        if r["thin"]:
            note = f"  (n={r['prompts']} < {MIN_BUCKET_PROMPTS} — rate shown, cannot support a verdict)"
        if r["in_flight"]:
            note = "  (this bucket has not finished — shown, excluded from trend and streak)"
        print(f"{r['bucket']:10} {r['corrections']:>12} {r['prompts']:>9} {rate:>9} {marg:>7}  {cov}{note}")

    if corr_excluded:
        print(f"\n[numerator] {corr_excluded} detector row(s) excluded by the shared filter "
              f"(sub-{MIN_PROMPT_CHARS}-char, skip-prefix, compaction summary, or same-day duplicate) "
              f"— the same filter the denominator applies.")

    # The in-flight bucket is displayed but never gets a vote: it is systematically low on the
    # numerator (mined at close) and short on the denominator (partial week), and it is always
    # the bucket a falling streak would terminate on.
    full = [r for r in rows if r["coverage"] == "full" and not r["in_flight"]]
    print()
    if len(full) < 2:
        print("NOT ENOUGH FULL-COVERAGE BUCKETS TO STATE A TREND.")
        print("This is a real answer, not a failure: transcript rotation has removed the")
        print("denominator for the earlier periods, so the plan's June->July comparison")
        print("cannot be reproduced from surviving data by this or any other method.")
        return 0

    # THIN buckets are barred from the STREAK but were still terminating the TREND line, which is
    # the line people actually quote. core-school caught it and core-ops confirmed it is worse on
    # org 5: its printed headline "25.9 -> 33.3 (RISING)" had BOTH endpoints on buckets the script
    # itself labels "cannot support a verdict" (n=27 and n=9). A refusal that only guards the
    # verdict and leaves the quotable sentence unguarded is not a refusal.
    solid = [r for r in full if not r["thin"]]
    if len(solid) < 2:
        thin_names = ", ".join(f"{r['bucket']}(n={r['prompts']})" for r in full if r["thin"])
        print(f"NO TREND STATED — fewer than 2 buckets clear the {MIN_BUCKET_PROMPTS}-prompt floor.")
        print(f"  Thin buckets shown above but not trended: {thin_names or '(none)'}")
    else:
        first, last = solid[0], solid[-1]
        direction = "FALLING" if last["per_100"] < first["per_100"] else "RISING"
        print(f"Trend over full-coverage buckets {first['bucket']} -> {last['bucket']}: "
              f"{first['per_100']:.1f} -> {last['per_100']:.1f}  ({direction})")
        dropped = [r for r in full if r["thin"]]
        if dropped:
            print(f"  (excluded from the trend, n < {MIN_BUCKET_PROMPTS}: "
                  + ", ".join(f"{r['bucket']}(n={r['prompts']})" for r in dropped) + ")")

    # Criterion #1 verbatim: falling for 3 consecutive weeks.
    if args.by == "week":
        # THE STREAK RUNS OVER THE SAME BUCKETS THE TREND DOES. It did not, for one commit, and
        # core-school and core-ops both caught it within a minute of each other by reading the
        # code rather than the output: :372 built the trend from `solid` while :391 still zipped
        # `full`, so a thin bucket was excluded from the quotable sentence and still allowed to
        # start, extend or BREAK the streak. ops was a live instance — its n=9 week was barred
        # from the trend and simultaneously counted in the streak.
        #
        # That is the identical half-application as the repo() fix earlier tonight: one side of a
        # pair repaired, the other left reading the thing the repair was about. Twice in one
        # session is a pattern rather than an accident — when a guard is added, every consumer of
        # the guarded value gets it in the same commit, or the guard is decorative.
        streak = 0
        streak_buckets = []
        for a, b in zip(solid, solid[1:]):
            if b["per_100"] < a["per_100"]:
                streak += 1
                streak_buckets = (streak_buckets or [a])+[b]
            else:
                streak, streak_buckets = 0, []

        # Belt and braces: `solid` cannot contain a thin bucket, so this branch should now be
        # unreachable. It stays because it is the thing that catches the NEXT half-application —
        # if some future edit reintroduces thin buckets upstream, the verdict refuses rather than
        # silently reporting a streak built on n=4.
        thin_in_streak = [r for r in streak_buckets if r["thin"]]
        if streak >= 3 and thin_in_streak:
            worst = max(thin_in_streak, key=lambda r: r["margin_95"])
            named = ", ".join("{}(n={})".format(r["bucket"], r["prompts"]) for r in thin_in_streak)
            print(f"Criterion #1 (falling 3 consecutive weeks): falling streak = {streak}/3, but "
                  f"VERDICT REFUSED — {len(thin_in_streak)} bucket(s) in the streak are under "
                  f"{MIN_BUCKET_PROMPTS} prompts ({named}).")
            print(f"  {worst['bucket']} alone carries a ±{worst['margin_95']:.1f} margin at 95%, "
                  f"against a total decline of "
                  f"{streak_buckets[0]['per_100'] - streak_buckets[-1]['per_100']:.1f}. "
                  f"Collect more weeks before claiming this.")
        else:
            print(f"Criterion #1 (falling 3 consecutive weeks): current falling streak = {streak}/3"
                  f" — {'MET' if streak >= 3 else 'NOT MET'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
