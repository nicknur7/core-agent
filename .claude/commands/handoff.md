---
description: Write a self-contained handoff doc capturing current task state so the user can /clear without losing the thread. Use when the conversation is long but the task isn't done — drafts the doc in your voice, saves to tasks/handoff-<slug>-<date>.md, surfaces the resume instruction. Pairs with /clear.
argument-hint: "<slug>"
---

# /handoff

Write a self-contained handoff document so the user can `/clear` the conversation and resume the SAME task in a fresh context window without losing tactical state.

## When this is the right call

Same task continuing, but the conversation has gotten long enough that:
- Rot signal climbing (statusline `rot:N%(d)` field is yellow/red)
- Compaction summaries blurring earlier work
- Model noticeably forgetting decisions made an hour ago

The handoff doc captures what matters; `/clear` wipes the noise.

## Steps

1. **Resolve the slug.** Take it from `$ARGUMENTS` if provided. Otherwise derive a short kebab-case identifier from the current task (e.g. `engine-split-cleanup`, `negotiation-case-prep`, `psy-220-reflection`) and confirm with the user before writing.

2. **Compose the doc — YOU write it directly.** Do NOT spawn a subagent. The parent model is the only context that holds the actual conversation history; a subagent would lose it.

3. **Save to** `tasks/handoff-<slug>-YYYY-MM-DD.md` (today's date in the user's timezone).

4. **Use this exact structure:**

```markdown
---
created: YYYY-MM-DD
slug: <slug>
task: <one-sentence task description>
---

# Handoff — <task title>

## Original task (still incomplete)
<Restate the task as the user originally framed it. Include deadlines, success criteria, stakeholders if any.>

## What's been done
<Bullet list of completed steps. Be specific — file paths, commit hashes, decisions made. The next session should NOT redo any of this.>

## What's been decided
<Decisions made during the session that affect future work. Format: "Decided X because Y." Include rejected alternatives if relevant.>  <!-- privacy-ok: generic engineering vocabulary -->

## What's NOT done and why
<Open work items + blocker for each. "Could not do X because Y." Include things the user explicitly stopped you on.>

## Files touched
<Every file modified or created with a one-line description. Use absolute paths.>

## Exact next step
<The single concrete action that should happen first when the user resumes. No vagueness — name the file, the command, the question to answer.>

## Re-pickup line
> "Read tasks/handoff-<slug>-YYYY-MM-DD.md and continue from the 'Exact next step' section."
```

5. **After writing, surface to the user:**

```
Handoff saved → tasks/handoff-<slug>-YYYY-MM-DD.md

Next:
  /clear
  "Read tasks/handoff-<slug>-YYYY-MM-DD.md and let's continue."
```

6. **Do NOT run `/clear` yourself.** The user decides when to wipe.

## Anti-patterns

- **Don't narrate chronologically.** Capture END STATE (what's true now), not the journey.
- **Don't duplicate `git log`.** Focus on tactical context (decisions, rejected paths, things-not-to-redo) that wouldn't survive in commit history.  <!-- privacy-ok: generic engineering vocabulary -->
- **Don't pad.** A 200-word handoff is fine if the task is small. 600 words is the cap. If you need more, the task should probably be `/close-core`'d, not `/handoff`'d.
- **Don't write to `memory/`.** Handoff docs are tactical scratch, not durable memory. They live in `tasks/`.
- **Don't spawn a subagent.** Only the parent has the actual conversation history.

## Precedent

A good handoff looks like the 2026-05-13 school-worktree-retirement model: mid-session bug discoveries + intentional non-edits + exact next step. That shape works.
