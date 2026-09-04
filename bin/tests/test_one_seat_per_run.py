#!/usr/bin/env python3
"""ONE RUN MEASURES ONE CORE — proven by running the real files, not by reading them.

core-business, #914: `CORE_INSTANCE=<business> python3 <life's tree>/bin/casebook-run.py` reported
ERROR on T11/T12/T13 from its seat. Reproduced here, the same command does something worse — it does
not fail at all:

    runner REPO        = …/core-business      graded business's files
    predicates TRANSCR = …-core-life          measured LIFE's behaviour
    SAME CORE?         = False                exit 0

An error is recoverable. A confident blended number is what gets acted on, and it is the invocation
the runner's own docstring calls preferred — the form a peer uses to review this Core's work.

WHAT THE FIX WAS, because it is the part worth keeping: not a patch to either resolver. Both were
individually correct. The defect was that there were THREE of them for one question, plus two
different path-slug implementations that agree only on paths without punctuation. business's
diagnosis names it — fixing "hardcoded" by writing a second resolver is the two-implementations
defect, so the answer is one module both sides import, not a third careful copy.

Run: python3 bin/tests/test_one_seat_per_run.py
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
BIN = ROOT / "bin"

PROBE = r"""
import importlib.util, sys
def load(n, p):
    s = importlib.util.spec_from_file_location(n, p)
    m = importlib.util.module_from_spec(s); sys.modules[n] = m; s.loader.exec_module(m); return m
cr = load("cr", %r)
cp = load("casebook_predicates", %r)
print("RUNNER=%%s" %% cr.REPO)
print("PRED=%%s" %% cp.SEAT)
print("TRANSCRIPTS=%%s" %% cp.TRANSCRIPTS)
"""


def seats(**env) -> dict:
    e = {k: v for k, v in os.environ.items() if k not in ("CORE_INSTANCE", "CLAUDE_PROJECT_DIR")}
    e.update({k: str(v) for k, v in env.items()})
    r = subprocess.run([sys.executable, "-c",
                        PROBE % (str(BIN / "casebook-run.py"), str(BIN / "casebook_predicates.py"))],
                       cwd=str(ROOT), env=e, text=True, capture_output=True, timeout=90)
    out = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    out["_err"] = (r.stderr or "")[-300:]
    return out


def main() -> int:
    p = f = 0
    abstain = 0
    print("=== one seat per run ===\n")

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("--- the author's own invocation, which always worked ---")
    a = seats()
    check("runner and predicates resolve the same Core with no env set",
          a.get("RUNNER") and a.get("RUNNER") == a.get("PRED"),
          "runner=%s pred=%s %s" % (a.get("RUNNER"), a.get("PRED"), a.get("_err")))

    print("\n--- the peer-review invocation, which silently blended two Cores ---")
    peers = [d for d in ROOT.parent.glob("core-*")
             if d.is_dir() and (d / ".claude" / "identity.json").is_file()
             and d.resolve() != ROOT.resolve()]
    if not peers:
        # NOT a pass, and not a counted FAIL either — the property is about cross-Core invocation
        # and cannot be shown without a second Core on disk, which a standalone clone (this repo's
        # own public tree, a fork, a CI checkout) legitimately never has. Routed through the
        # `abstain` counter so main() can report the run-all.sh ABSTAIN contract (rc=2 +
        # UNDECIDABLE) instead of the FAIL this used to collapse into via `f += 1` — the same text
        # was already printed, but the exit code never matched what it said.
        print("  UNDECIDABLE  no peer Core on this machine — the cross-seat case cannot be exercised")
        abstain += 1
    else:
        peer = peers[0]
        b = seats(CORE_INSTANCE=peer)
        check("CORE_INSTANCE moves BOTH sides, not just the runner",
              b.get("RUNNER") == str(peer.resolve()) and b.get("PRED") == str(peer.resolve()),
              "runner=%s pred=%s" % (b.get("RUNNER"), b.get("PRED")))
        check("the transcript directory follows the seat, not the file's location",
              b.get("TRANSCRIPTS", "").endswith(peer.name),
              "transcripts=%s want …%s" % (b.get("TRANSCRIPTS"), peer.name))

        print("\n--- dose: the answer must DEPEND on the input, not be a stable coincidence ---")
        # Two runs of identical code differing only in the env var. If both name the same Core the
        # agreement above proves nothing — it would be satisfied by a resolver that ignores its
        # input entirely, which is exactly how a stuck instrument passes its own test.
        check("no-env and peer-env resolve to DIFFERENT Cores",
              a.get("RUNNER") != b.get("RUNNER"),
              "both resolved to %s — agreement is a constant, not a result" % a.get("RUNNER"))

    print("\n--- the backstop refuses a blend rather than reporting one ---")
    spec = importlib.util.spec_from_file_location("core_seat", str(BIN / "core_seat.py"))
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    try:
        cs.assert_same_seat(ROOT, ROOT)
        same_ok = True
    except RuntimeError:
        same_ok = False
    try:
        cs.assert_same_seat(ROOT, ROOT.parent / "core-does-not-exist")
        diff_refused = False
    except RuntimeError:
        diff_refused = True
    check("identical seats pass and differing seats RAISE (both directions)",
          same_ok and diff_refused,
          "same_ok=%s diff_refused=%s" % (same_ok, diff_refused))

    print("\n--- the slug is ONE implementation, and it survives punctuation ---")
    # The two former implementations agreed on every path in use today and diverged on any path
    # containing a dot or underscore — a directory that does not exist, which reads as an empty
    # history rather than a bad path.
    got = cs.transcripts_dir(Path("/Users/x/AI Projects/core_life.d")).name
    check("non-alphanumeric characters all collapse to dashes",
          got == "-Users-x-AI-Projects-core-life-d", "got %s" % got)
    # NOT a code property — a fixture. `~/.claude/projects/<slug>/` is created by the Claude Code
    # CLI itself the first time an interactive session actually runs with this exact path as its
    # cwd; nothing in this repo provisions it. A standalone clone this test-fix pass is exercising
    # via subprocess/import (never a real `claude` session against ROOT) legitimately has no such
    # directory yet — same "transcript history" shape as test_slug_agreement.py and the sibling
    # check in test_objective_liveness.py. Abstain rather than fail: the slug FORMULA is already
    # proven correct by the punctuation check above; only its live existence is unverifiable here.
    real_transcripts = cs.transcripts_dir(ROOT)
    if real_transcripts.is_dir():
        check("the real Core's slug still resolves to a directory that exists",
              True)
    else:
        print("  UNDECIDABLE  the real Core's slug still resolves to a directory that exists\n"
              "          no dir at %s — no live claude session has run against this exact path "
              "yet; the slug FORMULA is proven above, only its live existence is unverifiable "
              "here" % real_transcripts)
        abstain += 1

    print("\n=== Results: %d passed, %d failed%s ===" % (
          p, f, ", %d undecidable" % abstain if abstain else ""))
    if f:
        return 1
    if abstain:
        # rc=2 + UNDECIDABLE, the run-all.sh ABSTAIN contract (test_wilson_ci_known_answers.py is
        # the precedent this copies): a real FAIL never launders into this, only a precondition
        # (a peer Core on disk, a live claude session's transcript history) this seat cannot supply.
        print("\n  UNDECIDABLE  %d check(s) could not run on this seat (no peer Core on disk, "
              "and/or no live claude session has run against this exact path). Not a pass." % abstain)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
