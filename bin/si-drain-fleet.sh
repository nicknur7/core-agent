#!/usr/bin/env bash
# si-drain-fleet.sh — run each seat's OWN si-drain.sh, sequentially.
#
# SEQUENTIAL BY DESIGN. All five seats share one brain lock with NO timeout (Nick's 2026-07-24
# call: "run until the queue clears"). Firing five drains on one cron minute would queue them on
# that lock with nobody watching. Running them one after another means the lock is never contended
# and each seat's own refuse-if-locked guard stays a backstop rather than the mechanism.
#
# EACH SEAT RUNS ITS OWN COPY. si-drain.sh is in bin/ (shared), so every seat gets it on pull, and
# each resolves its own org from its own identity.json. This runner never passes an org — a
# hardcoded CORE_ORG_ID in fleet-shared code already cross-wrote partitions once (2026-07-25).
#
# INTERPRETER: si-drain.sh pins its own. Do not rely on this PATH.
set -uo pipefail
# Fleet runner logs into the seat it was LAUNCHED from (life), not shared /tmp — bin/ syncs
# everywhere, so /tmp/si-drain-fleet-<date>.log is one file five Cores would interleave into.
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$_HERE/.claude/state/logs"
LOG="$_HERE/.claude/state/logs/si-drain-fleet-$(date +%F).log"
echo "[$(date)] === fleet drain start ===" >> "$LOG"
for SEAT in core-life core-business core-school core-finance core-ops; do
  D="$HOME/AI Projects/$SEAT"
  if [[ ! -x "$D/bin/si-drain.sh" ]]; then
    echo "[$(date)] $SEAT: no si-drain.sh yet (has not pulled) — skipped" >> "$LOG"
    continue
  fi
  # PER-SEAT HOLD. Nick deferred core-ops on 2026-08-28 ("ops is busy at the moment") while
  # approving unattended nightly SI everywhere else. Without this the deferral was honoured only by
  # ops not having pulled yet — i.e. it would have quietly expired the moment ops synced, which is
  # a decision being enforced by an accident. The marker is OWNED BY THE SEAT: ops lifts its own
  # hold by deleting the file, and no other Core can lift it from here.
  if [[ -f "$D/.claude/state/.si-drain-hold" ]]; then
    echo "[$(date)] $SEAT: HOLD — $(head -c 200 "$D/.claude/state/.si-drain-hold" | tr '\n' ' ')" >> "$LOG"
    continue
  fi
  echo "[$(date)] $SEAT: running" >> "$LOG"
  ( cd "$D" && CORE_BRAIN="$HOME/AI Projects/core-brain" bash bin/si-drain.sh >> "$LOG" 2>&1 )
  echo "[$(date)] $SEAT: exit=$?" >> "$LOG"
done
echo "[$(date)] === fleet drain done ===" >> "$LOG"
