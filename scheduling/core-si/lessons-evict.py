#!/usr/bin/env python3
"""Bounded lesson retirement — Reflexion capacity-eviction + DGM stepping-stone guard.

Spec: tasks/si-loop-completeness-map-2026-06-07.md §6 (external-research deltas).

The lessons file grows unbounded; the ~25-entry re-curation threshold in CLAUDE.md is
never enforced mechanically. This proposes (PROPOSAL mode, default) which lessons to
RETIRE — and it is deliberately conservative so it never strips live context:

  RETIRE candidate  := older than --age-days (default 30)
                       AND (active-count over --cap  OR  file over --token-cap)
                       AND NOT a stepping-stone (no linked hook in its body)   <- DGM guard
  Retire oldest-first only as many as needed to get back under both caps.

  The TOKEN cap was added 2026-07-30 because the count cap could not see the failure that
  happened: 22 entries (under the cap of 25, so zero proposals) costing 14,287 tokens on every
  turn. Entries got fat rather than numerous. See classify() for the full note.

  --apply MOVES retired entries to lessons-archive.md (reversible; never deletes).
  Default is proposal-only: prints what it WOULD retire, changes nothing.

DGM lineage guard: a lesson whose body mentions a hook (".sh"/".py" hook, "hook",
"enforced by", "gate") is treated as a stepping-stone that graduated to structure —
retiring it would lose the WHY behind a live mechanism. Kept regardless of age.
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[2]
LESSONS = CORE / "tasks" / "lessons.md"
ARCHIVE = CORE / "tasks" / "lessons-archive.md"

HOOK_RX = re.compile(
    r"\b(hook|enforced by|state-claim-gate|say-do-gap|time-claim-gate|stop-signal|"
    r"pretooluse-guard|verification-trigger|rot-check|\.sh\b|gate\b|codified)\b", re.I)
# 2026-07-30 (master plan Phase 0.5): `##` ONLY was wrong, and wrong in the direction that made
# this tool silently useless. tasks/lessons.md writes its ACTIVE entries as `###` — the `##`
# level is reserved for section banners and for the older archived block. So on the live file
# this reported "0 active entries" against 6,822 tokens of lessons, proposed nothing, and looked
# like a healthy no-op every time it was consulted. A parser that matches nothing and a parser
# that finds nothing to do are indistinguishable from the outside, which is why this went
# unnoticed: the tool was never wired to run, and when run by hand it printed 0 and looked fine.
HEADER_RX = re.compile(r"^#{2,3}\s+(\d{4}-\d{2}-\d{2})\s*(?:—|-)\s*(.*)$")


def parse_entries(text: str):
    """Return [(date|None, title, raw_block)] per '## or ### YYYY-MM-DD — title' section."""
    lines = text.splitlines(keepends=True)
    # find header line indices
    idxs = [i for i, ln in enumerate(lines) if HEADER_RX.match(ln.strip())]
    entries = []
    for n, i in enumerate(idxs):
        end = idxs[n + 1] if n + 1 < len(idxs) else len(lines)
        block = "".join(lines[i:end])
        m = HEADER_RX.match(lines[i].strip())
        try:
            d = dt.date.fromisoformat(m.group(1))
        except ValueError:
            d = None
        entries.append((d, m.group(2).strip(), block))
    return entries


def classify(entries, today, age_days, cap, token_cap=None):
    """Two independent pressures, either of which can put an entry over the line.

    COUNT (the original) and TOKENS (added 2026-07-30, master plan Phase 0.5).

    The count cap could not see the failure that actually happened. On 2026-07-30 lessons.md
    held 22 entries — under the cap of 25, so this proposed nothing — while costing 14,287
    tokens on EVERY turn, the single largest item in the per-turn load, ahead of every rules
    file combined. The 2026-06-26 re-curation had capped the entry COUNT and the entries then
    grew into 310-token essays. A count cap cannot see a file that got fat rather than long,
    and tokens are what the fitness function is denominated in: a lesson earns its keep when it
    saves more re-explaining than it costs to carry, and cost is measured in tokens per turn.

    Token eviction runs oldest-first over non-stepping-stones, exactly like count eviction, and
    stops as soon as the file is under budget. The DGM stepping-stone guard is unchanged and
    still absolute: a lesson whose body names a live hook is never retired by either pressure,
    because retiring it loses the WHY behind a mechanism that is still running.
    """
    active = len(entries)
    total_chars = sum(len(b) for _d, _t, b in entries)
    over_tokens = token_cap is not None and (total_chars // 4) > token_cap
    out = []
    for d, title, block in entries:
        age = (today - d).days if d else 0
        stepping_stone = bool(HOOK_RX.search(block))
        old_enough = d is not None and age >= age_days
        reason = "keep"
        if stepping_stone:
            reason = "keep:stepping-stone(linked-hook)"
        elif not old_enough:
            reason = f"keep:recent({age}d<{age_days})"
        elif active <= cap and not over_tokens:
            reason = f"keep:under-cap({active}<={cap})"
        else:
            reason = "RETIRE"
        out.append({"date": d, "title": title, "age": age, "chars": len(block),
                    "stepping_stone": stepping_stone, "reason": reason, "block": block})
    # retire oldest-first: down to the count cap, and then further while still over the token cap
    retirable = sorted([e for e in out if e["reason"] == "RETIRE"], key=lambda e: e["date"])
    overflow = max(0, active - cap)
    chosen = retirable[:overflow]
    if token_cap is not None:
        remaining = total_chars - sum(e["chars"] for e in chosen)
        for e in retirable[len(chosen):]:
            if remaining // 4 <= token_cap:
                break
            chosen.append(e)
            remaining -= e["chars"]
    keep_retire = set(id(e) for e in chosen)
    for e in out:
        if e["reason"] == "RETIRE" and id(e) not in keep_retire:
            e["reason"] = f"keep:within-cap-after-older-retired"
    return out, active


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--age-days", type=int, default=30)
    ap.add_argument("--cap", type=int, default=25)
    # 2,500 tok of the ~4,000-token always-loaded budget Phase 4.1 puts in CI. lessons.md is the
    # file that carries LIVE corrections, so it gets the largest single share; it was at 14,287
    # before today's archive pass and 6,822 after, i.e. still over on its own.
    ap.add_argument("--token-cap", type=int, default=2500,
                    help="evict oldest non-stepping-stones until lessons.md fits this budget")
    ap.add_argument("--apply", action="store_true", help="MOVE retired entries to lessons-archive.md (reversible)")
    ap.add_argument("--today", help="override today (YYYY-MM-DD) for testing")
    args = ap.parse_args()
    if not LESSONS.exists():
        sys.exit(f"no lessons file at {LESSONS}")
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    entries = parse_entries(LESSONS.read_text(encoding="utf-8"))
    classified, active = classify(entries, today, args.age_days, args.cap, args.token_cap)
    retire = [e for e in classified if e["reason"] == "RETIRE"]
    stones = [e for e in classified if e["stepping_stone"]]
    tok = sum(len(e["block"]) for e in classified) // 4
    print(f"[lessons-evict] {active} active entries · {tok} tok · cap={args.cap} · "
          f"token-cap={args.token_cap} · age-gate={args.age_days}d · today={today}")
    print(f"  stepping-stones kept (linked to a hook): {len(stones)}")
    print(f"  RETIRE proposals: {len(retire)}")
    for e in retire:
        print(f"    - {e['date']} ({e['age']}d)  {e['title'][:70]}")
    if not retire:
        if active > args.cap:
            print(f"  NOTE: {active}>{args.cap} but all overflow is recent or hook-linked — "
                  f"age-eviction protects it. Over-cap reduction here needs JUDGMENT re-curation "
                  f"(merge/consolidate similar lessons), not mechanical age-eviction.")
        return 0
    if not args.apply:
        print("  (proposal only — rerun with --apply to MOVE these to lessons-archive.md)")
        return 0
    # --apply: move retired blocks to archive, rewrite lessons.md without them
    arch = ARCHIVE.read_text(encoding="utf-8") if ARCHIVE.exists() else "# Lessons archive\n"
    moved = "".join(e["block"] for e in retire)
    ARCHIVE.write_text(arch.rstrip() + "\n\n" + moved, encoding="utf-8")
    full = LESSONS.read_text(encoding="utf-8")
    for e in retire:
        full = full.replace(e["block"], "")
    LESSONS.write_text(full, encoding="utf-8")
    print(f"  APPLIED: moved {len(retire)} entries to {ARCHIVE.name} (reversible via git)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
