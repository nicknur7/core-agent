#!/usr/bin/env bash
# si-unify-cutover.sh — the ONE switch that lights up the unified SI spine on THIS Core (WS1).
#
# Ordering is deliberate to avoid a double-fire window (Codex WS1 review): the classifier is removed
# BEFORE legacy enters the projection, so at worst the 4 legacy guardrails are briefly ABSENT (safe)
# rather than briefly DOUBLED. Every step fails HARD — a partial cutover exits non-zero and says so.
# THE OLD LINE HERE SAID "Run a fresh session afterwards so the new settings.json is loaded."
# THAT IS FALSE, measured by core-school on 2026-08-28 (bus #5582) after Nick challenged it.
#
# Claude Code re-reads settings.json per hook invocation; it does not cache the registration set at
# SessionStart. The natural experiment: at 01:53 PDT, 18 minutes AFTER the 01:35 cutover unregistered
# learned-classifier on school, Nick sent a prompt containing a word matching that classifier's
# trigger verbatim. school received the injection. But learned-classifier.py writes FIRE_LOG on EVERY
# match (line 25), and learned-fires.log has ZERO classifier entries at or after the cutover — its
# last is 08:09:48 UTC, pre-cutover — while friction-action-log.jsonl was written at exactly 01:53.
# The old hook did not run; the new spine served it. The unregistration was live immediately.
#
# The cost of the false line was not cosmetic: it told two seats to discard a live session for no
# reason, and school was mid-build when it relayed it. Verify with your fire logs instead of
# assuming a memory model nobody measured.
#
# Forward:   ! bash bin/si-unify-cutover.sh
# Rollback:  ! bash bin/si-unify-cutover.sh --rollback
#
# Must be run by Nick via `!` — reconcile mutates settings.json (trust-root), which the agent is denied.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# ORG COMES FROM THIS SEAT'S identity.json, NEVER FROM THE ENVIRONMENT (2026-08-28).
#
# This read `ORG="${CORE_ORG_ID:-1}"` — trusting a leaked env var over the seat's own identity, and
# defaulting to org 1 when unset. That is the precise pattern behind the 2026-07-25 cross-partition
# write, which core-si-close.py was fixed for and this script was not.
#
# CAUGHT LIVE running core-business's cutover from a core-life session: every Claude Bash call
# inherits CORE_ORG_ID=1 from life's settings.json, so `cd ../core-business && bash
# bin/si-unify-cutover.sh` announced "CUTOVER ... on core-business (org 1)" and then set the DB
# session context to org 1 while si_project resolved the ROWS as org 2 from identity. RLS refused
# the write and aborted the run — the guard held, and it is the only reason this was visible rather
# than a silent write into another Core's partition.
#
# _env.get_org_id() is the ONE resolver (identity wins over a leaked env). Falling back to the env
# var only if that resolver cannot run at all, and refusing outright rather than defaulting to 1 —
# a cutover that cannot prove which seat it is on must not pick one.
ORG="$(cd "$REPO" && python3 -c "
import sys; sys.path.insert(0, 'scheduling/brain-pg')
from _env import get_org_id; print(get_org_id())" 2>/dev/null)"
if ! [[ "$ORG" =~ ^[0-9]+$ ]]; then
  echo "✗ REFUSING: could not resolve org_id from $REPO/.claude/identity.json." >&2
  echo "  Not defaulting to 1 — that is how a cutover writes into another Core's partition." >&2
  exit 1
fi
MARKER="$REPO/.claude/state/.si-unified-spine"
export CORE_ORG_ID="$ORG" CORE_INSTANCE="$REPO"
# IDENT comes from the path registry, not a literal: this script REWRITES identity.json, so a
# stale location here would silently write the override to a file nothing reads. Sourced after
# CORE_INSTANCE is exported, which is what core-paths.sh resolves paths against.
source "$REPO/bin/core-paths.sh"
IDENT="$CORE_IDENTITY_JSON"
[ -n "$IDENT" ] || { echo "FATAL: CORE_IDENTITY_JSON unset — path registry did not load"; exit 1; }
cd "$REPO/scheduling/claude-si" || { echo "FATAL: cannot cd to claude-si"; exit 1; }

die() { echo "✗ CUTOVER ABORTED (incomplete): $1" >&2; exit 1; }

_set_override() {  # $1 = off | unset   (atomic write). Disables the OLD learned contract-injection +
  # resynth-nag hooks on THIS core (replaced by the friction spine). Enforcement guards stay.
  python3 - "$IDENT" "$1" <<'PY' || exit 1
import json, os, sys
p, val = sys.argv[1], sys.argv[2]
# learned-validator ADDED 2026-08-28. It reads learned_contracts — the very table this cutover
# retires — so leaving it registered means a cut-over seat keeps validating against a dead spine.
#
# Life proves the omission rather than argues it: life cut over 2026-07-23 and its identity note
# from 2026-07-30 reads "The cutover unregistered learned-classifier and learned-resynth-trigger
# and left three siblings behind; this finishes it for the one that actually depends on the dead
# table." That fix was applied BY HAND to life and never folded back into the script, so every seat
# cutting over afterwards inherited the same defect. core-school cut over 2026-08-28 01:35 and was
# still carrying five red "learned contract ... NOT BINDING" alerts computed from an 11-day-old
# contract-fitness.json measured against the old spine.
#
# THE OTHER TWO SIBLINGS STAY, deliberately: learned-recallguard and learned-stopguard do not read
# learned_contracts, so they are unaffected by the cutover and retiring them here would be exactly
# the fleet-propagation trap Fable flagged — one Core's local reading disabling live guards
# elsewhere. Gating on the cutover MARKER is what makes this safe: a pre-cutover seat still runs
# the validator legitimately against a table that is still live for it.
# learned-validator was ADDED here on 2026-08-28 and REMOVED THE SAME DAY. Recording both, because
# the reason it went in is the interesting part.
#
# I added it on the stated basis that it READS learned_contracts — the table this cutover retires —
# and cited life's 2026-07-30 identity note saying exactly that. core-finance opened the file:
#
#   .claude/hooks/learned-validator.py — "learned_contracts" appears EXACTLY ONCE, in the docstring
#   at line 4, describing where its clauses historically came from. No psycopg2, no _env, no SQL,
#   no DB import anywhere. main() reads latest_jsonl() — the session transcript.
#
# Verified independently on core-life's own copy before reverting. The hook has no runtime
# dependency on that table at all, so "the cutover retires the table it reads" was never true of
# it. I disabled a working hook on two peer seats on the strength of one docstring line, and
# propagated the same reasoning into this shared script where it would have retired it on every
# future cutover, fleet-wide. business and school had each already reverted their own before I got
# there.
#
# WHAT IS STILL OPEN, and is NOT a reason to leave the wrong change in: finance's narrower point —
# the validator enforces a clause once sourced from learned_contracts, and if those clauses now
# arrive through si_artifacts in a different shape it may be matching something that no longer
# comes. That is a live efficacy question nobody has measured. An unmeasured question does not
# justify a retirement, and a retirement justified by a false claim does not become correct because
# a true claim might exist.
RETIRED = ["learned-classifier", "learned-resynth-trigger"]
d = json.load(open(p))
ov = d.setdefault("hook_profile", {}).setdefault("overrides", {})
for h in RETIRED:
    ov.pop(h, None) if val == "unset" else ov.__setitem__(h, val)
tmp = p + ".tmp"
json.dump(d, open(tmp, "w"), indent=2); open(tmp, "a").write("\n")
os.replace(tmp, p)
print(f"   identity overrides {RETIRED} = {val}")
PY
}
_py() { python3 -c "$1"; }

if [[ "${1:-}" == "--rollback" ]]; then
  echo "== ROLLBACK (safe order: legacy OUT of projection first, then classifier back) =="
  rm -f "$MARKER" || die "could not remove marker"
  _py "import si_project as s; s.project($ORG)" || die "friction-only reprojection failed"
  _set_override unset || die "could not unset identity override"
  python3 "$REPO/bin/reconcile-hooks.py" --core "$REPO" --apply || die "reconcile failed — classifier not restored"
  echo "== rolled back cleanly. classifier restored; legacy out of the dispatcher. =="
  exit 0
fi

echo "== CUTOVER: unifying the SI spine on $(basename "$REPO") (org $ORG) =="
echo "1) stage canonical DB (import file-only artifacts + migrate legacy — no live effect yet)"
_py "import si_project as s; print('   import:',s.import_active_file($ORG))" || die "import_active_file failed (unreadable/malformed active.json) — staging aborted"
# migrate_legacy RAISES on a missing/malformed snapshot; assert EVERY trigger-bearing legacy contract
# migrated BEFORE we disable the classifier — otherwise cutover would leave guardrails unprotected.
_py "import si_project as s,sys; r=s.migrate_legacy($ORG); print('   migrate:',r); sys.exit(0 if r['migrated']==r['expected'] and r['migrated']>0 else 1)" \
  || die "legacy migration incomplete (expected all trigger-bearing contracts) — staging aborted, classifier untouched"
echo "2) remove learned-classifier on THIS core (identity override -> reconcile) BEFORE projecting legacy"
_set_override off || die "could not set identity override"
python3 "$REPO/bin/reconcile-hooks.py" --core "$REPO" --apply || die "reconcile failed — classifier still registered"
echo "3) flip the marker + project legacy+friction from the canonical DB"
mkdir -p "$(dirname "$MARKER")" && touch "$MARKER" || die "could not set marker"
_py "import si_project as s; print('   ',s.project($ORG))" || die "projection failed (marker set, classifier off — run --rollback)"
echo "4) ASSERT invariants (parity + rev in sync) — cutover FAILS if stale"
_py "import si_project as s,sys; inv=s.verify_invariants($ORG); print('   ',inv); sys.exit(0 if inv.get('count_parity') and inv.get('rev_in_sync') else 1)" \
  || die "post-cutover invariants NOT clean — run --rollback and investigate"
echo "== DONE. One spine live: friction_loop -> si_artifacts -> friction-dispatch. Rollback: --rollback =="
echo "   Takes effect IMMEDIATELY — settings.json is re-read per hook invocation, not cached at"
echo "   SessionStart (measured on core-school 2026-08-28, bus #5582). No restart required."
echo "   Verify on your own seat: a prompt matching a retired classifier trigger should produce NO"
echo "   new row in .claude/state/learned-fires.log, while friction-action-log.jsonl advances."
