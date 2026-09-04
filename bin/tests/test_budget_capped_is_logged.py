#!/usr/bin/env python3
"""A matched artifact suppressed by its fire budget must SAY SO. Silence corrupts every fitness verdict.

WHY THIS EXISTS (2026-08-12, Phase 4). friction_dispatch's loop records every way it can decline to
fire — payload_mismatch, shadow_block, fire_block, dispatch_error — with ONE exception:

    if not _budget_ok(art.get("artifact_id", "?"), session, cap):
        continue                                    # <- silent, until now

The artifact MATCHED. It was suppressed only because it had already fired its per-session cap. So a
`fire_count` of 2 is indistinguishable from "matched fifty times and was capped forty-eight" — and
measure-contract-fitness reads that number as evidence about the RULE when it is partly evidence
about the CAP. GRADUATED, NOT-BINDING and DECAYING all sit downstream of it.

MEASURED WHEN THE LOGGING SHIPPED, on life's real live artifacts, six dispatches of one matching
prompt:

    fire_inject   12
    budget_capped 24        <- two thirds of all matches were invisible

The master plan names this precisely: "Make 'matched but did not fire' a permanently logged
first-class state. Rete/OPS5 conflict-set logging exists precisely because a zero fire count is
otherwise indistinguishable from no match."

WHAT THIS ASSERTS. The log line exists, carries the fields that make it interpretable, and sits on
the budget path specifically — not that some artifact happened to be capped today. A test that
depended on live artifacts being over budget would pass or fail on the seat's mood.

`cap` rides along deliberately: a rule capped at 1 and a rule capped at 5 with the same suppressed
count are not the same finding, and a suppression rate is uninterpretable without the budget that
caused it.
"""
import inspect
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_budget_capped_is_logged")
    try:
        import friction_dispatch as fd
    except Exception as exc:
        print(f"  FAIL  cannot import friction_dispatch ({exc.__class__.__name__}: {exc})")
        return 1

    src = inspect.getsource(fd)

    # The budget check and the log must be adjacent — the property is not "budget_capped appears
    # somewhere in the file", it is "the budget path is the thing that emits it".
    # NESTED PARENS: the call is _budget_ok(art.get("artifact_id", "?"), session, cap), so a
    # [^)]* class stops at the first inner ")" and the anchor never matches. Matching to the
    # line end instead — caught by this test failing on its own first run, which is the point of
    # running it before trusting it.
    m = re.search(r"if not _budget_ok\(.*?\n(.*?)\n\s*continue", src, re.S)
    check("the budget check still exists and is followed by a continue", m is not None,
          "the guarded path moved; this test would silently stop measuring it")
    if not m:
        return 1
    branch_raw = m.group(1)
    # CODE ONLY. The first version checked `"dispatch_error" not in branch` and failed on the
    # COMMENT above the log call, which names the other exit paths in prose. That is the third time
    # tonight a textual test matched its own documentation instead of the code — the defect written
    # down in test_instruments_agree_on_corpus as "a test that reads source text cannot tell an
    # APPLIED clause from a MENTIONED one". Strip comments before asserting.  # privacy-ok: generic engineering vocabulary
    branch = "\n".join(re.sub(r"#.*$", "", ln) for ln in branch_raw.splitlines())

    check("the budget path LOGS instead of skipping silently",
          "budget_capped" in branch,
          "a matched-but-suppressed artifact leaves no trace, so fire_count cannot be told apart "
          "from a rule that never matched — which is what every fitness verdict reads")

    for field, why in (
        ("artifact_id", "without it the row cannot be attributed to a rule"),
        ("cap", "a suppression count is uninterpretable without the budget that caused it — "
                "capped-at-1 and capped-at-5 are different findings"),
        ("event", "UserPromptSubmit and PreToolUse have different match rates; pooling them hides both"),
        ("session_id", "budgets are PER SESSION, so a count without a session is not a rate"),
    ):
        check(f"...and records `{field}`", f'"{field}"' in branch, why)

    # It must be a distinct action, not folded into an existing one. Reusing dispatch_error would
    # put a NORMAL outcome into the watchdog's ERROR_ACTIONS set and quarantine healthy rules.
    check("it is its own action, not folded into an error action",
          '"action": "budget_capped"' in branch and "dispatch_error" not in branch,
          "budget suppression is a normal, healthy outcome — labelling it an error would make the "
          "watchdog quarantine rules for working as designed")

    # And the watchdog must NOT treat it as an error, for the same reason.
    try:
        import friction_watchdog as wd
        check("the watchdog does NOT treat budget_capped as an error action",
              "budget_capped" not in getattr(wd, "ERROR_ACTIONS", set()),
              f"ERROR_ACTIONS={getattr(wd, 'ERROR_ACTIONS', None)} — quarantining on a normal "
              f"outcome removes working rules")
    except Exception:
        pass

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
