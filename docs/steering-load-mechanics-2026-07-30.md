# Why the kernel is 10,800 tokens — it is structural, not editorial (2026-07-30)

## Question

The master plan's Phase 4.1 sets a 4,000-token budget for the always-loaded steering surface.
Measured, it is 10,809. Two days of editing redundancy out of the rules files has recovered ~400
tokens. Is the remaining 6,800 reachable by more editing, or is something structural holding it?

## Sources

- `https://code.claude.com/docs/en/memory.md` — quoted below.
- `https://code.claude.com/docs/en/skills.md` — quoted below.
- Direct observation of this session's own system prompt.

## Findings

**1. `.claude/rules/*.md` is auto-loaded by directory convention, not by import.**

> "Place markdown files in your project's `.claude/rules/` directory… All `.md` files are
> discovered recursively… **Rules without `paths` frontmatter are loaded at launch with the same
> priority as `.claude/CLAUDE.md`.**"

This is the whole answer to where the tokens go. Nothing in `CLAUDE.md` imports the five rules
files; they load because of where they sit. Confirmed against this session's own system prompt,
where all five appear inlined and labelled "(project instructions, checked into the codebase)".

**2. The "Pointers (read on demand)" heading in `CLAUDE.base.md` is false.** It lists the five
files as if naming them defers their cost. It does not. They were fully loaded on every turn of
every session for as long as that heading has existed.

**3. `@import` is not lazy either.**

> "Imported files are expanded and loaded into context at launch alongside the CLAUDE.md that
> references them."

So the `@.claude/CLAUDE.base.md` line at the top of `CLAUDE.md` is a structuring device with no
token benefit — worth knowing before anyone reaches for it as an optimisation.

**4. Two real deferral mechanisms exist.**

- **`paths` frontmatter** on a rules file: loads only when Claude reads a file matching the glob.
- **Skills** (`.claude/skills/<name>/SKILL.md`): *"a skill's body loads only when it's used, so long
  reference material costs almost nothing until you need it."* Auto-invoked on `description` match,
  or manual-only with `disable-model-invocation: true`.

## Mapping to Core — and why the obvious move is wrong

The obvious move is to convert all five rules files to skills and reclaim ~6,800 tokens per turn.
**That would be a mistake, and naming why is the point of this artifact.**

These five files are **action-triggered, not path-triggered**. `subagents.md` is needed when about
to spawn an agent. `privacy.md` is needed before a protected read. `memory.md`'s
grep-decisions-log-first rule is needed when writing strategic prose — not when touching
`memory/`. A `paths` glob cannot express any of those.

Skills *can* express them, via `description` match. But that hands the loading decision to my own
in-the-moment judgement, and this entire system exists because that judgement is unreliable —
`CLAUDE.base.md` says it directly: *"Discipline rules failed; structural enforcement now exists."*
A rule that loads only when I notice I need it is a rule I can fail to notice, and the failure is
silent. Converting the honesty rules or the anti-pattern gates to on-demand skills would trade a
measurable token cost for an unmeasurable behavioural one.

**The split is not file-by-file. It is within each file.** Every one of the five contains:

- a short RULE that must never be missed (3–10 lines), and
- long REFERENCE detail: enforcement history, tables, the story of the incident that produced it.

`privacy.md` is the clearest case — the Privacy Principle is three sentences; the rest is the
Sentinel spec, a dir-form-spec status table, and a corrected-claim narrative. `subagents.md` is a
paragraph of routing policy plus a tier table and spend caveats.

## Build implications

1. **The 4,000 target is reachable, and not by editing.** Keep a genuine kernel — the rules whose
   absence changes behaviour — in `.claude/rules/`. Move reference detail to skills whose
   `description` names the moment it is needed. Estimated recoverable: 5,000–6,000 tok/turn.
2. **Do it rule-by-rule, with the hook suite as the proof.** The test for "did we move something
   load-bearing" already exists: every hook fixture must stay green, because the gates encode the
   rules the prose describes. That is the same check that proved the 8,198→307 ABA cut lost no
   enforcement.
3. **Do NOT move:** the honesty rules, the three anti-pattern rules, the hard rules (money,
   outward actions, force-push), or the trust-root exclusions. Their cost is the price of them
   being unmissable.
4. **Fix the false heading regardless.** "Pointers (read on demand)" should say what is true:
   these load at launch, every turn.
5. This is a **shared-file, fleet-wide** change and per the plan's §7 gets an adversarial pass
   before shipping.

## Caveats

- The `paths` frontmatter mechanism is documented but untested here. Before relying on it, verify
  on one low-risk file that the content genuinely does not appear in a session where no matching
  file is read.
- Skill auto-invocation quality is a function of the `description`, and a bad description fails
  silently — the same failure class this session spent the day removing. Any converted rule needs
  a check that it actually loads when it should, not just that it stopped costing tokens.
- The token figures are `len(text)//4`, an approximation. The relative sizes are what matters.
