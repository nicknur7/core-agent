#!/usr/bin/env bash
# si-drain.sh — the unattended half of the self-improvement loop.
#
# WHY THIS EXISTS. Measured 2026-08-28: ask distillation had not run in 15 days on core-life and
# 76% of recorded corrections had never been distilled, so the loop was learning from a quarter of
# its own evidence while reporting healthy numbers.
#
# THE CAUSE WAS NOT "Nick rarely closes." That was this header's original claim and Nick rejected
# it to my face ("i have ran /close-core so many more time than that"). He was right. Counted from
# the raw transcripts the same day: he closes explicitly on 44 of 59 fleet sessions (75%) — life
# 11/17, business 10/12, school 9/12, finance 7/8, ops 7/10; 31 of those in the last 21 days. The
# real cause is stated in core-si-close.py:754 in Core's own words: the chain
# `extract_pending() -> model -> cache_asks()` existed and "NOTHING CALLS IT." The close path
# MEASURED the backlog every night and never drained it. This script is the first caller that ever
# ran it — not a workaround for a human who skipped his half.
#
# The lesson is the one that keeps recurring: a number quoted from Core's own prior write-up is not
# a measurement. Both the count and the causal story were wrong, and the wrong story blamed Nick. lifecycle_nightly()'s own comment says it cannot
# drain that debt because it "needs a live Agent()".
#
# WHAT CHANGED: the fleet moved to an always-on Mac mini, and `claude -p` runs on SUBSCRIPTION auth
# with no ANTHROPIC_API_KEY. The 2026-07-24 retirement killed the API-KEY path, not headless
# execution; its stated premise — "a live session is ALWAYS present at extraction time" — is
# measurably false now, which is what makes this legitimate rather than re-litigation.
#
# ─────────────────────────────────────────────────────────────────────────────────────────────
# THE MODEL IS READ-ONLY, STARTED OUTSIDE THE REPO, AND EVERY DB WRITE IS DONE BY THIS SCRIPT.
# ─────────────────────────────────────────────────────────────────────────────────────────────
# v1 of this script ran `claude -p "$PROMPT"` with a FORBIDDEN block in the prompt telling the model
# not to write files, commit, or push. sentinel-code BLOCKED it and was right: that is prose asked
# of a model, not an enforced boundary. With no --tools/--permission-mode flag the unattended
# session inherits this repo's settings.local.json — "defaultMode": "bypassPermissions" plus
# Write(core-life/**), Edit(core-life/**) and Bash(git *) — so it had the actual technical ability
# to rewrite memory/, sessions/ and current-state.md and to commit and push. The script's own
# header had ALREADY named that exact failure mode as the reason it rejected
# `claude -p "/close-core"`, and then did not close it for its own call.
#
# Tightening flags was not enough either: `--tools Bash` does drop Write/Edit (verified), but Bash
# is Turing-complete — python3 -c "open(...,'w')" writes files regardless.
#
# So the model is reduced as far as the CLI allows and started OUTSIDE the repo. It is a text
# transformer:
#   THIS SCRIPT reads the pending rows from Postgres and hands them to the model as text.
#   THE MODEL returns JSON on stdout. It has Read only — no Bash, Write, Edit or WebFetch — and
#   is started outside the repo. See the spawn site for what is NOT enforced.
#   THIS SCRIPT validates that JSON and performs every database write itself.
# See the measured flag behaviour at the spawn site below — there is NO zero-tool option, and the
# comment that once claimed one here was wrong.
#
# What remains reachable is exactly one thing: rows in `pattern_observations.canonical_ask` for THIS
# seat's org, written by this script, validated against the extraction contract first. Those are
# org-partitioned by RLS and reversible by setting the column back to NULL.
#
# NOT `claude -p "/close-core"`. Fable reviewed that design and refused it: /close-core is a protocol
# with a human in the control flow. Step 1 reads the INVOKING session's JSONL; step 5 surfaces
# reconciler AMBIGUOUS items for Nick's ok; steps 6-7 rewrite sessions/ and current-state.md, so five
# robot closes a night would prune Nick's own work narrative out of the file he treats as ground
# truth; 7.5 consolidation can mint a fabricated workflow that "later fires as durable instruction,
# which is worse than capturing nothing". Nick made the close synchronous and human-present
# deliberately, twice, after a detached version misfired. This does not reverse that.
#
# Usage:  bash bin/si-drain.sh [--dry-run]
set -uo pipefail

# INTERPRETER PINNED, NOT INHERITED (found by core-business, confirmed by ops and school).
# com.nick.brain-pipeline.plist declared PATH with /opt/homebrew/bin FIRST, so the scheduler resolved
# python3 3.14.7 — no psycopg2. ONLY /usr/bin/python3 (3.9.6) carries the brain dependency set on
# this machine. That is why life's 02:00 nightly died at 02:01:32 with EMBED_EXIT=1 while every
# hand-test passed: we were all testing a different interpreter than the scheduler uses.
PY_BIN="${CORE_PY:-/usr/bin/python3}"
if ! "$PY_BIN" -c 'import psycopg2' >/dev/null 2>&1; then
  for c in /usr/bin/python3 /opt/homebrew/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import psycopg2' >/dev/null 2>&1; then PY_BIN="$c"; break; fi
  done
fi
"$PY_BIN" -c 'import psycopg2' >/dev/null 2>&1 || {
  echo "si-drain: FATAL — no interpreter has psycopg2; refusing to run" >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SEAT="$(basename "$REPO")"
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
# Logs live with the SEAT, not in shared /tmp. bin/ syncs to every Core, so a /tmp path is
# the same path on all five — caught by test_no_cross_core_paths.
mkdir -p "$REPO/.claude/state/logs"
LOG="$REPO/.claude/state/logs/si-drain-$(date +%F).log"
REPORT="$REPO/.claude/state/si-drain-last.json"
WORK="$(mktemp -d "/tmp/si-drain-${SEAT}.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >> "$LOG"; }

# ORG FROM IDENTITY, NEVER A BARE DEFAULT. A hardcoded CORE_ORG_ID=1 in fleet-shared code already
# cross-wrote partitions once (2026-07-25). get_org_id() prefers identity.json over the environment.
ORG="$(cd "$REPO" && "$PY_BIN" -c "
import sys; sys.path.insert(0,'scheduling/brain-pg')
from _env import get_org_id; print(get_org_id())" 2>>"$LOG")"
[[ -n "$ORG" ]] || { log "FATAL: could not resolve org from identity.json"; exit 1; }
log "=== si-drain start · seat=$SEAT org=$ORG dry=$DRY ==="

# ── THE CRANK. Distillation FEEDS the loop; this TURNS it. ───────────────────────────────────
#
# Until 2026-08-28 the only runtime caller of friction_loop.run() anywhere in the repo was
# bin/core-si-close.py — i.e. the install pass ran at SESSION END and nowhere else. So this
# nightly job distilled corrections into asks every night and then left them sitting: on a session
# that ran 144 hours (core-life, measured 2026-08-22..28) nothing routed, gated, installed,
# graduated or swept for six days, while the drain reported success every night.
#
# fl.run() is the whole crank in one call — mine, route, generate, gate, install, watchdog,
# graduate. It is the SAME entry point core-si-close uses, with the same 90-day window, so this
# adds no second mechanism and no second policy; it only removes "a session has to end" as a
# precondition for the loop to turn.
#
# RUNS EVEN WHEN THERE WAS NOTHING TO DISTIL, which is why it sits above the early exit: a quiet
# night still has yesterday's asks, artifacts whose evidence has crossed a graduation bar, and
# artifacts the watchdog should retire. Making the crank conditional on new corrections would
# reintroduce the same defect one level up.
#
# FAIL-OPEN and never fatal: a drain that cannot install must still report its distillation
# honestly. Honours CORE_FRICTION_DISABLE=1, the same escape hatch core-si-close reads.
turn_the_crank() {
  if [[ "${CORE_FRICTION_DISABLE:-}" == "1" ]]; then
    log "crank: skipped (CORE_FRICTION_DISABLE=1)"; CRANK="disabled"; return 0
  fi
  if (( DRY )); then
    log "crank: --dry-run, not turning"; CRANK="dry"; return 0
  fi
  # CORE_SI_UNATTENDED=1 tells artifact_generator.auto_apply_directive to WITHHOLD the CLAUDE.md
  # write and return the proposal instead. Nick's 2026-08-17 approval of that write was for session
  # close with him present; this job runs at 03:10 with nobody there. Found by core-school (bus
  # #5707) after I shipped the crank, and after I had asked Nick for authorisation using a
  # description that never mentioned CLAUDE.md at all.
  CRANK="$(cd "$REPO" && CORE_INSTANCE="$REPO" CORE_ORG_ID="$ORG" CORE_SI_UNATTENDED=1 "$PY_BIN" -c "
import sys, json
sys.path.insert(0,'scheduling/claude-si'); sys.path.insert(0,'scheduling/brain-pg')
try:
    import friction_loop as fl
    o = fl.run(days=90, dry=False)
    g = o.get('graduation') or {}
    w = o.get('watchdog') or {}
    print(json.dumps({
        'cases': o.get('cases'), 'eligible': o.get('eligible'),
        'installed': o.get('installed'),
        'promoted': [x.get('name') for x in (g.get('promoted') or [])],
        'demoted': g.get('demoted') or [],
        'quarantined': w.get('quarantined') or [],
        'undispatchable': [a for a, _ in (w.get('undispatchable') or [])],
        'directives_withheld': [d for d in (o.get('generator') or {}).get('detail') or []
                                if 'withheld' in str(d)],
    }))
except Exception as e:
    print(json.dumps({'error': f'{type(e).__name__}: {str(e)[:160]}'}))
" 2>>"$LOG")"
  # NOT ${CRANK:-{...}} — bash ends the parameter expansion at the FIRST '}', so the rest of the
  # default is appended as literal text and every report came out with a trailing brace and failed
  # to parse. A report nothing can read is the dead-counter defect in miniature.
  if [[ -z "$CRANK" ]]; then CRANK='{"error":"no output"}'; fi
  log "crank: $CRANK"
}

# ── 1. THIS SCRIPT reads the pending rows. The model never touches the database. ─────────────
cd "$REPO" || exit 1
CORE_INSTANCE="$REPO" "$PY_BIN" -c "
import sys, json; sys.path.insert(0,'scheduling/claude-si'); sys.path.insert(0,'scheduling/brain-pg')
import ask_miner as am
json.dump(am.extract_pending($ORG, 200), open('$WORK/pending.json','w'))" 2>>"$LOG" || {
  log "FATAL: could not read pending rows"; exit 1; }

PENDING="$("$PY_BIN" -c "import json;print(len(json.load(open('$WORK/pending.json'))))" 2>>"$LOG")"
PENDING="${PENDING:-0}"
log "distillation backlog: $PENDING"

if [[ "$PENDING" == "0" ]]; then
  log "nothing to distil — no model spawned; still turning the crank"
  turn_the_crank
  printf '{"ran_at":"%s","seat":"%s","org":%s,"backlog":0,"action":"no-distillation-crank-turned","crank":%s}\n' \
    "$(date -Iseconds)" "$SEAT" "$ORG" "$CRANK" > "$REPORT"
  exit 0
fi
(( DRY )) && { log "--dry-run: would drain $PENDING row(s); no model spawned"; turn_the_crank; exit 0; }

# ── THE SHARED BRAIN LOCK. Five seats block on one lock with NO timeout (the operator, 2026-07-24:
# run until the queue clears). An unattended run REFUSES rather than queues — a skipped nightly is
# recoverable, a pile-up on a no-timeout lock is not. The fleet runner staggers seats anyway.
BRAIN_HASH="$(echo "${CORE_BRAIN:-$HOME/AI Projects/core-brain}" | md5 -q 2>/dev/null || echo x)"
if [[ -d "/tmp/core-brain-${BRAIN_HASH}.lock" ]]; then
  log "brain lock HELD — refusing to queue; next run picks it up"
  printf '{"ran_at":"%s","seat":"%s","org":%s,"backlog":%s,"action":"skipped-lock-held"}\n' \
    "$(date -Iseconds)" "$SEAT" "$ORG" "$PENDING" > "$REPORT"
  exit 0
fi

# ── 2. THE MODEL. Read-only, no MCP, cwd outside the repo. It sees text and returns text. ───
printf '{"mcpServers":{}}' > "$WORK/nomcp.json"
{
  echo "Distil each recorded correction into the DURABLE directive behind it. Strip the frustration; the subject is the ask, not the anger."
  echo
  echo "CONTRACT — violations are discarded downstream, so respect it exactly:"
  echo "  imperative mood · <=160 chars · no first- or second-person pronouns · no profanity ·"
  echo "  no pasted output, paths or code."
  echo "  type is exactly one of: constraint | procedure | none"
  echo "  Use ask:\"\" with type:\"none\" for pure frustration, one-off factual corrections, approvals,"
  echo "  and anything where the durable want cannot be read without guessing."
  echo "  A HIGH NULL RATE IS CORRECT — roughly half of real corrections carry no durable ask, and a"
  echo "  fabricated one pollutes a cluster permanently."
  echo "  If an existing cluster already says this, reuse its wording VERBATIM rather than a"
  echo "  near-synonym — a near-miss splits one strong cluster into two weak ones."
  echo
  echo "Return ONLY a JSON array, no prose, no code fence:"
  echo '  [{"id": <int>, "ask": "<string>", "type": "constraint|procedure|none"}]'
  echo
  echo "INPUT:"
  cat "$WORK/pending.json"
} > "$WORK/prompt.txt"

# HONEST STATEMENT OF WHAT IS AND IS NOT ENFORCED (2026-08-28, after sentinel-code ASKed on a
# claim I could not support). THERE IS NO ZERO-TOOL CONFIGURATION. Measured, not assumed:
#   --tools            (bare)  -> swallows the NEXT FLAG as a tool name; MCP servers still load
#   --tools ""                 -> Bash, Glob, Grep, Read, Edit, Write, NotebookEdit, WebFetch
#   --tools Read               -> Read, MEMORY (Write), Skill, AgentTool
# The CLI always injects MEMORY, Skill and AgentTool. My previous comment claimed "the model has NO
# TOOLS AT ALL ... true by construction". That was false, and it was the SECOND time in one night I
# described a guard in a comment that did not exist in the code. sentinel-code caught both.
#
# WHAT IS ACTUALLY ENFORCED HERE:
#   - cwd is a TEMP DIRECTORY, not the repo. The model is not started anywhere near core-life, so
#     nothing it does relatively can land in the repo.
#   - --tools Read: no Bash, no Write, no Edit, no NotebookEdit, no WebFetch. It cannot execute a
#     command, edit a file, or reach the network.
#   - --strict-mcp-config with an empty config: no calendar writes, no reminders writes, no search.
#   - The script performs EVERY database read and write. The model only returns text.
#   - That text is validated field by field before any of it reaches the database, and a row id the
#     model did not receive is rejected outright.
#
# WHAT IS NOT ENFORCED, stated plainly rather than papered over: MEMORY (Write), Skill and AgentTool
# remain in the session. MEMORY writes go to the user's memory directory, outside the repo. AgentTool
# could in principle spawn a subagent with a wider toolset. This is defence in depth, not a sandbox.
# If that residual is unacceptable the correct answer is a real sandbox — a separate user or a
# container — not another comment asserting a boundary that flags do not give.
log "spawning model (Read-only, no MCP, cwd outside repo) for $PENDING row(s)"
( cd "$WORK" && claude -p "$(cat "$WORK/prompt.txt")" \
    --tools Read \
    --strict-mcp-config --mcp-config "$WORK/nomcp.json" \
    --output-format text < /dev/null > "$WORK/out.txt" 2>>"$LOG" )
RC=$?
log "model exit=$RC"

# ── 3. THIS SCRIPT validates and writes. Nothing the model returned is trusted as-is. ────────
CACHED="$(cd "$REPO" && CORE_INSTANCE="$REPO" "$PY_BIN" - "$WORK/out.txt" "$WORK/pending.json" "$ORG" <<'PY' 2>>"$LOG"
import json, re, sys
sys.path.insert(0, 'scheduling/claude-si'); sys.path.insert(0, 'scheduling/brain-pg')
import ask_miner as am

out_p, pend_p, org = sys.argv[1], sys.argv[2], int(sys.argv[3])
raw = open(out_p).read().strip()
m = re.search(r"\[.*\]", raw, re.S)          # tolerate stray prose around the array
if not m:
    print("0 no-json"); raise SystemExit
try:
    items = json.loads(m.group(0))
except Exception:
    print("0 bad-json"); raise SystemExit

valid_ids = {r["id"] for r in json.load(open(pend_p))}
TYPES = {"constraint", "procedure", "none"}
PRON = re.compile(r"\b(i|me|my|you|your|we|our|us)\b", re.I)
pairs, rejected = [], 0
for it in items if isinstance(items, list) else []:
    try:
        i = int(it["id"]); ask = (it.get("ask") or "").strip(); t = (it.get("type") or "").strip()
    except Exception:
        rejected += 1; continue
    # EVERY CONTRACT TERM IS RE-CHECKED HERE. The model was asked; this enforces.
    if i not in valid_ids or t not in TYPES:            rejected += 1; continue
    if ask and (len(ask) > 160 or PRON.search(ask)):    rejected += 1; continue
    if ask and t == "none":                             rejected += 1; continue
    pairs.append({"id": i, "ask": ask, "type": t})
n = am.cache_asks(org, pairs) if pairs else 0
print(f"{n} rejected={rejected}")
PY
)"
log "cached: ${CACHED:-none}"

# ── 4. VERIFY THE CLAIM. Re-measure rather than trust the count we were handed. ──────────────
AFTER="$(cd "$REPO" && CORE_INSTANCE="$REPO" "$PY_BIN" -c "
import sys; sys.path.insert(0,'scheduling/claude-si'); sys.path.insert(0,'scheduling/brain-pg')
import ask_miner as am
print(len(am.extract_pending($ORG, 500)))" 2>>"$LOG")"
log "backlog after: ${AFTER:-unknown} (was $PENDING)"

# Newly-distilled asks are only useful once the loop has routed and installed them, so the crank
# turns AFTER caching — a distil-then-wait-a-day cycle is the slower version of the bug above.
turn_the_crank

"$PY_BIN" - "$REPORT" "$SEAT" "$ORG" "$PENDING" "${AFTER:-unknown}" "$RC" "${CACHED:-none}" "${CRANK:-null}" <<'PY'
import json, sys, datetime
p, seat, org, before, after, rc, cached = sys.argv[1:8]
json.dump({
    "ran_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "seat": seat, "org": int(org),
    "backlog_before": before, "backlog_after": after,
    "model_exit": int(rc), "cached": cached, "crank": json.loads(sys.argv[8]) if len(sys.argv) > 8 else None,
    "drained": before.isdigit() and after.isdigit() and int(after) < int(before),
    "model_tools": "Read-only, no MCP, cwd outside repo; MEMORY/Skill/AgentTool remain — defence in depth, not a sandbox. All DB reads and writes done by the script.",
}, open(p, "w"), indent=1)
PY
log "=== si-drain done ==="
exit 0
