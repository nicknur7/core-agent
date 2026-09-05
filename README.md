# Core

**A persistent, self-hosted agent for Claude Code.** It remembers your work across sessions, answers from a knowledge
graph of everything you have done with it, and compiles your repeated corrections into hooks that fire whether or not
the model remembers.

Fork it, run four commands, and you have:

- **Memory that survives `/clear`** — plain markdown you can read and edit, written at every session close, plus a
  Postgres graph of the entities, decisions and relationships pulled out of the transcript.
- **Recall with four legs** — pgvector, Postgres full-text, a graph walk over relationships, and edge-relation
  vectors, fused by reciprocal rank fusion and reranked.
- **47 hook registrations across 17 Claude Code lifecycle events** — an adversarial review subagent in front of every
  outward action, plus gates that refuse a tool call instead of asking the model nicely.
- **A loop that turns your corrections into rules** — mined, distilled, test-gated against your own past prompts,
  installed, then measured and retired when the evidence stops supporting them.

Everything runs on your machine. Nothing phones home.

## What actually makes this different

Plenty of agent setups say they learn. **This one grades its own rules, in code, and publishes the bad news.** Six
instruments, none of them a metaphor:

| Instrument | The question it answers |
|---|---|
| [`bin/grade-gate.py`](bin/grade-gate.py) | *How often does this rule fire on my real past prompts?* Replays a hook's `detect(text)` over the transcript corpus. Over-broad is anything above 3% — the same bar a machine-generated rule has to clear. |
| [`bin/grade-intent.py`](bin/grade-intent.py) | *Is it still catching what it was built to catch?* Replays each gate against its own founding positives and negatives. Deliberately deterministic: lexical "same-kind" scoring measured AUC 0.54 against a labelled set — a coin flip — and was deleted rather than shipped weak. |
| [`bin/steering-ledger.py`](bin/steering-ledger.py) | *Is it worth the tokens it costs?* Cost comes from the injection log and is a fact. Benefit is unobservable and is not faked; the verdict is drawn from recurrence and rarity instead — `EARNING`, `LOW-YIELD`, `EXPENSIVE`, `PRE-EMPTED`, `RARE-VALUABLE`, `COST-BLIND`. |
| [`bin/wiring-audit.py`](bin/wiring-audit.py) | *Does it actually run, or is it merely present?* Static reachability from registered hooks, LaunchAgents and imports. "A human can type it" is reported as `MANUAL`, never as wired. |
| [`bin/null-calibration.py`](bin/null-calibration.py) | *Would almost any date have shown that improvement?* A permutation test over every valid split point. On the corpus it was built for, 85.5% of the 83 available split dates produce a "decaying" verdict with no intervention at all — a raw before/after ratio proves nothing until you know its null. |
| [`bin/gate_tier_b.py`](bin/gate_tier_b.py) | *Did that prompt change actually help?* Paired live A/B trials — baseline rules vs candidate rules, identical prompts, graded by frozen predicate code the candidate cannot touch. **It has never scored a candidate**, and the file says so at length, including why a bigger budget would not fix it. |

That last row is the point: an instrument here is allowed to return bad news about the system that built it.

## See it work before you install anything

Sentinel is the review subagent standing in front of every outward action — git push, email, calendar write,
non-allowlisted fetch. It returns `APPROVE` / `ASK` / `BLOCK`, and a receipt parser turns that verdict into the
single-use token that unblocks the action. That parser is the softest spot in the whole trust model: if a report that
*refuses* can be read as an approval, the gate is decorative. It took **seven revisions**, three of which would have
minted an approval for a review that said no — because `APPROVE for bash x` (a verdict) and `APPROVE is withheld`
(not a verdict) are the same shape, and no regex separates them. Clone the repo and run the adversarial suite against
the shipped hook. No database, no API key, no install:

```bash
python3 bin/tests/test_sentinel_verdict_parser.py
```

```
sentinel verdict reader (revision 7 — bare approval, quoted verdicts disqualified)
  ...
  PASS  BLOCK      want=BLOCK      > APPROVE
  PASS  BLOCK      want=BLOCK      - APPROVE
  PASS  BLOCK      want=BLOCK      # APPROVE
  PASS  (decline)  want=(decline)  APPROVE
  PASS  (decline)  want=(decline)  Rules 1-8 pass with no findings.
  PASS  (decline)  want=(decline)  For reference, a clean report looks like:
  ...
all verdict-reader checks pass
```

Six of the 40 cases. Right column: the reviewer's report, truncated. Middle column: what the parser must read it as. A blockquoted
`> APPROVE`, a bullet, a heading, a bare `APPROVE` with no reviewed command attached, an approval quoted inside an
explanation of the format — every one is a real way a careful reviewer once minted a real token, and each is now a
test that fails if it comes back. The rule that ended it is positional, not semantic:
[`bin/verdict-contract.md`](bin/verdict-contract.md) puts the verdict on the **last non-blank line** and the reviewed
command on the **first**, and the parser reads only those two lines — position authorises, prose never does.
[`bin/ensure-verdict-contract.py`](bin/ensure-verdict-contract.py) stamps that contract onto every agent spec so
per-Core copies cannot drift from it.

## Quickstart

```bash
gh repo create my-core --template nicknur7/core-agent --private
cd my-core
claude
```

That alone gets you memory, session logging and the safety hooks. **Then run the full install** — not optional if you
want recall and the learned layer:

```bash
bash bin/install-deps.sh            # 1. Python deps (psycopg, voyage, flashrank, graphify)
bash bin/setup-brain.sh             # 2. Postgres DB + schema/migrations AND the markdown vault
bash bin/install-learned-layer.sh   # 3. Self-improvement schema, hooks and state
bash bin/sync-from-baseline.sh      # 4. Pull shared code; reconciles the hook registrations
python3 bin/reconcile-hooks.py --core "$PWD" --check   # 5. Verify — expect "in sync — no drift"
```

**Step 5 matters more than it looks.** If it lists missing hooks, re-run with `--apply`. Registration is the
difference between a Core with its enforcement layer and one without, and it has failed *silently* before: a fresh
Core once installed 12 of 37 hooks and no self-improvement spine at all, with nothing reported, because
`reconcile-hooks.py` exited non-zero on a missing `hook_profile.role` and the sync path swallowed the error.

Prerequisites: macOS or Linux, `bash`, `python3`, `git`, `jq`, Postgres, Claude Code authenticated. The vector legs
of recall want a [Voyage AI](https://voyageai.com) key (~15 minutes of one-time setup); without one,
`scheduling/brain-pg/query.py` drops those two legs automatically, keeps full-text and the graph walk, and exits 0.
That degradation is a fix, not a design — until 2026-08-31 the documented flagless invocation called `sys.exit()` if
you had no paid key. Full detail and troubleshooting: **[docs/SETUP.md](./docs/SETUP.md)**.

## First session

Open `claude` in the repo. The SessionStart hook prints a status block — clock, last close, pending items — and
Claude offers to populate `memory/about-me.md`, `memory/preferences.md` and `memory/goals.md`. Edit
**`.claude/identity.json` first:** your name, your Core's name, `hook_profile.role`. It ships with `YOUR_FIRST_NAME`
placeholders, and until you change them Core addresses you as "the operator." From then on every session close
writes what happened into the vault, and the next session opens knowing it.

## Memory that compounds

`memory/` is plain markdown, `sessions/` a dated log per day, `tasks/` research artifacts and plans — all of it in
*your* private fork. On close, Core extracts entities and relationships from the transcript into a Postgres graph.
Ask about a person, a project or a decision from three months ago and it comes back with the source file attached.

## Recall with four legs

```
your question
  ├─ pgvector          semantic similarity
  ├─ Postgres FTS      exact keywords
  ├─ graph BFS         walks relationships out from entities already scored
  └─ edge vectors      relationship-aware matching
        ↓
   reciprocal rank fusion → cross-encoder rerank → answer
```

The graph leg is the one a search box cannot replicate: it surfaces things that share **no words** with your query
but sit two hops away. Implementation and per-leg ablation flags: [`query.py`](scheduling/brain-pg/query.py).

## Hooks that enforce — and the loop that grades them

`.claude/settings.json` registers 47 hooks across 17 lifecycle events;
[`bin/hook-registry.json`](bin/hook-registry.json) carries all 59 the project has ever shipped, 12 of them explicitly
marked retired. A representative few:

- **Sentinel** ([`sentinel.md`](.claude/agents/sentinel.md), [`pretooluse-guard.sh`](.claude/hooks/pretooluse-guard.sh))
  — adversarial review before any outward action. Its approval token is written to disk, bound to the exact reviewed
  command string, and `unlink`ed the moment it is consumed, so it cannot approve a second command.
- **Trust root** — a change to Sentinel's own logic or the guard script can never be auto-approved, only confirmed by
  a human. No agent message counts as that confirmation, including the Core's own claim that you gave it.
- **Stop-signal gate** — if your last turn contained a halt, a mutating tool call is refused until the course change
  is explicitly acknowledged. And a **verification trigger** fires before any reply that makes a state claim.

The rules are not hand-written and left alone. They go round a measured loop:

```
you correct Claude                          all under scheduling/claude-si/ unless noted
  → at close, the session transcript is scanned for
    correction-shaped turns → pattern_observations ........... learned-corpus-miner.py
    (separately, Claude's OWN replies are watched for time-,
     state- and say-do violations, every turn) ............... .claude/hooks/reply-observer.py
  → (prompt, response, correction) rebuilt from the transcript  friction_miner.py
  → the recurring ask is distilled, frustration stripped out .. ask_miner.py
  → typed, and given a CONJUNCTIVE trigger: two distinctive
    words must CO-OCCUR, so a "database migration" rule
    cannot fire on "database vacation" ....................... friction_router.py
  → REPLAYED over >=40 of YOUR real past prompts, and
    REJECTED if it fires on more than 3% of them ............. friction_test_gate.py
  → installed, then fired ................................... friction_installer.py, friction_dispatch.py
  → did the guarded behaviour actually decline? if not,
    the rule is retired ...................... friction_watchdog.py, measure-contract-fitness.py,
                                               bin/steering-retire.py
  → the whole distillation runs unattended overnight ......... bin/si-drain.sh
  → the round is scored: did violations per 100 replies
    FALL, and at what token cost ............................. bin/si-objective.py
```

**The objective was rewritten because the old one was degenerate.** It read *minimise enforced blocks, subject to
shadow detections not rising* — an objective whose global optimum is doing nothing. A system that installed 1,070
artifacts and promoted exactly zero of them to enforcement was not failing against it; it was scoring perfectly. The
replacement measures the outcome — violations that actually reached you, counted on the real final text — so building
things earns nothing on its own, and any detector reporting zero must pass a liveness probe or that zero is reported
as `UNKNOWN` rather than as success.

**The overnight drain is read-only by construction, not by instruction.** `si-drain.sh` v1 asked the model in prose
not to write files; the review that blocked it was right, because with no tool flags an unattended session inherits
the repo's own permissive settings. The model now runs with Read only, is started outside the repo, and returns JSON
on stdout — *the script performs every database write itself*. It then audits its own sandbox and names what is still
reachable, calling it "defense in depth, not a sandbox."

**A brand-new Core refuses to install anything** until it has roughly 40 of your own prompts to test against
(`friction_test_gate.MIN_CORPUS`) — the safety property, not a defect. It will not learn from you before it has
watched you work.

## How it learns — the whole package, in the order it happens to you

Everything below ships in this repo and runs on its own. The only inputs are your prompts and your corrections.

**Day one, first prompt.** 30 hooks are registered across 17 lifecycle events. Two things fire before you have any
history: the **starter contracts** — seven generalised rules (verify before claiming, plan before executing,
recall before answering, stop-and-plan on a redirect, model routing, frustration de-escalation, verify a claim
adversarially) that `.claude/hooks/learned-classifier.py` injects into the prompt when it matches
(`📋 LEARNED CONTRACT matched — shape your response accordingly: DO … / DON'T …`) — and the **starter skills**,
five `.claude/skills/<name>/` directories the installer copies in, each one a skill the loop promoted on the
reference seat. `reply-observer.py` starts watching Claude's own replies for time-claims, state-claims and
promise-without-action from the first turn. Every other mechanism is a harmless no-op until it has data.

**Every session close (`/close-core`, runbook in [`.claude/commands/close-core.md`](.claude/commands/close-core.md)).**
Deterministic steps always run: session duration, git auto-commit, capabilities inventory, close receipt. The brain
steps run when Postgres and the vault exist and *skip by name* when they don't (`CONSOLIDATE: skipped (no
database)`) — a close never dies on a missing dependency. A few steps need the model, in-session: the reconciler
subagent, the narrative, and — only when there is a backlog — graph extraction, assertion extraction and ask
distillation. Hub refresh above 8 drifted hubs or about $1 of spend stops and asks you first. Then
`bin/core-si-close.py` runs the learning loop itself (next paragraph), unconditionally, at every close.

**Corrections become rules.** `learned-corpus-miner.py` scans the session transcript for correction-shaped turns
into Postgres `pattern_observations`. `ask_miner.py` distils each into a canonical ask — the one model call in the
pipeline, in-session at close (or unattended overnight by `bin/si-drain.sh`, which runs the model read-only and
performs every write itself). Once an ask has recurred **3 times across 2 sessions** and the corpus holds **40 of
your real prompts**, a fully deterministic chain — `friction_router` → `artifact_typer` → `artifact_generator` →
`friction_installer` — mints an artifact into `.claude/state/friction-artifacts/active.json`. No model writes
executable anything; artifacts are JSON-filled from `scheduling/claude-si/templates/enforcement-templates.json`. Types: an **inject
contract** (default), an **enforcement block**, a **hooked skill**, a **CLAUDE.md directive**, a **slash command**,
or a **workflow**. Caps per run: 5 contracts, 1 blocker.

**Rules fire on their own.** `.claude/hooks/friction-dispatch.py` (UserPromptSubmit, PreToolUse, Stop) evaluates every
active artifact's condition tree — prompt regex, tool name, tool mutability — and on a match injects the
artifact's message as context, or refuses the tool call. Each artifact fires at most N times per session (default
1). Blocks are born in **shadow** mode: they log what they would have refused, and only start refusing after
**5 fires across 3 sessions over 7 days** of proof (`friction_promote.py`, at close). A rule whose guarded
behaviour does not actually decline is retired on its own evidence (`friction_watchdog.py`,
`measure-contract-fitness.py`, `bin/steering-retire.py`).

**Hooked skills become real skills.** A hooked-skill artifact carries a procedure body and fires it into context when
its trigger matches. When it has earned **5 fires / 3 sessions / 7 days**, `skill_graduate.py` (at close) writes it
out as a real `.claude/skills/<name>/SKILL.md` — a native Claude Code skill, marked with the artifact that produced
it — and demotes it automatically if it stops being used. Delete the marker comment in a generated SKILL.md and it
becomes hand-authored and permanently exempt. That is exactly how the five starter skills came to exist.

**Workflows.** Two kinds. A hash-pinned catalog of authored Workflow-tool scripts
(`scheduling/claude-si/workflow-catalog/`, one today: a three-agent adversarial review) is offered as a
`/slash-command` when a mined case matches its signals. And behavioural sequences you repeat — **2+ steps across
2+ sessions**, mined from the brain at close by `friction_loop.generate_from_workflows` — are installed as hooked
skills so the sequence is offered next time the situation recurs.

**CLAUDE.md edits.** One thing writes to your `CLAUDE.md` automatically: at close, a mined directive that has proven
itself — the correction rate it targets is falling, its trigger is live, and the always-loaded steering budget has
headroom — is appended to a section headed `## Auto-generated standing directives`. You will see lines appear there.
Edit or delete them freely; the loop never rewrites what you touched. Everything else that manages steering text
(`bin/steering-compress.py`, `bin/steering-retire.py`, `scheduling/core-si/lessons-evict.py`) proposes by default and
only acts under `--apply`. The whole always-loaded set is held under a token ratchet
(`bin/tests/test_steering_budget.py`): adding steering prose requires deleting steering prose.

**Grading the loop itself.** `eval/casebook-v1.json` holds 13 failure-mode items with predicates;
`bin/casebook-run.py` scores the repo and past transcripts against them (a monitor, not a gate), and
`bin/trajectory-gate.py` can KEEP or REVERT a candidate change by re-executing from a frozen trusted checkout so a
change cannot grade itself. Both are manual tools, exercised by the test suite; `--promote` of a trust root is
guard-gated.

**What a fresh Core does not do yet, on purpose.** Nothing mints until you have about 40 prompts and a correction
that has recurred. Workflows need repeated sequences. Skills need fired procedures. The starter contracts and
starter skills are the floor; everything above it is learned from you, and it will not learn from you before it has
watched you work. `export LEARNED_LAYER=0` turns the entire layer off.

## What the loop has actually done

[`docs/si-track-record.md`](docs/si-track-record.md) is generated from a live database, not written by hand. Over
five days (2026-07-22 → 07-27) on the author's own instance: **173 corrections mined, 36 rules written, 30 of them
live** (29 inject, 1 block), **24 revised at least once** because evidence said so rather than because a human edited
them, and one retirement the system argued for itself — `approval-gate`, pulled after 24 blocks in 34 invocations. Of
the hand-written gates, 13 carry an intent record and all 13 held against it.

Those are counts from one deployment. They show a loop that mines, installs, revises and retires on its own
evidence. They do not show it working for anyone else, because it has not yet run for anyone else.

## Multiple Cores, one brain

Run separate Cores for separate domains — work, school, a side project — each with its own memory partition and its
own learned rules, sharing one Postgres brain with row-level isolation by `org_id`. One Core is the **baseline
writer**; the rest pull shared code and cannot push back. See **[MULTI-CORE.md](./MULTI-CORE.md)**.

Sharing a brain is harder than a `WHERE` clause. [`hub_ownership.py`](scheduling/brain-pg/hub_ownership.py) decides
which Core owns a shared knowledge hub by **citation dominance**, not by which Core happened to run the ingestion
pass — the binary "cites exactly one Core" version was tried first and filed a contact cited 298 times by one Core
and six by others as shared infrastructure. [`brain-export-si-layer.py`](bin/brain-export-si-layer.py) classifies
every table `IRREPLACEABLE` / `REGENERABLE` / `EXCLUDED`, exports only the first as org-scoped SQL, and fails a test
if a newly added table is left unclassified. And baseline changes carry intent, not just bytes:
[`docs/PULL-NOTES.md`](docs/PULL-NOTES.md) plus [`bin/pull-notes.py`](bin/pull-notes.py) implement a **closed
vocabulary** of actions a pull may trigger — a note arriving over the network can *name* an action but never supply
what it means, and an unrecognised name is reported rather than executed.

## What's inside

```
.claude/
  hooks/          the enforcement layer — 47 registrations across 17 events
  rules/          always-loaded steering (memory, session, privacy, subagents, routing)
  agents/         Sentinel, Sentinel-code, close-reconciler
  commands/       /close-core, /health, /sync, /deep-plan, /core-si, /recall-similar …
  identity.json   ← YOURS. Name, org id, writer-or-puller role.
scheduling/
  brain-pg/       Postgres brain: schema, embeddings, 4-leg retrieval, health
  graphify-brain/ transcript → entities + edges pipeline
  claude-si/      the self-improvement loop
bin/              install, sync, doctor, the measurement instruments, 168 tests
memory/           ← YOURS. about-me, preferences, goals, projects, relationships
sessions/         ← YOURS. dated session logs
```

## Verifying your install

```bash
CORE_INSTANCE="$PWD" bash bin/core-doctor.sh    # multi-section health report        (needs the DB)
python3 scheduling/brain-pg/brain-health.py     # brain integrity + coverage         (needs the DB)
python3 bin/wiring-audit.py                     # is every built script reachable?   (no DB)
python3 bin/grade-intent.py                     # do the gates still match intent?   (no DB)
bash bin/tests/run-all.sh                       # the 168-file suite                 (no DB)
```

**Expect honest, not green.** On a fresh clone `wiring-audit.py` exits 1 and names 34 scripts nothing automatic calls
yet, alongside a first-party, currently non-empty list of components the project itself knows are still dangling
([`bin/wiring-allowlist.json`](bin/wiring-allowlist.json)). `run-all.sh` is not green either: roughly a dozen of the
168 fail, because they check a live seat's own state — a transcript directory, sibling Cores — that a bare template
does not have. Both print the breakdown they measured instead of a reassuring summary, which is the whole design.

`wiring-audit.py` exists because three subsystems here were once built, given an entry point, and never called by
anything for months — a hub-summary refresher that had compiled 0.78% of hubs, a cross-Core bridge frozen 52 days, a
table built to measure rule usefulness holding zero rows, ever. Each failed silently: a component that never runs
looks exactly like one with nothing to do. Two relatives:
[`verify-close-sequence.py`](bin/verify-close-sequence.py) diffs the live shell source of the session-close
controller against a declared manifest — a step declared synchronous but actually detached is how a six-minute
database dump ended up inside a sixty-second Stop hook and produced 128 GB of orphaned fragments; `actuators()` in
[`system-inventory.py`](bin/system-inventory.py) walks the AST of every hook and script for write-capable functions
that nothing calls — capability the system believes it has and does not.

**Do not run the suite under pytest.** `bin/tests/conftest.py` refuses, loudly: these are plain scripts, 147 of 148
have no `def test_*`, so pytest collects nothing, prints "no tests ran" and exits 0 — a green light from zero
assertions. `run-all.sh` reads exit codes *and* detects `MUTE` (a file that ran but demonstrated no check) and `LEAK`
(a run that changed live state).

## Honest status

**Running daily across five instances since April 2026.** A personal system published as a template, not a supported
product. Genuinely load-bearing: memory, session close, the hook enforcement layer, Sentinel, the brain and its
recall, and the self-improvement loop.

**Where it is weaker — measured, not guessed:**

- **Most of the enforcement surface sits outside the instrument that grades it.** Run `python3 bin/grade-intent.py`
  on a fresh clone: 35 gates, 28 of which expose no `detect(text)` contract at all. Those hooks fire on tool names or
  file paths. They are not failing a measurement; they are outside the one that exists.
- **The A/B harness has never decided anything.** `gate_tier_b.py` runs and produces observations but cannot gate:
  its decision rule passes on any margin, with no test that the margin exceeds chance. Too few trials returns
  `UNDECIDABLE`; more trials buys false positives along with decidability. A design change, not a bigger budget.
- **Hub summaries go stale.** A summary is written when an entity is created; re-synthesis is a separate pass.
  `brain-health.py`'s `compile_coverage` check reports what fraction has been recompiled — on the author's instance,
  low single-digit percent.
- **Setup is real work** — Postgres, an embeddings key, a Python environment; this is not `npm install`. And it is
  **opinionated**: it encodes one person's working style. Fork it and change it, which is the intent.

## What Core is NOT, and the trust model

Not a chatbot UI, not a hosted service, not a plugin, not multi-user. No server, no telemetry, nothing phones home —
configuration plus scripts that make Claude Code behave like a persistent colleague on your own machine. The moment
you fork it, the fork is yours: `memory/`, `sessions/` and `tasks/` live in your private repo and never travel
upstream. Outward actions route through Sentinel, and trust-root files carry the stricter rule above — a change to
them can never be auto-approved, only confirmed by a human. That is the recursion breaker. A compromised source can
forge a diff and a rationale; it cannot forge your confirmation.

## Documentation

| | |
|---|---|
| [docs/SETUP.md](./docs/SETUP.md) | prerequisites, install, verification, troubleshooting |
| [MULTI-CORE.md](./MULTI-CORE.md) | running several Cores against one brain |
| [docs/architecture/](./docs/architecture/) | clickable system architecture map |
| [docs/si-track-record.md](./docs/si-track-record.md) | what the self-improvement loop has actually done |
| [bin/verdict-contract.md](./bin/verdict-contract.md) | the Sentinel verdict protocol, and the forgeries that shaped it |
| [.claude/commands/close-core.md](./.claude/commands/close-core.md) | the session-close runbook — every step, what needs the model, what skips without a brain |
| [eval/casebook-v1.json](./eval/casebook-v1.json) · [bin/casebook-run.py](./bin/casebook-run.py) | the 13 failure modes the loop is graded against, and the monitor that scores them |
| [bin/trajectory-gate.py](./bin/trajectory-gate.py) | keep-or-revert a change by re-executing from a frozen trusted checkout |

## Contributing

This is a personal system shared as a template. Issues and questions are welcome; the most useful contribution is
telling me where the install broke on your machine, because every onboarding gap fixed so far was found by someone
else hitting it.

## License

Apache 2.0. See [LICENSE](./LICENSE).
