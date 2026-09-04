# Spawning a new Core — clean runbook (federated brain Phase 4 + turnkey Phase 6.5)

The federated build made spawning a new Core a **strict cleanup**: no brain surgery,
no life-centric merge, no "whoever closed last wins." A new Core builds its own graph
from day 1 and auto-joins the shared layer. This is possible because Phases 1-3
generalized every pipeline step to key off `CORE_ORG_ID` / the `tenants` table.

## The one command (Phase 6.5 — turnkey)

```
bin/spawn-core <domain> <org_id> [writer|puller] [--deps]     # dry-run first: add --dry-run
bin/spawn-core ops 5 puller
```

`spawn-core` folds the whole ritual into a single verified flow: clone baseline →
strip scaffold → seed the `tenants` row → set `identity.json` (org_id, domain_label,
`hook_profile.role`) → set `.mcp.json` (`CORE_ORG_ID`, `CORE_INSTANCE`) → compose
`CLAUDE.md` (`@import` the universal `CLAUDE.base.md` + a domain overlay stub) →
[`--deps`] `install-deps.sh` → `init-brain-roles.sh` → `reconcile-hooks.py --apply`
(derive `settings.json` from role + registry) → `seed-hook-dispositions.py` (build the
hook matrix) → **`core-doctor.sh` pass gate** → readback verification. Success = a Core
that stands up with **no hand-editing** of settings.json / identity / tenants / .mcp.json.
The only thing left to author is the domain overlay in `CLAUDE.md` (marked `EDIT THIS`).

`--dry-run` validates args + preflight (tools, DB, baseline reachability, required
scripts) and prints the ordered plan without writing anything.

## What it automates, step by step (co-located Core, e.g. `core-ops` org 5)

1. **Register the tenant** — all three columns are `NOT NULL`; `vault_path` is the Core's
   **instance path** (matches org 1-4: `/Users/…/core-<name>`), NOT a `projects/` subpath:
   `INSERT INTO tenants (org_id, name, vault_path) VALUES (5, 'ops', '$HOME/AI Projects/core-ops');`
2. **Point the new Core's env** at `CORE_ORG_ID=5`, `CORE_INSTANCE=<its path>`, shared `CORE_BRAIN`.
3. **Its sessions/subagents extract automatically** — `extract-core-sessions.py` /
   `extract-core-subagents.py` resolve the slug from `CORE_ORG_ID` via `tenants` and
   write org-suffixed checkpoints (`chunk-core-sessions-ops.json`).
4. **Heavy build wires its graph** — `embed.py --graph-nodes` runs `pass_edges`
   (org-aware) + `pass_origin_edges_db` (DB-native origin backbone) → its entities
   are connected from the first close (this is exactly what took the 4 existing Cores
   to 0% isolation).
5. **Corroboration auto-joins the shared layer** — `corroborate.py` links its concepts
   to the other Cores' via `same_as` (is_cross_org). No manual step.
6. **Scope defaults to `shared`** — mutually aware immediately; mark items `private`
   via `set-scope.py` when needed.

Steps 1-2 are done by `spawn-core`; 3-6 happen automatically on the new Core's first
heavy close. Nothing touches the existing Cores' data. All reversible via
`teardown-org.py 5 --confirm` + `rm` the instance dir.

## Standalone Core (own machine / DB, e.g. an external fork)

Same, except it provisions a **fresh** `corebrain` from `schema.sql`. B3 folded every
migration into `schema.sql`, so a fresh provision matches the running end-state (Source
kind, originates_in, same_as, is_cross_org, scope) — its first heavy build won't crash
on an unknown enum. `verify-schema-checks.py` guards against future drift.

## Teardown (reversibility, §9 M8)

`scheduling/brain-pg/teardown-org.py <org_id> --confirm` — deletes the org's
entities/evidence/edges + tenants row (refuses orgs 1-4). Snapshot first. Archive the
markdown vault separately.

## Still to wire (Phase 5 — UI)

`discoverCores()` in Core OS auto-wires a new Core with `identity.json` into
Overview/nav/search/brain. The brain-tab + settings surfaces for a new org land with
the Phase 5 visualization build.
