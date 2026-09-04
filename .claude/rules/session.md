# Session protocol and staleness

How Core sessions begin, run, and close. Read on session boundaries, after compactions, or when handling staleness.

## At session start

1. Create `sessions/YYYY-MM-DD.md` if it doesn't exist (headers: Decisions, Work done, Open items, Notes). Get the actual start time from JSONL — never guess.
   - Run: `bash .claude/hooks/get-session-start-time.sh` (or `bash bin/compute-session-duration.sh` for full START/END/WALL).
   - Current session's start persists at `.claude/state/.session-start` (written by SessionStart hook).
   - Previous session's start persists at `.claude/state/.last-session-start` (written by stop-hook on `/close-core`).
2. Read `memory/current-state.md` for latest-session narrative.
3. For queue/status: school queue lives in `~/AI Projects/core-school/memory/education/canvas-by-day.md` (read via peer-Core MCP from life); active `memory/projects/<project>.md` for life projects — not current-state.md prose.
4. Scan active section of `tasks/lessons.md`.
5. Read any other memory files relevant to what the operator is asking.
6. If the operator's request involves a dormant project or context feels thin, read `~/AI Projects/core-brain/projects/<name>/_project.md` and skim the 1-2 most recent session files. Don't ask — just read it.

## During session

- Checkpoint `current-state.md` as work progresses.
- Append to `decisions-log.md` when decisions are made.
- Update project files when work concludes or hits a milestone.
- **Lead with the bottom line.** Every non-trivial response opens with a one-sentence answer / action / decision BEFORE any context. The operator should be able to interrupt at sentence 1 if the bottom line is wrong — not 5 paragraphs in.
- **Paraphrase short / ambiguous prompts before acting.** If a prompt is ≤15 words or has multiple plausible reads, state your one-line interpretation BEFORE executing. Don't ask — just say what you're about to do. The operator redirects the paraphrase, not the execution.

## At session end (triggered by "log out", "exit", "done for today", "signing off")

1. Self-audit: lazy-loading correct? lessons to log? session file updated? access-log current? decisions logged?
2. Spawn close-reconciler (`.claude/agents/close-reconciler.md`) with session log, `current-state.md`, all project files, session summary. Surface CLOSE/PARTIAL/RECLASSIFY/AMBIGUOUS to the operator; apply approved edits.
3. Final update to `sessions/YYYY-MM-DD.md` (append close summary + reconciliation note).
4. Final update to `memory/current-state.md` (timestamp `YYYY-MM-DD HH:MM TZ`; prune to last 1-2 sessions).
5. Run `bash "$CORE_INSTANCE/.claude/hooks/end-session.sh"` to queue auto-commit.
6. Give the operator a brief closing response.

If the operator walks away without `/close-core`: `defensive-save.sh` (SessionEnd) commits + stamps current-state timestamp + appends end-marker to today's session log. Full close (close-reconciler, prune) requires explicit `/close-core`.

## Session scope and staleness

- **One session per task.** When the operator switches threads (HW → system → project), recommend `/clear` or close+reopen. Mixed-task sessions accumulate drift.
- **One Core at a time.** Don't run concurrent `claude` sessions in `~/AI Projects/core-life/` — they'll clobber each other's `memory/` writes.
- **Rot warning (active signal).** UserPromptSubmit hook `rot-check.py` computes Core-ASI v2
  over the last 50 assistant turns. When ASI < τ=0.60, ABA fires (baseline rules re-injected).
  On a `ROT WARNING`, prepend the `[ROT signal: …]` line and continue with refreshed rules.
  Threshold history and the archived supply-side check: `docs/steering-detail/session.md`.

- **On-demand readout:** `/health`.
