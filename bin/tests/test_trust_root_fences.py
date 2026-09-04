#!/usr/bin/env python3
"""THE THING THAT DECIDES WHICH TREE GETS MEASURED MUST BE INSIDE THE FENCE, AND VISIBLE TO IT.

Three holes in the frozen evaluator, reported by core-business (bus #1058, one of them from a Fable
review), each verified on this tree before being fixed.

1. TWO FENCES, ONE SUBJECT. `bin/core_seat.py` was listed in bin/trajectory-gate.py's trust root and
   absent from bin/casebook-run.py's TCB. It is the module whose seat_root() decides WHICH TREE is
   measured and whose transcripts_dir() decides WHERE the evidence comes from. Uncovered, a
   candidate may rewrite the thing that chooses its own grader.

   The gate's copy of that list carries a comment calling this "the worst single omission
   available". It was written on this seat, about this defect, and nobody checked whether the other
   fence had the same hole. Fixing one instance of a class and documenting it is not fixing the
   class.

2. THE SELF-CHECK COULD NOT SEE ITS OWN OMISSION. `tcb_self_check` collected dependencies with
   `re.findall(r'"([A-Za-z0-9_./-]+\\.py)"', src)` — QUOTED path literals only. casebook-run.py
   depends on core_seat via `from core_seat import seat_root`, which matches nothing. The comment
   above the TCB credits this check with catching enforcement-audit.py in real time; it caught that
   one because the file happened to appear as a quoted string. It was structurally blind to the
   normal way Python code depends on Python code.

3. A DELETED MARKER SILENTLY REDIRECTED THE GRADER. seat_root adopts CORE_INSTANCE only if
   .claude/identity.json is present, and otherwise FELL THROUGH to Path(__file__).parents[1] — the
   frozen tree. The gate points CORE_INSTANCE at a materialised candidate arm, so a candidate diff
   deleting that tracked file made its own arm unadoptable and got the FROZEN TREE graded as the
   candidate. It now refuses instead.

Each is dosed against the shipped code, and each dose is paired with a control, because "refuse
everything" passes every one of these checks while breaking the system.

Run: python3 bin/tests/test_trust_root_fences.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
sys.path.insert(0, str(ROOT / "bin"))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== trust-root fences: the seat resolver is evaluator code ===\n")

    print("--- 1. BOTH fences must contain it, or they disagree about what an evaluator is ---")
    cb_src = (ROOT / "bin" / "casebook-run.py").read_text()
    gate_src = (ROOT / "bin" / "trajectory-gate.py").read_text()
    check("casebook-run.py's TCB lists bin/core_seat.py",
          '"bin/core_seat.py"' in cb_src.split("def tcb_self_check")[0],
          "the module that decides which tree is measured was outside this fence")
    check("trajectory-gate.py's trust root lists it too",
          '"bin/core_seat.py"' in gate_src)

    print("\n--- 2. the self-check must see a BARE import, not only a quoted literal ---")
    try:
        cb = load("cb_probe", "bin/casebook-run.py")
    except SystemExit:
        cb = sys.modules.get("cb_probe")
    except Exception as e:
        print("  SKIP — cannot load casebook-run: %s" % e)
        print("\n=== Results: %d passed, %d failed ===" % (p, f))
        return 1 if f else 0

    check("with the real TCB, the fence is consistent", not cb.tcb_self_check(),
          "a clean tree must not report a violation, or the check gets disabled")

    # THE DOSE: take core_seat back out and the check must refuse. Done by mutating the loaded
    # module's list rather than the file on disk, so a failing run cannot leave the shipped fence
    # edited — the same reason the live-state guard exists.
    saved = list(cb.TCB)
    try:
        cb.TCB[:] = [x for x in cb.TCB if x != "bin/core_seat.py"]
        missing = cb.tcb_self_check()
        check("removing it from the TCB makes the check FIRE",
              any("core_seat" in str(x) for x in missing),
              "the self-check still cannot see its own most important dependency: %r" % (missing,))
    finally:
        cb.TCB[:] = saved
    check("...and the fence is consistent again once restored", not cb.tcb_self_check())

    print("\n--- 3. an explicit seat that cannot be adopted must REFUSE, not redirect ---")
    import core_seat
    saved_env = {k: os.environ.get(k) for k in ("CORE_INSTANCE", "CLAUDE_PROJECT_DIR")}
    try:
        with tempfile.TemporaryDirectory() as td:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            os.environ["CORE_INSTANCE"] = td          # a real dir, no identity marker
            try:
                got = core_seat.seat_root()
                check("CORE_INSTANCE without the marker refuses", False,
                      "returned %s — the frozen tree would be graded as the candidate" % got)
            except RuntimeError as e:
                check("CORE_INSTANCE without the marker refuses", True)
                check("...and the message names the missing marker", "identity.json" in str(e),
                      str(e)[:120])

        # CONTROL. Refusing everything satisfies the check above and breaks every caller.
        os.environ["CORE_INSTANCE"] = str(ROOT)
        check("a real seat still resolves", core_seat.seat_root() == ROOT.resolve())

        # CLAUDE_PROJECT_DIR is deliberately NOT covered: the runtime sets it ambiently on every
        # hook invocation, so refusing on it would break hooks that run outside a Core.
        with tempfile.TemporaryDirectory() as td:
            os.environ.pop("CORE_INSTANCE", None)
            os.environ["CLAUDE_PROJECT_DIR"] = td
            try:
                core_seat.seat_root(fallback=ROOT)
                check("an ambient CLAUDE_PROJECT_DIR does NOT refuse (nobody chose it)", True)
            except RuntimeError as e:
                check("an ambient CLAUDE_PROJECT_DIR does NOT refuse (nobody chose it)", False,
                      "refusing here breaks every hook invoked outside a Core: %s" % str(e)[:90])
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
