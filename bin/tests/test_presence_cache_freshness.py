#!/usr/bin/env python3
"""A FRESH-LOOKING CACHE SERVING NINE-HOUR-OLD PEER STATE.

`session-presence.py` supplies the PEERS line — every Core's HEAD and the baseline it last synced.
It exists because the cross-core claim gate was RETIRED and replaced by supply: the line is there so
a Core can answer "is everyone synced" from context instead of guessing.

It cached that line for 600s, keyed on `digest_f.stat().st_mtime`. An mtime says when a file was
last TOUCHED, not when the numbers inside it were computed.

CAUGHT LIVE, 2026-08-11. The digest was 407 seconds old by mtime — comfortably inside the window —
and reported:

    business  baseline:1a2a69e        <- two baselines behind
    business's own marker file        6f36da2, unchanged since 01:20 that morning

A forced recompute returned the correct value immediately, so the computation was never wrong. The
cache was serving stale content behind a fresh timestamp, and the stale answer was the one that
would have changed a decision: it said a peer was behind when it was current.

THE FIX IS THAT FRESHNESS TRAVELS INSIDE THE CONTENT. `computed_at=<epoch>` is the first line of the
digest, so touching the file cannot make old numbers look new. An absent or unparsable stamp counts
as NOT fresh — recomputing costs ~93ms and believing a wrong peer state costs a wrong decision about
the fleet.

THE LAST CHECK IS THE ONE THAT MATTERS. Freshness is not correctness: a cache that recomputes on
schedule and computes the wrong thing passes every check above. So the emitted line is compared
against a direct read of each peer's marker file.

Run: python3 bin/tests/test_presence_cache_freshness.py
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root, core_name  # noqa: E402

ROOT = core_root()
SELF = core_name()
# THE FULL FLEET, SELF EXCLUDED — DERIVED, NOT A LIFE-CENTRIC HARDCODE. This loop used to iterate
# the literal tuple ("business", "school", "finance", "ops") — life's OWN peer list — no matter
# which seat ran the shared test. Found core-business, 2026-09-01: running from business, the loop
# still asked for a "business" peer, i.e. business checking whether ITS OWN baseline appears in
# the PEERS line as one of its OWN peers. session-presence.py correctly never lists a Core as its
# own peer, so that comparison fails by construction on every non-life seat — not a real digest
# defect, a test that only ever built the peer set life would have. Same class as the hardcoded
# org_id=1 default flagged the same night in compile-truth-refresh.py: a constant that quietly
# meant "life" wherever resolution should have been derived.
ALL_CORES = ("life", "business", "school", "finance", "ops")
PEERS_OF_SELF = tuple(n for n in ALL_CORES if n != SELF)
HOOK = ROOT / ".claude" / "hooks" / "session-presence.py"
DIGEST = ROOT / ".claude" / "state" / ".peer-digest"


def run_hook():
    r = subprocess.run(["python3", str(HOOK)], input='{"prompt":"x"}',
                       capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception:
        return r.stdout + r.stderr


def peers_line(text):
    for ln in (text or "").splitlines():
        if ln.startswith("PEERS"):
            return ln
    return ""


def main() -> int:
    p = f = 0
    abstain = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== the peers line must not be fresh-looking and stale ===\n")

    if not HOOK.is_file():
        print("  SKIP — session-presence.py absent")
        return 0

    # LEAVE THE SEAT AS FOUND. Running the real hook also rewrites .last-activity, which is what
    # computes the "step away detected" gap — a test run bumping it would suppress a genuine
    # away-notice for Nick. Same obligation test_trajectory_gate.sh has for .sentinel-last-blocked:
    # a test that must exercise the real thing in place cleans up after itself rather than being
    # excused into the digest's noise list.
    #
    # This restore was written once, reported clean, and was NOT IN THE FILE on the next read —
    # so it is re-added and grep-verified rather than trusted. An edit that says it applied and did  # privacy-ok: generic engineering vocabulary
    # not is the say-do gap in the tooling instead of the prose.
    ACTIVITY = ROOT / ".claude" / "state" / ".last-activity"
    saved_activity = ACTIVITY.read_bytes() if ACTIVITY.exists() else None
    saved = DIGEST.read_text() if DIGEST.exists() else None
    try:
        print("--- the stamp lives in the CONTENT, not the filesystem ---")
        DIGEST.unlink(missing_ok=True)
        first = peers_line(run_hook())
        check("a run with no cache emits a PEERS line", first.startswith("PEERS"), first[:120])
        check("...and writes a computed_at stamp as line 1",
              DIGEST.exists() and DIGEST.read_text().startswith("computed_at="),
              (DIGEST.read_text()[:60] if DIGEST.exists() else "<no digest>"))

        print("\n--- THE DOSE: old content behind a fresh mtime must be REFUSED ---")
        raw = DIGEST.read_text()
        body = raw.split("\n", 1)[1]
        poisoned = body.replace("baseline:", "baseline:DEADBEE", 1)
        DIGEST.write_text("computed_at=%d\n%s" % (int(time.time()) - 4000, poisoned))
        os.utime(DIGEST, None)                     # mtime says NOW; content says 4000s ago
        line = peers_line(run_hook())
        check("the poisoned cache is not served", "DEADBEE" not in line, line[:140])
        check("...and the digest was rewritten with a current stamp",
              int(DIGEST.read_text().split("\n")[0].split("=")[1]) > int(time.time()) - 120)

        print("\n--- a FRESH stamp is still honoured, or the cache is pointless ---")
        raw = DIGEST.read_text()
        marked = raw.split("\n", 1)[1].replace("PEERS", "PEERS_CACHED", 1)
        DIGEST.write_text("computed_at=%d\n%s" % (int(time.time()), marked))
        out = run_hook()
        check("a genuinely fresh digest is reused rather than recomputed",
              "PEERS_CACHED" in out, "recomputing every turn costs ~93ms of git calls per prompt")

        print("\n--- an unstamped or corrupt digest fails toward recomputing ---")
        # SEAT-NEUTRAL. This said "this Core is life" and ships to every Core and to a fork — the same
        # hardcoded-seat class sentinel-code caught in test_quarantine_is_durable.py before the
        # last push, surviving here only because nobody was looking. core-finance found it while
        # reviewing a leak-detector widening. The payload is garbage BY DESIGN; it should not also
        # name somebody else's seat.
        DIGEST.write_text("PEERS (this Core is <seat>): garbage@0000000 baseline:0000000")
        line = peers_line(run_hook())
        check("a digest with no stamp is not trusted", "garbage@" not in line, line[:120])

        print("\n--- FRESHNESS IS NOT CORRECTNESS: check the value against the peers' own files ---")
        DIGEST.unlink(missing_ok=True)
        line = peers_line(run_hook())
        checked = 0
        for name in PEERS_OF_SELF:
            d = ROOT.parent / ("core-" + name)
            marker = d / ".claude" / "state" / ".last-baseline-sync"
            if not marker.is_file():
                continue
            m = re.findall(r"baseline=([0-9a-f]{7,8})", marker.read_text())
            if not m:
                continue
            want = m[-1][:7]
            checked += 1
            check("%s's baseline in the line matches its own marker file (%s)" % (name, want),
                  ("%s@" % name) in line and want in line.split("%s@" % name)[1][:40],
                  "line said: %s" % line.split("%s@" % name)[1][:40] if ("%s@" % name) in line
                  else "peer missing from the line entirely")
        if checked >= 2:
            check("at least two peers were actually compared", True)
        else:
            # NOT A CODE PROPERTY — A FIXTURE. This sweep needs sibling core-<name>/.claude/state/
            # .last-baseline-sync marker files on disk, written by a real Core that has actually
            # synced against the baseline. A standalone clone (a fork, a CI checkout, this suite's
            # own audit tooling run from /tmp) has no sibling Cores at all, so `checked` is
            # unavoidably 0 — not evidence the correctness check above is broken, just that this
            # seat cannot supply the fixture it needs. Every check above ran against the real hook
            # for real and is unaffected.
            print("  UNDEC  at least two peers were actually compared\n"
                  "          only %d — no sibling core-*/.claude/state/.last-baseline-sync marker "
                  "files on this seat to compare against, not a broken sweep" % checked)
            abstain += 1
    finally:
        if saved_activity is not None:
            ACTIVITY.write_bytes(saved_activity)
        elif ACTIVITY.exists():
            ACTIVITY.unlink()
        if saved is not None:
            DIGEST.write_text(saved)
        else:
            DIGEST.unlink(missing_ok=True)

    print("\n=== Results: %d passed, %d failed%s ===" % (
          p, f, ", %d undecidable" % abstain if abstain else ""))
    if f:
        return 1
    if abstain:
        # rc=2 + UNDECIDABLE, the run-all.sh ABSTAIN contract (test_wilson_ci_known_answers.py is
        # the precedent this copies): every real check above ran against the live hook; only the
        # peer-marker-file fixture is missing on this standalone seat.
        print("\n  UNDECIDABLE  %d check(s) could not run on this seat (no sibling Core on disk "
              "with a baseline-sync marker to compare against). Not a pass." % abstain)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
