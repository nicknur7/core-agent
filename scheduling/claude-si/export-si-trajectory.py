#!/usr/bin/env python3
"""export-si-trajectory — the visible self-improvement surface (Nick, 2026-07-02).

Writes .claude/state/si-trajectory.json: per-pattern trajectory classified into
Nick's mental model — GRADUATED (extinct with a clean streak) · LIVE (still
occurring; rate now vs peak + 8-week sparkline) · NEW (first seen <30d) — plus
the latest contract-fitness verdicts and any resynth blocking-proposals awaiting
approval. Core OS renders this on /system.

Org-scoped + fork-portable: reads CORE_INSTANCE env, org_id from identity.json.
Wired: run-brain-update.sh heavy (nightly + /rebuild-graph) right after
measure-contract-fitness; also runnable standalone.
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
STATE = INSTANCE / ".claude" / "state"
OUT = STATE / "si-trajectory.json"
WINDOW_D = 14          # "live" window
NEW_D = 30             # first-seen younger than this = NEW
CLEAN_MIN_D = 14       # extinct at least this long = GRADUATED
SPARK_WEEKS = 8


def org_id() -> int:
    try:
        return int(json.loads((INSTANCE / ".claude" / "identity.json").read_text()).get("org_id", 1))
    except Exception:
        return 1


def q(sql: str) -> list[list[str]]:
    """psql corebrain, tab-separated rows."""
    r = subprocess.run(["psql", "corebrain", "-tAF", "\t", "-c", sql], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"psql failed: {r.stderr[:200]}", file=sys.stderr)
        sys.exit(1)
    return [line.split("\t") for line in r.stdout.strip().split("\n") if line.strip()]


def main() -> None:
    org = org_id()
    today = date.today()

    rows = q(f"""
      SELECT pattern_label,
             COUNT(*) AS lifetime,
             COUNT(*) FILTER (WHERE session_date >= current_date - {WINDOW_D}) AS in_window,
             MIN(session_date) AS first_seen,
             MAX(session_date) AS last_seen
      FROM pattern_observations WHERE org_id = {org}
        -- live detector only: 838 rows from the retired v1 detector use a
        -- different label vocabulary and would mix two generations in one chart.
        AND detector_version = 'learned-miner-v1'
      GROUP BY pattern_label ORDER BY pattern_label""")

    # weekly counts for sparklines (last SPARK_WEEKS weeks, oldest→newest)
    spark_rows = q(f"""
      SELECT pattern_label,
             ((current_date - session_date) / 7)::int AS weeks_ago,
             COUNT(*)
      FROM pattern_observations
      WHERE org_id = {org} AND detector_version = 'learned-miner-v1'
        AND session_date >= current_date - {SPARK_WEEKS * 7}
      GROUP BY 1, 2""")
    spark: dict[str, list[int]] = {}
    for label, weeks_ago, n in spark_rows:
        arr = spark.setdefault(label, [0] * SPARK_WEEKS)
        idx = SPARK_WEEKS - 1 - int(weeks_ago)
        if 0 <= idx < SPARK_WEEKS:
            arr[idx] = int(n)

    # peak weekly rate (busiest calendar week, lifetime)
    peak_rows = q(f"""
      SELECT pattern_label, MAX(n) FROM (
        SELECT pattern_label, date_trunc('week', session_date::timestamp) AS wk, COUNT(*) AS n
        FROM pattern_observations WHERE org_id = {org} GROUP BY 1, 2) t
      GROUP BY pattern_label""")
    peak = {r[0]: int(r[1]) for r in peak_rows}

    graduated, live, new = [], [], []
    for label, lifetime, in_window, first_seen, last_seen in rows:
        lifetime, in_window = int(lifetime), int(in_window)
        first_d = date.fromisoformat(first_seen)
        last_d = date.fromisoformat(last_seen)
        clean_days = (today - last_d).days
        entry = {
            "label": label,
            "lifetime": lifetime,
            "last14d": in_window,
            "per_wk_now": round(in_window / (WINDOW_D / 7), 1),
            "peak_per_wk": peak.get(label, 0),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "clean_days": clean_days,
            "spark": spark.get(label, [0] * SPARK_WEEKS),
        }
        if (today - first_d).days <= NEW_D:
            new.append(entry)
        elif in_window == 0 and clean_days >= CLEAN_MIN_D and lifetime >= 3:
            graduated.append(entry)
        else:
            live.append(entry)
    graduated.sort(key=lambda e: e["clean_days"], reverse=True)
    live.sort(key=lambda e: e["last14d"], reverse=True)
    new.sort(key=lambda e: e["first_seen"], reverse=True)

    contracts = []
    try:
        fit = json.loads((STATE / "contract-fitness.json").read_text())
        for c in fit.get("contracts", []):
            contracts.append({k: c.get(k) for k in ("contract", "verdict", "pre_rate_per_wk", "post_rate_per_wk", "fire_count", "rationale")})
        fitness_at = fit.get("measured_at")
    except Exception:
        fitness_at = None

    proposals = []
    try:
        resynth = json.loads((INSTANCE / "scheduling" / "claude-si" / "resynth-work" / "resynth-out.json").read_text())
        proposals = resynth.get("proposals", [])
    except Exception:
        pass

    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "org_id": org,
        "window_days": WINDOW_D,
        "graduated": graduated,
        "live": live,
        "new": new,
        "contracts": contracts,
        "fitness_measured_at": fitness_at,
        "proposals_pending": proposals,
    }, indent=1))
    print(f"si-trajectory: {len(graduated)} graduated · {len(live)} live · {len(new)} new → {OUT}")


if __name__ == "__main__":
    main()
