# Core — your instance overlay

This file is YOURS. It layers on top of `.claude/CLAUDE.base.md` (the shared baseline) and is
never overwritten by a baseline sync — `CLAUDE.md` is `per_core_keep`.

Replace everything below with how you want this Core to work.

## What this Core is for

One or two sentences. The domain this instance owns — work, school, a side business, everything.
If you run several Cores against one brain, this is what distinguishes them.

## How to address me

Your name and how you want to be spoken to. The operator name also lives in
`.claude/identity.json`, which is what hooks read; keep them consistent.

## Standing preferences

Things you want applied by default, without being asked each time. Keep these short — a
preference that needs a paragraph is usually a rule, and rules belong in `.claude/rules/`.

## Hard rules

Things this Core must never do without asking you first. The baseline already ships a set in
`.claude/CLAUDE.base.md`; add yours here.
