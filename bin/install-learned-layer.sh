#!/usr/bin/env bash
# install-learned-layer.sh — make the Learned Workflow Layer live in THIS Core.
#
# Idempotent. Safe to re-run. Part of the shared baseline (nicknur7/core-agent), so
# every fork ships with it; running it is what turns the layer ON for a Core.
#
# What it does:
#   1. Applies the additive DB schema (learned_contracts table + corpus columns).
#   2. Registers the 4 learned hooks in .claude/settings.json (skips if present).
#   3. Creates the empty learned-layer state files.
#   4. Verifies and prints status.
#
# The 3 deterministic blockers (validator / recallguard / stopguard) are live the
# moment this finishes — they are regex-only and need NO corpus. The classifier
# stays dormant (fails open) until this Core synthesizes its own contracts from
# accumulated corrections. See docs/learned-layer-setup.md.
#
# Kill-switch at any time:  export LEARNED_LAYER=0
set -uo pipefail

CORE_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SETTINGS="$CORE_DIR/.claude/settings.json"
BASE_SCHEMA="$CORE_DIR/scheduling/claude-si/schema.sql"
SCHEMA="$CORE_DIR/scheduling/claude-si/schema-learned.sql"
# schema-phase2.sql (artifacts + artifact_event/outcome/utility) and the core-si
# automation schema were never applied by ANY installer — verified 2026-07-27 by
# running the documented install on a clean clone. Without them the SI spine has no
# artifact tables, so the loop cannot record or measure anything it generates.
PHASE2_SCHEMA="$CORE_DIR/scheduling/claude-si/schema-phase2.sql"
CORESI_SCHEMA="$CORE_DIR/scheduling/core-si/schema-si-automation.sql"
STATE="$CORE_DIR/.claude/state"
DB="${COREBRAIN_DB:-corebrain}"

# This installer previously ran with `set -uo pipefail` (no -e) and printed
# "the 3 deterministic blockers are LIVE" unconditionally — including on a database
# where nothing had been created. Failures are now tracked and gate the final banner.
FAILED=0
fail() { echo "[err]  $*" >&2; FAILED=1; }

echo "== Learned Workflow Layer — installer =="
echo "Core: $CORE_DIR"
echo ""

# ---- 1. database schema (additive + idempotent) -----------------------------
if [[ ! -f "$SCHEMA" ]]; then
  fail "schema not found: $SCHEMA — is scheduling/claude-si/ synced?"
elif ! command -v psql >/dev/null 2>&1; then
  fail "psql not found — run bin/install-deps.sh, then re-run this."
elif ! psql -d "$DB" -c 'SELECT 1' >/dev/null 2>&1; then
  fail "Postgres '$DB' unreachable — run bin/setup-brain.sh first, then re-run."
else
  # Base substrate (pattern_observations + indexes/policies) — schema-learned.sql
  # ALTERs pattern_observations, so the base table must exist first. schema.sql is
  # idempotent (CREATE ... IF NOT EXISTS); a no-op on Cores where the SI loop predates
  # this layer (e.g. life), and the missing dependency that left forks' classifiers
  # inert (learned_contracts created, ALTER failed). 2026-06-18 fix (a fork's report, 6/09).
  if [[ -f "$BASE_SCHEMA" ]] && ! psql -d "$DB" -v ON_ERROR_STOP=1 -f "$BASE_SCHEMA" >/dev/null 2>&1; then
    fail "base schema apply failed — inspect: psql -d $DB -f $BASE_SCHEMA"
  fi
  if psql -d "$DB" -v ON_ERROR_STOP=1 -f "$SCHEMA" >/dev/null 2>&1; then
    echo "[ok]   schema applied to '$DB' (pattern_observations base + learned_contracts + corpus columns)"
  else
    fail "schema apply failed — inspect: psql -d $DB -f $SCHEMA"
  fi
  # Phase-2 artifact tables — the SI spine writes si_artifacts here and records
  # artifact_event/outcome/utility for fitness measurement.
  if [[ -f "$PHASE2_SCHEMA" ]]; then
    if psql -d "$DB" -v ON_ERROR_STOP=1 -f "$PHASE2_SCHEMA" >/dev/null 2>&1; then
      echo "[ok]   phase-2 artifact schema applied (artifacts, artifact_event/outcome/utility)"
    else
      fail "phase-2 schema apply failed — inspect: psql -d $DB -f $PHASE2_SCHEMA"
    fi
  else
    fail "phase-2 schema missing: $PHASE2_SCHEMA"
  fi
  # core-si automation tables (fix approvals + trusted fixes).
  if [[ -f "$CORESI_SCHEMA" ]]; then
    if psql -d "$DB" -v ON_ERROR_STOP=1 -f "$CORESI_SCHEMA" >/dev/null 2>&1; then
      echo "[ok]   core-si automation schema applied"
    else
      fail "core-si automation schema apply failed — inspect: psql -d $DB -f $CORESI_SCHEMA"
    fi
  fi
  # Brain migrations that harden the tables we just created (e.g.
  # 2026-07-19-learned-contracts-rls.sql) are DEFERRED by setup-brain.sh, because at
  # that point learned_contracts did not exist yet. Now it does, so pick them up.
  MIGRATOR="$CORE_DIR/bin/run-migrations.sh"
  if [[ -x "$MIGRATOR" || -f "$MIGRATOR" ]]; then
    if COREBRAIN_DB="$DB" bash "$MIGRATOR" --ensure >/dev/null 2>&1; then
      echo "[ok]   deferred brain migrations reconciled (learned-layer RLS)"
    else
      fail "deferred migration reconcile failed — inspect: COREBRAIN_DB=$DB bash $MIGRATOR --ensure"
    fi
  fi
fi

# ---- 2. register the 4 learned hooks (idempotent, backs up settings) --------
if [[ ! -f "$SETTINGS" ]]; then
  echo "[err]  settings.json not found at $SETTINGS" >&2
else
  python3 - "$SETTINGS" <<'PY'
import json, sys, shutil
p = sys.argv[1]
s = json.load(open(p))
h = s.setdefault('hooks', {})
def ensure(event, matcher, cmd, timeout=5):
    groups = h.setdefault(event, [])
    g = next((g for g in groups if g.get('matcher', '') == matcher), None)
    if g is None:
        g = {'matcher': matcher, 'hooks': []}; groups.append(g)
    if any(cmd in hk.get('command', '') for hk in g['hooks']):
        return False
    entry = {'type': 'command', 'command': cmd, 'timeout': timeout}
    # keep stop-hook.sh last in the Stop chain
    idx = len(g['hooks'])
    for i, hk in enumerate(g['hooks']):
        if 'stop-hook.sh' in hk.get('command', ''):
            idx = i; break
    g['hooks'].insert(idx, entry); return True
Q = '"$CLAUDE_PROJECT_DIR/.claude/hooks/%s"'
changed = []
if ensure('PreToolUse', 'Write|Edit|MultiEdit|NotebookEdit', Q % 'learned-stopguard.sh'): changed.append('stopguard')
if ensure('UserPromptSubmit', '', Q % 'learned-classifier.sh'): changed.append('classifier')
if ensure('Stop', '', Q % 'learned-validator.sh'): changed.append('validator')
# learned-recallguard is RETIRED in bin/hook-registry.json (replaced by recall-first-gate at
# PreToolUse — it blocked at Stop, after the reply, which the operator's own policy forbids).
# This installer kept registering it anyway, so a FRESH install wired a hook the registry
# says is dead, and reconcile-hooks then reported drift on a seat that had done nothing wrong.
# Found by a full-repo census the day the repo went public. The registry is the authority;
# an installer that disagrees with it is the bug.
if changed:
    shutil.copy(p, p + '.bak')
    json.dump(s, open(p, 'w'), indent=2); open(p, 'a').write('\n')
    print('[ok]   registered: ' + ', '.join(changed) + '  (backup: settings.json.bak)')
else:
    print('[ok]   all 4 learned hooks already registered')
PY
fi

# ---- 3. state + generalized starter contracts -------------------------------
mkdir -p "$STATE"; touch "$STATE/learned-fires.log"
STARTER="$CORE_DIR/scheduling/claude-si/learned-contracts-starter.json"
SNAPSHOT="$STATE/learned-contracts.json"
# NOTE ON THE TWO STORES, because the old messages here read as claims about the
# database and were not: learned-classifier.py reads the JSON SNAPSHOT
# (.claude/state/learned-contracts.json). The learned_contracts TABLE is the
# canonical store that si_snapshot.py projects into that file. Seeding the starter
# writes the FILE only — which is enough for the classifier to fire on a fresh Core,
# but it is not the same as having rows in the DB. Say which one we mean.
if [[ -f "$SNAPSHOT" ]]; then
  _n=$(python3 -c "import json,sys;d=json.load(open('$SNAPSHOT'));print(len(d) if hasattr(d,'__len__') else 0)" 2>/dev/null || echo '?')
  echo "[ok]   contract snapshot already present (${_n} entries) — left as-is; this Core keeps its own"
elif [[ -f "$STARTER" ]]; then
  cp "$STARTER" "$SNAPSHOT"
  _n=$(python3 -c "import json,sys;d=json.load(open('$SNAPSHOT'));print(len(d) if hasattr(d,'__len__') else 0)" 2>/dev/null || echo '?')
  echo "[ok]   seeded ${_n} generalized starter contracts into the classifier snapshot"
else
  echo "[warn] no starter found — classifier stays dormant until you synthesize your own"
fi
echo "[ok]   state ready ($STATE/learned-fires.log)"

# ---- 3a2. seed starter SKILLS from template/skills (never overwrite this Core's own) ---
# Skills the learned layer promoted on the reference seat, shipped as starters (2026-09-04): a fresh
# Core otherwise starts with only claude-brain and codex-routing-detail and mines the rest over
# weeks. Copy each template skill in ONLY if this Core has no directory of that name.
_TSK="$CORE_DIR/template/skills"
if [[ -d "$_TSK" ]]; then
  _added=0
  for _d in "$_TSK"/*/; do
    _n="$(basename "$_d")"
    if [[ ! -d "$CORE_DIR/.claude/skills/$_n" ]]; then
      mkdir -p "$CORE_DIR/.claude/skills/$_n" && cp -R "$_d". "$CORE_DIR/.claude/skills/$_n/" && _added=$((_added+1))
    fi
  done
  echo "[ok]   starter skills: ${_added} installed from template/skills (existing skills untouched)"
fi

# Scans this Core's recent sessions for corrections (org-scoped insert, no embed/
# no API cost). Gives a fresh Core material to self-tune from sooner instead of
# waiting to accumulate it all live. Best-effort; fail-open.
MINER="$CORE_DIR/scheduling/claude-si/learned-corpus-miner.py"
if [[ -f "$MINER" ]] && command -v psql >/dev/null 2>&1 && psql -d "$DB" -c 'SELECT 1' >/dev/null 2>&1; then
  if python3 "$MINER" --detect >/dev/null 2>&1; then
    echo "[ok]   seeded corpus from recent sessions (org-scoped; grows further each close)"
  else
    echo "[skip] corpus seed skipped (no recent corrections or miner error)"
  fi
else
  echo "[skip] corpus seed skipped (DB unavailable) — corpus grows at session close instead"
fi

# ---- 4. verify --------------------------------------------------------------
# This block used to grep settings.json for hook filenames and call that a
# verification, then print "LIVE" regardless. It never asked the database whether
# any of the work had actually happened. It now does, and the banner is gated on it.
echo ""
echo "== registered learned hooks =="
grep -o 'learned-[a-z]*\.sh' "$SETTINGS" 2>/dev/null | sort -u | sed 's/^/  · /' || echo "  (none — check settings.json)"

echo ""
echo "== verifying against '$DB' =="
REQUIRED_TABLES=(pattern_observations learned_contracts si_artifacts si_projection_state
                 friction_cases artifacts artifact_event artifact_outcome artifact_utility)
if command -v psql >/dev/null 2>&1 && psql -d "$DB" -c 'SELECT 1' >/dev/null 2>&1; then
  for t in "${REQUIRED_TABLES[@]}"; do
    if psql -d "$DB" -tAc "SELECT to_regclass('public.$t') IS NOT NULL" 2>/dev/null | grep -q '^t$'; then
      printf '  ✓ %s\n' "$t"
    else
      printf '  ✗ %s  MISSING\n' "$t"; FAILED=1
    fi
  done
  # A contract count of zero is not a failure (a fresh Core legitimately has none
  # until it mines its own), but claiming "the classifier is active" when the table
  # is empty is. Report the real number instead of asserting.
  CONTRACTS=$(psql -d "$DB" -tAc "SELECT count(*) FROM learned_contracts" 2>/dev/null | tr -d ' ')
  OBS=$(psql -d "$DB" -tAc "SELECT count(*) FROM pattern_observations" 2>/dev/null | tr -d ' ')
  echo "  · learned_contracts rows (DB, canonical): ${CONTRACTS:-?}"
  echo "  · pattern_observations rows (DB, corpus):  ${OBS:-?}"
  if [[ -f "$SNAPSHOT" ]]; then
    echo "  · classifier snapshot (what actually fires): $SNAPSHOT"
  else
    echo "  ✗ classifier snapshot MISSING — the classifier has nothing to enforce"; FAILED=1
  fi
else
  echo "  ✗ cannot reach '$DB' — nothing verified"; FAILED=1
fi

echo ""
if [[ $FAILED -ne 0 ]]; then
  echo "INSTALL INCOMPLETE — one or more steps above failed."
  echo "The learned layer is NOT live. Fix the errors above and re-run:"
  echo "  bash bin/setup-brain.sh && bash bin/install-learned-layer.sh"
  exit 1
fi
echo "Done. The 3 deterministic blockers are LIVE and the schema is verified present."
if [[ "${CONTRACTS:-0}" == "0" ]]; then
  echo "No contracts yet — this Core writes its own as you correct it."
  echo "Re-synthesize once corrections accumulate:"
  echo "  scheduling/claude-si/learned-corpus-miner.py + learned-resynth.py"
else
  echo "$CONTRACTS contract(s) active. Re-synthesize as corrections accumulate:"
  echo "  scheduling/claude-si/learned-corpus-miner.py + learned-resynth.py"
fi
echo "Disable the whole layer anytime:  export LEARNED_LAYER=0"
