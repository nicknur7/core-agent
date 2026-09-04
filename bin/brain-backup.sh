#!/usr/bin/env bash
# brain-backup.sh — routine, rotated, verified backup of the corebrain database.
#
# WHY THIS EXISTS
# ---------------
# corebrain is the source of truth for every Core's learned layer: si_artifacts,
# friction_cases, assertions, entities, edges. Lose it and every Core forgets not only
# what it knows but what it has learned about how to behave. There is no git copy —
# prior_spec gives per-artifact rollback and active.json is a disposable projection, so
# the database is the only durable record of the thing this whole system produces.
#
# On 2026-07-27, asked directly whether it was backed up, the honest answer was: not
# meaningfully. snapshot.sh existed but is a MANUAL pre-mutation tool that nothing ever
# scheduled. Four dumps sat on disk and the newest was TWENTY DAYS OLD — 85,528 entities
# and 20 days of learned layer on one disk with no rotation, while the entire session was
# spent auditing the repo for hygiene. The durability hole was never in git, and git was
# the only place anyone was looking.
#
# snapshot.sh keeps its job: a labelled dump you take deliberately before a destructive
# step. This is the other half — the one that runs whether or not anyone remembers.
#
#   bash bin/brain-backup.sh              # take one if the newest is older than MAX_AGE
#   bash bin/brain-backup.sh --force      # take one regardless
#   bash bin/brain-backup.sh --status     # report age/count/size, take nothing
#
# Exit 0 = a good backup exists (taken now, or recent enough). Exit 1 = it does not.
set -uo pipefail

SNAP_DIR="${BRAIN_SNAPSHOT_DIR:-$HOME/AI Projects/brain-snapshots}"
DB="${COREBRAIN_DB:-corebrain}"
MAX_AGE_HOURS="${BRAIN_BACKUP_MAX_AGE_HOURS:-24}"
KEEP="${BRAIN_BACKUP_KEEP:-14}"
PREFIX="auto"     # only auto-* rotate; labelled snapshots are never touched

MODE="check"
case "${1:-}" in
  --force)  MODE="force" ;;
  --status) MODE="status" ;;
  "")       ;;
  *) echo "usage: brain-backup.sh [--force|--status]" >&2; exit 2 ;;
esac

mkdir -p "$SNAP_DIR" 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# ORPHAN SWEEP — 2026-08-12. THE CLEANUP BELOW ONLY RUNS IF THIS SCRIPT LIVES.
#
# The .partial/verify/rename design is correct and the two failure paths DO
# `rm -f "$TMP"`. What neither covers is being KILLED: this ran inside the Stop
# hook, which carries `timeout: 60` in settings.json, against a pg_dump that
# takes ~6 minutes. The kill lands before any cleanup line is reached, so every
# close orphaned a ~200MB fragment.
#
# It compounded because `newest_age_hours` globs *.dump only, so a fragment
# never counts as a recent backup: the age check stayed STALE forever, fired
# again on the very next close, and orphaned another fragment. Self-perpetuating.
#
# Measured 2026-08-12 00:01 PDT: 671 fragments, 128 GB, on a disk with 45 GiB
# free — a fifth of the drive, growing ~200MB per assistant turn. The last dump
# that ever COMPLETED was 2026-08-08 15:55; every close since died in pg_dump,
# which is also why grade-gate/grade-intent froze at 2026-08-09 15:56.
#
# Sweeping at startup is the fail-safe: a trap cannot catch SIGKILL, so orphans
# must be recoverable by the NEXT run rather than only preventable by this one.
# 2h, not 0, so a genuinely-running concurrent dump is never deleted underneath.
_sweep_orphaned_partials() {
  local swept=0 f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    rm -f "$f" 2>/dev/null && swept=$((swept + 1))
  done < <(find "$SNAP_DIR" -maxdepth 1 -name '*.dump.partial' -type f -mmin +120 2>/dev/null)
  (( swept > 0 )) && echo "[brain-backup] swept ${swept} orphaned partial(s) from killed runs"
  return 0
}
_sweep_orphaned_partials

newest_age_hours() {
  local newest mtime now
  newest=$(ls -t "$SNAP_DIR"/*.dump 2>/dev/null | head -1)
  [[ -z "$newest" ]] && { echo 999999; return; }
  mtime=$(stat -f %m "$newest" 2>/dev/null || stat -c %Y "$newest" 2>/dev/null || echo 0)
  now=$(date +%s)
  echo $(( (now - mtime) / 3600 ))
}

count=$(ls "$SNAP_DIR"/*.dump 2>/dev/null | wc -l | tr -d ' ')
age=$(newest_age_hours)

if [[ "$MODE" == "status" ]]; then
  echo "brain backups: ${count} dump(s) in $SNAP_DIR"
  if (( count > 0 )); then
    echo "  newest: $(basename "$(ls -t "$SNAP_DIR"/*.dump | head -1)")  (${age}h old)"
    echo "  total:  $(du -sh "$SNAP_DIR" 2>/dev/null | cut -f1)"
  fi
  if (( age <= MAX_AGE_HOURS )); then echo "  status: OK"; exit 0; fi
  echo "  status: STALE — newest is ${age}h old, threshold ${MAX_AGE_HOURS}h"
  exit 1
fi

if [[ "$MODE" == "check" ]] && (( age <= MAX_AGE_HOURS )); then
  echo "[brain-backup] newest dump is ${age}h old (<= ${MAX_AGE_HOURS}h) — nothing to do"
  exit 0
fi

command -v pg_dump >/dev/null 2>&1 || { echo "[brain-backup] pg_dump not on PATH — no backup" >&2; exit 1; }
psql -d "$DB" -c 'SELECT 1' >/dev/null 2>&1 || {
  echo "[brain-backup] cannot reach database '$DB' — no backup taken" >&2; exit 1; }

TS="$(date +%Y%m%d-%H%M%S)"
OUT="$SNAP_DIR/${PREFIX}-${TS}.dump"
TMP="$OUT.partial"

# LOCK — one dump at a time. Without this, detaching the backup from the close (the fix
# that stops the 60s timeout killing it) would let a ~6-minute dump overlap the next
# close's dump, and N concurrent pg_dumps against one database is worse than none.
# mkdir is the atomic primitive that exists everywhere; macOS has no flock.
LOCK="$SNAP_DIR/.backup.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [[ -n "$(find "$LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ]]; then
    rmdir "$LOCK" 2>/dev/null && mkdir "$LOCK" 2>/dev/null || {
      echo "[brain-backup] another backup holds the lock — skipping"; exit 0; }
  else
    echo "[brain-backup] another backup is already running — skipping"
    exit 0
  fi
fi

# TRAP — the fix for the actual defect. The two `rm -f "$TMP"` failure paths below only
# run when this script REACHES them; an external timeout kills it first and leaves the
# fragment behind. A trap on EXIT covers every ordinary exit, and TERM/INT cover the
# timeout's own signal. SIGKILL still cannot be caught — that is what the startup sweep
# above is for. Belt and braces, deliberately: this defect cost 128 GB.
# A TRAP DOES NOT FIRE WHILE A FOREGROUND CHILD IS RUNNING. Dosed 2026-08-12: the first
# version of this trap ran `pg_dump` in the foreground, took a real SIGTERM mid-dump, and
# cleaned up NOTHING — bash queues the handler until the current command returns, which for
# a 6-minute dump is exactly the whole window that matters. The partial and the lock both
# survived the kill, i.e. the fix reproduced the bug it was written for.
# So: pg_dump runs in the BACKGROUND and we `wait` on it (wait is interruptible, the trap
# fires immediately), and the handler kills the child before removing its output.
_cleanup() {
  if [[ -n "${PGPID:-}" ]] && kill -0 "$PGPID" 2>/dev/null; then
    kill -TERM "$PGPID" 2>/dev/null || true
    sleep 1
    kill -9 "$PGPID" 2>/dev/null || true
  fi
  if [[ -n "${TMP:-}" && -f "$TMP" ]]; then rm -f "$TMP" 2>/dev/null || true; fi
  rmdir "$LOCK" 2>/dev/null || true
  return 0
}
trap _cleanup EXIT                 # every ordinary exit path, including `set -e`
trap '_cleanup; exit 143' TERM INT HUP   # the timeout's own signal; do NOT re-exit from EXIT

# Write to .partial and rename only after it verifies. A truncated dump that LOOKS like a
# backup is worse than no backup at all — it is the one you discover while restoring.
pg_dump -Fc -d "$DB" -f "$TMP" 2>/dev/null &
PGPID=$!
wait "$PGPID"; _pg_rc=$?
if (( _pg_rc != 0 )); then
  rm -f "$TMP" 2>/dev/null || true
  echo "[brain-backup] pg_dump FAILED (rc=${_pg_rc}) — nothing written" >&2
  exit 1
fi

# pg_restore --list reads the archive's table of contents and touches no data. If this
# cannot parse it, the file is not a backup no matter what its size says.
if ! pg_restore --list "$TMP" >/dev/null 2>&1; then
  rm -f "$TMP" 2>/dev/null || true
  echo "[brain-backup] dump did not verify as a readable archive — DISCARDED" >&2
  exit 1
fi

mv "$TMP" "$OUT"
echo "[brain-backup] OK  $(basename "$OUT")  ($(du -h "$OUT" | cut -f1))"

# Rotate auto-* only. Labelled snapshots are excluded on purpose: someone took those
# deliberately before a destructive step, and rotation must never be the thing that
# removes a pre-mutation safety net.
old=$(ls -t "$SNAP_DIR/${PREFIX}"-*.dump 2>/dev/null | tail -n +$((KEEP + 1)))
if [[ -n "$old" ]]; then
  while IFS= read -r f; do
    [[ -n "$f" ]] && rm -f "$f" && echo "[brain-backup] rotated out: $(basename "$f")"
  done <<< "$old"
fi
exit 0
