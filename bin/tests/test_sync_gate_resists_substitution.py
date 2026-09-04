#!/usr/bin/env python3
"""The baseline-sync gate must survive command substitution, backticks, and heredocs.

WHY THIS EXISTS (2026-08-20). core-ops's sentinel-code found a live bypass: a gated sync could
execute while `pretooluse-guard.sh` reported clean. Three defects in series, and they had to be
found in this order because each one hid the next.

  1. `_POS_RX`'s trailing boundary class was `([[:space:]|;&"]|'|$)` — no `)`, no backtick. A
     tightly-closed `$( )` never RAISED the gate, so the tokenizer, the heredoc stripper and the
     whole withdrawal path were moot: there was no flag to withdraw. This was the real defect.

  2. `_strip_heredoc_bodies` captured the heredoc delimiter's quote character as group 1 and never
     read it, deciding on destination alone. So `cat > f <<EOF` counted as inert data although an
     unquoted body undergoes command substitution before reaching the file. Real, and fixed — but
     A/B showed it closed NOTHING on its own, because of (1).

  3. The confirming tokenizer's `punctuation_chars` omitted the backtick, so shlex read `` `bash ``
     as a word and left the script name carrying a trailing backtick. `is_sync()` missed it and
     `_sync_tokens_clean` "positively proved" no sync command existed — WITHDRAWING the gate that
     fixing (1) had just correctly raised. Invisible until (1) was fixed.

The detector under-matched; the moment it stopped under-matching, the confirmer over-withdrew.
Opposite halves of one design.

THE COUNTERINTUITIVE PART, worth keeping: the sync gate was the ONLY exposed one. Every other
detector (`git[^\\n]{0,80}push`, `gmail\\.py[^\\n]{0,120}send`, `osascript|tell application`) is an
UNANCHORED substring match, so wrapping changes nothing. `_POS_RX` carried a boundary class because
it alone had to separate `bash <script>` from `grep ... <script>`. **The precision created the
vulnerability. The blunt detectors were safe by being blunt.**

WHAT THIS ASSERTS — behaviour through the real guard, by exit code. Not that the regex matches:
that is what made (3) invisible for an hour. The four evasion shapes must gate, and the four
legitimate shapes must still pass, because a gate that blocks everything gets switched off.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / ".claude" / "hooks" / "pretooluse-guard.sh"

SYNC = "bash bin/sync-to-baseline.sh"
BT = chr(96)

# (label, command, expected_exit) — 2 = gated, 0 = allowed
CASES = [
    ("bare $( ) substitution",        "echo $(%s)" % SYNC,                                    2),
    ("backtick substitution",         "echo %s%s%s" % (BT, SYNC, BT),                         2),
    ("assignment capture x=$( )",     "x=$(%s)" % SYNC,                                       2),
    ("unquoted heredoc carrying $( )", "cat > /tmp/n.txt <<EOF\nr: $(%s)\nEOF" % SYNC,         2),
    ("direct invocation",             SYNC,                                                   2),
    ("quoted heredoc, prose only",    "cat > /tmp/n.txt <<'EOF'\nabout %s\nEOF" % SYNC,        0),
    ("read-only --check",             "%s --check" % SYNC,                                    0),
    ("grep ABOUT the script",         'grep -n "psql" bin/sync-to-baseline.sh',                0),
]

failures = []


def run(cmd):
    p = subprocess.run(
        ["bash", str(GUARD)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        capture_output=True, text=True,
    )
    return p.returncode


if not GUARD.is_file():
    print("  SKIP — no pretooluse-guard.sh on this seat")
    sys.exit(0)

for label, cmd, want in CASES:
    got = run(cmd)
    ok = got == want
    kind = "gated" if want == 2 else "allowed"
    print(("  ok     " if ok else "  FAIL   ") + "%-32s -> %s (exit %d)" % (label, kind, got))
    if not ok:
        failures.append("%s: expected exit %d, got %d" % (label, want, got))

print()
if failures:
    print("  FAIL=%d of %d" % (len(failures), len(CASES)))
    for f in failures:
        print("    - " + f)
    sys.exit(1)
print("  ok=%d  FAIL=0 — substitution, backticks and heredocs cannot lower the sync gate" % len(CASES))
