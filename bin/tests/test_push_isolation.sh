#!/usr/bin/env bash
# test_push_isolation.sh — a Core's session state must never reach the shared baseline.
#
# WHY THIS EXISTS
# ---------------
# On 2026-06-19 a spawned Core auto-committed its own instance state into the baseline repo:
# its owner's identity.json, his personalised CLAUDE.md, his memory/, and a third party's
# email. It also deleted the template scaffolding — INSTALL.md, template/brain/, the lot.
# Every "the baseline is contaminated" and "the docs are broken" symptom traces to it.
#
# It was not carelessness. bin/init-multi-core.sh clones from the baseline repo and never
# repoints origin, and both push sites in session-lifecycle.sh ran a bare `git push` with
# no idea where origin pointed. His Core did exactly what our code told it to. The commit
# message on it is generated verbatim by session-lifecycle.sh.
#
# These tests pin the guard that closes it. Run: bash bin/tests/test_push_isolation.sh

set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
LIFECYCLE="$REPO/.claude/hooks/session-lifecycle.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
_fails=()

check() { # name, expected, actual
  if [[ "$2" == "$3" ]]; then printf "  PASS  %s\n" "$1"
  else printf "  FAIL  %s  (expected=%s actual=%s)\n" "$1" "$2" "$3"; _fails+=("$1"); fi
}

# Extract the guard functions rather than sourcing the whole controller, which would run it.
python3 - "$LIFECYCLE" > "$TMP/guard.sh" <<'PY'
import sys
src = open(sys.argv[1]).read()
def fn(name):
    i = src.index(f"{name}() {{")
    return src[i: src.index("\n}\n", i) + 3]
print(fn("_normalize_remote")); print(fn("_push_allowed"))
PY
bash -n "$TMP/guard.sh" || { echo "FATAL: could not extract guard functions"; exit 1; }

echo "=== remote normalisation ==="
# BSD sed (macOS) treats GNU's \? literally. The first cut of this used https\? — every
# URL normalised to "https://github.com/owner/repo", which could never equal the manifest's
# "owner/repo", so the guard was permanently open while looking correct. Caught only by
# testing all five URL forms instead of the happy path.
norm() { bash -c "source '$TMP/guard.sh'; _normalize_remote '$1'"; }
check "https + .git"        "nicknur7/widgets"   "$(norm 'https://github.com/nicknur7/Widgets.git')"
check "ssh scp-style"       "nicknur7/widgets"   "$(norm 'git@github.com:nicknur7/Widgets.git')"
check "trailing slash"      "nicknur7/widgets"   "$(norm 'https://github.com/nicknur7/Widgets/')"
check "ssh:// + mixed case" "nicknur7/widgets"   "$(norm 'ssh://git@github.com/NickNur7/WIDGETS.git')"
check "a Core's own repo"   "nicknur7/my-own-core" "$(norm 'https://github.com/nicknur7/my-own-core.git')"

echo
echo "=== push decisions ==="
verdict() { # url, enrolled, dirname -> ALLOW|REFUSE
  local d="$TMP/$3"; rm -rf "$d"; mkdir -p "$d/.claude/state" "$d/bin"
  ( cd "$d" && git init -q . )
  [[ -n "$1" ]] && ( cd "$d" && git remote add origin "$1" )
  cp "$REPO/bin/sync-manifest.json" "$d/bin/"
  [[ "$2" == "yes" ]] && touch "$d/.claude/state/.baseline-writer-enrolled"
  ( cd "$d" && bash -c "source '$TMP/guard.sh'; REPO='$d'
      if _push_allowed >/dev/null 2>&1; then echo ALLOW; else echo REFUSE; fi" )
}

check "spawned Core, origin=baseline (https)" REFUSE "$(verdict 'https://github.com/nicknur7/core-agent.git' no spawned1)"
check "spawned Core, origin=baseline (ssh)"   REFUSE "$(verdict 'git@github.com:nicknur7/core-agent.git' no spawned2)"
check "the 2026-06-19 case cannot recur"      REFUSE "$(verdict 'https://github.com/nicknur7/core-agent.git' no core-business)"
check "a Core pushing to its OWN repo"        ALLOW  "$(verdict 'https://github.com/nicknur7/my-own-core.git' no core-life)"
check "no origin — skip, do not error"        REFUSE "$(verdict '' no fresh)"

# Enrollment needs ALL THREE: manifest names this Core the writer, directory name matches,
# and the local-only marker is present. hook_profile.role is deliberately NOT sufficient —
# it controls hook composition, not repository ownership, and a fork can edit it.
check "enrolled writer may push to baseline"  ALLOW  "$(verdict 'https://github.com/nicknur7/core-agent.git' yes core-life)"
check "right name, NO marker -> refused"      REFUSE "$(verdict 'https://github.com/nicknur7/core-agent.git' no core-life)"
check "marker but wrong Core -> refused"      REFUSE "$(verdict 'https://github.com/nicknur7/core-agent.git' yes core-school)"

echo
echo "=== no bare pushes remain ==="
bare=$(grep -cE '^\s*git push' "$LIFECYCLE" || true)
check "every push site goes through the guard" "1" "$bare"   # the 1 is inside _guarded_push

echo
if (( ${#_fails[@]} )); then
  printf "FAILURES (%d): %s\n" "${#_fails[@]}" "${_fails[*]}"; exit 1
fi
echo "ALL PASS"
