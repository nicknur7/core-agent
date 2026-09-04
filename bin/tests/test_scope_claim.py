#!/usr/bin/env python3
"""A UNIVERSAL CLAIM MADE FROM ONE READ. The ninth observer class, and the only decidable slice.

The rule already exists in .claude/rules/memory.md, and it is there because it has cost real work:

    "Absence claims need a multi-file grep, not one read. One read LOOKS like diligence while
     verifying nothing when the claim's truth lives in a different doc — a single read buys exactly
     the confidence it earns, which is none for a claim spanning files."

core-business ruled the wider 8-case pattern unenforceable and named this the one decidable slice.
It is decidable for a specific reason: the claim's SHAPE is universal, so a single read cannot be
sufficient evidence NO MATTER WHAT IT FOUND. Every other detector here asks "did you look?"; this
one asks "did you look in more than one place?", which is a countable property of the turn rather
than a judgement about the answer.

TWO DISTINCT TARGETS, NOT TWO CALLS. Re-reading one file twice is one place looked. Counting calls
would score a doubled read as diligence — precisely the substitution the rule warns about — so the
sourcing test is over the SET of file paths and search commands.

MEASURED BEFORE SHIPPING, on 3,975 real assistant replies from this Core: fires on 143 (3.6%), in
the same range as the artifact gate's 3% ceiling and instruction-directive's measured 7.4%. The
first version fired at 4.1% and was catching "and nothing else" — a qualifier, not an absence claim
— so `nothing` now requires an existence verb after it.

Run: python3 bin/tests/test_scope_claim.py
"""
import json
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
HOOK = ROOT / ".claude" / "hooks" / "reply-observer.py"


def load():
    """Compile from source every call — NOT importlib (2026-08-13).

    SourceFileLoader serves CACHED BYTECODE when the source's (mtime, size) match what the .pyc
    recorded, and on macOS system python3.9 that cache is redirected to
    ~/Library/Caches/com.apple.python/<abs-path>.pyc — so `find . -name '*.pyc'` finds nothing and
    the staleness is invisible from inside the repo.

    It bit while dosing this suite: a same-BYTE-LENGTH edit restored within the same second kept
    serving the DOSED bytecode. The file on disk read one value, grep agreed, git diff was clean,
    and the loaded module reported the other. The symptom is a test that stays red after a correct
    restore — which reads exactly like the dose found a real bug, the most expensive possible
    misreading, since it argues for changing correct code.
    """
    import types
    m = types.ModuleType("ro")
    m.__file__ = str(HOOK)
    exec(compile(HOOK.read_text(), str(HOOK), "exec"), m.__dict__)
    return m


FIRES = [
    "there is no trust-root hole",
    "All eight are false — including three claiming pretooluse",
    "None of those have a detector, a fitness measure, or a retirement path",
    "All of them carry compiled_truth_md",
    "no such files exist",
    "it is not referenced anywhere",
]
QUIET = [
    "I read the file and it looks correct.",
    "The runner drops directory timestamp-only lines and nothing else.",   # qualifier, not a claim
    "That authorizes nothing in particular.",                              # qualifier
    "I fixed the three tests that were failing.",
]


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== scope_claim: a universal claim needs more than one place looked ===\n")

    if not HOOK.is_file():
        print("  SKIP — reply-observer.py absent")
        return 0
    m = load()

    check("scope_claim is a registered detector",
          "scope_claim" in [n for n, _ in m.DETECTORS])

    print("\n--- it fires on universal and absence claims ---")
    rx = dict(m.DETECTORS)["scope_claim"]
    for s in FIRES:
        check("fires: %r" % s[:52], bool(rx.search(s)))

    print("\n--- and stays quiet on qualifiers and ordinary reports ---")
    # Without these the detector is satisfied by one that fires on everything, which would bury the
    # real signal and get the class switched off.
    for s in QUIET:
        check("quiet: %r" % s[:52], not rx.search(s))

    print("\n--- SOURCING: two DISTINCT places, not two calls ---")
    one = '{"file_path": "a.py"}'
    two = '{"file_path": "a.py"} {"file_path": "b.py"}'
    dup = '{"file_path": "a.py"} {"file_path": "a.py"}'
    grep = '{"command": "grep -rn foo bin/"} {"file_path": "a.py"}'
    check("one read -> UNSOURCED", not m.sourced_for("scope_claim", "there is no hook", one, ""))
    check("two distinct reads -> sourced", m.sourced_for("scope_claim", "there is no hook", two, ""))
    check("the SAME file read twice -> still UNSOURCED",
          not m.sourced_for("scope_claim", "there is no hook", dup, ""),
          "counting calls instead of targets scores a doubled read as diligence")
    check("a grep plus a read counts as two places",
          m.sourced_for("scope_claim", "there is no hook", grep, ""),
          "a multi-file grep is the evidence the rule actually asks for")
    check("no tool activity at all -> UNSOURCED",
          not m.sourced_for("scope_claim", "there is no hook", "", ""))

    print("\n--- the other classes are unchanged by this addition ---")
    # A new detector must not perturb the ones already being measured, or every historical number
    # silently changes meaning.
    check("all nine detectors present", len(m.DETECTORS) == 9, str([n for n, _ in m.DETECTORS]))
    check("duration_claim still resolves its own sourcing",
          m.sourced_for("duration_claim", "this session", '{"command": "date"}', "") is not None)

    # ---- FORGEABLE SOURCING ON financial_figure (core-finance, 2026-08-13) --------------------
    # The fallback stripped every non-digit from the WHOLE tool blob into one string and
    # substring-tested the claim's digits against it, so ANY number in the turn's output could
    # vouch for ANY figure whose digits fell inside it. Verified against _turn_tool_blob's real
    # shape, not a hand-built one.
    #
    # Direction is why it mattered: unsourced is the SAFE failure (over-report costs a log line,
    # under-report hides the thing being measured). This failed the UNSAFE way — a false SOURCED
    # silently removes a row from the numerator of the rule it exists to enforce.
    FORGERIES = [
        ("$450", "echo processed 1450 items", "an item count is not a balance"),
        ("$45", "echo took 3452 ms", "a duration is not a balance"),
        ("$120", "ls -la /var/log/1120.txt", "a filename is not a balance"),
        ("$1,240.00", "touch -t 1755012400 /tmp/x", "an epoch stamp is not a balance"),
    ]
    for claim, cmd, why in FORGERIES:
        blob = json.dumps({"name": "Bash", "input": {"command": cmd}})
        check("a coincidental digit run does NOT source %s (%s)" % (claim, why),
              not m.sourced_for("financial_figure", claim, blob, ""))

    # And the tightening must not cost the legitimate cases — including the rounding shape the
    # fallback's own comment says was 14 of 19 unsourced rows.
    LEGIT = [
        ("$1,240.00", "echo balance 1240.00", "exact match"),
        ("$450", "echo balance 450", "exact match, short claim"),
        ("$2.00", "echo 1.9950964999999998", "rounds from a measured float"),
        ("$45.20", "echo total 45.2", "trailing-zero precision"),
    ]
    for claim, cmd, why in LEGIT:
        blob = json.dumps({"name": "Bash", "input": {"command": cmd}})
        check("a real figure still sources: %s (%s)" % (claim, why),
              m.sourced_for("financial_figure", claim, blob, ""))

    # AND THE FALLBACK MUST STILL EXIST — with a case the DETECTOR CAN ACTUALLY PRODUCE.
    #
    # This first asserted European grouping ("1.240,00" vs a blob reporting 124000). core-finance
    # then showed that case is UNREACHABLE: `_FINANCIAL` emits nothing for the bare form and only
    # "$1" for "$1.240,00", so `matched` can never be the string the assertion passed. It called
    # sourced_for directly with an input the detector cannot generate — a guard defending dead
    # code, which would have BLOCKED a correct deletion. Same vacuity class this suite exists to
    # catch, introduced while fixing a different vacuity.
    #
    # The reachable case is CENTS-DENOMINATED tool output, which is ordinary for financial APIs:
    # a reply saying $1,240.00 against a tool reporting balance_cents 124000. The primary rounding
    # loop cannot match those (124000 does not round to 1240.00); digits-equal-token does.
    # Verified producible: _FINANCIAL emits "$1,240.00" verbatim.
    #
    # NOTE THE TENSION, because it is the real decision and not a detail: the same branch that
    # rescues cents is the one carrying the irreducible false-positive rate, since "$2.00" vouched
    # by a token "200" is indistinguishable from a coincidental 200 anywhere in the blob. Keeping
    # it is a tradeoff, NOT a dead-code question — which is why "the stated reason was wrong" does
    # not settle it.
    for claim, cmd in (("$1,240.00", "echo balance_cents 124000"),
                       ("$45.20", "echo amount_cents 4520")):
        cents_blob = json.dumps({"name": "Bash", "input": {"command": cmd}})
        check("cents-denominated output still sources %s (only the fallback catches this; "
              "the primary cannot round 124000 to 1240.00)" % claim,
              m.sourced_for("financial_figure", claim, cents_blob, ""))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
