#!/usr/bin/env bash
# setup-brain.sh — from-zero DB bootstrap for the corebrain Postgres layer.
#
# The single orchestrator the STANDING INVARIANT (spec-brain-unfreeze Phase 4)
# requires: a fresh fork runs ONE command and gets a working brain DB. Composes
# the existing pieces in the correct order, idempotently:
#
#   1. dep check (psql, python3)
#   2. createdb (if missing)
#   3. schema.sql            (base tables/indexes — idempotent)
#   4. run-migrations.sh     (RLS, edge-embeddings, brain_admin, retag — tracked)
#   5. init-brain-roles.sh   (brain_app + brain_admin roles + grants)
#   6. seed primary tenant   (org_id from identity.json, default 1=life)
#   7. bootstrap the VAULT   ($CORE_BRAIN markdown vault + _build pipeline)
#   8. core-doctor.sh        (verify green)
#
# The baseline Makefile's `setup-brain` target should just call this script, so
# the real logic stays on-disk (syncable) rather than in the Makefile.
#
# Usage: bash bin/setup-brain.sh           # bootstrap/verify corebrain
#        COREBRAIN_DB=foo bash bin/setup-brain.sh   # target a different DB (testing)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${COREBRAIN_DB:-corebrain}"
SCHEMA="$REPO/scheduling/brain-pg/schema.sql"
# Source the path registry so the identity path comes from the central JSON
# (CORE_IDENTITY_JSON) rather than a hardcoded literal — keeps code-paths lint clean.
export CORE_INSTANCE="${CORE_INSTANCE:-$REPO}"
# shellcheck source=bin/core-paths.sh
source "$REPO/bin/core-paths.sh"
step() { echo ""; echo "── [setup-brain] $* ──"; }

# 1. deps
command -v psql >/dev/null 2>&1 || { echo "ERROR: psql not found (install Postgres / run install-deps.sh)." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found." >&2; exit 1; }

# 2. createdb if missing
step "ensuring database '$DB' exists"
if psql -d "$DB" -c 'SELECT 1' >/dev/null 2>&1; then
  echo "  database '$DB' already exists."
else
  createdb "$DB" && echo "  created database '$DB'."
fi

# 3. base schema (idempotent)
step "applying schema.sql"
[[ -f "$SCHEMA" ]] || { echo "ERROR: $SCHEMA not found." >&2; exit 1; }
COREBRAIN_DB="$DB" psql -d "$DB" -v ON_ERROR_STOP=1 -q -f "$SCHEMA" && echo "  schema applied."

# 4. migrations — object-aware reconcile.
#
# This used to run `--baseline`, which records every migration as applied WITHOUT
# running it, on the stated assumption that "schema.sql is the COMPLETE current
# end-state." That assumption was false: si_artifacts, si_projection_state,
# friction_cases, assertions and the source-revision ledger exist ONLY in
# migrations/. The result (verified 2026-07-27 by running this script on a clean
# clone) was 17 migrations marked applied while 10 tables were never created — and
# because the tracker then claimed they were applied, run-migrations.sh could never
# repair it. Every fresh Core silently had no learned layer.
#
# `--ensure` applies a migration when the tables it declares are absent and merely
# records it when they are already present, so it is correct both here on a fresh DB
# and as a repair for any Core that was previously mis-baselined.
step "reconciling migrations (apply what's missing, record what's present)"
COREBRAIN_DB="$DB" bash "$REPO/bin/run-migrations.sh" --ensure

# 5. roles
step "ensuring roles (brain_app + brain_admin)"
COREBRAIN_DB="$DB" bash "$REPO/bin/init-brain-roles.sh"

# 6. seed primary tenant from identity.json (fallback 1=life)
step "seeding primary tenant row"
IDENTITY="$CORE_IDENTITY_JSON"   # set by core-paths.sh (sourced above)
ORG_ID=1; NAME="life"
if [[ -f "$IDENTITY" ]] && command -v python3 >/dev/null 2>&1; then
  read -r ORG_ID NAME < <(python3 -c "
import json
try:
    d=json.load(open('$IDENTITY'))
    print(d.get('org_id',1), d.get('core_slug') or d.get('core_label') or 'life')
except Exception:
    print(1,'life')
")
fi
psql -d "$DB" -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO tenants (org_id, name, vault_path) VALUES ($ORG_ID, '$NAME', '$REPO') ON CONFLICT (org_id) DO NOTHING;" \
  && echo "  tenant ensured: $ORG_ID=$NAME"

# 7. brain VAULT bootstrap
#
# WHY THIS IS HERE AND NOT IN ITS OWN SCRIPT (2026-08-29). The DB is only half the brain: the
# other half is the markdown vault at $CORE_BRAIN, whose _build/ pipeline the Stop hook calls by
# absolute path (`run-brain-update.sh` → "$CORE_BRAIN/_build/update-brain.sh"). Nothing in the
# documented install created it. bin/init-brain.sh did, correctly — but nothing called it, it was
# unreferenced by SETUP.md, and it pointed at an INSTALL.md that no longer exists. So a fresh Core
# that followed the README got a working database and a missing vault, and the heavy brain pass
# failed into a log file on every close with no surfaced error.
#
# Folded in here rather than re-wired as a sixth README step because setup-brain.sh is already THE
# brain installer; two brain-setup entry points is the accretion pattern, not the fix.
step "bootstrapping brain vault at \$CORE_BRAIN"
VAULT="${CORE_BRAIN:?CORE_BRAIN unset — core-paths.sh should export it}"
TEMPLATE_DIR="$REPO/template/brain"
VAULT_OK=1
if [[ ! -d "$TEMPLATE_DIR" ]]; then
  # NOT a warning. A missing template is precisely the state this whole step exists to prevent —
  # a user with a database and no pipeline — so reporting it inside a run that then prints
  # "✓ setup-brain done" reproduces the original bug with extra steps. Flagged in review.
  echo "  ERROR: $TEMPLATE_DIR missing — cannot seed the vault; the brain pipeline will not run."
  VAULT_OK=0
else
  NEW_VAULT=0
  [[ -d "$VAULT" ]] || { mkdir -p "$VAULT"; NEW_VAULT=1; echo "  created $VAULT"; }

  # --ignore-existing is load-bearing, NOT defensive. The vault's _build/ scripts are
  # PERSONALISED per Core (entity alias maps, topic merges, the push-destination allowlist), and
  # this script is idempotent by contract — re-run on an established Core. A plain `rsync -a`
  # here would silently overwrite a live vault's real alias map with the stripped template one on
  # every re-run. bin/init-brain.sh's --graft mode documented "never overwrites" and then used a
  # bare `rsync -a` that did exactly that; the promise was in the comment, not the flag.
  rsync -a --ignore-existing --exclude="__pycache__" --exclude="*.pyc" "$TEMPLATE_DIR/" "$VAULT/"
  # Only chmod when the bit is actually absent. An unconditional chmod mutates a file in an
  # established, personalised vault on every idempotent re-run — content preserved, metadata not,
  # which makes the "never overwrites" promise above narrower than it reads.
  [[ -f "$VAULT/_build/update-brain.sh" && ! -x "$VAULT/_build/update-brain.sh" ]] \
    && chmod +x "$VAULT/_build/update-brain.sh" 2>/dev/null || true

  if [[ $NEW_VAULT -eq 1 && ! -d "$VAULT/.git" ]]; then
    # Report what actually happened. Swallowing every failure and then printing "git initialised"
    # unconditionally means a missing git, or an unset user.email, leaves the vault untracked
    # while setup claims otherwise — the say-without-do shape, in the installer.
    if (cd "$VAULT" && git init -q && git add -A \
         && git commit -q -m "Initial commit — vault bootstrapped by setup-brain.sh") 2>/dev/null; then
      echo "  git initialised (add a PRIVATE remote: the vault holds every transcript)"
    else
      echo "  NOTE: could not git-init the vault (is git configured?). The vault works, but is"
      echo "        unversioned — run 'git init' in $VAULT yourself."
    fi
  fi
  echo "  vault ready: $VAULT"
fi

# 8. doctor
#
# DOCTOR_OK follows the same shape as VAULT_OK above (2026-08-31, same defect class): this step
# used to run core-doctor.sh and then unconditionally echo "✓ setup-brain done" a few lines
# below regardless of what it found — the documented VERIFICATION step ran, correctly printed
# e.g. "✗ SI-spine tables MISSING: learned_contracts", and the very next thing on screen was a
# green checkmark contradicting it. That was possible only because core-doctor.sh itself never
# exited non-zero (fixed the same day — see bin/core-doctor.sh header); now that it does, this
# is the other half: actually read the exit code instead of `|| echo`-ing past it.
step "core-doctor verification"
DOCTOR_OK=1
if [[ -x "$REPO/bin/core-doctor.sh" ]]; then
  COREBRAIN_DB="$DB" bash "$REPO/bin/core-doctor.sh" || DOCTOR_OK=0
  if [[ "$DOCTOR_OK" -eq 0 ]]; then
    echo "  core-doctor reported a FAILED INVARIANT — review the ✗ lines above."
  fi
else
  # Not "skip and move on": the documented install's own verification step never ran, so
  # nothing below is entitled to say "done" any more than the missing-template branch above is.
  echo "  core-doctor.sh not found — verification did NOT run."
  DOCTOR_OK=0
fi

echo ""
if [[ "${VAULT_OK:-1}" -eq 1 && "$DOCTOR_OK" -eq 1 ]]; then
  echo "✓ setup-brain done for '$DB' (database + vault)."
else
  echo "✗ setup-brain INCOMPLETE for '$DB':"
  [[ "${VAULT_OK:-1}" -eq 0 ]] && echo "  - the vault was not created. The brain pipeline cannot run until it exists. See docs/SETUP.md."
  [[ "$DOCTOR_OK" -eq 0 ]] && echo "  - core-doctor verification did not pass. Re-run: bash bin/core-doctor.sh"
  exit 1
fi
