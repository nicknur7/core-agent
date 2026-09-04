---
description: The ONE self-improvement gate. Runs all domain detectors (behavior, recall, system, liveness) into a single ranked decision table; one approval pass; applies via existing engines. Unifies /claude-si + /system-si.
---

# /core-si — the single system-improvement gate

One place for everything that needs a decision or fix: recurring corrections/frustration, recall misses, system drift/staleness, and dead/dark producers. Detect → rank → propose → **one** approval → apply → measure. Replaces running `/claude-si` and `/system-si` separately (they remain as internal engines, not deleted).

The detection logic lives in ONE place — `scheduling/core-si/detect.sh` — reused by this command, the SessionStart first-reply lead, and the statusline badge. Do not reimplement detectors here.

## Step 1 — Detect + render (silent setup, then show)

```bash
CORE_INSTANCE="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}" \
CORE_ORG_ID="${CORE_ORG_ID:-1}" \
bash "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}/scheduling/core-si/detect.sh"
```

That prints the ranked markdown table + the reply line. If it prints `core-si: 0 items — clean.`, say so and stop.

## Step 1b — auto-resolve TRUSTED fixes first (ADAS, 2026-06-07)

A fix EARNS autonomy by being approved **2× running with no reject** (the trusted-fix set,
`scheduling/core-si/si-fix-admission.py`; K lowered 5→2 on 2026-06-27 — K=5 was untested theory
that nothing ever crossed). Before showing the table, check each item:

```bash
python3 "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}/scheduling/core-si/si-fix-admission.py" --check <KEY> "<proposed fix>"
# current streak (read-only, for the x/2 counter):
python3 "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}/scheduling/core-si/si-fix-admission.py" --streak <KEY> "<proposed fix>"
```

If `trusted=True` **AND** the `KEY` is in the **AUTO_SAFE allowlist** (`scheduling/core-si/auto-safe.txt`)
**AND** it has a registered applier in `bin/core-si-close.py`, apply that item via its Step-3 action
*silently*, drop it from the table, and report it under "auto-resolved (trusted)".
Everything else surfaces for approval as normal. This is the payoff — fixes you've blessed 2× stop asking.

**AUTO_SAFE allowlist** lives in `scheduling/core-si/auto-safe.txt` (one KEY per line; life-local +
reversible ONLY — extend deliberately, one key at a time). Currently admitted: `recall-eval`.
`sys-docpath` is **trusted but NOT auto-safe** — `lint-doc-paths` has no deterministic auto-fix, so
deciding which broken refs are archival stays judgment → notify-only.

**Autonomy now runs AT CLOSE, not only here.** `bin/core-si-close.py` (called by the close hook,
`session-lifecycle.sh`) auto-applies trusted+AUTO_SAFE+has-applier fixes on every session close,
writes `.claude/state/core-si-inbox.json` (the visibility surface + future-app bridge), and fires
local notifications (`bin/core-notify.sh`) for critical-needs-you + the FIRST time a fix auto-fires.
This command remains the interactive approval surface; the close pass is the unattended one.

**NEVER auto-apply, even if trusted:** anything outward (git push, baseline sync, the `sys-marker`
*push* branch), anything needing judgment (`sys-memstale`, `sys-brainlint`), or any trust-root /
behavioral-hook change. Trust earns past the **surfacing** gate, never past the **outward/destructive** gate.
Then show the (possibly reduced) table verbatim.

## Step 2 — Take ONE reply

Plain-text reply (NOT AskUserQuestion): `approve all` · `approve 1,3` · `edit N: <text>` · `reject N reason:<why>` · `details N` · `skip`.

For `details N`: re-read the item's evidence (query `pattern_observations` for the label, or open the file) and show it; do not apply yet.

## Step 3 — Apply approved items (route by the item's KEY)

Each item carries a `KEY` (see `--tsv` output). Route each approved item:

| KEY prefix | Apply action |
|---|---|
| `learned-resynth` | Corpus grew past threshold → `learned-resynth.py --prepare` → regenerate guidance (subagent) → `--apply`. (Behavior SI is now the learned-workflow layer; the old `behav-promote/escalate/retire` rule loop is **retired** — corrections become typed contracts, not promoted rules.) |
| `sys-docpath` | Apply the archival exemption (add `Status:EXTRACTED\|archived\|HISTORICAL` skip to `lint-doc-paths`), and/or fix the one real broken ref. |
| `sys-marker` | Verify the listed files against baseline (`git`/`sync-to-baseline.sh --check`); if already pushed, clear the marker; if not, surface the push (Sentinel-gated). |
| `live-detector` | Re-run `detect-patterns.py --since 14` (cwd-safe) and confirm `detector_runs` advances. |
| `recall-*` (S2) | `verification_trigger_tune` on `brain-recall-trigger.py` via `apply-promotion.py`, or schedule/run `eval.py`. |

**Outward actions** (git push, baseline sync) still go through Sentinel — applying a `sys-marker` push is not exempt.

**Record every decision** (this is what fills the K=5 counter so a deterministic fix earns autonomy
over time — without it the trusted set stays empty forever):

```bash
# approve → +1 toward the streak:
python3 "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}/scheduling/core-si/si-fix-admission.py" --record <KEY> "<proposed fix>"
# reject → resets the streak to 0:
python3 "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}/scheduling/core-si/si-fix-admission.py" --record <KEY> "<proposed fix>" --reject
```

When `--record` prints `-> ADMITTED to trusted-fix set`, tell the operator: that fix will auto-resolve from
now on (if it's in AUTO_SAFE). One reject any time resets it — the gate re-engages.

## Step 4 — Confirm + report

Re-run `detect.sh` after applying; confirm the resolved items dropped out. Report: `✅ N applied · M deferred · K rejected. Auto-commit at close lands changes.` (The old promotions ledger `memory/claude-si-ledger.md` (removed cc3d63b) and `apply-promotion.py` are retired — decision recording happens via `si-fix-admission.py --record` above.)  <!-- privacy-ok: generic status report vocabulary -->

## What you do NOT do

- **Never auto-apply `behav-escalate` (hook generation) or any trust-root change** — design + explicit operator ok, shadow-mode first.
- **Do not delete `/claude-si` or `/system-si`** — they're the engines this orchestrates.
- **Do not reimplement detectors here** — they live in `scheduling/core-si/detect.sh`. Add new detectors there.
- **Do not silently truncate** — show every item `detect.sh` returns (no per-invocation cap).

## Reference
- Engine: `scheduling/core-si/detect.sh` · Spec: `tasks/specs/spec-core-si-2026-05-26.md` (instance-only — `tasks/**` is per_core_keep, so this is absent on a fresh clone)
- Appliers: `bin/lint-doc-paths.py`, `bin/sync-to-baseline.sh`, `scheduling/claude-si/learned-resynth.py`
- Trusted-fix admission (ADAS): `scheduling/core-si/si-fix-admission.py` · Bounded lesson retirement: `scheduling/core-si/lessons-evict.py`
- (The old `apply-promotion.py` / `measure-rule-fitness.py` are archived under `scheduling/archive/claude-si-behavior-loop/` with the retired rule loop.)
