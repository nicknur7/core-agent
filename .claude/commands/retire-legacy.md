---
description: Sweep the legacy parts the memory-brain redesign superseded — only once the new system is proven
argument-hint: ""
---

# Retire Legacy (memory-brain-SI unified redesign, step ⑤)

Archive the superseded legacy parts (freshness-gate etc.) that the new ledger + brain_status + assertion
recall replaced. **Gated on proof**: refuses unless the new system has sustained the readiness threshold
of clean close cycles (tracked automatically by `retire-legacy.py --tick` at every close). Reversible.

## Steps

1. **Check readiness + execute the archive.** Run:
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" python3 bin/retire-legacy.py --execute
   ```
   - If it prints "NOT ready (N/10)", STOP — the new system hasn't proven out yet. Tell the operator the count.
   - On success it `git mv`s the superseded files into `scheduling/_archive/legacy-retired-<date>/`
     (reversible) and writes a README listing what was archived + what still needs a manual in-code edit.

2. **Apply the caller edits** the archive README flags (the archived files had references):
   - `.claude/commands/close-core.md` step 8 (freshness-gate.py) → replace with `brain_status.py`.
   - `bin/sync-manifest.json` `per_core_extras.life` → remove `scheduling/_archive/legacy-retired-2026-07-23/claude-si.md` (instance-only — archive dirs are not shipped).
   - The `flag`-action items (e.g. basename-checkpoint logic inside `extract-pending.sh`) — surface to
     the operator for a reviewed in-code edit; do NOT auto-rewrite live extraction code unattended.

3. **Verify** nothing that still runs references an archived file: `grep -rl "freshness-gate\|remap-checkpoints\|claude-si.md"` across `.claude/ scheduling/ bin/` (excluding the archive dir + this command). Fix any live reference.

4. **Commit** the sweep. Do NOT drop any DB tables — legacy DB-data deletion is a SEPARATE, explicitly-
   approved destructive step (needs a fresh backup + the operator's yes), never part of this command.

5. **Sync** the shared-code changes to baseline (`/sync push`) so peers retire on their next pull.
