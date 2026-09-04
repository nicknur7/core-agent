#!/usr/bin/env python3
"""
One-shot backfill for mixed-format timestamps in access-log.md.
Run once with `--apply` from repo root. DO NOT add new mixed-format entries —
canonical format from 2026-04-24 forward is `YYYY-MM-DD HH:MM UTC`.

Usage:
    python3 scripts/backfill-access-log-timestamps.py            # dry-run
    python3 scripts/backfill-access-log-timestamps.py --apply    # write changes
"""

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

ACCESS_LOG = "memory/access-log.md"

# Patterns in priority order (most specific first)

# ISO 8601: 2026-04-21T04:18:00Z
ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
)

# YYYY-MM-DD HH:MM UTC/PDT/PST (with optional leading ~)
PLAIN_RE = re.compile(
    r"^(~?\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (UTC|PDT|PST)"
)

OFFSETS = {
    "UTC": 0,
    "PDT": 7,   # UTC-7
    "PST": 8,   # UTC-8
}


def convert_line(line: str):
    """
    Returns (new_line, changed, reason) where reason is a human-readable note.
    If the line doesn't start with a timestamp, returns (line, False, None).
    """

    # ISO 8601 match
    m = ISO_RE.match(line)
    if m:
        iso_str = m.group(1)
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        canonical = dt.strftime("%Y-%m-%d %H:%M UTC")
        new_line = line.replace(iso_str, canonical, 1)
        changed = new_line != line
        return new_line, changed, f"ISO→UTC: {iso_str} → {canonical}"

    # Plain timestamp match
    m = PLAIN_RE.match(line)
    if m:
        ts_part = m.group(1)      # e.g. "2026-04-21 04:18" or "~2026-04-21 19:00"
        tz_label = m.group(2)     # UTC / PDT / PST
        offset_hours = OFFSETS[tz_label]

        if tz_label == "UTC":
            # Already canonical — nothing to change
            return line, False, None

        # Strip leading ~ for parsing
        clean_ts = ts_part.lstrip("~")
        dt_naive = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M")
        dt_utc = dt_naive + timedelta(hours=offset_hours)
        canonical = dt_utc.strftime("%Y-%m-%d %H:%M UTC")

        # Replace the original timestamp+tz token in the line
        original_token = f"{ts_part} {tz_label}"
        new_line = line.replace(original_token, canonical, 1)
        changed = new_line != line
        return new_line, changed, f"{tz_label}→UTC: {original_token} → {canonical}"

    return line, False, None


def main():
    ap = argparse.ArgumentParser(description="Backfill mixed-format timestamps in access-log.md to canonical UTC.")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = ap.parse_args()

    with open(ACCESS_LOG, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    total = len(lines)
    changed_count = 0
    unchanged_count = 0
    unparseable = []
    samples = []
    new_lines = []

    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        new_stripped, changed, reason = convert_line(stripped)
        new_lines.append(new_stripped + "\n" if line.endswith("\n") else new_stripped)

        if changed:
            changed_count += 1
            if len(samples) < 3:
                samples.append((stripped, new_stripped, reason))
            print(f"  CHANGED line {i+1}: {reason}", file=sys.stderr)
        else:
            # Check if this line looks like it starts with a timestamp-ish thing but we didn't parse it
            # Heuristic: starts with a 4-digit year
            if re.match(r"^\d{4}-", stripped) and not PLAIN_RE.match(stripped) and not ISO_RE.match(stripped):
                unparseable.append((i + 1, stripped))
            unchanged_count += 1

    print(f"\n--- Dry-run summary ---", file=sys.stderr)
    print(f"Total lines   : {total}", file=sys.stderr)
    print(f"Changed       : {changed_count}", file=sys.stderr)
    print(f"Unchanged     : {unchanged_count}", file=sys.stderr)
    print(f"Unparseable   : {len(unparseable)}", file=sys.stderr)

    if unparseable:
        print(f"\nUnparseable lines:", file=sys.stderr)
        for lineno, content in unparseable:
            print(f"  line {lineno}: {content[:120]}", file=sys.stderr)

    print(f"\nSample conversions (up to 3):", file=sys.stderr)
    for before, after, reason in samples:
        print(f"  BEFORE: {before[:100]}", file=sys.stderr)
        print(f"  AFTER : {after[:100]}", file=sys.stderr)
        print(f"  ({reason})", file=sys.stderr)
        print(file=sys.stderr)

    if args.apply:
        with open(ACCESS_LOG, "w", encoding="utf-8") as fh:
            fh.writelines(new_lines)
        print(f"Applied: {changed_count} lines rewritten to {ACCESS_LOG}", file=sys.stderr)
    else:
        print("Dry-run only. Pass --apply to write changes.", file=sys.stderr)


if __name__ == "__main__":
    main()
