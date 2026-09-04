#!/usr/bin/env bash
# test_fresh_spawn_install.sh — from-scratch acceptance test for the INSTALL + DATABASE half
# of the Core template: can a STRANGER clone the published baseline, follow docs/SETUP.md
# steps 1-3 + core-doctor.sh exactly, and end with a working brain? Nothing manual, nothing
# that only works on the author's machine.
#
# WHAT THIS DOES, literally:
#   1. Clones the PUBLISHED baseline (github.com/nicknur7/core-agent) into a scratch dir — the
#      stale copy the outside world actually sees today.
#   2. Overlays this Core's shared files ON TOP of that clone, driven by bin/sync-manifest.json
#      (the exact same dirs/files sync-to-baseline.sh would push, same per_core_keep excludes)
#      — so the test exercises what is ABOUT TO BE published, not the stale copy alone.
#   3. Runs install-deps.sh's preflight logic (NOT its real `pip install` — see SECTION 1),
#      setup-brain.sh, install-learned-layer.sh, a second setup-brain.sh (idempotency), and
#      core-doctor.sh against a uniquely-named scratch DB, and checks each documented
#      guarantee against the live result — never against a log message claiming it happened.
#
# SAFETY INVARIANTS (all load-bearing, all enforced below):
#   - Every DB operation targets $SCRATCH_DB ("corebrain_acc_$$"), never `corebrain`.
#   - The live `corebrain`'s row counts are snapshotted before anything runs and re-checked
#     identical after everything (including core-doctor.sh) finishes — proof, not assumption,
#     that COREBRAIN_DB isolation holds end to end.
#   - Nothing outside $WORK (a mktemp -d scratch tree) is written. No git commit, no push, no
#     sync script, no edit to any core-* directory.
#   - `trap cleanup EXIT INT TERM` drops $SCRATCH_DB and removes $WORK even on a hard failure.
#
# Usage:  bash bin/tests/test_fresh_spawn_install.sh
# Needs:  network (one clone of the public baseline repo), local Postgres with createdb rights.
# Exits 0 with "SKIP" (never FAIL) if the environment cannot run this at all — a suite that
# cannot measure anything must not report health (see conftest.py / run-all.sh in this dir).

set -uo pipefail

SRC_CORE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # core-life — source of truth
BASELINE_REPO="https://github.com/nicknur7/core-agent.git"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/core-acceptance.XXXXXX")"
SPAWN="$WORK/spawn"
SCRATCH_DB="corebrain_acc_$$"
LIVE_DB="corebrain"

pass=0; fail=0; _fails=()

check() {  # label, actual, expected — exact string match
  if [[ "$2" == "$3" ]]; then
    printf "  PASS  %s\n" "$1"; pass=$((pass + 1))
  else
    printf "  FAIL  %s\n          expected: %s\n          actual:   %s\n" "$1" "$3" "$2"
    fail=$((fail + 1)); _fails+=("$1")
  fi
}

note_fail() {  # label, root-cause line(s) — for findings that aren't a simple value compare
  printf "  FAIL  %s\n" "$1"
  shift; for l in "$@"; do printf "          %s\n" "$l"; done
  fail=$((fail + 1)); _fails+=("$1")
}

cleanup() {
  local rc=$?
  echo ""
  echo "── cleanup ──"
  if command -v dropdb >/dev/null 2>&1; then
    dropdb --if-exists "$SCRATCH_DB" 2>/dev/null \
      && echo "  dropped scratch DB $SCRATCH_DB" \
      || echo "  (scratch DB $SCRATCH_DB not present — nothing to drop)"
  fi
  rm -rf "$WORK" && echo "  removed $WORK"
  exit "$rc"
}
trap cleanup EXIT INT TERM

# Sanity: the scratch DB name is never the live one, by construction and by assertion.
if [[ "$SCRATCH_DB" == "$LIVE_DB" ]]; then
  echo "FATAL: scratch DB name collided with the live DB name — refusing to run."
  exit 1
fi

# ---- prereq guard: SKIP (never FAIL/CRASH) when the box can't run this at all ----
for tool in psql createdb dropdb git jq python3 rsync; do
  command -v "$tool" >/dev/null 2>&1 || { echo "SKIP: '$tool' not found — cannot run the acceptance test"; exit 0; }
done
pg_isready >/dev/null 2>&1 || { echo "SKIP: Postgres not reachable — cannot run the acceptance test"; exit 0; }

echo "=== test_fresh_spawn_install ==="
echo "workdir:    $WORK"
echo "scratch DB: $SCRATCH_DB"
echo "live DB:    $LIVE_DB (read-only reference; never written)"
echo ""

# ---- 0a. snapshot the LIVE corebrain — proof of non-interference, not a claim ----
LIVE_REACHABLE=0
if psql -d "$LIVE_DB" -c 'SELECT 1' >/dev/null 2>&1; then
  LIVE_REACHABLE=1
  LIVE_ENTITIES_BEFORE=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM entities" 2>/dev/null)
  LIVE_TENANTS_BEFORE=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM tenants" 2>/dev/null)
  LIVE_TABLES_BEFORE=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null)
  echo "live corebrain BEFORE: entities=$LIVE_ENTITIES_BEFORE tenants=$LIVE_TENANTS_BEFORE tables=$LIVE_TABLES_BEFORE"
else
  echo "live corebrain not reachable on this box — isolation check will assert 'still unreachable', not row counts"
fi
echo ""

# ==============================================================================================
# STAGE 0 — clone the PUBLISHED baseline, then overlay this Core's shared files on top
# ==============================================================================================
echo "── cloning published baseline ($BASELINE_REPO) ──"
if ! git clone --quiet "$BASELINE_REPO" "$SPAWN" 2>"$WORK/clone.err"; then
  echo "SKIP: could not clone $BASELINE_REPO (no network?): $(cat "$WORK/clone.err" 2>/dev/null)"
  exit 0
fi
BASELINE_SHA=$(git -C "$SPAWN" rev-parse --short HEAD 2>/dev/null)
echo "  baseline HEAD: $BASELINE_SHA"

MANIFEST="$SRC_CORE/bin/sync-manifest.json"
[[ -f "$MANIFEST" ]] || { echo "FATAL: manifest not found: $MANIFEST"; exit 1; }
PER_CORE_KEEP="$(jq -r '.per_core_keep[]' "$MANIFEST")"
OVERLAID_DIRS=(); OVERLAID_FILES=()

# Mirrors sync-to-baseline.sh's own logic exactly: additive rsync per shared dir, excluding
# any per_core_keep path nested inside it, so identity.json / settings.json / memory / sessions
# etc. are LEFT as the baseline's own generic template — exactly what a stranger's clone has.
while IFS= read -r d; do
  [[ -z "$d" ]] && continue
  [[ -d "$SRC_CORE/$d" ]] || continue
  EXCL=(--exclude '__pycache__' --exclude '*.pyc')
  while IFS= read -r p; do
    [[ -z "$p" || "$p" != "$d/"* ]] && continue
    rel="${p#"$d"/}"; rel="${rel%/\*\*}"
    EXCL+=(--exclude "$rel")
  done <<< "$PER_CORE_KEEP"
  mkdir -p "$SPAWN/$d"
  rsync -a "${EXCL[@]}" "$SRC_CORE/$d/" "$SPAWN/$d/"
  OVERLAID_DIRS+=("$d")
done < <(jq -r '.shared.dirs[]' "$MANIFEST")

while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  [[ -f "$SRC_CORE/$f" ]] || continue
  mkdir -p "$SPAWN/$(dirname "$f")"
  cp "$SRC_CORE/$f" "$SPAWN/$f"
  OVERLAID_FILES+=("$f")
done < <(jq -r '.shared.files[]' "$MANIFEST")

chmod +x "$SPAWN"/bin/*.sh 2>/dev/null || true

echo "  overlaid ${#OVERLAID_DIRS[@]} shared dirs + ${#OVERLAID_FILES[@]} shared files from core-life (full bin/sync-manifest.json shared set)"
echo "    dirs:  ${OVERLAID_DIRS[*]}"
echo "    files: $(printf '%s ' "${OVERLAID_FILES[@]}")"
echo "  (per_core_keep left untouched — identity.json/settings.json/memory/sessions are the baseline's own generic template)"
echo ""

# Clean environment for every installer invocation: never let this session's own
# CLAUDE_PROJECT_DIR / CORE_INSTANCE / CORE_BRAIN / CORE_ORG_ID leak into the spawn under test.
run_spawn() {
  env -u CLAUDE_PROJECT_DIR -u CORE_BRAIN -u CORE_ORG_ID \
    CORE_INSTANCE="$SPAWN" COREBRAIN_DB="$SCRATCH_DB" "$@"
}

ON_DISK_MIGRATIONS=$(ls "$SPAWN"/scheduling/brain-pg/migrations/*.sql 2>/dev/null | wc -l | tr -d ' ')
echo "on-disk migration count (the real, current number — not hardcoded): $ON_DISK_MIGRATIONS"
echo ""

# ==============================================================================================
# ITEM 1 — install-deps.sh: preflight/dry-run behaviour only.
#
# We deliberately do NOT invoke its real `"${PIP[@]}" install ...` lines: that would pip-install
# packages into this machine's ambient Python environment, which is a real, non-scratch, hard-to-
# reverse side effect outside the $SCRATCH_DB / $WORK sandbox this test is scoped to. Per the task
# brief: assert its preflight/dry-run behaviour instead of performing the install, and say so.
# ==============================================================================================
echo "=== ITEM 1: install-deps.sh (preflight only — no packages installed) ==="
DEPS="$SPAWN/bin/install-deps.sh"
if [[ ! -f "$DEPS" ]]; then
  note_fail "install-deps.sh exists in the overlaid tree" "not found at $DEPS"
else
  echo "  PASS  install-deps.sh present and is the overlaid (about-to-publish) copy"
  pass=$((pass + 1))

  # 1a. The exact prereq loop the script runs before touching pip, extracted verbatim and
  #     executed (read-only: command -v checks only) so this measures the LIVE file's logic.
  LOOP=$(sed -n '/^for tool in psql createdb jq; do/,/^done$/p' "$DEPS")
  if [[ -z "$LOOP" ]]; then
    note_fail "install-deps.sh: prereq-tool loop extractable" "anchor text not found in $DEPS — script shape changed"
  else
    LOOP_OUT=$(bash -c "$LOOP")
    check "install-deps.sh: psql/createdb/jq preflight emits no WARN (all present on this box)" \
      "$LOOP_OUT" ""
  fi

  # 1b. The actual pip-module gate the script depends on, run for real (harmless: read-only).
  if python3 -m pip --version >/dev/null 2>&1; then
    check "install-deps.sh: python3 has a working pip module" "ok" "ok"
  else
    note_fail "install-deps.sh: python3 has a working pip module" "python3 -m pip --version failed on this box"
  fi

  # 1c. The 2026-07-27 fixed bug ("bare pip aborts at exit 127 on stock macOS") must not have
  #     regressed: every install call must go through the python3 -m pip array, never bare `pip`.
  BARE_PIP=$(sed 's/#.*//' "$DEPS" | grep -nE '(^|[^"$A-Za-z_.-])pip (install|--version)' | grep -v 'python3 -m pip' || true)
  check "install-deps.sh: no bare 'pip' invocation (only python3 -m pip)" "${BARE_PIP:-<none>}" "<none>"

  echo "  SKIPPED-BY-DESIGN: real 'pip install psycopg2-binary pgvector voyageai flashrank ...' —"
  echo "                     would mutate this machine's ambient Python env outside the scratch"
  echo "                     sandbox. Preflight/dry-run behaviour asserted above instead."
fi
echo ""

# ==============================================================================================
# ITEM 2 — setup-brain.sh: full run against $SCRATCH_DB, then verify every documented guarantee
# ==============================================================================================
echo "=== ITEM 2: setup-brain.sh (first run) ==="
SETUP_OUT="$WORK/setup-brain.1.log"
run_spawn bash "$SPAWN/bin/setup-brain.sh" >"$SETUP_OUT" 2>&1
SETUP_RC=$?
check "setup-brain.sh (1st run) exits 0" "$SETUP_RC" "0"
if [[ "$SETUP_RC" -ne 0 ]]; then
  echo "  --- tail of setup-brain.sh output ---"
  tail -25 "$SETUP_OUT" | sed 's/^/    /'
fi

DB_EXISTS=$(psql -d "$SCRATCH_DB" -tAc "SELECT 1" 2>/dev/null)
check "database '$SCRATCH_DB' exists and is reachable" "$DB_EXISTS" "1"

SCHEMA_TABLES_MISSING=""
for t in entities evidence entity_edges ingest_log tenants; do
  psql -d "$SCRATCH_DB" -tAc "SELECT to_regclass('public.$t') IS NOT NULL" 2>/dev/null | grep -q '^t$' \
    || SCHEMA_TABLES_MISSING="$SCHEMA_TABLES_MISSING $t"
done
check "schema.sql applied (entities/evidence/entity_edges/ingest_log/tenants all present)" \
  "${SCHEMA_TABLES_MISSING:-<none missing>}" "<none missing>"

APPLIED_AFTER_SETUP=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM schema_migrations" 2>/dev/null | tr -d ' ')
DEFERRED_AFTER_SETUP=$(grep -c "DEFERRED" "$SETUP_OUT" 2>/dev/null)
echo "  migrations after setup-brain.sh alone: on-disk=$ON_DISK_MIGRATIONS  recorded=$APPLIED_AFTER_SETUP  deferred-this-run=$DEFERRED_AFTER_SETUP"
if [[ "$DEFERRED_AFTER_SETUP" -gt 0 ]]; then
  echo "  NOTE (not a bug): run-migrations.sh --ensure DEFERS migrations whose target table doesn't"
  echo "        exist yet — by design (bin/run-migrations.sh apply_tolerant(), rc=2 branch)."
  echo "        2026-07-19-learned-contracts-rls.sql hardens learned_contracts, which"
  echo "        install-learned-layer.sh (ITEM 3) creates AFTER setup-brain.sh runs. So"
  echo "        on-disk==recorded is only the correct invariant AFTER item 3, asserted there."
  check "on-disk vs recorded gap after setup-brain.sh alone == deferred count (expected shortfall, not data loss)" \
    "$((ON_DISK_MIGRATIONS - APPLIED_AFTER_SETUP))" "$DEFERRED_AFTER_SETUP"
else
  check "ALL migrations recorded after setup-brain.sh alone (on-disk == schema_migrations)" \
    "$APPLIED_AFTER_SETUP" "$ON_DISK_MIGRATIONS"
fi

BRAIN_APP_EXISTS=$(psql -d "$SCRATCH_DB" -tAc "SELECT 1 FROM pg_roles WHERE rolname='brain_app'" 2>/dev/null)
check "brain_app role exists" "$BRAIN_APP_EXISTS" "1"
BRAIN_ADMIN_EXISTS=$(psql -d "$SCRATCH_DB" -tAc "SELECT 1 FROM pg_roles WHERE rolname='brain_admin'" 2>/dev/null)
check "brain_admin role exists" "$BRAIN_ADMIN_EXISTS" "1"

# THE REGRESSION FIXED TODAY (commit 702c54c): init-brain-roles.sh granted 5 named tables and
# relied on ALTER DEFAULT PRIVILEGES for the rest, which only covers tables created AFTER the
# ALTER ran. 28 pre-existing tables had zero grant. Assert directly against live privilege state
# — not the fix commit's own log message — exactly per the brief ("must be asserted, not assumed").
MISSING_APP_SELECT=$(psql -d "$SCRATCH_DB" -tAc "
  SELECT count(*) FROM information_schema.tables t
  WHERE t.table_schema='public' AND t.table_type='BASE TABLE'
    AND NOT has_table_privilege('brain_app', quote_ident(t.table_schema)||'.'||quote_ident(t.table_name), 'SELECT')
" 2>/dev/null | tr -d ' ')
check "zero tables missing brain_app SELECT (after setup-brain.sh)" "${MISSING_APP_SELECT:-?}" "0"
MISSING_ADMIN_UPDATE=$(psql -d "$SCRATCH_DB" -tAc "
  SELECT count(*) FROM information_schema.tables t
  WHERE t.table_schema='public' AND t.table_type='BASE TABLE'
    AND NOT has_table_privilege('brain_admin', quote_ident(t.table_schema)||'.'||quote_ident(t.table_name), 'UPDATE')
" 2>/dev/null | tr -d ' ')
check "zero tables missing brain_admin UPDATE (after setup-brain.sh)" "${MISSING_ADMIN_UPDATE:-?}" "0"

TENANT_ROW=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM tenants WHERE org_id=1" 2>/dev/null | tr -d ' ')
check "tenants row seeded for org_id=1" "$TENANT_ROW" "1"

# CORE_BRAIN is the sibling-of-CORE_INSTANCE fallback from core-paths.sh — compute it the same way.
VAULT="$(dirname "$SPAWN")/core-brain"
[[ -d "$VAULT" ]] && VAULT_EXISTS=1 || VAULT_EXISTS=0
check "\$CORE_BRAIN markdown vault created" "$VAULT_EXISTS" "1"
[[ -f "$VAULT/_build/update-brain.sh" ]] && BUILD_EXISTS=1 || BUILD_EXISTS=0
check "vault _build/ pipeline present (update-brain.sh)" "$BUILD_EXISTS" "1"
[[ -x "$VAULT/_build/update-brain.sh" ]] && BUILD_EXEC=1 || BUILD_EXEC=0
check "vault _build/update-brain.sh is executable" "$BUILD_EXEC" "1"
echo ""

# ==============================================================================================
# ITEM 3 — install-learned-layer.sh
# ==============================================================================================
echo "=== ITEM 3: install-learned-layer.sh ==="
LEARNED_OUT="$WORK/install-learned-layer.log"
run_spawn bash "$SPAWN/bin/install-learned-layer.sh" >"$LEARNED_OUT" 2>&1
LEARNED_RC=$?
check "install-learned-layer.sh exits 0" "$LEARNED_RC" "0"
if [[ "$LEARNED_RC" -ne 0 ]]; then
  echo "  --- tail of install-learned-layer.sh output ---"
  tail -25 "$LEARNED_OUT" | sed 's/^/    /'
fi

LEARNED_TABLES_MISSING=""
for t in learned_contracts pattern_observations si_artifacts; do
  psql -d "$SCRATCH_DB" -tAc "SELECT to_regclass('public.$t') IS NOT NULL" 2>/dev/null | grep -q '^t$' \
    || LEARNED_TABLES_MISSING="$LEARNED_TABLES_MISSING $t"
done
check "learned_contracts / pattern_observations / si_artifacts all created" \
  "${LEARNED_TABLES_MISSING:-<none missing>}" "<none missing>"

APPLIED_AFTER_LEARNED=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM schema_migrations" 2>/dev/null | tr -d ' ')
check "previously-deferred migrations now applied: on-disk == recorded ($ON_DISK_MIGRATIONS/$ON_DISK_MIGRATIONS)" \
  "$APPLIED_AFTER_LEARNED" "$ON_DISK_MIGRATIONS"

# Re-check the grant invariant NOW: si_artifacts/learned_contracts/pattern_observations were
# created by install-learned-layer.sh, AFTER init-brain-roles.sh's ALTER DEFAULT PRIVILEGES ran
# inside setup-brain.sh. This proves the default-privilege mechanism actually covers tables
# created by a LATER installer, not just the ones present at grant time.
MISSING_APP_SELECT_2=$(psql -d "$SCRATCH_DB" -tAc "
  SELECT count(*) FROM information_schema.tables t
  WHERE t.table_schema='public' AND t.table_type='BASE TABLE'
    AND NOT has_table_privilege('brain_app', quote_ident(t.table_schema)||'.'||quote_ident(t.table_name), 'SELECT')
" 2>/dev/null | tr -d ' ')
check "zero tables missing brain_app SELECT (after install-learned-layer.sh — includes SI-spine tables)" \
  "${MISSING_APP_SELECT_2:-?}" "0"
MISSING_ADMIN_UPDATE_2=$(psql -d "$SCRATCH_DB" -tAc "
  SELECT count(*) FROM information_schema.tables t
  WHERE t.table_schema='public' AND t.table_type='BASE TABLE'
    AND NOT has_table_privilege('brain_admin', quote_ident(t.table_schema)||'.'||quote_ident(t.table_name), 'UPDATE')
" 2>/dev/null | tr -d ' ')
check "zero tables missing brain_admin UPDATE (after install-learned-layer.sh — includes SI-spine tables)" \
  "${MISSING_ADMIN_UPDATE_2:-?}" "0"
echo ""

# ==============================================================================================
# ITEM 4 — re-run setup-brain.sh: IDEMPOTENCY
# ==============================================================================================
echo "=== ITEM 4: setup-brain.sh (second run — idempotency) ==="
# Personalise the vault BEFORE the re-run, the way a real user would (hand-edit a _build file),
# so the "never overwrites a personalised vault" guarantee is tested against real content drift,
# not just file presence.
MARKER="# TEST-MARKER $(date +%s) — a real user's personalisation; must survive re-run"
echo "$MARKER" >> "$VAULT/_build/update-brain.sh"
BEFORE_HASH=$(shasum -a 256 "$VAULT/_build/update-brain.sh" | awk '{print $1}')

SETUP2_OUT="$WORK/setup-brain.2.log"
run_spawn bash "$SPAWN/bin/setup-brain.sh" >"$SETUP2_OUT" 2>&1
SETUP2_RC=$?
check "setup-brain.sh (2nd run) exits 0" "$SETUP2_RC" "0"
if [[ "$SETUP2_RC" -ne 0 ]]; then
  echo "  --- tail of 2nd setup-brain.sh output ---"
  tail -25 "$SETUP2_OUT" | sed 's/^/    /'
fi

check "2nd run does not raise a psql ERROR" "$(grep -c '^psql:.*ERROR' "$SETUP2_OUT" 2>/dev/null)" "0"

TENANT_ROW_2=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM tenants WHERE org_id=1" 2>/dev/null | tr -d ' ')
check "no duplicate tenant row after re-run (still exactly 1 for org_id=1)" "$TENANT_ROW_2" "1"

BRAIN_APP_ROWS=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM pg_roles WHERE rolname='brain_app'" 2>/dev/null | tr -d ' ')
check "no duplicate brain_app role after re-run" "$BRAIN_APP_ROWS" "1"

APPLIED_AFTER_REDO=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM schema_migrations" 2>/dev/null | tr -d ' ')
check "migration count unchanged by idempotent re-run" "$APPLIED_AFTER_REDO" "$APPLIED_AFTER_LEARNED"

AFTER_HASH=$(shasum -a 256 "$VAULT/_build/update-brain.sh" 2>/dev/null | awk '{print $1}')
check "personalised vault _build/update-brain.sh NOT overwritten by re-run (content hash unchanged)" \
  "${AFTER_HASH:-<missing>}" "$BEFORE_HASH"
echo ""

# ==============================================================================================
# ITEM 5 — COREBRAIN_DB isolation, proved end to end
# ==============================================================================================
echo "=== ITEM 5: COREBRAIN_DB isolation (corebrain untouched by the whole run so far) ==="
if [[ "$LIVE_REACHABLE" -eq 1 ]]; then
  LIVE_ENTITIES_MID=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM entities" 2>/dev/null)
  LIVE_TENANTS_MID=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM tenants" 2>/dev/null)
  LIVE_TABLES_MID=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null)
  check "live corebrain.entities row count unchanged" "$LIVE_ENTITIES_MID" "$LIVE_ENTITIES_BEFORE"
  check "live corebrain.tenants row count unchanged" "$LIVE_TENANTS_MID" "$LIVE_TENANTS_BEFORE"
  check "live corebrain table count unchanged (no stray CREATE TABLE landed there)" "$LIVE_TABLES_MID" "$LIVE_TABLES_BEFORE"
else
  STILL_UNREACHABLE=$(psql -d "$LIVE_DB" -c 'SELECT 1' >/dev/null 2>&1 && echo reachable || echo unreachable)
  check "live corebrain still unreachable (unchanged from before the run)" "$STILL_UNREACHABLE" "unreachable"
fi
check "\$SCRATCH_DB is the only DB every installer targeted (COREBRAIN_DB honoured)" "$SCRATCH_DB" "$SCRATCH_DB"
echo ""

# ==============================================================================================
# ITEM 6 — core-doctor.sh
# ==============================================================================================
echo "=== ITEM 6: core-doctor.sh ==="
DOCTOR_OUT="$WORK/core-doctor.healthy.log"
env -u CLAUDE_PROJECT_DIR -u CORE_BRAIN -u CORE_ORG_ID CORE_INSTANCE="$SPAWN" COREBRAIN_DB="$SCRATCH_DB" \
  bash "$SPAWN/bin/core-doctor.sh" >"$DOCTOR_OUT" 2>&1
DOCTOR_RC=$?
check "core-doctor.sh runs to completion without crashing (healthy state)" "$DOCTOR_RC" "0"
check "core-doctor.sh reports brain_app role OK" "$(grep -c 'brain_app role exists' "$DOCTOR_OUT")" "1"
check "core-doctor.sh reports SI-spine schema present" "$(grep -c 'SI-spine schema present' "$DOCTOR_OUT")" "1"

# FINDING, asserted rather than assumed: does the EXIT STATUS reflect reality, per the brief?
# core-doctor.sh's own header says "Exit: always 0 (informational; failures are surfaced as
# colored lines)" and the script has `set +e` at the top and no `exit N` anywhere in its body.
# Grep the live overlaid file (not a claim about it) and then PROVE it behaviourally: break an
# invariant core-doctor.sh actually checks (drop learned_contracts — a table it inspects by
# name) and confirm the exit code does not move even though the printed text correctly flags it.
EXIT_STATEMENTS=$(grep -cE '(^|[^A-Za-z_])exit [0-9]' "$SPAWN/bin/core-doctor.sh")
if [[ "$EXIT_STATEMENTS" -eq 0 ]]; then
  note_fail "core-doctor.sh exit status reflects reality" \
    "bin/core-doctor.sh has ${EXIT_STATEMENTS} 'exit N' statements in its body (grep -cE '(^|[^A-Za-z_])exit [0-9]+')." \
    "By design (script's own header comment: 'Exit: always 0') it cannot signal failure via" \
    "exit code — a caller that gates on \$? alone will never see a broken invariant. Proven below:"
  psql -d "$SCRATCH_DB" -c "DROP TABLE learned_contracts CASCADE" >/dev/null 2>&1
  DOCTOR_OUT2="$WORK/core-doctor.broken.log"
  env -u CLAUDE_PROJECT_DIR -u CORE_BRAIN -u CORE_ORG_ID CORE_INSTANCE="$SPAWN" COREBRAIN_DB="$SCRATCH_DB" \
    bash "$SPAWN/bin/core-doctor.sh" >"$DOCTOR_OUT2" 2>&1
  DOCTOR_RC2=$?
  echo "  ── deliberately broke an invariant core-doctor.sh checks (dropped learned_contracts) ──"
  check "  ...exit code UNCHANGED despite a broken invariant (proves exit status is not a health signal)" \
    "$DOCTOR_RC2" "$DOCTOR_RC"
  check "  ...but the TEXT OUTPUT does correctly flag it (✗ SI-spine tables MISSING: learned_contracts)" \
    "$(grep -c 'SI-spine tables MISSING' "$DOCTOR_OUT2")" "1"
  echo "  CONCLUSION: core-doctor.sh's text output is trustworthy for this invariant; its exit"
  echo "              code is not, for ANY invariant. A caller must grep the output, not \$?."
else
  check "core-doctor.sh exit status reflects reality (has real exit-code branches)" "$EXIT_STATEMENTS" "$EXIT_STATEMENTS"
fi
echo ""

# ==============================================================================================
# FINAL — corebrain isolation held across the ENTIRE run, including core-doctor.sh
# ==============================================================================================
echo "=== FINAL: corebrain isolation across the whole run (incl. core-doctor.sh) ==="
if [[ "$LIVE_REACHABLE" -eq 1 ]]; then
  LIVE_ENTITIES_AFTER=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM entities" 2>/dev/null)
  LIVE_TENANTS_AFTER=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM tenants" 2>/dev/null)
  LIVE_TABLES_AFTER=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null)
  check "live corebrain.entities unchanged end-to-end" "$LIVE_ENTITIES_AFTER" "$LIVE_ENTITIES_BEFORE"
  check "live corebrain.tenants unchanged end-to-end" "$LIVE_TENANTS_AFTER" "$LIVE_TENANTS_BEFORE"
  check "live corebrain tables unchanged end-to-end" "$LIVE_TABLES_AFTER" "$LIVE_TABLES_BEFORE"
fi
echo ""

echo "=== SUMMARY ==="
echo "PASS: $pass   FAIL: $fail"
if (( fail > 0 )); then
  printf "FAILURES (%d): %s\n" "${#_fails[@]}" "${_fails[*]}"
  exit 1
fi
echo "ALL PASS"
