---
description: Force a full heavy graph rebuild (auto-pipeline + lint + tracker + embed). ~90s.
---

Run the heavy mode of the brain-update chain on demand. Same work the nightly
LaunchAgent performs — useful when:

- Just did a big session and want the graph updated before nightly fires
- Suspect the graph is stale (broken viz, missing nodes, etc.)
- Deleted hubs / merged hubs / changed denylist and want a fresh build NOW

What it runs:
1. `$CORE_BRAIN/_build/update-brain.sh` — pull session JSONLs into the vault
2. `scheduling/brain-lint/lint-pass.sh` — flag gaps/orphans
3. `scheduling/graphify-brain/build-tracker.py` — extraction tracker refresh
4. `scheduling/graphify-brain/auto-pipeline.sh` — merge → name-communities →
   make-2d/3d → reconcile-isolated
5. `scheduling/brain-pg/embed.py --incremental` — embed any new/changed files
   into Postgres for hybrid recall

Lock: shares the brain lock with the Stop-hook's fast path. If a close is in
flight on another Core, waits for it.

Execute:
```
bash $CLAUDE_PROJECT_DIR/.claude/hooks/run-brain-update.sh heavy
```

Report the result. If exit code is non-zero, surface the tail of
`/tmp/brain-stop-hook.log` for diagnosis.
