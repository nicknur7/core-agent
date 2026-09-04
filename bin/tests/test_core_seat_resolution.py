#!/usr/bin/env python3
"""_core.py DECIDES WHICH CORE EVERY OTHER TEST MEASURES, and until now nothing tested it.

Ten test files import core_root() from it. If it picks the wrong seat, all ten report another
Core's numbers as their own — at exit 0, with no symptom. That is not a hypothetical: it is exactly
what happened when core-business ran test_root_anchors.py from its seat, measured LIFE, and read
"the measured number is 0" as a clean result. _core.py was written to end that class, and it became
the one unverified file in it.

WHAT THIS PINS, from core-business's ASK in #909 — it ran core_root() from three cwd positions and
found the env branch returning BEFORE the refuse-on-mismatch check, so the file's entire stated
guarantee did not cover its own first branch. The answer given, and enforced here:

    CORE_INSTANCE       DELIBERATE. An operator set it and meant it, so it wins. An override that
                        can be overruled is not an override.

    CLAUDE_PROJECT_DIR  AMBIENT. The runtime sets it on every hook and subagent invocation and
                        nobody chose it. Demoted to a hint: honoured when it agrees with the file's
                        own location, REFUSED when it disagrees. Letting it win is the wrong-Core
                        defect with a new door.

Every case runs a REAL subprocess against a REAL throwaway Core tree, because the property is about
process environment and file location — the two things an in-process assertion cannot exercise
honestly.

Run: python3 bin/tests/test_core_seat_resolution.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

REAL = core_root()
SRC = Path(__file__).resolve().parent / "_core.py"

PROBE = """
import sys
sys.path.insert(0, %r)
from _core import core_root, core_name
try:
    print("OK " + str(core_root()))
except RuntimeError as e:
    print("REFUSED " + str(e).splitlines()[0])
"""


def make_core(base: Path, name: str) -> Path:
    """A minimal but genuine Core: the identity marker is what every resolver keys on."""
    d = base / name
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    (d / ".claude" / "identity.json").write_text('{"core": "%s"}' % name[5:])
    (d / "bin" / "tests").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC, d / "bin" / "tests" / "_core.py")
    # _core.py now imports the shared walk from bin/core_seat.py, so a throwaway Core needs both.
    # Copying only the file under test made every case here raise ImportError — and an exception is
    # not a refusal: two assertions expecting REFUSED would have passed on the traceback if the
    # check had matched on failure rather than on the refusal string.
    shutil.copy2(SRC.parents[1] / "core_seat.py", d / "bin" / "core_seat.py")
    return d


def resolve(script_core: Path, cwd: Path, **env) -> str:
    """Run _core.py's resolver from a given SEAT with a given ENVIRONMENT."""
    e = {k: v for k, v in os.environ.items() if k not in ("CORE_INSTANCE", "CLAUDE_PROJECT_DIR")}
    e.update({k: str(v) for k, v in env.items() if v is not None})
    r = subprocess.run([sys.executable, "-c", PROBE % str(script_core / "bin" / "tests")],
                       cwd=str(cwd), env=e, text=True, capture_output=True, timeout=30)
    return (r.stdout or r.stderr).strip()


def main() -> int:
    p = f = 0
    print("=== _core.py seat resolution ===\n")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        life = make_core(base, "core-alpha")      # stands in for the file's home Core
        biz = make_core(base, "core-beta")        # stands in for a peer

        def check(label, got, want_kind, want_core=None):
            nonlocal p, f
            ok = got.startswith(want_kind) and (want_core is None or str(want_core) in got)
            print(("  PASS  " if ok else "  FAIL  ") + label + ("" if ok else "\n          got: " + got[:150]))
            if ok:
                p += 1
            else:
                f += 1

        print("--- the deliberate override wins, which is the documented answer ---")
        check("CORE_INSTANCE=beta while the file lives in alpha → beta",
              resolve(life, life, CORE_INSTANCE=biz), "OK", biz)
        check("CORE_INSTANCE beats CLAUDE_PROJECT_DIR when they disagree",
              resolve(life, life, CORE_INSTANCE=biz, CLAUDE_PROJECT_DIR=life), "OK", biz)

        print("\n--- the AMBIENT variable is a hint, not an override (the ASK's answer) ---")
        # This is the case that returned beta silently at exit 0 before the split.
        check("CLAUDE_PROJECT_DIR=beta while the file lives in alpha → REFUSES",
              resolve(life, life, CLAUDE_PROJECT_DIR=biz), "REFUSED")
        check("CLAUDE_PROJECT_DIR=alpha, agreeing with the file → alpha",
              resolve(life, life, CLAUDE_PROJECT_DIR=life), "OK", life)

        print("\n--- and the original guarantee still holds with no env at all ---")
        check("standing in beta, running alpha's copy → REFUSES rather than guessing",
              resolve(life, biz), "REFUSED")
        check("standing in alpha, running alpha's copy → alpha",
              resolve(life, life), "OK", life)

        print("\n--- dose: the refusal must depend on the input, not be the constant answer ---")
        # Two runs differing ONLY in which Core the ambient variable names. If both refuse, the
        # refusal is a stuck answer and proves nothing — the rule this session adopted fleet-wide.
        agree = resolve(life, life, CLAUDE_PROJECT_DIR=life)
        disagree = resolve(life, life, CLAUDE_PROJECT_DIR=biz)
        if agree.startswith("OK") and disagree.startswith("REFUSED"):
            print("  PASS  one differing input flips resolve → refuse")
            p += 1
        else:
            print("  FAIL  agreement and disagreement produced the same class of answer:"
                  "\n          agree=%s\n          disagree=%s" % (agree[:90], disagree[:90]))
            f += 1

        print("\n--- an EXISTING directory that is not a Core must not be adopted ---")
        # `if (p / _MARKER).is_file() or (p / "bin").is_dir()` — the right disjunct defeated the
        # left one entirely, so ANY directory on the machine with a bin/ subdirectory qualified as
        # a Core. core-business demonstrated it rather than arguing it: CORE_INSTANCE=/usr/local
        # and /opt/homebrew were both ADOPTED. The case originally reported (<core>/memory) was
        # rejected only INCIDENTALLY, because memory/ happens to have no bin/ — so the fix that
        # closed it looked complete while the disjunct survived.
        for _path in ("/usr/local", "/opt/homebrew"):
            if not Path(_path).is_dir():
                continue
            check("CORE_INSTANCE=%s (has bin/, no identity marker) falls through to the real Core"
                  % _path, resolve(life, life, CORE_INSTANCE=_path), "OK", life)

        print("\n--- a nonexistent env path must not be honoured, and must not crash ---")
        check("CORE_INSTANCE pointing at a directory with no identity marker → falls through",
              resolve(life, life, CORE_INSTANCE=base / "nope"), "OK", life)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
