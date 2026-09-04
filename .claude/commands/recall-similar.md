---
description: Grep the brain vault for a keyword and surface top-3 prior occurrences with 1-line context. Manual recall before risky actions or when context is thin. Pairs with the auto-loading claude-brain skill — use claude-brain for project-shaped recall ("let's work on X"); use /recall-similar for narrow keyword lookup ("show me everywhere we touched Y").
argument-hint: "<keyword>"
---

# /recall-similar

Take the keyword from $ARGUMENTS. Surface the top 3 most relevant prior occurrences from the Core Brain vault.

## Primary path: hybrid RRF query (pgvector + tsvector + entity_edges BFS)

The brain is indexed in Postgres `corebrain` (pgvector + tsvector + entity_edges). The hybrid query layer fuses three legs via Reciprocal Rank Fusion. Wired 2026-05-17 after brain-primitives Steps 2-7.

**Recall is measured on TWO eval sets that disagree, and a figure without its set is not a figure.** Current: **R@5 0.769 on `eval-set.json` (30 queries, substring ground truth), 2026-08-17**; **R@5 0.062 on `eval-set-v2.json` (44 queries, exact entity_id/source_file ground truth), 2026-08-25**. The gap is loose-vs-strict ground truth — the first counts a relevant substring in the top-5, the second demands the exact entity_id or source_file. Normalised against each set's own ceiling (3.2 vs 9.2 relevant per query at k=5) that is 77% of achievable versus 11%.

> _Corrected 2026-08-25._ This line read *"benchmarks measured at +40.9 pp R@5 over the prior grep-on-markdown approach"* — a number that was **superseded on the day it was measured**: `tasks/research/baseline-census-findings-2026-07-27.md:173` records +40.9pp (R@5 0.467) being replaced by graph densification the same afternoon, 2026-05-17, at R@5 0.764. It then sat here unqualified for three months. Found by core-ops, which noticed this file and `docs/architecture/core-system-architecture.html` publish two recall figures **27.8 points apart** with neither naming an instrument.

**Step 1 — Check Postgres is reachable:**
```bash
psql -d corebrain -c 'SELECT 1' >/dev/null 2>&1
```
If not, skip to the Fallback section.

**Step 2 — Run hybrid query:**
```bash
python3 "$(git rev-parse --show-toplevel)/scheduling/brain-pg/query.py" --k 10 "$KEYWORD"
```

Returns lines like:
```
[1] entity  untrusted-reader  rrf=0.047493  legs=vector#1 fts#8 graph#1
    # untrusted-reader
```

`legs=` shows which retrieval legs ranked this result — `vector#N` (pgvector cosine rank N), `fts#N` (Postgres tsvector rank), `graph#N` (entity_edges BFS rank from a name-similarity anchor). Results appearing across multiple legs score higher under RRF.

**Step 3 — Translate top 3 to output format:**

For each top result, use `source` as the file path or entity name. If `kind=entity`, read `${CORE_BRAIN}/entities/<slug>.md` or `${CORE_BRAIN}/topics/<slug>.md` for context. If `kind=evidence`, the source field is already a full path to the session/subagent file.

For an `evidence` row, use offset+limit reads to extract a 1-line context.

If hybrid query errors out or returns zero rows: fall through to Fallback.

## Fallback path: grep

Use grep when graphify is not available, errors out, or returns only god-nodes with no signal.

**Step A — Check for a hub page:**
```
${CORE_BRAIN}/topics/<slug>.md
${CORE_BRAIN}/tools/<slug>.md
${CORE_BRAIN}/entities/<slug>.md
```
Where `<slug>` = keyword normalized: lowercase, spaces → hyphens. If a hub exists, read its first ~30 lines — it is the strongest signal.

**Step B — Grep session files:**
```bash
grep -rliE "$KEYWORD" "${CORE_BRAIN:?CORE_BRAIN not set}/projects/" --include="*.md" 2>/dev/null | head -20
```
For each match, get the session date from the filename or frontmatter.

**Step C — Rank + extract context:**
Rank by recency (newer first). Take top 3. For each, use `grep -n` + a targeted Read with offset+limit to extract a 1-line context. Never full-file reads.

## Output format

Tight. One section per occurrence. Cap total response at ~30 lines. Use this format regardless of whether graphify or grep produced the hits.

```
Recall: "<keyword>" — top 3 occurrences in brain

1. <session-date> · <project-or-section>
   <one-line context, max 120 chars>
   `<absolute path>:<line number or node-id>`

2. ...

3. ...

(Hub: `~/AI Projects/core-brain/topics/<slug>.md` — N sessions reference this)
```

If no hub exists, omit the trailing line. If both graphify and grep return zero matches, say `No prior occurrences of "<keyword>" found in brain.` and stop.

When the hybrid query is the source, the path field may be an entity name (kind=entity) rather than a file:line — that is acceptable; entity rows include the full compiled-truth body in the source field's neighborhood.

## Rules

- **Read-only on brain.** Never write to `~/AI Projects/core-brain/`.
- **No file dumping.** Read with `offset` + `limit`. The point is recall, not a transcript reload.
- **Honest emptiness.** If signal is weak (only 1 match, or all matches are tangential), say so. Don't pad to fill 3 slots.
- **Vector-leg saturation.** Embeddings will often surface semantically-related but topically-tangential results (e.g., "Docker" for a "container security" query). The `legs=` field shows whether a result appeared in vector-only or in fts/graph too — multi-leg hits are stronger signal than vector-only hits.
- **Surface before acting.** When this command fires before another action, give the recall summary, then ask whether to proceed with the original action.

## Example

> User: `/recall-similar receipt-reader`

Expected output:
```
Recall: "receipt-reader" — top 3 occurrences in brain

1. 2026-04-29 · receipt-reader rate-limit fix
   "Etsy API pagination fixed, receipt-reader/overview.md updated with rate-limit notes"
   `${CORE_BRAIN}/projects/<org>/sessions/<date>-evening.md:42`

2. <date> · <topic>
   "<one-line context>"
   `${CORE_BRAIN}/projects/<org>/sessions/<date>-evening.md:18`

3. <date> · <topic>
   "<one-line context>"
   `${CORE_BRAIN}/projects/<org>/sessions/<date>.md:62`

(Hub: `${CORE_BRAIN}/topics/<slug>.md` — N sessions reference this)
```
