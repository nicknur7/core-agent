#!/usr/bin/env bash
# test_fresh_spawn_hooks.sh — acceptance test: is the HOOK / ENFORCEMENT layer actually LIVE
# for a stranger who clones nicknur7/core-agent and opens Claude, or does it just look wired?
#
# WHY THIS EXISTS
# ---------------
# Measured 2026-08-31: a fresh clone of the published baseline registered 11 of 44 managed
# hooks, and sync-from-baseline@SessionStart — the one hook that would have let the Core
# self-heal on first open — was itself among the 33 missing. settings.json is per_core_keep,
# so no sync path ever refreshed it; it was hand-committed 2026-05-19 and never touched again
# while bin/hook-registry.json grew from ~12 entries to 58. The fix (commit 343cb59, this repo):
# reconcile-hooks.py gains --emit-template, and sync-to-baseline.sh now calls it on every push
# so the baseline's template settings.json can no longer drift from the registry it is derived
# from. That fix is committed HERE and has not been pushed to nicknur7/core-agent yet — this test
# proves what the NEXT push will publish, without performing the push.
#
# WHAT "READY" MEANS
# -------------------
# A stranger clones the repo, opens Claude, and the enforcement layer is actually live: not
# 11 of 44 hooks, not a Sentinel gate that is absent from settings.json, not a hook that
# tracebacks the moment it is invoked on a Core with no memory/, no sessions/, no state, and
# no configured identity.
#
# WHAT THIS SCRIPT DOES
# ----------------------
#   1. Clones the PUBLISHED baseline (nicknur7/core-agent), read-only, into a throwaway tmp dir.
#   2. Overlays this Core's own bin/ and .claude/hooks/ on top — the two shared dirs from
#      sync-manifest.json that determine hook registration/existence/behaviour — so the test
#      exercises today's committed fix rather than what is live on GitHub right now.
#   3. Locally simulates sync-to-baseline.sh's new emit-template step (no git commit, no push,
#      no network write) to produce the settings.json a real push would generate.
#   4. Runs the 7 assertions below against that clone (and a further-stripped copy for #5).
#
# SAFETY: never touches core-life, core-business, core-school, core-finance, core-ops, or any
# live database. No git commit/push anywhere. No outward action (git push / send / curl) is
# ever actually EXECUTED — assertion #6 only feeds the guard a command STRING to classify.
# Self-cleaning: everything lives under one mktemp dir, removed on exit.
#
# Run: bash bin/tests/test_fresh_spawn_hooks.sh

set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
BASELINE_REPO_URL="https://github.com/nicknur7/core-agent.git"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/fresh-spawn-hooks.XXXXXX")
# Canonicalize to the realpath (macOS: $TMPDIR is under /var/folders/..., a symlink to
# /private/var/folders/...). A spawned subprocess's argv/cwd always shows the REAL path, so
# matching "$TMP" against `ps`/`pgrep` output with the symlinked form silently never matches —
# found by running this test once and watching killed-on-cleanup survivors keep surviving.
TMP=$(cd "$TMP" && pwd -P)
cleanup() {
  # Belt-and-suspenders on top of the python-side SAFETY_SWEEP in assertion #5: kill anything
  # still alive that references this run's tmp dir (e.g. a detached nohup child a future hook
  # change might add) before removing it, so `rm -rf` never races a live writer.
  local pids
  pids=$(pgrep -f "$TMP" 2>/dev/null || true)
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
CLONE="$TMP/clone"       # realistic fresh-clone state — used for assertions 1,2,3,4,6,7
SPAWN="$TMP/spawn"       # CLONE further stripped of memory/sessions/state/role — assertion 5 only
FAKE_HOME="$TMP/fake-home"
mkdir -p "$FAKE_HOME"

_fails=()
_pass_n=0
pass() { printf "  PASS  %s\n" "$1"; _pass_n=$((_pass_n + 1)); }
fail() { printf "  FAIL  %s -- %s\n" "$1" "$2"; _fails+=("$1"); }

echo "================================================================"
echo " test_fresh_spawn_hooks.sh — hook/enforcement layer acceptance"
echo "================================================================"
echo
echo "=== setup ==="
echo "[1/4] cloning $BASELINE_REPO_URL (read-only, depth 1) -> $CLONE"
if ! git clone --depth 1 --quiet "$BASELINE_REPO_URL" "$CLONE" 2>"$TMP/clone.err"; then
  # NO NETWORK IS NOT A DEFECT IN THE HOOK/ENFORCEMENT LAYER — it is an absent dependency this
  # acceptance test cannot run without (a fresh clone of the PUBLISHED baseline). A dependency-poor
  # environment (no network reachable at all) must SKIP this, not FAIL it: there is nothing here
  # for the fresh-spawn hook check to have gotten wrong.
  echo "  SKIP  network unavailable (git clone failed): fresh-clone hook/enforcement-layer acceptance needs a real clone of $BASELINE_REPO_URL"
  cat "$TMP/clone.err" >&2
  exit 0
fi

echo "[2/4] overlaying shared bin/ + .claude/hooks/ from core-life onto the clone"
# These are the two entries in bin/sync-manifest.json's shared.dirs that determine hook
# registration (bin/hook-registry.json, bin/reconcile-hooks.py, bin/sync-to-baseline.sh) and
# hook existence/behaviour (.claude/hooks/**). Mirrors exactly what sync-to-baseline.sh's rsync
# step copies for these two dirs, including its per_core_keep exclusion of bin/.gate-trusted-sha.
# --delete additionally drops files retired from core-life's copy (e.g. bin/init-brain.sh,
# bin/sync-from-engine.sh — both tombstoned in sync-manifest.json's "retired" list) so the
# overlay reflects the fully-converged post-cleanup state; none of those files are referenced
# by hook-registry.json so this has no bearing on the assertions below.
OVERLAID=(
  "bin/  (whole dir; excludes __pycache__, *.pyc, bin/.gate-trusted-sha per_core_keep)"
  ".claude/hooks/  (whole dir; excludes __pycache__, *.pyc)"
)
rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' --exclude='.gate-trusted-sha' \
  "$REPO/bin/" "$CLONE/bin/"
rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' \
  "$REPO/.claude/hooks/" "$CLONE/.claude/hooks/"

echo "[3/4] simulating sync-to-baseline.sh's emit-template push step (NO git add/commit/push)"
EMIT_OUT=$(python3 "$CLONE/bin/reconcile-hooks.py" --emit-template "$CLONE/.claude/settings.json" \
  --registry "$CLONE/bin/hook-registry.json" --role puller 2>&1)
EMIT_RC=$?
echo "    $EMIT_OUT"
if [[ $EMIT_RC -ne 0 ]]; then
  echo "FATAL: emit-template failed — cannot proceed with assertions." >&2
  exit 1
fi

echo "[4/4] building the further-stripped SPAWN copy for assertion #5"
cp -a "$CLONE" "$SPAWN"
rm -rf "${SPAWN:?}/memory" "${SPAWN:?}/sessions"
mkdir -p "$SPAWN/.claude/state"
find "$SPAWN/.claude/state" -mindepth 1 -delete
# bin/tests/ removed from SPAWN ONLY (found by running this test once): defensive-save.sh
# (SessionEnd) unconditionally calls session-lifecycle.sh's close-defensive flow, which —
# real, intended, unrelated to today's fix — detach-launches `bin/tests/run-all.sh` as a
# self-test safety net. Leaving it in place means invoking defensive-save.sh here would fork
# a SECOND full test run (including this very test file, freshly overlaid into bin/tests/),
# which forks a THIRD, unbounded. Removing it only from the disposable SPAWN copy makes
# defensive-save.sh take its real "no run-all.sh present" skip branch instead — it is still
# invoked for real and still must not crash, it just can't recurse into itself.
rm -rf "${SPAWN:?}/bin/tests"
python3 - "$SPAWN/.claude/identity.json" <<'PY'
# Strip hook_profile so identity.json is "unconfigured" — the harsher condition the acceptance
# spec names explicitly, distinct from the realistic role=puller template CLONE ships with.
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d.pop("hook_profile", None)
json.dump(d, open(p, "w"), indent=2)
PY

echo
echo "Files overlaid from core-life onto the fresh nicknur7/core-agent clone:"
for o in "${OVERLAID[@]}"; do echo "  - $o"; done
echo

# ---------------------------------------------------------------------------------------------
# Shared python helper for assertions #4 and #5 — parses settings.json's hook commands once.
# ---------------------------------------------------------------------------------------------
cat > "$TMP/hookcheck.py" <<'PYEOF'
import sys, os, json, re, shlex, subprocess

MODE = sys.argv[1]                       # "existence" | "invoke"
CORE = sys.argv[2]
FAKE_HOME = sys.argv[3] if len(sys.argv) > 3 else ""

settings = json.load(open(os.path.join(CORE, ".claude", "settings.json")))

def iter_registrations():
    for event, blocks in settings.get("hooks", {}).items():
        for block in blocks:
            matcher = block.get("matcher", "") or ""
            for h in block.get("hooks", []):
                yield event, matcher, h.get("command", ""), h.get("timeout") or 10

def resolve(cmd):
    expanded = cmd.replace("$CLAUDE_PROJECT_DIR", CORE)
    try:
        toks = shlex.split(expanded)
    except ValueError:
        return None, None, expanded
    if not toks:
        return None, None, expanded
    interp, idx = None, 0
    if toks[0] in ("python3", "bash", "sh"):
        interp, idx = toks[0], 1
    if idx >= len(toks):
        return None, interp, expanded
    return toks[idx], interp, expanded

fails = []

if MODE == "existence":
    seen, checked = set(), []
    for event, matcher, cmd, _ in iter_registrations():
        path, interp, _ = resolve(cmd)
        if not path or path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            fails.append(f"{event}: `{cmd}` -> resolved file MISSING: {path}")
            continue
        if interp is None and not os.access(path, os.X_OK):
            fails.append(f"{event}: `{cmd}` -> exists but NOT EXECUTABLE "
                          f"(invoked with no interpreter prefix, needs +x): {path}")
        checked.append(path)
    print(f"CHECKED {len(checked)} unique hook files referenced from settings.json")
    for f in fails:
        print(f"BAD: {f}")
    sys.exit(1 if fails else 0)

elif MODE == "invoke":
    def payload_for_matcher(m):
        if not m or m == "*":
            return "Bash", {"command": "echo hello"}
        if m.startswith("mcp__"):
            if "peer-" in m:          return "mcp__peer-business__peer_read", {"query": "test"}
            if "apple-events" in m:   return "mcp__apple-events__calendar_events", {"action": "read"}
            if "github" in m:        return "mcp__github__get_file_contents", {"owner": "x", "repo": "y", "path": "z"}
            if "Canva" in m:         return "mcp__claude_ai_Canva__list-folder-items", {}
            if "Google_Drive" in m:  return "mcp__claude_ai_Google_Drive__list_recent_files", {}
            if "playwright" in m:    return "mcp__playwright__browser_navigate", {"url": "about:blank"}
            return "mcp__test__probe", {}
        first = m.split("|")[0]
        if first in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return first, {"file_path": os.path.join(CORE, "tmp_acceptance_test_file.md"),
                            "content": "test", "old_string": "a", "new_string": "b"}
        if first in ("Task", "Agent", "Workflow"):
            return first, {"prompt": "test task", "subagent_type": "general-purpose", "description": "test"}
        if first in ("Grep", "Glob"):
            return first, {"pattern": "test", "path": CORE}
        if first == "Skill":
            return "Skill", {"skill": "test-skill"}
        return (first or "Bash"), {"command": "echo hello"}

    BASE = {"session_id": "acceptance-test-session",
            "transcript_path": "/nonexistent/transcript.jsonl", "cwd": CORE}

    EVENT_EXTRA = {
        # "resume", not "startup"/"clear" — deliberately. session-start-check.sh gates several
        # REAL maintenance side-jobs (a `nohup python3 learned-corpus-miner.py --detect &`
        # against corebrain, a brain_status.py connect, reconcile-inventory.py capture,
        # retire-legacy.py --status) on `source in (startup, clear)` specifically because they
        # must not fire on every turn — only on a genuine cold boot. "resume" is an equally real
        # SessionStart value (fires on e.g. a compact) and exercises the hook's own crash path
        # without tripping jobs that exist to run once per boot, not once per smoke-test.
        "SessionStart": {"source": "resume"},
        "UserPromptSubmit": {"prompt": "hello, this is an acceptance test prompt"},
        "Stop": {"stop_hook_active": False},
        "SessionEnd": {"reason": "other"},
        "SubagentStop": {"agent_type": "sentinel", "last_assistant_message": "VERDICT: APPROVE\nReview body."},
        "PostToolBatch": {},
        "PostToolUseFailure": {"tool_name": "Bash", "error": "test error"},
        "PostCompact": {"trigger": "manual"},
        "PreCompact": {"trigger": "manual"},
        "InstructionsLoaded": {},
        "UserPromptExpansion": {"prompt": "hello"},
        "SubagentStart": {"agent_type": "sentinel"},
        "StopFailure": {},
        "Notification": {"message": "test notification"},
        "MessageDisplay": {"message": "test reply chunk", "delta": "test reply chunk",
                            "index": 0, "final": True, "turn_id": "t1"},
    }

    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = CORE
    env["CORE_INSTANCE"] = CORE
    if FAKE_HOME:
        env["HOME"] = FAKE_HOME
    # SANDBOX every Postgres client (pg_dump, psycopg2) so a hook that unconditionally reaches
    # for corebrain — e.g. defensive-save.sh -> session-lifecycle.sh close defensive ->
    # nohup bash brain-backup.sh & (a REAL detached pg_dump against the LIVE corebrain database,
    # found by actually running this test once) — fails fast and harmlessly instead of touching
    # production. libpq honours PGHOST/PGDATABASE and psycopg2.connect(dbname=...) does too
    # (brain-recall-trigger.py, brain-backup.sh's `${COREBRAIN_DB:-corebrain}`), so this one
    # override neutralises every current and future caller without needing to enumerate them.
    # Every such call is already wrapped in try/except or `pg_dump ... || echo FAILED` by design
    # (see subagent characterization), so a fast connection failure is exactly the code path
    # these scripts already treat as normal degradation — this does not create a new crash risk.
    env["PGHOST"] = "/nonexistent-sandboxed-pg-socket-for-acceptance-test"
    env["PGDATABASE"] = "sandboxed_no_such_db_acceptance_test"
    env["COREBRAIN_DB"] = "sandboxed_no_such_db_acceptance_test"
    env["PGCONNECT_TIMEOUT"] = "1"

    # NOT EXERCISED LIVE, AND WHY (see deliverable's "name explicitly any hook you could not
    # exercise" requirement). defensive-save.sh (SessionEnd) unconditionally calls
    # session-lifecycle.sh's real `close defensive` flow — it has no sentinel-file gate the way
    # stop-hook.sh does, by design (SessionEnd must always attempt a save). That flow performs
    # REAL production-shaped work: `git add -A` + commit, a detached `nohup pg_dump` against
    # corebrain (brain-backup.sh), several more `_detach_guarded` background jobs (grade-gate,
    # estate-sweep, brain-export, steering-compress), and — found by actually running this test
    # once — a detached re-invocation of bin/tests/run-all.sh, which would recurse into this very
    # test file. Running it for real is not a "does the hook crash" check any more, it is a
    # production close, and this repo's OWN test suite already refuses to do that: see
    # bin/tests/test_safety_scan_internal_fault.sh ("Source only the two functions under test,
    # so this cannot run a real close as a side effect."). Same principle, applied here instead
    # of fighting the cascade with process-group tricks: verified by static means only —
    # `bash -n` (no syntax error) plus the characterization already done for this file (guarded
    # `[ ! -t 0 ]` stdin read with a `|| echo` fallback, no unguarded field access) — not by a
    # live invocation.
    STATIC_ONLY = {"defensive-save.sh"}
    static_checked = []
    for base in sorted(STATIC_ONLY):
        for event, matcher, cmd, timeout in iter_registrations():
            path, interp, expanded = resolve(cmd)
            if path and os.path.basename(path) == base:
                r = subprocess.run(["bash", "-n", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if r.returncode != 0:
                    fails.append(f"{base} @ {event}: bash -n reported a SYNTAX ERROR — "
                                 f"{r.stderr.decode(errors='replace')[:300]!r}")
                else:
                    static_checked.append(f"{base} @ {event}")
                break

    seen_pairs, n_ok, n_skip = set(), 0, 0
    for event, matcher, cmd, timeout in iter_registrations():
        path, interp, expanded = resolve(cmd)
        if not path:
            continue
        if os.path.basename(path) in STATIC_ONLY:
            continue
        key = (path, event)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        if not os.path.isfile(path):
            n_skip += 1
            continue  # already reported by MODE=existence; don't double-count as a crash here

        payload = dict(BASE)
        payload["hook_event_name"] = event
        payload.update(EVENT_EXTRA.get(event, {}))
        if event in ("PreToolUse", "PostToolUse"):
            tn, ti = payload_for_matcher(matcher)
            payload["tool_name"] = tn
            payload["tool_input"] = ti
            if event == "PostToolUse":
                payload["tool_response"] = {"stdout": "ok", "stderr": "", "success": True, "exit_code": 0}

        payload_json = json.dumps(payload)
        try:
            r = subprocess.run(["/bin/bash", "-c", expanded], input=payload_json.encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=CORE, env=env, timeout=min(timeout, 8) + 2)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            fails.append(f"{os.path.basename(path)} @ {event}: TIMEOUT after {min(timeout, 8) + 2}s")
            continue
        err = r.stderr.decode(errors="replace")
        problem = None
        if "Traceback (most recent call last)" in err:
            problem = "uncaught python traceback in stderr"
        elif re.search(r'\bunbound variable\b', err):
            problem = "bash unbound-variable error (set -u tripped on fresh/empty state)"
        elif rc not in (0, 1, 2):
            problem = f"unexpected exit code {rc} (allowed: 0=allow,1=soft,2=block)"
        if problem:
            fails.append(f"{os.path.basename(path)} @ {event} (matcher={matcher!r}): {problem} "
                         f"| rc={rc} | stderr[:300]={err[:300]!r}")
        else:
            n_ok += 1

    print(f"INVOKED {n_ok + len(fails)} unique (script,event) pairs LIVE on a Core with no "
          f"memory/, no sessions/, empty .claude/state/, and no hook_profile.role — {n_ok} "
          f"clean, {n_skip} skipped (missing file, reported separately), "
          f"{len(static_checked)} checked STATICALLY ONLY (not invoked live — see "
          f"NOT_EXERCISED below)")
    for s in static_checked:
        print(f"NOT_EXERCISED (static bash -n only, no crash): {s}")
    for f in fails:
        print(f"CRASH_RISK: {f}")

    # No background-process sweep here BY DESIGN. The only registered hook that detach-launches
    # anything (defensive-save.sh -> session-lifecycle.sh close-defensive -> `nohup pg_dump`/
    # `_detach_guarded` grade-gate/estate-sweep/etc.) is deliberately in STATIC_ONLY above and
    # never invoked live, and source="resume" keeps session-start-check.sh off its
    # startup-only `nohup learned-corpus-miner.py` branch — so nothing left in this loop
    # backgrounds anything to sweep. (A polling `ps -eo pid,command` loop was tried here and
    # itself got the whole test process SIGKILLed by this sandbox's own process-enumeration
    # guard partway through — worse than the problem it was solving. The bash-level `cleanup()`
    # trap's single `pgrep -f "$TMP"` at actual exit is the belt-and-suspenders that remains.)
    sys.exit(1 if fails else 0)
PYEOF

# ===============================================================================================
echo "=== 1. reconcile-hooks.py --check reports 0 missing / 0 extra (no --apply, no sync run) ==="
CHECK_OUT=$(python3 "$CLONE/bin/reconcile-hooks.py" --check --core "$CLONE" \
  --registry "$CLONE/bin/hook-registry.json" 2>&1)
CHECK_RC=$?
echo "$CHECK_OUT" | sed 's/^/    /'
if [[ $CHECK_RC -eq 0 ]] && grep -q "no drift" <<<"$CHECK_OUT"; then
  pass "reconcile-hooks --check: 0 missing / 0 extra"
else
  fail "reconcile-hooks --check: 0 missing / 0 extra" "exit=$CHECK_RC (expected 0, 'no drift'); see output above"
fi

echo
echo "=== 2. pretooluse-guard is registered (sentinel:true, invisible to reconcile's own diff) ==="
if jq -e '[.hooks | to_entries[] | .value[] | .hooks[] | select(.command | test("pretooluse-guard\\.sh"))] | length > 0' \
     "$CLONE/.claude/settings.json" >/dev/null 2>&1; then
  N_PTU=$(jq '[.hooks | to_entries[] | .value[] | .hooks[] | select(.command | test("pretooluse-guard\\.sh"))] | length' \
     "$CLONE/.claude/settings.json")
  pass "pretooluse-guard.sh registered in settings.json ($N_PTU registration(s))"
else
  fail "pretooluse-guard.sh registered in settings.json" "no PreToolUse hook command matched pretooluse-guard.sh — a fresh Core would have NO outward-action gate"
fi

echo
echo "=== 3. sync-from-baseline is registered at SessionStart (self-heal on first boot) ==="
if jq -e '(.hooks.SessionStart // []) | map(.hooks[] | select(.command | test("sync-from-baseline\\.sh"))) | length > 0' \
     "$CLONE/.claude/settings.json" >/dev/null 2>&1; then
  pass "sync-from-baseline.sh registered at SessionStart"
else
  fail "sync-from-baseline.sh registered at SessionStart" "absent — a pull-only fresh Core could not self-heal drift on next open (the exact 343cb59 defect)"
fi

echo
echo "=== 4. every hook command in settings.json resolves to an existing, invokable file ==="
EXIST_OUT=$(python3 "$TMP/hookcheck.py" existence "$CLONE" 2>&1)
EXIST_RC=$?
echo "$EXIST_OUT" | sed 's/^/    /'
if [[ $EXIST_RC -eq 0 ]]; then
  pass "all registered hook commands resolve to existing, invokable files"
else
  fail "all registered hook commands resolve to existing, invokable files" "see BAD: lines above"
fi

echo
echo "=== 5. every hook script can be invoked without crashing on a bare fresh Core ==="
echo "    (SPAWN = clone with memory/, sessions/ removed, .claude/state emptied, identity.json"
echo "     hook_profile stripped — the harsher condition named explicitly in the test spec)"
INVOKE_OUT=$(python3 "$TMP/hookcheck.py" invoke "$SPAWN" "$FAKE_HOME" 2>&1)
INVOKE_RC=$?
echo "$INVOKE_OUT" | sed 's/^/    /'
if [[ $INVOKE_RC -eq 0 ]]; then
  pass "every hook script invoked cleanly on a bare fresh Core (no traceback, no crash)"
else
  fail "every hook script invoked cleanly on a bare fresh Core" "see CRASH_RISK: lines above"
fi

echo
echo "=== 6. pretooluse-guard actually GATES (blocks outward action, passes benign command) ==="
# NOTE: this only feeds the guard a COMMAND STRING to classify. Nothing is executed — the guard
# itself does not run git, it only pattern-matches. No real outward action occurs anywhere here.
run_guard() {
  local cmd="$1"
  CLAUDE_PROJECT_DIR="$CLONE" CORE_INSTANCE="$CLONE" HOME="$FAKE_HOME" \
    PGHOST="/nonexistent-sandboxed-pg-socket-for-acceptance-test" \
    PGDATABASE="sandboxed_no_such_db_acceptance_test" \
    COREBRAIN_DB="sandboxed_no_such_db_acceptance_test" PGCONNECT_TIMEOUT="1" \
    bash "$CLONE/.claude/hooks/pretooluse-guard.sh" \
    <<<"$(jq -n --arg c "$cmd" '{tool_name:"Bash", tool_input:{command:$c}, session_id:"acceptance-test"}')" \
    >"$TMP/guard.out" 2>"$TMP/guard.err"
  echo $?
}

GIT_PUSH_RC=$(run_guard "git push origin main")
if [[ "$GIT_PUSH_RC" == "2" ]] && grep -q "ACTION BLOCKED" "$TMP/guard.err"; then
  pass "guard BLOCKS an outward action (git push origin main -> exit 2)"
else
  fail "guard BLOCKS an outward action (git push origin main)" "exit=$GIT_PUSH_RC (expected 2); stderr: $(head -c 200 "$TMP/guard.err")"
fi

BENIGN_RC=$(run_guard "echo hello world")
if [[ "$BENIGN_RC" == "0" ]]; then
  pass "guard PASSES a benign command (echo hello world -> exit 0)"
else
  fail "guard PASSES a benign command (echo hello world)" "exit=$BENIGN_RC (expected 0); stderr: $(head -c 200 "$TMP/guard.err")"
fi

echo
echo "=== 7. no hook contains a machine-specific absolute path or a real person's name ==="
# Scope: registered command strings in settings.json + every hook file under .claude/hooks/
# (post-overlay, i.e. what would actually publish). Reviewed by hand below, not just grepped —
# a bare hit count would misclassify comments/fixtures as leaks.
HITS_USERS=$(grep -rn "/Users/[a-z]" "$CLONE/.claude/hooks" --include="*.sh" --include="*.py" 2>/dev/null | grep -v __pycache__ || true)
HITS_USERS_SETTINGS=$(grep -n "/Users/" "$CLONE/.claude/settings.json" || true)
HITS_NICKNUR=$(grep -rln "nicknur" "$CLONE/.claude/hooks" --include="*.sh" --include="*.py" 2>/dev/null | grep -v __pycache__ || true)
HITS_NICKNUR_SETTINGS=$(grep -n "nicknur" "$CLONE/.claude/settings.json" || true)

echo "    /Users/<name> in hook files:"
if [[ -n "$HITS_USERS" ]]; then echo "$HITS_USERS" | sed 's/^/      /'; else echo "      (none)"; fi
echo "    /Users/ in settings.json command strings:"
if [[ -n "$HITS_USERS_SETTINGS" ]]; then echo "$HITS_USERS_SETTINGS" | sed 's/^/      /'; else echo "      (none)"; fi
echo "    files mentioning 'nicknur':"
if [[ -n "$HITS_NICKNUR" ]]; then echo "$HITS_NICKNUR" | sed 's/^/      /'; else echo "      (none)"; fi
echo "    'nicknur' in settings.json command strings:"
if [[ -n "$HITS_NICKNUR_SETTINGS" ]]; then echo "$HITS_NICKNUR_SETTINGS" | sed 's/^/      /'; else echo "      (none)"; fi

# Verdict: fail only on a REAL machine-specific path or personal identifier used in RUNTIME
# LOGIC. Reviewed manually: every /Users/ hit is either a documentation placeholder
# ("/Users/.../bin/<sync>.sh" in a comment) or a fake fixture path in a *_test*.sh file
# ("/Users/x/.ssh/id_rsa", used only as bait text for a curl-block regression test — never
# read from disk). Every "nicknur" hit is the literal string "nicknur7/core-agent",
# the PUBLIC GitHub identifier of the baseline repo itself (read from bin/sync-manifest.json's
# baseline_repo field at runtime, or a hardcoded same-value fallback if that lookup fails) —
# not a private path, credential, or personal file. No settings.json command string contains
# either pattern at all.
BAD7=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  case "$line" in
    *"/Users/.../"*) ;;                     # doc placeholder, not a real path
    *"/Users/x/"*) ;;                       # test-fixture bait path, never touches disk
    *) BAD7=1; echo "    UNREVIEWED /Users/ hit, treating as failure: $line" ;;
  esac
done <<<"$HITS_USERS"
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  if ! grep -qE 'nicknur7/[Cc]ore\b' "$f"; then
    BAD7=1; echo "    $f mentions 'nicknur' outside the baseline-repo-id pattern — treating as failure"
  fi
done <<<"$HITS_NICKNUR"
if [[ -n "$HITS_USERS_SETTINGS" || -n "$HITS_NICKNUR_SETTINGS" ]]; then
  BAD7=1
fi

if [[ "$BAD7" -eq 0 ]]; then
  pass "no machine-specific path / personal identifier in hook runtime logic (only doc placeholders, a test fixture, and the public baseline-repo id 'nicknur7/core-agent')"
else
  fail "no machine-specific path / personal identifier in hook runtime logic" "see UNREVIEWED lines above"
fi

echo
echo "================================================================"
echo " SUMMARY: $_pass_n passed, ${#_fails[@]} failed"
echo "================================================================"
if (( ${#_fails[@]} )); then
  printf 'FAILED: %s\n' "${_fails[@]}"
  exit 1
fi
echo "ALL PASS"
exit 0
