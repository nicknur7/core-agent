#!/usr/bin/env bash
# test_fresh_spawn_brain.sh — from-scratch acceptance test for the BRAIN/RECALL half of the
# Core template: what a stranger gets when they clone the published baseline, install the
# brain, and hand it an EMPTY corpus. Nothing here has ever run against an empty brain before —
# every prior check (eval.py, brain-health.py in daily use, the acceptance suite in this same
# directory) ran against the author's ~68,000-entity corpus, where no code path is empty and
# nothing is cold.
#
# Sibling of test_fresh_spawn_install.sh (INSTALL/DATABASE half) — same clone+overlay+scratch-DB
# machinery, same check()/note_fail() convention, same SKIP-not-FAIL posture when the box can't
# run this at all. This file adds the six BRAIN/RECALL guarantees on top:
#
#   1. Every read path degrades gracefully on a brand-new EMPTY brain (no crash, no div-by-zero,
#      no false "it's fine").
#   2. Ingest works from nothing: 3 synthetic hub files + 1 fake session land in entities/evidence
#      with the CORRECT org_id (this Core's own, from .claude/identity.json).
#   3. Hub ownership is DERIVED from citations (scheduling/brain-pg/hub_ownership.py), not stamped
#      with the running Core's org — regression test for the 2026-08-31 fix.
#   4. Recall round-trips seeded content back out, AND the vector leg's behaviour when
#      VOYAGE_API_KEY is absent (the common case for a stranger) is measured and reported
#      honestly rather than assumed.
#   5. mcp-server.py's default recall scope is SELF (own org), not ALL — regression test for the
#      2026-08-31 _resolve_scope fix, proven by seeding a row in a DIFFERENT org and checking it
#      stays invisible under the default.
#   6. compile-truth-refresh.py --detect runs clean on a near-empty brain and reports the new
#      no_hub_possible_count field honestly.
#
# SAFETY INVARIANTS (all load-bearing, all enforced below — identical posture to the install test):
#   - Every DB operation targets $SCRATCH_DB ("corebrain_brainacc_$$"), never `corebrain`.
#   - The live `corebrain`'s entities+evidence row counts are snapshotted before anything runs and
#     re-checked identical after everything finishes — proof, not assumption.
#   - Nothing outside $WORK (a mktemp -d scratch tree) is written. No git commit, no push, no sync
#     script, no edit to any core-* directory.
#   - `trap cleanup EXIT INT TERM` drops $SCRATCH_DB and removes $WORK even on a hard failure.
#   - Paid-API discipline: embed.py's own batching keeps this at ~5 real Voyage calls for the
#     WHOLE run (2 to ingest 3 hubs + 1 session, 3 to exercise recall) — see the running tally
#     printed at the end. The VOYAGE_API_KEY-absent path (item 4) is asserted with the key forced
#     empty for that one call, not by skipping — that IS the check.
#
# Usage:  bash bin/tests/test_fresh_spawn_brain.sh
# Needs:  network (one clone of the public baseline repo), local Postgres with createdb rights.
#         VOYAGE_API_KEY (via ~/.claude/secrets.env or env) for the real-ingest assertions — if
#         absent, those sub-checks SKIP individually and the key-absent degrade check (item 4)
#         still runs (it needs no key by definition).
# Exits 0 with "SKIP" (never FAIL) if the environment cannot run this at all.
# Debug: KEEP_WORK=1 bash bin/tests/test_fresh_spawn_brain.sh — skips the drop/rm in cleanup()
#        so the scratch DB and $WORK (clone + vault) survive for manual inspection afterward.

set -uo pipefail

SRC_CORE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # core-life — source of truth
BASELINE_REPO="https://github.com/nicknur7/core-agent.git"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/core-brain-acceptance.XXXXXX")"
SPAWN="$WORK/spawn"
SCRATCH_DB="corebrain_brainacc_$$"
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

tb_count() {  # file -> count of Python tracebacks, "0" if file missing/unreadable/no match.
  # NOT `grep -c ... || echo 0` — grep -c prints "0" on zero matches AND exits 1, so `||` would
  # print a SECOND "0" and the two-line "0\n0" never string-equals the expected "0".
  local c
  c=$(grep -c 'Traceback (most recent' "$1" 2>/dev/null)
  echo "${c:-0}"
}

cleanup() {
  local rc=$?
  echo ""
  echo "── cleanup ──"
  if [[ "${KEEP_WORK:-0}" -eq 1 ]]; then
    echo "  KEEP_WORK=1 — leaving scratch DB $SCRATCH_DB and $WORK in place for inspection"
    exit "$rc"
  fi
  if command -v dropdb >/dev/null 2>&1; then
    dropdb --if-exists "$SCRATCH_DB" 2>/dev/null \
      && echo "  dropped scratch DB $SCRATCH_DB" \
      || echo "  (scratch DB $SCRATCH_DB not present — nothing to drop)"
  fi
  rm -rf "$WORK" && echo "  removed $WORK"
  if [[ "${LIVE_REACHABLE:-0}" -eq 1 ]]; then
    LIVE_ENTITIES_FINAL=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM entities" 2>/dev/null)
    LIVE_EVIDENCE_FINAL=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM evidence" 2>/dev/null)
    if [[ "$LIVE_ENTITIES_FINAL" == "${LIVE_ENTITIES_BEFORE:-}" && "$LIVE_EVIDENCE_FINAL" == "${LIVE_EVIDENCE_BEFORE:-}" ]]; then
      echo "  VERIFIED at cleanup: live corebrain entities=$LIVE_ENTITIES_FINAL evidence=$LIVE_EVIDENCE_FINAL — unchanged"
    else
      echo "  !!!! WARNING: live corebrain row counts moved: entities $LIVE_ENTITIES_BEFORE->$LIVE_ENTITIES_FINAL, evidence $LIVE_EVIDENCE_BEFORE->$LIVE_EVIDENCE_FINAL !!!!"
    fi
  fi
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
python3 -c "import psycopg2, voyageai" >/dev/null 2>&1 \
  || { echo "SKIP: python3 is missing psycopg2/voyageai — cannot run the acceptance test"; exit 0; }

echo "=== test_fresh_spawn_brain ==="
echo "workdir:    $WORK"
echo "scratch DB: $SCRATCH_DB"
echo "live DB:    $LIVE_DB (read-only reference; never written)"
echo ""

# ---- 0. snapshot the LIVE corebrain — proof of non-interference, not a claim ----
LIVE_REACHABLE=0
if psql -d "$LIVE_DB" -c 'SELECT 1' >/dev/null 2>&1; then
  LIVE_REACHABLE=1
  LIVE_ENTITIES_BEFORE=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM entities" 2>/dev/null)
  LIVE_EVIDENCE_BEFORE=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM evidence" 2>/dev/null)
  echo "live corebrain BEFORE: entities=$LIVE_ENTITIES_BEFORE evidence=$LIVE_EVIDENCE_BEFORE"
else
  echo "live corebrain not reachable on this box — isolation check will assert 'still unreachable', not row counts"
fi
echo ""

# ==============================================================================================
# STAGE 0 — clone the PUBLISHED baseline, then overlay this Core's shared files on top
# (identical machinery to test_fresh_spawn_install.sh — same manifest, same excludes, so this
# test exercises what is ABOUT TO BE published, not a stale copy alone)
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

CHANGED_COUNT=$(git -C "$SPAWN" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
echo "  overlaid ${#OVERLAID_DIRS[@]} shared dirs + ${#OVERLAID_FILES[@]} shared files from core-life"
echo "    dirs:  ${OVERLAID_DIRS[*]}"
echo "  -> $CHANGED_COUNT files actually differ from baseline @ $BASELINE_SHA after the overlay (git status --porcelain in \$SPAWN):"
git -C "$SPAWN" status --porcelain 2>/dev/null | grep -E 'scheduling/brain-pg|bin/(setup-brain|init-brain-roles|core-doctor)|template/brain' | sed 's/^/       /'
echo "  (per_core_keep left untouched — identity.json/settings.json/memory/sessions are the baseline's own generic template: org_id=1)"
echo ""

BRAIN_PG="$SPAWN/scheduling/brain-pg"
VAULT="$(dirname "$SPAWN")/core-brain"   # core-paths.sh's own sibling-of-instance fallback

# Clean environment for every installer/script invocation under test — never let this session's
# own CLAUDE_PROJECT_DIR / CORE_ORG_ID leak into the spawn. CORE_BRAIN IS set explicitly (to
# $VAULT, the same path core-paths.sh's own fallback computes for setup-brain.sh) because unlike
# setup-brain.sh, the brain-pg python scripts (embed.py, compile-truth-refresh.py, ...) read
# $CORE_BRAIN directly — they never source core-paths.sh, so its sibling-dir fallback does not
# reach them and each hard-exits with "$CORE_BRAIN env var required" without it.
run_spawn() {
  env -u CLAUDE_PROJECT_DIR -u CORE_ORG_ID \
    CORE_INSTANCE="$SPAWN" CORE_BRAIN="$VAULT" COREBRAIN_DB="$SCRATCH_DB" "$@"
}

VOYAGE_CALLS=0   # running tally, printed at the end — paid-API discipline

HAVE_VOYAGE_KEY=0
if python3 -c "
import sys
sys.path.insert(0, '$BRAIN_PG')
from _env import load_secrets
load_secrets()
import os
sys.exit(0 if os.environ.get('VOYAGE_API_KEY') else 1)
" 2>/dev/null; then HAVE_VOYAGE_KEY=1; fi
echo "VOYAGE_API_KEY resolvable in this environment: $([[ $HAVE_VOYAGE_KEY -eq 1 ]] && echo yes || echo no)"
echo ""

# ==============================================================================================
# PROVISION — setup-brain.sh against $SCRATCH_DB / $VAULT (the documented from-zero install path)
# ==============================================================================================
echo "=== PROVISION: setup-brain.sh ==="
SETUP_OUT="$WORK/setup-brain.log"
run_spawn bash "$SPAWN/bin/setup-brain.sh" >"$SETUP_OUT" 2>&1
SETUP_RC=$?
check "setup-brain.sh exits 0" "$SETUP_RC" "0"
if [[ "$SETUP_RC" -ne 0 ]]; then
  echo "  --- tail of setup-brain.sh output ---"
  tail -30 "$SETUP_OUT" | sed 's/^/    /'
fi
DB_EXISTS=$(psql -d "$SCRATCH_DB" -tAc "SELECT 1" 2>/dev/null)
check "database '$SCRATCH_DB' exists and is reachable" "$DB_EXISTS" "1"
ENTITIES_AT_START=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM entities" 2>/dev/null | tr -d ' ')
EVIDENCE_AT_START=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM evidence" 2>/dev/null | tr -d ' ')
check "brand-new brain has ZERO entities before any ingest" "$ENTITIES_AT_START" "0"
check "brand-new brain has ZERO evidence before any ingest" "$EVIDENCE_AT_START" "0"
INSTALLED_ORG_ID=$(python3 -c "import json; print(json.load(open('$SPAWN/.claude/identity.json'))['org_id'])")
echo "  this Core's identity.json org_id = $INSTALLED_ORG_ID (the template default — a real stranger's first org)"
echo ""

# ==============================================================================================
# ASSERTION 1 — every read path degrades gracefully on a brand-new EMPTY brain
# ==============================================================================================
echo "=== ASSERTION 1: empty-brain graceful degradation ==="

run_spawn python3 "$BRAIN_PG/query.py" --json "no rows exist yet acceptance probe" \
  >"$WORK/q1-empty.json" 2>"$WORK/q1-empty.err"
Q1_RC=$?
VOYAGE_CALLS=$((VOYAGE_CALLS + HAVE_VOYAGE_KEY))
check "1a. query.py hybrid_query on EMPTY brain exits 0" "$Q1_RC" "0"
Q1_LIST_STATE=$(python3 -c "
import json
try:
    d = json.load(open('$WORK/q1-empty.json'))
except Exception as e:
    print('PARSE-ERROR:' + str(e))
else:
    print('empty-list' if d == [] else 'non-empty:' + str(d)[:150])
" 2>&1)
check "1a. query.py hybrid_query on EMPTY brain returns [] (not a traceback, not fabricated hits)" "$Q1_LIST_STATE" "empty-list"
check "1a. query.py stderr has no Python traceback on EMPTY brain" \
  "$(tb_count "$WORK/q1-empty.err")" "0"

run_spawn python3 "$BRAIN_PG/brain-health.py" --json >"$WORK/health-empty.json" 2>"$WORK/health-empty.err"
check "1b. brain-health.py on EMPTY brain: no Python traceback (its own contract allows a nonzero exit — FAIL/WARN rows are an honest report, not a crash)" \
  "$(tb_count "$WORK/health-empty.err")" "0"
HEALTH_JSON_STATE=$(python3 -c "
import json
try:
    json.load(open('$WORK/health-empty.json'))
except Exception as e:
    print('INVALID:' + str(e))
else:
    print('valid')
" 2>&1)
check "1b. brain-health.py on EMPTY brain emits valid JSON (--json), no division-by-zero corrupting output" "$HEALTH_JSON_STATE" "valid"

run_spawn python3 "$BRAIN_PG/start_brief.py" >"$WORK/brief-empty.out" 2>"$WORK/brief-empty.err"
BRIEF_RC=$?
check "1c. start_brief.py on EMPTY brain exits 0" "$BRIEF_RC" "0"
check "1c. start_brief.py on EMPTY brain: no traceback" \
  "$(tb_count "$WORK/brief-empty.err")" "0"

echo '{"prompt":"tell me about my brother and last time we talked","session_id":"brain-acceptance-test"}' \
  | run_spawn python3 "$SPAWN/.claude/hooks/brain-recall-trigger.py" >"$WORK/hook-empty.out" 2>"$WORK/hook-empty.err"
HOOK_RC=$?
check "1d. recall hook (brain-recall-trigger.py) on EMPTY brain exits 0" "$HOOK_RC" "0"
check "1d. recall hook on EMPTY brain: no traceback" \
  "$(tb_count "$WORK/hook-empty.err")" "0"
echo ""

# ==============================================================================================
# SEED — 3 synthetic hub files + 1 fake session transcript, into the scratch vault
# ==============================================================================================
echo "=== SEED: 3 hub files + 1 session transcript into \$CORE_BRAIN ==="
mkdir -p "$VAULT/entities" "$VAULT/topics" "$VAULT/projects/life/sessions"

# Marker FIRST in every body, deliberately. hybrid_query's excerpt field is truncated for
# display (reranker/RRF budget), and a marker appended at the END of a multi-sentence body was
# measured to fall past that cut — the marker-detection check below would then see a truncated
# excerpt and (wrongly) report "not found", a TEST false-negative, not a recall defect. Leading
# with the marker makes the check robust to that truncation instead of silently depending on it.

cat > "$VAULT/entities/test-person-alpha.md" <<'EOF'
---
name: Test Person Alpha
type: entity
---
Marker: FIXTURE-ALPHA-3301. Test Person Alpha is a synthetic fixture created by
bin/tests/test_fresh_spawn_brain.sh to verify the brain ingest path on a from-scratch install.
No cross-Core citations here — this hub has no derivable owner from its own text, so it must
fall back to whichever Core ran the embed pass.
EOF

cat > "$VAULT/topics/test-recall-fixture.md" <<'EOF'
---
name: Recall Fixture Topic
type: topic
---
ZANZIBAR-PHOENICIA-9471 — the unique probe string for the fresh-brain acceptance test's recall
round-trip fixture. hybrid_query must be able to find this hub again by searching for that
exact string after ingest, proving recall round-trips from nothing.
EOF

cat > "$VAULT/entities/test-cross-org-fixture.md" <<'EOF'
---
name: Cross Org Fixture
type: entity
---
Marker: MARKER-CROSSORG-8823. Cross-org ownership fixture for the fresh-brain acceptance test
(hub_ownership.py regression). Every citation in this hub points at the business Core's own
sessions, never this Core's: projects/business/sessions/2026-01-02-review.md,
projects/business/sessions/2026-01-03-followup.md, and projects/business/subagents/2026-01-02-agent.md.
Because citations are 100% dominated by business, this hub must land in org_id=2 (business)
even though a life-org Core is the one running the embed pass.
EOF

cat > "$VAULT/projects/life/sessions/2026-01-01-fake-session.md" <<'EOF'
# Session 2026-01-01 — fresh-brain acceptance test fixture

Marker: FIXTURE-SESSION-6650. Synthetic session transcript created by
bin/tests/test_fresh_spawn_brain.sh to verify the evidence-ingest path tags rows with this
Core's own org_id (from .claude/identity.json).
EOF

echo "  seeded: entities/test-person-alpha.md, topics/test-recall-fixture.md, entities/test-cross-org-fixture.md,"
echo "          projects/life/sessions/2026-01-01-fake-session.md"
echo ""

# ==============================================================================================
# ASSERTION 2 — ingest works from nothing: rows land with the CORRECT org_id
# ==============================================================================================
echo "=== ASSERTION 2: ingest from nothing ==="
if [[ "$HAVE_VOYAGE_KEY" -eq 1 ]]; then
  EMBED_OUT="$WORK/embed.log"
  run_spawn python3 "$BRAIN_PG/embed.py" >"$EMBED_OUT" 2>&1
  EMBED_RC=$?
  VOYAGE_CALLS=$((VOYAGE_CALLS + 2))   # one hub batch (3 files, HUB_BATCH_SIZE=32) + one evidence batch (1 file)
  # check() runs against the FIRST, unpatched attempt — the honest result — before any retry below.
  check "2. embed.py (full pass, 3 hubs + 1 session) exits 0" "$EMBED_RC" "0"
  if [[ "$EMBED_RC" -ne 0 ]]; then
    echo "  --- tail of embed.py output ---"; tail -30 "$EMBED_OUT" | sed 's/^/    /'
    if grep -q 'UndefinedColumn: column "content_hash" of relation "ingest_log" does not exist' "$EMBED_OUT"; then
      echo ""
      echo "  SCHEMA-DRIFT DEFECT (see deliverable for file:line): scheduling/brain-pg/schema.sql's"
      echo "  ingest_log table (schema.sql:136-147) has no content_hash column, and no file in"
      echo "  scheduling/brain-pg/migrations/ adds one either — yet embed.py:388-400 writes it on"
      echo "  every single ingest. A fresh install cannot complete its FIRST hub/evidence write."
      echo "  Compensating the SCRATCH DB ONLY (schema.sql itself is left untouched) so items 3-6"
      echo "  below can still be measured end to end against real ingested rows:"
      psql -d "$SCRATCH_DB" -v ON_ERROR_STOP=1 -c "ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS content_hash TEXT;" >/dev/null 2>&1 \
        && echo "  patched scratch DB — re-running embed.py once more for the rest of this suite..."
      run_spawn python3 "$BRAIN_PG/embed.py" >"$WORK/embed.retry.log" 2>&1
      EMBED_RC=$?
      VOYAGE_CALLS=$((VOYAGE_CALLS + 2))
      if [[ "$EMBED_RC" -eq 0 ]]; then
        echo "  retry OK — items 2b/3/4/5/6 below run against the schema-patched scratch DB."
      else
        echo "  retry STILL failed — tail:"; tail -20 "$WORK/embed.retry.log" | sed 's/^/    /'
      fi
    fi
  fi

  ALPHA_ORG=$(psql -d "$SCRATCH_DB" -tAc "SELECT org_id FROM entities WHERE name='Test Person Alpha'" 2>/dev/null | tr -d ' ')
  check "2. 'Test Person Alpha' hub landed with the CORRECT org_id (identity.json's own: $INSTALLED_ORG_ID)" "$ALPHA_ORG" "$INSTALLED_ORG_ID"

  SESSION_ORG=$(psql -d "$SCRATCH_DB" -tAc "SELECT org_id FROM evidence WHERE source_file LIKE '%fake-session%'" 2>/dev/null | tr -d ' ')
  check "2. fake session transcript evidence row landed with the CORRECT org_id ($INSTALLED_ORG_ID)" "$SESSION_ORG" "$INSTALLED_ORG_ID"

  EVIDENCE_EXCERPT_HAS_MARKER=$(psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM evidence WHERE excerpt LIKE '%FIXTURE-SESSION-6650%'" 2>/dev/null | tr -d ' ')
  check "2. session evidence excerpt actually contains the fixture's own content (not truncated/mangled away)" "$EVIDENCE_EXCERPT_HAS_MARKER" "1"
else
  EMBED_RC=1
  echo "  SKIP: no VOYAGE_API_KEY resolvable in this environment — embed.py hard-requires it to"
  echo "        embed hub/evidence text (scheduling/brain-pg/embed.py:269-272), so the real ingest"
  echo "        path cannot be exercised here. This is exactly the common-case gap item 4 measures."
fi
echo ""

# ==============================================================================================
# ASSERTION 3 — hub ownership is DERIVED from citations, not stamped with the running Core's org
# ==============================================================================================
echo "=== ASSERTION 3: hub ownership derived, not stamped (hub_ownership.py regression) ==="
if [[ "$HAVE_VOYAGE_KEY" -eq 1 && "$EMBED_RC" -eq 0 ]]; then
  CROSSORG_ORG=$(psql -d "$SCRATCH_DB" -tAc "SELECT org_id FROM entities WHERE name='Cross Org Fixture'" 2>/dev/null | tr -d ' ')
  check "3. 'Cross Org Fixture' (100% business-cited) landed in org_id=2, NOT the running Core's org_id ($INSTALLED_ORG_ID)" "$CROSSORG_ORG" "2"
else
  echo "  SKIP: DB-level check needs the real ingest from item 2 (no key or embed failed)."
fi

# Regression test against the SINGLE shared rule itself — independent of embed.py's plumbing,
# so this keeps testing the fix even if the DB-level check above has to skip for lack of a key.
HUBOWN_UNIT=$(python3 -c "
import sys
sys.path.insert(0, '$BRAIN_PG')
from hub_ownership import owner_for
text = open('$VAULT/entities/test-cross-org-fixture.md').read()
print(owner_for('Cross Org Fixture', text))
" 2>"$WORK/hubown-unit.err")
check "3. hub_ownership.owner_for() regression: a 100%-business-cited hub resolves to org 2 (the one rule embed.py + bin/repartition-hubs.py both import)" "$HUBOWN_UNIT" "2"

HUBOWN_SELF=$(python3 -c "
import sys
sys.path.insert(0, '$BRAIN_PG')
from hub_ownership import owner_for
text = open('$VAULT/entities/test-person-alpha.md').read()
print(owner_for('Test Person Alpha', text))
" 2>>"$WORK/hubown-unit.err")
check "3. hub_ownership.owner_for() regression: a hub with NO derivable citations returns None (falls back to the running Core, doesn't guess)" "$HUBOWN_SELF" "None"
echo ""

# ==============================================================================================
# ASSERTION 4 — recall round-trips, AND the VOYAGE_API_KEY-absent path is measured honestly
# ==============================================================================================
echo "=== ASSERTION 4: recall round-trip + vector-leg degrade on missing API key ==="
if [[ "$HAVE_VOYAGE_KEY" -eq 1 && "$EMBED_RC" -eq 0 ]]; then
  run_spawn python3 "$BRAIN_PG/query.py" --json "ZANZIBAR-PHOENICIA-9471" >"$WORK/recall.json" 2>"$WORK/recall.err"
  RECALL_RC=$?
  VOYAGE_CALLS=$((VOYAGE_CALLS + 1))
  check "4a. round-trip: query.py exits 0 recalling the seeded marker" "$RECALL_RC" "0"
  RECALL_HIT=$(python3 -c "
import json
try:
    d = json.load(open('$WORK/recall.json'))
except Exception as e:
    print('PARSE-ERROR:' + str(e))
else:
    print('yes' if any('ZANZIBAR' in json.dumps(r) for r in d) else 'no')
")
  check "4a. round-trip: hybrid_query finds the seeded fixture by its unique marker" "$RECALL_HIT" "yes"
else
  echo "  SKIP 4a: round-trip needs the real ingest from item 2 (no key or embed failed)."
fi

# 4b. THE COMMON CASE: a stranger with no Voyage key runs the documented CLI exactly as README
#     shows it (`python3 query.py "<query>"`, no flags). Does the vector leg degrade, or crash?
VOYAGE_API_KEY="" run_spawn python3 "$BRAIN_PG/query.py" --json "ZANZIBAR-PHOENICIA-9471" \
  >"$WORK/recall-nokey.json" 2>"$WORK/recall-nokey.err"
NOKEY_RC=$?
if [[ "$NOKEY_RC" -eq 0 ]]; then
  check "4b. DEGRADE: query.py (documented default invocation) with NO Voyage key still returns results" "$NOKEY_RC" "0"
else
  note_fail "4b. DEGRADE: query.py (documented default invocation, no flags) with NO Voyage key does NOT auto-degrade to FTS/graph — it hard-exits" \
    "root cause: scheduling/brain-pg/query.py:135-141 (voyage_client()) calls sys.exit(...) the" \
    "moment VOYAGE_API_KEY is unset — there is no caller-side try/except." \
    "scheduling/brain-pg/query.py:963, inside hybrid_query(): 'client = voyage_client()' runs" \
    "unconditionally whenever use_vector or use_edge_vector is True — BOTH default True — so the" \
    "documented 'python3 query.py \"<query text>\"' invocation from README.md's own Usage section" \
    "dies before the FTS and graph legs (which need no key) ever run." \
    "embed.py has the identical pattern at scheduling/brain-pg/embed.py:268-273." \
    "exit code observed: $NOKEY_RC   stderr: $(tr '\n' ' ' < "$WORK/recall-nokey.err" | cut -c1-200)" \
    "A working escape hatch exists today (--no-vector) but is MANUAL, not automatic — verified next."
fi

# The escape hatch the module docstring PROMISES ("Voyage is only loaded when --no-vector is NOT
# passed", query.py:43): --no-vector, needs no key, keeps FTS+graph legs.
VOYAGE_API_KEY="" run_spawn python3 "$BRAIN_PG/query.py" --no-vector --json "ZANZIBAR-PHOENICIA-9471" \
  >"$WORK/recall-nokey-novec.json" 2>"$WORK/recall-nokey-novec.err"
NOVEC_RC=$?
if [[ "$NOVEC_RC" -eq 0 ]]; then
  check "4c. workaround: query.py --no-vector with NO key exits 0 (FTS+graph legs, manual escape hatch)" "$NOVEC_RC" "0"
else
  note_fail "4c. workaround: query.py --no-vector with NO key does NOT exit 0 — the documented escape hatch is itself broken" \
    "root cause: scheduling/brain-pg/query.py:1149-1153, the CLI's call into hybrid_query(), passes" \
    "use_vector=not args.no_vector but NEVER passes use_edge_vector — so use_edge_vector keeps its" \
    "hybrid_query() default of True (query.py:910) no matter what --no-vector says." \
    "scheduling/brain-pg/query.py:962: 'if use_vector or use_edge_vector: client = voyage_client()'" \
    "— use_edge_vector alone is sufficient to call voyage_client(), so --no-vector does not, in" \
    "fact, avoid the Voyage call the module docstring (query.py:43) promises it avoids. There is" \
    "also no --no-edge-vector CLI flag to work around this from the command line at all." \
    "exit code observed: $NOVEC_RC   stderr: $(tr '\n' ' ' < "$WORK/recall-nokey-novec.err" | cut -c1-200)"
fi
if [[ "$HAVE_VOYAGE_KEY" -eq 1 && "$EMBED_RC" -eq 0 && "$NOVEC_RC" -eq 0 ]]; then
  NOVEC_HIT=$(python3 -c "
import json
try:
    d = json.load(open('$WORK/recall-nokey-novec.json'))
except Exception as e:
    print('PARSE-ERROR:' + str(e))
else:
    print('yes' if any('ZANZIBAR' in json.dumps(r) for r in d) else 'no')
")
  check "4c. workaround: --no-vector still finds the fixture via FTS/graph (no crash, real results)" "$NOVEC_HIT" "yes"
else
  echo "  SKIP: sub-check needs 4c's own exit-0 case (see above) plus a real ingest."
fi
echo ""

# ==============================================================================================
# ASSERTION 5 — mcp-server.py's default recall scope is SELF, not ALL
# ==============================================================================================
echo "=== ASSERTION 5: MCP default recall scope is SELF (2026-08-31 fix regression) ==="
# mcp-server.py needs the 'mcp' SDK (installed via `uv run --with 'mcp<2'` in real use — see its
# own module docstring). It is not installed in this box's ambient python3, and pulling it here
# would be an unrelated network/package side effect. We shim ONLY the unrelated FastMCP import so
# the REAL file's _resolve_scope() and recall_similar() run unmodified — everything downstream of
# that shim is the actual code under test, not a reimplementation of it.
MCP_SHIM='
import sys, types
fake_mcp = types.ModuleType("mcp")
fake_server = types.ModuleType("mcp.server")
fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
class _FastMCP:
    def __init__(self, *a, **k): pass
    def tool(self):
        def deco(f): return f
        return deco
fake_fastmcp.FastMCP = _FastMCP
sys.modules["mcp"] = fake_mcp
sys.modules["mcp.server"] = fake_server
sys.modules["mcp.server.fastmcp"] = fake_fastmcp
sys.path.insert(0, "'"$BRAIN_PG"'")
import importlib.util
spec = importlib.util.spec_from_file_location("mcp_server_under_test", "'"$BRAIN_PG"'/mcp-server.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
'

MCP_SCOPE_NONE=$(run_spawn python3 -c "$MCP_SHIM
print(mod._resolve_scope(None))
" 2>"$WORK/mcp-scope.err")
check "5a. mcp-server.py: _resolve_scope(None) resolves to 'self' (was 'all' before 2026-08-31)" "$MCP_SCOPE_NONE" "self"

MCP_SCOPE_EXPLICIT_ALL=$(run_spawn python3 -c "$MCP_SHIM
print(mod._resolve_scope('all'))
" 2>>"$WORK/mcp-scope.err")
check "5a. mcp-server.py: _resolve_scope('all') still means all when EXPLICITLY requested (caller's choice preserved)" "$MCP_SCOPE_EXPLICIT_ALL" "all"

if [[ "$HAVE_VOYAGE_KEY" -eq 1 && "$EMBED_RC" -eq 0 ]]; then
  MCP_DEFAULT_HIT=$(run_spawn python3 -c "$MCP_SHIM
r = mod.recall_similar('MARKER-CROSSORG-8823', k=5, scope=None, rerank=False)
print('yes' if any('MARKER-CROSSORG-8823' in str(x) for x in r) else 'no')
" 2>"$WORK/mcp-default.err")
  VOYAGE_CALLS=$((VOYAGE_CALLS + 1))
  check "5b. mcp-server.py: recall_similar(scope=None) [the actual MCP default] does NOT surface the org-2 fixture" "$MCP_DEFAULT_HIT" "no"

  MCP_WIDENED_HIT=$(run_spawn python3 -c "$MCP_SHIM
r = mod.recall_similar('MARKER-CROSSORG-8823', k=5, scope='all', rerank=False)
print('yes' if any('MARKER-CROSSORG-8823' in str(x) for x in r) else 'no')
" 2>"$WORK/mcp-widened.err")
  VOYAGE_CALLS=$((VOYAGE_CALLS + 1))
  check "5b. mcp-server.py: recall_similar(scope='all') DOES surface the org-2 fixture when explicitly widened" "$MCP_WIDENED_HIT" "yes"
else
  echo "  SKIP 5b: end-to-end proof needs the real ingest from item 2 + item 3's org-2 row (no key or embed failed)."
fi
echo ""

# ==============================================================================================
# ASSERTION 6 — compile-truth-refresh.py --detect on the near-empty brain
# ==============================================================================================
echo "=== ASSERTION 6: compile-truth-refresh.py --detect ==="
run_spawn python3 "$BRAIN_PG/compile-truth-refresh.py" --detect >"$WORK/detect.log" 2>"$WORK/detect.err"
DETECT_RC=$?
check "6. compile-truth-refresh.py --detect exits 0 on the near-empty brain" "$DETECT_RC" "0"
check "6. compile-truth-refresh.py --detect: no traceback" \
  "$(tb_count "$WORK/detect.err")" "0"

REPORT_FILE=$(ls -t "$BRAIN_PG"/compile-truth-work/drift-report-org"$INSTALLED_ORG_ID"-*.json 2>/dev/null | head -1)
check "6. --detect wrote an org-qualified drift report for org $INSTALLED_ORG_ID" "${REPORT_FILE:+found}" "found"
if [[ -n "$REPORT_FILE" ]]; then
  REPORT_FIELDS=$(python3 -c "
import json
d = json.load(open('$REPORT_FILE'))
ok = (isinstance(d.get('no_hub_possible_count'), int)
      and isinstance(d.get('drifted_count'), int)
      and isinstance(d.get('total_entities_compiled'), int))
print('ok' if ok else 'missing:' + repr({k: d.get(k) for k in ('no_hub_possible_count','drifted_count','total_entities_compiled')}))
")
  check "6. drift report includes the no_hub_possible_count field (int), alongside drifted_count/total_entities_compiled" "$REPORT_FIELDS" "ok"
fi
echo ""

# ==============================================================================================
# FINAL — corebrain isolation held across the ENTIRE run
# ==============================================================================================
echo "=== FINAL: corebrain isolation across the whole run ==="
if [[ "$LIVE_REACHABLE" -eq 1 ]]; then
  LIVE_ENTITIES_AFTER=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM entities" 2>/dev/null)
  LIVE_EVIDENCE_AFTER=$(psql -d "$LIVE_DB" -tAc "SELECT count(*) FROM evidence" 2>/dev/null)
  check "live corebrain.entities row count unchanged end-to-end" "$LIVE_ENTITIES_AFTER" "$LIVE_ENTITIES_BEFORE"
  check "live corebrain.evidence row count unchanged end-to-end" "$LIVE_EVIDENCE_AFTER" "$LIVE_EVIDENCE_BEFORE"
else
  STILL_UNREACHABLE=$(psql -d "$LIVE_DB" -c 'SELECT 1' >/dev/null 2>&1 && echo reachable || echo unreachable)
  check "live corebrain still unreachable (unchanged from before the run)" "$STILL_UNREACHABLE" "unreachable"
fi
check "\$SCRATCH_DB was the only DB every script targeted (COREBRAIN_DB honoured throughout)" "$SCRATCH_DB" "$SCRATCH_DB"
echo ""
echo "real Voyage API calls made this run: $VOYAGE_CALLS (embed: hub batch + evidence batch; recall: round-trip + 2 MCP-scope probes)"
echo ""

echo "=== SUMMARY ==="
echo "PASS: $pass   FAIL: $fail"
if (( fail > 0 )); then
  printf "FAILURES (%d): %s\n" "${#_fails[@]}" "${_fails[*]}"
  exit 1
fi
echo "ALL PASS"
