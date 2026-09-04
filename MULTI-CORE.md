# Multi-Core architecture

`nicknur7/core-agent` ships as a shared baseline that can spawn one or more
sibling Cores, each scoped to a domain. Default fork = single Core; add
siblings via `bin/init-multi-core.sh`.

## 👋 New here? You probably want a single Core.

**Most people only need one Core**, and it gives you everything — memory, brain,
learned layer, self-governance. Start here:

- **Setup guide** — [`docs/SETUP.md`](docs/SETUP.md): fork → four commands → live Core.
- **Visual map of the whole system** — open [`docs/architecture/core-system-architecture.html`](docs/architecture/core-system-architecture.html)
  in any browser. Hit **🧭 Start here** for the mental model and **📖 Glossary** for the terms,
  then click any subsystem (brain, memory, learned layer, SI, rot, the session loop) to follow it
  down to the hooks, the embedding pipeline, and the exact files.

**The rest of this document is the *optional* multi-Core setup** — only if you want
several sibling Cores (e.g. one for work, one for school) sharing one brain.

## Pattern

```
~/AI Projects/
├── core/              The fork from nicknur7/core-agent (a fully-functional single Core)
├── core-business/     OPTIONAL sibling Core specialized for business work
├── core-school/       OPTIONAL sibling Core specialized for coursework
└── core-brain/        Shared recall vault (one per machine, partitioned by org_id)
```

Each Core:
- Has its own `.claude/`, `scheduling/`, `bin/`, `memory/`, `sessions/`, `tasks/`
- Sets `org_id` in `.claude/identity.json` (must be unique per Core)
- Connects to the shared `corebrain` Postgres DB filtered by `org_id`
- Optionally exposes a `peer-mcp-server.py` for read-only cross-Core state access

## Sync model

The first Core you fork from `nicknur7/core-agent` IS the source of baseline edits.
When you edit shared code in it (anything under `.claude/hooks`, `.claude/rules`,
`.claude/agents`, `scheduling/`, `bin/`, plus shared slash commands listed in
`bin/sync-manifest.json`), pushing back to baseline updates the template for
all future sibling Cores.

- **Pull**: `bin/sync-from-baseline.sh` — clones latest baseline, rsyncs shared
  paths into current Core, never touches `per_core_keep` paths.
- **Push**: `bin/sync-to-baseline.sh` — packages shared subset, commits, pushes.
  Hardened: no `--delete`, sentinel-file check on source, per_core_keep
  excludes, 50-file-change abort sanity limit.
- **Slash**: `/sync` for manual pull+push, `/sync pull`, `/sync push`, `/sync check`.

## Spawning a sibling Core

```bash
bash bin/init-multi-core.sh business 2   # spawns ~/AI Projects/core-business/ with org_id=2
bash bin/init-multi-core.sh school   3   # spawns ~/AI Projects/core-school/ with org_id=3
```

The script:
1. Clones `nicknur7/core-agent` into a new sibling dir
2. Updates `.claude/identity.json` with the chosen domain + org_id
3. Customizes `.mcp.json` to point at the new instance (peer slots
   pre-configured for life ↔ siblings)
4. Strips template-scaffold files unsuited for a specialized Core
5. Seeds `tenants` row in `corebrain` Postgres if reachable

After spawn, populate `CLAUDE.md` + `README.md` to define the Core's voice
+ scope. The base template at fork time is the single-Core ("life") flavor.

## Per-Core configuration

`per_core_keep` paths in `bin/sync-manifest.json` are NEVER touched by sync:
- `.claude/{identity.json,settings.json,settings.local.json}`
- `.mcp.json`
- `CLAUDE.md`, `CLAUDE.local.md` (optional, gitignored personal overrides)
- `memory/**`, `sessions/**`, `tasks/**`, `agents/**`, `secrets/**`
- `scheduling/brain-pg/eval-set*.json`, `path-rewrites.json`,
  `compile-truth-work/**`

`per_core_extras` lists Core-local slash commands the manifest knows about,
so the drift-check doesn't flag them.

## Conflict policy

When the same shared file is edited in two Cores: **baseline wins**. To
prevent drift, edit shared code in your designated source Core (typically
`life` or whichever you forked first), push to baseline, then open other
Cores so they pull the latest. This is the same pattern as `git pull` in
any multi-clone workflow.
