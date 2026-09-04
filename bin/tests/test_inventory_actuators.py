#!/usr/bin/env python3
"""THE MAP THAT SAYS WHAT IS SWITCHED OFF MUST NOT LIE ABOUT IT — in either direction.

`bin/actuator-map.py` exists because Nick caught both Cores about to rebuild things that already
existed, three times in one session, and said: *"I still feel like you guys didn't map everything we
had built already which is wild."*

A wrong answer here is worse than no map:

    FALSE DARK    reports a live mechanism as missing -> someone rebuilds a working thing.
                  This is the expensive direction and it is the one the first three versions hit.
    FALSE WIRED   reports a dead mechanism as live -> the capability gap stays invisible.

THREE FALSE-DARK CLASSES WERE HIT WHILE BUILDING IT, all pinned below:

  1. `(?<![A-Za-z0-9_.])name\\(` — a negative lookbehind excluding a preceding dot, which is exactly
     how a cross-module call is written in Python. It reported route() dark while friction_loop.py
     calls `fr.route(case, neighbors=...)`. The map built to stop us rebuilding existing mechanisms
     was itself reporting live ones as missing.

  2. Requiring a caller in a DIFFERENT module. 63 of 170 came back dark, and most were CLI entry
     points invoked by their own module's main() — which is how a command-line tool is wired.

  3. Framework registration. `@mcp.tool()` functions are never called by name; the framework invokes
     them. get_topic() was flagged for that alone.

63 -> 8 -> 2 -> 1. Each step was a defect in the instrument, not a change in the tree, which is why
the final number is only worth quoting with these checks attached.

Run: python3 bin/tests/test_actuator_map.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
# REPOINTED. This tested a standalone bin/actuator-map.py that I wrote before finding
# bin/system-inventory.py, which already existed for the same reason. The analysis was folded into
# the existing inventory and the duplicate removed, so this now tests the ONE tool.
MAP = ROOT / "bin" / "system-inventory.py"

FIXTURE = {
    "mod_a.py": (
        "import mod_b\n"
        "def writes_and_called_dotted(p):\n"
        "    p.write_text('x')\n"
        "def writes_and_called_same_module(p):\n"
        "    p.write_text('x')\n"
        "def writes_and_dark(p):\n"
        "    p.write_text('x')\n"
        "def writes_but_only_named_in_a_comment(p):\n"
        "    p.write_text('x')\n"
        "@app.route('/x')\n"
        "def writes_and_registered(p):\n"
        "    p.write_text('x')\n"
        "def main():\n"
        "    writes_and_called_same_module(None)\n"
        "    # writes_but_only_named_in_a_comment(None) <- a comment is not a caller\n"
    ),
    "mod_b.py": (
        "import mod_a\n"
        "def go():\n"
        "    mod_a.writes_and_called_dotted(None)\n"
    ),
}


def run_map_on(tmp):
    """Point the map at the FIXTURE seat explicitly.

    Passing only cwd was not enough: the map resolves its root through core_seat, which honours
    CORE_INSTANCE/CLAUDE_PROJECT_DIR from the environment — so it measured the REAL repo and the
    fixture names appeared nowhere. Three of the five checks then passed VACUOUSLY, because
    "fixture_name not in dark_block" is trivially true when the fixture was never scanned. The two
    that required a name to be PRESENT are the only reason it was caught.
    """
    import os
    env = dict(os.environ, CORE_INSTANCE=str(tmp))
    env.pop("CLAUDE_PROJECT_DIR", None)
    r = subprocess.run(["python3", str(MAP)], cwd=str(tmp), capture_output=True,
                       text=True, timeout=300, env=env)
    return r.stdout + r.stderr


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== the actuator map must not report live mechanisms as dark ===\n")

    if not MAP.is_file():
        print("  SKIP — actuator-map.py absent")
        return 0

    with tempfile.TemporaryDirectory() as td:
        seat = Path(td)
        (seat / ".claude" / "state").mkdir(parents=True)
        (seat / ".claude" / "identity.json").write_text('{"hook_profile":{"role":"puller"}}')
        (seat / "bin").mkdir(exist_ok=True)
        (seat / "scheduling" / "probe").mkdir(parents=True)
        for name, body in FIXTURE.items():
            (seat / "scheduling" / "probe" / name).write_text(body)
        (seat / "bin" / "system-inventory.py").write_text(MAP.read_text())
        (seat / "bin" / "core_seat.py").write_text(
            (ROOT / "bin" / "core_seat.py").read_text())

        out = run_map_on(seat)
        dark_block = out.split("NO CALLER ANYWHERE")[1] if "NO CALLER ANYWHERE" in out else out
        # THE PLANTING IS A MEASUREMENT. Every "X is not reported dark" check below is trivially
        # true if the fixture was never scanned at all, which is exactly what happened first.
        check("the fixture seat was actually scanned (not the real repo)",
              "writes_and_dark" in out,
              "map output does not mention the fixture: %s" % out.splitlines()[:4])

        check("a DOTTED cross-module call counts as a caller",
              "writes_and_called_dotted" not in dark_block,
              "mod_b calls mod_a.writes_and_called_dotted(); reporting it dark sends someone to "
              "rebuild a working mechanism")
        check("a SAME-MODULE call from main() counts as a caller",
              "writes_and_called_same_module" not in dark_block,
              "CLI entry points are wired by their own main()")
        check("a DECORATED function is not dark (the framework invokes it)",
              "writes_and_registered" not in dark_block)

        check("a function nobody calls IS reported dark",
              "writes_and_dark" in dark_block,
              "if nothing is ever reported, the map is decoration")
        check("a call appearing ONLY in a comment does NOT count as a caller",
              "writes_but_only_named_in_a_comment" in dark_block,
              "this is how skill_graduate.promote() reads as live on this seat while never running")

    print("\n--- and on the REAL tree it produces a specific, checkable answer ---")
    real = subprocess.run(["python3", str(MAP)], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=600).stdout
    check("it finds a substantial number of actuators to judge",
          any(w.replace(",", "").isdigit() and int(w.replace(",", "")) > 50 for w in real.split()),
          "a map over almost nothing passes every check above vacuously")
    check("it names its exclusions rather than hiding them",
          "reachability only" in real,
          "an unstated exclusion is indistinguishable from a miss")

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
