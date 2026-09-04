#!/usr/bin/env python3
"""bin/repartition-hubs.py's brain-lock path MUST be byte-identical to run-brain-update.sh's.

WHY THIS EXISTS (2026-09-02, core-ops bus #5870). repartition-hubs.py writes entities and
entity_edges across every org as brain_admin with no lock, while every embed path serialises on a
mkdir mutex at `/tmp/core-brain-<md5($CORE_BRAIN)>.lock` — one path, shared, taken in
`.claude/hooks/run-brain-update.sh`. A first attempt at adding a lock to repartition-hubs.py derived
a DIFFERENT path for the SAME $CORE_BRAIN:

    shell  (run-brain-update.sh):  /tmp/core-brain-77d26ddb4e9c72dbb153397105ea500d.lock
    python (repartition-hubs.py):  /tmp/core-brain-4e0b284d0af35bc7088fa39570bc1c37.lock

ROOT CAUSE, proved by this file: the shell hashes `$CORE_BRAIN` PIPED THROUGH `echo`, which
appends a trailing newline (`echo "$X" | md5 -q` hashes "$X\n", not "$X"). The python side hashed
`str(Path(brain))` with no newline. A second, independent divergence exists for any CORE_BRAIN with
a trailing slash: `str(Path("/x/"))` == "/x" — pathlib silently drops it — while bash's `echo`
does not, so a Path-normalized hash and a raw-string hash disagree even before the newline is
considered. A lock on a path nothing else derives serialises against NOTHING. It is worse than no
lock, because it looks safe.

THIS FILE DOES NOT REIMPLEMENT THE SHELL'S DERIVATION. It extracts the live `BRAIN_HASH=...` line
out of run-brain-update.sh with a regex and executes THAT line in a real bash subprocess. A
reimplementation would silently re-diverge the moment either side's derivation changes; running the
shell's own line is the only form that catches that.

Run: python3 bin/tests/test_brain_lock_path_matches_shell.py
"""
from __future__ import annotations

import importlib.util
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SHELL_SCRIPT = ROOT / ".claude" / "hooks" / "run-brain-update.sh"
PY_SCRIPT = ROOT / "bin" / "repartition-hubs.py"

passes: list[str] = []
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  -> ' + detail}")


def load_shell_hash_line(script: Path) -> str:
    """Pull the exact `BRAIN_HASH=...` assignment out of run-brain-update.sh.

    Failing to find it is itself a finding worth reporting loudly (the derivation moved or was
    renamed) rather than silently falling back to a guess.
    """
    text = script.read_text()
    m = re.search(r"^BRAIN_HASH=.*$", text, re.MULTILINE)
    if not m:
        sys.exit(f"FATAL: could not find a BRAIN_HASH= line in {script} — "
                  "has the derivation moved or been renamed? Update this test's regex.")
    return m.group(0)


def shell_lock_path(hash_line: str, value: str) -> str:
    """Compute the lock path the SHELL would use for CORE_BRAIN=value, by running the shell's
    own extracted line — not a reimplementation of it."""
    script = f'CORE_BRAIN={shlex.quote(value)}\n{hash_line}\nprintf "%s" "/tmp/core-brain-${{BRAIN_HASH}}.lock"'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        sys.exit(f"FATAL: shell derivation subprocess failed: {r.stderr}")
    return r.stdout.strip()


def load_python_module(path: Path):
    """Load bin/repartition-hubs.py as a module. Hyphenated filename, so plain `import` can't
    reach it — same pattern as bin/tests/test_hub_partition_pairs.py."""
    spec = importlib.util.spec_from_file_location("repartition_hubs_under_test", str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> int:
    print("=== _brain_lock_path matches run-brain-update.sh's shell derivation ===\n")

    hash_line = load_shell_hash_line(SHELL_SCRIPT)
    print(f"  (shell line under test: {hash_line})\n")

    rph = load_python_module(PY_SCRIPT)

    real_core_brain = f"{Path.home()}/AI Projects/core-brain"
    cases = [
        ("real $CORE_BRAIN value", real_core_brain),
        ("value with a trailing slash", real_core_brain + "/"),
        ("value containing a space", f"{Path.home()}/AI Projects/le brain test dir"),
    ]

    for label, value in cases:
        want = shell_lock_path(hash_line, value)
        got = str(rph._brain_lock_path(value))
        check(f"{label}: {value!r}", got == want, f"shell={want} python={got}")

    # Pin the actual regression concretely, not just "matches the shell today": the ORIGINAL bug
    # was hashing str(Path(brain)) with no trailing newline (the naive, no-`echo` port). Compute
    # that specific wrong formula independently (not by calling into rph — this is describing the
    # bug, not the fix) and assert the real function no longer produces it, for whatever
    # $CORE_BRAIN this machine actually has (no hardcoded path, so this holds on every seat/fork).
    import hashlib
    old_buggy_digest = hashlib.md5(real_core_brain.encode()).hexdigest()
    old_buggy_path = f"/tmp/core-brain-{old_buggy_digest}.lock"
    got_real = str(rph._brain_lock_path(real_core_brain))
    check("real $CORE_BRAIN no longer hashes via the old (buggy, no-newline) formula",
          got_real != old_buggy_path, f"got={got_real} old-buggy-would-be={old_buggy_path}")

    print(f"\n=== Results: {len(passes)} passed, {len(failures)} failed ===")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
