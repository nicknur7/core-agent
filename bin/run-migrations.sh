#!/usr/bin/env bash
# run-migrations.sh — apply scheduling/brain-pg/migrations/*.sql to corebrain in
# order, tracked so each runs exactly once. The missing piece that left fresh
# forks (one reported 2026-05-26) on a partial schema: INSTALL.md applied only 1 of 6
# migrations by hand, so org_id / RLS / edge-embeddings / brain_admin never
# landed and embed.py errored. Phase 4, spec-brain-unfreeze-2026-05-28.
#
# A schema_migrations(filename, applied_at) tracker records what ran, so the
# individual migration files do NOT need to be idempotent — the RUNNER provides
# idempotency by skipping recorded files.
#
# Modes:
#   (default)     apply every migrations/*.sql not yet recorded, in sorted order.
#   --baseline    record ALL current migrations as applied WITHOUT running them.
#                 For a DB that was already hand-migrated (existing installs) so
#                 the tracker reflects reality without re-running CREATE ROLE etc.
#                 WARNS (and, with --strict, fails) if a migration's tables are
#                 absent — see --ensure.
#   --ensure      object-aware reconcile. For each unrecorded migration: if every
#                 table it declares already exists, record it; otherwise APPLY it,
#                 then verify those tables exist before recording. This is the
#                 correct mode for a fresh DB and for repairing one that was
#                 wrongly baselined.
#   --status      list applied vs pending, then exit.
#
# WHY --ensure EXISTS (2026-07-27, found by executing the documented install on a
# clean clone): setup-brain.sh called --baseline on a FRESH database, on the stated
# assumption that "schema.sql is the COMPLETE current end-state". That assumption is
# false — si_artifacts, si_projection_state, friction_cases, assertions and the
# source-revision ledger exist ONLY in migrations. Blanket-recording them marked 17
# migrations applied while 10 tables were never created, and because the tracker then
# reported them applied, `run-migrations.sh` could never repair it ("0 new, 17
# already-recorded"). A new Core silently had no learned layer and no way to get one.
#
# Connects as the invoking OS user (Postgres superuser on a dev box) — migrations
# create extensions + roles.
# Usage: bash bin/run-migrations.sh [--ensure|--baseline|--status] [--strict]
set -euo pipefail

DB="${COREBRAIN_DB:-corebrain}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIG_DIR="$REPO/scheduling/brain-pg/migrations"
MODE="apply"
STRICT=0

for arg in "$@"; do
  case "$arg" in
    --baseline) MODE="baseline" ;;
    --ensure)   MODE="ensure" ;;
    --status)   MODE="status" ;;
    --strict)   STRICT=1 ;;
    "")         ;;
    *) echo "usage: run-migrations.sh [--ensure|--baseline|--status] [--strict]" >&2; exit 2 ;;
  esac
done

command -v psql >/dev/null 2>&1 || { echo "ERROR: psql not found." >&2; exit 1; }
psql -d "$DB" -c 'SELECT 1' >/dev/null 2>&1 || { echo "ERROR: cannot reach DB '$DB' (create it + apply schema.sql first)." >&2; exit 1; }
[[ -d "$MIG_DIR" ]] || { echo "ERROR: $MIG_DIR not found." >&2; exit 1; }

# Tracker table.
psql -d "$DB" -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SQL

is_applied() { psql -d "$DB" -tA -c "SELECT 1 FROM schema_migrations WHERE filename = '$1'" 2>/dev/null | grep -q 1; }
record()     { psql -d "$DB" -v ON_ERROR_STOP=1 -q -c "INSERT INTO schema_migrations(filename) VALUES ('$1') ON CONFLICT DO NOTHING"; }

# Tables a migration declares it creates. Used to decide whether the migration's
# effect is already present, instead of assuming it. Deliberately conservative:
# only CREATE TABLE is parsed, so a migration that merely ALTERs reports nothing
# and is treated as "cannot verify" (see decide() below).
declared_tables() {
  # Strip -- line comments and /* */ block comments first: a migration header that
  # merely MENTIONS "CREATE TABLE IF NOT EXISTS cannot ..." in prose must not be
  # parsed as declaring a table called "cannot" (2026-07-23b-si-artifacts-pkfix.sql
  # does exactly that, and it produced a permanently-undetectable phantom).
  sed -e 's://.*$::' -e 's:--.*$::' "$1" 2>/dev/null \
    | tr '\n' ' ' | sed 's:/\*[^*]*\*+\([^/*][^*]*\*+\)*/: :g' \
    | grep -oiE 'CREATE[[:space:]]+TABLE([[:space:]]+IF[[:space:]]+NOT[[:space:]]+EXISTS)?[[:space:]]+[a-zA-Z_][a-zA-Z0-9_.]*' \
    | awk '{print tolower($NF)}' | sed 's/^public\.//' | sort -u
}
table_exists() { psql -d "$DB" -tA -c "SELECT to_regclass('public.$1') IS NOT NULL" 2>/dev/null | grep -q '^t$'; }

# Errors that mean "this migration's effect is already present" rather than
# "this migration is broken". schema.sql folds in several older migrations
# (RLS policies, bitemporal columns, edge embeddings), so re-running those files
# legitimately collides. Anything NOT in this list is a real failure.
BENIGN_ERR='already exists|duplicate key|duplicate object|duplicate column|is already a member|already a partition'

# Apply a migration tolerantly: normal run first; if it fails, re-run without
# ON_ERROR_STOP so every statement is attempted, then decide based on whether the
# remaining errors are exclusively benign. Returns 0 = satisfied, 1 = real failure.
apply_tolerant() {
  local file="$1" out rc
  if psql -d "$DB" -v ON_ERROR_STOP=1 -q -f "$file" >/dev/null 2>&1; then
    return 0
  fi
  # ON_ERROR_ROLLBACK=on wraps each statement inside a transaction block in an
  # implicit savepoint, so one benign collision rolls back only that statement
  # instead of aborting the file's whole BEGIN/COMMIT and cascading
  # "current transaction is aborted" through every statement after it.
  out="$(psql -d "$DB" -v ON_ERROR_ROLLBACK=on -q -f "$file" 2>&1 || true)"
  local real
  real="$(printf '%s\n' "$out" | grep -E '^psql.*ERROR:' | grep -vEi "$BENIGN_ERR" || true)"
  if [[ -n "$real" ]]; then
    # A migration whose target table does not exist yet is not broken — it is out of
    # order. The learned-layer schemas (learned_contracts, pattern_observations) are
    # created by install-learned-layer.sh, which runs AFTER setup-brain.sh, yet
    # 2026-07-19-learned-contracts-rls.sql hardens one of those tables. Defer it and
    # let the learned-layer installer re-run --ensure once the table exists, instead
    # of hard-failing the whole brain setup on an ordering artifact.
    if printf '%s\n' "$real" | grep -qE 'relation "[^"]+" does not exist'; then
      printf '%s\n' "$real" | head -2 >&2
      return 2
    fi
    printf '%s\n' "$real" >&2
    return 1
  fi
  return 0
}

# Returns: present | absent | unknown
objects_present() {
  local tables; tables="$(declared_tables "$1")"
  [[ -z "$tables" ]] && { echo unknown; return; }
  local t
  while IFS= read -r t; do
    [[ -z "$t" ]] && continue
    table_exists "$t" || { echo absent; return; }
  done <<< "$tables"
  echo present
}

if [[ "$MODE" == "status" ]]; then
  echo "[migrations] DB=$DB"
  drift=0
  for f in "$MIG_DIR"/*.sql; do
    [[ -e "$f" ]] || continue
    b="$(basename "$f")"
    if is_applied "$b"; then
      if [[ "$(objects_present "$f")" == "absent" ]]; then
        echo "  ⚠ DRIFT    $b  (recorded applied, but its tables are MISSING — run --ensure)"; drift=$((drift+1))
      else
        echo "  ✓ applied  $b"
      fi
    else
      echo "  • PENDING  $b"
    fi
  done
  [[ $drift -gt 0 ]] && echo "[migrations] $drift migration(s) recorded but not actually present. Repair: bash bin/run-migrations.sh --ensure"
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# SERIALISE THE WHOLE CHECK -> APPLY -> RECORD SEQUENCE ACROSS CORES.
#
# `corebrain` is ONE database shared by all five Cores, and this runner does
# is_applied() -> psql -f -> record() as three separate connections. Nothing bound them
# together, so two Cores running migrations at overlapping times could both read a migration
# as pending and both apply it. Codex flagged it 2026-08-05; the failure mode is a duplicate
# object error that aborts one Core's run part-way through the set, leaving the remainder
# unapplied — loud rather than silently corrupting, but still a broken run to untangle by hand.
#
# The window is real and got realer: `run-migrations.sh` is invoked from SessionStart pulls and
# from run-brain-update.sh's heavy pass, so it fires without anyone deciding to run it.
#
# A POSTGRES advisory lock rather than a file lock: the contended resource is the database, and
# a lock held in the database is correct even if a Core is ever run from another host or a
# container. `pg_advisory_lock` is session-scoped, so it is taken in a psql session that is held
# open for the duration of the loop via a coprocess-style background psql reading from a FIFO;
# the simpler `pg_advisory_xact_lock` cannot span three separate psql invocations.
#
# The key is an arbitrary but FIXED 64-bit constant — every Core must pick the same number or
# they lock different things and serialise nothing.
MIG_LOCK_KEY=778811223344
_MIG_LOCK_FIFO=""
_MIG_LOCK_MARK=""
_MIG_LOCK_PID=""

release_migration_lock() {
  # Postgres releases SESSION-level advisory locks when the session disconnects, so killing the
  # holder is a correct release even when this script is being killed rather than exiting cleanly.
  [[ -n "$_MIG_LOCK_PID" ]] && kill "$_MIG_LOCK_PID" 2>/dev/null || true
  exec 9>&- 2>/dev/null || true
  [[ -n "$_MIG_LOCK_FIFO" ]] && rm -f "$_MIG_LOCK_FIFO" 2>/dev/null || true
  [[ -n "$_MIG_LOCK_MARK" ]] && rm -f "$_MIG_LOCK_MARK" 2>/dev/null || true
  _MIG_LOCK_PID=""; _MIG_LOCK_FIFO=""; _MIG_LOCK_MARK=""
}
trap release_migration_lock EXIT INT TERM

acquire_migration_lock() {
  local waited=0 timeout=300
  _MIG_LOCK_FIFO="$(mktemp -u "${TMPDIR:-/tmp}/core-mig-lock.XXXXXX")"
  _MIG_LOCK_MARK="$(mktemp "${TMPDIR:-/tmp}/core-mig-mark.XXXXXX")"
  if ! mkfifo "$_MIG_LOCK_FIFO" 2>/dev/null; then
    echo "[migrations] WARN: cannot create lock FIFO — proceeding UNSERIALISED" >&2
    _MIG_LOCK_FIFO=""; return 0
  fi
  # A psql session held open on the FIFO: it blocks in pg_advisory_lock until granted, and only
  # THEN prints the marker. So the marker's appearance is proof this session holds the lock.
  #
  # Polling pg_locks from a second connection would have been wrong, and was the first version of
  # this: an advisory lock held by ANOTHER Core is also `granted`, so that check would have seen a
  # rival's lock, concluded "we hold it", and proceeded straight through the mutex it exists to be.
  ( psql -d "$DB" -tA -f - < "$_MIG_LOCK_FIFO" > "$_MIG_LOCK_MARK" 2>&1 ) &
  _MIG_LOCK_PID=$!
  exec 9>"$_MIG_LOCK_FIFO"          # keep the write end open for the life of the script
  printf 'SELECT pg_advisory_lock(%s);\nSELECT %s;\n' "$MIG_LOCK_KEY" "'CORE_MIG_LOCK_HELD'" >&9
  while :; do
    if grep -q 'CORE_MIG_LOCK_HELD' "$_MIG_LOCK_MARK" 2>/dev/null; then
      echo "[migrations] holding advisory lock $MIG_LOCK_KEY (serialised across all Cores)"
      return 0
    fi
    # If the holder died the lock is not held and never will be — fail open loudly rather than
    # spinning for five minutes on a psql that exited immediately.
    if ! kill -0 "$_MIG_LOCK_PID" 2>/dev/null; then
      echo "[migrations] WARN: lock session exited (${_MIG_LOCK_MARK}: $(head -c 200 "$_MIG_LOCK_MARK" 2>/dev/null))" >&2
      echo "[migrations] WARN: proceeding UNSERIALISED" >&2
      return 0
    fi
    sleep 2; waited=$((waited+2))
    if [[ $waited -ge $timeout ]]; then
      echo "[migrations] WARN: lock not granted after ${timeout}s — another Core may be mid-migration." >&2
      echo "[migrations] Refusing to run unserialised on a shared database. Re-run when it finishes." >&2
      exit 1
    fi
    [[ $((waited % 20)) -eq 0 ]] && echo "[migrations] waiting for another Core's migration run (${waited}s)..."
  done
}

acquire_migration_lock

# --ensure repairs a wrongly-baselined DB, so it must reconsider migrations that are
# already RECORDED, not just unrecorded ones.
applied=0; skipped=0; repaired=0; warned=0; deferred=0
for f in "$MIG_DIR"/*.sql; do
  [[ -e "$f" ]] || continue
  b="$(basename "$f")"
  present="$(objects_present "$f")"

  if is_applied "$b"; then
    if [[ "$MODE" == "ensure" && "$present" == "absent" ]]; then
      echo "[migrations] REPAIR $b — recorded applied but its tables are missing; applying now."
      if apply_tolerant "$f"; then
        if [[ "$(objects_present "$f")" == "absent" ]]; then
          echo "[migrations] ✗ $b ran but its tables still absent — stopping." >&2; exit 1
        fi
        echo "[migrations] ✓ repaired $b"; repaired=$((repaired+1))
      else
        echo "[migrations] ✗ FAILED repairing $b — stopping." >&2; exit 1
      fi
    else
      skipped=$((skipped+1))
    fi
    continue
  fi

  if [[ "$MODE" == "baseline" ]]; then
    if [[ "$present" == "absent" ]]; then
      echo "[migrations] ⚠ WARNING: baselining $b but its tables do NOT exist." >&2
      echo "[migrations]   This records a migration as applied that never ran. Use --ensure on a fresh DB." >&2
      warned=$((warned+1))
      [[ $STRICT -eq 1 ]] && { echo "[migrations] --strict: refusing to record a phantom migration." >&2; exit 1; }
    fi
    record "$b"; echo "[migrations] baselined (marked applied, not run): $b"; applied=$((applied+1)); continue
  fi

  # apply / ensure, unrecorded
  if [[ "$MODE" == "ensure" && "$present" == "present" ]]; then
    record "$b"; echo "[migrations] already satisfied, recorded: $b"; skipped=$((skipped+1)); continue
  fi

  echo "[migrations] applying $b ..."
  if [[ "$MODE" == "ensure" ]]; then
    # Tolerate collisions with effects schema.sql already folded in; still fail on
    # genuine errors. Then confirm any declared tables really landed.
    rc=0; apply_tolerant "$f" || rc=$?
    if [[ $rc -eq 0 ]]; then
      if [[ "$(objects_present "$f")" == "absent" ]]; then
        echo "[migrations] ✗ $b applied but its declared tables are still absent — stopping." >&2; exit 1
      fi
      record "$b"; echo "[migrations] ✓ $b"; applied=$((applied+1))
    elif [[ $rc -eq 2 ]]; then
      echo "[migrations] ⏸ DEFERRED $b — its target table doesn't exist yet (created by a later installer). Not recorded."
      deferred=$((deferred+1))
    else
      echo "[migrations] ✗ FAILED on $b — stopping (nothing past this point applied)." >&2
      exit 1
    fi
  elif psql -d "$DB" -v ON_ERROR_STOP=1 -q -f "$f"; then
    record "$b"; echo "[migrations] ✓ $b"; applied=$((applied+1))
  else
    echo "[migrations] ✗ FAILED on $b — stopping (nothing past this point applied)." >&2
    exit 1
  fi
done

if [[ $warned -gt 0 ]]; then
  echo "[migrations] ⚠ $warned migration(s) were baselined without their tables present. Repair: bash bin/run-migrations.sh --ensure" >&2
fi
[[ $repaired -gt 0 ]] && echo "[migrations] repaired $repaired previously-phantom migration(s)."
[[ $deferred -gt 0 ]] && echo "[migrations] $deferred deferred (dependency not yet created). install-learned-layer.sh re-runs --ensure to pick them up."

echo "[migrations] done. ${MODE}: ${applied} new, ${skipped} already-recorded."
