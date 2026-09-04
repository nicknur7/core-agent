# Incident history moved out of `.claude/rules/subagents.md`

Why this file exists: the prose in `.claude/rules/subagents.md` loads on EVERY prompt. The rules stay there; the dated history explaining why each rule exists lives here, where it costs nothing until someone needs it. Moved automatically by `bin/steering-compress.py`, verbatim, never deleted.

## From “Model routing — by work type, not by tier” — moved 2026-08-27

The table is canonical — it replaced four overlapping prose restatements of itself on 2026-08-06, when the ratchet made redundancy something to be paid for rather than tolerated. This edit paid for a lessons.md entry the same way.

## Haiku safety condition — the 06-11 5-of-25 failure (moved 2026-08-27)

**Haiku's one safety condition.** Brain/graphify extraction runs Haiku IN-SESSION (`Agent()` subagents on subscription auth — the headless `ANTHROPIC_API_KEY` path retired 2026-07-24) and is safe there ONLY with: (1) parent verifies every checkpoint exists and `json.loads`-parses before merge, (2) repair-retry on invalid JSON, then Sonnet fallback. RAW Haiku is forbidden — that was the 06-11 5-of-25 failure. `extract-pending.sh --phase close` bakes all three in.
