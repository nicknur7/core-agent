#!/usr/bin/env python3
"""The block miner must read every generation of a log that rotates.

WHY THIS EXISTS (2026-08-13). `hook-events.log` rotates at 1 MB. `hook_block_miner` read the live
file directly, so after a rotation it measured whatever had accumulated since — and it rotated
tonight at 21:28. Measured minutes later:

    live file      173 lines        everything the miner could see
    rotated .1    7013 lines        invisible to it
    true corpus   7186 lines   ->   2% visible

    blocks visible, live only:      4
    blocks visible, across rotation: 386

A block-mining organ reading 2% of the blocks does not fail. It returns a small, confident, wrong
answer — and its recurrence bar (a block must repeat N times before it becomes a case) is exactly
the kind of threshold that silently stops being reachable. After the fix it mines 5 cases, including
`recall-gate blocked 66x across 12 sessions`, which 4 visible blocks could never have produced.

The file's own header says the defect it was built to fix is that the blocks were there and
"nothing has ever read them". A rotation had quietly turned it back into nothing reading almost all
of them.

HOW IT WAS FOUND, because the method transfers better than the fix. Two of my own censuses
disagreed: one counted 6789 rows across 31 hooks, a later one counted 157 rows across 14 — same
command, same path, forty minutes apart. Neither was wrong; the file had rotated between them. **A
disagreement between two of your own measurements is worth more than either measurement**, and it is
the third time tonight that chasing one produced a real finding.

WHAT THIS ASSERTS. Not a line count — that changes constantly. It asserts the miner READS the
rotated generations when they exist, and that it degrades to live-only rather than to empty when the
canonical resolver is unavailable. An organ that returns [] on a resolver failure would look exactly
like a clean seat.
"""
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scheduling" / "claude-si" / "hook_block_miner.py"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _decomment(text: str) -> str:
    return "\n".join(re.sub(r"(?<!['\"])#.*$", "", ln) for ln in text.splitlines())


def main() -> int:
    print("test_block_miner_reads_across_rotation")
    if not SRC.is_file():
        print(f"  FAIL  {SRC} missing")
        return 1
    code = _decomment(SRC.read_text())

    # ANCHOR ON THE CANONICAL SOURCE, NOT THE ALIAS. The first version matched
    # `read_rotated|rotated_set`, which the local alias `_read_rotated` satisfies as a SUBSTRING —
    # so replacing the import with a live-only lambda left the assertion green and the dose could
    # not fail. Ninth substring/prose false-match today. Requiring the core_paths import means the
    # only way to satisfy it is to actually use the shared resolver.
    check("the miner resolves the log across rotation generations",
          re.search(r"from\s+core_paths\s+import[^\n]*\b(read_rotated|rotated_set)\b", code)
          or re.search(r"\bcore_paths\.(read_rotated|rotated_set)\s*\(", code),
          "it reads the live generation only. hook-events.log rotates at 1 MB, so this silently "
          "measures 'since the last rotation' while reporting it as the corpus — 2% of it, the one "
          "time this was caught.")

    check("...and degrades to LIVE-ONLY, never to empty, if the resolver is unavailable",
          re.search(r"except[^\n]*:\s*\n\s*_text\s*=\s*HOOK_EVENTS\.read_text", code) is not None,
          "an organ that returns nothing when its resolver fails is indistinguishable from a seat "
          "with no blocks at all — the exact conflation this file exists to prevent")

    # BEHAVIOURAL: build a fake rotated set and confirm both generations are counted. Uses a temp
    # dir so the live seat's log is never read or written by this test.
    sys.path.insert(0, str(REPO / "bin"))
    try:
        from core_paths import read_rotated
    except Exception as e:  # noqa: BLE001  # privacy-ok: noqa linter directive, not a course code
        print(f"  SKIP  core_paths.read_rotated unavailable ({type(e).__name__}) — cannot test "
              f"the resolver behaviourally")
        print(f"\n{len(passes)} passed, {len(failures)} failed")
        return 1 if failures else 0

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "hook-events.log"
        base.write_text("live-1\nlive-2\n")
        (Path(td) / "hook-events.log.1").write_text("rot-1\nrot-2\nrot-3\n")
        (Path(td) / "hook-events.log.2").write_text("older-1\n")
        text = read_rotated(base)
        lines = [l for l in text.splitlines() if l.strip()]
        check("the resolver returns every generation (2 live + 3 + 1 older = 6)",
              len(lines) == 6, f"got {len(lines)}: {lines}")
        check("...oldest first, so ordering is chronological",
              lines[0] == "older-1" and lines[-1] == "live-2",
              f"got {lines} — a reader that assumes chronological order would mis-window every "
              f"time-bounded query built on this")
        # And it must not invent generations that do not exist.
        solo = Path(td) / "only.log"
        solo.write_text("just-one\n")
        check("a log with no rotations returns exactly itself",
              read_rotated(solo).strip() == "just-one")

    # ---- NO LOCAL COPIES OF THE ROTATION RULE ------------------------------------------------
    # core_paths.rotated_set was written on 2026-08-12 to be the ONE resolver, and its docstring
    # names the four readers it was written for by line number. Three of them shipped their own
    # byte-identical `_rotated_text` anyway — the same escape the slug helper made twice. Verified
    # equivalent before consolidating (numeric suffix order, `.10` older than `.2`, and non-numeric
    # siblings ignored), then routed through core_paths.
    #
    # This is a fence, not a ratchet: unlike the slug helper there is no backlog to work down, so
    # the correct count is zero and any new copy is a regression rather than inherited debt.
    import re as _re
    copies = []
    for root in ("bin", "scheduling", ".claude/hooks"):
        for p in sorted((REPO / root).rglob("*.py")):
            if p.name == "core_paths.py" or "archive" in p.parts or "tests" in p.parts:
                continue
            body = _decomment(p.read_text(errors="ignore"))
            if _re.search(r"\ndef _rotated_text\b", body) and "core_paths" not in body:
                copies.append(str(p.relative_to(REPO)))
    check("no file defines its own rotation resolver instead of using core_paths",
          not copies,
          "a local copy of the rotation rule is another place for the next generation suffix to be "
          "missed — core_paths' own docstring makes this argument. Found:\n          "
          + "\n          ".join(copies))

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
