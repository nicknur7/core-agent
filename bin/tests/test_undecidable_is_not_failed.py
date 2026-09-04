#!/usr/bin/env python3
"""A gate refusal that means "no conclusion exists" must not be recorded as a failure.

WHY THIS EXISTS (2026-08-12, found by core-finance on their own seat). `friction_test_gate.gate()`
returns `(False, reason)` for three materially different things:

    DECIDED-BAD      "over-broad: fires on 83/150 real prompts (55%)"   the artifact is unsafe
    MALFORMED SPEC   "duplicate example ids"                           the submission is broken
    CANNOT YET TELL  "corpus too small (31<40)"                        NOTHING was determined

`friction_installer` logged all three as `test_fail` with no class field, and nothing read the
reason. Measured on core-finance: **234 `test_fail` rows, 234 of them undecidable** — two artifacts
retried 117 times each between 2026-08-04 and 08-12, 100 of those on the final day, against a corpus
that grew 27 -> 31 versus a threshold of 40. An unsatisfiable precondition retried as if transient,
with the retry rate RISING.

The wasted work is the smaller cost. A reader of that log sees 234 specificity failures against two
artifacts and concludes the seat has two badly-broken rules. The truth is the opposite: those two
had never been tested at all. A correct refusal, recorded as a verdict it never reached.

INVISIBLE FROM LIFE, which is why it needed a second seat to find. Life's corpus is several times
the threshold, so the undecidable branch never executes here. That is the third defect tonight whose
discriminating evidence lived on a seat other than the author's.

WHAT THIS ASSERTS. Not "undecidable should install" — it must NOT; an untested artifact installing is
strictly worse. The assertion is that the three refusal kinds stay DISTINGUISHABLE, in both
directions: a genuine failure must never be classified undecidable (which would let a real defect
read as "we couldn't tell"), and an undecidable must never be classified as failure.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _spec_and_examples():
    spec = {"type": "contract", "event": "UserPromptSubmit",
            "condition": {"all": [{"op": "prompt_regex", "value": r"\bzzzunlikelytoken\b"}]},
            "effect": {"mode": "inject", "message": "x"},
            "tests": {"positive_ids": ["p1"], "negative_ids": ["n1", "n2"]}}
    ex = {"positive": [{"id": "p1", "expected": "fire", "event": "UserPromptSubmit",
                        "hook_input": {"prompt": "zzzunlikelytoken"}, "provenance": "a"}],
          "negative": [{"id": "n1", "expected": "no_fire", "event": "UserPromptSubmit",
                        "hook_input": {"prompt": "nope"}, "provenance": "event_mismatch"},
                       {"id": "n2", "expected": "no_fire", "event": "UserPromptSubmit",
                        "hook_input": {"prompt": "also nope"}, "provenance": "polarity_mutation"}]}
    return spec, ex


def main() -> int:
    print("test_undecidable_is_not_failed")
    try:
        import friction_test_gate as tg
    except Exception as e:  # noqa: BLE001  # privacy-ok: noqa linter directive, not a course code
        print(f"  FAIL  cannot import friction_test_gate: {e}")
        return 1

    check("the gate exposes the undecidable marker and its predicate",
          hasattr(tg, "UNDECIDABLE") and callable(getattr(tg, "is_undecidable", None)),
          "the classification was removed or renamed; without it every refusal collapses back into "
          "one bucket, which is the defect this file guards")
    if not (hasattr(tg, "UNDECIDABLE") and callable(getattr(tg, "is_undecidable", None))):
        return 1

    spec, ex = _spec_and_examples()
    floor = getattr(tg, "MIN_CORPUS", 40)

    # --- UNDECIDABLE: below the corpus floor, nothing can be concluded ------------------------
    ok, why = tg.gate(spec, ex, corpus_prompts=["p"] * max(1, floor // 4))
    check(f"a corpus under MIN_CORPUS={floor} is UNDECIDABLE, not a failure",
          ok is False and tg.is_undecidable(why),
          f"ok={ok} why={why!r}. This is the live case: 234 of 234 refusals on core-finance were "
          f"this, logged as test_fail, and read as two badly-broken rules that had in fact never "
          f"been tested.")
    check("...and it still REFUSES — an untested artifact must not install",
          ok is False, "an undecidable verdict must never become an install")

    ok, why = tg.gate(spec, ex, corpus_prompts=None)
    # corpus_prompts=None means "not applicable" (worksheet path), which is a pass-through, not a
    # refusal — asserted so a future change that starts refusing here is noticed.
    check("corpus_prompts=None is not treated as an undecidable refusal",
          not (ok is False and tg.is_undecidable(why)),
          f"ok={ok} why={why!r} — the None path is the deliberate exemption, not a missing corpus")

    # --- THE OTHER DIRECTION: real failures must NOT be excused as undecidable ----------------
    ok, why = tg.gate(spec, ex, corpus_prompts=["zzzunlikelytoken"] * (floor + 10))
    check("an OVER-BROAD rule is a real failure, never undecidable",
          ok is False and not tg.is_undecidable(why),
          f"why={why!r}. Misclassifying a decided defect as 'we could not tell' is the expensive "
          f"direction — it would silence a rule that fires on everything.")

    bad = dict(spec)
    bad["tests"] = {"positive_ids": ["p1", "p1"], "negative_ids": ["n1", "n2"]}
    ok, why = tg.gate(bad, ex, corpus_prompts=["harmless"] * (floor + 10))
    check("a MALFORMED spec is a real failure, never undecidable",
          ok is False and not tg.is_undecidable(why), f"why={why!r}")

    # --- and the gate must still be able to PASS, or every check above is vacuous -------------
    ok, why = tg.gate(spec, ex, corpus_prompts=["harmless prompt"] * (floor + 10))
    check("a clean spec against a sufficient corpus still PASSES",
          ok is True,
          f"ok={ok} why={why!r} — if nothing can pass, the classification above is measuring a "
          f"gate that refuses everything")

    # --- THE FORGERY ATTACK A MARKER-PREFIX SCHEME INVITES -------------------------------------
    # Contributed by core-finance, who attacked the scheme rather than re-running my tests. Example
    # ids are ARTIFACT-CONTROLLED and they land inside reason strings, so a spec can try to make its
    # own failure read as "we could not tell" — turning a decided defect into a non-verdict.
    #
    # It holds, and for a structural reason worth pinning: `is_undecidable` uses .startswith(), and
    # every artifact-controlled interpolation in gate() sits behind a static prefix
    # (`positive {id}…`, `negative {id} ({prov})…`), so position 0 is never attacker-reachable.
    # THAT is the property under test. If anyone ever adds a reason string that OPENS with spec
    # content, the property dies silently and this assertion is the only thing that would notice.
    evil_spec, evil_ex = _spec_and_examples()
    forged = f"{tg.UNDECIDABLE}pwned"
    evil_spec["tests"]["positive_ids"] = [forged]
    evil_ex["positive"][0]["id"] = forged
    evil_ex["positive"][0]["hook_input"] = {"prompt": "will not match"}   # force the failure path
    ok, why = tg.gate(evil_spec, evil_ex, corpus_prompts=["harmless"] * (floor + 10))
    check("an artifact CANNOT forge the undecidable marker via a spec-controlled example id",
          ok is False and not tg.is_undecidable(why),
          f"why={why!r} — a spec that can classify its own failure as 'no conclusion' would launder "
          f"a decided defect into a non-verdict. Holds only because every spec-controlled "
          f"interpolation sits behind a static prefix; if that changes, fix the prefix, not this "
          f"test.")

    # --- DEDUP ON UNCHANGED STATE, and it must FAIL OPEN ---------------------------------------
    import friction_installer as fi
    import tempfile
    real_log = fi.ACTION_LOG
    try:
        fi.ACTION_LOG = Path(tempfile.mkdtemp()) / "a.jsonl"
        check("an unseen artifact/corpus is always new", fi._undecidable_is_new("art_t", 31))
        fi._log("test_undecidable", artifact_id="art_t", reason="r", corpus_n=31)
        check("the SAME corpus size is deduped — a repeat carries no information",
              not fi._undecidable_is_new("art_t", 31),
              "1800 projected rows of one unsatisfiable state would bury the log this seat uses "
              "as a diagnostic surface")
        check("a GROWN corpus is new — the retry is never delayed, only the duplicate row",
              fi._undecidable_is_new("art_t", 32),
              "if this fails, crossing MIN_CORPUS goes unrecorded, which is the capability a "
              "backoff would have cost and this design exists to keep")
        fi.ACTION_LOG = Path("/nonexistent/dir/a.jsonl")
        check("an unreadable log FAILS OPEN — over-logging beats a dropped observation",
              fi._undecidable_is_new("art_t", 31),
              "a dedup that swallows records when it cannot read its own history converts a "
              "logging optimisation into data loss")
    finally:
        fi.ACTION_LOG = real_log

    # --- the caller must record the distinction, not just compute it --------------------------
    src = (REPO / "scheduling" / "claude-si" / "friction_installer.py").read_text()
    check("the installer logs undecidable as its own action, not as test_fail",
          "test_undecidable" in src and "is_undecidable" in src,
          "the gate can distinguish the cases but the caller collapses them again — which is "
          "exactly the state that produced 234 mislabelled rows")
    check("...and records the corpus size, the thing that has to move",
          "corpus_n" in src,
          "a refusal that does not say what would change it reads as permanent")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
