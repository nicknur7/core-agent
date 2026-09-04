---
description: Manually sync shared code between current Core and the nicknur7/core-agent baseline. Default = pull + push-if-changed. Subcommands "pull" / "push" / "check".
argument-hint: "[pull|push|check]"
---

# /sync

Manual fire of the multi-Core baseline sync mechanism. Pulls latest shared code
from `nicknur7/core-agent` into the current Core, and pushes back any local shared
edits. **Both directions are gated by Sentinel-code (Sonnet) review** —
PreToolUse hook `pretooluse-guard.sh` blocks sync-{from,to}-baseline.sh
invocations until Sentinel-code returns APPROVE.

Sync execution by core role (updated 2026-06-04 — auto-pull on SessionStart wired):
- **Pull-only cores** (business / school / finance + external forks):
  a **SessionStart hook** runs `sync-from-baseline.sh --quiet` on every open, so
  they auto-pull shared code from baseline with no manual `/sync`. Clone failure
  fails-safe (logs + skips). `session-start-check.sh` then surfaces status.
  (SessionStart hook execution does NOT pass through the PreToolUse Sentinel gate
  — that gate fires only on Claude's Bash *tool* invocations of the sync scripts.
  Auto-pull is read-shaped + baseline-wins, so it's allowed unattended.)
- **Baseline-writer core** (life — see manifest `.baseline_writer`): SessionStart
  does **NOT** auto-pull. A writer auto-pull would rsync the older baseline over
  its own unpushed shared edits, so the writer-guard in `sync-from-baseline.sh`
  skips `--quiet` mode on the writer. Life syncs manually via `/sync`.
- **Stop hook** (`stop-hook.sh`): writes `.pending-push-marker` listing any
  committed files in the shared subset. Does NOT push to baseline; the writer
  runs `/sync push` (Sentinel-code-gated) to publish.

This design closes the Phase 7 incident vector — script-internal `git push`
from a subprocess bypassed PreToolUse. By keeping the script invocation
at Claude's Bash tool boundary, Sentinel-code reviews the diff before the
script ever runs.

Use `/sync` to force a sync now (skipping the SessionStart notification step),
or `/sync check` to preview what would change in either direction.

## Subcommands

- `/sync` (no args) → pull first, then push-if-changed
- `/sync pull` → only pull from baseline
- `/sync push` → only push to baseline (Sentinel review fires)
- `/sync check` → dry-run; report what would change in pull direction. No writes.

## What syncs

Defined in `bin/sync-manifest.json`, which is the authority. This list is checked against it by
`bin/tests/test_documented_checks_are_run.py`, so it cannot silently drift — **shared** = same
across all Cores:

- `.claude/{hooks,rules,agents}/`
- `.claude/commands/{close-core,handoff,health,recall-similar,refresh-truth,ship,sync,rebuild-graph,core-si,deep-plan,retire-legacy}.md`
- `scheduling/{brain-pg,graphify-brain,brain-lint,claude-si,core-si,system-health}/`
- `bin/` — every script AND the whole test suite. This is why a pull-only Core cannot add a test:
  `bin/tests/` is shared, so `shared-write-guard` refuses it and the writer installs it instead.
- `eval/`, `docs/`, `template/`
- `.claude/skills/{claude-brain,codex-routing-detail}/SKILL.md`, `.claude/CLAUDE.base.md`
- repo root: `Makefile`, `README.md`, `LICENSE`, `MULTI-CORE.md`, `.mcp.json.template`, `pyproject.toml`

To print the authority instead of trusting the copy:

```bash
jq -r '"DIRS:", (.shared.dirs[]|"  "+.), "FILES:", (.shared.files[]|"  "+.)' bin/sync-manifest.json
```

> **This list has drifted twice and I nearly deleted it a third time.** 2026-08-12 it omitted
> `claude-si`, `core-si`, `system-health`, `eval`, `docs`, `template`, so a reader concluded the SI
> loop was per-Core and that a fix to `measure-contract-fitness.py` had to be hand-applied on every
> seat. That correction added a guard — and then left `bin/` stranded as a bullet BELOW the
> correction note, where I failed to see it while auditing this section specifically for `bin/`.
>
> On 2026-08-13 I replaced the whole enumeration with the `jq` command above, reasoning that a
> hand-copy of a machine-readable list will always drift. **The test caught it as a FAIL, and the
> test was right:** the guard has existed since the 08-12 drift, so the list was already
> unfalsifiable-by-hand, and removing it would have deleted the only checkable claim the doc makes
> in exchange for an instruction nobody is obliged to run. The enumeration is restored, the three
> commands and nine files it never listed are added, and the guard now covers files too.

**Per-Core kept** (NEVER touched by sync): identity.json, settings.json,
.mcp.json, CLAUDE.md, memory/, sessions/, tasks/, agents/, secrets/, plus
brain-pg eval-set + path-rewrites + compile-truth-work.

**Per-Core extras** (this Core may have local-only command files; manifest
declares them so the sync drift-check doesn't flag them).

## Behavior

Default (`/sync` no args):
```bash
bash $CLAUDE_PROJECT_DIR/bin/sync-from-baseline.sh
bash $CLAUDE_PROJECT_DIR/bin/sync-to-baseline.sh --only-if-changed
```

`/sync pull`:
```bash
bash $CLAUDE_PROJECT_DIR/bin/sync-from-baseline.sh
```

`/sync push`:
```bash
bash $CLAUDE_PROJECT_DIR/bin/sync-to-baseline.sh
```

`/sync check`:
```bash
bash $CLAUDE_PROJECT_DIR/bin/sync-from-baseline.sh --check
```

## Output format

Tight. Report what changed (file count + first 5 paths), or "no changes" if
the sync was a no-op. On Sentinel BLOCK during push, surface the verdict +
explain (usually: per-Core data accidentally inside a shared path).

## Rules

- Conflict policy: **baseline wins** for shared files. To avoid losing local
  edits, edit shared code in life-Core first + push, THEN open business/school.
- Both `/sync pull` and `/sync push` trigger Sentinel-code review (Sonnet) via
  PreToolUse. The agent enforces 8 rules (trust-root integrity, no credentials,
  no destructive ops, no new outbound calls, no obfuscation, manifest integrity,
  size sanity, no per-Core data leak). Don't auto-approve — any non-trivial
  change to baseline deserves eyes.
- This command does NOT touch `memory/`, `sessions/`, `tasks/`, etc.
- **Trust-root exclusion (UPDATED 2026-07-10).** Only the Sentinel *agent logic*
  stays in `per_core_keep`: `.claude/agents/sentinel/**`,
  `.claude/agents/sentinel-code/**` (plus the flat `.claude/agents/sentinel.md`
  and `sentinel-code.md`). The guard *scripts* `.claude/hooks/pretooluse-guard.sh`
  and `.claude/hooks/sentinel-approve.sh` are **now SHARED and DO sync** (Nick's
  call, decisions-log 2026-07-10 - so life's hardened guard propagates to peers).
  They are no longer per_core_keep-excluded; instead a guard-script change in a
  pull is reviewed by Sentinel-code Rule 1 (content review -> ASK-with-note ->
  the operator's explicit confirm, never auto-APPROVE). **Seeing those two scripts in a
  pull diff is EXPECTED - review the content and confirm; it is not an anomaly.**
  Any doc or brain node still calling them per_core_keep-excluded is pre-2026-07-10
  and stale; this is the current record.
