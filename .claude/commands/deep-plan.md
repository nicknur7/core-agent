---
description: Plan-to-no-questions for a system/primitive task — recall the brain, research (system + external), grade built-vs-original-intent, write a complete plan artifact, surface it, wait for go. The loop the operator has asked for ~10 different ways.
argument-hint: "<the task / area to plan>"
---

# /deep-plan

The structural version of Nick's most-repeated directive — *"verify-first, plan to the
point of no questions, research first, explain and wait for my go"* (blessed as
`feedback_verify_first_operating_mode`; reinforced ~10 ways across the brain). When a
task is a **Core primitive / new system / >1h / "do it right"** ask, run this loop instead
of free-handing it. Built from the 2026-06-26 memory-overhaul session, which is the
reference example of this going right.

**Use it when:** the task touches a Core primitive (memory, hooks, Sentinel, brain pipeline,
agents), is a new subsystem, is open-ended/"figure out the right way," or the operator says
"plan it out / research and plan / do it right once." **Skip it** for routine edits, bug fixes, and
conversational turns — those just get done.

## The loop

**1. Recall the brain FIRST.** Before anything: `mcp__core-brain__recall_similar` /
`get_entity` / the `claude-brain` skill on the task + any named person/project/past decision.
The brain is ground truth; local files drift. Pull *intent and history* from it, not from
`memory/` summaries. (This is the step that was missing every time a plan went wrong.)

**2. Research — system + external.** Don't synthesize from one read.
- **System:** measure the actual thing (file census, hook map, live DB/state) — don't guess.
- **External:** published practice if the task is a known problem class (context engineering,
  agent memory, security patterns). The Anthropic context-engineering doc is the bar.
- **Fan out** mechanical census/research to **parallel Sonnet subagents** (pinned Sonnet, never
  inherited Opus). **Heads-up the spend first** — "spawning N Sonnet subagents (~Xk tokens) on
  your capped window" — the operator can intervene before the spend. Judgment/synthesis stays inline.

**3. Grade what exists against its original intent.** When the task touches an EXISTING
subsystem (not a greenfield build), do NOT stop at "is it healthy?" — trace each piece to the
problem it was built to solve and grade whether it *still* does. A three-column table per
subsystem: **Original problem (recalled from the brain in step 1) → What we built → Does it
actually solve it? (verified against live behavior/data, not the doc's own claim).** Remediation
flows from column 3. A thing that's "healthy" but no longer serving its original reason is drift
the health check alone won't catch — this is where you catch a *resolved* item still carried as
*open*, or a gate that solves its problem but over-blocks. (Added 2026-07-01 from the era-item +
approval-gate cases — the operator's question: whether what was built still serves the reason it was built.)

**4. Write the plan to the no-questions bar.** A research artifact at
`tasks/research/<topic>-YYYY-MM-DD.md`: diagnosis (measured) → external grounding → a phased,
**reversible**, **autonomously-executable** plan where every decision is already made. If you'd
have to ask the operator mid-execution, the plan isn't done — make the call and state it. The ONLY
thing left open should be genuine judgment the operator owns (e.g. "push trust-adjacent shared code to peers
now or after"). Name it explicitly.

**5. Surface it in Core OS, then WAIT.** Append the artifact to `memory/reading-queue.json` (instance-only — `memory/**` is per_core_keep, absent on a fresh clone) so it
pops in the Reading tab (`:3737/reading`) — never SendUserFile, never a wall of chat text. Give a
tight bottom-line summary + the one open call. Then **stop and wait for "go."** Do not start
executing. (Primitive changes are never auto-applied.)  <!-- privacy-ok: generic engineering vocabulary -->

**6. On "go" — execute autonomously, end to end.** No re-litigating, no "simpler version" pitch
(greenlight → execute). Work in reversible phases, commit per phase, test every primitive/hook
change before keeping it, and **hold the genuinely-irreversible / affects-others step** (baseline
`/sync push`, outward sends) for an explicit nod even within the go. Report results per phase.

## Why this is a skill and not a rule
Nick has given this directive ~10 different ways and it still got missed — because prose rules
don't graduate (that's why Core has hooks). Making it an invokable loop + the brain-recall hook
fixes (recall now requires a *brain* read) is the structural version. Pairs with the
`plan-not-execute` / `verify-dont-claim` learned contracts and `tasks/lessons.md`'s
Find→Present→Approve rule.
