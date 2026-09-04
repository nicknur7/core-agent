#!/usr/bin/env bash
# extract-pending.sh — detect un-extracted brain evidence + emit a dispatch
# directive so the LIVE Claude session fans out extraction subagents.
#
# This is the automation of the manual chunk-extraction fan-out (the gate the
# whole brain-unfreeze spec is about — auto-pipeline.sh:7-12). Hooks/cron cannot
# call Agent(); a live session can. So this script DETECTS + PARTITIONS and emits
# a MANDATORY directive; the close-core flow (or the SessionStart safety-net for
# walk-away closes) acts on it by dispatching Read+Write extraction subagents,
# then re-merging the graph and loading entities/edges via embed --graph-nodes.
#
# Mirrors session-start-truth-drift.sh (same in-session-dispatch pattern, Max-sub
# spend not API key for the LLM extraction; embed.py's Voyage calls are the only
# metered piece and are tiny).
#
# Cap: extraction can't drain a large backlog in one close (the 14-subagent cap +
# context limits). We process the most-recent CAP_FILES per invocation, newest
# first (freshest content never goes stale), and report the remaining backlog.
#
# Usage: extract-pending.sh [--cap N] [--phase close|start]
#   exit 0 always (fail-open); emits directive to stdout only when work pending.

set -uo pipefail

CAP_FILES=25            # max files dispatched per invocation (5 batches x 5)
FILES_PER_BATCH=5
PHASE="close"
# --headless RETIRED 2026-07-24: extraction is IN-SESSION only (Agent() subagents on the session's
# subscription auth — no ANTHROPIC_API_KEY). The headless extractor (extract-headless.py) was archived;
# it existed only to extract on walk-aways, but the synchronous-close model defers walk-aways to the
# next in-session /close-core, so a live session is always present at extraction time.

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cap)      CAP_FILES="${2:-25}"; shift 2 ;;
    --phase)    PHASE="${2:-close}"; shift 2 ;;
    --headless) echo "extract-pending: --headless is RETIRED (in-session extraction only) — ignoring" >&2; shift ;;
    *) shift ;;
  esac
done

CORE_INSTANCE="${CORE_INSTANCE:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}"
WORK_DIR="$CORE_INSTANCE/scheduling/graphify-brain/extract-work"
PROMPT_REF="$CORE_INSTANCE/scheduling/graphify-brain/body-extraction-prompt.md"

# Prereq missing → nothing to extract in this env (no brain / no prompt / no python). Emit an
# UNAMBIGUOUS status the close can gate on (Codex: a silent exit 0 was indistinguishable from a
# clean no-op vs a failure). exit 0 = safe to mark synced (there is genuinely nothing to extract).
[[ -d "$CORE_BRAIN" ]] || { echo "EXTRACT-STATUS: skipped (no brain dir)"; exit 0; }
[[ -f "$PROMPT_REF" ]] || { echo "EXTRACT-STATUS: skipped (no extraction prompt)"; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "EXTRACT-STATUS: skipped (no python3)"; exit 0; }

# ORG FROM IDENTITY, NEVER A BARE DEFAULT — the rule bin/si-drain.sh:92 already states, and which
# this script violated. Gate 1 below reads CORE_ORG_ID to decide which project dirs THIS Core claims
# for extraction, and it read it as `os.environ.get("CORE_ORG_ID", "1")` from the INHERITED
# environment: the heredoc invocation passes CORE_BRAIN/CAP/PER/WORK/PHASE explicitly and CORE_ORG_ID
# not at all.
#
# Each Core does set CORE_ORG_ID correctly in its own settings.json (life 1 ... ops 5), so in normal
# operation the default never fired. The hazard is CROSS-CORE INVOCATION, which is not hypothetical:
# bin/si-unify-cutover.sh:35 documents a Core inheriting life's CORE_ORG_ID=1 via
# `cd ../core-business && bash ...`, and bin/brain-export-si-layer.py:86 records that a hardcoded
# CORE_ORG_ID=1 in fleet-shared code "already cross-wrote partitions once, caught 2026-07-25". Every
# peer script run from another seat's session this session inherited life's value the same way.
#
# Consequence if it fires: the Core claims LIFE's projects for extraction and never claims its own,
# so two Cores extract the same slice and one Core's evidence is never extracted at all — the exact
# duplicate-Haiku burn Gate 1 was built to prevent, inverted.
#
# identity.json is the seat's own declaration and cannot be inherited from a parent process, so it
# wins; the env var remains as a fallback for a seat with no identity file. Found by a Codex
# verification sweep, ninth instance of tonight's "shared code silently means life" class.
SEAT_ORG="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("org_id",""))
except Exception:
    print("")' "$CORE_INSTANCE/.claude/identity.json" 2>/dev/null)"
[[ "$SEAT_ORG" =~ ^[0-9]+$ ]] || SEAT_ORG="${CORE_ORG_ID:-1}"

mkdir -p "$WORK_DIR" 2>/dev/null || true
# Clean stale batch lists from a prior invocation.
rm -f "$WORK_DIR"/pending-batch-*.txt 2>/dev/null || true

# Detect unprocessed files (newest-first), capped. Reuses the auto-pipeline.sh
# definition of "unprocessed" = a session/subagent .md with no chunk-body
# checkpoint whose metadata.source_file matches it. Writes capped batch lists +
# prints "TOTAL CAP" so the caller knows the backlog.
SUMMARY=$(CORE_BRAIN="$CORE_BRAIN" CAP="$CAP_FILES" PER="$FILES_PER_BATCH" WORK="$WORK_DIR" PHASE="$PHASE" CORE_ORG_ID="$SEAT_ORG" python3 - <<'PYEOF' 2>/dev/null
import json, os, re, sys
from pathlib import Path
BRAIN = Path(os.environ["CORE_BRAIN"])
# 2026-07-18 (implements decided-but-never-shipped d_38b9e02, brain rerank 0.999): at CLOSE, extract
# only main SESSION files — NOT subagent-output files. Life spawns 20-30 subagents/heavy session; each
# dropped a vault file that got individually LLM-extracted at close (~6 min of mostly-boilerplate churn,
# whose real content is already in the parent session). This was the recurring "big life backlog" Nick
# flagged twice. Subagent files drain via the nightly heavy pipeline / an explicit --phase all run.
_PHASE = os.environ.get("PHASE", "close")
CP = BRAIN / "_build" / "output" / "checkpoints"
CAP = int(os.environ["CAP"]); PER = int(os.environ["PER"]); WORK = Path(os.environ["WORK"])
processed = set()
if CP.is_dir():
    for p in CP.glob("chunk-body-*.json"):
        try:
            sf = (json.loads(p.read_text()).get("metadata", {}) or {}).get("source_file")
            if sf:
                processed.add(sf)
        except Exception:
            pass
# Gate 1 — org-scoped selection (2026-06-09): only queue files THIS Core owns,
# so N Cores closing concurrently drain DISJOINT slices instead of re-extracting
# the same global backlog (the duplicate-Haiku usage burn). Owner of a project
# dir = its tenant slug's org_id; non-tenant dirs (job-hunter, legacy 'core')
# belong to org 1 (life/primary) so they're never double-claimed by two Cores.
MY_ORG = int(os.environ.get("CORE_ORG_ID", "1"))
TENANT = {"life": 1, "business": 2, "school": 3, "finance": 4, "ops": 5}  # ops/org5 added 2026-07-18 (was absent → DB-down fallback misrouted ops dirs to org 1)
try:
    import psycopg2
    # COREBRAIN_DB resolver (2026-08-31 fix) — was hardcoded "corebrain", the same defect as
    # _env.py's admin/tenant paths: a COREBRAIN_DB=other_db install would read tenant rows from
    # the wrong database instead of its own.
    _c = psycopg2.connect(dbname=os.environ.get("COREBRAIN_DB", "corebrain"),
                          user=os.environ.get("CORE_BRAIN_DB_USER", "brain_app"))
    _cur = _c.cursor(); _cur.execute("SELECT org_id, name FROM tenants")
    _rows = {name: oid for (oid, name) in _cur.fetchall()}
    if _rows:
        TENANT = _rows
    _c.close()
except Exception:
    pass
def _owner_org(slug):
    if slug == "core":
        return 1  # legacy pre-split slug → life
    return TENANT.get(slug, 1)  # unknown slug → org 1 (primary owns global/legacy dirs)

unproc = []
_openings = []          # brief opening per pending file, for the leak alarm after the scan
projects = BRAIN / "projects"
if projects.is_dir():
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir():
            continue
        if proj.name.startswith("engine-stripped"):
            # Stale engine/instance-split fork remnants (2026-05-14). No tenant row,
            # so the "unknown slug -> org 1" fallback made life scan them forever; and
            # their files are basename-duplicates of life's own subagent runs, so the
            # basename-keyed checkpoint can never mark all copies processed → permanent
            # false-positive "pending". Not a real partition; skip. (2026-07-11)
            continue
        if _owner_org(proj.name) != MY_ORG:
            continue  # another Core owns this partition — it extracts its own
        # EVERY phase extracts sessions AND subagents (2026-07-25, Nick's call — "option one").
        #
        # History, so this does not get "fixed" back into a mismatch a fourth time:
        #   2026-06-19  d_38b9e02 — skip subagent files at close, push them to the nightly.
        #               Only the skip half ever shipped; the nightly half was never built, so
        #               subagent files accumulated forever (61 -> 46 -> 185 pending).
        #   2026-07-19  "kill subagent extraction entirely" — an INITIAL call, flipped the SAME
        #               session by Codex C12 on measured evidence: 62-89% of subagent graph nodes
        #               are unique and ~half are genuine signal. Dropping them deletes knowledge.
        #   2026-07-19  LOCKED: relevance-gated, judged by whether a file yields durable
        #               Decision/Lesson/Rule/Incident nodes, NOT by producer type. Conservative.
        #   2026-07-25  Nick chose the EXTRACTOR-AS-GATE reading of that lock: feed everything to
        #               the extractor and let the contract decide. Noise yields no durable nodes
        #               and is marked processed. Nothing is ever pattern-matched away UNREAD, so
        #               the gate cannot silently delete signal — the failure mode that ruled out
        #               a filename/producer blacklist.
        #
        # Volume is governed by CAP_FILES (newest-first), NOT by hiding files from detection:
        # a capped run still reports the true TOTAL, so the backlog stays visible instead of
        # being structurally invisible to the close that is supposed to drain it.
        _subdirs = ("sessions", "subagents")

        # EXTRACTION-PIPELINE EXHAUST IS NEVER EXTRACTED (Nick's call, 2026-07-26: "we should not
        # extract the extraction subagent sessions. no need."). This is a deliberate, NARROW
        # producer exclusion for exactly one producer class — the pipeline's own workers — and it
        # REFINES (does not reverse) the 07-25 extractor-as-gate decision: C12's evidence was about
        # Sentinel/census/research subagents, which carry real signal and still flow through the
        # gate; a transcript whose entire content is this pipeline's own brief carries none, and
        # extracting it caused BOTH observed failures (workers obeying the embedded brief as their
        # own task, and the contract's rules landing in the brain as Nick's decisions) PLUS the
        # recursion that made "nothing pending after close" unreachable (extraction spawns workers
        # whose transcripts become new pending evidence, forever).
        # Match on the transcript's own first_message frontmatter — the worker brief's opening
        # words, stable because WE write those briefs. Read only the head; cheap and deterministic.
        # 2026-07-26 addition: 'Compiled-truth refresh' — hub-refresh worker transcripts are the
        # same exhaust class: their content is compiled truth ALREADY ingested, and extracting
        # them made every refreshed hub re-drift (the refresh run itself indexed as new evidence
        # on the hub it refreshed — a perpetual treadmill, observed on sentinel-security-review
        # 3x in 2 days).
        # 2026-07-28: the phrase list moved OUT of this regex and into pipeline-exhaust.json.
        # It lived here as literals while the briefs were hand-written from prose directives
        # elsewhere, and only the EXTRACTION producer was ever told its required phrase — the
        # hub-refresh producer was told none, invented three phrasings, and this regex matched
        # none of them ('Compiled-truth refresh' vs the actual 'Compiled-truth HUB refresh').
        # 22 of 42 pending files were leaked exhaust for two days, silently, which is precisely
        # what the comment above predicted for a reworded first line. Two lists that must agree,
        # stored in two files, is a drift generator; there is now one file, and every dispatch
        # directive quotes it instead of restating phrases.
        _EX = {}
        try:
            # Derived from WORK (…/graphify-brain/extract-work) rather than a new env var —
            # CORE_INSTANCE is a shell local here, not exported into this heredoc, and reading a
            # KeyError into the silent fallback is how a config file ends up never actually read.
            _EX = json.loads((Path(os.environ["WORK"]).parent / "pipeline-exhaust.json").read_text())
        except Exception:
            _EX = {}
        _PHRASES = ((_EX.get("opening_phrases") or {}).get("phrases")
                    # Fail toward the KNOWN list, never toward an empty one: an empty phrase list
                    # silently re-admits every worker transcript, i.e. the bug this file fixes.
                    or ["Brain-graph extraction worker", "RETRY EXTRACTION", "REPAIR TASK",
                        "Compiled-truth refresh", "Compiled-truth hub refresh", "Graph-node extraction"])
        # Literal phrases are escaped; `patterns` are NOT — they exist for worker families whose brief
        # wording varies per invocation. Three Sentinel variants surfaced in one afternoon because the
        # brief is authored fresh each time, so a literal list was losing to prose. A bad pattern is
        # caught here rather than silently matching nothing: re.compile raises, and the except below
        # falls back to the literal-only regex instead of admitting every worker transcript.
        _PATS = list((_EX.get("opening_phrases") or {}).get("patterns") or [])
        _alts = [re.escape(p) for p in _PHRASES] + _PATS
        try:
            _WORKER_RE = re.compile(r'first_message:\s*"(?:' + "|".join(_alts) + r')')
        except re.error as _e:
            print(f"[extract-pending] WARN bad pattern in pipeline-exhaust.json ({_e}) — "
                  f"falling back to literal phrases only", file=sys.stderr)
            _WORKER_RE = re.compile(
                r'first_message:\s*"(?:' + "|".join(re.escape(p) for p in _PHRASES) + r')')
        _SIGS = ((_EX.get("structural_signatures") or {}).get("groups")
                 or [["pending-batch-", "chunk-body-"], ["refresh-batch-", "compiled truth"]])

        def _head(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return fh.read(1500)
            except Exception:
                return None   # unreadable -> NOT excluded; fail toward extraction, never toward silent skip

        # MACHINE-READABLE MARKER, CHECKED FIRST (2026-08-28, core-business's design, bus #5837).
        # A producer that stamps the token says WHAT IT IS instead of leaving the detector to infer
        # it from prose. Fixed token, so it cannot be reworded by accident the way an opening
        # sentence can. Falls back to the empty string only if the config is unreadable, and an
        # empty token must never match every head — hence the truthiness guard in the check below.
        _MARK = ((_EX.get("dispatch_marker") or {}).get("token") or "") if isinstance(_EX, dict) else ""

        def _is_pipeline_exhaust_head(head):
            if head is None:
                return False
            # Declared, not inferred. Cheapest and most reliable signal available.
            if _MARK and _MARK in head:
                return True
            if _WORKER_RE.search(head):
                return True
            # Prose-independent backstop: a worker transcript unavoidably carries the pipeline's
            # own file paths in the brief, so a producer that invents new wording is still caught.
            low = head.lower()
            return any(all(t.lower() in low for t in group) for group in _SIGS)

        for sub in _subdirs:
            d = proj / sub
            if d.is_dir():
                for p in sorted(d.glob("*.md")):
                    rel = f"projects/{proj.name}/{sub}/{p.name}"
                    if rel in processed:
                        continue
                    head = _head(p)
                    if _is_pipeline_exhaust_head(head):
                        continue
                    unproc.append(rel)
                    # Keep the brief opening for the leak alarm below.
                    m = re.search(r'first_message:\s*"([^"\n]{0,200})', head or "")
                    _openings.append(m.group(1) if m else "")
total = len(unproc)

# LEAK ALARM (2026-07-28) — make the NEXT phrase drift loud instead of silent.
# When many pending files share a near-identical brief opening, that is the signature of a
# producer whose required phrase stopped matching: not N pieces of evidence, one leak N times.
# It only WARNS. Auto-excluding on repetition would let a legitimately repetitive class of real
# evidence disappear unread, which is the failure mode that ruled out a producer blacklist.
_alarm = Path(os.environ["WORK"]) / "exhaust-leak-suspect.txt"
try:
    _alarm.unlink()
except Exception:
    pass
_cfg = (_EX.get("leak_alarm") or {}) if isinstance(_EX, dict) else {}
_minid = int(_cfg.get("min_identical_openings", 3))
_pfx = int(_cfg.get("opening_prefix_chars", 60))
_groups = {}
for _o in _openings:
    if _o.strip():
        _groups.setdefault(_o[:_pfx], 0)
        _groups[_o[:_pfx]] += 1
_susp = sorted(((n, o) for o, n in _groups.items() if n >= _minid), reverse=True)

# THE REPETITION THRESHOLD FINDS LEAKS IN PROPORTION TO HOW BADLY THEY HAVE ALREADY ACCUMULATED, and
# core-finance named that precisely (bus #704) after core-business found the Sentinel leak (#700):
#
#   business had 7 identical Sentinel openings -> tripped min_identical_openings=3, alarm fired.
#   finance had ONE Sentinel transcript and ONE close-reconciler transcript -> two distinct openings,
#   neither repeated, no alarm at all, and it extracted both as legitimate evidence.
#
# So a Core that closes REGULARLY leaks one per close and never trips this. business only caught it
# because it was behind. The detector was rewarding backlog.
#
# The fix is not a lower threshold — one is not a suspicious number of anything. It is to surface an
# UNRECOGNIZED subagent opening on its FIRST occurrence, because a new worker type is exactly the thing
# a phrase allowlist cannot anticipate. Known-exhaust is still excluded silently; unknown now asks.
#
# Accepted cost, stated because it is a real one: a legitimate research subagent also lives under
# subagents/ and will be listed here until someone decides. That is once per new opening, and it fails
# toward asking rather than toward silently indexing a worker's reasoning as the Core's own evidence —
# which is the failure this whole file exists to prevent.
# Built from `unproc` + `_openings`, which the scan above already produced index-aligned and already
# filtered through _is_pipeline_exhaust_head — so anything still here is BY DEFINITION not a recognised
# worker. My first version re-read every file via os.environ["BRAIN"], which is not the variable name
# (it is CORE_BRAIN) — the guard would have skipped the whole block and this alarm would have silently
# never fired. Reusing the data the loop already has removes both the re-read and the chance to get a
# name wrong.
_unknown = {}
for _rel, _op in zip(unproc, _openings):
    if "/subagents/" not in _rel.replace("\\", "/"):
        continue
    if not _op.strip():
        continue
    if _op[:_pfx] in _groups and _groups[_op[:_pfx]] >= _minid:
        continue          # already named by the repetition alarm above — one fact, one report
    _unknown.setdefault(_op[:_pfx], []).append(_rel)

_lines = []
if _susp:
    _lines += [f"⚠ EXHAUST-LEAK-SUSPECT — {len(_susp)} brief opening(s) repeat across pending files.",
               "  A repeated worker brief is one producer leaking, not N pieces of evidence.",
               "  If these are pipeline workers, add the opening to scheduling/graphify-brain/",
               "  pipeline-exhaust.json (opening_phrases.phrases) and re-run — do NOT extract them."]
    _lines += [f"    {n:4}x  {o}" for n, o in _susp]
if _unknown:
    if _lines:
        _lines.append("")
    _lines += [f"⚠ UNRECOGNIZED SUBAGENT OPENING(S) — {len(_unknown)} distinct, on FIRST occurrence.",
               "  These are NOT excluded and WILL be extracted as evidence. Decide which they are:",
               "  a WORKER (add the opening to pipeline-exhaust.json, do not extract) or genuine",
               "  evidence (leave it — this notice costs one decision per new opening).",
               "  Fires without needing a repeat, because a Core that closes regularly leaks one per",
               "  close and never trips the repetition threshold (finance, bus #704)."]
    for _o, _fs in sorted(_unknown.items(), key=lambda kv: -len(kv[1])):
        _lines.append(f"    {len(_fs):4}x  {_o}")
        _lines.append(f"          e.g. {_fs[0]}")
if _lines:
    _alarm.write_text("\n".join(_lines) + "\n")

# Newest-first by leading YYYY-MM-DD in filename (fallback: lexical).
def datekey(rel):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", rel.rsplit("/", 1)[-1])
    return m.group(1) if m else "0000-00-00"
unproc.sort(key=datekey, reverse=True)
batch = unproc[:CAP]
# Write per-batch lists.
n = 0
for i in range(0, len(batch), PER):
    n += 1
    (WORK / f"pending-batch-{n:02d}.txt").write_text("\n".join(batch[i:i+PER]) + "\n")
print(f"{total} {len(batch)} {n}")
PYEOF
)

# Parse "TOTAL DISPATCHED NBATCHES".
read -r TOTAL DISPATCHED NBATCHES <<< "$SUMMARY"
# DETECTION FAILURE (python errored → non-numeric SUMMARY) is NOT a clean no-op — exit NON-ZERO with a
# distinct status so the close does NOT mark the brain synced on an undetected backlog (Codex #2).
[[ "$TOTAL" =~ ^[0-9]+$ ]] || { echo "EXTRACT-STATUS: detect-error (could not enumerate pending — DO NOT mark synced)"; exit 2; }
# Surface a suspected exhaust leak BEFORE the dispatch directive, so it is read before agents are
# spawned on what may be worker transcripts rather than after the spend.
[[ -s "$WORK_DIR/exhaust-leak-suspect.txt" ]] && cat "$WORK_DIR/exhaust-leak-suspect.txt"
# CLEAN no-op: detection ran, nothing pending. Distinct status → safe to mark synced.
(( TOTAL > 0 )) || { echo "EXTRACT-STATUS: none-pending (all evidence already extracted)"; exit 0; }

REMAIN=$(( TOTAL - DISPATCHED ))
BATCH_LIST=$(ls "$WORK_DIR"/pending-batch-*.txt 2>/dev/null | sort | sed 's|^|    - |')

# START phase = lightweight notice only (don't force a heavy fan-out at every
# session open while a large historical backlog drains). CLOSE phase = the
# mandatory dispatch directive (work just happened; Nick is leaving anyway).
if [[ "$PHASE" == "start" ]]; then
  cat <<EOF
🧠 BRAIN EXTRACTION PENDING — ${TOTAL} evidence file(s) not yet in the graph (org-tagged entities).
   Not auto-dispatched at start. To catch up: run \`/close-core\` (extracts this session's files on close),
   or \`bash scheduling/graphify-brain/extract-pending.sh --phase close --cap N\` then follow its directive.
EOF
  exit 0
fi

cat <<EOF
🧠 BRAIN EXTRACTION PENDING (${PHASE}) — ${TOTAL} evidence file(s) not yet extracted into the graph.
   Dispatching ${DISPATCHED} this pass (newest-first), in ${NBATCHES} batch(es) of ≤${FILES_PER_BATCH}.
   $([ "$REMAIN" -gt 0 ] && echo "Backlog after this pass: ${REMAIN} (drains over future closes; run with --cap higher for a catch-up)." || echo "No backlog remaining after this pass.")
   Batch lists (one source path per line, relative to \$CORE_BRAIN):
${BATCH_LIST}

   MANDATORY ACTION (the brain stays current only if this runs):
   1. Spawn ${NBATCHES} parallel foreground **Haiku** subagents (pin model:haiku — 2026-07-24: Haiku is safe
      for IN-SESSION extraction WITH the two safety nets in step 2. The 06-11 5/25 failure (4 unescaped-quote
      invalid JSON + 1 never-written, false readbacks) was RAW Haiku without them; the headless extractor's
      16/16 pass came from its json.loads+repair-retry, which step 2 now reproduces here) (run_in_background:false),
      one per pending-batch-NN.txt.
      Each subagent brief MUST OPEN with the exact words "Brain-graph extraction worker" (retry briefs:
      "RETRY EXTRACTION"; repair briefs: "REPAIR TASK") **Also include the literal token CORE-PIPELINE-EXHAUST/v1 anywhere in the brief.** That token is what the filter keys on now: an opening SENTENCE can be reworded by accident — this dispatcher's own history is three hub-refresh phrasings none of which matched — and a fixed token cannot. The phrase requirement above stays for briefs written before the token existed. NEVER put this token on a Sentinel, census or research brief: those carry real signal and must keep flowing into the evidence pool (extract-pending.sh:145). — the filter keys on these opening phrases to keep
      worker transcripts out of the extraction pool (Nick 2026-07-26). Reword that first line and the
      recursion this filter kills comes back SILENTLY — which is not hypothetical: on 2026-07-28 a run
      opened "Graph-node extraction" instead, and separately the hub-refresh producer (never told any
      phrase) invented three of its own; 24 leaked transcripts became 57% of the pending backlog.
      The phrases now live in ONE place — scheduling/graphify-brain/pipeline-exhaust.json — which this
      script reads. If a brief opening must change, edit that file in the SAME commit, never after.
      Brief body:
        - Read the extraction contract IN FULL: ${PROMPT_REF}
        - For each relative path in its batch file: read \$CORE_BRAIN/<path>, extract nodes+edges per the
          contract (reasoning pass: 5-15 Decision/Lesson/Rule/Incident on substantive sessions), and WRITE
          \$CORE_BRAIN/_build/output/checkpoints/chunk-body-<basename>.json (basename = filename minus .md).
        - metadata.source_file MUST be the EXACT relative path given (load-bearing for org_id ownership —
          never absolute, never rewrite the project slug). metadata also: chunk_id, extraction_date, extractor.
        - Tools allowed: Read, Write only. After each Write, READ the checkpoint back and confirm it
          parses as JSON. If a Write errors or the readback fails, REPORT THE FAILURE — never claim done.
          Return per-file node/edge counts.
   2. VERIFY BEFORE MERGE (dispatch-verify standard 2026-06-09 — agents have claimed "done" with no file
      on disk twice now, 06-08 Haiku and 06-09 Sonnet). Every batch path must have a parseable checkpoint:
        cd "\$CORE_INSTANCE/scheduling/graphify-brain" && python3 -c "
import json,sys,glob,os
brain=os.path.expanduser(os.environ.get('CORE_BRAIN','~/AI Projects/core-brain')); miss=[]
for bf in sorted(glob.glob('extract-work/pending-batch-*.txt')):
    for rel in open(bf).read().split():
        cp=os.path.join(brain,'_build/output/checkpoints','chunk-body-'+os.path.basename(rel).replace('.md','')+'.json')
        try: json.load(open(cp))
        except Exception: miss.append((bf,rel))
print('MISSING/UNPARSEABLE:',len(miss)); [print(' ',b,r) for b,r in miss]; import sys; sys.exit(1 if miss else 0)"
        # ^ exits non-zero when any checkpoint is missing/unparseable, so this line can gate merge (\`&& merge.py\`)
        #   as well as being read by the agent. 0 = all present + parseable → safe to merge.
      If MISSING/UNPARSEABLE > 0: **repair-retry** (this is the safety net that makes Haiku safe — it
      reproduces the headless extractor's repair pass): re-dispatch ONLY those files to a Haiku retry agent
      whose brief adds "the prior checkpoint was invalid JSON — re-emit strictly valid JSON only, escape
      every \" as \\\", no markdown fences, no prose." Re-verify. If a file is STILL missing/unparseable
      after that ONE repair pass, re-dispatch that single file to a **Sonnet** agent (the fallback — quality
      over cost for the rare hard file). Only merge once every batch path has a parseable checkpoint.
   3. After verification passes, rebuild the graph + load entities/edges (org-aware, Phase 1B):
        cd "\$CORE_INSTANCE/scheduling/graphify-brain" && CORE_BRAIN="\${CORE_BRAIN:-\$HOME/AI Projects/core-brain}" \\
          CORE_INSTANCE="\$(git rev-parse --show-toplevel)" CORE_ORG_ID="\${CORE_ORG_ID:-1}" python3 merge.py >/dev/null
        cd "\$CORE_INSTANCE/scheduling/brain-pg" && CORE_BRAIN="\${CORE_BRAIN:-\$HOME/AI Projects/core-brain}" \\
          CORE_ORG_ID="\${CORE_ORG_ID:-1}" python3 embed.py --graph-nodes
   4. Continue the close (or, at START, proceed to the operator's question).

   If ${TOTAL} > 200, tell the operator the backlog size first — a full catch-up is many subagent passes.
EOF
exit 0
