#!/usr/bin/env python3
"""trajectory-gate.py — the tier that can actually GATE: keep or revert a candidate, no human.

WHAT MAKES THIS DIFFERENT FROM bin/casebook-run.py. That runner is a MONITOR: it grades the repo as
it stands and trajectories already recorded, so it cannot answer "would this candidate make things
worse" — a candidate diff has produced neither a repo state nor a transcript yet. core-business:

    A GATE REQUIRES COUNTERFACTUAL EXECUTION. A MONITOR DOES NOT.

THE HOLE EVERY DESIGN HAD. A four-agent design panel produced four independent architectures
(replay, fixture, shadow, property) and an adversary killed each one THE SAME WAY: the gate script
lives in bin/, bin/ is candidate-editable, so a candidate edits the gate and grades itself. Four
architectures, one root cause. Scores 6/5/4/3 — nothing cleared the bar.

THE ARCHITECTURE NOBODY PROPOSED, from the panel's completeness critic: this is not a testing
problem, it is Ken Thompson's trusting-trust problem. The existing answer — "refuse to grade if the
diff touches the evaluator" — is not a location for the trust root, it is an abstention. So:

    THE GATE RE-EXECUTES ITSELF FROM A FROZEN, SEPARATELY-MATERIALISED CHECKOUT AT AN APPROVED SHA
    BEFORE IT GRADES ANYTHING. The candidate's copy of every evaluator file, including this one, is
    never loaded. When the candidate IS a change to the evaluator, the OLD trusted evaluator grades
    it, and only explicit promotion moves the trust root forward.

TWO CONSTRAINTS EVERY DESIGN DODGED, resolved here rather than left implicit:

  UNDECIDABLE DEFAULTS TO REVERT. A monitor may print NO-DATA forever; a human reads it. A GATE'S
  SILENCE IS A DECISION — no action means the change stays, which is the direction that looks like
  success. So every path that cannot decide returns REVERT. The cost is real (a flaky evaluator
  reverts good work) and is bounded by making the failure LOUD and the revert reversible: revert is
  a `git revert` commit on the candidate's own branch, never a force-push, never a history rewrite.

  ISOLATING CODE DOES NOT ISOLATE STATE. `.claude/state/**`, `memory/**` and `sessions/**` are
  per_core_keep — unversioned, and read by the production monitor. A trial that executes the
  candidate agent writes reply-observations.jsonl, hook-dispositions.json and steering-ledger.json,
  CONTAMINATING CORE'S OWN SELF-KNOWLEDGE with counterfactual activity. Every trial therefore runs
  with CLAUDE_PROJECT_DIR pointed at its own scratch tree and its own state dir.

  python3 bin/trajectory-gate.py --candidate <sha>        # decide KEEP or REVERT
  python3 bin/trajectory-gate.py --candidate <sha> --apply  # ...and execute the revert
  python3 bin/trajectory-gate.py --promote <sha>          # move the trust root (Nick/Sentinel only)
  python3 bin/trajectory-gate.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TRUSTED_SHA_FILE = REPO / "bin" / ".gate-trusted-sha"
TRUSTED_ENV = "CORE_GATE_TRUSTED_ROOT"

KEEP, REVERT, REFUSED = "KEEP", "REVERT", "REFUSED"
# UNGRADED — measured NOTHING, as distinct from measured and WORSE.
#
# core-business's argument, and it is about WHO HAS WORK TO DO rather than about caution:
# "REVERT, the candidate regressed" is a message to the candidate's AUTHOR. "UNGRADED, an arm
# produced nothing parseable" is a message to the GATE'S MAINTAINER. Collapsing them sends every
# message to the wrong person, and the gate maintainer is the one who never hears.
#
# This is not hypothetical here. FOUR separate defects made this gate unable to grade anything —
# a trust root pinned to a runner whose --json emitted prose, a frozen tree reading its own
# historical pointer, a finding embedding the arm's temp path, and a matched pair that was not
# matched — and every one of them surfaced as REVERT. Tier B had the same shape one tier down:
# seven runs, six distinct INSTRUMENT failures, all printing the identical 0/0 UNDECIDABLE.
#
# THE SAFE ACTION IS UNCHANGED: an ungraded candidate is not kept. Only the VERDICT differs, so the
# two audiences can tell which one is being spoken to.
UNGRADED = "UNGRADED"

# Evaluator code. A candidate touching any of it cannot be graded by the CURRENT tree — it is
# graded by the frozen one, which is the whole point of the re-exec below.
TCB = [
    "eval/",
    "bin/casebook-run.py",
    "bin/casebook_predicates.py",
    # CONSOLIDATION MOVES THE PROTECTION SURFACE, and no list re-derives itself. core_seat.py did not
    # exist when this list was written — it was created BY the consolidation that collapsed three
    # Core-root resolvers into one module (core-business, 2026-08-10). So the list was CORRECT before
    # that change and became WRONG without being edited.
    #
    # It is the worst single omission available: seat_root() decides WHICH tree is measured and
    # transcripts_dir() decides WHERE the evidence comes from. Uncovered, a candidate may rewrite it,
    # touches_tcb records no evaluator touch, and the trust root can auto-advance onto a commit that
    # changed what the evaluator measures.
    "bin/core_seat.py",
    "bin/trajectory-gate.py",
    "bin/gate_tier_b.py",
    # THE POINTER ITSELF. Not on any list before: a candidate could name its own grader by
    # committing a new value here, and nothing refused.
    "bin/.gate-trusted-sha",
    ".claude/hooks/reply-observer.py",
    "bin/si-objective.py",
    "bin/lint-doc-paths.py",  # lint-code-paths: ignore — TCB fence entry compared by startswith against RELATIVE git paths; an absolute constant never matches and silently disarms the fence
]


def sh(args, cwd=None, timeout=120):
    return subprocess.run(args, cwd=str(cwd or REPO), capture_output=True, text=True,
                          timeout=timeout)


# ── the trust root ────────────────────────────────────────────────────────────────────────────
# Case-INSENSITIVE, then normalised. `git rev-parse` emits lowercase, but an uppercase
# 40-hex string IS the shape, and refusing it would be a false refusal on a trust root —
# the failure mode this whole change exists to avoid, pointed the other way. Enforce the
# SHAPE and canonicalise the case; that is validation, not leniency.
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")


def trusted_sha():
    """The SHA whose evaluator is trusted. Absent = UNDECIDABLE = the caller must revert."""
    # READ FROM THE SOURCE REPO. TRUSTED_SHA_FILE is relative to REPO, and after the re-exec REPO
    # is the frozen tree — whose copy of this file holds whatever the value was AT THAT COMMIT.
    # Reporting it showed a stale trust root that had already been promoted past.
    # SOURCE_REPO IS THE TREE UNDER TEST. Reading the trust pointer from it FIRST meant THE
    # CANDIDATE NAMED ITS OWN GRADER — core-business planted a SHA in a scratch source repo and
    # trusted_sha() returned it. The pointer is now read from the RUNNING (frozen) copy first when
    # we are already inside one, and a candidate that touches the pointer is refused outright.
    # THE STAMP IS THE AUTHORITY INSIDE A FROZEN TREE, NOT THE TREE'S OWN COPY OF THE POINTER.
    #
    # Two fixes for two different failures were applied to these same lines, and the second undid
    # the first — the comment three lines above still describes the bug the code below reintroduced:
    # "after the re-exec REPO is the frozen tree — whose copy of this file holds whatever the value
    # was AT THAT COMMIT."
    #
    # Reading the frozen tree's `.gate-trusted-sha` first correctly stops a CANDIDATE from naming its
    # own grader, and it also reads a HISTORICAL pointer. Measured after promoting to 651c321, whose
    # committed pointer is e4f2f6a370a1:
    #
    #     REVERT — CORE_GATE_TRUSTED_ROOT is set but does not point at the trusted tree.
    #       expected frozen-at e4f2f6a370a1, found '651c32189666'
    #
    # So the gate REFUSES AFTER EVERY PROMOTION whose target committed a different pointer — which is
    # every promotion — and the refusal bites precisely when someone fixes the other inertness.
    #
    # `.gate-frozen-at` is written by the PARENT, from the live repo, immediately before the re-exec.
    # It is the SHA the parent actually materialised, so it cannot be stale and cannot be chosen by
    # the candidate. The anti-self-grading property is unchanged and is enforced where it belongs:
    # `bin/.gate-trusted-sha` is in TCB, so a candidate touching the pointer is refused outright.
    _running = Path(os.environ.get(TRUSTED_ENV) or "")
    order = ([_running / ".gate-frozen-at",
              _running / "bin" / ".gate-trusted-sha"] if os.environ.get(TRUSTED_ENV) else []) + [
        TRUSTED_SHA_FILE, source_repo() / "bin" / ".gate-trusted-sha"]
    for cand in order:
        try:
            s = cand.read_text().strip().split()[0]
        except Exception:
            continue
        # WELL-FORMED, NOT MERELY PRESENT. This returned the first whitespace-delimited token of
        # whatever the file held, and core-business found the shipped file contaminated with a line
        # of gate self-test output ABOVE the SHA. Measured, not inferred:
        #
        #     trusted_sha()     -> 'FAIL'   (4 chars — the first token of a log line)
        #     if not sha:       -> False    the REVERT branch does NOT fire
        #     git archive FAIL  -> fatal: not a valid object name
        #
        # A TOLERANT PARSE ON A TRUST ROOT: the guard checked missing-or-empty and never checked
        # SHAPE, so a trust root that is present and garbage walked past the one branch written to
        # catch it. It failed CLOSED — materialise() raises — which is the only reason this was a
        # must-fix rather than a silent KEEP.
        #
        # Same fragility as the sentinel receipt's positional REVIEWED parse: parse leniency standing
        # in for validation. Both want the same answer — refuse what does not match the shape, and
        # say so rather than proceeding on a value that cannot be what it claims to be.
        if _SHA_RE.fullmatch(s or ""):
            return s.lower()
        if s:
            sys.stderr.write(
                "trajectory-gate: trust root %s is present but MALFORMED (%r) — refusing to treat "
                "it as a SHA.\n" % (cand, s[:60]))
    return None


def materialise(sha, dest):
    """Extract a commit as a real tree. `git archive`, NOT `git worktree add`.

    A worktree REGISTERS itself in the repo being read — the evaluator would be mutating the
    repository it is grading, which is the same class of defect as grading yourself. An archive
    extract touches nothing on the source side.
    """
    dest.mkdir(parents=True, exist_ok=True)
    # FROM THE SOURCE REPO, NOT REPO. After the re-exec, REPO is the FROZEN checkout — which has no
    # .git, because git archive does not include it. Archiving against it emitted "fatal: not a git
    # repository" to stderr, produced an EMPTY tarball, and tar exited 0 on empty input. So the gate
    # materialised two empty trees, graded them, found no regressions and no improvements, and
    # printed a confident REVERT with a fabricated reason. It failed in the safe DIRECTION and for
    # an invented cause — had the polarity been reversed it would have been a silent KEEP.
    src = source_repo()
    p1 = subprocess.Popen(["git", "-C", str(src), "archive", sha], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    p2 = subprocess.Popen(["tar", "-x", "-C", str(dest)], stdin=p1.stdout)
    p1.stdout.close()
    p2.communicate()
    err = (p1.stderr.read() or b"").decode(errors="replace").strip()
    if p2.returncode != 0 or p1.wait() != 0:
        raise RuntimeError("could not materialise %s: %s" % (sha[:12], err))
    # AN EMPTY TREE IS NOT A TREE. Checked because the failure above was silent: every downstream
    # comparison still "worked", on nothing.
    # bin/ ONLY. eval/ is shipped again (2026-09-04, scrubbed item set) but it is DATA, and for one
    # day in between it was absent from every tree, which made this check fail on every seat for a
    # reason that had nothing to do with an empty or corrupt archive — the failure it exists to catch.
    # bin/ alone is the integrity anchor: universally shipped, present at every commit.
    if not (dest / "bin").is_dir():
        raise RuntimeError("materialised tree for %s is empty or malformed (%s)" % (sha[:12], err))
    return dest


def reexec_from_trusted(argv):
    """Re-run THIS SCRIPT from the frozen checkout, then never return.

    THE SINGLE MOST IMPORTANT LINE IN THE FILE. Without it the orchestrator that computes the
    verdict and issues the revert is the candidate's own copy, and every other precaution is
    theatre — that is exactly how all four panel designs died.
    """
    marker = os.environ.get(TRUSTED_ENV)
    if marker:
        # VERIFY THE CLAIM, DO NOT TRUST THE VARIABLE. `if env: return` was a ONE-LINE OFF-SWITCH:
        # core-business did not theorise it, it USED it — setting CORE_GATE_TRUSTED_ROOT is how it
        # loaded this module to run the SHA test. One environment variable and the candidate's own
        # copy grades itself, which is the entire property the re-exec exists to establish.
        #
        # The materialiser now stamps the SHA it extracted into the tree; re-entry requires that
        # stamp to match the trust pointer. A hand-set variable pointing anywhere else is refused.
        # THE CHECK BELOW COMPARED A FILE TO ITSELF, and it was written as the fix for this exact
        # hole. `trusted_sha()` reads `<marker>/.gate-frozen-at` FIRST when TRUSTED_ENV is set, and
        # `got` reads that same file. So `want == got` held by construction, for any marker, and the
        # comment above ("re-entry requires that stamp to match the trust pointer") described a
        # comparison that could not fail. Demonstrated rather than argued, 2026-08-10:
        #
        #     CORE_GATE_TRUSTED_ROOT=/tmp/fake  with a planted 40-hex .gate-frozen-at
        #     trusted_sha() -> the planted value        got -> the planted value        ACCEPTED
        #
        # core-business pointed at this layer from a db708c1 checkout (bus #1008) and described a
        # bare presence check; that older shape was already fixed. The replacement was vacuous, which
        # is worse than the original, because it reads as a verification and reviews as one.
        #
        # The authority must be a value the claimed frozen tree does NOT supply: the SOURCE repo's
        # COMMITTED pointer. That also closes the working-tree tamper — TCB detection is commit-based
        # while the pointer was read from the working tree, so an uncommitted edit was invisible to
        # it. Committing the pointer instead IS a TCB touch, which the existing machinery refuses.
        stamp = Path(marker) / ".gate-frozen-at"
        want = source_pointer()
        try:
            got = stamp.read_text().strip()
        except Exception:
            got = ""
        if not want or got != want:
            print("REVERT — CORE_GATE_TRUSTED_ROOT is set but does not point at the trusted tree.")
            print("  expected frozen-at %s, found %r" % ((want or "<none>")[:12], got[:12]))
            print("  Refusing: an unverified trusted root is the candidate grading itself.")
            sys.exit(2)
        return                                   # verified: inside the real frozen tree
    # BOOTSTRAP. --promote and --status must run WITHOUT a trust root or the tool can never
    # acquire one: re-exec happens before argument parsing, so the only command that can create
    # the trust root would itself require the trust root to exist. Found by running --status on a
    # fresh install. Neither command grades anything, so neither needs a frozen evaluator —
    # --promote WRITES the root (and is a reviewed trust-root change), --status only reports.
    if any(x in argv for x in ("--promote", "--status")):
        return
    sha = trusted_sha()
    if not sha:
        print("REVERT — no trust root. bin/.gate-trusted-sha is missing or empty, so there is no")
        print("  frozen evaluator to grade with. Undecidable defaults to REVERT, never to KEEP.")
        print("  Establish one with: python3 bin/trajectory-gate.py --promote <sha>")
        sys.exit(2)

    root = Path(tempfile.mkdtemp(prefix="core-gate-trusted-"))
    materialise(sha, root)
    me = root / "bin" / "trajectory-gate.py"
    if not me.is_file():
        print("REVERT — the trusted checkout %s has no bin/trajectory-gate.py." % sha[:12])
        sys.exit(2)

    try:
        (root / ".gate-frozen-at").write_text(sha + "\n")
    except OSError:
        print("REVERT — could not stamp the frozen tree; refusing rather than running unverified.")
        sys.exit(2)
    env = dict(os.environ)
    env[TRUSTED_ENV] = str(root)
    env["CORE_GATE_SOURCE_REPO"] = str(REPO)     # the tree under test, passed explicitly
    os.execve(sys.executable, [sys.executable, str(me), *argv[1:]], env)


def source_repo():
    """The repo being graded. Distinct from REPO once we are executing inside the frozen tree."""
    return Path(os.environ.get("CORE_GATE_SOURCE_REPO") or REPO)


def source_pointer():
    """The trust pointer as the SOURCE repo holds it — never the claimed frozen tree's own copy.

    EXISTS SOLELY SO THE RE-ENTRY CHECK IS NOT VACUOUS. `trusted_sha()` consults
    `<marker>/.gate-frozen-at` FIRST when the env var is set, which is correct for reporting (that
    stamp is what the parent actually materialised, so it cannot be stale) and useless for
    VERIFYING the marker, because it is the same file the check compares against. Reading the
    pointer from the source side gives the comparison two independent operands.

    WHY NOT `HEAD:bin/.gate-trusted-sha`. I tried that first and it broke the gate's own matched-pair
    test. `bin/.gate-trusted-sha` is PER-SEAT STATE, not source — `--promote` writes it into the
    working tree and does not commit it, which the test says in its own header and which I read only
    after the red. Reading HEAD made the parent materialise from one value and the child verify
    against another: two resolvers for one subject, the class I have spent the day removing,
    introduced by me while fixing a different hole in the same function.

    HONEST LIMIT, stated because the alternative is claiming a property this does not have.
    `source_repo()` resolves via `CORE_GATE_SOURCE_REPO`, which is environment, and
    `.claude/settings.json` carries an `env` block injected into every tool call and is not in TCB.
    A caller controlling the whole environment can point both variables at trees they authored and
    no in-process check can tell. That boundary is not closable here. What IS closed is the case
    core-business demonstrated: a claimed frozen tree supplying its own authority.
    """
    # SOURCE FIRST, AND THE ORDER IS THE WHOLE FUNCTION. TRUSTED_SHA_FILE is relative to REPO, and
    # inside the frozen tree REPO *is* the frozen tree — so reading it first returns that tree's
    # HISTORICAL copy of the pointer, which is the stale-root bug this file has now hit three times.
    # It surfaced as:
    #
    #     REVERT — CORE_GATE_TRUSTED_ROOT is set but does not point at the trusted tree.
    #       expected frozen-at e017d05ef6ef, found '0d80dad28a8d'
    #
    # where 0d80dad2 is what the parent actually materialised (the test promotes to its scratch
    # HEAD) and e017d05e is what the frozen tree's own committed pointer happened to say. The PARENT
    # resolves the pointer with REPO == source, so the child must resolve it the same way or the two
    # disagree by construction — two resolvers, one subject, which is what the first two attempts at
    # this fix each reintroduced in a different spelling.
    #
    # This still leaves two INDEPENDENT operands for the re-entry check: the pointer comes from the
    # source side, the stamp from the claimed marker tree. That is the property that was missing when
    # `want` and `got` read the same file.
    # NO FALLBACK TO TRUSTED_SHA_FILE. I kept it as a second candidate; core-business shipped the
    # single-element form and it is right. In the child, TRUSTED_SHA_FILE resolves to the FROZEN
    # tree's copy — the very value this function exists to avoid — so a fallback here is the same
    # defect waiting for the source file to be absent for any reason. Returning None instead is
    # correct: no pointer is UNDECIDABLE, and undecidable defaults to REVERT.
    for cand in (source_repo() / "bin" / ".gate-trusted-sha",):
        try:
            tok = cand.read_text().strip().split()[0]
        except Exception:
            continue
        if _SHA_RE.fullmatch(tok or ""):
            return tok.lower()
    return None


# ── the decision inputs ───────────────────────────────────────────────────────────────────────
TRAILER_RE = re.compile(r"^\s*Casebook-Target:\s*(.+?)\s*$", re.I | re.M)


def declared_targets(sha, repo):
    """Casebook items the candidate CLAIMS to improve, from a commit trailer.

    NO DECLARED HYPOTHESIS IS A REVERT, not a pass. Without this a candidate can change anything,
    have nothing get worse, and be kept — which is p-hacking with extra steps: the gate would
    reward changes it never had a prediction for.
    """
    r = sh(["git", "-C", str(repo), "log", "-1", "--format=%B", sha])
    if r.returncode != 0:
        return None
    m = TRAILER_RE.search(r.stdout)
    if not m:
        return []
    return [x.strip().upper() for x in re.split(r"[,\s]+", m.group(1)) if x.strip()]


def changed_files(sha, repo):
    base = sh(["git", "-C", str(repo), "rev-parse", "%s^" % sha]).stdout.strip()
    if not base:
        return None
    r = sh(["git", "-C", str(repo), "diff", "--name-only", "%s..%s" % (base, sha)])
    if r.returncode != 0:
        return None
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def touches_tcb(files):
    return [f for f in files if any(f.startswith(t.rstrip("/")) or f == t for t in TCB)]


# ── Tier A: deterministic replay (zero model cost) ────────────────────────────────────────────
def score_tree(tree, state_dir):
    """Run the TRUSTED casebook runner against a materialised tree, in an isolated state dir.

    The runner comes from the frozen checkout; only the TREE differs between arms. That is what
    makes the two arms comparable — one variable, not two.
    """
    runner = Path(os.environ.get(TRUSTED_ENV, str(REPO))) / "bin" / "casebook-run.py"
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(tree)
    env["CORE_INSTANCE"] = str(tree)
    env["CORE_GATE_STATE"] = str(state_dir)      # trials never write production state
    r = subprocess.run([sys.executable, str(runner), "--json"], cwd=str(tree),
                       capture_output=True, text=True, env=env, timeout=1800)
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def tier_a(base_sha, cand_sha, targets):
    """Replay both arms through the frozen evaluator and diff the per-item verdicts."""
    work = Path(tempfile.mkdtemp(prefix="core-gate-arms-"))
    out = {"ran": False, "detail": [], "regression": None, "target_improved": None}
    try:
        try:
            base_tree = materialise(base_sha, work / "base")
            cand_tree = materialise(cand_sha, work / "cand")
        except RuntimeError as e:
            # Surfaced, never swallowed: ran stays False, so decide() returns REVERT with THIS as
            # the reason rather than an invented one about targets not improving.
            out["detail"].append(str(e))
            return out
        b = score_tree(base_tree, work / "state-base")
        c = score_tree(cand_tree, work / "state-cand")
        # THREE STATES, NOT ONE — and one of them is a fact about the CANDIDATE.
        #
        #   base None, cand None    the instrument is broken        UNGRADED
        #   base None, cand OK      the instrument is broken        UNGRADED
        #   base OK,   cand None    THE CANDIDATE BROKE ITS OWN MEASUREMENT   -> REVERT
        #
        # core-business found this on the UNGRADED split I made at its request, and the reason it
        # matters is not adversarial — there is no attacker here. IT IS A GRADIENT. This gate sits in
        # a loop where the thing being graded is also the thing proposing changes, so "candidates the
        # grader cannot read are not reverted" is a rule the loop can DISCOVER by ordinary search,
        # without anyone choosing it and without a single step looking wrong. A self-improving system
        # does not need malice to walk downhill; it needs a slope. The old everything-is-REVERT
        # behaviour was flat here, and the UNGRADED fix — which is right — tilted it.
        #
        # THE GENERAL RULE, which outlives this branch: whenever a verdict stops being auto-applied,
        # CHECK WHETHER THE CANDIDATE CAN CAUSE THAT VERDICT. UNGRADED was the first verdict in this
        # gate a candidate could induce and benefit from.
        #
        # Demonstrated rather than argued: same commit into two trees, eval/casebook-v1.json replaced
        # with "{ this is not json" in the candidate's tree only — base parses, cand does not,
        # ran=False, UNGRADED, --apply does nothing. Under the previous behaviour it was REVERTed.
        #
        # ACCEPTED COST, business's own caveat against its recommendation: a genuinely good candidate
        # can make the FROZEN runner crash for a legitimate reason — it adds a file an older runner
        # mis-parses — and gets REVERTed. That is correct: a change the trusted evaluator cannot read
        # is not yet gradeable, and "not yet" must not resolve to "kept". The cost lands on exactly
        # the changes that improve the evaluator, which is why --promote is a separate deliberate step.
        if b is None and c is None:
            out["detail"].append("NEITHER arm produced a parseable result — the instrument, not the"
                                 " candidate")
            return out
        if b is None:
            out["detail"].append("the BASE arm produced no parseable result — the instrument, not"
                                 " the candidate")
            return out
        if c is None:
            out["ran"] = True
            out["regression"] = ["the candidate's tree could not be scored by the trusted evaluator"
                                 " — it broke its own measurability, which is a finding about the"
                                 " candidate and not about the gate"]
            out["target_improved"] = []
            return out

        def items(d):
            return {r["id"]: r for r in (d.get("results") or d.get("items") or [])}

        bi, ci = items(b), items(c)
        out["ran"] = True
        regressions, improved = [], []
        DECIDABLE = ("PASS", "FAIL")

        def findings(rec):
            """The item's findings as a SET, so improvement is measured per-defect not per-status.

            THE RULE THIS REPLACES REJECTED EVERY REAL IMPROVEMENT. It required a declared target to
            move FAIL -> PASS, but the static items AGGREGATE: S3 had violations on several lines,
            so a candidate that genuinely fixed one left the item still FAIL and was reverted with
            "the change did not do the thing it claimed" — when it had done exactly that. Found by
            gating a correct candidate and disbelieving the verdict.

            Set comparison is stricter than a count: fixing one defect while introducing another
            inside the SAME item nets to zero on a count and is caught here.
            """
            return {str(f) for f in (rec.get("findings") or [])}

        # HOW MANY ITEMS COULD ACTUALLY BE COMPARED, reported so a clean run cannot imply more.
        #
        # core-business's condition, and it is right: "a green run must not imply thirteen items were
        # compared when five were." The transcript tier grades RECORDED trajectories, and an arm is a
        # materialised tree with no ~/.claude/projects — `git archive` cannot carry evidence that
        # lives outside the repo. So those items are UNDECIDABLE in BOTH arms and contribute nothing.
        #
        # PROVEN, not assumed, and by two experiments neither of which is sufficient alone: gutting
        # the CANDIDATE's own t11() changed no verdict (the frozen evaluator loads the frozen
        # predicates), and sabotaging the FROZEN copy moved three items. Together: the candidate's
        # copy is never read, and the predicates are load-bearing. The tier cannot discriminate arms
        # by any arrangement of evidence — the fence that makes the gate trustworthy is the same
        # fence that makes this tier a SNAPSHOT rather than a regression detector.
        _decidable_both = sum(
            1 for _i, _b in bi.items()
            if _b.get("status") in DECIDABLE and (ci.get(_i) or {}).get("status") in DECIDABLE)
        _dark = [i for i, b in bi.items()
                 if b.get("status") not in DECIDABLE
                 and (ci.get(i) or {}).get("status") not in DECIDABLE]
        out["comparable"] = {"decidable_in_both": _decidable_both, "total": len(bi),
                             "undecidable_in_both": sorted(_dark)}

        for iid, br in bi.items():
            cr = ci.get(iid)
            if cr is None:
                regressions.append("%s vanished from the candidate's results" % iid)
                continue
            bs, cs = br.get("status"), cr.get("status")
            bf, cf = findings(br), findings(cr)
            if iid in targets:
                if bs == "FAIL" and cs == "PASS":
                    improved.append("%s FAIL -> PASS" % iid)
                elif cf < bf:                       # strict subset: fixed some, introduced none
                    improved.append("%s %d -> %d findings" % (iid, len(bf), len(cf)))
                elif cf - bf:
                    regressions.append("%s is the declared target and GAINED findings: %s"
                                       % (iid, "; ".join(sorted(cf - bf))[:120]))
                continue
            # A NON-TARGET ITEM GOING DARK IS A REGRESSION, NEVER A NULL. Losing the ability to
            # measure something is how a change looks harmless while removing the evidence.
            if bs in DECIDABLE and cs not in DECIDABLE:
                regressions.append("%s went from %s to %s — the check went dark" % (iid, bs, cs))
            elif bs == "PASS" and cs == "FAIL":
                regressions.append("%s regressed PASS -> FAIL" % iid)
            elif cf - bf:
                # New findings on an item that was ALREADY failing is still a regression. Without
                # this, a candidate could pile defects onto anything already red and score clean.
                regressions.append("%s gained findings: %s" % (iid, "; ".join(sorted(cf - bf))[:120]))
        out["regression"] = regressions
        out["target_improved"] = improved
        out["detail"].append("%d items compared" % len(bi))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


# ── verdict ───────────────────────────────────────────────────────────────────────────────────
def decide(cand_sha, repo):
    v = {"candidate": cand_sha, "verdict": REVERT, "reasons": [], "trusted_sha": trusted_sha()}

    # ENTITLEMENT, NOT JUST AUTHENTICITY. core-business, bus #1015, and the distinction is the whole
    # point: the `.gate-frozen-at` stamp proves the child is running the tree the PARENT
    # materialised — authenticity. It says nothing about whether that tree should have been trusted
    # AT ALL. A candidate that points the trust root at ITSELF is still materialised as its own
    # frozen evaluator, and the stamp agrees with it perfectly.
    #
    # It found a third route needing no tampering whatsoever: `--promote <candidate>` then grade it.
    # Promotion is the SANCTIONED way to move the trust root, it skips the re-exec by design (it must,
    # or the tool could never bootstrap), and nothing in pretooluse-guard.sh matches it. One
    # normal-sounding maintenance command and the candidate is its own grader, with nothing tampered
    # and the gate still printing verdicts.
    #
    # ONE CHECK COVERS BOTH: the trust root must be a STRICT ANCESTOR of the candidate.
    #
    #     trusted == candidate                     refuse   (self-grading, however it was reached)
    #     promoted to the candidate, then graded   refuse   (same state, sanctioned route)
    #     trusted is an older commit               allow    (the honest case)
    #     promote to HEAD, then grade HEAD+1       allow    (the legitimate promote workflow)
    #     no ancestry at all                       refuse   (undecidable, and undecidable reverts)
    #
    # The last two rows are why this is a fix and not a wall: a check that also breaks the
    # legitimate workflow is not a fix, and business verified those rows specifically before
    # proposing it.
    # ASKED IN THE SOURCE REPO, NOT `repo`, AND IT FAILS CLOSED. The first version of this check
    # used `repo` and silently did nothing: after the re-exec an arm is a `git archive` extraction,
    # NOT a git repository, so `git -C <arm> rev-parse` returns empty, and an `if _same and _cand`
    # guard skipped the whole check. It ran, printed nothing, and let the self-grading case through
    # — a guard that fails OPEN, written into the fix for a trust-root bypass, and caught only
    # because the refusal I expected did not appear in the output.
    #
    # source_repo() is a real repository in both the parent and the child. And an unresolvable SHA
    # is now UNDECIDABLE rather than ignorable, which this file's own doctrine routes to REVERT.
    _t = trusted_sha()
    if _t:
        _src = str(source_repo())
        _same = sh(["git", "-C", _src, "rev-parse", "--verify", "%s^{commit}" % _t]).stdout.strip()
        _cand = sh(["git", "-C", _src, "rev-parse", "--verify", "%s^{commit}" % cand_sha]).stdout.strip()
        if not _same or not _cand:
            v["reasons"].append(
                "cannot resolve the trust root (%s) or the candidate (%s) as commits in %s — "
                "entitlement is UNDECIDABLE, so REVERT rather than grade on an unchecked root."
                % (_t[:12], str(cand_sha)[:12], _src))
            return v
        if _same == _cand:
            v["reasons"].append(
                "the trust root IS the candidate (%s) — a commit cannot be its own grader. "
                "Reached by pointing the root at the candidate, or by --promote-then-grade; both "
                "look normal and neither is." % _same[:12])
            return v
        anc = sh(["git", "-C", _src, "merge-base", "--is-ancestor", _t, cand_sha])
        if anc.returncode != 0:
            v["reasons"].append(
                "the trust root %s is not an ancestor of the candidate %s — no shared history means "
                "no basis for trusting that evaluator over this change. UNDECIDABLE, so REVERT."
                % (_t[:12], cand_sha[:12]))
            return v

    files = changed_files(cand_sha, repo)
    if files is None:
        v["reasons"].append("cannot read the candidate diff — UNDECIDABLE, so REVERT")
        return v

    tainted = touches_tcb(files)
    if tainted:
        # NOT a refusal to act. The frozen evaluator grades it; we only record that the trust root
        # cannot advance to it without explicit promotion.
        v["reasons"].append("candidate touches evaluator code: %s" % ", ".join(tainted))
        v["reasons"].append("graded by the FROZEN evaluator at %s; --promote is a separate,"
                            " deliberate step" % (trusted_sha() or "?")[:12])

    targets = declared_targets(cand_sha, repo)
    if targets is None:
        v["reasons"].append("cannot read the candidate commit message — UNDECIDABLE, so REVERT")
        return v
    if not targets:
        v["reasons"].append("no `Casebook-Target:` trailer — a change with no declared hypothesis"
                            " is never silently KEPT")
        return v
    v["targets"] = targets

    base = sh(["git", "-C", str(repo), "rev-parse", "%s^" % cand_sha]).stdout.strip()

    # ROUTE BY WHAT THE CANDIDATE CHANGED. Deterministic harness code -> Tier A (replay, $0).
    # Steering text (rules, CLAUDE.md, agent specs) changes what the MODEL does and is invisible to
    # replay, so it needs Tier B (paired counterfactual execution). A candidate that is BOTH gets
    # Tier A for the regression check AND Tier B for the steering targets — a steering change can
    # still break a deterministic item.
    # gate_tier_b comes from the FROZEN checkout, beside this running copy — never the candidate's.
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gate_tier_b as tb
    steering_only = tb.is_steering_only(files)

    a = tier_a(base, cand_sha, set(targets))
    v["tier_a"] = a
    if not a["ran"]:
        # NOT A REVERT. Nothing was measured, so nothing can be said about the candidate.
        v["verdict"] = UNGRADED
        v["reasons"] += a["detail"] or ["tier A did not complete"]
        v["reasons"].append("UNGRADED — the candidate was never measured. This is a message to the"
                            " GATE's maintainer, not the candidate's author. The candidate is still"
                            " not kept.")
        return v
    if a["regression"]:
        v["reasons"] += ["REGRESSION: " + r for r in a["regression"]]
        return v

    if steering_only:
        # Replay cannot judge a prompt-text change: the two arms differ only in a .md file, so
        # every deterministic item is IDENTICAL between them and target_improved is always empty.
        # That is not "no improvement" — it is the wrong instrument. Hand the targets to Tier B.
        b = run_tier_b(base, cand_sha, targets, repo)
        v["tier_b"] = b
        if b.get("skipped"):
            v["reasons"].append("tier B unavailable (%s) — UNDECIDABLE for a steering change,"
                                " so REVERT" % b["skipped"])
            return v
        improved, detail = b["improved"], b["detail"]
        v["reasons"] += detail
        if not improved:
            return v
        v["verdict"] = KEEP
        return v

    if not a["target_improved"]:
        v["reasons"].append("no declared target improved; the change did not do the thing it"
                            " claimed")
        return v

    v["verdict"] = KEEP
    v["reasons"].append("targets improved: %s" % ", ".join(a["target_improved"]))
    v["reasons"].append("no regression on any other decidable item")
    return v


def pmod_peek(frozen):
    """The predicate module from the FROZEN checkout — the scorer is never the candidate's copy."""
    import importlib.util
    key = "casebook_predicates_frozen"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        key, str(Path(frozen) / "bin" / "casebook_predicates.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def run_tier_b(base_sha, cand_sha, targets, repo):
    """Paired counterfactual execution for steering targets. Absent claude -> skipped, not passed."""
    import gate_tier_b as tb
    import shutil
    if shutil.which("claude") is None:
        return {"skipped": "the `claude` CLI is not on PATH"}
    trials = int(os.environ.get("CORE_GATE_TRIALS", str(tb.DEFAULT_TRIALS)))

    frozen = Path(os.environ.get(TRUSTED_ENV, str(REPO)))
    try:
        items = json.loads((frozen / "eval" / "casebook-v1.json").read_text())
        items = items.get("items", items) if isinstance(items, dict) else items
    except Exception as e:
        return {"skipped": "cannot read the casebook: %s" % e}
    probes, unscoreable = tb.probes_for(items, set(targets), pmod_peek(frozen))
    if unscoreable and not probes:
        # Named precisely: this is not "tier B failed", it is "these targets are the wrong tier".
        # A steering change targeting a STATIC item (e.g. "no doc claims a retired hook enforces")
        # IS deterministically checkable — the static check reads the doc text — so Tier A judges
        # it and Tier B has no business spending model calls on it.
        return {"skipped": "targets %s have no trajectory predicate (static/other tier) — "
                           "Tier A judges those" % ", ".join(sorted(unscoreable))}
    if not probes:
        return {"skipped": "no probe_prompt/minted_from for the declared targets"}

    pmod = pmod_peek(frozen)

    work = Path(tempfile.mkdtemp(prefix="core-gate-tierb-"))
    try:
        base_tree = materialise(base_sha, work / "base")
        cand_tree = materialise(cand_sha, work / "cand")
        results = tb.paired(base_tree, cand_tree, work, probes, pmod, trials)
        improved, detail = tb.verdict(results)
        return {"improved": improved, "detail": detail, "results": results, "trials": trials}
    except Exception as e:
        return {"skipped": "tier B execution failed: %s" % e}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def apply_revert(sha, repo):
    """A revert COMMIT on the candidate's own branch. Never a reset, never a force-push.

    An auto-reverting actuator must itself be trivially reversible, because it will be wrong
    sometimes — undecidable-defaults-to-revert guarantees that.
    """
    r = sh(["git", "-C", str(repo), "revert", "--no-edit", sha], timeout=180)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def main():
    reexec_from_trusted(sys.argv)                # never returns unless already trusted

    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate")
    ap.add_argument("--promote")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    repo = source_repo()

    if a.status:
        print("trusted evaluator SHA : %s" % (trusted_sha() or "NONE — every verdict is REVERT"))
        print("executing from        : %s" % os.environ.get(TRUSTED_ENV, "(untrusted!)"))
        print("grading repo          : %s" % repo)
        return 0

    if a.promote:
        # Deliberately NOT automatic. Moving the trust root is the one action this tool must never
        # take on its own — a gate that can promote its own evaluator has no trust root at all.
        full = sh(["git", "-C", str(repo), "rev-parse", a.promote]).stdout.strip()
        if not full:
            print("no such commit: %s" % a.promote)
            return 1
        (Path(repo) / "bin" / ".gate-trusted-sha").write_text(full + "\n")
        print("trust root moved to %s" % full[:12])
        print("This is a trust-root change: it must be reviewed like one.")
        return 0

    if not a.candidate:
        print("nothing to do — pass --candidate <sha>, --promote <sha>, or --status")
        return 1

    full = sh(["git", "-C", str(repo), "rev-parse", a.candidate]).stdout.strip()
    if not full:
        print("REVERT — no such commit: %s" % a.candidate)
        return 2

    v = decide(full, repo)

    # ONLY REVERT AUTO-APPLIES. UNGRADED deliberately does not: reverting a candidate the gate never
    # measured destroys possibly-good work on the strength of a broken instrument, which is precisely
    # what this gate would have done for however long it was inert — four defects, every one
    # surfacing as REVERT, and --apply would have acted on all of them.
    #
    # Not keeping is the safe action. Auto-DESTROYING is a different act and needs a measurement.
    if a.apply and v["verdict"] == REVERT:
        ok, detail = apply_revert(full, repo)
        v["reverted"] = ok
        v["revert_detail"] = detail

    if a.json:
        print(json.dumps(v, indent=2))
    else:
        print("\n  TRAJECTORY GATE — %s\n" % v["verdict"])
        print("  candidate : %s" % v["candidate"][:12])
        print("  trusted   : %s" % (v.get("trusted_sha") or "NONE")[:12])
        if v.get("targets"):
            print("  targets   : %s" % ", ".join(v["targets"]))
        # WHAT THE VERDICT IS ACTUALLY BASED ON. A clean run must not imply every item was compared.
        _c = (v.get("tier_a") or {}).get("comparable") or v.get("comparable")
        if _c:
            print("  compared  : %d of %d items decidable in BOTH arms"
                  % (_c["decidable_in_both"], _c["total"]))
            if _c.get("undecidable_in_both"):
                print("              UNDECIDABLE in both, contributing nothing: %s"
                      % ", ".join(_c["undecidable_in_both"]))
                print("              (transcript-tier items grade RECORDED trajectories; an arm is a")
                print("               materialised tree with no evidence directory, so they are a")
                print("               SNAPSHOT here, not a regression detector)")
        print()
        for r in v["reasons"]:
            print("   - %s" % r)
        if "reverted" in v:
            print("\n  revert %s" % ("applied" if v["reverted"] else "FAILED: " + v["revert_detail"]))
        print()
    return 0 if v["verdict"] == KEEP else 2


if __name__ == "__main__":
    sys.exit(main())
