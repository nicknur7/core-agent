#!/usr/bin/env bash
# pretooluse-guard.sh — the codex danger-full-access fence must block INVOCATIONS and ignore
# QUOTED OCCURRENCES.
#
# WHY THIS FILE EXISTS, AND WHY IT IS A FILE.
# The fence is categorical: its own message says "No Sentinel token can approve this." Until
# 2026-08-04 it required only that the word `codex` appear SOMEWHERE in the command, then matched
# the escalation flags anywhere else in the same string. That made it impossible to regression-test
# by anything it governs — to prove it blocks the flag you must write the flag, and writing it
# blocked you. core-school hit exactly that running a read-only harness where the flag was quoted
# JSON test data being piped TO the guard, and completed its review only by DELETING the test case.
# So the highest-severity gate in the system was also the one branch nobody could verify still
# worked, which is the defect test_pipeline_exhaust_filter exists to name, in the worst place.
#
# The payloads live in this file rather than inline in a shell command on purpose: a test that
# cannot be written down is a test that does not get written. Running it is `bash <this file>`,
# which contains no escalation flag at all.
#
#   bash .claude/hooks/tests/test-codex-fence.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
GUARD="$REPO/.claude/hooks/pretooluse-guard.sh"
FAILS=0

# Assembled at runtime so the literal never appears as one token in this file either — belt and
# braces for any future scanner that greps the repo for it.
DANGER="--dangerously-bypass-approvals-and-sandbox"
DANGER2="--sandbox danger-full-access"
DANGER3="-sdanger-full-access"        # attached short value — clap parses it; fence missed it
DANGER4="--yolo"                      # hidden bypass alias, absent from `codex exec --help`
DANGER5="--dangerously-bypass-hook-trust"

run_guard() {  # $1 = command string → echoes the guard's exit code
  printf '%s' "$1" | python3 -c '
import json,sys
print(json.dumps({"session_id":"fence-test","transcript_path":"/tmp/x.jsonl","cwd":"'"$REPO"'",
                  "hook_event_name":"PreToolUse","tool_name":"Bash",
                  "tool_input":{"command":sys.stdin.read()}}))' \
    | bash "$GUARD" >/dev/null 2>&1
  echo $?
}

check() {  # $1 = label, $2 = command, $3 = expected: BLOCK | ALLOW
  local rc; rc=$(run_guard "$2")
  local got; [[ "$rc" == "2" ]] && got="BLOCK" || got="ALLOW"
  if [[ "$got" == "$3" ]]; then
    echo "  PASS  $1"
  else
    echo "  FAIL  $1 — wanted $3, got $got (rc=$rc)"
    FAILS=$((FAILS+1))
  fi
}

echo "codex fence — invocations block"
check "bare codex + escalation flag"            "codex exec $DANGER"                         BLOCK
check "escalation flag QUOTED but still an arg" "codex exec \"$DANGER\""                     BLOCK
check "sandbox danger-full-access"              "codex exec $DANGER2"                        BLOCK
check "absolute path to codex"                  "/opt/homebrew/bin/codex exec $DANGER"       BLOCK
check "env assignment before codex"             "FOO=1 codex exec $DANGER"                   BLOCK
check "codex in a later clause"                 "cd /tmp && codex exec $DANGER"              BLOCK
check "companion runtime"                       "node scripts/codex-companion.mjs task $DANGER" BLOCK

echo
echo "codex fence — ESCALATION VECTORS THE PATTERN NEVER COVERED (found 2026-08-05)"
# Both of these exited 0 on this fence AND on the baseline before it — they were never covered by
# any version. Found by interrogating codex-cli 0.144.1 rather than re-reading the regex.
check "attached short value -sVALUE"            "codex exec $DANGER3 'x'"                    BLOCK
check "attached short value, quoted"            "codex exec -s'danger-full-access' 'x'"      BLOCK
check "hidden --yolo alias"                     "codex exec $DANGER4 'x'"                    BLOCK
check "--yolo under a wrapper"                  "sudo codex exec $DANGER4 'x'"               BLOCK
check "bypass-hook-trust"                       "codex exec $DANGER5 'x'"                    BLOCK
check "-c config override"                       "codex -c sandbox_mode=danger-full-access exec 'x'" BLOCK
check "--config override, quoted value"         "codex exec --config sandbox_mode=\"danger-full-access\" 'x'" BLOCK
# Must NOT over-block: `-y` is a near-universal assume-yes flag and is deliberately not a vector.
check "unrelated -y stays allowed"              "apt-get -y install ripgrep"                 ALLOW
check "yolo as a bare word, not a flag"         "git commit -m 'yolo shipped it'"            ALLOW

echo
echo "codex fence — PROCESS WRAPPERS (the 2026-08-05 fail-open regression)"
# The first version of the clause-scoped fence matched the command word POSITIVELY against
# {codex,node} and skipped the clause otherwise, so every wrapper absent from its strip-list was a
# silent bypass. All four of these exited 0 under that version. `firejail` is the important one: it
# is deliberately NOT in the strip-list, and must still block — proving the fence no longer depends
# on a complete list of wrappers.
check "sudo"                                    "sudo codex exec $DANGER"                    BLOCK
check "sudo with flags"                         "sudo -E -u nick codex exec $DANGER"         BLOCK
check "env -i"                                  "env -i codex exec $DANGER"                  BLOCK
check "timeout with duration"                   "timeout 30 codex exec $DANGER"              BLOCK
check "nice"                                    "nice -n5 codex exec $DANGER"                BLOCK
check "nested wrappers"                         "sudo -E env -i timeout 5m codex exec $DANGER" BLOCK
check "wrapper NOT in the strip-list"           "firejail codex exec $DANGER"                BLOCK
check "command substitution"                    "OUT=\$(codex exec $DANGER)"                 BLOCK
check "subshell"                                "( codex exec $DANGER )"                     BLOCK

echo
echo "codex fence — 'inert' commands that can actually EXEC (Codex found these)"
# The inert allowlist is the fence's own attack surface: an entry that can spawn a process
# exculpates a real launch. All five of these were ALLOW on the first draft of the list and had been
# BLOCKED by the older flag-anywhere fence, i.e. the list was itself a weakening. Any future
# addition to _cx_inert must come here first.
check "awk system()"                            "awk 'BEGIN { system(\"codex exec $DANGER x\") }'" BLOCK
check "awk print into a shell"                  "awk 'BEGIN { print \"codex exec $DANGER x\" | \"/bin/sh\" }'" BLOCK
check "GNU sed e command"                       "sed -e 'e codex exec $DANGER x' /dev/null"  BLOCK
check "sort --compress-program"                 "sort --compress-program='codex exec $DANGER' /dev/null" BLOCK
check "less shell escape"                       "less '+!codex exec $DANGER x' /dev/null"    BLOCK
check "more shell escape"                       "more '+!codex exec $DANGER x' /dev/null"    BLOCK

echo
echo "codex fence — SHELL QUOTE CONCATENATION reassembles the flag (Codex found these)"
# The shell joins adjacent fragments, so these reach codex as the exact flag while a raw-text regex
# sees quotes interrupting the literal. Detection now also runs against a quote/backslash-stripped
# copy of the clause; the command word is still resolved from the untouched clause, so the school
# case below still passes.
check "quote splits the flag"                   "codex exec --dangerously-bypass-approvals-and-\"sandbox\" x" BLOCK
check "quoted = separator"                      "codex exec --sandbox\"=danger-full-access\" x" BLOCK
check "two quoted fragments"                    "codex exec '--dangerously'\"-bypass-approvals-and-sandbox\" x" BLOCK
check "empty-string splice"                     "codex exec --dangerously-bypass''-approvals-and-sandbox x" BLOCK
check "backslash splice"                        "codex exec --dangerously-bypass\\-approvals-and-sandbox x" BLOCK

echo
echo "codex fence — quoted occurrences are DATA, not invocations (the school case)"
check "flag as echoed test payload"             "echo '{\"cmd\":\"$DANGER\"}' | bash $GUARD"  ALLOW
check "flag inside a grep pattern"              "grep -n '$DANGER' .claude/hooks/pretooluse-guard.sh" ALLOW
check "writing the flag into a test file"       "printf '%s' '$DANGER' > /tmp/fence-fixture.txt" ALLOW
check "flag in a cat heredoc-ish read"          "cat /tmp/fence-fixture.txt # $DANGER"        ALLOW
# Deliberately BLOCK, changed from ALLOW on 2026-08-05. `git` is not exculpated: `git -c
# alias.x='!codex exec …' x` executes arbitrary commands, so putting git on the inert list would
# reopen the hole this fence just closed. Cost of the false positive is rephrasing a commit
# message; cost of the hole is the fleet's highest-severity gate. Same reasoning excludes python3
# (`python3 -c` can os.system), which is why run_guard's payload builder is the only python here
# and why it lives inside this file rather than in the command line that invokes it.
check "flag in a commit message (fail-closed)"  "git commit -m 'document why $DANGER is fenced'" BLOCK

echo
echo "codex fence — safe codex usage still allowed"
check "read-only sandbox"                       "codex exec --sandbox read-only -C . 'review'" ALLOW
check "workspace-write"                         "codex exec --sandbox workspace-write 'fix'"   ALLOW
check "deprecated --full-auto (maps to safe)"   "codex exec --full-auto 'scaffold'"            ALLOW

# KNOWN, ACCEPTED LIMIT — deliberately NOT asserted as a passing case, because asserting ALLOW would
# read as sanctioning it. Codex raised it and is right that it cannot be fixed here:
#
#   printf '%s' 'codex exec <flag> x' | tee /tmp/l.sh >/dev/null; bash /tmp/l.sh
#
# The flag literal lives only in the inert printf clause; the clause that executes carries no flag.
# Closing it needs dataflow tracking from written file to later execution, which no regex over a
# single command string can do. This is not specific to the codex fence — the force-push fence, the
# rm -rf checks and every other string matcher in this guard fall to the same shape. The guard's
# real job is to stop the direct, plausible, one-line mistake; an agent that writes a launcher
# script and runs it has left the class of thing a PreToolUse string matcher can see. Documented so
# nobody mistakes silence here for coverage.

echo
if [[ $FAILS -eq 0 ]]; then echo "=== codex fence: all checks pass ==="; exit 0; fi
echo "=== codex fence: $FAILS FAILED ==="; exit 1
