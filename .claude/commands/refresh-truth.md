---
description: Refresh compiled_truth_md on hubs whose evidence has drifted since last compile. Detects + reports drift first; spawns parallel Sonnet subagents to re-synthesize only the drifted hubs. Manual approval required before spend.
argument-hint: "[--dry-run|--auto|--force <entity-name,...>]"
---

# /refresh-truth

Selective re-synthesis of `entities.compiled_truth_md` for drifted hubs. Companion to brain-primitives Step 6 (which keeps the vector index current); this command keeps the hub *summaries* current.

## What it does

1. Runs the drift detector (`$(git rev-parse --show-toplevel)/scheduling/brain-pg/compile-truth-refresh.py --detect`).
2. Surfaces the drift count + cost estimate to the operator.
3. On approval, partitions drifted hubs into batches and spawns parallel Sonnet 4.6 subagents (foreground only — per `tasks/lessons.md`).
4. Ingests subagent outputs into `entities.compiled_truth_md` and bumps `last_compiled_at`.

## Args

- `--dry-run` (default if no args): run detection only. Print report. No spend.
- `--auto`: skip approval step. Use only when you trust the threshold + are OK with the estimated cost.
- `--force <entity-name>[,<name2>...]`: write names to `$(git rev-parse --show-toplevel)/tasks/compile-truth-refresh/force-list.json` then run as a drift pass. Useful for exercising the pipeline or refreshing specific entities ahead of schedule.

## Drift signals (from spec-compile-truth-refresh-2026-05-17.md)

Re-synthesize a hub when ANY of:
- `last_compiled_at` ≥14 days old AND ≥3 new evidence rows linked since
- Evidence row count grew ≥20% since `last_compiled_at`
- Entity name listed in `$(git rev-parse --show-toplevel)/tasks/compile-truth-refresh/force-list.json`

## Workflow

### Step 1 — Detect

```bash
CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
  python3 "$(git rev-parse --show-toplevel)/scheduling/brain-pg/compile-truth-refresh.py" --detect
```

Output: `Drifted (would re-synthesize): N` and `Estimated cost: $X`. Report written to `$(git rev-parse --show-toplevel)/scheduling/brain-pg/compile-truth-work/drift-report-<date>.json`.

**If N = 0**: report so + exit. Nothing to refresh.

**If N > 0**: surface the headline (count + cost + top 5 drifted by score). Ask the operator: *"Proceed to refresh N hubs at ~$X?"*

### Step 2 — Force-list mode (if `--force` arg given)

Write names to `$(git rev-parse --show-toplevel)/tasks/compile-truth-refresh/force-list.json`:
```json
["entity name 1", "entity name 2", ...]
```
Then run `--detect` — the `forced` reason will appear in the drift report.

### Step 3 — Partition (after the operator approves)

```bash
python3 "$(git rev-parse --show-toplevel)/scheduling/brain-pg/compile-truth-refresh.py" --partition --batches N
```
Where N = `min(14, ceil(drift_count / 38))`. Emits `refresh-batch-NN.json` files.

### Step 4 — Subagent fan-out (FOREGROUND only)

For each `refresh-batch-NN.json`, spawn a Sonnet subagent in parallel. Each brief MUST OPEN with the exact words "Compiled-truth hub refresh" **Also include the literal token `CORE-PIPELINE-EXHAUST/v1` anywhere in the brief.** That token is what the filter keys on now: an opening SENTENCE can be reworded by accident — this dispatcher's own history is three hub-refresh phrasings none of which matched — and a fixed token cannot. The phrase requirement above stays for briefs written before the token existed. NEVER put this token on a Sentinel, census or research brief: those carry real signal and must keep flowing into the evidence pool (extract-pending.sh:145). — the extraction filter keys on opening phrases to keep worker transcripts out of the evidence pool, and a hub-refresh transcript that leaks indexes as new evidence ON THE HUB IT JUST REFRESHED, so that hub re-drifts instantly and the next close refreshes it again. If this wording changes, edit `scheduling/graphify-brain/pipeline-exhaust.json` in the SAME commit. Dispatch-verify standard (2026-06-09 — 5 of 7 write-briefed agents claimed success while writing nothing): subagents are **Read-only**; they RETURN the JSON, the parent writes the files.

> Compile-truth refresh pass for batch N. Input: $(git rev-parse --show-toplevel)/scheduling/brain-pg/compile-truth-work/refresh-batch-NN.json. Read up to 5 source files per entity (prefer most recent), produce 2-3 paragraph compiled_truth_md with inline date citations + contested-claim flagging, plus confidence 0.0-1.0. Do NOT write files. Tools allowed: Read ONLY. Your FINAL MESSAGE must be ONLY the raw JSON (no prose, no fences), kind/name VERBATIM from the batch file: {"results":[{"kind":"...","name":"...","compiled_truth_md":"...","confidence":0.0}]}

Then the PARENT writes each return to `refresh-batch-NN-out.json` and verifies before Step 5: every out-file parses, and every (kind,name) exists in its batch input. Ingest count must equal drifted-hub count — a shortfall means a lost batch; re-run it, don't shrug.

**Critical:** `run_in_background: false` (foreground).

### Step 5 — Ingest

```bash
python3 "$(git rev-parse --show-toplevel)/scheduling/brain-pg/compile-truth-refresh.py" --ingest
```
UPDATES `entities.compiled_truth_md` and bumps `last_compiled_at`. Logs the refresh row to `$(git rev-parse --show-toplevel)/tasks/compile-truth-refresh/history.jsonl` (optional).

### Step 6 — Cost surface

Surface the actual Sonnet token usage at end (sum of `total_tokens` from each subagent return). Compare to the estimated cost from Step 1.

## Output format

Tight. Total cap ~30 lines unless the operator asks for detail.

```
/refresh-truth — drift detection

Compiled entities scanned: 532
Drifted: N (cost estimate: $X)

Top 5 by drift score:
1. <name>  age=Yd  evidence: A → B (+C)  reasons=[age,growth]
2. ...

Proceed? (yes / show all / cancel)
```

After ingest:

```
Refreshed N hubs in M batches. Actual cost: $X. last_compiled_at bumped for N rows.
```

## Rules

- **Never auto-fire** without `--auto` flag. Surfacing drift + cost to the operator BEFORE spend is non-negotiable.
- **Foreground subagents only.** Background = silent Write deny.
- **Cost cap**: if estimate > $20, refuse without explicit `--auto-confirm-spend $X`. Prevents surprise spend.
- **Sentinel: no outward action needed** — refresh is local Postgres + Sonnet API. No git push, email, etc.

## When to use

- After 1-2 weeks of new sessions when entity recall starts feeling out-of-date.
- Manually when you've had a big-event session (e.g., major decision, new project launch) and want that hub re-synthesized immediately via `--force`.
- Scheduled (future): a launchd plist firing weekly with `--auto` once the manual flow is proven.

## See also

- `$(git rev-parse --show-toplevel)/scheduling/brain-pg/compile-truth.py` — original one-shot pass (still available for full-corpus refresh).
- `$(git rev-parse --show-toplevel)/scheduling/brain-pg/compile-truth-refresh.py` — drift detector + partition + ingest implementation.
