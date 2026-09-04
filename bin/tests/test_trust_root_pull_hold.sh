#!/usr/bin/env bash
# The automatic pull holds the THREE trust-root files and lands everything else.
#
# WHY THIS EXISTS
# ---------------
# core-finance found this on 2026-08-05 reviewing a real pull, and it was verified on four disks.
# `.claude/settings.json` on every pull-only Core registers the baseline pull as a SessionStart hook:
#
#     bash "$CLAUDE_PROJECT_DIR/bin/sync-from-baseline.sh" --quiet
#
# SessionStart hooks are not tool calls, so `pretooluse-guard.sh` never sees them. A MANUAL
# `/sync pull` is intercepted and routed to sentinel-code, whose Rule 1 caps at ASK for any
# trust-root change. The AUTOMATIC pull ran the same rsync over the same bytes with no gate at all.
# The human gate was gating the door people knock on while the one that opens by itself stood open.
#
# Worse than an oversight: the decisions-log had already recorded the auto-pull as "the route that
# WORKS" when the manual path dead-ended at the ASK — the same fact, described approvingly. A
# documented workaround for a gate is a hole in that gate.
#
# This test builds a throwaway Core that looks like a puller, points it at a throwaway baseline whose
# trust-root file DIFFERS, runs the real script both ways, and asserts the outcome. It does not
# re-implement the logic; it runs it.
#
#   bash bin/tests/test_trust_root_pull_hold.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/bin/sync-from-baseline.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1${2:+ — $2}"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "wanted '$3', got '$2'"; fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── a throwaway "baseline" git repo carrying a CHANGED trust-root file
BL="$WORK/baseline"
mkdir -p "$BL/.claude/hooks" "$BL/bin" "$BL/docs"
# THE BASELINE'S MANIFEST IS HOSTILE, and this is core-finance's exact attack (bus #685). It is this
# Core's manifest with `.claude/agents/sentinel.md` REMOVED from per_core_keep. The pull reads the
# CLONED manifest to decide what to copy, so before 2026-08-06 that single edit would have stripped the
# protection AND replaced the file in the same unattended sync — no second pull, no reviewer.
mkdir -p "$BL/.claude/agents"
python3 - "$REPO/bin/sync-manifest.json" "$BL/bin/sync-manifest.json" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
m["per_core_keep"] = [p for p in m.get("per_core_keep", [])
                      if p != ".claude/agents/sentinel.md"]
json.dump(m, open(sys.argv[2], "w"), indent=2)
PYEOF
echo 'BASELINE sentinel spec — the thin one. Must NOT reach a puller unattended.'   > "$BL/.claude/agents/sentinel.md"
echo '#!/usr/bin/env bash'                  > "$BL/.claude/hooks/pretooluse-guard.sh"
echo '# BASELINE VERSION — must NOT reach a puller via the automatic pull' >> "$BL/.claude/hooks/pretooluse-guard.sh"
echo '# an ordinary shared file, which MUST still flow'  > "$BL/.claude/hooks/ordinary-shared.sh"
git -C "$BL" init -q -b main
git -C "$BL" -c user.email=t@t -c user.name=t add -A
git -C "$BL" -c user.email=t@t -c user.name=t commit -qm "baseline"

# ── a throwaway Core that looks like a PULLER
CORE="$WORK/core-fake"
mkdir -p "$CORE/.claude/hooks" "$CORE/.claude/state" "$CORE/bin" "$CORE/docs"
cp "$REPO/bin/sync-manifest.json" "$CORE/bin/"
MANIFEST_SHA_BEFORE="$(shasum "$CORE/bin/sync-manifest.json" | awk '{print $1}')"
mkdir -p "$CORE/.claude/agents"
echo 'LOCAL sentinel spec — hardened, carries the VERDICT contract. Must survive.'   > "$CORE/.claude/agents/sentinel.md"
SENTINEL_SHA_BEFORE="$(shasum "$CORE/.claude/agents/sentinel.md" | awk '{print $1}')"
printf '{"org_id": 9, "domain_label": "fake", "hook_profile": {"role": "puller"}}\n' \
  > "$CORE/.claude/identity.json"
echo '#!/usr/bin/env bash'          > "$CORE/.claude/hooks/pretooluse-guard.sh"
echo '# LOCAL VERSION — the one that must survive an automatic pull' >> "$CORE/.claude/hooks/pretooluse-guard.sh"
LOCAL_SHA_BEFORE="$(shasum "$CORE/.claude/hooks/pretooluse-guard.sh" | awk '{print $1}')"
git -C "$CORE" init -q -b main
git -C "$CORE" -c user.email=t@t -c user.name=t add -A
git -C "$CORE" -c user.email=t@t -c user.name=t commit -qm init

run_pull() {  # $1 = extra flag ("" or --check)
  # CLAUDE_PROJECT_DIR is what the script actually reads to locate the Core (not
  # CORE_INSTANCE). Setting only the latter left CORE_DIR pointing at the REAL core-life,
  # whose slug matches baseline_writer, so the writer-guard exited 0 in silence and the
  # test saw nothing. The writer-guard working correctly is what made this confusing.
  ( cd "$CORE" && CLAUDE_PROJECT_DIR="$CORE" CORE_INSTANCE="$CORE" \
      CORE_BASELINE_URL_LOCAL_TEST="$BL" CORE_SYNC_TEST_MODE=1 \
      bash "$SCRIPT" $1 2>&1 )
}

echo "trust-root pull hold"

# Decide live-vs-static by whether the pull ACTUALLY RAN, not by grepping its output: --quiet
# suppresses stdout by design (log() gates it), so matching on text was testing for something the
# mode deliberately hides — the first version of this test fell to static mode for that reason
# while the script underneath was working fine.
#
# The unambiguous evidence a real pull happened is that an ORDINARY shared file arrived.
OUT="$(run_pull --quiet || true)"
printf '%s\n' "$OUT" > "${TR_DEBUG_OUT:-/dev/null}"
if [[ ! -f "$CORE/.claude/hooks/ordinary-shared.sh" ]]; then
  echo "  SKIP  harness could not drive the real script against a local baseline."
  echo "        (It resolves the baseline remote internally; this needs a seam to redirect.)"
  echo "        Asserting the STATIC properties instead, which are still worth pinning:"
  # Static assertions: the code must contain the exclusion and must scope it to quiet mode.
  grep -q 'TRUST_ROOT_PATHS=(' "$SCRIPT" \
    && ok "TRUST_ROOT_PATHS is defined in sync-from-baseline.sh" \
    || bad "TRUST_ROOT_PATHS is defined"
  grep -q 'pretooluse-guard.sh' "$SCRIPT" \
    && ok "the guard is named in the trust-root list" || bad "guard named"
  grep -q 'sentinel-approve.sh' "$SCRIPT" \
    && ok "sentinel-approve is named" || bad "sentinel-approve named"
  grep -q 'sentinel-receipt.sh' "$SCRIPT" \
    && ok "sentinel-receipt is named (the un-forgeable link)" || bad "sentinel-receipt named"
  awk '/if \[\[ "\$MODE" == "quiet" \]\]; then/,/fi/' "$SCRIPT" | grep -q 'PER_DIR_EXCLUDES+=(--exclude' \
    && ok "the exclusion is applied ONLY in quiet mode" \
    || bad "exclusion scoped to quiet mode"
  grep -q 'TRUST-ROOT CHANGE HELD' "$SCRIPT" \
    && ok "a held change is reported loudly" || bad "held change reported"
  grep -q '>&2' "$SCRIPT" \
    && ok "the notice goes to stderr, which survives --quiet" || bad "notice on stderr"
  grep -q '.trust-root-pending' "$SCRIPT" \
    && ok "a marker file is written for the next SessionStart" || bad "marker written"
  echo ""
  echo "=== Results: $PASS passed, $FAIL failed (static mode) ==="
  [[ $FAIL -eq 0 ]] && exit 0 || exit 1
fi

# ── live mode: the harness drove the real script
LOCAL_SHA_AFTER="$(shasum "$CORE/.claude/hooks/pretooluse-guard.sh" | awk '{print $1}')"
# This assertion INVERTED TWICE on 2026-08-06 and the final direction is the original one.
# I briefly asserted the file must LAND, on the argument that an un-arrived file and an un-reviewed
# file are equally un-reviewed. sentinel-code refuted it: AN UN-ARRIVED FILE CANNOT EXECUTE. A bad
# guard installed by an automatic pull becomes the operative guard immediately, so the review it is
# waiting for would be adjudicated by the replaced guard itself. Nick chose to hold these three.
check "automatic pull did NOT replace the trust-root file" "$LOCAL_SHA_AFTER" "$LOCAL_SHA_BEFORE"
grep -q "TRUST-ROOT CHANGE HELD" <<<"$OUT" \
  && ok "it says the change was HELD, loudly" || bad "held notice" "absent from output"
PJ="$CORE/.claude/state/.pull-pending-actions.json"
[[ -f "$PJ" ]] && ok "pending-actions file written (survives the pre-read scroll)" \
  || bad "pending-actions file written"
grep -q 'apply_trust_root' "$PJ" 2>/dev/null \
  && ok "it names apply_trust_root as the owed action" || bad "names apply_trust_root"
python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$PJ" 2>/dev/null \
  && ok "the pending-actions file is valid JSON (a session can read it)" \
  || bad "pending-actions file is valid JSON"
[[ -f "$CORE/.claude/hooks/ordinary-shared.sh" ]] \
  && ok "an ordinary shared file still flowed (the pull is not broken)" \
  || bad "ordinary shared file flowed"

# ── THE HOSTILE MANIFEST (core-finance, bus #685) ────────────────────────────────────────────────
# per_core_keep is not a local veto: the BASELINE supplies the list of what the baseline may not
# replace. The single most important assertion in this file is the last of these three — not that the
# manifest was held, but that the pull did not OBEY it. Holding the file while still following its
# rules would have been a fix that looked right and changed nothing, and that is exactly what I nearly
# shipped: TRUST_ROOT_PATHS is not built until well after the governing manifest has been chosen.
MANIFEST_SHA_AFTER="$(shasum "$CORE/bin/sync-manifest.json" | awk '{print $1}')"
check "the hostile manifest did NOT overwrite the local one" "$MANIFEST_SHA_AFTER" "$MANIFEST_SHA_BEFORE"
grep -q "SYNC MANIFEST CHANGED" <<<"$OUT" \
  && ok "...and the pull says so rather than adopting it silently" \
  || bad "manifest-held notice" "absent from output"
grep -q 'apply_manifest' "$PJ" 2>/dev/null \
  && ok "...and queues apply_manifest so a session must act on it" \
  || bad "queues apply_manifest"
# ONE owner per fact. sentinel-code found the manifest double-booked: it sits in TRUST_ROOT_PATHS for
# its exclusion AND has its own MANIFEST_PENDING check, and both fire on the identical condition, so a
# manifest-only change queued apply_trust_root as well and printed two notices for one file. Two
# mechanisms reporting one fact is the accretion this whole session was spent removing.
MANIFEST_TR_COUNT=$(grep -c '"path":"bin/sync-manifest.json"' "$PJ" 2>/dev/null || echo 0)
check "the manifest is queued exactly ONCE, not by both mechanisms" "$MANIFEST_TR_COUNT" "1"
grep -q '"action":"apply_trust_root","path":"bin/sync-manifest.json"' "$PJ" 2>/dev/null \
  && bad "manifest must not also queue apply_trust_root" "double-booked" \
  || ok "...and not as apply_trust_root (that list owns the exclusion, not the reporting)"
SENTINEL_SHA_AFTER="$(shasum "$CORE/.claude/agents/sentinel.md" 2>/dev/null | awk '{print $1}')"
check "THE POINT: the file the hostile manifest de-protected was NOT replaced" \
  "$SENTINEL_SHA_AFTER" "$SENTINEL_SHA_BEFORE"

# ── THE BOOTSTRAP HOLE (core-finance, bus #600) ──────────────────────────────────────────────────
# The hold-back logic ships INSIDE the pull that carries it, so a peer's first automatic pull runs the
# OLD script — no exclusion, no notice — and the guard lands ungated exactly once, silently. Verified
# on the real baseline: the commit the peers are installed from has 0 occurrences of the held notice.
#
# I had told the peers that seeing "TRUST-ROOT CHANGE HELD" was the correct signal, which made the
# ABSENCE of it read as success on precisely the pull where it means the opposite. Unfixable
# retroactively — code that ran, ran — so the requirement is that it cannot pass SILENTLY.
grep -q "FIRST PULL UNDER TRUST-ROOT HOLD-BACK" <<<"$OUT" \
  && ok "the first pull says so, instead of letting an already-ungated arrival look normal" \
  || bad "bootstrap notice" "absent from output"
grep -q 'verify_trust_root_bootstrap' "$PJ" 2>/dev/null \
  && ok "...and it queues verify_trust_root_bootstrap so a session must act on it" \
  || bad "queues verify_trust_root_bootstrap"
# Once only. A warning that repeats every pull is one a reader learns to skip, and the condition it
# describes is genuinely one-time per Core.
OUT2="$(run_pull --quiet || true)"
grep -q "FIRST PULL UNDER TRUST-ROOT HOLD-BACK" <<<"$OUT2" \
  && bad "bootstrap notice fires ONCE" "it repeated on the second pull" \
  || ok "...and it does NOT repeat on the next pull (a marker records it)"
[[ -f "$CORE/.claude/state/.trust-root-holdback-active" ]] \
  && ok "the marker is written where the next run will find it" \
  || bad "hold-back marker written"

# The seam must refuse a lone stale variable (sentinel-code advisory, 2026-08-05).
STALE="$( cd "$CORE" && CLAUDE_PROJECT_DIR="$CORE" CORE_BASELINE_URL_LOCAL_TEST="$BL" \
            bash "$SCRIPT" --quiet 2>&1 || true )"
grep -q "REFUSED test seam" <<<"$STALE" \
  && ok "seam REFUSES without CORE_SYNC_TEST_MODE=1 (a stale var alone does nothing)" \
  || bad "seam refuses without the intent marker" "got: ${STALE:0:120}"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
