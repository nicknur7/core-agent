#!/usr/bin/env python3
"""Tier B — counterfactual execution for STEERING changes, where replay cannot work.

WHY TIER A IS NOT ENOUGH. Tier A replays deterministic harness code (hooks, linters, predicates)
against recorded data: pure functions, same input, same output, zero model cost. That covers most
of bin/ and .claude/hooks/ and it is genuinely the majority of harness diffs.

IT CANNOT TOUCH THE STEERING SURFACE. `.claude/rules/*.md`, `CLAUDE.md`, `.claude/agents/*.md` are
PROMPT TEXT. They change what the model DOES, and nothing about that is computable from the file —
you cannot statically evaluate "does this wording make Core check the brain more often". The only
way to know is to run the agent both ways. That is what makes this the tier that costs money and
the tier the whole gate exists for.

THE THREE THINGS THAT MAKE THIS HONEST RATHER THAN THEATRE:

  1. PAIRED, NOT ABSOLUTE. Every probe runs in BOTH arms — baseline rules and candidate rules —
     with identical prompts, and only the DIFFERENCE is read. An LLM's absolute pass rate on a
     probe is meaningless; the delta between two arms differing in one file is not.

  2. THE SCORER IS NOT THE THING BEING SCORED. Trajectories are graded by the SAME predicate
     functions Tier A uses, loaded from the frozen trusted checkout. The candidate's rules shape
     the agent's behaviour; they never touch the code that judges it.

  3. NONDETERMINISM IS MEASURED, NOT ASSUMED AWAY. n paired trials per probe. If the arms overlap
     within noise, the answer is UNDECIDABLE — which the gate treats as REVERT, because a change
     that cannot be shown to help has not been shown to help.

STATUS, STATED SO THE WORD CANNOT DRIFT. core-business grepped the tree for my claim strings and
found them ONLY in this file's own comments — no log, no research artifact, no decisions-log entry,
no test referencing run_trial/score_trial/paired. "PROVEN" is an evidentiary word and it was resting
on the author asserting it. Corrected:

  MEASURED, reproducibly: one arm costs 390s / $2.00 / 28 turns unbounded; arms materialise; hooks
  are neutralised; a transcript is written, found and parsed into turns by the same turns() the
  transcript tier uses. Each of those was observed directly and the observation is what the eight
  0/0 diagnoses were made of.

  NOT ESTABLISHED: that this tier can DECIDE anything. Until a paired run yields a base-vs-candidate
  number, Tier B has never scored a candidate, and any wording stronger than "the mechanism executes"
  overstates it.

  SPLIT 2026-08-10, because "can it decide" was two questions wearing one label. `verdict()` is a
  pure function over the records `paired()` builds, it needs no arms, and NOTHING HAD EVER RUN IT —
  the same inert-mechanism shape found three times elsewhere today. It is now covered by
  `bin/tests/test_tier_b_decision.py` (15 checks, every branch, each dosed against a case that must
  answer differently): improvement passes, the mirror image reverts, a tie is not a win, too few
  scoreable trials reads UNDECIDABLE rather than as a failed candidate, and a regression
  short-circuits so a win scored earlier cannot rescue it.

  SETTLED 2026-08-10, AND NOT BY SPENDING. The open item was "run four live trials (~$8) and see
  whether the arms differ". That run cannot produce information, and the reason is arithmetic over
  this file's own decision rule, so it cost nothing to establish. `bin/tier-b-power.py` recomputes
  all of it; the numbers are NOT restated here, because a figure in a docstring ages and this file
  has already been wrong about one.

  THERE IS NO GOOD OPERATING POINT, which is a stronger answer than "unproven":

    too few trials   `verdict()` refuses unless BOTH arms reach min_ran scoreable trials. At the
                     2-trial floor that happens in a few percent of runs across every plausible
                     observe rate — so the cheapest run is overwhelmingly likely to return
                     UNDECIDABLE before the candidate is considered at all. A REVERT produced that
                     way is the absence of a measurement wearing a verdict's clothes.

    enough trials    raising the count buys decidability and FALSE POSITIVES together. `verdict()`
                     passes on ANY margin — `if c > b` — with no test that the margin exceeds
                     chance, so at ten trials it declares an improvement from pure noise a large
                     fraction of the time. A bigger budget makes this worse, not better.

  So the blocker was never the money. It is that `c > b` cannot separate signal from variance at any
  n, and fixing that is a DESIGN change to this file — a test that accounts for the trial count —
  not an experiment to authorise.

  ONE CORRECTION TO THE PARAGRAPH ABOVE, found while doing this. It states "an arm observes with
  p=0.20", and the evidence it cites disagrees: 10 pairs is 20 arms, of which 6-neither /
  2-both / 2-exactly-one gives 6 observing arms of 20, i.e. 0.30. The "differs by chance 32%" figure
  implies 2p(1-p)=0.32 and therefore p=0.20; at 0.30 it would be 0.42. The constant and the data it
  was drawn from cannot both be right. tier-b-power.py sweeps the range instead of inheriting
  either, and the conclusion holds across all of it — which is why this is recorded rather than
  resolved.

  CORRECTED 2026-08-10. This said "no probe has ever produced an observation — the last diagnostic
  found 0 of 9". THAT WAS AN ARTIFACT OF A BROKEN PREDICATE and it outlived the defect by a day: the
  diagnostic ran on ONE arm through a FULL_READ that matched only the Read tool's `file_path`, so
  every bash-based read — which is how this Core reads most files — counted as not having read. It
  was fixed 2026-08-09 and nobody re-ran the diagnostic.

  Re-scored at $0 against the 21 arm transcripts still on disk under ~/.claude/projects: 240 turns,
  T11 6 observations, T13 3. PROBES DO PRODUCE OBSERVATIONS.

  The tier still cannot gate, for a sharper reason. Across 10 complete base/cand pairs: 6 where
  neither arm observed, 2 where both observed and both FAILed, 2 where exactly one arm observed —
  and ZERO arms producing a PASS, ever. An arm observes with p=0.20, so a pair differs BY CHANCE
  32% of the time and a 3-trial run sees a spurious difference 69% of the time, while the
  candidate's effect on a verdict has never been observed at all. The tier emits just enough signal
  to look functional at a variance that makes the comparison noise. Full working:
  docs/tier-b-cost-2026-08-09.md.

WHAT THIS DELIBERATELY DOES NOT DO: run unsupervised agents with tool access against a live system.
Each arm executes in its own materialised scratch tree with its own state directory and no git
remote, so a probe cannot push, cannot write production state, and cannot contaminate the
reply-observations the production monitor reads.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Steering files: prompt text, not code. A candidate touching only these is Tier B's business.
STEERING_PREFIXES = (
    ".claude/rules/", ".claude/rules-life/", ".claude/agents/",
    "CLAUDE.md", ".claude/CLAUDE.base.md",
)

DEFAULT_TRIALS = 3          # paired trials per probe; raised by --trials
# 300s was too short: a headless turn in a materialised tree loads the full steering surface and
# runs SessionStart hooks before it answers, and every trial timed out — reported as 0/0 scoreable,
# which the gate correctly called UNDECIDABLE. Correct verdict, useless signal: an instrument that
# ALWAYS times out is indistinguishable from a candidate that cannot be judged.
TRIAL_TIMEOUT = int(os.environ.get("CORE_GATE_TRIAL_TIMEOUT", "900"))

# MEASURED, NOT ESTIMATED. One unbounded arm on 2026-08-09: 390 seconds, $2.00, 28 turns, 21k
# output tokens. So a paired trial is ~13 minutes and ~$4 PER PROBE, and the default 3 trials is
# ~$12 for one item. That is the number the design panel argued about without anyone having it.
#
# 28 turns happened because the probe read as a TASK and the agent went off and DID the work —
# editing the scratch tree for six minutes. Tier B does not want the work done; it wants to observe
# whether the FIRST move cites the brain before proposing a primitive change. Bounding turns cuts
# the cost by roughly an order of magnitude AND shrinks the autonomous surface, which the design
# panel's adversary flagged as an unpriced safety exposure: unsupervised agents with full tool
# access, replaying incidents, in a fully-hooked tree.
MAX_TURNS = int(os.environ.get("CORE_GATE_MAX_TURNS", "6"))


def framed(prompt: str) -> str:
    """Wrap the incident so the probe elicits the FIRST MOVE, not a six-minute repair.

    The predicate reads what the turn DID before it proposed — so the trial only needs the opening
    move. Asking for the fix produced a correct, expensive, and irrelevant transcript.
    """
    return (
        "Situation from this Core's own history:\n\n%s\n\n"
        "Say what you would do about this and why. Do not make any edits." % prompt)


# Per-turn churn that is not intentional change: state, session logs, generated manifests. These
# are dirty on nearly every commit (this session's own git status shows a dozen), and routing on
# them would send a pure-steering change down the wrong tier — measured: a rules-only commit swept
# in 18 state files and Tier A judged it, finding no deterministic improvement, so it reverted a
# change replay could never assess. Excluded from routing; a REAL edit to one of them is still
# caught by Tier A's regression check, which reads their content, not just their names.
NOISE_PREFIXES = (".claude/state/", "sessions/", "memory/", "bin/.gate-trusted-sha")


def _signal(files):
    return [f for f in files if not any(f.startswith(p) for p in NOISE_PREFIXES)]


def is_steering_only(files):
    """True when every SIGNAL-BEARING changed file is prompt text — Tier A cannot judge those.

    Filters per-turn state churn first, because whether a change is 'steering-only' is a question
    about what the author MEANT to change, and a dozen auto-written state files are not that.
    """
    sig = _signal(files)
    return bool(sig) and all(
        any(f == p or f.startswith(p) for p in STEERING_PREFIXES) for f in sig)


def probes_for(items, targets, predicates=None):
    """Probe prompts, taken from each casebook item's own minted_from narrative.

    NOT invented strings. Each item exists because a specific failure happened; the probe is that
    situation put back to the agent. A probe someone made up tests the probe author's imagination,
    which is the same defect as a re-implementation testing your reading of the original.
    """
    out, unscoreable = [], []
    for it in items:
        iid = it.get("id")
        if iid not in targets:
            continue
        # A TARGET WITH NO PREDICATE CANNOT BE SCORED, so it must not be TRIALLED. The first run
        # of this executed real model calls in both arms, scored every one as None because
        # PREDS holds only T11/T12/T13, and reported "0/0 scoreable trials". Correct verdict,
        # paid for at full price, and indistinguishable from a candidate that genuinely could not
        # be judged. Spending money to produce an unusable number is worse than refusing up front.
        if predicates is not None and iid not in getattr(predicates, "PREDS", {}):
            unscoreable.append(iid)
            continue
        prompt = it.get("probe_prompt") or it.get("minted_from") or it.get("claim")
        if prompt:
            out.append({"id": iid, "prompt": str(prompt)})
    return out, unscoreable


def neutralise_lifecycle_hooks(tree):
    """Strip SessionEnd/Stop/SessionStart hooks from an arm before running a trial.

    A materialised arm has NO .git — git archive does not include one — so defensive-save.sh tries
    to commit, cancels, and `claude -p` returns non-zero. Every trial failed with
    'SessionEnd hook failed: Hook cancelled' and the probe scored 0/0 UNDECIDABLE. The candidate
    was never the reason.

    SAFE FOR TIER B SPECIFICALLY: this tier only judges STEERING changes, so both arms carry
    IDENTICAL hooks and removing them from both cannot bias the comparison. These are
    session-management hooks — commit, push, stamp — not the behaviour under test. Tier A, which
    judges hook code itself, does not run trials and is unaffected.
    """
    import json as _json
    s = tree / ".claude" / "settings.json"
    if not s.is_file():
        return
    try:
        doc = _json.loads(s.read_text())
    except Exception:
        return
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return
    for ev in ("SessionEnd", "Stop", "SessionStart", "SubagentStop"):
        hooks.pop(ev, None)
    try:
        s.write_text(_json.dumps(doc, indent=2))
    except OSError:
        pass


def run_trial(tree, state_dir, prompt):
    """One headless turn inside one arm. Returns the transcript path, or None if it did not run.

    NONE IS NOT A PASS. The caller counts a failed trial as a failed trial; a trial that did not
    execute must never be silently dropped from the denominator, because dropping the hard ones is
    how a candidate survives on its easy probes.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(tree)
    env["CORE_INSTANCE"] = str(tree)
    env["CORE_GATE_STATE"] = str(state_dir)
    env["CORE_GATE_TRIAL"] = "1"                 # hooks can see this is not a real session
    # STRIP THE STALE API KEY. A leftover ANTHROPIC_API_KEY in the environment takes precedence
    # over the claude.ai subscription login, and the headless path retired for that key on
    # 2026-07-24 — so every trial returned {"is_error": true} with ZERO tokens and a warning on
    # stderr. Measured directly: with the key set the run errors; with `env -u ANTHROPIC_API_KEY`
    # the identical command succeeds.
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        r = subprocess.run(
            ["claude", "-p", framed(prompt), "--output-format", "json",
             "--max-turns", str(MAX_TURNS)],
            cwd=str(tree), capture_output=True, text=True, env=env, timeout=TRIAL_TIMEOUT,
            # CLOSE STDIN. Without this `claude` waits 3s for piped input, warns on stderr, and the
            # warning lands in the captured output — a fourth distinct cause of the SAME
            # '0/0 scoreable trials' verdict. Nothing about it involves the candidate.
            stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, "%s: %s" % (type(e).__name__, e)
    # A WARNING ON STDERR IS NOT A FAILED TURN. Judge by whether a usable transcript came back,
    # not by the exit code alone — three of today's four 0/0 causes were the harness, not the model,
    # and each looked identical from the verdict. If stdout parses and is not is_error, it ran.
    if r.returncode != 0 and not (r.stdout or "").lstrip().startswith("{"):
        return None, (r.stderr or r.stdout or "").strip()[:200]
    # AN ERRORED TURN IS NOT A TRANSCRIPT. `claude -p` exits 0 while returning
    # {"is_error": true, "output_tokens": 0} — so the trial "succeeded", scored None for want of
    # content, and landed in the UNDECIDABLE bucket with an EMPTY error list. Nothing anywhere said
    # the model had not run. Surfaced as the error it is.
    try:
        doc = json.loads(r.stdout)
        if isinstance(doc, dict) and doc.get("is_error"):
            toks = (doc.get("usage") or {}).get("output_tokens", 0)
            # EXHAUSTING --max-turns IS NOT A FAILED TURN, it is MY bound. The CLI reports it as
            # is_error, but the transcript is real and scoreable — measured at 6450 and 4500 output
            # tokens across the two arms. Discarding it was the fifth distinct cause of the same
            # '0/0 scoreable' verdict, and the first one where the model had genuinely run.
            #
            # Zero tokens is the case that IS a failure: nothing ran, nothing to score. That
            # distinction is the whole difference between a bounded trial and a dead one, and
            # collapsing them is how a working instrument reads as a broken candidate.
            if not toks:
                return None, "model produced NO output (is_error, 0 tokens): %s" % (
                    str(doc.get("result") or r.stderr or "")[:120])
    except Exception:
        pass
    return r.stdout, None


def _session_jsonl(tree, session_id):
    """The real transcript claude wrote for this trial, under the ARM's project slug."""
    # BOTH THE RESOLVED AND UNRESOLVED PATH. macOS symlinks /tmp -> /private/tmp, so .resolve()
    # yields /private/var/folders/... while `claude` slugs the cwd it was GIVEN, /var/folders/... .
    # Looking only at the resolved form found no transcript, scored None, and produced the SEVENTH
    # instance of 'UNDECIDABLE — 0/0 scoreable trials'. A missing file and an unobservable
    # behaviour had, once again, the identical signature.
    # SLUGGED BY THE ONE IMPLEMENTATION, not a third local copy. This carried
    # `.replace("/", "-").replace(" ", "-")` — the SAME two-character substitution that
    # bin/core_seat.py was created to eliminate, and core-business found it still live here after
    # that consolidation (#924 BLOCK 3): "the fix consolidated three and left a fourth."
    #
    # It diverges from Claude Code's actual slug on any path containing a dot or an underscore, and
    # the arm directories are named `core-gate-tierb-<random>-base`, so a run id containing either
    # character resolves to a directory that does not exist. The failure is a MISSING TRANSCRIPT,
    # which this tier already reports as `0/0 scoreable — UNDECIDABLE` — the eighth distinct cause
    # of the identical verdict, and indistinguishable from the seven others.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core_seat import transcripts_dir
    roots = {str(Path(tree)), str(Path(tree).resolve())}
    for r in roots:
        d = transcripts_dir(r)
        if session_id:
            direct = d / ("%s.jsonl" % session_id)
            if direct.is_file():
                return direct
        if d.is_dir():
            cands = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
            if cands:
                return cands[0]
    return None


def score_trial(raw, predicate_mod, item_id, tree=None):
    """Grade one trajectory with the FROZEN predicate. Returns True/False/None(unscoreable).

    SCORES THE TRANSCRIPT, NOT THE SUMMARY. `claude -p --output-format json` returns a SUMMARY
    OBJECT — session_id, usage, cost, a `result` string — and NOT the trajectory. Measured: 8179
    output tokens produced a 1408-character JSON blob with an EMPTY result field, because the work
    lived in tool calls that the summary does not carry.

    So every trial was scored against metadata. T11 asks whether the turn READ the artifact and
    QUERIED THE BRAIN — both of which are TOOL CALLS — and neither can appear in a summary. The
    trigger could never fire, and the probe reported 'not an observation' forever. Sixth distinct
    cause of the same 0/0 verdict, and the only one that was not a harness accident: I was reading
    the wrong artifact from the start.

    Now parses the session JSONL with the SAME turns() the transcript tier uses, so Tier A and
    Tier B grade identical structures with identical code.
    """
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except Exception:
        return None
    fn = getattr(predicate_mod, "PREDS", {}).get(item_id)
    if fn is None:
        return None

    path = _session_jsonl(tree, doc.get("session_id")) if tree else None
    if path is None:
        return None
    try:
        turns = predicate_mod.turns(str(path))
    except Exception:
        return None
    if not turns:
        return None

    # A probe is ONE prompt, so the trial is the turns it produced. If ANY turn is an observation,
    # the trial is scored on it; a trial with no observation is not a failure, it is no data.
    seen = [v for v in (fn(t) for t in turns) if v is not None]
    if not seen:
        return None
    return all(v == "PASS" for v in seen)


def paired(base_tree, cand_tree, work, probes, predicate_mod, trials):
    """Run every probe in BOTH arms, n times, and report the per-arm pass counts.

    Reads only the DIFFERENCE. An arm's absolute rate is not a number this system is entitled to.
    """
    for _arm in (base_tree, cand_tree):
        neutralise_lifecycle_hooks(_arm)
    results = []
    for p in probes:
        rec = {"id": p["id"], "trials": trials, "base_pass": 0, "cand_pass": 0,
               "base_ran": 0, "cand_ran": 0, "errors": []}
        for i in range(trials):
            for arm, tree, key in (("base", base_tree, "base"), ("cand", cand_tree, "cand")):
                raw, err = run_trial(tree, work / ("state-%s-%d" % (arm, i)), p["prompt"])
                if err:
                    rec["errors"].append("%s trial %d: %s" % (arm, i, err))
                    continue
                v = score_trial(raw, predicate_mod, p["id"], tree)
                if v is None:
                    continue
                rec[key + "_ran"] += 1
                if v:
                    rec[key + "_pass"] += 1
        results.append(rec)
    return results


def verdict(results, min_ran=2):
    """PASS only on a clear paired improvement. Everything else is UNDECIDABLE, hence REVERT.

    A tie is not a win. Fewer than min_ran scoreable trials in either arm is not a win either —
    it is an absence of evidence, and this gate does not convert absence into approval.
    """
    detail, improved = [], False
    for r in results:
        if r["base_ran"] < min_ran or r["cand_ran"] < min_ran:
            detail.append("%s UNDECIDABLE — only %d/%d scoreable trials (base/cand)"
                          % (r["id"], r["base_ran"], r["cand_ran"]))
            return False, detail
        b = r["base_pass"] / r["base_ran"]
        c = r["cand_pass"] / r["cand_ran"]
        if c > b:
            improved = True
            detail.append("%s improved %.0f%% -> %.0f%% (%d/%d vs %d/%d)"
                          % (r["id"], b * 100, c * 100, r["base_pass"], r["base_ran"],
                             r["cand_pass"], r["cand_ran"]))
        elif c < b:
            detail.append("%s REGRESSED %.0f%% -> %.0f%%" % (r["id"], b * 100, c * 100))
            return False, detail
        else:
            detail.append("%s unchanged at %.0f%% — a tie is not a win" % (r["id"], b * 100))
    return improved, detail
