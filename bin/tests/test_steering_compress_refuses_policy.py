#!/usr/bin/env python3
"""steering-compress must move HISTORY and must refuse POLICY. Both directions, or it ships nothing.

The first proposal run of this tool selected six blocks. THREE of them it must never have selected:
memory.md's "Nothing enforces this. It is on you", the Three Anti-Patterns paragraph, and a block
opening "Nick's standing directive, 2026-07-24". That last one is the failure that matters — moving
one of Nick's standing directives out of the always-loaded set is strictly worse than being over
budget, because the budget costs tokens and that costs an instruction he will never see again.

The cause was mundane: the guard matched `Do not` and not `do not`. One case gap, and a directive
was queued for removal from the file whose job is to state it.

So the refusal half is tested harder than the detection half. A compressor that finds nothing is a
no-op; a compressor that moves policy is a silent lobotomy, and nothing downstream would notice —
the file still parses, the rules still load, the missing paragraph looks like it was never there.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin"))
import steering_load as _sl        # noqa: E402
import importlib.util             # noqa: E402

_spec = importlib.util.spec_from_file_location("sc", ROOT / "bin" / "steering-compress.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


HISTORY = (
    "The table is canonical here. It replaced four overlapping restatements of itself on "
    "2026-08-06, when the ratchet made redundancy something to be paid for rather than tolerated, "
    "and the edit paid for an entry in the same pass. Recorded so the provenance is not lost."
)
DIRECTIVE = (
    "Nick's standing directive, 2026-07-24 on the 20X Max plan: orchestrate the triad by default "
    "for real work, and treat the cost question as settled rather than reopening it each time."
)
LOWER_IMPERATIVE = (
    "If you find a dir-form spec on disk, do not cite it, and do not assume why it is there. "
    "On a peer Core it most likely means that Core has not pulled since 2026-07-29, which is a "
    "different thing entirely from the case a fork presents."
)
SECOND_PERSON = (
    "Nothing enforces this. It is on you. A rule that tells you a gate will catch you when no gate "
    "runs is worse than silence, because you relax against a net that was taken down on 2026-08-06."
)


def main() -> int:
    # ---- the refusal half, tested first and hardest -------------------------------------------
    check("MOVES a purely retrospective dated block", sc.movable(HISTORY))
    check("REFUSES a block attributed to Nick (the near-miss)", not sc.movable(DIRECTIVE))
    check("REFUSES lowercase `do not` (the exact case gap)", not sc.movable(LOWER_IMPERATIVE))
    check("REFUSES second-person prose", not sc.movable(SECOND_PERSON))
    check("REFUSES an undated block (not history)",
          not sc.movable("A paragraph of ordinary policy prose with no date in it at all, long "
                         "enough to clear the minimum block size that the tool applies to every "
                         "candidate before it considers anything else about the content."))
    check("REFUSES a table", not sc.movable("| a | b |\n|---|---|\n| 2026-08-06 | x |\n" + "z" * 200))
    check("REFUSES a heading block", not sc.movable("# Heading 2026-08-06\n\n" + "z" * 200))
    check("REFUSES a short block", not sc.movable("2026-08-06 tiny."))

    # ---- ownership: never touch what another mechanism owns ------------------------------------
    check("lessons.md is not in this tool's scope", "tasks/lessons.md" in sc.NOT_OURS)
    check("CLAUDE.md is not in this tool's scope", "CLAUDE.md" in sc.NOT_OURS)
    check("scope is derived from the shared ALWAYS_LOADED list, not a private copy",
          all(r in _sl.ALWAYS_LOADED for r in sc.NOT_OURS))

    # ---- behaviour on a real temp seat ---------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel in _sl.ALWAYS_LOADED:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            # PADDED. A tiny temp seat cannot go "over budget" at all: TOLERANCE is a flat 200 tok,
            # so with a 166-tok seat every reachable ceiling still leaves positive headroom and the
            # tool correctly no-ops. The first version of this test read that as the tool failing to
            # move anything. The fixture was wrong, not the tool — but a fixture that cannot reach
            # the state under test is the same class of defect as an instrument that cannot fail.
            p.write_text("# F\n\n" + ("filler prose for budget mass. " * 60) + "\n")
        rules = root / ".claude" / "rules" / "memory.md"
        rules.write_text("# Memory\n\n## A section\n\n" + HISTORY + "\n\n" + DIRECTIVE + "\n")

        # UNDER budget -> must be a no-op, and must not create the detail tree.
        _, total = _sl.measure(root)
        _sl.baseline_path(root).parent.mkdir(parents=True, exist_ok=True)
        _sl.baseline_path(root).write_text(json.dumps({"ceiling": total + 5_000}))
        before = rules.read_text()
        sc.run(root, apply=True)
        check("under budget -> no-op", rules.read_text() == before)
        check("under budget -> writes no detail file", not (root / sc.DETAIL_DIR).exists())

        # OVER budget -> moves the history, leaves the directive, stays reversible.
        _sl.baseline_path(root).write_text(json.dumps({"ceiling": max(1, total - 400)}))
        sc.run(root, apply=True)
        after = rules.read_text()
        check("over budget -> the HISTORY block is gone from the rules file",
              "replaced four overlapping restatements" not in after)
        check("over budget -> the DIRECTIVE is STILL in the rules file",
              "Nick's standing directive" in after, after[:160])
        check("a pointer to the detail file was left behind", "steering-detail" in after)
        detail = root / sc.DETAIL_DIR / "memory.md"
        check("the moved text exists verbatim in the detail file",
              detail.is_file() and "replaced four overlapping restatements" in detail.read_text())
        logp = root / sc.LOG_REL
        check("the move is logged with its full body for one-paste reversal",
              logp.is_file()
              and any("replaced four overlapping restatements" in json.loads(l)["body"]
                      for l in logp.read_text().splitlines() if l.strip()))

    print("\n" + ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "ALL PASS"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
