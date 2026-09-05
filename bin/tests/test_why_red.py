#!/usr/bin/env python3
"""A RED TEST ON A PULLER IS AMBIGUOUS, and why-red.sh is the thing that resolves it.

core-business, bus #1036: *"on a puller that ambiguity is the DEFAULT case, not an edge case, and
the runner is the only thing positioned to name it. finance and ops are two baselines behind; every
FAIL they see will be that shape."* Of three real failures on business's seat, TWO were stale copies
whose baseline versions pass. It ran the comparison by hand three times and it decided all three.

THE THREE ANSWERS ARE ASSERTED AGAINST A REAL BASELINE, not a mock. Each case is built as an actual
git repo and compared through the shipped script, because the property under test is a byte
comparison between two checkouts — the one thing a stubbed baseline cannot exercise honestly.

THE REFUSAL CASES ARE THE LOAD-BEARING HALF. This script's failure mode is not "no answer", it is
**"BROKEN" reported for a file it never actually compared** — a false accusation against shared code,
sent to peers, sourced from a clone that silently did not happen. So the unreachable-baseline path
must say UNDECIDABLE and the mis-set test seam must refuse, and both are dosed here. A diagnosis tool
that guesses when it cannot see is worse than one that is absent, because its output is quotable.

Run: python3 bin/tests/test_why_red.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SCRIPT = ROOT / "bin" / "tests" / "why-red.sh"


def sh(cwd, cmd):
    r = subprocess.run(["bash", "-c", cmd], cwd=str(cwd), capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("fixture failed: %s\n%s" % (cmd, r.stderr[:300]))
    return r.stdout


def build(tmp, seat_files, base_files, role="puller", repo="nicknur7/core-agent"):
    """Construct a seat and a baseline repo that differ exactly as the case requires."""
    seat, base = Path(tmp) / "seat", Path(tmp) / "base"
    (seat / "bin" / "tests").mkdir(parents=True)
    (seat / ".claude").mkdir(parents=True)
    (seat / "bin" / "sync-manifest.json").write_text(json.dumps({
        "baseline_repo": repo, "baseline_branch": "main",
        "baseline_writer": "core-life", "shared": {"dirs": ["bin"], "files": []},
    }))
    (seat / ".claude" / "identity.json").write_text(json.dumps({"hook_profile": {"role": role}}))
    (seat / "bin" / "tests" / "why-red.sh").write_text(SCRIPT.read_text())
    for rel, body in seat_files.items():
        f = seat / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    for rel, body in base_files.items():
        f = base / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    base.mkdir(exist_ok=True)
    sh(base, "git init -q . && git config user.email t@t && git config user.name t "
             "&& git add -A && git commit -qm base")
    return seat, base


def run(seat, base, args, extra_env=None, seam=True):
    env = dict(os.environ)
    env["CORE_INSTANCE"] = str(seat)
    if seam:
        env["CORE_SYNC_TEST_MODE"] = "1"
        env["CORE_BASELINE_URL_LOCAL_TEST"] = str(base)
    env.update(extra_env or {})
    r = subprocess.run(["bash", str(seat / "bin" / "tests" / "why-red.sh")] + args,
                       capture_output=True, text=True, timeout=180, env=env)
    return r.stdout + r.stderr, r.returncode


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== why-red.sh: stale copy, or real defect? ===\n")

    # PREFLIGHT: most of this file drives why-red.sh through its LOCAL test seam
    # (CORE_BASELINE_URL_LOCAL_TEST) specifically so it needs no network — but the seam still shells
    # out to a real `git clone` of that local path (why-red.sh:160), and an environment with no git
    # transport at all (e.g. a PATH wrapper that fails clone/fetch/pull/push fast, standing in for
    # "no network") cannot complete even a same-machine clone. Every seam-based case below would then
    # report UNDECIDABLE for a reason that has nothing to do with why-red.sh's own logic — and the
    # `out.split("DIFFERS")[1]`-style assertions crash outright on text that was never printed. Not a
    # defect in the script under test: a dependency this file cannot supply here.
    with tempfile.TemporaryDirectory() as ptd:
        probe_src, probe_dst = Path(ptd) / "src", Path(ptd) / "dst"
        probe_src.mkdir()
        sh(probe_src, "git init -q . && git config user.email t@t && git config user.name t "
                      "&& touch f && git add -A && git commit -qm probe")
        probe = subprocess.run(["git", "clone", "-q", str(probe_src), str(probe_dst)],
                               capture_output=True, text=True, timeout=30)
        if probe.returncode != 0:
            print("  SKIP  network unavailable (git clone failed): why-red.sh's stale-vs-defect "
                  "diagnosis needs a working `git clone`, even of a local path — "
                  f"{(probe.stderr or probe.stdout).strip()[:160]}")
            return 0

    with tempfile.TemporaryDirectory() as td:
        seat, base = build(
            td,
            seat_files={"bin/tests/test_same.py": "IDENTICAL\n",
                        "bin/tests/test_diff.py": "SEAT VERSION\n",
                        "tasks/x/test_local.py": "local only\n"},
            base_files={"bin/tests/test_same.py": "IDENTICAL\n",
                        "bin/tests/test_diff.py": "BASELINE VERSION\n"},
        )
        out, rc = run(seat, base, ["bin/tests/test_same.py", "bin/tests/test_diff.py",
                                   "tasks/x/test_local.py"])

        check("identical-to-baseline is called BROKEN (a real defect in shared code)",
              "BROKEN" in out and "test_same.py" in out.split("BROKEN")[1][:60], out[:600])
        check("a differing copy is called DIFFERS, not BROKEN",
              "DIFFERS" in out and "test_diff.py" in out.split("DIFFERS")[1][:60], out[:600])
        check("a non-shared path is called LOCAL",
              "LOCAL" in out and "test_local.py" in out.split("LOCAL")[1][:60], out[:600])

        # THE ONE THAT MATTERS MOST. Reporting BROKEN for a file whose copies differ sends a Core
        # chasing a defect that is not there -- which is exactly the wasted trip this script exists
        # to prevent, produced by the script meant to prevent it.
        check("...and the DIFFERING file is never called BROKEN",
              "BROKEN" not in out.split("DIFFERS")[1], out[:600])
        check("it names the baseline commit it compared against", "nicknur7/core-agent@" in out, out[:300])
        check("it is diagnosis only — always exits 0", rc == 0, "rc=%d" % rc)

        print("\n--- a CURRENT test against a STALE MODULE must not be called BROKEN ---")
        # core-business, bus #1037: three failures reported as real defects in shared code, all three
        # passing on the writer's tree. The test file matched the baseline; the module it exercised
        # did not. The old logic compared the TEST and concluded about the CODE.
        #
        # Weight this one: finance and ops are two baselines behind, so nearly every red row they
        # see has this shape, and the old answer told each of them to report a defect on the bus.
        seat2, base2 = build(
            tempfile.mkdtemp(),
            seat_files={"bin/tests/test_mod.py": "SAME TEST\n", "bin/thing.py": "STALE MODULE\n"},
            base_files={"bin/tests/test_mod.py": "SAME TEST\n", "bin/thing.py": "CURRENT MODULE\n"},
        )
        out_sd, _ = run(seat2, base2, ["bin/tests/test_mod.py"])
        check("an identical test over a stale module is UNDECIDABLE, not BROKEN",
              "UNDECIDABLE" in out_sd and "BROKEN" not in out_sd, out_sd[:600])
        check("...and it NAMES the stale file, which is the part that was done by hand",
              "bin/thing.py" in out_sd, out_sd[:600])

        print("\n--- but when test AND subject both match, BROKEN is still available ---")
        # Without this the fix is consistent with a script that never accuses anything -- which would
        # be safe, useless, and indistinguishable from working.
        seat3, base3 = build(
            tempfile.mkdtemp(),
            seat_files={"bin/tests/test_mod.py": "SAME TEST\n", "bin/thing.py": "SAME MODULE\n"},
            base_files={"bin/tests/test_mod.py": "SAME TEST\n", "bin/thing.py": "SAME MODULE\n"},
        )
        out_ok, _ = run(seat3, base3, ["bin/tests/test_mod.py"])
        check("a fully-current seat still gets a real BROKEN verdict",
              "BROKEN" in out_ok and "UNDECIDABLE" not in out_ok, out_ok[:600])

        print("\n--- a shared file the baseline has never seen is NEW HERE, not BROKEN ---")
        (seat / "bin" / "tests" / "test_brandnew.py").write_text("authored here\n")
        out2, _ = run(seat, base, ["bin/tests/test_brandnew.py"])
        check("unpushed local authorship is NEW HERE", "NEW HERE" in out2, out2[:400])
        check("...and is not miscalled BROKEN", "BROKEN" not in out2, out2[:400])

        print("\n--- on the WRITER seat, differing is expected and must be said so ---")
        seatw, basew = build(
            td + "/w" if False else tempfile.mkdtemp(),
            seat_files={"bin/tests/test_diff.py": "WRITER AHEAD\n"},
            base_files={"bin/tests/test_diff.py": "OLD BASELINE\n"},
            role="writer",
        )
        out3, _ = run(seatw, basew, ["bin/tests/test_diff.py"])
        check("the writer is told being ahead is expected, not staleness",
              "WRITER" in out3 and "expected" in out3, out3[:500])

    print("\n--- DOTTED PATHS: .claude/** is shared and must be compared, not written off ---")
    # core-business, bus #1048. `is_shared` normalised with `lstrip("./")` — a CHARACTER SET, not a
    # prefix — so ".claude/hooks/x.py" became "claude/hooks/x.py", matched no manifest entry, and
    # every .claude/** path read as LOCAL: "this failure is yours, nothing to compare", with no clone
    # and no diff. On .claude/hooks, which is pretooluse-guard, sentinel-approve, sentinel-receipt
    # and reply-observer.
    #
    # THIS FILE PASSED 18/18 WITH THAT BUG because every fixture path started with `bin/`. A suite
    # that only ever exercises the undotted case cannot see a bug in how dots are handled, and the
    # whole point of why-red is to answer correctly about SHARED code — most of which is dotted.
    with tempfile.TemporaryDirectory() as td:
        seat = Path(td) / "seat"
        for sub in ("bin/tests", ".claude/hooks", ".claude/rules"):
            (seat / sub).mkdir(parents=True)
        (seat / ".claude").mkdir(exist_ok=True)
        (seat / "bin" / "sync-manifest.json").write_text(json.dumps({
            "baseline_repo": "nicknur7/core-agent", "baseline_branch": "main", "baseline_writer": "core-life",
            "shared": {"dirs": ["bin", ".claude/hooks", ".claude/rules"],
                       "files": [".mcp.json.template"]},
        }))
        (seat / ".claude" / "identity.json").write_text(json.dumps({"hook_profile": {"role": "puller"}}))
        (seat / "bin" / "tests" / "why-red.sh").write_text(SCRIPT.read_text())

        base = Path(td) / "dotbase"
        for rel, body in {".claude/hooks/reply-observer.py": "SAME\n",
                          ".claude/rules/privacy.md": "SAME\n",
                          ".mcp.json.template": "SAME\n"}.items():
            # NOT `f` — that is this function's FAIL COUNTER, and binding a PosixPath to it made
            # the summary line raise "%d format: a number is required, not PosixPath" AFTER every
            # check had already printed PASS. A test file that cannot count its own results is the
            # same failure class it is here to catch.
            bf = base / rel
            bf.parent.mkdir(parents=True, exist_ok=True)
            bf.write_text(body)
            sf = seat / rel
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text(body)
        sh(base, "git init -q . && git config user.email t@t && git config user.name t "
                 "&& git add -A && git commit -qm base")

        for target in (".claude/hooks/reply-observer.py", ".claude/rules/privacy.md",
                       ".mcp.json.template"):
            out, _ = run(seat, base, [target])
            check("%s is recognised as SHARED" % target, "LOCAL" not in out, out[:400])

        # And with a leading "./" — the form run-all.sh actually passes, since its discovery globs
        # produce "./bin/tests/x.py". Both spellings must reach the same answer.
        out, _ = run(seat, base, ["./.claude/hooks/reply-observer.py"])
        check("...and a leading ./ is stripped as a PREFIX, not a character set",
              "LOCAL" not in out, out[:400])

    print("\n--- REFUSALS: it must never answer from a comparison it did not make ---")
    with tempfile.TemporaryDirectory() as td:
        seat, base = build(td, seat_files={"bin/tests/test_a.py": "x\n"},
                           base_files={"bin/tests/test_a.py": "x\n"})

        # An unreachable baseline must produce UNDECIDABLE. If it instead fell through to the
        # comparison loop with an empty clone dir, every shared file would read NEW HERE -- a wrong
        # answer with the same confident shape as a right one.
        #
        # THE FIRST VERSION OF THIS CASE DID NOT CONSTRUCT IT. It just dropped the test seam, so the
        # script cloned the REAL baseline repo -- successfully -- and correctly reported the fixture
        # file as NEW HERE, since the real baseline has no bin/tests/test_a.py. The script was right
        # and the FIXTURE was wrong, with the failure pointing squarely at the subject. That is the
        # same shape three fixtures hit earlier today, and the tell is identical every time: the
        # accusation lands on the thing under test rather than on the setup. Unreachability has to
        # be BUILT, not assumed from the absence of a seam.
        unreach, _b = build(tempfile.mkdtemp(), seat_files={"bin/tests/test_a.py": "x\n"},
                            base_files={"bin/tests/test_a.py": "x\n"},
                            repo="nicknur7/core-does-not-exist-9271")
        out, rc = run(unreach, _b, ["bin/tests/test_a.py"], seam=False,
                      extra_env={"CORE_BASELINE_CLONE_TIMEOUT": "20"})
        check("an unreachable baseline says UNDECIDABLE", "UNDECIDABLE" in out, out[:400])
        check("...and claims nothing about the file",
              "BROKEN" not in out and "NEW HERE" not in out, out[:400])
        check("...and still exits 0 rather than failing the suite", rc == 0, "rc=%d" % rc)

        # The seam needs BOTH variables, mirroring sync-from-baseline.sh: a stale variable left in
        # an environment must not silently redirect what a seat compares itself against.
        env = {"CORE_BASELINE_URL_LOCAL_TEST": str(base)}
        out2, _ = run(seat, base, ["bin/tests/test_a.py"], seam=False, extra_env=env)
        check("the test seam refuses without CORE_SYNC_TEST_MODE=1",
              "REFUSED test seam" in out2, out2[:400])

        out3, _ = run(seat, base, ["bin/tests/test_a.py"], seam=False,
                      extra_env={"CORE_SYNC_TEST_MODE": "1",
                                 "CORE_BASELINE_URL_LOCAL_TEST": str(Path(td) / "not-a-repo")})
        check("...and refuses a seam path that is not a git repo",
              "REFUSED test seam" in out3, out3[:400])

        out4, _ = run(seat, base, ["bin/tests/test_a.py"], extra_env={"CORE_NO_BASELINE_CHECK": "1"})
        check("CORE_NO_BASELINE_CHECK=1 opts out entirely", out4.strip() == "", out4[:200])

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
