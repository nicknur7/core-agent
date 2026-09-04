# Setting up a Core

This is the full setup path for a fresh Core — forked or cloned from
`nicknur7/core-agent`. Start with the visual map, then run four commands.

> **See it first:** open [`docs/architecture/core-system-architecture.html`](architecture/core-system-architecture.html)
> in any browser. Hit **🧭 Start here** for the mental model, then **📖 Glossary**,
> then click any subsystem to follow it down to the hooks, the brain pipeline,
> the learned layer, and the exact files. The map is the index; this guide and the
> code are the source of truth.

---

## What a Core is

A self-hosted AI assistant built on **Claude Code**. It is not a server or a
hosted product — it is a folder of markdown files, small hook scripts, and a
Postgres database that give a general AI agent four things it lacks out of the box:

- **Memory** — version-controlled files it remembers you in.
- **Brain** — a searchable knowledge graph of every past session.
- **Learned layer** — turns your corrections into enforced behavioral rules.
- **Self-governance** — hooks that catch the agent's own failure modes (rot, unverified claims, ignored stop signals).

Each part, and how they fire during a normal session, is in the architecture map.

> **You do not need multi-Core.** A single Core gives you everything in this
> guide — memory, brain, learned layer, and self-governance. Running several
> sibling Cores (one for work, one for school, …) is an *optional* add-on;
> ignore it unless you want it (see [`MULTI-CORE.md`](../MULTI-CORE.md)).

---

## Prerequisites

- macOS or Linux, `bash`, `python3`, `git`, `jq`
- [Claude Code](https://claude.com/claude-code) installed and authenticated
- Postgres (the brain index lives in a DB named `corebrain`)
- A [Voyage AI](https://www.voyageai.com/) API key for embeddings — the brain's recall and the
  close-time hub refresh call `embed.py`, which reads `VOYAGE_API_KEY` from `~/.claude/secrets.env`
  (one `export VOYAGE_API_KEY=...` line; `chmod 600` it). Without it, those steps print a named
  error and skip — the rest of the close still runs.

---

## Install (4 steps)

Run these from the Core's root directory.

```bash
# 1. System + Python dependencies (Postgres client, psycopg, voyage, flashrank, …)
bash bin/install-deps.sh

# 2. Stand up the brain: BOTH halves — the `corebrain` Postgres DB (schema, migrations,
#    roles, tenant row) AND the markdown vault at $CORE_BRAIN with its _build pipeline.
bash bin/setup-brain.sh

# 3. Turn on the Learned Workflow Layer (schema + register the 4 hooks + state)
bash bin/install-learned-layer.sh

# 4. Pull the latest shared code from the baseline (nicknur7/core-agent)
#    NOTE: this step also REGISTERS THE FULL HOOK SET. A fresh clone ships with
#    11 of ~44 hook registrations; step 4 installs the rest via reconcile-hooks.
bash bin/sync-from-baseline.sh

# 5. VERIFY the hook set actually landed. Passing output is exactly:
#      ✓ in sync — no drift        (exit 0)
#    Running step 3 by hand before step 4 (as above) commonly leaves a couple of
#    EXTRA entries alongside 0 MISSING — that still exits 1, because "0 missing"
#    alone is NOT the pass condition; "no drift" (0 missing AND 0 extra) is.
python3 bin/reconcile-hooks.py --core "$PWD" --check
#    If it lists MISSING and/or EXTRA hooks (any non-zero exit), apply — this adds
#    what's missing AND removes what's extra, then re-verifies itself:
#      python3 bin/reconcile-hooks.py --core "$PWD" --apply
```

> **Why step 5 is not optional.** Hook registration is the difference between a Core with
> two-thirds of its enforcement layer and one without. It has failed silently before: a fresh
> Core once installed 12 of 37 hooks and had no self-improvement spine at all, with nothing
> reported, because `reconcile-hooks.py` exited non-zero on a missing `hook_profile.role` and
> the sync path swallowed the error. `--check` is how you find out instead of assuming.

Before the first session, point this clone at YOUR remote — the close flow auto-commits and pushes
on session end, and it refuses to push to the template repo it was cloned from:

```bash
git remote set-url origin git@github.com:<you>/<your-core>.git   # private repo, please
```

That's it. Open a Claude Code session in this directory and the system is live.

---

## What each step gives you

| Step | Script | Result |
|------|--------|--------|
| 1 | `install-deps.sh` | Python venv + Postgres client + embedding/rerank deps. |
| 2 | `setup-brain.sh` | **Both halves of the brain.** The `corebrain` Postgres DB (schema, migrations, roles, tenant row) *and* the markdown vault at `$CORE_BRAIN` — default `../core-brain` — seeded from `template/brain/`. Recall works. Idempotent: re-running never overwrites vault files you have edited. |
| 3 | `install-learned-layer.sh` | Learned-layer DB schema + the 4 hooks registered + a generalized starter contract set. **All blockers AND the classifier go live immediately** (the starter is generic; it gets replaced by your own as corrections accumulate). Idempotent — safe to re-run. **Existing Cores run this automatically on every `sync-from-baseline.sh` pull** — only a brand-new clone needs to run it by hand. |
| 4 | `sync-from-baseline.sh` | Latest shared hooks/rules/agents/pipeline from `nicknur7/core-agent`. Run anytime to update. |

---

## How the Learned Workflow Layer behaves on a brand-new Core

This is the part that makes the architecture map *true* from day one:

- **Blockers are live immediately.** `learned-validator`, `learned-recallguard`,
  and `learned-stopguard` are regex-only — they need no history. They start
  catching the universal failures (ignoring a stop signal, paraphrasing instead of
  recalling, editing before acknowledging) the moment you finish step 3.
- **The classifier is live from install, via a generalized starter set.** The
  installer seeds six universal contracts — stop-and-plan, verify-don't-claim,
  recall-first, plan-not-execute, model-routing, frustration-deescalate — so the
  classifier steers responses on day one. As you correct the assistant, the corpus
  grows at every session close; re-synthesize to replace the generic starter with
  contracts tailored to *you* (`scheduling/claude-si/learned-corpus-miner.py` +
  `learned-resynth.py`). It always fails open — a missing snapshot is never an error.
- **Kill-switch:** `export LEARNED_LAYER=0` disables the whole layer instantly.
  Everything fails open, so an error never blocks you.

Full internals: open the **Learned Workflow Layer** box in the architecture map.

---

## Updating later

This Core pulls shared improvements from the baseline. Run `bash bin/sync-from-baseline.sh`
(or it runs automatically at session start). The baseline is **additive and
pull-only** for your Core — your personal `memory/`, `tasks/`, and settings are
never overwritten.

---

## Where to look when something breaks

- **The brain vault is private — give it a private remote.** `setup-brain.sh` runs `git init` in
  `$CORE_BRAIN` but adds no remote, deliberately. The vault holds every session transcript and every
  person you have discussed. If you add one, it must be a **private** repo; `_build/update-brain.sh`
  carries a destination allowlist that refuses to push anywhere whose final path segment is not
  `core-brain`, precisely because that push is an unattended nightly no human reviews.

- **Brain pipeline silently does nothing** → check `$CORE_BRAIN/_build/update-brain.sh` exists. Before 2026-08-29 nothing in this install created the vault, so the heavy pass failed into a
  log on every close. Re-run `bin/setup-brain.sh`.

- **Recall returns nothing** → is Postgres up? `psql -d corebrain -c 'SELECT 1'`. Re-run `bin/setup-brain.sh`.
- **A hook is blocking you wrongly** → read the block message; it tells you what to fix. To disable the learned layer: `export LEARNED_LAYER=0`.
- **Brain health** → `python3 scheduling/brain-pg/brain-health.py` prints every invariant.
- **What's installed** → `grep -o 'learned-[a-z]*\.sh' .claude/settings.json | sort -u`.
