# Subagents and model routing

Read when about to spawn a subagent or considering a tier switch.

## Three tiers

Handle inline → spawn a subagent for execution-shaped work → build a full agent only for proven recurring domains (the operator's approval required).

## ENFORCED, not advised — `delegation-gate.py` (PreToolUse)

Counts consecutive mechanical tool calls (Bash/Read/Edit/Write/Grep/Glob) since the last
Agent/Task/Workflow call and fires every 14 — advisory and fail-open, since a counter cannot know
whether work was parallelisable. The measured miss it exists for: delegation happens when work looks
RISKY, never when it is merely LARGE. Why prose alone failed for months: `docs/steering-detail/subagents.md`.

## When to spawn a subagent

Any of:
- Independent parallelizable work (multiple file batches, multiple targets).
- Bulk reads where you only need a structured summary back, not the raw content.
- Current context is heavy and you can articulate what you need without keeping the raw content.
- The work is mechanical/extraction-shaped (the operator won't be reading the raw subagent output).

## When to stay inline

Judgment, synthesis, reasoning, or anything where the operator would have to re-verify a subagent's output. Don't fan judgment to subagents — that pushes the verification burden back onto the operator.

## Briefs

- **Tight always:** goal + exact paths + constraints + deliverable format + explicit tool allowlist.
- **Output cap:** add when warranted; skip when the agent should pick its own length.
- **Tool minimization:** name allowed tools in every brief. Default deny anything not listed.
- **Heads-up before parallel-spawn:** "Spawning N Sonnet subagents for X." The operator can intervene before the spend.
- **Pre-fan-out tier check (≥3 agents):** stop and pin the model explicitly — see Model routing below, which states this rule once.
- **Verify write-path access on ONE test agent BEFORE parallel batches.** Subagent sandbox is tighter than main thread — sandbox-denied parallel runs waste tokens.
- **Dispatch-verify standard (2026-06-09 — third recurrence of claimed-done-without-writing):** subagents that produce small/medium artifacts (≲30KB JSON) are briefed **Read-only** and RETURN the artifact as their final message; the PARENT writes the file. When the artifact is too big to return (e.g. extraction checkpoints), the subagent Writes but must readback-confirm in its brief, AND the parent MUST verify every expected file exists + parses before any downstream step (merge, ingest). A subagent's "done" is a claim, not evidence — count outputs against inputs every time.

## Model routing — by work type, not by tier

Model identity is best-effort (no live signal). **A subagent's model is independent of the session model** — set it with `model:` in every call, in BOTH directions: pinning a sub-task up or down never moves the operator's session.

**Never let a fan-out INHERIT the session tier** — the recurring "you should have done sonnet" miss. On an Opus session an unpinned N-agent fan-out all runs on Opus, so pin every agent in a ≥3 fan-out explicitly: Sonnet/Haiku for mechanical phases (bulk reads, extraction, classification), session tier only for judgment. Kept as a rule and not a hook because session-model detection is best-effort, so a hook would fire on bad data.


### Task → model quick table
| Work | Model | Where |
|------|-------|-------|
| Bulk file reads for a summary, classification, extraction | Haiku 4.5 | pinned subagent |
| Code-claim verification, per-tab refactors, execution batches, most "do X across N files" | Sonnet 4.6 | pinned subagent |
| Orchestration, synthesizing agent outputs, drafting what the operator reads, decisions the operator re-verifies | Opus 4.8 | session (inline) |
| Adversarial review of a plan/design, "quality product" critique, hardest architecture call | Fable 5 | pinned-UP subagent (session stays Opus) |

_(A Core may add domain-specific rows in a non-synced `.claude/rules-life/` overlay.)_

**Fable spend:** 2× Opus ($10/$50 per MTok). ONE hard sub-task at a time; never a Fable fan-out without the operator's explicit go; warn their usage window first. `model: "fable"` works in Agent/Workflow calls.

**Haiku's one safety condition.** Brain/graphify extraction runs Haiku IN-SESSION (`Agent()`
subagents on subscription auth) and is safe there ONLY with: (1) parent verifies every
checkpoint exists and `json.loads`-parses before merge, (2) repair-retry on invalid JSON,
then Sonnet fallback. RAW Haiku is forbidden. `extract-pending.sh --phase close` bakes all
three in. RAW Haiku produced 5 bad checkpoints of 25 on 2026-06-11 — the failure this exists
for; full account in `docs/steering-detail/subagents.md`.

## Escalation as suggestion, not gate

If you think the task would genuinely benefit from Opus depth, say so before starting — *"This looks Opus-tier; want to switch?"* Frame as capability call, not billing question. The operator decides.

## Background subagents

`run_in_background: true` auto-denies non-allowlisted bash and writes to peer projects (e.g., `core-ui/` from `core/`). For new bash patterns or peer-project writes, use foreground or inline.

## Full agents

Live as flat `.claude/agents/<agent-name>.md` files (the Claude Code subagent format — YAML frontmatter for name/description/tools + instructions in one file). Don't build without the operator's approval. Existing: `close-reconciler`, `sentinel`, `sentinel-code`.

_Dated incident history for these rules: `docs/steering-detail/subagents.md`._
