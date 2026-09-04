"""Refuse pytest on this directory, loudly, instead of reporting green from zero assertions.

WHY THIS FILE EXISTS. core-school pulled `c7d0e37`, ran the new tests correctly, and then checked
how they behave under the runner whose naming convention they appear to follow:

    python3 -m pytest bin/tests/test_trust_is_evidence_seeded.py -q
      no tests ran in 0.07s          exit status 0

    python3 bin/tests/test_trust_is_evidence_seeded.py
      ALL PASS

**"no tests ran" is not "passed", and it exits ZERO.** Every file in this directory is a plain
script — 147 of 148 have no pytest-discoverable `def test_*`, because `run-all.sh` invokes them
directly and reads their exit codes. But the filenames are `test_*.py`, so pytest is exactly what a
person reaches for. Point it here and you get a green light from a run that never executed a single
assertion.

That is the same failure class as the psycopg2 fail-open found the same day — thirteen DB steps
logging "skipped" while the close reported success — except one level worse, because it is in the
layer whose entire job is catching that. A suite that cannot run is indistinguishable from a suite
that passed. school's line: *"the thing that verifies the fix, silently not running."*

THE FIX IS NOT TO CONVERT 148 FILES. The script convention is deliberate — these tests do real I/O,
spawn subprocesses, hit Postgres, and are read by `run-all.sh` for exit status, MUTE/LEAK detection
and per-file timing that pytest would not reproduce. Rewriting them to satisfy a runner nobody uses
would be a large change made for a tool, not for a reason.

So: make the wrong invocation IMPOSSIBLE TO MISREAD. `pytest.UsageError` exits non-zero with the
message below, so anyone who reaches for pytest is told what to run instead, immediately, rather
than being handed a false pass.

Run the suite with:  bash bin/tests/run-all.sh
Run one test with:   python3 bin/tests/<name>.py
"""


def pytest_configure(config):
    import pytest
    raise pytest.UsageError(
        "\n"
        "  ┌────────────────────────────────────────────────────────────────────────────┐\n"
        "  │  pytest CANNOT run this suite, and would have reported a FALSE PASS.       │\n"
        "  └────────────────────────────────────────────────────────────────────────────┘\n"
        "\n"
        "  These files are plain scripts, not pytest modules. 147 of 148 have no\n"
        "  `def test_*`, so pytest collects nothing, prints 'no tests ran', and EXITS 0.\n"
        "  A green light from zero assertions is worse than a red one.\n"
        "\n"
        "  Run the whole suite:   bash bin/tests/run-all.sh\n"
        "  Run a single test:     python3 bin/tests/<name>.py\n"
        "\n"
        "  run-all.sh reads each file's exit code and also detects MUTE (a file that\n"
        "  executed but demonstrated no check ran) and LEAK — neither of which pytest\n"
        "  would reproduce here.\n"
        "\n"
        "  Found by core-school on 2026-08-26, after pulling c7d0e37 and testing how the\n"
        "  verification layer itself behaves rather than trusting that it ran.\n"
    )
