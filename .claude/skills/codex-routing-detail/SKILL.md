---
name: codex-routing-detail
description: Reference detail for delegating work to Codex (GPT-5.6) — the full task→model table with pricing, why the danger-full-access fence is shaped the way it is and how pretooluse-guard enforces it structurally, fork-safety for Cores without the codex CLI, and the data-exposure rules for Cores holding sensitive material. Load when choosing a Codex model tier, when launching Codex with a non-default sandbox mode, when a Codex invocation is blocked by the guard, when adding Codex to a new Core, or when deciding what context to hand Codex on a finance/business Core. The RULE itself lives resident in .claude/rules/codex-routing.md — this is the detail behind it.
---

# Codex routing — the detail behind the rule

The resident rule in `.claude/rules/codex-routing.md` is the part that must never be missed.
This file is the reference half: the table, the reasoning, and the edges.

Split out 2026-08-02 (Phase 4, kernel diet). Before that, all of this loaded on every prompt of
every session on every Core, including Cores with no `codex` binary installed.

## Scope

**Fleet-wide as of 2026-07-24** — Codex was expanded from life+ops to every Core (life, business,
school, finance, ops) per the operator. The routing policy is SHARED (syncs via the `.claude/rules` dir)
so it is one source of truth rather than five drifting copies.

A Core may add local posture in a non-synced `.claude/rules-<core>/` overlay. On core-life that
overlay carries the triad-orchestration default and the operator's cost directive, deliberately kept
OUT of the baseline so a fork never inherits the operator's personal directives.

**Fork-safe / conditional.** This policy is INERT wherever the `codex` CLI is absent — no codex
command is ever invoked, so nothing fires. A fork that pulls the baseline inherits it harmlessly;
it activates only once that environment actually has Codex. Verify with `which codex`.

## Task → model table

Codex additions to the `subagents.md` quick table.

| Work | Model | Price | Where |
|------|-------|-------|-------|
| Straight-line code execution / grunt edits (version bumps, scaffolding, changelog) | **Codex Terra** | $2.50/$15 per MTok | `/codex:rescue` (OpenAI meter, off the Claude window) |
| Trivial mechanical code passes | **Codex Luna** | $1/$6 per MTok | `/codex:rescue --model luna` |
| Adversarial code/design review before a blast-radius change | **Codex Sol** (or Fable 5) | — | `/codex:adversarial-review` — cross-model diversity |

Two shapes worth naming separately:

- **Grunt execution → Terra/Luna.** Straight-line code work that does not need Claude's engineering
  depth. Runs on OpenAI's meter, sparing the capped Claude window.
- **Adversarial review → Sol.** A strong abstract reasoner (ARC-AGI-2 92, FrontierMath 89) pointed
  at Claude's own design assumptions. **Gate by blast radius, not diff size** — reversible = skip;
  migrations, baseline pushes, irreversible = review.

## Why the fence is shaped the way it is

Codex runs its own agent loop **outside every Core hook, Sentinel gate and privacy gate** once
launched, and GPT-5.6's own system card notes it is *more* prone to unrequested actions than its
predecessors. Nothing inside a running Codex session can be gated by Core.

That single fact determines the whole fence: **the sandbox mode at launch is the only thing that
can prevent an outward action mid-run.** Capping it at workspace-write is therefore what
transitively enforces "Codex never performs an outward action" — there is no second line of defence
further in.

Consequences:

- Read-only for review. Workspace-write at most, for scaffolding.
- **Never** `danger-full-access`.
- Never an outward action (push / send / curl / protected read). Output routes back through Claude
  and the existing gates.
- **Never enable the plugin's Stop-hook review gate** — OpenAI warns it loops and drains usage.
- Treat Codex review output as **leads to verify, not gospel**; its open-ended hallucination rate
  is high.

### Structural enforcement

The shared `pretooluse-guard.sh` (already fleet-wide) categorically **hard-blocks — exit 2, no
Sentinel token can approve, same tier as force-push-to-main**:

- any Codex invocation carrying `--dangerously-bypass-approvals-and-sandbox`,
  `--dangerously-bypass-hook-trust`, or `-s danger-full-access`
- any Codex launch while `~/.codex/config.toml` sets a `danger-full-access` default

This fence is inert where no `codex` command is invoked, so it ships safely to non-Codex Cores.

## Privacy note — sensitive-data Cores

Codex adds a SECOND external model (OpenAI) to Core's data-exposure surface, and reads whatever
context it is handed.

On Cores handling sensitive data — **finance** (brokerage/trading), **business**
(workplace/employer material) — keep inputs scoped to the actual work: pass explicit diffs and file
lists to review, not the whole repo, and be deliberate about pointing it at `memory/`.

Nick's standing call (2026-07-24) was full-default routing everywhere; this note is the
minimize-the-slice discipline that still applies to what Codex is *handed*, per the Privacy
Principle.
