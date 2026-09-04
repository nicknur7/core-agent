#!/usr/bin/env python3
"""EVERY CLAIM THE PROBE MAKES MUST BE ABLE TO GO RED, or it is decoration.

`bin/observation-probe.py` re-derives the observation-log figures quoted in this session's commit
messages — the 1.6x chunk inflation, the duration_claim position bias before and after collapsing.
It exists because core-business turned my own point back on me (bus #978): *"a number whose query
lives nowhere is a citation to a conversation."* Mine were in commit messages, which read as more
durable than a research note and are harder to correct.

But a probe that has only ever been observed PASSING is exactly the shape this whole day has been
about. All three of its structural claims pass on the live log, and would also pass if the checks
were `return True`. So each is planted here as a log that must make that specific claim fail, and
only that one.

    inflation > 1.0            planted: every claim appears in exactly one chunk -> 1.0x, must FAIL
    |gap after| < |gap before| planted: collapsing makes the gap WIDER, must FAIL
    non-empty log              planted: an empty file, must REFUSE with exit 2 rather than report

Run: python3 bin/tests/test_observation_probe.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
PROBE = ROOT / "bin" / "observation-probe.py"


def row(turn, kind, matched, index, sourced):
    return {"turn": turn, "kind": kind, "matched": matched, "index": index,
            "sourced": sourced, "final": True, "session": "s", "ts": 1786400000}


def write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def run(log):
    r = subprocess.run([sys.executable, str(PROBE), "--log", str(log)],
                       capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr, r.returncode


def healthy_rows():
    """Inflation above 1.0 AND collapsing narrows the gap — the shape the live log has."""
    out = []
    # 12 claims spanning 3 chunks each, evidence late -> raw index-0 looks unsourced, collapse fixes
    for c in range(12):
        for i in range(3):
            out.append(row("t%d" % c, "duration_claim", "about 3 hours", i, i == 2))
    # 6 single-chunk claims, sourced -> gives the only-index-0 bucket a high rate after collapsing
    for c in range(12, 18):
        out.append(row("t%d" % c, "duration_claim", "about 3 hours", 0, True))
    return out


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== observation-probe claims must be falsifiable ===\n")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        print("--- the baseline: a healthy log must PASS (or the reds below prove nothing) ---")
        good = d / "good.jsonl"
        write(good, healthy_rows())
        out, rc = run(good)
        check("healthy log exits 0", rc == 0, out[-700:])
        check("...and reports inflation above 1.0", "inflation > 1.0" in out and "PASS  inflation" in out,
              out[-500:])

        print("\n--- claim 1: no inflation must go RED ---")
        flat = d / "flat.jsonl"
        # every claim in exactly one chunk -> raw rows == distinct claims -> 1.00x
        write(flat, [row("t%d" % c, "duration_claim", "about 3 hours", 0, c % 2 == 0)
                     for c in range(20)])
        out, rc = run(flat)
        check("1.00x inflation exits nonzero", rc != 0, out[-500:])
        check("...and names THAT claim as the failure", "FAIL  inflation" in out, out[-500:])

        print("\n--- claim 2: a WIDENING gap must go RED ---")
        wide = d / "wide.jsonl"
        rows = []
        # multi-chunk claims all UNSOURCED, single-chunk claims all SOURCED -> collapsing widens
        for c in range(10):
            for i in range(3):
                rows.append(row("w%d" % c, "duration_claim", "about 3 hours", i, False))
        for c in range(10, 20):
            rows.append(row("w%d" % c, "duration_claim", "about 3 hours", 0, True))
        write(wide, rows)
        out, rc = run(wide)
        widened = "FAIL  |gap after|" in out
        check("a log where collapsing widens the gap is detected", widened, out[-700:])

        print("\n--- claim 3: an empty log must REFUSE, not report health ---")
        empty = d / "empty.jsonl"
        empty.write_text("")
        out, rc = run(empty)
        check("empty log exits 2", rc == 2, "rc=%d %s" % (rc, out[-300:]))
        check("...and says REFUSING rather than PASS", "REFUSING" in out and "=== PASS ===" not in out,
              out[-300:])

        print("\n--- and a bad --log target must refuse rather than silently read the live log ---")
        out, rc = run(d / "does-not-exist.jsonl")
        check("nonexistent --log refuses", "REFUSING" in out and rc != 0, "rc=%d %s" % (rc, out[-300:]))
        check("...and reports no figures at all", "inflation by class" not in out, out[-300:])

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
