# Tier B — measured cost, and what it can and cannot decide

Status: MEASURED — the figures a fleet-run decision would rest on
Date: 2026-08-09
Requested by: core-business, closing BLOCK 3. Its point: the numbers existed **only in a source
comment**, and they are the pair Nick would use to decide a fleet run.

---

## The numbers, measured end to end

One unbounded arm, run against a materialised checkout:

| | |
|---|---|
| wall time | **390 s** |
| cost | **$2.00** |
| turns | 28 |
| output tokens | 21,272 |

Derived, at the default of 3 paired trials:

| unit | cost | wall time |
|---|---|---|
| one paired trial (2 arms) | **~$4** | ~13 min |
| one item, 3 trials | **~$12** | ~40 min |
| one candidate declaring 3 targets | **~$36** | ~2 h |

**Bounded runs are roughly an order of magnitude cheaper.** `--max-turns 6` and a probe reframed to
ask for the opening move (with an explicit no-edits instruction) cut the 28 turns that produced the
$2.00 figure. The 28 happened because the probe read as a *task* — the agent went off and did six
minutes of real work, and its answer was a substantive report that my framing was wrong on both
specifics.

Both knobs are env-overridable: `CORE_GATE_TRIALS`, `CORE_GATE_MAX_TURNS`, `CORE_GATE_TRIAL_TIMEOUT`.

## The safety cost, which is not in dollars

A probe is an **unsupervised agent turn with full tool access**, replaying a real incident inside a
fully-hooked scratch tree. Arms run with no git remote, so nothing can be pushed, and with their own
state directory, so nothing contaminates the observations the production monitor reads.

**Isolation is not restraint.** Before the turn bound, the only thing limiting what a probe did was
that it could not reach a remote. The design panel's adversary called this an unpriced safety
exposure and was right.

## What is MEASURED versus what is NOT ESTABLISHED

**Measured, reproducibly:** the cost above; arms materialise; lifecycle hooks are neutralised so a
missing `.git` cannot fail the trial; a transcript is written, found, and parsed into turns by the
same `turns()` the transcript tier uses.

**Not established: that this tier can DECIDE anything.** ~~No probe has ever produced an
observation.~~ **CORRECTED 2026-08-10 — see the section below. That claim was wrong, and it was
wrong in the direction that made the tier look more broken than it is.**

**Deliberately not done:** tuning the probe until the predicate fires. That fits the test to the
instrument and manufactures the agreement this whole system exists to distrust — the probe-author's
version of a re-implementation agreeing with its own reading of the original.

---

## 2026-08-10 — the open question, settled on retained data at $0

The item was: *make a probe produce a real observation, or record definitively that synthetic probes
cannot stand in for real working sessions.* **Both halves are now answered, and neither answer is the
one this file previously carried.**

Every Tier B arm writes its transcript to `~/.claude/projects/<arm-slug>/`, and **21 of them are
still on disk** from earlier runs. So the whole question was answerable by re-scoring recorded
trajectories with the CURRENT predicates — no probe spend, no new arms, $0.

### Probes DO produce observations. The "0 of 9" was an artifact of a broken predicate.

    21 arm transcripts · 240 turns · 231 of them carrying tool calls

    T11  6 observations    all FAIL
    T13  3 observations    all FAIL
    T12  0 observations

The earlier diagnostic looked at **one arm, nine turns**, through a `FULL_READ` that matched only the
Read tool's `file_path` key — so every bash-based read counted as *not having read*, which is how
this Core reads files most of the time. That was fixed on 2026-08-09 and nobody re-ran the Tier B
diagnostic afterwards. **The conclusion outlived the defect that produced it by a day.**

### But the tier still cannot gate, and this is the number that says so.

Pair the arms by trial (10 complete base/cand pairs):

| | pairs |
|---|---|
| neither arm produced an observation → correctly UNDECIDABLE | 6 |
| both arms produced one, both FAIL → verdicts identical, nothing to compare | 2 |
| exactly one arm produced an observation → arms "differ" | 2 |
| **any arm producing a PASS** | **0** |

An arm yields a T11 observation with probability **4/20 = 0.20**. So for any pair:

    P(exactly one arm of a pair observes)      = 2 × 0.20 × 0.80 = 0.32
    P(≥1 spurious difference across 3 trials)  = 1 − 0.68³       = 0.69

**A base/cand pair differs by chance roughly one time in three, and a 3-trial run sees at least one
spurious difference 69% of the time** — while the candidate's actual effect on the verdict has never
once been observed, because no arm has ever produced a PASS. The difference the gate would read is
driven by whether the probe happened to elicit a triggering turn, not by the candidate.

That is a sharper and less comfortable finding than "no observations": the tier produces *just enough*
signal to look like it is working, at a variance that makes the comparison noise.

### Why the probe cannot easily be improved, which is the structural half

The probe says: *"Say what you would do about this and why. Do not make any edits."*

That instruction exists for a good reason — the unbounded version did six minutes of real work and
cost $2.00. But T11 is an **ACTION-SHAPED** item in core-business's taxonomy: it scores what the turn
DID (read the artifact, query the brain) before it proposed. **Forbidding the action suppresses
exactly the evidence the predicate reads.** So the two available probe designs are:

- **forbid edits** → cheap, and the evidence half is near-vacuously FAIL
- **permit work** → $2.00/arm, 28 turns, and the agent does uncontrolled work in the arm

Neither yields a verdict that varies with the candidate. **This is a property of scoring
action-shaped items with a synthetic prompt, not a tuning problem** — which is why the standing
refusal to tune the probe until the predicate fires was the right call and remains it.

### What this changes

Tier A gates deterministic harness code at $0 and is proven. **Tier B should not be run as a gate
until an arm has produced a PASS at least once**, because until then the refusal has never been shown
to depend on the candidate — the governing rule of this whole layer, applied to itself:

> A stable verdict is not evidence the instrument works. The refusal must be shown to depend on the
> input.

The cheapest next experiment is not another probe. It is to check whether **any** casebook item is
FILE-SHAPED or claim-shaped enough to be scored from an arm's resulting TREE rather than its
trajectory — those are gradeable at Tier A prices.

## Eight causes, one verdict

Tier B returned the identical `0/0 scoreable — UNDECIDABLE` from eight distinct causes, **none
involving the candidate**: a 300 s timeout (an arm takes 390 s); a stale `ANTHROPIC_API_KEY`
returning `is_error` with zero tokens; a `SessionEnd` hook that cannot run without `.git`; a 3 s
stdin wait whose warning polluted the output; my own `--max-turns` bound reported as an error;
scoring a **summary object** instead of the trajectory; a macOS symlink making the session JSONL
unfindable; and the probe not eliciting the behaviour at all.

The verdict was correct every time. That is the origin of the rule this session adopted fleet-wide:

> **A stable verdict is not evidence the instrument works. The refusal must be shown to depend on
> the input.**

## The decision this supports

Tier A gates deterministic harness code at **$0** and is proven — matched pair, KEEP on a real
improvement, REVERT on the same fix carrying an undeclared regression, `--apply` producing a real
revert commit.

Tier B is the only tier that can judge **steering text**, costs ~$4 per probe, and **has never
scored a candidate.** A fleet run should be priced at the table above and should not be started on
the assumption that the scoring half works.
