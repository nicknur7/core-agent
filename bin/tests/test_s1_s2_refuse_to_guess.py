#!/usr/bin/env python3
"""
AUTHORED BY core-finance (T012, 2026-08-13). INSTALLED BY core-life.

finance cannot write bin/tests (pull-only Core, shared-write-guard): it authors and RUNS,
life reviews and installs. Author and runner being different seats is the evidence.
T012 — planted-defect regime for casebook static matchers S1 and S2 (the DELEGATING pair).

AUTHORED AND RUN BY core-finance, 2026-08-13, per the T012 routing (bus #1094 -> #1105 -> #1387):
finance authors and RUNS, life installs. `bin/` is baseline-shared and finance is a puller, so this
ships as a proposal.

    intended install path: bin/tests/test_s1_s2_refuse_to_guess.py

WHY THIS PAIR IS PROBED DIFFERENTLY FROM S3/S4/S5. Those match text. S1 and S2 match nothing — they
DELEGATE to a subprocess (`enforcement-audit.py --json`, `lint-doc-paths.py --json`) and translate
its answer. So "can the matcher match" is the wrong question for them. The property that decides
whether they are trustworthy is the one both docstrings insist on:

    AN UNREADABLE ANSWER IS UNDECIDABLE, NEVER CLEAN.

That is not an abstract principle here; it is the scar. S2's own comment records that it asked for
`data["broken"]`, a key lint-doc-paths.py has NEVER emitted, so the `.get()` default fired on every
run and **S2 was an always-clean check sitting in the scalar's numerator** for its entire existence.
A regime that only asked "does S2 return the broken paths" would have passed against that version,
because the version returned [] and [] is what a clean repo returns too.

So every assertion below drives a DELEGATE STUB that emits a controlled answer, and asserts which
answers are translated and which are REFUSED. The historical `{"broken": []}` shape is included by
name as a regression test: it must raise, not return clean.

THE OVER-REFUSAL HALF, asserted deliberately. A guard that refuses everything silently disables the
mechanism, so this file also asserts the cases that must NOT refuse:

  · a valid answer with zero findings returns [] rather than raising
  · S1 accepts a NONZERO exit code with parseable output, because enforcement-audit.py:191 is
    `return 1 if findings else 0` — nonzero is its normal "findings exist" signal. An S1 that
    treated rc != 0 as a crash would refuse on every run that found something.

That second point was checked before it was asserted. The asymmetry between the two matchers — S2
tests `rc not in (0, 1)`, S1 ignores the return code entirely — looks like a defect against S1's own
docstring ("a crashed auditor is UNDECIDABLE, never clean") and is not one: the only divergence is
that S1 would accept rc=2 with parseable stdout, and that producer exits only 0 or 1. Recorded here
so the next reader does not re-derive it and "fix" S1 into refusing on normal findings.

Read-only. Compiles casebook-run.py from source text (never importlib). REPO is redirected to a temp
tree holding only a stub delegate, restored in a finally, and asserted restored at the end. The real
enforcement-audit.py and lint-doc-paths.py are never executed.

Run: python3 tasks/si-verification/probes/authored_T012_casebook_s1_s2_regime.py
"""
import json
import sys
import tempfile
import types
from pathlib import Path


def _find_root(start: Path) -> Path:
    """Walk up for the directory that CONTAINS the runner, never a fixed depth (bus #1390)."""
    for cand in [start] + list(start.parents):
        if (cand / "bin" / "casebook-run.py").is_file():
            return cand
    raise SystemExit("SKIP - could not locate a Core root containing bin/casebook-run.py")


ROOT = _find_root(Path(__file__).resolve().parent)
RUNNER = ROOT / "bin" / "casebook-run.py"

STUB = "import sys\nsys.stdout.write(%r)\nsys.exit(%d)\n"

RAISES = "RuntimeError"

# (label, stdout payload, exit code, expected) — expected is RAISES or the returned list
S2_CASES = [
    ("valid answer, one broken citation",
     {"scanned": 10, "broken_total": 1, "files": {"a.md": [[3, "missing.py"]]}}, 0,
     ["a.md:3 -> missing.py"],
     "a real broken citation is not translated, so S2 reports clean while the producer found one"),
    ("valid answer, zero broken  (MUST NOT refuse)",
     {"scanned": 10, "broken_total": 0, "files": {}}, 0, [],
     "S2 refuses a legitimately clean answer, which disables the item as surely as always-clean did"),
    ("EMPTY stdout", "", 0, RAISES,
     "silence scores as a clean bill — the producer said nothing and S2 called it good"),
    ("unparseable output", "not json at all", 0, RAISES,
     "an unreadable answer scores clean"),
    ("HISTORICAL always-clean shape {'broken': []}", {"broken": []}, 0, RAISES,
     "THE REGRESSION: this is the exact shape S2 read for its whole existence, and returning [] "
     "here is indistinguishable from a clean repo. It must REFUSE, not guess"),
    ("producer count disagrees with extraction",
     {"scanned": 9, "broken_total": 5, "files": {"a.md": [[1, "x"]]}}, 0, RAISES,
     "the producer said 5 and 1 was extracted; reporting either is reporting a number nobody "
     "computed"),
    ("delegate exits 2", {"scanned": 1, "broken_total": 0, "files": {}}, 2, RAISES,
     "a failed delegate is translated as a clean result"),
]

S1_CASES = [
    ("valid answer, one unbacked claim",
     {"unbacked": [{"doc": "CLAUDE.md", "line": 9, "hook": "dead.py"}]}, 0,
     ["CLAUDE.md:9 claims 'dead.py' enforces — registered in nothing"],
     "a doc claiming a retired hook enforces is not reported"),
    ("valid answer, zero unbacked  (MUST NOT refuse)", {"unbacked": []}, 0, [],
     "S1 refuses a legitimately clean answer"),
    ("EMPTY stdout", "", 0, RAISES, "silence scores as a clean bill"),
    ("unparseable output", "boom", 0, RAISES, "an unreadable answer scores clean"),
    ("NONZERO exit with a valid answer  (MUST NOT refuse)", {"unbacked": []}, 1, [],
     "S1 now refuses on rc != 0 — but enforcement-audit.py:191 is `return 1 if findings else 0`, "
     "so nonzero is its NORMAL findings signal and refusing on it breaks S1 on every run that "
     "finds something"),
]


def load():
    m = types.ModuleType("casebook_run_s1s2_probe")
    m.__file__ = str(RUNNER)
    exec(compile(RUNNER.read_text(), str(RUNNER), "exec"), m.__dict__)
    return m


def drive(mod, fn_name, delegate, payload, rc, write_stub=True):
    """Point the matcher at a temp Core holding only a stub delegate. Restore unconditionally."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "bin").mkdir()
        if write_stub:
            (tmp / "bin" / delegate).write_text(STUB % (body, rc))
        saved = mod.REPO
        mod.REPO = tmp
        try:
            return ("OK", getattr(mod, fn_name)())
        except Exception as exc:
            return (type(exc).__name__, str(exc))
        finally:
            mod.REPO = saved


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== T012 — S1/S2: an unreadable answer must be UNDECIDABLE, never clean ===\n")
    if not RUNNER.is_file():
        print("  SKIP - bin/casebook-run.py absent")
        return 0
    mod = load()
    real_repo = mod.REPO

    check("CONTROL - both matchers are present on the runner",
          callable(getattr(mod, "s1_no_retired_hook_claimed_live", None))
          and callable(getattr(mod, "s2_cited_paths_resolve", None)),
          "a regime run against absent matchers passes vacuously")

    # CONTROL for the harness itself: if the stub never ran, every RAISES case below would pass
    # for the wrong reason (a missing delegate also raises).
    kind, val = drive(mod, "s2_cited_paths_resolve", "lint-doc-paths.py",
                      {"scanned": 1, "broken_total": 0, "files": {}}, 0)
    check("CONTROL - the stub delegate is actually executed (got %r)" % (val if kind == "OK" else kind),
          kind == "OK" and val == [],
          "the stub never ran, so every refusal assertion below would pass because the delegate "
          "was MISSING rather than because its answer was rejected")
    print()

    for name, fn, delegate, cases in (
            ("S2", "s2_cited_paths_resolve", "lint-doc-paths.py", S2_CASES),
            ("S1", "s1_no_retired_hook_claimed_live", "enforcement-audit.py", S1_CASES)):
        for label, payload, rc, expected, why in cases:
            kind, val = drive(mod, fn, delegate, payload, rc)
            if expected is RAISES:
                ok = kind == RAISES
                got = "%s(%s)" % (kind, str(val)[:60]) if kind != RAISES else "refused"
            else:
                ok = kind == "OK" and val == expected
                got = "%s %r" % (kind, val)
            check("%s %-48s %s" % (name, label, "REFUSES" if expected is RAISES else "translates"),
                  ok, "%s. got %s" % (why, got))
        print()

    # The delegate being absent must also refuse — S1 checks this explicitly.
    kind, val = drive(mod, "s1_no_retired_hook_claimed_live", "enforcement-audit.py",
                      {"unbacked": []}, 0, write_stub=False)
    check("S1 a MISSING delegate refuses rather than scoring clean", kind == RAISES,
          "a matcher whose auditor is not installed reports no violations, which is what an "
          "auditor finding none also reports. got %s %r" % (kind, val))

    check("SAFETY - REPO restored to the real tree", mod.REPO == real_repo,
          "the probe left the module pointed at a temp tree that no longer exists")

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
