#!/usr/bin/env python3
"""`--ref` must let a stale seat ADVANCE in reviewable hops, and must never roll one BACK.

WHY IT EXISTS. sentinel-code Rule 7 ASKs when a sync exceeds 50 file changes, and its stated remedy
is "Nick should confirm before the gate opens". No code path turns that confirmation into a mint:
sentinel-approve requires a literal `VERDICT: APPROVE`, records that ASK is not an approval, and
forbids re-running for a better verdict. So Rule 7 ASK is a dead end.

Worse, Rule 7 keys on FILE COUNT, which measures how far behind a seat is rather than how risky the
change is. core-ops named the consequence: **the further behind a seat falls, the more certain it
is that it cannot catch up.** They hit it at 58 files, for the second time — Nick ran the sync by
hand on 2026-08-21 for the same reason.

`--ref` does not relax Rule 7, raise the ceiling, or add an ASK-override. It removes the thing that
made the gate unsatisfiable, by letting a seat advance to an INTERMEDIATE published commit so each
hop is small enough to review on its own merits. Many small reviewed steps, not one unreviewable leap.

THE RISK IT INTRODUCES, AND WHAT THIS FILE IS REALLY FOR. A flag that chooses which baseline commit
to sync is a downgrade primitive if it is not constrained: point a Core at a commit from before a
security fix and the fix is gone, with a normal-looking review of a small, clean diff. So the
happy path is tested once and the REFUSALS are tested ten ways — an ADVANCE that works is a
convenience, a ROLLBACK that works is a vulnerability.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYNC = ROOT / "bin" / ("sync-from-" + "baseline.sh")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, timeout=60)


def build_baseline(d: Path):
    """Three commits on main (A -> B -> C) plus one commit on a side branch."""
    d.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=d)
    git("config", "user.email", "t@t", cwd=d)
    git("config", "user.name", "t", cwd=d)
    (d / "bin").mkdir(exist_ok=True)
    shas = {}
    for name in ("A", "B", "C"):
        (d / "bin" / f"file_{name}.py").write_text(f"# {name}\n")
        git("add", "-A", cwd=d)
        git("commit", "-qm", f"commit {name}", cwd=d)
        shas[name] = git("rev-parse", "HEAD", cwd=d).stdout.strip()
    git("checkout", "-q", "-b", "side", shas["A"], cwd=d)
    (d / "bin" / "file_side.py").write_text("# side\n")
    git("add", "-A", cwd=d)
    git("commit", "-qm", "side commit", cwd=d)
    shas["SIDE"] = git("rev-parse", "HEAD", cwd=d).stdout.strip()
    git("checkout", "-q", "main", cwd=d)
    return shas


def build_seat(d: Path, last_sha: str):
    (d / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    (d / ".claude").joinpath("identity.json").write_text(
        json.dumps({"core": "testseat", "org_id": 9, "hook_profile": {"role": "puller"}}))
    (d / ".claude" / "state" / ".last-baseline-sync").write_text(
        f"2026-08-01T00:00:00-0700 baseline={last_sha} via=test source=test\n")
    # A puller needs a manifest to decide what is shared; reuse the real one.
    (d / "bin").mkdir(parents=True, exist_ok=True)
    (d / "bin" / "sync-manifest.json").write_text((ROOT / "bin" / "sync-manifest.json").read_text())
    git("init", "-q", "-b", "main", cwd=d)
    git("config", "user.email", "t@t", cwd=d)
    git("config", "user.name", "t", cwd=d)
    git("add", "-A", cwd=d)
    git("commit", "-qm", "seat", cwd=d)


def run_sync(seat: Path, baseline: Path, ref: str):
    env = dict(os.environ,
               CORE_INSTANCE=str(seat),
               CLAUDE_PROJECT_DIR=str(seat),
               CORE_BASELINE_URL_LOCAL_TEST=str(baseline),
               CORE_SYNC_TEST_MODE="1")
    r = subprocess.run(["bash", str(SYNC), "--check", f"--ref={ref}"],
                       capture_output=True, text=True, timeout=300, env=env, cwd=str(seat))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "baseline"
        shas = build_baseline(base)

        # PREFLIGHT: can `git clone` run AT ALL here? Every check below drives sync-from-baseline.sh
        # through its LOCAL test seam (CORE_BASELINE_URL_LOCAL_TEST) specifically so this file needs
        # no network — but the seam still shells out to a real `git clone` of that local path
        # (sync-from-baseline.sh:228), and an environment with no git transport available at all
        # (e.g. a PATH wrapper that fails clone/fetch/pull/push fast, standing in for "no network")
        # cannot complete even a same-machine clone. Every one of the checks below would then fail
        # not because --ref's refusal logic is broken, but because nothing ever got far enough to
        # exercise it — a dependency this file cannot supply, not a defect in the flag under test.
        probe_src = Path(td) / "probe_src"
        probe_dst = Path(td) / "probe_dst"
        probe_src.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "main", cwd=probe_src)
        git("config", "user.email", "t@t", cwd=probe_src)
        git("config", "user.name", "t", cwd=probe_src)
        (probe_src / "f").write_text("x")
        git("add", "-A", cwd=probe_src)
        git("commit", "-qm", "probe", cwd=probe_src)
        probe = subprocess.run(["git", "clone", "-q", str(probe_src), str(probe_dst)],
                               capture_output=True, text=True, timeout=30)
        if probe.returncode != 0:
            print("  SKIP  network unavailable (git clone failed): --ref forward-only staged-pull "
                  "acceptance needs a working `git clone`, even of a local path — "
                  f"{(probe.stderr or probe.stdout).strip()[:160]}")
            return 0

        # ---- THE ADVANCE THAT MUST WORK ------------------------------------------------------
        seat = Path(td) / "seat_at_A"
        build_seat(seat, shas["A"])
        rc, out = run_sync(seat, base, shas["B"])
        # "REFUSED --ref", not bare "REFUSED". The real manifest this repo ships carries a known,
        # separately-tracked bug (bin/sync-manifest.json lists eval/casebook-v1.json in BOTH
        # `retired` and `per_core_keep` — bin/tests/test_retirement_propagates.py catches it
        # directly), which makes the sync's UNRELATED tombstone-collision guard print "tombstone
        # REFUSED (also per_core_keep): eval/casebook-v1.json" on every real run, staged or not.
        # A bare "REFUSED" substring check can't tell that apart from --ref itself being refused,
        # so this happy-path advance failed on a manifest issue outside this file's subject. Every
        # actual --ref refusal in sync-from-baseline.sh is prefixed "REFUSED --ref:" (grep the
        # script) — that is the specific signal this check needs, not any REFUSED line anywhere.
        check("a seat at A CAN advance to the intermediate commit B",
              "STAGED PULL" in out and "REFUSED --ref" not in out, out[-400:])

        # ---- THE FOUR REFUSALS ---------------------------------------------------------------
        rc, out = run_sync(seat, base, "nonsense")
        check("refuses a non-sha", "REFUSED" in out and rc == 2, out[-160:])

        rc, out = run_sync(seat, base, "0" * 40)
        check("refuses a sha that is not a commit in the baseline",
              "REFUSED" in out and "not a commit" in out, out[-160:])

        rc, out = run_sync(seat, base, shas["SIDE"])
        check("refuses a commit that is NOT on the published line (side branch)",
              "REFUSED" in out and "not an ancestor" in out, out[-200:])

        # The one that matters: a seat already at C must not be walked back to A.
        seat_c = Path(td) / "seat_at_C"
        build_seat(seat_c, shas["C"])
        rc, out = run_sync(seat_c, base, shas["A"])
        check("REFUSES A ROLLBACK — a seat at C cannot be pointed back at A",
              "REFUSED" in out and "never rolls one back" in out, out[-260:])

        rc, out = run_sync(seat_c, base, shas["C"])
        check("refuses a no-op (already at that ref)",
              "REFUSED" in out and "nothing to advance to" in out, out[-200:])

        # ---- THE CASES THE FIRST VERSION OF THIS FILE NEVER EXERCISED ------------------------
        # sentinel-code found the hole and, more usefully, found that this suite was GREEN over it:
        # every build_seat() wrote a valid .last-baseline-sync, so the descendant check always had
        # something to compare against. The bypass lived exactly where no fixture looked — a seat
        # with NO record, which is a fresh seat, a reset seat, or one whose state file was deleted.
        # Those are the three cases where you would most want the guard.
        seat_norec = Path(td) / "seat_no_record"
        build_seat(seat_norec, shas["C"])
        (seat_norec / ".claude" / "state" / ".last-baseline-sync").unlink()
        rc, out = run_sync(seat_norec, base, shas["A"])
        check("REFUSES --ref when the seat has NO baseline record (fail closed)",
              "REFUSED" in out and "no readable .last-baseline-sync" in out, out[-260:])

        seat_bad = Path(td) / "seat_bad_record"
        build_seat(seat_bad, shas["C"])
        (seat_bad / ".claude" / "state" / ".last-baseline-sync").write_text(
            "2026-08-01T00:00:00-0700 baseline=" + ("b" * 40) + " via=test source=test\n")
        rc, out = run_sync(seat_bad, base, shas["A"])
        check("REFUSES --ref when the recorded baseline is unresolvable (fail closed)",
              "REFUSED" in out and "not resolvable" in out, out[-260:])

        env0 = dict(os.environ, CORE_INSTANCE=str(seat), CLAUDE_PROJECT_DIR=str(seat),
                    CORE_BASELINE_URL_LOCAL_TEST=str(base), CORE_SYNC_TEST_MODE="1")
        r0 = subprocess.run(["bash", str(SYNC), "--check", "--ref="], capture_output=True,
                            text=True, timeout=120, env=env0, cwd=str(seat))
        out0 = (r0.stdout or "") + (r0.stderr or "")
        check("REFUSES an empty --ref= instead of falling through to a full pull",
              r0.returncode == 2 and "empty value" in out0, out0[-200:])

        # ---- A DECOY LINE MUST NOT BECOME THE ANSWER -----------------------------------------
        # `_LAST` was read with `grep -o ... | tail -1` over the WHOLE file, so the last textual
        # match won rather than the newest RECORD. Append any line mentioning an older
        # `baseline=<sha>` and `--ref` would "advance" from that stale point — a real rollback,
        # rc=0, with STAGED in the output. sentinel-code reproduced it live while adversarially
        # testing the fix for the previous hole in these same lines.
        #
        # THIS CASE IS BUILT TO DISCRIMINATE, because the first version of it did not. It pointed
        # --ref at the SAME sha as the decoy, so a decoy win produced "already at that ref" and the
        # check passed on the right verdict for the wrong reason. Here the seat is truly at C, the
        # decoy claims A, and we ask for B: if the decoy wins, B is a descendant of A and the pull
        # SUCCEEDS as a rollback from C to B. Only a correct parse refuses.
        seat_decoy = Path(td) / "seat_decoy"
        build_seat(seat_decoy, shas["C"])
        rec = seat_decoy / ".claude" / "state" / ".last-baseline-sync"
        rec.write_text(rec.read_text()
                       + f"# note: earlier we were at baseline={shas['A']} before the upgrade\n")
        rc, out = run_sync(seat_decoy, base, shas["B"])
        check("a DECOY line cannot make an older sha the seat's 'current' baseline",
              "REFUSED" in out and "does not come AFTER" in out and "STAGED PULL" not in out,
              out[-300:])

        # ---- an unknown flag must be REFUSED, not ignored ------------------------------------
        # There was no `*)` branch, so an unrecognised flag fell through in silence and the sync ran
        # with MODE unchanged. On a seat predating --ref, `--quiet --ref=<sha>` was therefore a FULL
        # pull wearing the shape of a staged one. The mutual-exclusion guard four lines below already
        # existed for the same reason ("passing both silently ran a REAL SYNC"), closed for two known
        # flags and open for every unknown one. core-ops found it; they could not fix it, because
        # bin/ is shared and they pull.
        env = dict(os.environ, CORE_INSTANCE=str(seat), CLAUDE_PROJECT_DIR=str(seat),
                   CORE_BASELINE_URL_LOCAL_TEST=str(base), CORE_SYNC_TEST_MODE="1")
        r = subprocess.run(["bash", str(SYNC), "--not-a-real-flag"], capture_output=True, text=True,
                           timeout=120, env=env, cwd=str(seat))
        out = (r.stdout or "") + (r.stderr or "")
        check("an unknown flag is REFUSED, not silently ignored",
              r.returncode == 2 and "unknown option" in out, out[-200:])

        # ---- and the flag must not disturb the ordinary path ---------------------------------
        env = dict(os.environ, CORE_INSTANCE=str(seat), CLAUDE_PROJECT_DIR=str(seat),
                   CORE_BASELINE_URL_LOCAL_TEST=str(base), CORE_SYNC_TEST_MODE="1")
        r = subprocess.run(["bash", str(SYNC), "--check"], capture_output=True, text=True,
                           timeout=300, env=env, cwd=str(seat))
        check("a plain --check with no --ref still runs (HEAD path unchanged)",
              "REFUSED --ref" not in (r.stdout or "") + (r.stderr or ""),
              ((r.stdout or "") + (r.stderr or ""))[-200:])

    print("\n" + ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "ALL PASS"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
