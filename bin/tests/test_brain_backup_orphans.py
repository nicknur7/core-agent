#!/usr/bin/env python3
"""brain-backup must not orphan a .partial when it is KILLED mid-dump.

WHY THIS EXISTS (2026-08-12). bin/brain-backup.sh writes to `<out>.dump.partial`, verifies,
then renames — a correct design whose two failure paths both `rm -f "$TMP"`. Neither runs
when the process is killed from outside. It ran inside the Stop hook, which carries
`timeout: 60` in settings.json, against a pg_dump of a 1,613 MB database that takes ~6
minutes. Every close was killed mid-dump and left a ~200 MB fragment.

It compounded three ways:
  1. `newest_age_hours()` globs *.dump only, so a fragment never counts as a recent backup:
     the age check stayed STALE forever and re-fired on the very next close.
  2. Rotation globs `${PREFIX}-*.dump` — nothing ever swept fragments.
  3. All five Cores default SNAP_DIR to the same $HOME/AI Projects/brain-snapshots, so every
     seat's closes piled into one directory.
Measured 2026-08-12 00:01 PDT: 671 fragments, 128 GB, on a disk with 45 GiB free — a fifth of
the drive, growing ~200 MB per assistant turn. And because pg_dump sat mid-close, EVERY stage
after it stopped running: grade-gate and grade-intent froze at 2026-08-09T22:56:33Z, one
minute after the backup window expired, across 187 life closes and 223 business closes.

THE FIRST FIX FAILED ITS OWN DOSE, which is why this file tests behaviour and not source text:
a trap does NOT fire while a foreground child is running — bash queues the handler until the
current command returns, so a SIGTERM during a 6-minute pg_dump cleaned up nothing. The
working version runs pg_dump in the background, `wait`s on it (interruptible), and the handler
kills the child before removing its output.

These tests dose the real script with a real kill. They do not read it for keywords.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bin" / "brain-backup.sh"
LIFECYCLE = REPO / ".claude" / "hooks" / "session-lifecycle.sh"

failures: list[str] = []
passes: list[str] = []
abstains: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name if ok else f"{name}: {detail}")
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  -> ' + detail}")


def db_reachable() -> bool:
    try:
        return subprocess.run(
            ["psql", "-d", os.environ.get("COREBRAIN_DB", "corebrain"), "-c", "SELECT 1"],
            capture_output=True, timeout=15,
        ).returncode == 0
    except Exception:
        return False


def test_kill_midway_leaves_no_partial(snap: Path) -> None:
    """THE test. Start a real dump, SIGTERM it mid-write, assert nothing is left behind."""
    env = {**os.environ, "BRAIN_SNAPSHOT_DIR": str(snap)}
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "--force"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Wait until the dump is genuinely in progress, so the kill lands mid-write and the
    # test cannot pass by killing something that never started.
    started = False
    for _ in range(60):
        if list(snap.glob("*.partial")):
            started = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.25)

    if not started:
        proc.kill()
        # NOT A CODE PROPERTY — A FIXTURE, specifically DATABASE SIZE. This dose needs pg_dump to
        # still be mid-write when the poll checks in, which is why it exists at all (2026-08-12's
        # incident measured a 1,613 MB corebrain taking ~6 minutes to dump). A near-empty scratch
        # DB — this suite's own fixtests_* — dumps in well under 0.25s, the poll interval, so the
        # window this test needs to observe never opens; the process may finish and rename to
        # .dump before the very first glob(). That is not evidence the kill-handling code is
        # broken, only that this seat's corpus is too small to observe it happening. The audit
        # that found this test failing standalone also found it PASSING inside the full suite run
        # (same scratch DB, populated by 173 other tests' side effects by the time this one ran) —
        # consistent with a timing/size precondition, not a flaky assertion.
        abstains.append("dump reached the .partial stage (fixture precondition)")
        print("  UNDEC  dump reached the .partial stage (fixture precondition)\n"
              "          no .partial ever appeared in 15s of polling — this seat's database is too "
              "small for pg_dump to still be writing when checked, not evidence the kill/cleanup "
              "path is broken")
        return
    check("dump reached the .partial stage (fixture precondition)", True)

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

    time.sleep(1.5)  # let the handler's child-kill and unlink settle
    leftover = list(snap.glob("*.partial"))
    check("SIGTERM mid-dump leaves no orphaned .partial", not leftover,
          f"{len(leftover)} fragment(s) survived: {[p.name for p in leftover]}")

    lock = snap / ".backup.lock"
    check("SIGTERM mid-dump releases the lock", not lock.exists(),
          "lock still held — the next backup would skip forever")


def test_startup_sweep_reclaims_sigkill_orphans(snap: Path) -> None:
    """A trap cannot catch SIGKILL, so the NEXT run must reclaim what it left."""
    orphan = snap / "auto-19990101-000000.dump.partial"
    orphan.write_bytes(b"x" * 1024)
    old = time.time() - (3 * 3600)  # older than the 120-minute safety window
    os.utime(orphan, (old, old))

    subprocess.run(
        ["bash", str(SCRIPT), "--status"],
        env={**os.environ, "BRAIN_SNAPSHOT_DIR": str(snap)},
        capture_output=True, timeout=60,
    )
    check("startup sweep removes a stale orphaned .partial", not orphan.exists(),
          "a >2h-old fragment survived — SIGKILL orphans would accumulate forever")


def test_sweep_spares_a_live_dump(snap: Path) -> None:
    """The sweep must never delete a fragment a concurrent dump is actively writing."""
    fresh = snap / "auto-20990101-000000.dump.partial"
    fresh.write_bytes(b"x" * 1024)  # mtime = now
    subprocess.run(
        ["bash", str(SCRIPT), "--status"],
        env={**os.environ, "BRAIN_SNAPSHOT_DIR": str(snap)},
        capture_output=True, timeout=60,
    )
    check("startup sweep spares a fresh (in-progress) .partial", fresh.exists(),
          "the sweep deleted a fragment a running dump was still writing")
    fresh.unlink(missing_ok=True)


def test_backup_is_not_inline_in_the_close() -> None:
    """The structural half: a ~6-minute job must not sit inside a 60-second hook."""
    body = LIFECYCLE.read_text(encoding="utf-8", errors="replace")
    line = next((l for l in body.splitlines() if "brain-backup.sh" in l and "-f " not in l), "")
    check("brain-backup is detached from the close path", "nohup" in line and line.rstrip().endswith("&"),
          f"still invoked synchronously: {line.strip()[:110]!r} — the Stop timeout will kill it again")


def main() -> int:
    print(f"test_brain_backup_orphans  ({SCRIPT.relative_to(REPO)})")
    if not SCRIPT.exists():
        print("  FAIL  bin/brain-backup.sh missing")
        return 1

    test_backup_is_not_inline_in_the_close()

    if not db_reachable():
        print("  SKIP  corebrain unreachable — kill/sweep doses need a real dump")
        print(f"\n{len(passes)} passed, {len(failures)} failed (dump doses skipped)")
        return 1 if failures else 0

    tmp = Path(tempfile.mkdtemp(prefix="brainbackup-test-"))
    try:
        test_startup_sweep_reclaims_sigkill_orphans(tmp)
        test_sweep_spares_a_live_dump(tmp)
        test_kill_midway_leaves_no_partial(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(passes)} passed, {len(failures)} failed"
          + (f", {len(abstains)} undecidable" if abstains else ""))
    if failures:
        return 1
    if abstains:
        # rc=2 + UNDECIDABLE, the run-all.sh ABSTAIN contract (test_wilson_ci_known_answers.py is
        # the precedent this copies): every other check above ran for real; only the SIGTERM-mid-
        # dump dose needs a database large enough to still be writing when polled.
        print(f"\nUNDECIDABLE: {len(abstains)} check(s) could not be armed on this seat's database "
              f"size. Not a pass.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
