# brain-pg — Compiled-truth + hybrid retrieval substrate

Postgres + pgvector substrate for Core's brain layer. Replaces grep-on-markdown
recall with hybrid Reciprocal Rank Fusion across vector embeddings, full-text
search, and entity-edge graph BFS.

## Files

- `schema.sql` — canonical schema (entities, evidence, entity_edges, ingest_log) with bi-temporal columns (`valid_from`, `valid_until`, `superseded_by`) on entities + evidence (Zep Graphiti pattern).
- `migrations/` — incremental schema migrations applied to live DB.
- `embed.py` — read brain markdown → Voyage `voyage-3-large` embeddings (1024d) → upsert into Postgres. Incremental via `ingest_log.last_mtime`. Halving fallback on per-request token overflow.
- `compile-truth.py` — partition hubs into N batches for parallel Sonnet subagents; ingest subagent JSON outputs back into `entities.compiled_truth_md`.
- `query.py` — `hybrid_query(text, k)` returns RRF-fused results across vector + tsvector + entity_edges BFS. Graceful degradation to grep if Postgres unreachable.
- `eval.py` — benchmark hybrid vs true-grep baseline; writes report. Acceptance: hybrid ≥20 pp R@5 lift.

## Instance-specific files (live in `$CORE_INSTANCE/scheduling/brain-pg/`)

- `eval-set.json` — ~30 representative recall queries (per-instance).
- `compile-truth-work/` — subagent batch I/O (per-instance).

## Setup

```bash
brew install postgresql@17 pgvector
brew services start postgresql@17
createdb corebrain
psql -d corebrain -f scheduling/brain-pg/schema.sql

# Set VOYAGE_API_KEY in shell rc, then:
python3 scheduling/brain-pg/embed.py        # full pass
python3 scheduling/brain-pg/embed.py --incremental  # session-close
```

## Usage

```bash
# Hybrid recall (called by /recall-similar and claude-brain skill)
python3 scheduling/brain-pg/query.py --k 10 "<query text>"

# Compile-truth pass (one-shot after embedding)
python3 scheduling/brain-pg/compile-truth.py --partition --batches 14
# (spawn 14 Sonnet subagents per partitioned batches)
python3 scheduling/brain-pg/compile-truth.py --ingest

# Benchmark
python3 scheduling/brain-pg/eval.py
```

## Acceptance benchmark (2026-05-17)

Hybrid RRF beat true grep baseline by **+40.9 pp R@5** (0.467 vs 0.058) and **+34.6 pp P@5** (0.413 vs 0.067) across 30 representative queries. Substrate wired into `/recall-similar` and `claude-brain` skill.

Report: `$CORE_INSTANCE/tasks/research/brain-primitives-benchmark-2026-05-17.md`.

Spec: `$CORE_INSTANCE/tasks/spec-brain-primitives-2026-05-13.md`.
