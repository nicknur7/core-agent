#!/usr/bin/env python3
"""MOVING THE TRUST ROOT MUST REQUIRE REVIEW — the restriction was documented and unenforced.

`bin/trajectory-gate.py`'s own header has claimed, for as long as the file has existed, that
promotion is "Nick/Sentinel only". `pretooluse-guard.sh` matched nothing of the sort.

core-business found the consequence by running it (bus #1015): promote to a candidate, then grade
that candidate, and the candidate is its own grader — using only the SANCTIONED command, with
nothing tampered and the gate still printing verdicts. Promotion skips the re-exec by design; it
must, or the tool could never bootstrap a root.

The gate now refuses to GRADE when trusted == candidate, which closes the consequence. This closes
the ACTION. Both are wanted: one stops the state being reachable by accident, the other stops it
being reached at all without a second pair of eyes.

"Documented but unenforced" is the exact class CLAUDE.base.md names — *a rule that tells you a gate
will catch you when no gate runs is worse than silence, because you relax against a net that was
taken down.*

TWO THINGS THIS FILE DOES ON PURPOSE, both learned the hard way within ten minutes of writing it:

  PAYLOADS ARE ASSEMBLED AT RUNTIME. The first dose put them in a shell command and the guard
  BLOCKED THE TEST — correctly, because the command string carried the real pattern. Evidence the
  matcher works, and unusable as a test. Same lesson as de-naming Core paths in fixtures.

  AND THE GUARD CANNOT TELL RUNNING FROM QUOTING. Writing this very file with a heredoc was blocked
  too, because the heredoc body quoted the invocation. That is the matched-prose-as-code shape
  core-business hit with its own substring gate, and here it is ACCEPTED rather than fixed: the
  guard scans raw command text, a heredoc body IS command text, and narrowing the pattern to a
  command position would not separate them either. The friction is real, it is narrow (it bites only
  when writing text that quotes the invocation), and it errs toward blocking. Use the Write tool.

Run: python3 bin/tests/test_promote_is_gated.py
"""
import json
import tempfile
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import core_root  # noqa: E402

REPO = str(core_root())
GUARD = os.path.join(REPO, ".claude/hooks/pretooluse-guard.sh")

# Assembled at runtime so this file's own text does not carry the literal gated string.
GATE = "trajectory-gate" + ".py"
PROMOTE = "--" + "promote"

CASES = [
    ("promotion MUST be gated",            "python3 bin/%s %s abc123" % (GATE, PROMOTE), True),
    ("--status must NOT be gated",         "python3 bin/%s --status" % GATE,             False),
    ("--candidate must NOT be gated",      "python3 bin/%s --candidate HEAD" % GATE,     False),
    ("prose mentioning it is NOT gated",   "echo discuss the promotion step later",      False),
    ("another tool's flag is NOT it",      "python3 bin/other-tool.py %s x" % PROMOTE,   False),
]


# REDIRECT THE SEAT BEFORE TOUCHING THE SUBJECT. Neither of these tests writes to .claude/state
# itself — the guard and the friction modules they exercise do, resolving their root from
# CORE_INSTANCE and falling back to their own repo root when it is unset. So running this suite
# appended to the live seat's telemetry and left a bypass-shaped fixture in .sentinel-last-blocked,
# which core-business once read back as an attack by the Core under review. Caught by run-all.sh's
# state-digest check on its first full run.
_SEAT_TMP = tempfile.mkdtemp(prefix="promote-gate-seat-")
_SEAT_ENV = dict(os.environ, CORE_INSTANCE=_SEAT_TMP, CLAUDE_PROJECT_DIR=_SEAT_TMP)


def blocked(cmd):
    p = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    r = subprocess.run(["bash", GUARD], input=p, text=True, capture_output=True,
                       cwd=REPO, timeout=60, env=_SEAT_ENV)
    out = (r.stdout or "") + (r.stderr or "")
    return "ACTION BLOCKED" in out or r.returncode == 2


def main():
    ok = True
    print("=== trust-root promotion must be gated ===\n")
    for label, cmd, want in CASES:
        got = blocked(cmd)
        good = got == want
        ok &= good
        print("  %-38s blocked=%-5s want=%-5s %s" % (label, got, want, "ok" if good else "WRONG"))

    # THE DOSE. Without a case that must NOT block, "gated" is satisfied by a guard that blocks
    # everything — which is indistinguishable from a broken guard until someone tries to work.
    print("\n  (the four NOT-gated rows are the dose: a guard that blocks everything would fail them)")
    print("\n%s" % ("ALL DIRECTIONS PASS" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
