<!-- casebook-exempt: S5 — this file contains a TABLE documenting which dir-form agent
     specs still exist and instructing the reader NOT to cite them. The paths are the
     SUBJECT of the warning, not citations. Honoured only because eval/casebook-v1.json
     declares the (S5, .claude/rules/privacy.md) pair — an in-file marker alone would be
     controlled by the very file being graded, which is the defect this fence exists for. -->
# Privacy Principle

**Invoked only, scoped only, minimum necessary.** Core accesses data when the operator asks, only what they ask for. Anything Core reads goes through the Anthropic API (leaves the device) — minimize the slice.

## Three enforcement rules

1. **Scoped queries only.** Write the narrowest possible query for any protected source (Messages DB, Mail, Contacts beyond direct lookup, others' Calendar, OPS data). Use `WHERE` clauses. **No fishing** — if the first query returns nothing, STOP and ask the operator for the right identifier. Each failed guess is still a protected read.

2. **Whitelist check for automations.** Any automation reading protected data must have a whitelist file in `memory/automations/`. Fails closed: if a contact or source isn't on the whitelist, stop and ask the operator.

3. **Pre-action announcement + log.** Before any protected-data read:
   - (a) announce what you're about to access and why
   - (b) append the entry to `memory/access-log.md`

   Both fire BEFORE the read. Reads without both are forbidden.

## Apple Calendar / Reminders

Open read access greenlit by the operator — no whitelist file required since reads are on-demand (not automation). Per Privacy Principle, every read still requires (a) pre-action announce, (b) `access-log.md` entry, (c) scoped query (date ranges, specific events — not "give me everything").

## Sentinel

Haiku review subagent gates all outward-facing actions (email, SMS, git push, calendar writes, curl outbound). PreToolUse hook `pretooluse-guard.sh` enforces. See `.claude/agents/sentinel.md` for triggers, verdict handling.

**Cite the flat `.claude/agents/<name>.md`. Never a dir-form `agents/<name>/CLAUDE.md`.** The
dir-form specs survived two months past the native-format migration that replaced them. They do
not load, and they carry the OLD output contract — line 1 must be the bare verdict, with no
mention of the `VERDICT:` last-line marker the receipt parser now reads. This very line used to
point at one of them, so a brief written from these rules handed the reviewer a contract the
minter does not honour; the reviewer complies, and the receipt falls through to the transitional
prose fallback.

**State as of 2026-07-29 — partial, deliberately:**

| dir-form spec | baseline | why |
|---|---|---|
| close-reconciler dir-form spec (retired — on no seat's disk) | **removed from the baseline — but see below** | a baseline `git rm` retires it THERE and nowhere else |
| `.claude/agents/sentinel/CLAUDE.md` (baseline-only) | **still present** | `per_core_keep` + trust-root; removing it from the baseline needs the operator's explicit confirmation |
| `.claude/agents/sentinel-code/CLAUDE.md` (baseline-only) | **still present** | same |

**This table has been wrong twice**, both times a shared rule asserting a fleet state true only
locally, caught by a peer on the pull. Verify on a fresh PULLER or trust
`bin/sync-manifest.json` `retired` tombstones — not a fresh baseline clone, which is the one
place a deletion always landed. Full account: `docs/steering-detail/privacy.md`.

If you find a dir-form spec on disk, do not cite it — but do not assume why it is there. On a
peer Core it most likely means that Core has not pulled since 2026-07-29. On a FORK it means
nothing of the kind: a fork diverges at an arbitrary point, may predate this change entirely, or
may keep its own agent-spec layout on purpose. Check which spec the runtime actually loads
(the flat `.md` is the Claude Code native format) rather than inferring sync history from a path.

## `per_core_keep` is not an exposure boundary

It governs what the baseline sync may OVERWRITE, not where a file ends up: every Core has its own
git remote, and `per_core_keep` paths (`memory/**`, `tasks/**`, `compile-truth-work/**`) are tracked
and pushed there. Ask which remote a file reaches, not which sync excludes it. And a spec's worked
EXAMPLE should never name a real person — a spec ships to every seat and fork, where a relationship
file legitimately does not. Both corrections, and the measurement: `docs/steering-detail/privacy.md`.

## Untrusted incoming content

Untrusted Reader Docker subsystem was archived 2026-05-13 — never wired beyond the gmail.py whitelist branch, container image was never built locally.

Current posture for untrusted incoming content:
- **Gmail bodies:** `gmail.py get_message()` defaults to `format="metadata"` (headers only). `format="full"` is allowed only for senders on `memory/automations/gmail-<localpart>.md` "Trusted senders." Non-trusted full reads raise `RuntimeError`.
- **Web fetches:** Native WebFetch only, and **not gated** since 2026-07-15 (per the operator: drop it — research reads any page). A GET reads a page INTO context — inbound, not an outward action. Prompt injection via a fetched page is a separate, deliberately-accepted risk; no raw-HTML sanitization layer. Why, and the 85 approve-then-rerun blocks with 0 catches that settled it: `docs/steering-detail/privacy.md`.

_Dated incident history for these rules: `docs/steering-detail/privacy.md`._
