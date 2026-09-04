# Pull notes — what each baseline change MEANS, and what a pulling Core must do

The operator's directive that produced this file, paraphrased: a Core should never pull without
knowing what the change means and what to do about it, so pulls run unattended — the operator is
involved only when a Sentinel-gated file changes.

A pull already applies files, runs `reconcile-hooks --apply`, and runs any new brain-pg migration —
all idempotent, all unattended. What it could not do was tell the pulling Core **what any of it
meant**. It moved bytes and reconciled registrations with no notion of intent, so a Core could pull
a change that requires a one-time adoption step, apply it perfectly, and sit in a broken-looking
state that nobody had told it to expect.

This file is that missing channel. The writer Core appends an entry per push; every puller reads
the entries it has not seen yet (`bin/pull-notes.py`), performs the declared actions, and surfaces
the rest at SessionStart.

## Entry format

    ## <YYYY-MM-DD> · <baseline sha>
    **What changed:** one paragraph, plain.
    **Actions:** comma-separated names from the CLOSED VOCABULARY below, or `none`
    **Needs Operator:** anything requiring the operator's own hands, or `none`
    **Heads-up:** state a puller should expect but not act on, or `none`

## The closed action vocabulary

An action is a NAME the puller maps to its own local implementation. A note can never supply a
command — this file arrives over the network, and the same rule that governs `hook-registry.json`
governs it: a data file from the network gets no benefit of the doubt. An unrecognised name is
reported, never executed.

| name | what the puller runs locally |
|---|---|
| `run-migrations` | `bin/run-migrations.sh` — idempotent, tracked in `schema_migrations` |
| `reconcile-hooks` | `bin/reconcile-hooks.py --apply` |
| `adopt-si-projection` | `bin/si-adopt-projection.py` — one-time, adopts existing artifacts into the spine |
| `extract-asks` | surfaces the ask-extraction dispatch; needs a session to run the subagent |
| `retire-stale-entities` | surfaces this Core's own stale trust-root entities (RLS means only it can) |
| `none` | nothing to do |

## Why `Needs Operator` is a separate field

Everything in the action vocabulary is local, reversible and idempotent, so it runs unattended —
that is the whole point. **Trust-root changes are the one exception**, and they are not an exception
because they are risky to apply; they are an exception because approving one is a decision that
must be un-forgeable. `sentinel-approve.sh` states it directly: *"THERE IS NO SELF-SERVICE PATH FOR
A TRUST-ROOT CHANGE. THIS IS DELIBERATE."* No agent message counts as the operator's consent —
including the pulling Core's own assertion that they said yes.

So a `Needs Operator` line never blocks the pull. The files arrive, the safe actions run, and the line
is surfaced for them. It is information, not a gate.

---

## Entries

Entries are appended below by the writer Core, one per push, newest last. **A fresh fork ships with
no entries** — this file is the protocol, and the entries are the history of a specific fleet's
pushes, which a new fork does not have. Your first pull-note will be the first push you make from
your own writer.

A fork that copied this file with the original fleet's entries still attached (it shipped that way
once) saw "5 unread baseline pull notes" on its first session, about pushes to a baseline it had
never pulled from. That is the history being mistaken for the protocol.
