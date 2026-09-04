#!/usr/bin/env python3
"""A DIGEST THE REVIEWER CANNOT RECOMPUTE IS A NUMBER TAKEN ON FAITH.

The peer-approval protocol binds an APPROVE to a content digest so a tree that moves between review
and push is refused. That half works — `sentinel-approve.sh` recomputes at mint time and has
genuinely refused a drifted push.

But it protects the PRODUCER, not the reviewer. core-business tried to verify the digest twice and
both times got `e3b0c442...` — the sha256 of the EMPTY STRING — because `git diff --cached --raw` is
only non-empty inside the transient staging state on the producer's machine. By the time a reviewer
looks, the staging area is gone.

    "The reviewer is asked to approve a hash that only exists inside a transient staging state on
     the producer's machine. This is not carelessness on your part; the protocol cannot work as
     written."

So `--check` can now emit the exact sorted lines the hash is taken over (SHOW_DIGEST_INPUT=1), and
a reviewer derives the number instead of trusting it.

THE PROPERTY UNDER TEST is that the emitted input actually hashes to the emitted digest. If those
two ever diverge, the reviewer's independent check would start failing on honest pushes — and a
verification step that cries wolf is one that gets skipped, which is worse than the faith it
replaced.

Run: python3 bin/tests/test_digest_is_verifiable.py
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SYNC = ROOT / "bin" / "sync-to-baseline.sh"


def main() -> int:
    p = f = u = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    def undec(label, detail):
        nonlocal u
        u += 1
        print(f"  UNDEC  {label}\n          {detail}")

    print("=== the content digest must be independently derivable ===\n")

    if not SYNC.is_file():
        print("  SKIP — sync-to-baseline.sh absent on this Core")
        return 0

    src = SYNC.read_text()
    # IS THIS SEAT EVEN ABLE TO REACH THE DIGEST CODE PATH? Measured 2026-09-01, core-business:
    # `SHOW_DIGEST_INPUT=1 bash bin/sync-to-baseline.sh --check` printed nothing but "REFUSED —
    # 'core-business' is not the baseline writer ('core-life'). ... this Core is pull-only by
    # policy." and exited 4 — Safeguard 6's single-writer policy (sync-to-baseline.sh:72-83), which
    # fires BEFORE `CHECK_ONLY` is ever consulted. No DIGEST line, no DIGEST-INPUT block, on ANY
    # run, by design: a pull-only Core computing a push digest at all would be the trust violation,
    # not the fix. This is the exact same shape as test_wilson_ci_known_answers.py's per_core_keep
    # ABSTAIN, one gate lower: there the FILE is absent on a puller; here the file is present but its
    # role check refuses to run past line 81 for every seat except the one named in
    # bin/sync-manifest.json's baseline_writer. Read here, not hardcoded — CORE_NAME in the script
    # is `basename($CORE_DIR)`, so this mirrors it rather than restating "core-life".
    try:
        _manifest = json.loads((ROOT / "bin" / "sync-manifest.json").read_text())
        _writer = _manifest.get("baseline_writer")
    except Exception:
        _writer = None
    IS_WRITER = _writer is not None and ROOT.name == _writer
    check("--check can emit the digest INPUT, not only the digest",
          "SHOW_DIGEST_INPUT" in src and "DIGEST-INPUT-BEGIN" in src,
          "without the inputs a reviewer can only take the hash on faith")
    check("the digest is computed FROM the captured input, not independently of it",
          "SYNC_DIGEST_INPUT" in src and 'printf' in src.split("SYNC_DIGEST=")[1][:120],
          "if the two are computed separately they can silently diverge")

    print("\n--- THE DOSE: run it and derive the number the way a reviewer would ---")
    # LEAVE THE SEAT AS FOUND. --check clones the baseline over the network and appends to
    # .claude/state/.sync-failures when that clone fails — so this test writes into live state on
    # exactly the runs where it is least able to conclude anything. Saved and restored, the same
    # obligation the gate test has for .sentinel-last-blocked.
    _sf = ROOT / ".claude" / "state" / ".sync-failures"
    _saved_sf = _sf.read_bytes() if _sf.is_file() else None
    # THE DOSE MUST NOT DEPEND ON THE SEAT'S SYNC STATE. Until 2026-08-12 this derived the digest
    # from whatever shared changes happened to be pending, so it proved the recipe on a seat with
    # unpushed work and proved NOTHING on a clean one — and it FAILED rather than said so, because
    # the empty case was handled for the derive step below but not for the two checks above it.
    # Pushing the baseline made the pending set empty and turned this red, which is the test
    # reporting on the repo's sync status rather than on the code it names.
    #
    # So: manufacture one pending change. A file under a shared dir is enough — verified that
    # --check reports it and emits a real DIGEST + input block for it. Removed in the finally,
    # and swept at the top of the run in case a previous kill left one behind.
    probe = ROOT / "bin" / ".digest-selftest-probe.txt"
    for stray in (ROOT / "bin").glob(".digest-selftest-probe*"):
        stray.unlink()                       # a kill mid-run must not poison the next one
    env = dict(os.environ, SHOW_DIGEST_INPUT="1")
    try:
        probe.write_text("digest selftest probe — transient; see bin/tests/test_digest_is_verifiable.py\n")
        r = subprocess.run(["bash", str(SYNC), "--check"], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, timeout=900)
        out = r.stdout + r.stderr
    finally:
        if probe.exists():
            probe.unlink()

    claimed = ""
    for line in out.splitlines():
        if line.startswith("DIGEST:"):
            claimed = line.split(":", 1)[1].strip()

    body, inside = [], False
    for line in out.splitlines():
        if line.strip() == "DIGEST-INPUT-BEGIN":
            inside = True
            continue
        if line.strip() == "DIGEST-INPUT-END":
            inside = False
            continue
        if inside:
            body.append(line)

    if not IS_WRITER:
        # A hard FAIL here would blame this test's own dose for a refusal the trust boundary is
        # SUPPOSED to produce — the "no ask clears MIN_PRE_N" wrong-alarm shape one gate lower. This
        # seat cannot supply a digest via this mechanism, ever, by design; that is not a defect to
        # chase on core-business, and it is not something the shared script should stop doing.
        undec("a digest is emitted at all",
              f"got {claimed!r} — this seat ({ROOT.name}) is pull-only; sync-to-baseline.sh refuses "
              f"before computing SYNC_DIGEST at all (Safeguard 6). Not a pass: the recipe under test "
              f"never ran here.")
        undec("the input block is emitted when asked for",
              "same refusal — no DIGEST-INPUT block is reachable from a non-writer seat")
        undec("the probe produced a derivable input block",
              "same refusal — the probe cannot exercise a code path this seat's role never reaches")
    else:
        check("a digest is emitted at all", len(claimed) == 64, "got %r" % claimed[:20])
        check("the input block is emitted when asked for", inside is False and body != [] or claimed,
              "no DIGEST-INPUT block found in --check output")

        if body:
            derived = hashlib.sha256(("\n".join(body) + "\n").encode()).hexdigest()
            check("the emitted input hashes to the emitted digest", derived == claimed,
                  "claimed %s\n          derived %s\n          A reviewer following the documented "
                  "recipe would report a false mismatch." % (claimed[:24], derived[:24]))
        else:
            # No longer reachable via "the seat happens to be clean" — the probe guarantees one
            # pending change. If it IS reached, --check stopped reporting a manufactured change,
            # which is a real regression in the thing under test, so it fails instead of narrating.
            check("the probe produced a derivable input block", False,
                  "a file was created under a shared path and --check emitted no DIGEST-INPUT for "
                  "it. Either --check no longer reports added files, or the probe path stopped "
                  "being inside a synced dir. Both make the digest unverifiable by the reviewer it "
                  "exists for.")

    print("\n--- and the flag stays OFF by default, asserted from SOURCE, not a second clone ---")
    # NO SECOND NETWORK CALL. The first version ran --check twice, and the second run FAILED to
    # clone the baseline ("SSL certificate problem: self signed certificate") — which appended a
    # row to .claude/state/.sync-failures, so the test wrote into the seat it was measuring, and its
    # two assertions passed on a run that never produced a digest at all. A vacuous pass from a
    # failed network call, inside the file whose subject is "can this number be trusted".
    #
    # The flag-off property is structural and readable: the emission is inside a conditional, and
    # the DIGEST line is outside it.
    # No character window. The first version sliced [:600] after the DIGEST line and the explanatory
    # comment pushed the `if` past it, so the check failed on correct code — an arbitrary constant
    # deciding a structural question.
    guarded = src.split('echo "DIGEST: $SYNC_DIGEST"')[1]
    # MATCH THE CODE, NOT THE DOCUMENTATION. The first version compared against the bare marker
    # "DIGEST-INPUT-BEGIN", and the usage comment above the conditional contains that marker inside
    # a sed recipe — so index() found the COMMENT, which precedes the `if`, and the check failed on
    # correct code. The matcher was reading the prose that describes the mechanism instead of the
    # mechanism. Anchor on the emitting statement itself.
    _emit = 'echo "DIGEST-INPUT-BEGIN"'
    check("the input block is emitted only inside a SHOW_DIGEST_INPUT conditional",
          'if [[ "${SHOW_DIGEST_INPUT:-0}" == "1" ]]' in guarded
          and _emit in guarded
          and guarded.index('if [[ "${SHOW_DIGEST_INPUT') < guarded.index(_emit),
          "routine --check output would grow by one line per changed file")
    check("...and the DIGEST line sits OUTSIDE that conditional",
          src.index('echo "DIGEST: $SYNC_DIGEST"') < src.index('SHOW_DIGEST_INPUT:-0'),
          "sentinel-approve.sh parses this back; it must not depend on a display flag")

    if _saved_sf is not None:
        _sf.write_bytes(_saved_sf)
    elif _sf.is_file():
        _sf.unlink()

    print("\n=== Results: %d passed, %d failed%s ===" % (p, f, f", {u} undecidable" if u else ""))
    if f:
        return 1
    if u:
        # rc=2 + UNDECIDABLE — the run-all.sh ABSTAIN contract, same shape as
        # test_wilson_ci_known_answers.py: a real FAIL never launders into this.
        print(f"\n  UNDECIDABLE  {u} of {p + f + u} checks could not run: this seat ('{ROOT.name}') "
              f"is not the baseline writer ('{_writer}'), so sync-to-baseline.sh refuses before a "
              f"digest is ever computed. Not a pass: this suite cannot verify a digest it was never "
              f"given. On the writer, every check above runs for real.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
