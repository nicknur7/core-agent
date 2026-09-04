#!/usr/bin/env bash
# test_sentinel_approval_chain.sh — a Sentinel approval token requires Sentinel to have approved.
#
# WHY THIS EXISTS
# ---------------
# Until 2026-07-27 sentinel-approve.sh did `touch` on a file named sha256(command), and
# pretooluse-guard.sh checked only that the file existed. Any Bash call minted one, so the
# gate protecting every outward action could be satisfied by the agent it was guarding.
# This Core did it twice in one session: once deliberately to demonstrate the hole, once
# in good faith after deciding a review was unnecessary. The guard could not tell those
# apart because there was nothing to tell apart.
#
# WHAT THIS PROVES, AND WHAT IT DOES NOT
# ---------------------------------------
# It proves the chain: no receipt -> no token; a BLOCK or ASK receipt -> no token; an
# APPROVE receipt -> exactly one token; and the receipt is consumed so it cannot approve
# a second command.
#
# It does NOT prove security. An agent with Bash under this UID can write a receipt file
# directly. No local mechanism survives that, and the code comments say so. This is an
# accident-and-drift barrier: it makes skipping review require deliberate forgery rather
# than a judgement call. That is the failure that actually happened.
#
# Run: bash bin/tests/test_sentinel_approval_chain.sh
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
_fails=()
check() { if [[ "$2" == "$3" ]]; then printf "  PASS  %s\n" "$1"
  else printf "  FAIL  %s  (expected=%s actual=%s)\n" "$1" "$2" "$3"; _fails+=("$1"); fi; }

# Sandbox: the real hooks, an empty state dir, and sentinel-approve under a different name
# so pretooluse-guard's exact-invocation whitelist does not intercept the test itself.
mkdir -p "$TMP/.claude/hooks/lib" "$TMP/.claude/state" "$TMP/memory"
cp "$REPO/.claude/hooks/sentinel-approve.sh" "$TMP/.claude/hooks/mint.sh"
cp "$REPO/.claude/hooks/sentinel-receipt.sh" "$TMP/.claude/hooks/"
cp "$REPO/.claude/hooks/lib/hookinvoke.sh" "$TMP/.claude/hooks/lib/" 2>/dev/null || true

receipt() { # verdict, agent_type, [reviewed command -> emits the modern REVIEWED-first form]
  # TWO SHAPES, BOTH REAL, AND THE TEST NEEDS BOTH.
  #
  # Without a third argument this emits the TRANSITIONAL form: bare verdict on line 1, no REVIEWED
  # line. That is not a legacy artefact — sentinel-approve.sh:387 deliberately falls back to
  # searching report_text when `reviewed_command` is empty, because agent specs load at SESSION
  # START and a reviewer in a session that began before the contract shipped cannot emit the line.
  # Refusing there would wedge every Core until it restarts. It is logged to .binding-fallback.log
  # so the retirement is earned by evidence, and on this Core it has fired three times.
  #
  # I nearly deleted that path: I read the WRITER, saw reviewed_command='', concluded "this receipt
  # can never authorise anything", and built a decline for it. It CAN authorise, via the fallback.
  # Patching this fixture took the suite from 2 failures to 6, which is what made me check the
  # premise instead of the code.
  #
  # With a third argument it emits the MODERN form: `REVIEWED: <cmd>` as the FIRST non-blank line,
  # parsed positionally by sentinel-receipt.sh, with the verdict marker last.
  if [[ -n "${3:-}" ]]; then
    printf '{"agent_type":"%s","session_id":"t","last_assistant_message":"REVIEWED: %s\\nReview body.\\nVERDICT: %s"}' "$2" "$3" "$1" \
      | CORE_INSTANCE="$TMP" bash "$TMP/.claude/hooks/sentinel-receipt.sh" >/dev/null 2>&1
    return
  fi
  # Verdict on LINE 1 — see the note on vparse below. Prepending "Review body." put it on line
  # 3, which an approval is deliberately no longer recoverable from.
  printf '{"agent_type":"%s","session_id":"t","last_assistant_message":"%s\\n\\nReview body."}' "$2" "$1" \
    | CORE_INSTANCE="$TMP" bash "$TMP/.claude/hooks/sentinel-receipt.sh" >/dev/null 2>&1
}
mint() { ( cd "$TMP" && bash .claude/hooks/mint.sh "$1" >/dev/null 2>&1 && echo MINTED || echo REFUSED ); }
tokens() { ls "$TMP/.claude/state/".sentinel-approved-* 2>/dev/null | grep -cv provenance || echo 0; }

echo "=== the chain ==="
check "no receipt -> refused"                 REFUSED "$(mint 'git push origin main')"

receipt BLOCK sentinel
check "a BLOCK receipt is not an approval"    REFUSED "$(mint 'git push origin main')"

receipt ASK sentinel
check "an ASK receipt is not an approval"     REFUSED "$(mint 'git push origin main')"

# THE MODERN FORM. This assertion had been failing since 952678d and read as a broken trust root;
# the fixture encoded the pre-952678d contract, writing an APPROVE with no command anywhere, so the
# mint correctly refused. The mechanism was never broken.
receipt APPROVE sentinel 'git push origin main'
check "an APPROVE receipt mints a token"      MINTED  "$(mint 'git push origin main')"
check "exactly one token exists"              "1"     "$(tokens)"

check "the receipt is consumed (one-time)"    REFUSED "$(mint 'curl https://evil.example')"

echo
echo "=== receipt minting is fail-closed on agent identity ==="
before=$(ls "$TMP/.claude/state/".sentinel-receipt-*.json 2>/dev/null | wc -l | tr -d ' ')
# The matcher in settings.json is configuration, not authentication. An unrelated subagent
# terminating must not mint — this is the 2026-07-18 forgery lesson applied here.
receipt APPROVE general-purpose
after=$(ls "$TMP/.claude/state/".sentinel-receipt-*.json 2>/dev/null | wc -l | tr -d ' ')
check "a non-Sentinel subagent cannot mint a receipt" "$before" "$after"

receipt APPROVE sentinel-code
after2=$(ls "$TMP/.claude/state/".sentinel-receipt-*.json 2>/dev/null | wc -l | tr -d ' ')
check "sentinel-code CAN mint (it reviews baseline syncs)" "$((before + 1))" "$after2"

echo
echo "=== a stale review cannot approve a later command ==="
rm -f "$TMP/.claude/state/".sentinel-receipt-*.json "$TMP/.claude/state/".sentinel-approved-*
receipt APPROVE sentinel
# Backdate the receipt past the TTL: a review from an hour ago never saw this command.
python3 - "$TMP" <<'PY'
import glob, json, os, sys, time
for p in glob.glob(os.path.join(sys.argv[1], ".claude/state/.sentinel-receipt-*.json")):
    r = json.load(open(p)); r["ts"] = int(time.time()) - 99999
    json.dump(r, open(p, "w"))
PY
check "an expired receipt is refused" REFUSED "$(mint 'git push origin main')"

echo
echo "=== the verdict parser accepts the formats the agent ACTUALLY writes ==="
# The first parser matched only a bare "APPROVE" line. sentinel-code writes "APPROVE",
# "`BLOCK`", "Verdict: **APPROVE**" depending on the review — so the hook fired 13 times in
# one session and minted ZERO receipts. Every real review silently failed to produce the
# approval it had earned, and the only receipts that ever existed were this test's own
# fixtures. A parser that only passes its own fixtures is not a parser; these are the real
# observed formats.
vparse() { # message -> verdict or NONE
  rm -f "$TMP/.claude/state/".sentinel-receipt-*.json 2>/dev/null
  # Verdict on LINE 1, which is what sentinel.md:112 and sentinel-code.md:148 require and what
  # real reviewers emit. This fixture used to prepend "body\n\n", putting the verdict on line 3.
  # That encoded the assumption that a verdict may appear anywhere in the report — the exact
  # assumption behind five broken revisions of the parser, including two that MINTED APPROVALS
  # FOR REVIEWS THAT REFUSED. An approval is now only recoverable from line 1.
  printf '{"agent_type":"sentinel","session_id":"t","last_assistant_message":"%s\\n\\nbody"}' "$1" \
    | CORE_INSTANCE="$TMP" bash "$TMP/.claude/hooks/sentinel-receipt.sh" >/dev/null 2>&1
  cat "$TMP/.claude/state/".sentinel-receipt-*.json 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('verdict','NONE'))" 2>/dev/null || echo NONE
}
check "bare APPROVE"                 APPROVE "$(vparse 'APPROVE')"
check "Verdict: **APPROVE** (real)"  APPROVE "$(vparse 'Verdict: **APPROVE**')"
check "backticked BLOCK (real)"      BLOCK   "$(vparse '`BLOCK`')"
check "bolded ASK"                   ASK     "$(vparse '**ASK**')"
check "a verdict word MENTIONED is not a verdict" NONE \
      "$(vparse 'this would be BLOCK if the diff had X')"

echo
echo "=== there is no self-service trust-root path ==="
# A --human-confirmed flag existed for about an hour and was removed. See the long
# note in sentinel-approve.sh: no quote-matching scheme can separate "approved THIS
# command" from "said a short thing recently", because Nick's real approvals are
# short. A trust-root push is a command he runs himself.
check "the --human-confirmed flag no longer exists" "REFUSED" "$( cd "$TMP" && bash .claude/hooks/mint.sh --human-confirmed "git push" "anything" >/dev/null 2>&1 && echo MINTED || echo REFUSED )"

echo
if (( ${#_fails[@]} )); then printf "FAILURES (%d): %s\n" "${#_fails[@]}" "${_fails[*]}"; exit 1; fi
echo "ALL PASS"
