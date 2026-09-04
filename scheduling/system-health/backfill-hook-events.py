#!/usr/bin/env python3
"""Backfill durable hook-event telemetry from session JSONL transcripts.

The JSONL durably records three hook-fire shapes (passes leave no trace — those
accrue once the L0 live logger is wired):

  1. PreToolUse BLOCK  -> user/tool_result content starting "PreToolUse:<Tool> hook error:"
  2. Stop BLOCK        -> user message whose content is a STRING starting "Stop hook feedback:"
  3. UserPromptSubmit/SessionStart INJECT -> attachment {type: hook_(additional_context|success)}

Output (both files use the SAME line schema the L0 live logger will append to, so
history + live form one dataset):

  .claude/state/hook-events.log        (pipe-delimited, durable, version-controlled)
     {iso_ts} | hook={name} | event={Event} | verdict={block|inject} | session={sid} | excerpt={...}

  + a per-hook summary printed to stdout and written to the report path (arg --report).

Usage:
  python3 backfill-hook-events.py [--projects-dir DIR ...] [--out PATH] [--report PATH] [--dry-run]

Read-only on transcripts. Idempotent: rewrites the backfill region of the log each run.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict, Counter

HOME = os.path.expanduser("~")


def _default_projects_dir() -> str:
    """This Core's OWN transcript directory, derived — never hardcoded.

    This line used to be a literal path to core-life's transcripts. That was harmless while
    the file lived only in core-life. On 2026-07-27 scheduling/system-health was added to the
    shared dirs so the hook benchmark could ship, and this file travelled with it — at which
    point every other Core would, on its next close, scan LIFE's session transcripts instead
    of its own and write excerpts of life's content into its own hook-events.log.

    Not a credential leak and not a cross-file overwrite (the output path is correctly scoped
    by CORE_INSTANCE), but it is personal-Core content bleeding into a workplace Core's
    telemetry — the exact class of thing stay-scoped.py exists to prevent — plus a functional
    bug, since those Cores would never record their own events.

    Caught in a RETROACTIVE review, after the file had already shipped: the push listed 20 of
    29 files because sync-to-baseline truncated its own review artifact, so this file was
    invisible to three rounds of adversarial review. Nine other hooks in this codebase already
    derive the path this way; this one simply never did, and nothing compared them.
    """
    instance = os.environ.get("CORE_INSTANCE") or os.getcwd()
    slug = "-" + instance.lstrip("/").replace("/", "-").replace(" ", "-")
    return os.path.join(HOME, ".claude", "projects", slug)


DEFAULT_PROJECTS = [_default_projects_dir()]

# --- canonical hook resolution -------------------------------------------------
# PreToolUse block: hook path is in the tool_result text as hooks/<name>.(sh|py)
PRE_HOOK_RX = re.compile(r"hooks/([\w.-]+)\.(?:sh|py)")

# Stop block: classify by the banner in the injected "Stop hook feedback:" text.
STOP_BANNERS = [
    (re.compile(r"SAY/DO GAP", re.I), "say-do-gap"),
    (re.compile(r"STATE[- ]CLAIM", re.I), "state-claim-gate"),
    (re.compile(r"TIME[- ]CLAIM", re.I), "time-claim-gate"),
    (re.compile(r"RECALL GATE", re.I), "recall-gate"),
    (re.compile(r"LEARNED.*RECALL", re.I), "learned-recallguard"),
    (re.compile(r"LEARNED", re.I), "learned-validator"),
]

# UserPromptSubmit/SessionStart inject: one merged additionalContext per prompt may
# contain multiple hook signatures. Count each present.
INJECT_SIGS = [
    (re.compile(r"BRAIN RECALL TRIGGER", re.I), "brain-recall-trigger"),
    (re.compile(r"VERIFICATION TRIGGER", re.I), "verification-trigger"),
    (re.compile(r"STOP SIGNAL detected", re.I), "stop-signal-gate"),
    (re.compile(r"LEARNED CONTRACT", re.I), "learned-classifier"),
    (re.compile(r"ROT WARNING|\[ROT signal", re.I), "rot-check"),
    (re.compile(r"SESSION-START CHECK", re.I), "session-start-check"),
    (re.compile(r"AWAITING APPROVAL|APPROVAL GATE", re.I), "approval-gate"),
]


def excerpt(s, n=80):
    return re.sub(r"\s+", " ", (s or ""))[:n].strip()


def parse_file(path):
    """Yield event dicts {ts, hook, event, verdict, session, excerpt} from one JSONL."""
    sid = os.path.basename(path).replace(".jsonl", "")
    for line in open(path, errors="replace"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = r.get("timestamp") or ""
        # 3. inject via attachment
        att = r.get("attachment")
        if isinstance(att, dict) and att.get("type") in ("hook_additional_context", "hook_success"):
            blob = json.dumps(att)
            ev = att.get("hookEvent") or "UserPromptSubmit"
            for rx, name in INJECT_SIGS:
                if rx.search(blob):
                    yield {"ts": ts, "hook": name, "event": ev, "verdict": "inject",
                           "session": sid, "excerpt": ""}
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        c = msg.get("content")
        # 2. Stop block: string content starting with the banner
        if isinstance(c, str) and c.startswith("Stop hook feedback:"):
            for rx, name in STOP_BANNERS:
                if rx.search(c[:300]):
                    yield {"ts": ts, "hook": name, "event": "Stop", "verdict": "block",
                           "session": sid, "excerpt": excerpt(c[20:])}
                    break
            continue
        # 1. PreToolUse block: tool_result starting with "PreToolUse:"
        if isinstance(c, list):
            for it in c:
                if isinstance(it, dict) and it.get("type") == "tool_result":
                    txt = it.get("content")
                    if isinstance(txt, str) and txt.startswith("PreToolUse:"):
                        m = PRE_HOOK_RX.search(txt)
                        name = m.group(1) if m else "unknown"
                        yield {"ts": ts, "hook": name, "event": "PreToolUse", "verdict": "block",
                               "session": sid, "excerpt": excerpt(txt[:160])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects-dir", action="append", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--merge", action="store_true",
                    help="union new events with the existing log (dedupe); never shrinks. "
                         "Use for close-time refresh so rotated-away JSONL can't erase history.")
    a = ap.parse_args()
    instance = os.environ.get("CORE_INSTANCE") or os.getcwd()
    dirs = a.projects_dir or DEFAULT_PROJECTS
    out = a.out or os.path.join(instance, ".claude/state/hook-events.log")

    events = []
    files = []
    for d in dirs:
        files += sorted(glob.glob(os.path.join(d, "*.jsonl")))
    for f in files:
        events += list(parse_file(f))
    # sort by timestamp (empty ts sinks to front)
    events.sort(key=lambda e: e["ts"])

    # aggregates
    per_hook = defaultdict(lambda: {"block": 0, "inject": 0, "sessions": set(), "first": "", "last": ""})
    for e in events:
        h = per_hook[e["hook"]]
        h[e["verdict"]] = h.get(e["verdict"], 0) + 1
        h["sessions"].add(e["session"])
        if e["ts"]:
            h["first"] = h["first"] or e["ts"]
            h["last"] = e["ts"]

    lines = [
        f'{e["ts"]} | hook={e["hook"]} | event={e["event"]} | verdict={e["verdict"]} '
        f'| session={e["session"]} | excerpt={e["excerpt"]}'
        for e in events
    ]

    print(f"Scanned {len(files)} transcripts -> {len(events)} hook events reconstructed\n")
    rows = sorted(per_hook.items(), key=lambda kv: -(kv[1]["block"] + kv[1]["inject"]))
    print(f'{"hook":28} {"block":>6} {"inject":>7} {"sessions":>9}  span')
    print("-" * 78)
    report = ["# Hook-event backfill summary", "",
              f"Reconstructed **{len(events)}** events from **{len(files)}** transcripts.",
              "", "_Blocks + injects only — passes leave no JSONL trace (accrue once L0 live logger is wired)._",
              "", "| hook | blocks | injects | sessions | first | last |",
              "|---|---|---|---|---|---|"]
    for name, h in rows:
        span = f'{(h["first"] or "?")[:10]}..{(h["last"] or "?")[:10]}'
        print(f'{name:28} {h["block"]:>6} {h["inject"]:>7} {len(h["sessions"]):>9}  {span}')
        report.append(f'| {name} | {h["block"]} | {h["inject"]} | {len(h["sessions"])} '
                      f'| {(h["first"] or "?")[:10]} | {(h["last"] or "?")[:10]} |')

    if not a.dry_run:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        # Merge-safe write: union reconstructed events with any existing log lines,
        # keyed on (ts,hook,event,verdict,session) so the durable log only ever grows.
        # Without --merge, behavior is the original full-overwrite from this run's events.
        # 2026-07-30 (steering-surface Phase 0): detail + tokens_injected are OPTIONAL so lines
        # written before those fields existed still parse. Also fixes a pre-existing miss —
        # session was `[\w-]+`, which requires at least one character, but the great majority of
        # live lines carry an EMPTY session (`session= |`). That regex therefore failed to match
        # most of the log it was written to read. Now `[\w-]*`.
        LINE_RX = re.compile(
            r"^(?P<ts>\S+) \| hook=(?P<hook>[\w.:-]+) \| event=(?P<event>\S+) "
            r"\| verdict=(?P<verdict>\w+) \| session=(?P<session>[\w-]*) "
            r"(?:\| detail=(?P<detail>[^|]*) )?(?:\| tokens_injected=(?P<tokens>\d+) )?"
            r"\| excerpt=(?P<ex>.*)$")
        merged = {}
        if a.merge and os.path.exists(out):
            for ln in open(out, errors="replace"):
                ln = ln.rstrip("\n")
                if not ln or ln.startswith("#"):
                    continue
                m = LINE_RX.match(ln)
                if not m:
                    continue
                merged[(m["ts"], m["hook"], m["event"], m["verdict"], m["session"])] = ln
        for e in events:  # this run's reconstruction is authoritative for its keys
            merged[(e["ts"], e["hook"], e["event"], e["verdict"], e["session"])] = (
                f'{e["ts"]} | hook={e["hook"]} | event={e["event"]} | verdict={e["verdict"]} '
                f'| session={e["session"]} | excerpt={e["excerpt"]}')
        final = sorted(merged.values(), key=lambda L: L.split(" | ", 1)[0])  # sort by leading ts
        with open(out, "w") as f:
            f.write("# hook-events.log — backfilled from JSONL + appended live by hooklog\n")
            f.write("\n".join(final) + "\n")
        print(f"\nWrote {len(final)} lines -> {out}" + (" (merge)" if a.merge else ""))
        if a.report:
            os.makedirs(os.path.dirname(a.report), exist_ok=True)
            open(a.report, "w").write("\n".join(report) + "\n")
            print(f"Wrote report -> {a.report}")


if __name__ == "__main__":
    main()
