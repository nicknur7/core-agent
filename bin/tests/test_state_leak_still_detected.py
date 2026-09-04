#!/usr/bin/env python3
"""Widening the leak detector's noise list must not blind it. Plant a real leak; it must still fire.

WHY THIS EXISTS (2026-08-12, T034). run-all.sh hashes `.claude/state` before and after a run and
reports LEAK if it changed, because a test writing into the live seat is invisible until a peer
reads it back. Two files were excluded as "append-only telemetry ... real hooks derive their state
dir from their own location and cannot be redirected."

That list was incomplete. `gate-fires.log` and `.peer-digest` have the identical property — written
by live hooks (recall-gate.py, brain-recall-trigger.py, hooklog.py, session-presence.py), not by any
test — so the check fired on EVERY run in a live session. `bin/tests` exited 1 with `ok=100 FAIL=0`,
and because `leak` is not in the printed counter line (run-all.sh:465 acts on six counters and shows
five), the summary could not say why.

I then reported "ALL GREEN 100/100" to Nick and to the bus repeatedly, because my grep filter  # privacy-ok: 'GREEN 100' status text, not a course code
(`ok=|ALL GREEN|NOT GREEN`) removed the LEAK line from every run. A red that is always red trains
the reader to skip it. That is the failure the widening fixes.

WHAT THIS FILE PROTECTS. Widening a detector reduces what it catches, which is the direction that
needs a guard rather than an argument. So: plant a file in `.claude/state` whose name is NOT on the
noise list, run the checker's own digest function, and require the digest to MOVE. If a future edit
broadens `_STATE_NOISE` far enough to cover a real leak — a bare `.*`, a stray `|.*log`, an
accidental anchor removal — this goes red.

It asserts the digest FUNCTION, not a full suite run: a full run inside a test would recurse, and
the property under test is "does the digest respond to an unlisted file", which needs no tests to
execute.

LEAVES THE SEAT AS FOUND — the planted file is removed in a finally, and the test refuses to run at
all if a file by that name already exists, rather than deleting something it did not create.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "bin" / "tests" / "run-all.sh"
STATE = REPO / ".claude" / "state"
PLANT = STATE / ".t034-leak-probe-DELETE-ME"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _digest() -> str:
    """Run the runner's OWN _state_digest definition — extracted, never reimplemented.

    Sourcing run-all.sh is not an option: it has no source-only mode and would execute the whole
    suite (and recurse into this file). So the REAL text of `_STATE_NOISE` and `_state_digest` is
    lifted out of the script and executed verbatim.

    That distinction matters. A hand-written copy of the digest here would reproduce none of the
    runner's filters and would drift silently the first time the original changed — the
    copy-vs-shipped defect, committed inside the file that exists to guard the original. Extraction
    fails loudly instead: if either block is renamed, the regex below finds nothing and the test
    says so rather than testing a stale duplicate.
    """
    src = RUNNER.read_text()
    noise = re.search(r"^_STATE_NOISE=.*$", src, re.M)
    body = re.search(r"^_state_digest\(\) \{.*?^\}", src, re.M | re.S)
    if not (noise and body):
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(noise.group(0) + "\n" + body.group(0) + "\n_state_digest\n")
        tmp = fh.name
    try:
        r = subprocess.run(["bash", tmp], cwd=str(REPO),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout or "").strip().splitlines()
        return out[-1] if out else ""
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> int:
    print("test_state_leak_still_detected")
    if not RUNNER.is_file():
        print(f"  FAIL  {RUNNER} missing")
        return 1

    src = RUNNER.read_text()
    m = re.search(r"^_STATE_NOISE='([^']*)'", src, re.M)
    check("the noise list is a findable, anchored pattern", m is not None,
          "could not locate _STATE_NOISE — a future rename would make this whole file vacuous")
    if not m:
        return 1
    noise = m.group(1)

    # A pattern that matches everything would silence the detector completely and still let the
    # planted-file check below pass if the plant happened to be excluded. Assert the shape first.
    check("the noise list is not a catch-all",
          noise.strip() not in (".*", "^.*$", "") and noise.endswith("$"),
          f"_STATE_NOISE={noise!r} — an unanchored or universal pattern disables the leak check "
          f"entirely while leaving it looking configured")

    d0 = _digest()
    check("the runner's own _state_digest is callable and returns a hash",
          bool(d0) and d0 != "-" and len(d0) >= 32,
          f"got {d0!r} — if the digest cannot be computed this test proves nothing about it")
    if not d0 or d0 == "-":
        return 1

    if PLANT.exists():
        print(f"  FAIL  {PLANT.name} already exists — refusing to plant over a file I did not create")
        return 1

    try:
        PLANT.write_text("planted by test_state_leak_still_detected\n")
        d1 = _digest()
        check("THE DOSE: a file NOT on the noise list moves the digest (leak still detectable)",
              d1 != d0,
              f"digest unchanged ({d0[:16]}...) after planting {PLANT.name}. The noise list has been "
              f"widened far enough to hide a real leak, or the digest stopped reading .claude/state. "
              f"Either way a test writing into the live seat would now go unreported.")
    finally:
        PLANT.unlink(missing_ok=True)

    d2 = _digest()
    check("removing the plant restores the original digest (the check is symmetric)",
          d2 == d0,
          f"before {d0[:16]}... after-cleanup {d2[:16]}... — this test left the seat changed, which "
          f"is the exact offence it exists to detect")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
