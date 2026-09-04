# Self-improvement: the track record

_Generated 2026-07-28T08:35:57+00:00 from this Core's live database. Structure and counts only —
no rule text, no correction text. See bin/si-record.py for why._

This is the evidence the loop **learns**. `bin/si-demo` shows the mechanism — a
correction becoming a tested, installed, firing rule in about a second. That proves the
pipe exists. This proves water went through it.

**173 corrections mined** from real sessions, 2026-07-22 to 2026-07-27.

## Rules the system wrote for itself

| origin | total | active | revised at least once | most revisions |
|---|---:|---:|---:|---:|
| friction | 31 | 25 | 19 | 9 |
| legacy | 4 | 4 | 4 | 5 |
| enforcement | 1 | 1 | 1 | 12 |

`revised` counts rules carrying a `prior_spec` — a previous version kept for rollback.
A revision means the rule was changed after installation because evidence said so,
not because a human edited it.

## What the live rules do

| effect | live |
|---|---:|
| inject | 29 |
| block | 1 |

`inject` adds a reminder to context. `block` stops the turn until the
condition is satisfied. Blocks are deliberately rare and must survive a
shadow-proof window before they enforce.

## The hand-written gates, measured against themselves

13 gates carry an intent record — the examples they must
catch and must not — so behaviour can be compared to purpose rather than to a rate.

Intent verdicts: **holds** 13
Rate verdicts: **watch** 6, **within_bar** 7

## Retirement

Rules are removed when evidence says they stopped earning their place:

- **approval-gate** — 2026-07-27: 24 blocks in 34 invocations (41%). Five patches deep and still matching text over meaning — it read [quote redacted] as a stop signal, arming against the broadest authorization in the session. the operator [quote redacted] Implementation archived at .claude/hooks/archive/approval-gate.py

## What this does not claim

These are counts from one deployment — the author's. They show a loop that mines,
installs, revises and retires on its own evidence. They do not show it working for
anyone else, because it has not yet run for anyone else.
