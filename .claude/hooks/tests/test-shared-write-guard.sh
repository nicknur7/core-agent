#!/usr/bin/env bash
# test-shared-write-guard.sh — harness for shared-write-guard.py
# Exits 0 if all pass, 1 if any fail.
set -uo pipefail
export CORE_HOOKLOG_OFF=1

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$TESTS_DIR" in
  */.claude/hooks/tests) HOOKS_DIR="$(cd "$TESTS_DIR/.." && pwd)" ;;
  *)                     HOOKS_DIR="$(cd "$TESTS_DIR/../../.claude/hooks" && pwd)" ;;
esac
LIFE="$(cd "$HOOKS_DIR/../.." && pwd)"          # the LOCAL instance running the test
GUARD="$HOOKS_DIR/shared-write-guard.py"

# Role-aware expectation for the LOCAL instance editing a SHARED file.
# The guard's behavior depends on identity.hook_profile.role: the WRITER may edit
# shared code (exit 0); a PULLER is blocked (exit 2, it'd be clobbered on next pull).
# The harness used to hardcode 0, assuming it always ran in life (the writer) — so it
# FALSE-FAILED in every puller's CI (school/business/finance/ops: T01/T02 got 2, not 0).
# Derive the expectation from the local role instead. (2026-07-11 CI-remediation.)
LOCAL_ROLE="$(python3 -c "import json;print((json.load(open('$LIFE/.claude/identity.json')).get('hook_profile') or {}).get('role','writer'))" 2>/dev/null || echo writer)"
if [[ "$LOCAL_ROLE" == "writer" ]]; then EXP_LOCAL_SHARED=0; else EXP_LOCAL_SHARED=2; fi

PASS=0; FAIL=0

# Build a temp PEER instance: real manifest, but identity.domain_label=business.
PEER="$(mktemp -d)"
mkdir -p "$PEER/.claude" "$PEER/bin" "$PEER/.claude/hooks" "$PEER/.claude/rules" \
         "$PEER/.claude/agents/sentinel" "$PEER/memory" "$PEER/sessions"
cp "$LIFE/bin/sync-manifest.json" "$PEER/bin/sync-manifest.json"
python3 -c "import json;p='$PEER/.claude/identity.json';json.dump({'domain_label':'business','org_id':2},open(p,'w'))"
# Build a TEMPLATE-SHAPED instance: exactly what a Core spawned from the shipped template
# looks like — hook_profile.role="puller", and NO domain_label (the template carries
# core_slug instead).
#
# This fixture exists because its absence let a real defect ship. The guard read
# identity.domain_label and failed OPEN when it was empty, so every Core created from the
# template could edit any shared hook, rule, bin/ or scheduling/ file with nothing stopping
# it — and the next sync would silently overwrite the edit, because baseline wins. This suite
# was green the entire time, because every case ran against THIS Core, which is the writer and
# is supposed to be allowed. A guard test that only tests the party the guard permits cannot
# fail. (core-business found it on a cold clone of 172a758, 2026-07-28.)
TMPL="$(mktemp -d)"
mkdir -p "$TMPL/.claude/hooks" "$TMPL/.claude/rules" "$TMPL/bin" "$TMPL/memory"
cp "$LIFE/bin/sync-manifest.json" "$TMPL/bin/sync-manifest.json"
python3 -c "import json;p='$TMPL/.claude/identity.json';json.dump({'core_slug':'core','org_id':0,'hook_profile':{'role':'puller'}},open(p,'w'))"
trap 'rm -rf "$PEER" "$TMPL"' EXIT

# run_case <name> <instance_root> <file_path> <expected_rc>
run_case() {
  local name="$1" inst="$2" fp="$3" exp="$4"
  local payload rc
  payload=$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' "$fp")
  rc=$(echo "$payload" | CORE_INSTANCE="$inst" python3 "$GUARD" >/dev/null 2>&1; echo $?)
  if [[ "$rc" == "$exp" ]]; then printf "  PASS  %s\n" "$name"; ((PASS++))||true
  else printf "  FAIL  %s (expected=%s actual=%s)\n" "$name" "$exp" "$rc"; ((FAIL++))||true; fi
}

echo "=== shared-write-guard test harness ==="
# Local instance edits shared → writer passes (0), puller blocked (2). Role-derived.
run_case "T01 local($LOCAL_ROLE) edits shared hook" "$LIFE" "$LIFE/.claude/hooks/say-do-gap.py" "$EXP_LOCAL_SHARED"
run_case "T02 local($LOCAL_ROLE) edits shared rule" "$LIFE" "$LIFE/.claude/rules/privacy.md"    "$EXP_LOCAL_SHARED"
# Peer editing SHARED files → block
run_case "T03 peer edits shared hook"          "$PEER" "$PEER/.claude/hooks/say-do-gap.py" 2
run_case "T04 peer edits shared rule"          "$PEER" "$PEER/.claude/rules/privacy.md"    2
run_case "T05 peer edits shared bin file"      "$PEER" "$PEER/bin/check-self-knowledge.py" 2
# T06: pretooluse-guard.sh was UNFROZEN from per_core_keep 2026-07-10 — the guard now
# baseline-syncs under sentinel-code's human-gated Rule 1 (categorical BLOCK → ASK+confirm).
# As a now-SHARED file it's clobbered by baseline on pull, so a peer editing it locally IS
# the clobber loop → BLOCK (exit 2), same as any shared hook (T03). The per_core_keep
# trust-root "peer may edit its own copy" case is still covered by T10 (sentinel agent,
# which stays per_core_keep). (Was exit 0 while the guard was per_core_keep.)
run_case "T06 peer edits now-shared guard (unfrozen 2026-07-10)" "$PEER" "$PEER/.claude/hooks/pretooluse-guard.sh" 2
# Peer editing PER-CORE-KEEP files → pass
run_case "T07 peer edits memory/ file"         "$PEER" "$PEER/memory/current-state.md"     0
run_case "T08 peer edits settings.json"        "$PEER" "$PEER/.claude/settings.json"       0
run_case "T09 peer edits identity.json"        "$PEER" "$PEER/.claude/identity.json"       0
# T10 covers the per_core_keep "a peer may edit its OWN trust-root reviewer spec" case.
# Uses the FLAT .claude/agents/sentinel.md, which is the path that actually exists — the
# dir-form agents/sentinel/CLAUDE.md was deleted 2026-07-29, two months after the
# native-format migration (decisions-log:2531) converted the agents to flat files and
# recorded that the redundant dirs should follow. The manifest still carries
# `.claude/agents/sentinel/**` in per_core_keep and that is deliberate, not dead weight:
# sentinel-code's Rule 6 audits per_core_keep completeness, so removing the dir patterns
# would trip its own check. A glob matching nothing cannot misfire, and it is defence in
# depth if the dir-form reappears on a peer or fork. T10b keeps that pattern covered.
# (Rule 6, not Rule 1 — Rule 1 defines the trust-root set and gates content plus the
# human confirm; the per_core_keep completeness audit is Rule 6. Corrected after
# sentinel-code caught the mislabel on the push review, since this comment ships to
# every Core.)
run_case "T10 peer edits sentinel agent (flat)" "$PEER" "$PEER/.claude/agents/sentinel.md" 0
run_case "T10b peer edits sentinel dir pattern" "$PEER" "$PEER/.claude/agents/sentinel/CLAUDE.md" 0
run_case "T11 peer edits own session log"      "$PEER" "$PEER/sessions/2026-06-24.md"      0
# Non-write tool / no path → pass
echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | CORE_INSTANCE="$PEER" python3 "$GUARD" >/dev/null 2>&1
[[ $? -eq 0 ]] && { printf "  PASS  T12 non-Write tool ignored\n"; ((PASS++))||true; } || { printf "  FAIL  T12\n"; ((FAIL++))||true; }

# ── TEMPLATE-SHAPED CORE: the case whose absence let the guard ship disabled ──────────
# role="puller", no domain_label. Must be BLOCKED on shared paths and ALLOWED on its own.
run_case "T13 template Core (no domain_label) edits shared hook" "$TMPL" "$TMPL/.claude/hooks/say-do-gap.py" 2
run_case "T14 template Core edits shared rule"                   "$TMPL" "$TMPL/.claude/rules/privacy.md"    2
run_case "T15 template Core edits shared bin file"               "$TMPL" "$TMPL/bin/core-doctor.sh"          2
run_case "T16 template Core edits its OWN memory"                "$TMPL" "$TMPL/memory/about-me.md"          0

# And an identity with NO role and NO usable slug must fail CLOSED, not open. This is the
# direction that was backwards: a guard that cannot establish who it is guarding should not
# permit the thing it guards.
BLANK="$(mktemp -d)"
mkdir -p "$BLANK/.claude/hooks" "$BLANK/bin"
cp "$LIFE/bin/sync-manifest.json" "$BLANK/bin/sync-manifest.json"
python3 -c "import json;json.dump({'org_id':0},open('$BLANK/.claude/identity.json','w'))"
run_case "T17 identity with no role and no slug fails CLOSED"    "$BLANK" "$BLANK/.claude/hooks/say-do-gap.py" 2
rm -rf "$BLANK"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
