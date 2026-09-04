# The four rules that promised gates which had not run in three days

Status: RECORD — the archaeology behind four one-clause disclosures in the always-loaded rules
Date: 2026-08-09
Tool: `bin/enforcement-audit.py` · Found by: core-life · Blocked and shaped by: core-business

**This file loads never.** That is the point of it. The rules files carry the *duty* and one clause
of disclosure plus a pointer here; the history lives where it costs nothing per prompt.

---

## What was false

Four claims, in files loaded on **every prompt, on every Core**:

| file | claimed | truth |
|---|---|---|
| `.claude/rules/memory.md:20` | "the state-claim-gate Stop hook now enforces this structurally" | retired 2026-08-06 |
| `.claude/rules/memory.md:24` | "one read satisfies the state-claim-gate regex" | same hook, same retirement |
| `.claude/rules/memory.md:32` | "the say-do-gap Stop hook blocks 'I'll save that'" | retired 2026-08-06 |
| `.claude/CLAUDE.base.md:122` | "`time-claim-gate.sh` enforces" | retired 2026-08-06 |

All three hooks are still on disk. **None is registered in any settings file**, so none had run in
three days. The only things still referencing `time-claim-gate` are two *other* retired hooks and the
inventory tooling.

## Why it was worse than ordinary staleness

`CLAUDE.base.md` already documented the 2026-08-06 retirement honestly, in a section written for
that purpose. So `memory.md` was contradicting a file loaded beside it, every single turn, and the
contradiction survived because **nothing compared the two.**

A rule that promises a net which is not there does not merely fail to help. You relax against it, and
the cost shows up only as a mistake nobody caught. Silence would have been safer than what these
files said.

## The fix, and the tension it created

`bin/enforcement-audit.py` **derives** the comparison — reads the rules, reads `settings.json`,
matches hook-name-plus-enforcement-verb, skips lines whose own wording says the hook is retired.
There is nothing in it to update, so it cannot itself go stale; a hand-maintained list of dead hooks
would be the same bug one level up. Wired at SessionStart, not close: catching this at close means a
session was already spent trusting it.

**Then core-business measured what the honest version cost.** Four disclosures written out in full
added ~404 tokens *per prompt, on five Cores, forever* — and blew both Cores' steering-budget
ratchets. Its framing, which is the durable finding:

> Every honest disclosure about a retired gate costs tokens on every prompt forever. That is a real
> tension and you do not resolve it by staying silent — the disclosure is right. You resolve it by
> writing the duty and moving the archaeology out.

It also caught the tone failure I had specifically asked it to look for. Three of the four rewrites
ended on an obligation; the time-claim one ended on *"you are given what you need to be right and are
trusted to use it"* — which reads as **permission**, not responsibility. "Trusted to use it" is what
you say to someone you are not going to check on. And the actual rule sat *above* nine lines of
history, so on a skim the archaeology was the content and the obligation was a preamble.

**Shape adopted for all four:** duty first, one clause of disclosure, pointer to this file.

## The precedent I had already set and then ignored

Commit `7e6667c`, 2026-08-06, same file set, same author:

> *"fix(steering): my own doc fix blew the steering-budget ratchet — paid for it by consolidating"*

Same class, three days apart, recorded in the history of the file I was editing. And
`bin/tests/test_steering_budget.py` prints, next to `memory.md`, the annotation
**"←enforcement-history narratives that could be pointer"** — the exact diagnosis business arrived at
independently. **I had a test that would have caught this and did not run it before shipping.**

That is the sixth time in one night that the answer was already recorded somewhere in the system
before it was rediscovered — and the first where the record was mine, in a commit message, in the
same file's own history.

## The 2026-08-06 retirement itself, and what replaced it

Moved here from `CLAUDE.base.md` on 2026-08-09 for the same reason as the four disclosures: it was
~330 tokens of history charged to every prompt on five Cores.

Nick retired nine Stop hooks on 2026-08-06, because **a Stop hook fires after the reply is already
sent** — it cannot prevent, only fail the turn and make him read it twice. The three anti-patterns
they targeted are state-claims-from-memory, paraphrase-instead-of-read, and say-without-do. The trade
is a real loss on one side: nothing stops these at the last instant now.

What exists instead:

- **SUPPLY** — `session-presence.py` (UserPromptSubmit) injects live clock, session START/WALL, and
  each peer's HEAD + last-synced baseline, so duration and "all Cores" claims are answerable from
  context. That is what the retired gates demanded a tool call for.
- **PRE-EMPT** — `adversarial-review-gate.py` (PreToolUse) checks a blast-radius command before it
  runs. Shadow: advises, does not block.
- **OBSERVE** — `reply-observer.py` (MessageDisplay) reads the final reply and *provably cannot block
  it* — which is what makes it safe to run and useless as enforcement. Read via
  `bin/reply-violations.py`; scored by `bin/si-objective.py`, which will not report a zero unless a
  liveness probe proves the detector still works.

`verification-trigger.py` survives — it fires BEFORE the reply.

These anti-patterns are now **measured, not prevented**, and whether supply reduces them is open with
one Core's early evidence. Retired hooks are tombstoned in `bin/hook-registry.json` with reasons,
never silently deleted; `memory/capabilities.md` is the live per-Core inventory.

## Related

- `bin/enforcement-audit.py` — the deriving tool; `--root` for cross-Core, refuses to guess
- `bin/tests/test_steering_budget.py` — the ceiling that made this a cost rather than a free good
- `.claude/hooks/session-start-check.sh` — four-state wiring: clean / findings / crashed / missing

> **Moved from `tasks/research/` to `docs/` on 2026-08-09.** Four SHARED steering files cited it, and `tasks/**` is `per_core_keep` — so the citation resolved on life and dangled on business, school, finance and ops. Found by core-business the same hour the casebook's S2 check started working, which is the point: S2 had been an always-pass check, so nothing had ever read the lint's answer.
>
> **A SHARED FILE MAY ONLY CITE SHARED PATHS.** A pointer into per_core_keep is a dangling reference on every Core but the author's — and the author is the one seat that cannot see it.
