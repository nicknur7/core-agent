#!/usr/bin/env python3
"""A detector that has quietly stopped detecting must fail the suite, not just the report.

WHY THIS EXISTS (D4 of the enforcement-layer plan, 2026-08-06)
--------------------------------------------------------------
The old objective was "minimise ENFORCED blocks subject to shadow detections not rising." Read as an
optimiser would: both terms are minimised by doing nothing. A system that installed 1,070 artifacts
and promoted 0 was not failing that objective — it was scoring perfectly. And its success signal was
indistinguishable from its total-failure signal, because "0 blocks" is what you see when everything
works AND what you see when every detector is dead.

`bin/si-objective.py` replaces it, and the load-bearing part is the LIVENESS PROBE: feed the shipped
hook a reply containing a known violation and check it comes back out. Without that, every zero in
the report is ambiguous.

But a probe that only runs when someone chooses to read a report is not enforcement — it is another
thing to remember. So it runs here, on every `run-all.sh`, and a detector that has stopped detecting
turns the suite red. That is the difference between measuring and binding, which is the distinction
this entire subsystem keeps failing on.

IT ALREADY EARNED ITS KEEP. On its first real run `deliverable_format` failed: the pattern required
the noun to sit immediately after "the", so the most natural phrasing of the mistake — "here's the
FULL table" — walked past it. The detector had never fired, and its 0 was being read as clean
behaviour. The probe found in one run what months of the old objective could not express.

It also caught its own harness bug, which is worth recording because the failure mode is nasty: the
first version tagged probes `probe_<kind>`, the hook stores `turn` as `turn_id[:12]`, so every tag was
silently truncated and all seven probes reported "produced no row" while the hook was working
perfectly. A liveness probe that fails closed on its own bug condemns healthy detectors and invites
you to "fix" code that was never broken. Hence the assertion below that the probe can distinguish
detected-and-unsourced from every other outcome.

Run: python3 bin/tests/test_objective_liveness.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PASS = 0
FAIL: list[str] = []
ABSTAIN: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> int:
    obj = load(REPO / "bin" / "si-objective.py")

    # ── THE POINT OF THE FILE: every shipped detector still detects ────────────
    live = obj.probe_liveness()
    check("every detector in reply-observer.py has a probe "
          "(an unprobed detector's zero is unverifiable)",
          set(live) == set(obj.PROBES) and len(obj.PROBES) == len(_detector_names()),
          f"probes={sorted(obj.PROBES)} detectors={sorted(_detector_names())}")
    for kind, r in sorted(live.items()):
        check(f"detector still live: {kind}", bool(r.get("ok")), r.get("why", ""))

    # ── the probe must be able to FAIL, and must drive the real hook ──────────
    src = (REPO / "bin" / "si-objective.py").read_text()
    fn = src.split("def probe_liveness", 1)[-1].split("\ndef ", 1)[0]
    check("the probe runs reply-observer.py as a SUBPROCESS, not by importing its regexes "
          "(an imported regex passes while the hook is unregistered or crashing)",
          "subprocess.run" in fn and "reply-observer" in src)
    check("...and reads the row the hook WROTE, rather than the hook's return value",
          "reply-observations.jsonl" in fn)
    check("a hit marked SOURCED against an empty transcript is treated as a FAILURE "
          "(over-permissive sourcing hides real violations just as effectively)",
          'hit.get("sourced")' in fn)

    # Prove the probe can distinguish outcomes: a detector name with no matching text must fail.
    saved = dict(obj.PROBES)
    try:
        obj.PROBES.clear()
        obj.PROBES["duration_claim"] = "this text contains no duration claim whatsoever"
        bad = obj.probe_liveness()
        check("the probe FAILS a detector whose probe text does not trigger it "
              "(it can distinguish outcomes, so a pass means something)",
              bad.get("duration_claim", {}).get("ok") is False,
              str(bad.get("duration_claim")))
    finally:
        obj.PROBES.clear()
        obj.PROBES.update(saved)

    # ── the honesty guards ────────────────────────────────────────────────────
    v = obj.violations(7)
    check("violations() reports whether the PRIOR window was actually observed",
          "prior_covered" in v)
    live_since = v.get("observer_live_since")
    if live_since:
        expected = live_since <= int(time.time()) - 14 * 86400
        check("prior_covered agrees with when the observer actually started recording",
              v["prior_covered"] == expected,
              f"live_since={live_since} covered={v['prior_covered']}")
    rep = src.split("def main", 1)[-1]
    check("an uncovered prior window prints UNKNOWN, never 0.00 "
          "(reporting it as 0 makes every real violation read as a regression)",
          'if v["prior_covered"] else None' in src and "NO BASELINE" in rep)
    check("main() exits NONZERO when any detector fails liveness "
          "(so a caller cannot read a broken report as a clean one)",
          "return 1" in rep and "UNSCOREABLE" in rep)
    check("--no-probe still labels every zero UNVERIFIED rather than reporting success",
          "UNVERIFIED" in rep and "--no-probe" in src)

    # ── cost terms exist and are honest about their estimate ──────────────────
    c = obj.costs(7)
    for k in ("enforcement_fires", "injections", "injected_tokens_est", "unpriced_injections"):
        check(f"cost term present: {k}", k in c)
    check("an injection with no payload on record is counted as an UNDERCOUNT, not as zero cost",
          "unpriced_injections" in c and "undercount" in src)
    # `.claude/state/*` is gitignored (blank-scaffolded on every fresh Core, .gitignore:32-33) —
    # active.json is runtime state the friction loop writes only after it has actually installed
    # and fired an artifact, so a fresh clone or a Core still on its first sessions legitimately
    # has none yet. Requiring `.is_file()` here fails every such seat forever, checking a fixture
    # rather than the property the docstring names: the PATH the code resolves against must be
    # the real subdirectory (not the old buggy 'friction/'), and a missing file must degrade to an
    # honest undercount rather than silently pricing everything at 0.
    active_path = REPO / ".claude" / "state" / "friction-artifacts" / "active.json"
    check("injected tokens resolve against the real active.json path "
          "(the first version pointed at friction/ and silently priced everything at 0)",
          "friction-artifacts" in src)
    c_probe = obj.costs(7)
    if not active_path.is_file():
        check("...and costs() degrades honestly (no crash, an undercount not a silent 0) when "
              "active.json is absent, as it legitimately is on a fresh seat",
              c_probe.get("unpriced_injections", -1) == c_probe.get("injections", -2))

    # ── fork/peer safety: this file ships to 4 peer Cores ─────────────────────
    # Inspect the ASSIGNMENT, not the whole file. The first version grepped the source for the
    # literal core-life path and failed on the comment that EXPLAINS the old hardcoded bug — a test
    # that forbids naming a defect in prose is a test that discourages documenting it.
    # ASSERT THE PROPERTY, NOT THE SPELLING. This used to require the literal token `re.sub` in the
    # assignment, which passed only while the slug was inlined here — so consolidating the THIRD
    # copy of that slug into bin/core_seat.py (core-business #914) turned this red without anything
    # regressing. A test keyed to one implementation of a property blocks the correct fix to it.
    #
    # The property is: point it at a different Core and it must answer differently. That is
    # unsatisfiable by any hardcoded path, which is the whole thing being defended.
    assign = src.split("TRANSCRIPTS = ", 1)[-1].split("\n\n", 1)[0]
    other = obj.transcripts_dir(Path("/Users/x/AI Projects/core-somewhere-else"))
    check("the transcript directory is DERIVED from the repo path, not hardcoded to core-life "
          "(a literal path makes this tool silently dead on all four peers)",
          str(other).endswith("-core-somewhere-else") and str(other) != str(obj.TRANSCRIPTS)
          and str(Path(__file__).resolve().parents[2]).replace("/", "-") not in assign,
          "%s | assign=%s" % (other.name, assign.strip()[:60]))
    # NOT A CODE PROPERTY — A FIXTURE, same shape as bin/tests/test_slug_agreement.py and
    # test_one_seat_per_run.py. `~/.claude/projects/<slug>/` is created by the Claude Code CLI
    # itself, the first time an interactive session actually runs with this exact path as its cwd;
    # nothing in this repo provisions it. A standalone clone exercised via subprocess/import
    # (never a real `claude` session against this path) legitimately has no such directory yet.
    # The DERIVATION property just above is already proven correct against a different Core's
    # path; only this directory's live existence is unverifiable here. Abstain, don't fail.
    if obj.TRANSCRIPTS.is_dir():
        check("...and it resolves to a real directory on THIS Core", True)
    else:
        ABSTAIN.append("...and it resolves to a real directory on THIS Core")
        print(f"  UNDEC  ...and it resolves to a real directory on THIS Core\n"
              f"          no dir at {obj.TRANSCRIPTS} — no live claude session has run against "
              f"this exact path yet; the derivation is proven correct above, only its live "
              f"existence is unverifiable here")
    check("reply_count returns 0 rather than raising when the directory is absent",
          _no_dir_returns_zero(obj))

    # A zero denominator with a nonzero numerator must not read as "flat" — that is the one direction
    # nothing here may fail in. Reachable via an unreadable transcript dir or a clock skew.
    check("a zero denominator with violations on record is reported as DENOMINATOR BROKEN, not flat",
          "DENOMINATOR BROKEN" in src and 'not v["replies_current"]' in src)
    check("...and that state exits nonzero, so a caller cannot read it as clean",
          src.split("DENOMINATOR BROKEN —", 1)[-1].split("if broken:", 1)[0].count("return 1") == 1)

    # ── the pre-empt proposal: test the branch that FIRES, not just the quiet one ──
    #
    # On today's data propose_preempts() correctly returns []. An escalation path that has only ever
    # been exercised in its empty case is indistinguishable from one that cannot fire at all, and this
    # repo has already shipped two of those (a tune branch that could not match any generated artifact
    # id, and a proof-window reader filtering on one action while the writer wrote another). So drive
    # it with synthetic counts that cross the threshold.
    live_all_ok = {k: {"ok": True} for k in obj.PROBES}
    fake = {
        "prior_covered": True, "observer_live_since": 0,
        "replies_current": 100, "replies_prior": 100,
        "current": {"state_claim": {"total": 9, "unsourced": 9},
                    "cross_core_claim": {"total": 9, "unsourced": 9},
                    "duration_claim": {"total": 1, "unsourced": 1}},
        "prior": {},
    }
    props = obj.propose_preempts(fake, live_all_ok, write=False)
    kinds = {p["case_id"] for p in props}
    check("a class over the threshold with NO possible supply earns a pre-empt proposal",
          "observed_state_claim" in kinds, str(sorted(kinds)))
    check("a class over the threshold that SUPPLY can reach does NOT earn one "
          "(supply is cheaper than a per-turn nudge, and cross_core_claim was in fact closed by it)",
          "observed_cross_core_claim" not in kinds, str(sorted(kinds)))
    check("a class under the absolute floor earns nothing, however high its rate",
          "observed_duration_claim" not in kinds)

    nobase = dict(fake, prior_covered=False)
    check("no proposal at all without a covered baseline — one window is not a trend",
          obj.propose_preempts(nobase, live_all_ok, write=False) == [])
    dead = {k: {"ok": False, "why": "x"} for k in obj.PROBES}
    check("a FAILING detector cannot justify building anything off its counts",
          obj.propose_preempts(fake, dead, write=False) == [])

    sp = next(p for p in props if p["case_id"] == "observed_state_claim")
    check("the proposal names PostToolBatch as eligible and refuses Stop and MessageDisplay",
          "PostToolBatch" in sp["eligible_events"]
          and set(sp["ineligible_events"]) >= {"Stop", "MessageDisplay"})
    check("...and says WHY MessageDisplay cannot enforce, which is the whole reason it is safe "
          "to observe from",
          "cannot block" in sp["ineligible_events"]["MessageDisplay"])
    check("the proposal must be hand-written, not minted by the loop",
          sp["must_be_handwritten"] is True)
    check("it lands in the SAME queue D3 writes, not a second one",
          obj.QUEUE.name == "oracle-request-queue.json")

    # write=False must not touch the queue — the default path in --json and in tests.
    before = obj.QUEUE.read_bytes() if obj.QUEUE.is_file() else None
    obj.propose_preempts(fake, live_all_ok, write=False)
    after = obj.QUEUE.read_bytes() if obj.QUEUE.is_file() else None
    check("write=False leaves the queue byte-identical", before == after)

    # ── the replaced objective must not still be documented as current ────────
    disp = (REPO / "scheduling" / "claude-si" / "friction_dispatch.py").read_text()
    if "minimise ENFORCED blocks" in disp:
        check("friction_dispatch no longer states the degenerate objective as the current one",
              "si-objective.py" in disp or "SUPERSEDED" in disp,
              "the old objective text is still there with nothing marking it replaced")

    print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed"
          + (f", {len(ABSTAIN)} undecidable" if ABSTAIN else "") + " ===")
    if FAIL:
        return 1
    if ABSTAIN:
        # rc=2 + UNDECIDABLE, the run-all.sh ABSTAIN contract (test_wilson_ci_known_answers.py is
        # the precedent this copies): every other check in this file ran; only the live-session-
        # directory fixture is missing on a seat no real `claude` session has ever run against.
        print(f"\n  UNDECIDABLE  {len(ABSTAIN)} check(s) could not run on this seat (no live claude "
              f"session has run against this exact path). Not a pass.")
        return 2
    return 0


def _no_dir_returns_zero(obj) -> bool:
    """reply_count against a path that does not exist must be 0, not an exception — a peer Core with
    no transcripts yet must get a quiet zero rather than a crashed report."""
    saved = obj.TRANSCRIPTS
    try:
        obj.TRANSCRIPTS = Path("/nonexistent-transcripts-dir-for-test")
        return obj.reply_count(0) == 0
    except Exception:
        return False
    finally:
        obj.TRANSCRIPTS = saved


def _detector_names() -> set:
    """Read the shipped DETECTORS tuple, so adding a detector without a probe fails this suite."""
    m = load(REPO / ".claude" / "hooks" / "reply-observer.py")
    return {n for n, _ in m.DETECTORS}


if __name__ == "__main__":
    sys.exit(main())
