#!/usr/bin/env bash
# The peer-approval path, tested REFUSAL-FIRST.
#
# core-business's casebook item, and it is the reason this file exists rather than a scratch script:
#
#   WHEN A NEW APPROVAL MECHANISM SHIPS, THE FIRST TEST IS NOT "DOES IT ACCEPT A GOOD VERDICT" —
#   IT IS "DOES REFUSING ACTUALLY BLOCK." An approval path that cannot refuse is indistinguishable
#   from no approval path, and it is invisible from inside a successful review.
#
# It was invisible for seven rounds. `_peer_approval` did real work — read the bus, found the
# verdict, checked the author was not the requester — and assigned the answer to a variable nothing
# read. Business confirmed it from a fresh baseline clone by DELETING the assignment and diffing the
# parse: identical. A mechanism that runs, reports success, and has no effect.
#
# So the refusal cases run FIRST here, and case 4 (the accept) runs LAST. If the refuse cases ever
# start passing vacuously, the accept case failing is what tells you.
#
# Everything runs against a COPY of the trust root with a scratch state dir and a scratch bus, so a
# test can never mint a real token or touch the real thread.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC="$ROOT/.claude/hooks/sentinel-approve.sh"
CMD="bash bin/sync-to-baseline.sh"
PASS=0; FAIL=0

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/.claude/hooks" "$WORK/.claude/state" "$WORK/memory"
cp "$SRC" "$WORK/.claude/hooks/"

# A scratch bus containing exactly one message.
mkbus() {
  local h; h="$(mktemp -d)"
  mkdir -p "$h/AI Projects/core-bus"
  cp "$HOME/AI Projects/core-bus/peer-msg.py" "$h/AI Projects/core-bus/" 2>/dev/null || true
  printf '%s\n' "$1" > "$h/AI Projects/core-bus/thread.jsonl"
  echo "$h"
}

msg() {  # from, verdict, reviewed-command
  python3 -c '
import json, sys, time
print(json.dumps({"seq": 900, "to": "life", "from": sys.argv[1],
                  "ts": time.strftime("%Y-%m-%d %H:%M:%S PDT"),
                  "text": "REVIEWED: %s\n\nbody\n\nVERDICT: %s" % (sys.argv[3], sys.argv[2])}))' \
    "$1" "$2" "$3"
}

try() {  # label, expect(refuse|allow), bus-home
  local label="$1" expect="$2" home="$3"
  # CORE_BUS_NAME pinned: whoami() resolves from the project-dir NAME, and a mktemp dir is not
  # named core-<x>. Without this every peer row is skipped on `to != me` and ALL FOUR CASES PASS
  # VACUOUSLY — which is how the accept case failed on its first run and looked like a code bug.
  HOME="$home" CORE_BUS_NAME=life CLAUDE_PROJECT_DIR="$WORK" \
    bash "$WORK/.claude/hooks/sentinel-approve.sh" "$CMD" >/dev/null 2>&1
  local rc=$?
  local got="allow"; [[ $rc -ne 0 ]] && got="refuse"
  if [[ "$got" == "$expect" ]]; then
    printf '  PASS  %s\n' "$label"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %s (expected %s, got %s)\n' "$label" "$expect" "$got"; FAIL=$((FAIL + 1))
  fi
}

echo
echo "=== peer approval — REFUSALS FIRST ==="

try "R1 a Core cannot approve itself"          refuse "$(mkbus "$(msg life     APPROVE "$CMD")")"
try "R2 approval naming a DIFFERENT command"   refuse "$(mkbus "$(msg business APPROVE "bash bin/other.sh")")"
try "R3 peer returned BLOCK"                   refuse "$(mkbus "$(msg business BLOCK   "$CMD")")"
try "R4 peer returned ASK"                     refuse "$(mkbus "$(msg business ASK     "$CMD")")"
try "R5 empty bus — no verdict at all"         refuse "$(mkbus '')"

echo
echo "=== direction fence: a peer approval is for PUSHES only ==="
# core-business: on a PULL the approver is the AUTHOR of the content, so `frm == me` passes while
# the property it guarantees — that someone other than the author looked — does not hold at all.
# The pull case must refuse even with a PERFECTLY VALID approval naming it.
PULLCMD="bash bin/sync-from-baseline.sh"
PULLBUS="$(mkbus "$(python3 -c '
import json, sys, time
print(json.dumps({"seq": 901, "to": "life", "from": "business",
                  "ts": time.strftime("%Y-%m-%d %H:%M:%S PDT"),
                  "text": "REVIEWED: %s\n\nbody\n\nVERDICT: APPROVE" % sys.argv[1]}))' "$PULLCMD")")"
HOME="$PULLBUS" CORE_BUS_NAME=life CLAUDE_PROJECT_DIR="$WORK" \
  bash "$WORK/.claude/hooks/sentinel-approve.sh" "$PULLCMD" >/dev/null 2>&1
if [[ $? -ne 0 ]]; then
  printf '  PASS  D1 valid peer APPROVE for a PULL is refused\n'; PASS=$((PASS + 1))
else
  printf '  FAIL  D1 a peer approved its own content onto another Core\n'; FAIL=$((FAIL + 1))
fi

echo
echo "=== Codex 2026-08-09 — identity aliasing and allowlist evasion ==="
# Every case here PASSED (i.e. wrongly authorised) before the fixes in this commit. Identity was
# compared as a raw string, so anything textually different from `me` read as "a different Core";
# and the direction fence was a denylist on one filename substring.
try "C1 self-approval via CASE ('Life')"        refuse "$(mkbus "$(msg Life       APPROVE "$CMD")")"
try "C2 self-approval via WHITESPACE (' life')" refuse "$(mkbus "$(msg ' life'    APPROVE "$CMD")")"
try "C3 a non-Core sender ('review-bot')"       refuse "$(mkbus "$(msg review-bot APPROVE "$CMD")")"
try "C4 a pull via 'make pull'"                 refuse "$(mkbus "$(msg business   APPROVE 'make pull')")"
try "C5 a pull via an unlisted wrapper"         refuse "$(mkbus "$(msg business   APPROVE 'bash ./pull-link')")"

echo
echo "=== Codex 2026-08-09 — replay via an equivalent command spelling ==="
# The matcher normalised quotes/case/whitespace but the replay ledger hashed the RAW string, so one
# approval minted once per SPELLING. Both of these normalise identically and must share a ledger.
EQBUS="$(mkbus "$(msg business APPROVE "$CMD")")"
HOME="$EQBUS" CORE_BUS_NAME=life CLAUDE_PROJECT_DIR="$WORK" \
  bash "$WORK/.claude/hooks/sentinel-approve.sh" "$CMD" >/dev/null 2>&1
first=$?
HOME="$EQBUS" CORE_BUS_NAME=life CLAUDE_PROJECT_DIR="$WORK" \
  bash "$WORK/.claude/hooks/sentinel-approve.sh" 'bash "bin/sync-to-baseline.sh"' >/dev/null 2>&1
second=$?
if [[ $first -eq 0 && $second -ne 0 ]]; then
  printf '  PASS  C6 requoted command cannot re-mint the same approval\n'; PASS=$((PASS + 1))
else
  printf '  FAIL  C6 re-mint via requoting (first=%s second=%s)\n' "$first" "$second"; FAIL=$((FAIL + 1))
fi

echo
echo "=== and only then, the accept ==="
# CLEAR THE REPLAY LEDGER, and only here. C6 above deliberately mints with this same command, and
# the ledger is per-command-hash in the shared scratch state dir — so without this A1 is refused as
# a genuine replay and looks like a regression in the accept path. Verified in isolation before
# adding this line, rather than assuming the comfortable explanation: a fresh state dir accepts the
# same approval at exit 0.
#
# Scoped to this phase ON PURPOSE. A2 must still see A1's ledger entry, because "the same approval
# reused is refused" is the property it exists to prove. Clearing per-case would silently delete
# that test's subject and it would pass while checking nothing.
rm -f "$WORK/.claude/state/.peer-approval-consumed-"* 2>/dev/null || true
GOODBUS="$(mkbus "$(msg business APPROVE "$CMD")")"
try "A1 genuine peer APPROVE authorises"       allow  "$GOODBUS"
try "A2 the SAME approval reused is refused"   refuse "$GOODBUS"

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]]
