# Memory rules

How to read, write, and maintain Core's memory layer. Read this when working with `memory/`, `tasks/`, or any persistent state.

## Memory-first

Read relevant memory files before responding to anything substantive. Lazy-load:
- **At session start (auto):** `memory/current-state.md`, latest `sessions/YYYY-MM-DD.md`, `tasks/lessons.md` (active section). The SessionStart hook surfaces stale-state warnings.
- **On demand:** `memory/about-me.md`, `memory/preferences.md`, `memory/relationships/`, `memory/education/`, project files in `memory/projects/`, `memory/capabilities.md`.
- **Don't pre-load everything.**

## Two memory systems

- **Core memory** (`memory/` in this repo): project-specific, version-controlled, canonical.
- **Claude Code auto-memory** (`~/.claude/projects/.../MEMORY.md`): cross-session user/relationship/feedback patterns.
- Project-specific info → Core memory. Do NOT create `feedback_*.md` inside the Core repo (defensive-save and stop-hook block this).

## Read before asking. Verify before claiming.

**Nothing enforces this. It is on you** (`docs/steering-detail/memory.md`). Before claiming "X is broken," "Y wasn't done," "Z isn't in any file" — exhaust available reads first: `memory/about-me.md`, `memory/relationships/`, `memory/access-log.md`, project files, session logs, the actual git log, the actual file. Hook warnings, current-state.md prose, and prior session summaries all need verification against the live file before you act on them.

If a question is unavoidable, ask exactly ONE specific question — not a list, not a triage table.

**Absence claims need a multi-file grep, not one read.** "X doesn't exist", "never shipped", or any scope claim spanning docs — grep every plausibly-authoritative file first; one read verifies nothing for a claim whose truth lives in a different doc.

**Name the flip when reversing a prior recommendation.** When walking back a position you took earlier in the same conversation, FIRST name what the prior position was AND why you're changing it. Never silently flip — the operator can't audit a recommendation reversal they can't see.

**For strategic / Path / Track / business-direction framings, grep `memory/decisions-log.md` FIRST.** Project files NARRATE current work; `decisions-log.md` RECORDS what was decided and why. Before synthesizing or recommending a direction, your first tool call must be that grep.

## Update memory as you go

When the operator shares new info, update the relevant memory file immediately — in the SAME turn you say you will. **Write it now or do not say it.** Nothing blocks "I'll save that" followed by no Write; `reply-observer.py` records the gap after the fact, never prevents it. (`say-do-gap` retired 08-06 — `docs/enforcement-audit-2026-08-09.md`.)

## Write-first for artifacts

Write substantive artifacts (walkthroughs, plans, tables, drafts) to a file FIRST, then paste a tight summary into chat. Prevents content loss on context compression.

## File path registry

When the operator shares a file path, code location, URL, or doc location → add it to the relevant project's "Key Files" section in `memory/projects/<project>.md`. Don't ask, just add.

## Course-specific files

Live under `memory/education/courses/<slug>/`. Each course is a folder: `overview.md` plus working files. Do NOT put course-specific working files in `tasks/` or `memory/projects/`. `tasks/` is for cross-course or system-level ephemera only.

## current-state.md is narrative, not a queue

Last 1-2 sessions of work + project pointers + pickup-for-next. Prune at session close (defensive-save now stamps timestamp; full prune still requires explicit `/close-core`). Authoritative status lives elsewhere:

- **School:** `~/AI Projects/core-school/memory/education/canvas-by-day.md` (read via peer-Core MCP from life)
- **Projects:** `memory/projects/<project>.md` Active Work / Open Issues / Deferred
- **Decisions:** `memory/decisions-log.md` (append-only)
- **Past sessions:** `sessions/YYYY-MM-DD.md`
- **Capabilities:** `memory/capabilities.md`

Never quote current-state.md prose as authoritative queue status.

## Lessons loop

When the operator corrects you, append to top of `tasks/lessons.md` (newest-first). When a lesson gets enforced by a hook or codified in CLAUDE.md → move it to `tasks/lessons-archive.md`. Lessons that grow past ~25 active entries should be re-curated.

## Context hygiene

- Use `offset`+`limit` for reads when the section is known; pipe verbose bash through `| head -20`; extract only what's needed from a large tool result.

## Research artifacts

When work touches a Core primitive (memory layer, hooks, agents, Sentinel, brain pipeline), is going to take >1h, or is a new system rather than a patch — **research first, build second**. Write the research as an artifact.

Location: `tasks/research/<topic>-YYYY-MM-DD.md`. Template at `tasks/research/_template.md`.

Shape: Question → Sources → Findings → Mapping to Core → Build implications → Caveats.

Three uses: pre-build ("what does published practice say?"), empirical (measure the system to calibrate a parameter rather than guess), post-build audit. The full loop is `/deep-plan` (`.claude/commands/deep-plan.md`); this section is the pointer, not a second copy of it.

Bug fixes, one-off scripts, and routine HW work skip the research pass. The trigger is *primitive touch / new system / >1h*, not "every change."

When a research artifact yields a primitive change (new threshold, new hook, refactor) — surface the recommendation to the operator. **Do not auto-apply primitive changes.**

## Memory file freshness (Last updated stamp)

Personal/project memory files (`about-me.md`, `preferences.md`, `goals.md`, `skills-interests.md`, `relationships/*.md`, `projects/*.md`) carry a `Last updated: YYYY-MM-DD` line in their frontmatter. Bump it when:

- Content is meaningfully edited
- The operator reverifies the content is still accurate (e.g., "yeah that's still right")

**Do NOT silently roll the date forward on bare reads.** A read isn't a verification.

SessionStart (Check g) warns at >30d. Treat it as *the facts may have drifted* — verify before quoting these as current. It skips archived files, `business/` planning docs (which use `Status:` markers) and `tasks/research/`; those need no `Last updated`.
