# Incident history moved out of `.claude/rules/privacy.md`

The rules stay in the rules file; the dated history explaining why each rule exists lives here, where it costs nothing until someone needs it. Nothing is deleted — every block below is verbatim.

## Dir-form agent specs — fleet state table (RESTORED to the rules file)

This section briefly held the dir-form state table. It went BACK to
`.claude/rules/privacy.md` the same evening: `eval/casebook-v1.json` pins those three table
rows as live lines of that file (an S5 exemption), and `test_exemption_is_line_scoped`
asserts every named line still matches verbatim. Content another mechanism pins is not free
to relocate, and the compressor's guard cannot see a pin that lives in a casebook.

Leaving the table here as well put the same citation in two shared files, which
`test_shared_cites_fleet_wide` correctly flagged. Only the narrative moved.

## Untrusted incoming content — why WebFetch is ungated (moved 2026-08-27)

- **Web fetches:** Native WebFetch only, and **not gated** since 2026-07-15 (per the operator: drop it — research reads any page). A GET reads a page INTO context — an inbound read, not an outward action — and gating it as outward produced 85 approve-then-rerun blocks across Cores with 0 real catches. The untrusted-incoming-content risk (prompt injection via a fetched page) is a separate, deliberately-accepted risk. No raw-HTML sanitization layer. _(This line described an allowlist that `pretooluse-guard.sh` had already stopped enforcing; corrected 2026-07-28 when an unrun test surfaced the contradiction.)_

## dir-form fleet-state table — RESTORED 2026-08-27

_Archived, then lost when a later revert rewrote this file, then put back from
`.claude/state/steering-compress-log.jsonl`. The log body is why it was recoverable._

**State as of 2026-07-29 — partial, deliberately.** The three-row table itself stays in
`.claude/rules/privacy.md`: `eval/casebook-v1.json` pins those rows verbatim as an S5
exemption. Keeping a second copy here put the same citation in two shared files, which
`test_shared_cites_fleet_wide` flags. The narrative that surrounded it is below.


**Do not cite them; their presence is evidence of nothing.** A fresh clone starts with two,
finance/school/ops still have the third.

**This table has been wrong twice the same way** — a shared rule asserting a fleet state true only
locally, caught both times by a peer on the pull. The first correction's remedy ("check a fresh
baseline clone") CAUSED the second: the clone is the one place a deletion always landed, while
`rsync -a` with no `--delete` means it reached no puller. Verify on a fresh PULLER, or trust
`bin/sync-manifest.json`'s `retired` tombstones.


---

### 2026-08-28 — `per_core_keep` mistaken for an exposure boundary, twice in one day

**It governs what the baseline sync may OVERWRITE. It says nothing about where a file ends up.**
Named by core-school 2026-08-28 (bus #5808) after core-finance made the same correction about
CLAUDE.md that morning — twice in one day, which is why it is written down here instead of being
re-derived a third time.

The trap: a path is in `per_core_keep`, so it never reaches `nicknur7/core-agent`, so it feels contained.
Every Core also has its OWN git remote, and `per_core_keep` paths are tracked and pushed there like
anything else. `memory/**`, `tasks/**` and `scheduling/brain-pg/compile-truth-work/**` are all on
GitHub right now.

Measured when it came up: a real third party's email address appears in tracked files on four of
five seats — 11 on life, 5 on business, 2 each on school and finance. **Not an incident**: all five
origins verified PRIVATE with `gh repo view --json isPrivate` (checked, not assumed), and the
content is Nick's own records about someone he knows, legitimately derived from his own files. The
files stay. Deleting a person's record because their address appears in it is not a privacy fix.

What DID need fixing was the **agent spec**, and the distinction is the useful part: a spec is a
template that gets read, copied and shipped to every seat and every fork. Using a real person as its
worked example spends someone's privacy for no reason, and `<recipient@example.com>` reads exactly
as well. A relationship file naming the person it is about is the system working; a Sentinel rule
example naming them is an accident nobody chose.

Before reasoning about exposure, ask which remote a file reaches — not which sync excludes it.

