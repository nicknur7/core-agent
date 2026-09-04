#!/usr/bin/env python3
"""The S3/S4 write gate must refuse the right writes, permit the rest, and BE ONE INSTRUMENT.

core-business built the two pure functions (#913); .claude/hooks is shared and it is role=puller, so
the wiring is life's. It flagged one tradeoff it wanted decided rather than inherited: its copy
duplicates the matchers from bin/casebook-run.py, because a PreToolUse hook that imports from bin/
depends on a directory that can be absent, renamed, or mid-sync when the hook fires.

I ANSWERED "KEEP THE COPY" AND THE MEASUREMENT REFUTED ME INSIDE THE HOUR. The copy had already
diverged at birth: run against the real memory/access-log.md it produced exactly one refusal, and it
was a FALSE POSITIVE on a `- **Why:** …` sub-field matching the word "create". Two instruments, one
subject — the defect it warned about, live in the artifact that warned about it.

The dependency is INVERTED rather than removed, which keeps business's reason intact: the definition
lives in .claude/hooks/casebook_matchers.py, present whenever a hook can run at all, and the runner
imports UPWARD from it. This file asserts they cannot drift apart again.

FIRE RATE, MEASURED BEFORE SHIPPING, on real content — the bar set by the WebFetch precedent
(85 approve-then-rerun blocks fleet-wide, 0 real catches):

    S3   11 list-shaped lines in access-log.md, 0 refusals with the guards in place
    S4   1 line carrying a percentage across nine steering targets, 0 refusals

Run: python3 bin/tests/test_casebook_write_gate.py
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
GATE = ROOT / ".claude" / "hooks" / "casebook-write-gate.py"
MATCHERS = ROOT / ".claude" / "hooks" / "casebook_matchers.py"


def gate(tool: str, tool_input: dict):
    r = subprocess.run([sys.executable, str(GATE)],
                       input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                       text=True, capture_output=True, timeout=60)
    return r.returncode, (r.stderr or "")


CASES = [
    # (label, tool, input, expect_refusal)
    ("S3 unstamped entry recording a write -> REFUSE", "Write",
     {"file_path": "/x/memory/access-log.md",
      "content": "- 2026-08-09 | WRITE to memory/foo.md | reason\n"}, True),
    ("S3 stamped entry -> allow", "Write",
     {"file_path": "/x/memory/access-log.md",
      "content": "- 2026-08-09 | WRITE to memory/foo.md | reason — done\n"}, False),
    ("S3 a **Why:** sub-field is NOT an entry -> allow", "Write",
     {"file_path": "/x/memory/access-log.md",
      "content": "- **Why:** Nick asked me to create a calendar event\n"}, False),
    ("S3 non-list prose mentioning write -> allow", "Write",
     {"file_path": "/x/memory/access-log.md", "content": "I will write about this later.\n"}, False),
    ("S3 a different file -> allow", "Write",
     {"file_path": "/x/tasks/notes.md", "content": "- WRITE to foo | no stamp\n"}, False),
    ("S3 a non-write tool -> allow", "Bash",
     {"command": "- WRITE to memory/access-log.md"}, False),
    ("S4 bare percentage in steering text -> REFUSE", "Write",
     {"file_path": "/x/CLAUDE.md", "content": "Corrections fell 82% after the change.\n"}, True),
    ("S4 percentage WITH a date -> allow", "Write",
     {"file_path": "/x/CLAUDE.md", "content": "Corrections fell 82% (measured 2026-08-09).\n"}, False),
    ("S4 percentage in a non-steering file -> allow", "Write",
     {"file_path": "/x/tasks/scratch.md", "content": "Fell 82% overall.\n"}, False),
    ("S4 an Edit payload is checked too", "Edit",
     {"file_path": "/x/.claude/rules/memory.md", "new_string": "improved by 40%\n"}, True),
    ("S4 inside a fenced block is illustration, not a claim -> allow", "Write",
     {"file_path": "/x/CLAUDE.md", "content": "```\nfell 82% overall\n```\n"}, False),
]


def main() -> int:
    p = f = 0
    print("=== S3/S4 write gate ===\n")

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    for label, tool, inp, want in CASES:
        rc, err = gate(tool, inp)
        check(label, (rc == 2) == want, "rc=%d stderr=%s" % (rc, err.strip()[:90]))

    print("\n--- EXECUTED vs READ: the same text is a defect in one and not the other ---")
    # core-business, bus #993, drew this line after two cases landed hours apart and it is the
    # sharpest statement of what S4 is actually for.
    #
    #   Its substring_gate flagged `ALREADY_GATED` inside a DOCSTRING that documented the old defect
    #   — a false positive, because a docstring is INERT. Quoting a defect to explain it is what
    #   documentation IS.
    #
    #   My gate then flagged a bare percentage in its lessons.md — a TRUE positive, because
    #   lessons.md is loaded into context on EVERY PROMPT. A model reading it need not carry the
    #   surrounding "this figure was stale" framing along with the number. Quoting a bad number to
    #   illustrate bad numbers still puts the number in the window.
    #
    # So what separates them is not the text. It is whether the file is EXECUTED or READ. This gate
    # already encodes that by scoping S4 to DOC_TARGETS — the nine always-loaded steering files —
    # but nothing pinned it, and widening that list to `.py` would LOOK like more coverage while
    # blocking every docstring that documents a defect with a number in it. That is the
    # false-positive direction, and false positives are what get gates disabled.
    # FIXTURE CHOSEN CAREFULLY, AFTER THE FIRST ONE FAILED TO CONSTRUCT THE CASE. My first sample
    # read "The detector was wrong 82% of the time" and the steering path ALLOWED it — correctly:
    # S4_INST counts the word "detector" as naming an instrument, so the line was already compliant.
    # The gate was right and my planting was wrong, which is business's "the construction is also a
    # measurement" landing inside the test written about that principle. This sample carries a bare
    # percentage and no instrument word, no filename, no date.
    METRIC = "The failure rate was 82% across the sample.\n"
    rc_code, _ = gate("Write", {"file_path": "/x/bin/some_tool.py",
                                "content": '"""Docstring: %s"""\n' % METRIC.strip()})
    check("the SAME metric text in a .py docstring is ALLOWED (code is executed, not read)",
          rc_code != 2, "rc=%d — S4 has leaked into code" % rc_code)

    rc_steer, err_steer = gate("Write", {"file_path": "/x/tasks/lessons.md", "content": METRIC})
    check("...and REFUSED in tasks/lessons.md (loaded on every prompt)", rc_steer == 2,
          "rc=%d %s" % (rc_steer, err_steer.strip()[:80]))

    # THE DOSE. Without it the pair above is consistent with a gate that refuses nothing, or one
    # that refuses everything — the two checks must disagree, on identical content.
    check("...so the verdict depends on the FILE, not the text", rc_code != rc_steer,
          "both paths returned rc=%d for identical content" % rc_code)

    print("\n--- the graded thing may NOT exempt itself ---")
    # The runner closed this hole already: an in-file `casebook-exempt:` comment is
    # candidate-controlled, so anything being graded could suppress its own check by writing a
    # comment. Consolidating the matchers re-imported that hole from the prototype, and it is worse
    # on the gate side — the agent whose write is refused is the same agent that can add the comment.
    rc, _ = gate("Write", {"file_path": "/x/memory/access-log.md",
                           "content": "- 2026-08-09 | WRITE to foo | reason  <!-- casebook-exempt: S3 -->\n"})
    check("a bare in-file exempt marker does NOT suppress the refusal", rc == 2, "rc=%d" % rc)

    print("\n--- ...but the gate must not be STRICTER than the item either ---")
    # EXEMPTION IS A TWO-KEY SYSTEM and I shipped it wrong in BOTH directions within an hour.
    # core-business caught both (#921). A bare marker is candidate-controlled and must do nothing;
    # NO marker handling at all makes the gate refuse writes the casebook would exempt, which on a
    # PreToolUse path is the expensive failure — Nick hits a block on a legitimate write and learns
    # to distrust it. The item's rule, verbatim from _lines_with_context: "The in-file marker is
    # still READ, but only when the item set has declared that exact (item, file) pair."
    import json as _j
    import tempfile as _t
    spec = importlib.util.spec_from_file_location("cm2", str(MATCHERS))
    cm2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm2)
    line = "- 2026-08-09 | WRITE to foo | reason  <!-- casebook-exempt: S3 -->\n"
    check("with the second key ABSENT, the marker is inert",
          len(cm2.s3_violations(line, honour_markers=False)) == 1)
    check("with the second key PRESENT, the marker exempts the line",
          len(cm2.s3_violations(line, honour_markers=True)) == 0)
    with _t.TemporaryDirectory() as td:
        (Path(td) / "eval").mkdir()
        (Path(td) / "eval" / "casebook-v1.json").write_text(_j.dumps(
            {"items": [{"id": "S3", "exemptions": [{"file": "memory/access-log.md", "reason": "t"}]}]}))
        check("declared_exempt reads the declaration from the ITEM SET (inside the TCB fence)",
              cm2.declared_exempt(td, "S3", "memory/access-log.md") is True)
        check("...and returns False for a file it does not name",
              cm2.declared_exempt(td, "S3", "memory/other.md") is False)
    check("an unreadable/absent item set means NOT exempt — stricter, never laxer",
          cm2.declared_exempt("/nonexistent-root", "S3", "memory/access-log.md") is False)
    # THE DOSE: the two answers must differ on one input, or the second key is decorative.
    check("the second key CHANGES the answer (it is not decorative)",
          len(cm2.s3_violations(line, honour_markers=False))
          != len(cm2.s3_violations(line, honour_markers=True)))

    print("\n--- one instrument: the gate and the casebook item cannot drift apart ---")
    src = (ROOT / "bin" / "casebook-run.py").read_text()
    check("the runner IMPORTS the matchers rather than redefining them",
          "from casebook_matchers import" in src,
          "runner still carries its own copy")
    check("...and RAISES if they are missing, rather than scoring without them",
          "def _matchers()" in src and "sys.path.insert" in src)
    # And prove it by behaviour, not by reading: drive the same content through both paths.
    spec = importlib.util.spec_from_file_location("cm", str(MATCHERS))
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
    sample = "- 2026-08-09 | WRITE to memory/foo.md | reason\n- **Why:** asked me to create it\n"
    rc, _ = gate("Write", {"file_path": "/x/memory/access-log.md", "content": sample})
    check("gate refusal and matcher output agree on the same text",
          (rc == 2) == bool(cm.s3_violations(sample)) and len(cm.s3_violations(sample)) == 1,
          "rc=%d matcher=%s" % (rc, cm.s3_violations(sample)))

    print("\n--- fire rate on THIS Core's real content (the WebFetch bar) ---")
    al = ROOT / "memory" / "access-log.md"
    live3 = cm.s3_violations(al.read_text(errors="replace")) if al.is_file() else []
    live4 = []
    for t in cm.DOC_TARGETS:
        q = ROOT / t
        if q.is_file():
            live4 += cm.s4_violations(q.read_text(errors="replace"))
    # THESE TWO ASSERT A PROPERTY OF *THIS SEAT'S CORPUS*, NOT OF THE SHARED GATE — and until
    # 2026-08-13 their failure text did not say so. core-finance ran the shared suite for the first
    # time and got 22 FAILs, one of them here: S4 flags core-finance/CLAUDE.md:21, its founding
    # posture line, which carries "~51%" with no instrument and no date. That is a CORRECT flag.
    # The gate is doing its job; finance's steering text has a real S4 violation; and the test
    # passes on life only because life's CLAUDE.md happens not to contain one.
    #
    # A shared test whose verdict depends on the running seat's own prose will be green on the
    # writer and red on everyone else — the 2026-08-06 lesson, in the suite that enforces it.
    #
    # The assertion is KEPT, because a matcher that over-fires on real steering text is exactly
    # what it exists to catch and that is a shared defect. What changes is the failure text: a
    # reader on a puller must be able to tell "the shared matcher is broken" from "my own file
    # violates the rule", and act on the second without filing the first. So the message names the
    # seat, the file and the line, and states both readings.
    _seat = ROOT.name
    for _label, _hits in (("S3", live3), ("S4", live4)):
        check(f"{_label} refuses nothing already written in {_seat}'s own steering text",
              not _hits,
              f"{len(_hits)} line(s) on THIS seat ({_seat}) would be refused:\n          "
              + "\n          ".join(f"{l[:80]}" for _, l in _hits[:3])
              + f"\n          TWO READINGS, and they need different actions. (1) The line really "
                f"does violate {_label} — an uninstrumented metric, or an action logged with no "
                f"completion stamp — in which case FIX THE LINE; this is your content, not a "
                f"shared-code defect, and the gate is working. (2) The matcher is over-firing on "
                f"correct prose, which IS a shared defect and should be reported to the baseline "
                f"writer with the line quoted. Check which before filing either.")

    print("\n--- registered, or it is a file that does nothing ---")
    settings = (ROOT / ".claude" / "settings.json").read_text()
    check("the gate is registered in settings.json on PreToolUse",
          "casebook-write-gate.py" in settings,
          "unregistered — an unwired hook is a hook that has never run")

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
