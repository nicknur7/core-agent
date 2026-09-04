# Codex (GPT-5.6) routing — ALL Cores

**Scope: every Core** (2026-07-24). Shared file — one source of truth; a Core may ADD local posture
in its own non-synced `rules-<core>/` overlay. **Inert** wherever the `codex` CLI is absent, so it
ships harmlessly to every Core and to forks. Verify with `which codex`.

## Task → model rows

Grunt execution (version bumps, scaffolding, changelog, mechanical passes) → **Codex Terra**, or
**Luna** for trivial. Adversarial review before a blast-radius change → **Codex Sol** or **Fable**.
Full table with pricing and invocation paths: the **`codex-routing-detail`** skill.

Route to Codex **by default, without waiting to be told.** Gate review by blast radius, not diff
size: reversible = skip; migrations, baseline pushes, irreversible = review.

## Hard fence — Codex proposes, Claude + Sentinel dispose

Codex runs its own agent loop OUTSIDE every Core hook, Sentinel and privacy gate once launched, and
GPT-5.6's system card notes it is *more* prone to unrequested actions. So: **read-only** for review,
**workspace-write** at most, **never** `danger-full-access`, and **never** an outward action (push /
send / curl / protected read) — output routes back through Claude and the existing gates. Never
enable the plugin's Stop-hook review gate. Treat its output as **leads to verify**, not gospel.

Structurally enforced by the shared `pretooluse-guard.sh`, fleet-wide: it hard-blocks the bypass
flags and configs at exit 2 — the same tier as force-push-to-main, where no Sentinel token can
approve.

**Keep inputs scoped** on Cores holding sensitive data (finance = brokerage, business = employer
material). Codex is a SECOND external model reading whatever it is handed — pass explicit diffs and
file lists, not the whole repo. Minimize-the-slice, per the Privacy Principle.

Detail — the task→model table with pricing, the exact flags the guard blocks and why the launch
sandbox is the only thing that can stop a run, fork-safety, and the full privacy rationale — is in
the **`codex-routing-detail`** skill.
