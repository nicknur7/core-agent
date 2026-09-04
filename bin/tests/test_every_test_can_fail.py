#!/usr/bin/env python3
"""EVERY TEST MUST HAVE A REACHABLE NONZERO EXIT, or it cannot report the thing it checks.

Found twice in one hour on 2026-08-10, both in files whose entire job was checking:

    test_org_scoping_lint.py    printed STILL FLAGGING / DETECTOR IS BLIND / STILL CRIES WOLF /
                                MANGLED and fell off the end of the module. Four carefully worded
                                failure verdicts, every one exiting 0.
    test_token_hash_roundtrip.py  computed `bad`, printed "%d form(s) still diverge", returned 0
                                regardless — while guarding the agreement between how a token is
                                HASHED when minted and when redeemed.

Both are this session's own lesson turned on its author: **reporting a value is not checking it.**
The number was computed, printed, and never branched on. A hand sweep found these two; a hand sweep
finds today's instances and nothing after, which is why this is a file rather than a shell one-liner.

WHY THE SUITE RUNNER CANNOT CATCH THIS. `run-all.sh` classifies MUTE (exit 0, no evidence any check
ran) — but these files print "ok" on their passing lines, so they satisfy the evidence test and exit
0 legitimately. A test that lies in the PASSING direction is invisible to any runner that trusts
exit codes. The exit code has to be real, and that is a property of the source, not of the run.

ON THE MATCHER, WHICH WAS WRONG THE FIRST TIME. Its first version missed
`sys.exit(0 if before == after else 1)` and reported test_fence_test_is_safe.py as defective —
the seventh matcher in one day that could not match what it was written for. Every accepted form is
therefore pinned below as a must-fire control, and a file using a form not listed is reported rather
than assumed fine.

HONEST LIMIT — WHAT THIS DOES NOT PROVE, stated so nobody reads more off a green run than is there.

**This finds the TEXT of a failure exit, not its REACHABILITY.** core-business named the distinction
(bus #983) while checking its own artifacts for this class: statically every one of them carried a
`return 1`, and the count proved nothing, because the question is which CLEAN rows can ever STOP
being clean. A `sys.exit(1)` inside a branch no input can reach satisfies this file completely.

Its stronger form is the same finding stated as a property: **a detector that has never fired is
indistinguishable from one that cannot.** By that standard the honest status of this tree is that a
minority of its tests have been OBSERVED going red — the ones dosed deliberately: the trajectory
gate's matched pair, the steering ratchet under a planted regression, run-all.sh against planted
CRASH/MUTE/SKIP fixtures, observation-probe against planted logs, the two exit-code repairs of
2026-08-10, this file's own print-only control. The rest carry a reachable-looking exit and no
demonstration.

So: passing here means "a failure path exists and the matcher recognises its form". It does not mean
"this test can fail on a real defect". Closing that gap needs a dose per test, not a better regex,
and pretending otherwise would make this file the thing it was written to catch.

Run: python3 bin/tests/test_every_test_can_fail.py
"""
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
TESTS = ROOT / "bin" / "tests"
SELF = Path(__file__).name

# Every form actually in use in this tree. Each is exercised as a control below, so narrowing this
# set silently is not possible — a removed pattern turns its control red.
FORMS = {
    "sys.exit(1)":              re.compile(r"sys\.exit\(\s*1\s*\)"),
    "sys.exit(2)":              re.compile(r"sys\.exit\(\s*2\s*\)"),
    "sys.exit(main())":         re.compile(r"sys\.exit\(\s*main\(\s*\)\s*\)"),
    "sys.exit(<conditional>)":  re.compile(r"sys\.exit\(\s*[^)]*\bif\b[^)]*\)"),
    "sys.exit(1 if ...)":       re.compile(r"sys\.exit\(\s*1\s+if\b"),
    "sys.exit(<expr>)":         re.compile(r"sys\.exit\(\s*[A-Za-z_][\w.]*\s*\)"),
    "exit(1)":                  re.compile(r"(?<!sys\.)\bexit\(\s*1\s*\)"),
    "raise SystemExit":         re.compile(r"raise\s+SystemExit"),
    "bare assert":              re.compile(r"^\s*assert\s+", re.M),
}


def failure_forms(src):
    return sorted(name for name, rx in FORMS.items() if rx.search(src))


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== every test file must be able to exit nonzero ===\n")

    # EVERY TEST DIRECTORY, NOT JUST THIS ONE. The first version globbed bin/tests only, which left
    # scheduling/claude-si/tests entirely unexamined — six files this check silently claimed to
    # cover. Same overstatement the shell-test block below was added to prevent, one axis over: a
    # check that reports "every test file" while looking at one directory is wrong in the flattering
    # direction. Found by pointing run-all.sh at that directory and noticing it had never been in
    # scope here. Vendored and archived trees are excluded because they are not this Core's tests.
    SKIP_PARTS = (".venv", "site-packages", "node_modules", "/archive/", "/tasks/archive/")
    files = sorted(
        x for x in ROOT.rglob("test_*.py")
        if x.name != SELF and not any(s in str(x) for s in SKIP_PARTS)
    )
    if not files:
        print("  REFUSING: no test files found. An empty sweep is not a clean sweep.")
        return 2
    dirs = sorted({str(x.parent.relative_to(ROOT)) for x in files})
    print("  scanning %d test files across %d directories:" % (len(files), len(dirs)))
    for d in dirs:
        print("    %-40s %d file(s)" % (d, sum(1 for x in files if str(x.parent.relative_to(ROOT)) == d)))
    check("more than one test directory is in scope", len(dirs) > 1,
          "only %s — if the others moved, re-point this rather than narrowing it" % dirs)
    print()

    silent = []
    for t in files:
        src = t.read_text()
        forms = failure_forms(src)
        if not forms:
            silent.append(t.name)

    check("every test file has at least one nonzero-exit path", not silent,
          "no failure path found in:\n          " + "\n          ".join(silent))

    print("\n--- THE CONTROLS: each accepted form must actually be recognised ---")
    # Without these the check above passes trivially if FORMS is ever narrowed to nothing, and it
    # is exactly how the first version of this matcher reported a working file as defective.
    SAMPLES = {
        "sys.exit(1)":             "import sys\nif bad:\n    sys.exit(1)\n",
        "sys.exit(main())":        "import sys\nsys.exit(main())\n",
        "sys.exit(<conditional>)": "import sys\nsys.exit(0 if before == after else 1)\n",
        "exit(1)":                 "if bad:\n    exit(1)\n",
        "raise SystemExit":        "raise SystemExit('boom')\n",
        "bare assert":             "assert x == 1\n",
    }
    for name, sample in SAMPLES.items():
        check("recognises %s" % name, bool(failure_forms(sample)),
              "this form is in use in the tree and would be reported as a defect")

    print("\n--- and a file with NO failure path must be detected ---")
    # The dose. Without it, "0 silent files" is consistent with a matcher that accepts everything.
    inert = "print('all good')\nprint('  PASS  something')\n"
    check("a print-only file is flagged as having no failure path", not failure_forms(inert),
          "the matcher accepts a file that cannot fail — it has no teeth")

    print("\n--- sys.exit(main()) proves nothing unless main() can return nonzero ---")
    # OVER-MATCHING IS THE DANGEROUS DIRECTION HERE. core-business (bus #985) split the
    # can't-match failure into two with different blast radii: *"an under-matching regex costs you a
    # finding; an over-matching one costs you your credibility with the person reading the report."*
    # It had just produced its first FALSE finding of the day — `grep 'return [0-9]'` naming two
    # working artifacts as permanently broken, because the regex matched only the first number in
    # `return 0 if ok else 1`.
    #
    # This file's risk is the mirror: reporting that a test CAN fail when it cannot. 44 of the files
    # here satisfy the check with `sys.exit(main())`, which is worth nothing if main() only ever
    # returns 0. Regex cannot see that; an AST walk can, so this one uses one.
    def main_can_fail(tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                for r in ast.walk(node):
                    if isinstance(r, ast.Return) and r.value is not None:
                        if isinstance(r.value, ast.Constant) and r.value.value == 0:
                            continue
                        return True
                return False
        return None

    weak = []
    for t in files:
        src = t.read_text()
        if "sys.exit(main())" not in src:
            continue
        try:
            if main_can_fail(ast.parse(src)) is False:
                weak.append(t.name)
        except SyntaxError:
            weak.append(t.name + " (does not parse)")
    check("every sys.exit(main()) file has a main() that can return nonzero", not weak,
          "main() can only return 0 in:\n          " + "\n          ".join(weak))
    check("...and the AST check can tell the two apart (control)",
          main_can_fail(ast.parse("def main():\n    return 0\n")) is False
          and main_can_fail(ast.parse("def main():\n    if b:\n        return 1\n    return 0\n")) is True,
          "the AST check cannot distinguish a main() that can fail from one that cannot")

    print("\n--- shell tests too, or the check implies a coverage it does not have ---")
    # Omitting these would let this file report "every test can fail" while never looking at four
    # of them. All four pass today; the point is that a fifth cannot slip in silently.
    SH = re.compile(r"exit\s+[1-9]|exit\s+\$|return\s+1")
    sh_files = sorted(TESTS.glob("test_*.sh"))
    check("shell tests were actually found (an empty glob is not coverage)", bool(sh_files),
          "no test_*.sh matched — if they moved, re-point this check rather than deleting it")
    sh_silent = [t.name for t in sh_files if not SH.search(t.read_text())]
    check("every shell test has a nonzero-exit path (%d file(s))" % len(sh_files), not sh_silent,
          "no failure path in: " + ", ".join(sh_silent))
    check("...and the shell matcher rejects a script that cannot fail",
          not SH.search("#!/usr/bin/env bash\necho ok\nexit 0\n"),
          "the shell matcher has no teeth")

    print("\n--- report which form each file relies on, so a narrowing is visible ---")
    counts = {}
    for t in files:
        for name in failure_forms(t.read_text()):
            counts[name] = counts.get(name, 0) + 1
    for name in sorted(counts, key=lambda k: -counts[k]):
        print("    %-26s %d file(s)" % (name, counts[name]))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
