# Incident history moved out of `.claude/CLAUDE.base.md`

Why this file exists: the prose in `.claude/CLAUDE.base.md` loads on EVERY prompt. The rules stay there; the dated history explaining why each rule exists lives here, where it costs nothing until someone needs it. Moved automatically by `bin/steering-compress.py`, verbatim, never deleted.

## From “Rules files — ALL of these load at launch, every turn” — moved 2026-08-27

Not "on demand". `.claude/rules/*.md` auto-loads by directory convention and `@import` expands at
launch, so naming a file here defers nothing. Mechanism: `docs/steering-load-mechanics-2026-07-30.md`.

## Three Anti-Patterns — why the nine Stop gates were retired (moved 2026-08-27)

State-claims-from-memory, paraphrase-instead-of-read, say-without-do. **Nothing stops these at
the last instant** — the nine Stop gates that did were retired 2026-08-06, because a Stop hook
fires after the reply is sent. What replaced them is supply, not enforcement: `session-presence.py`
injects the clock, START/WALL and peer HEADs, so the claims are answerable from context.
`verification-trigger.py` survives and fires BEFORE the reply. **If a hook blocks, follow it** —
but assume nothing catches you. Record: `docs/enforcement-audit-2026-08-09.md`, live inventory
`memory/capabilities.md`, tombstones `bin/hook-registry.json`.
