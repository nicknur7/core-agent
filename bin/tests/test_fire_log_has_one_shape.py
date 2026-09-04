#!/usr/bin/env python3
"""Every writer of learned-fires.log emits the same row shape: timestamp first, prompt last.

WHY THIS EXISTS (2026-08-20, found by core-ops).

`learned-fires.log` is the only artifact that records WHAT a contract fired on, which makes it the
only place the "aim" question can be answered — core-finance's point that a rule firing at a healthy
rate entirely on machine traffic passes both a specificity floor and a sensitivity ceiling.

Five writers produced FOUR different row shapes:

    learned-classifier      ts \t classifier  \t keys   \t prompt     4 fields
    learned-stopguard          stopguard   \t shadow \t prompt        3 fields, no ts
    learned-recallguard        recallguard \t shadow \t prompt        3 fields, no ts
    learned-validator          validator   \t shadow \t prompt        3 fields, no ts
    learned-validator       ts \t validator  \t block  \t prompt      4 fields

So every reader guessed at a column, and the guess decided the answer. On ops the same predicate
over the same file gave **1% via split[2] and 67% via split[-1]** — a 66-point swing from the index
alone, on a seat whose rows are 98% four-field. Four seats published false-fire numbers without any
of them stating which index they used, and life marked the whole column "VALID, KEEP IT" before ops
pointed out the input was unverified.

The prompt was always last, so `[-1]` was always right and `[2]` was wrong for every 3-field row.
The fix is at the source rather than in each reader: timestamp first, prompt last, on every row.

WHAT THIS ASSERTS.
  1. Every write to the fire log begins with a timestamp field.
  2. The prompt is the LAST field at every write site.
  3. No reader in the tree extracts the prompt by a hardcoded positive index — `[2]` is the exact
     mistake this exists to prevent, and it must not come back in a reader either.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".claude" / "hooks"
checks = 0

WRITERS = ["learned-classifier", "learned-stopguard", "learned-recallguard", "learned-validator"]
sites = []
for name in WRITERS:
    src = (HOOKS / (name + ".py")).read_text()
    for m in re.finditer(r"f\.write\((.+?)\+ \"\\n\"\)", src, re.S):
        sites.append((name, " ".join(m.group(1).split())))
assert sites, "no fire-log write sites found — retarget this test"
checks += 1

# 1) every row starts with a timestamp
for name, expr in sites:
    assert re.match(r'^(_ts|_fire_ts\(\))', expr.strip()), \
        ("%s writes a fire row that does not begin with a timestamp: %s\n"
         "   -> four row shapes from five writers is how the same predicate returned 1%% and 67%% "
         "on one file" % (name, expr[:110]))
checks += 1

# 2) the prompt is last at every site
for name, expr in sites:
    tail = expr.rsplit("+", 1)[-1]
    assert "prompt" in tail, \
        "%s does not put the prompt last: ...%s — [-1] must be the prompt on every row" % (name, tail[:70])
checks += 1

# 3) no reader extracts the prompt by a hardcoded positive index
for py in list((REPO / "scheduling" / "claude-si").glob("*.py")) + list(HOOKS.glob("*.py")):
    src = py.read_text()
    if "learned-fires" not in src and "FIRE_LOG" not in src:
        continue
    bad = re.findall(r"\.split\(\s*[\"']\\t[\"']\s*\)\s*\[\s*[0-9]+\s*\]", src)
    # index 0 is the timestamp and is legitimate; anything else is guessing at a variable-arity row
    bad = [b for b in bad if not b.rstrip().endswith("[0]")]
    assert not bad, "%s extracts a fire-log field by positive index %s — the row arity varies by " \
                    "writer and verdict, which is the defect this test exists for" % (py.name, bad[:2])
checks += 1

# 4) ONE TIMESTAMP FORMAT ACROSS EVERY WRITER — not merely "a timestamp".
#
# The first fix here gave three writers `str(int(time.time()))` while learned-classifier wrote ISO,
# so the repair for the ARITY problem introduced a FORMAT problem in the same file and shipped it.
# core-business hit the same class on its own disk within the hour and named both directions:
#
#     ISO + explicit +00:00   -> a lexical string compare fabricates a LEAK
#     Unix epoch int          -> an ISO parse fabricates an EMPTY WINDOW
#
# Both fail silently, in opposite directions, and each produces a clean plausible number. A reader
# forced to sniff the format of field 0 will eventually sniff wrong, and the wrong answer is
# indistinguishable from a real one. One format per file is the only safe answer.
fmt = {}
for name in WRITERS:
    src = (HOOKS / (name + ".py")).read_text()
    fmt[name] = ("iso" if 'isoformat(timespec="seconds")' in src else
                 "epoch" if "int(_t.time())" in src or "int(time.time())" in src else "unknown")
kinds = set(fmt.values())
assert kinds == {"iso"}, (
    "fire-log writers disagree on timestamp format: %s\n"
    "   -> mixed formats in field 0 mean every reader must sniff, and a wrong sniff produces a "
    "clean false number rather than an error" % fmt)
checks += 1

print("ok — %d checks: %d write sites, ts-first and prompt-last, one ISO format, no positive-index readers"
      % (checks, len(sites)))
