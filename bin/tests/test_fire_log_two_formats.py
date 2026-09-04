#!/usr/bin/env python3
"""THE FIRE LOG HAS TWO FORMATS AND THE PARSER KNEW ONE — so it counted hook names as contracts.

`measure-contract-fitness.fire_counts()` read column 1 unconditionally:

    old   timestamp \\t hook \\t CONTRACT \\t prompt      contract in col 2
    new   hook \\t CONTRACT \\t prompt                    contract in col 1

On every old-format line it tallied the HOOK NAME as a contract, and the real contract on that line
was attributed to nothing.

FOUND BY core-business, on its own seat, where 378 of 405 lines are old-format — 93% of its records
parsed wrong. Seven of ten contracts reported as never-fired had fired:

    instruction directive   0 -> 342        recall-first        0 -> 20
    instruction tooling     0 -> 120        verify-dont-claim   0 -> 15
    instruction preference  0 ->  52        plan-not-execute    0 ->  7

On life the split runs the other way (28 old / 84 new) and the damage is smaller but identical in
kind: `classifier` — a HOOK — was counted 27 times as a contract, while recall-first was
undercounted 19 -> 38 and verify-dont-claim 5 -> 12.

WHAT IT COST, and this is why it is worth a test rather than a comment. Both Cores built conclusions
on those zeros:

  - business's retirement pass proposed deleting EIGHT contracts including the four good
    hand-authored ones, purely on counts this function invented. With the fix it proposes one.
  - "contract_state binds 0%, 8 never fired" — a figure I repeated to Nick — was an artifact of
    this line.

A counter that reports zero for something that fired 342 times does not fail loudly; it produces a
confident, actionable, wrong answer, and every consumer downstream inherits it.

Run: python3 bin/tests/test_fire_log_two_formats.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
MCF = ROOT / "scheduling" / "claude-si" / "measure-contract-fitness.py"


def load(fires_log):
    sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
    sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))
    spec = importlib.util.spec_from_file_location("mcf_probe", MCF)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    m.FIRES_LOG = fires_log
    return m


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== the fire log has two formats; both must attribute to the CONTRACT ===\n")

    if not MCF.is_file():
        print("  SKIP — measure-contract-fitness.py absent")
        return 0

    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "learned-fires.log"
        log.write_text(
            # old: timestamp first, contract in col 2, hook in col 1
            "2026-08-05T16:38:01Z\tlearned-classifier\trecall-first\tsome prompt\n"
            "2026-08-05T16:39:02Z\tlearned-classifier\tverify-dont-claim\tanother\n"
            "2026-08-05T16:40:03Z\tlearned-validator\trecall-first\tthird\n"
            # new: hook first, contract in col 1
            "learned-stopguard\tplan-not-execute\ta prompt\n"
            "learned-stopguard\trecall-first\tanother prompt\n"
        )
        m = load(log)
        c = m.fire_counts()

        check("old-format lines attribute to the CONTRACT, not the hook",
              c.get("recall-first", 0) == 3,
              "recall-first counted %d, expected 3 (2 old-format + 1 new)" % c.get("recall-first", 0))
        check("new-format lines still work", c.get("plan-not-execute", 0) == 1,
              "got %d" % c.get("plan-not-execute", 0))
        check("verify-dont-claim picked up from an old-format line",
              c.get("verify-dont-claim", 0) == 1, "got %d" % c.get("verify-dont-claim", 0))

        # THE CONTROL THAT NAMES THE DEFECT. Without it, a parser that reads column 1 everywhere
        # still passes the counts above by coincidence on a hand-picked fixture.
        check("a HOOK NAME is never counted as a contract",
              c.get("learned-classifier", 0) == 0 and c.get("learned-validator", 0) == 0,
              "hooks tallied as contracts: classifier=%d validator=%d — this is the whole defect"
              % (c.get("learned-classifier", 0), c.get("learned-validator", 0)))

        print("\n--- malformed lines must not crash the counter ---")
        log.write_text("2026-08-05T16:38:01Z\tonly-two-cols\n"
                       "\n"
                       "single\n"
                       "2026-08-05T16:38:01Z\thook\tgood-contract\tp\n")
        c2 = m.fire_counts()
        check("a timestamped line with no col 2 is skipped, not fatal",
              c2.get("good-contract", 0) == 1, str(dict(c2)))

    print("\n--- and on the LIVE log, no hook name appears as a contract ---")
    live = ROOT / ".claude" / "state" / "learned-fires.log"
    # EMPTY counts the same as ABSENT. install-learned-layer.sh creates this file at zero bytes
    # on every fresh Core (docs/SETUP.md step 3) — the file exists before a single hook has ever
    # fired, so `is_file()` alone is true on a Core with no fixture yet, and "the live tally is
    # not empty" then fails not because the parser is broken but because there is nothing to
    # parse. That is the same "no fixture, not a defect" case the missing-file branch already
    # exists to cover — extended here to cover the fresh-seat shape it was one check short of.
    if not live.is_file() or live.stat().st_size == 0:
        print("  SKIP — no live fire log on this Core (fresh seat has no hook-fire history yet)")
    else:
        m2 = load(live)
        c3 = m2.fire_counts()
        hooks = [n for n in c3 if n.startswith("learned-") or n in ("classifier", "validator")]
        check("no hook-shaped name in the live tally", not hooks,
              "still counting hooks as contracts: %s" % hooks)
        check("the live tally is not empty (a parser that returns nothing passes vacuously)",
              sum(c3.values()) > 0, "tallied %d fires" % sum(c3.values()))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
