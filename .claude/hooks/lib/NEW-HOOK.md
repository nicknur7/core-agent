# Adding a new Core hook — the trackable checklist

A hook isn't "done" when the script works — it's done when it's **visible in System
Health** (`/system`). The whole point of the 2026-06-22 tracking work is that no hook
is a black box. Follow these 4 steps and the new hook shows up with live fire counts +
a disposition the day it ships.

## 1. Write the script
- Copy `lib/_template.py` → `.claude/hooks/<name>.py` (base name = the filename).
- Fill the header (WHY / CREATED / EVENT), set `HOOK` and `EVENT`, write the detect logic.
- Keep the contract: **fail-open** (errors → exit 0), **kill-switch** (`LEARNED_LAYER=0`),
  and **call `hooklog.log()` on every fire** (verdict = `pass` | `block` | `inject`).
- Exit 0 = allow; exit 2 = block (PreToolUse + Stop only) with a stderr reason.

## 2. Register it (settings.json)
- Add the command under the right event in `.claude/settings.json` `hooks`.
- Convention: a thin `.sh` wrapper around the `.py` (matches the existing hooks).
- This is a **guardrail edit** → goes through a tested apply-script the operator runs + `/sync push`.

## 3. Track it (hook-dispositions.json)
- Add an entry to `.claude/state/hook-dispositions.json` `hooks`:
  `{ event, disposition: "keep", priority, rationale, action, audit_refs: [], origin: { created, why } }`.
- Without this, System Health shows the hook as **untracked** (the prompt to add it).
- The `origin.why` is mandatory — every hook records the incident that justified it.

## 4. Test it
- Unit: add a case to `.claude/hooks/tests/` (pattern: `test-<name>.sh`) and run the suite.
- Live trace: fire the trigger once, then
  `grep "hook=<name>" .claude/state/hook-events.log` should show the event.
- Open `/system` — the hook now renders with its disposition, why, and live count.

## Telemetry schema (don't drift from it)
`hooklog.log()` and the JSONL backfill both emit:
```
{iso_ts} | hook={name} | event={Event} | verdict={pass|block|inject} | session={id} | excerpt={trigger}
```
`core-ux/src/lib/readers/health.ts` parses exactly this. New fields → update the reader too.

## Lifecycle after shipping
- **Backfill** can only see blocks + injects (passes leave no JSONL trace); once `hooklog`
  is live, passes accrue too — that's how "silent (never fired)" becomes provable.
- Review disposition each audit. A hook with 0 fires over many sessions is a
  **remove/merge candidate** — but check its `origin.why` first (a conservative safety
  net is rarely-triggered by design, not dead). Never cut a hook without knowing its why.
