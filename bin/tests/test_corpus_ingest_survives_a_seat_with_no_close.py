#!/usr/bin/env python3
"""Corpus ingest must be reachable on a seat that never closes a session and has no launchd job.

WHY THIS EXISTS (2026-08-20).

Every Core's correction corpus was frozen for 70-92 hours and nothing anywhere said so:

    org 1 life      1293 rows   newest 2026-08-17 10:46   78.1h
    org 2 business   327 rows   newest 2026-08-17 18:34   70.3h
    org 3 school     235 rows   newest 2026-08-16 20:22   92.5h
    org 4 finance     37 rows   newest 2026-08-16 20:22   92.5h
    org 5 ops       130 rows   newest 2026-08-16 20:24   92.4h

Ingest had two doors and both were shut. Door one is a session close, and Nick's sessions run for
days. Door two is `run-brain-update.sh` heavy, which the 02:00 nightly reached ONLY when
`verify-brain-synced.py --nightly-debt` reported debt — a probe that asks whether graph.json is
stale, which says nothing whatever about whether there are unmined corrections. Four consecutive
nights logged `BRAIN-DEBT: none -> no-op`. **The job ran perfectly every night. It was answering a
different question**, and the learning loop's input had become a side effect of an embedding
schedule.

And door two exists on ONE SEAT. `com.nick.brain-pipeline` is pinned CORE_INSTANCE=core-life,
CORE_ORG_ID=1; no other plist references any Core lifecycle. So four of five Cores had exactly one
door, and it only opens when Nick types /close-core or walks away from a session.

WHAT THIS ASSERTS — reachability, never a row count. A seat may legitimately ingest zero rows
(life's own backlog was 6, three seats' was 0); what it may never do is have no path at all.

  1. session-start-check.sh invokes the corpus miner. This is the only door that exists on every
     seat without a system-level install, because `.claude/hooks/` is shared and SessionStart fires
     on all five.
  2. That invocation is gated to startup|clear. A compact re-fire is not a new session; ungating it
     would run ingest on every context compaction.
  3. It is backgrounded. It runs on the critical path of every session open, and a 2.15s
     foreground scan there is a latency regression nobody asked for.
  4. The nightly runs the miner ABOVE the brain-debt gate — the specific defect above. If the call
     ever sinks below the probe again, ingest silently re-couples to the embedding schedule.
  5. The nightly's heavy rebuild stays debt-gated. Nick's 2026-07-25 call was "a nightly job that is
     just a fall back ... it shouldn't be used", about the graphify rebuild. Fixing ingest must not
     smuggle in a nightly full rebuild.

This test is deliberately static. The dynamic version would need a DB and a session boundary, and a
test that cannot run on a fresh clone is a test that stops being run.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START = ROOT / ".claude" / "hooks" / "session-start-check.sh"
LIFECYCLE = ROOT / ".claude" / "hooks" / "session-lifecycle.sh"

failures = []
checks = 0


def check(label, ok, detail=""):
    global checks
    checks += 1
    print(("  ok     " if ok else "  FAIL   ") + label + (("\n           " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(label)


def block_containing(text, needle, opener, closer):
    """Return the source of the innermost `opener`...`closer` block containing needle."""
    i = text.index(needle)
    start = text.rindex(opener, 0, i)
    end = text.index(closer, i)
    return text[start:end]


start_src = START.read_text()
life_src = LIFECYCLE.read_text()

MINER = "learned-corpus-miner.py"

# --- 1. the universal door exists ------------------------------------------------------------
check("session-start-check.sh invokes the corpus miner",
      MINER in start_src,
      "no seat without a launchd job has any ingest path that does not require a session close")

if MINER in start_src:
    # the enclosing `if [[ ... ]]; then` for the miner call
    guard = block_containing(start_src, MINER, "if [[", "fi")

    # --- 2. gated to real session starts ------------------------------------------------------
    gated = ('HOOK_SOURCE" == "startup"' in guard and 'HOOK_SOURCE" == "clear"' in guard)
    check("the SessionStart ingest is gated to startup|clear",
          gated,
          "ungated, it fires on every compact re-fire, which is not a new session")

    # --- 3. off the critical path -------------------------------------------------------------
    backgrounded = ("nohup" in guard and "&" in guard)
    check("the SessionStart ingest is backgrounded",
          backgrounded,
          "a foreground scan on every session open is a latency regression")

# --- 4. the nightly mines above the debt gate -------------------------------------------------
if "lifecycle_nightly()" in life_src:
    nightly = life_src[life_src.index("lifecycle_nightly()"):]
    nightly = nightly[:nightly.index("\n}\n")] if "\n}\n" in nightly else nightly

    has_miner = MINER in nightly
    check("lifecycle_nightly runs the corpus miner", has_miner)

    if has_miner:
        probe = "--nightly-debt"
        check("the nightly's miner call is ABOVE the brain-debt gate",
              probe in nightly and nightly.index(MINER) < nightly.index(probe),
              "below the probe, ingest only happens on nights the GRAPH is stale — the 2026-08-20 defect")

        # --- 5. Nick's 2026-07-25 heavy-rebuild decision survives ------------------------------
        # The gated body launches the rebuild through $CORE_HOOK_RUN_BRAIN_UPDATE, not a literal
        # path — asserting on the literal is how this check failed the first time it was written.
        heavy = re.search(r"if\s+CORE_INSTANCE=.*?--nightly-debt.*?then(.*?)\n  fi", nightly, re.S)
        body = heavy.group(1) if heavy else ""
        check("the heavy rebuild is still gated behind the debt probe",
              heavy is not None and "CORE_HOOK_RUN_BRAIN_UPDATE" in body and "heavy" in body,
              "Nick's call was that the nightly rebuild is a fallback, not a routine job")
else:
    check("session-lifecycle.sh defines lifecycle_nightly", False)

print()
if failures:
    print("  FAIL=%d of %d" % (len(failures), checks))
    for f in failures:
        print("    - " + f)
    sys.exit(1)
print("  ok=%d  FAIL=0 — ingest is reachable without a close and without a launchd job" % checks)
