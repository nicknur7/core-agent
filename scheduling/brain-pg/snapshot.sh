#!/usr/bin/env bash
# B1 (federated-brain-plan-2026-07-07 §9) — pre-mutation snapshot tool.
#
# Takes a labeled custom-format pg_dump of the corebrain database BEFORE any
# destructive/mutating brain step (per-Core heavy build, migration, corroborate
# pass, peer migration). §9-B1 requires a REAL snapshot — the plan's prose
# "snapshot before" was never implemented; this is that implementation.
#
# Usage:   snapshot.sh <label>        e.g.  snapshot.sh pre-phase1-business
# Output:  $BRAIN_SNAPSHOT_DIR/<label>-<YYYYmmdd-HHMMSS>.dump
#          (default dir: ~/AI Projects/brain-snapshots — OUTSIDE all git repos)
# Restore: pg_restore --clean --if-exists --no-owner -d "$COREBRAIN_DB" <dumpfile>
#
# Connects as the local OS user (Postgres superuser, BYPASSRLS) so the dump
# captures ALL orgs, not just the caller's. Trust auth on the local socket —
# no password handling.
set -euo pipefail

LABEL="${1:-}"
if [[ -z "$LABEL" ]]; then
  echo "ERROR: label required.  Usage: snapshot.sh <label>  (e.g. pre-phase1-business)" >&2
  exit 2
fi
# sanitize: keep only safe filename chars
LABEL="$(printf '%s' "$LABEL" | tr -c 'A-Za-z0-9._-' '-')"

# COREBRAIN_DB resolver (2026-08-31 fix) — was hardcoded "corebrain". This is a
# PRE-MUTATION SAFETY tool: callers snapshot before a destructive step, then trust the
# snapshot exists if this script exits 0. On a COREBRAIN_DB=other_db install with a
# `corebrain` also present, this silently dumped the WRONG database and reported success
# — a false safety net right before the mutating step it exists to protect against.
COREBRAIN_DB="${COREBRAIN_DB:-corebrain}"

SNAP_DIR="${BRAIN_SNAPSHOT_DIR:-$HOME/AI Projects/brain-snapshots}"
mkdir -p "$SNAP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$SNAP_DIR/${LABEL}-${TS}.dump"

if ! command -v pg_dump >/dev/null 2>&1; then
  echo "ERROR: pg_dump not found on PATH" >&2
  exit 3
fi

echo "Snapshotting $COREBRAIN_DB -> $OUT"
pg_dump -Fc -d "$COREBRAIN_DB" -f "$OUT"
SIZE="$(du -h "$OUT" | cut -f1)"
echo "OK  $OUT  ($SIZE)"
echo
echo "RESTORE (manual, destructive — reverts $COREBRAIN_DB to this snapshot):"
echo "  pg_restore --clean --if-exists --no-owner -d \"$COREBRAIN_DB\" \"$OUT\""
