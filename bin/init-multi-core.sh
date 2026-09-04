#!/usr/bin/env bash
# init-multi-core.sh — spawn a sibling Core from the baseline template.
#
# Usage:
#   bash bin/init-multi-core.sh <domain> <org_id>
#   bash bin/init-multi-core.sh business 2
#   bash bin/init-multi-core.sh school   3
#
# Creates ~/AI Projects/core-<domain>/, sets identity.json org_id, customizes
# .mcp.json peer slots, strips template scaffold files. Does NOT customize
# CLAUDE.md / README.md — populate those manually to define the Core's voice.
#
# Spec: tasks/specs/spec-self-hosted-cores-2026-05-19.md Phase 10.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <domain> <org_id>" >&2
  echo "Example: $0 business 2" >&2
  exit 1
fi

DOMAIN="$1"
ORG_ID="$2"
PARENT_DIR="${CORE_PARENT_DIR:-$HOME/AI Projects}"
TARGET_DIR="$PARENT_DIR/core-$DOMAIN"
# ONE SOURCE OF TRUTH. This literal used to be independent of bin/sync-manifest.json's
# `baseline_repo`, with no wiring between them (core-business, bus #5873 "D2"). Both happened to
# agree, so nothing broke — but a forker who repoints the manifest at THEIR fork has not repointed
# what this script clones, and their spawned children keep cloning from the original author's repo.
# The manifest is authoritative; the literal survives only as the fallback if that read fails.
BASELINE_REPO="${BASELINE_REPO:-$(jq -r '.baseline_repo // empty' "$(dirname "${BASH_SOURCE[0]:-$0}")/sync-manifest.json" 2>/dev/null)}"
BASELINE_REPO="${BASELINE_REPO:-nicknur7/core-agent}"

if [[ -e "$TARGET_DIR" ]]; then
  echo "init-multi-core: $TARGET_DIR already exists; refusing to overwrite." >&2
  exit 2
fi

if ! [[ "$ORG_ID" =~ ^[0-9]+$ ]]; then
  echo "init-multi-core: org_id must be an integer (got '$ORG_ID')." >&2
  exit 1
fi

echo "[init-multi-core] domain=$DOMAIN  org_id=$ORG_ID  target=$TARGET_DIR"
echo "[init-multi-core] cloning baseline $BASELINE_REPO..."
git clone --quiet "https://github.com/${BASELINE_REPO}.git" "$TARGET_DIR"

cd "$TARGET_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# DROP THE INHERITED ORIGIN. This is the other half of the 2026-06-19 incident.
#
# The clone above leaves origin pointing at the shared baseline. Nothing here ever
# repointed it and docs/SETUP.md never told anyone to, so every spawned Core had
# `origin = nicknur7/core-agent` until a human fixed it by hand. Combined with the bare
# `git push` that used to sit in session-lifecycle.sh, that is exactly how a Core's
# private session state — identity, memory, a third party's email — landed on the
# shared template in commit ea2e780.
#
# Removing origin outright beats setting a guessed one: a missing remote fails
# visibly on the first push, while a wrong remote fails invisibly INTO SOMEONE
# ELSE'S REPO, which is the bug being fixed.
#
# Unconfigured is a normal state, not an error. The marker tells the close hook to
# skip its push quietly with one line instead of erroring at every single close —
# a noisy failure people learn to ignore protects nobody.
# ─────────────────────────────────────────────────────────────────────────────
git remote remove origin 2>/dev/null || true
mkdir -p "$TARGET_DIR/.claude/state"
cat > "$TARGET_DIR/.claude/state/.unconfigured-remote" <<EOF
This Core has no git remote yet, on purpose.

It was cloned from ${BASELINE_REPO}, and keeping that as origin is how a spawned
Core's private state reaches the shared baseline (see 2026-06-19, commit ea2e780).

To finish setup, create your own repo and point at it:

    git remote add origin https://github.com/<you>/<your-core>.git

Until then, session-close will commit locally and skip the push. Shared code still
reaches the baseline the proper way, via bin/sync-to-baseline.sh.

Delete this file once origin is set.
EOF
echo "[init-multi-core] origin REMOVED (was ${BASELINE_REPO}) — set your own before first push:"
echo "[init-multi-core]     git remote add origin https://github.com/<you>/<your-core>.git"

# Update identity.json
IDENTITY="$TARGET_DIR/.claude/identity.json"  # path mirrors $CORE_IDENTITY_JSON for the target instance (not self)
if [[ -f "$IDENTITY" ]] && command -v jq >/dev/null 2>&1; then
  TMP_ID=$(mktemp)
  jq --arg d "$DOMAIN" --argjson o "$ORG_ID" \
    '.org_id = $o | .core_label = $d | .core_slug = $d' \
    "$IDENTITY" > "$TMP_ID"
  mv "$TMP_ID" "$IDENTITY"
  echo "[init-multi-core] identity.json updated: org_id=$ORG_ID, label=$DOMAIN"
else
  echo "[init-multi-core] WARN: identity.json or jq missing — edit $IDENTITY manually." >&2
fi

# Strip template scaffolding (matches Phase 9 of spec-self-hosted-cores)
STRIP=(template examples tests AGENTS.md CHANGELOG.md CHANGELOG.archive.md
       CODE_OF_CONDUCT.md CONTRIBUTING.md INSTALL.md SECURITY.md)
for item in "${STRIP[@]}"; do
  [[ -e "$item" ]] && git rm -rq "$item" 2>/dev/null || true
done
echo "[init-multi-core] stripped: ${STRIP[*]}"

# Seed tenants row if Postgres reachable. COREBRAIN_DB resolver (2026-08-31 fix) — this
# hardcoded the literal "corebrain", the same defect core-doctor.sh hit and fixed 2026-07-27
# (see its "Honour COREBRAIN_DB" comment) and bin/spawn-core already gets right one line above
# where it invokes this script. A COREBRAIN_DB=other_db spawn seeded the tenants row into a
# DIFFERENT database than the one the new Core was actually built against.
COREBRAIN_DB="${COREBRAIN_DB:-corebrain}"
if command -v psql >/dev/null 2>&1 && psql -d "$COREBRAIN_DB" -c 'SELECT 1' >/dev/null 2>&1; then
  psql -d "$COREBRAIN_DB" -c "INSERT INTO tenants (org_id, name, vault_path) VALUES ($ORG_ID, '$DOMAIN', '$TARGET_DIR') ON CONFLICT (org_id) DO NOTHING;" >/dev/null 2>&1 \
    && echo "[init-multi-core] tenants row seeded: $ORG_ID=$DOMAIN (db=$COREBRAIN_DB)" \
    || echo "[init-multi-core] WARN: tenants seed failed (DB may not exist yet)." >&2
else
  echo "[init-multi-core] WARN: psql/$COREBRAIN_DB not reachable — seed tenants manually." >&2
fi

echo ""
echo "[init-multi-core] DONE. Next steps:"
echo "  1. Edit $TARGET_DIR/CLAUDE.md + README.md to define this Core's voice + scope."
echo "  2. Edit $TARGET_DIR/.mcp.json to set CORE_ORG_ID + customize peer-MCP slots."
echo "  3. Commit + push if you keep the new Core under version control."
echo "  4. Open: cd '$TARGET_DIR' && claude"
