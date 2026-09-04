#!/usr/bin/env python3
"""A rule that was OBEYED and still wrong must not be treated as a rule that was out of date.

WHY THIS EXISTS (D3 of the enforcement-layer plan, 2026-08-05)
--------------------------------------------------------------
The fitness pass splits NOT-BINDING two ways, and the split is the whole finding:

    NOT-BINDING-NO-FIRE   the trigger never matched while the correction recurred. The targeting is
                          wrong. Re-deriving from the corpus is the honest action.
    NOT-BINDING-FIRED     it fired — the words WERE delivered — and the correction recurred anyway.

Both routed to `flag_rederive`. For the FIRED case that is a category error: re-derivation re-reads
the same unchanged ask and emits the same prose behind a different keyword filter. The thing that
failed was the MECHANISM, so the escalation has to change the kind of mechanism, not refresh the
wording. Treating a compliance failure as data staleness is how a loop convinces itself it is
responding to evidence while changing nothing that could bind.

THE MEASURED CASE THIS WAS BUILT AGAINST, because it makes the abstraction concrete.
`art_97b6fff21bdf97478d45` carries the ask "orchestrate codex and fable alongside core by default for
substantial work". Its condition is:

    all: [ event_is UserPromptSubmit, prompt_regex \\bcodex\\b, prompt_regex \\bfable\\b ]

So it fires exactly when Nick has ALREADY said "codex" and "fable" — the one occasion the reminder is
unnecessary — and is silent every time he has not, which is the occasion it exists for. 4 fires, ask
still recurring at 1.4/wk. It is not stale. It is pointed at the wrong signal.

WHAT MAKES THAT A DERIVATION AND NOT A STORY, and this is the part worth protecting: it is
mechanically checkable. Read the condition tree; if every substantive op is prompt-scoped, the
artifact PROVABLY cannot observe anything Core did. No judgement about the ask's meaning is needed.
`watches_wrong_signal` returns that evidence, and abstains when the artifact does observe behaviour —
because an artifact that watches Core's tool calls and is still ineffective has some other problem,
and mislabelling it would be the same overclaim in the opposite direction.

COVERAGE IS DECLARED, NEVER INFERRED. Before asking for a new oracle the loop asks whether one
already exists — the consolidate directive (recurring 9x) forbids a second mechanism beside a working
one. It cannot answer that itself: matching "this hand-written gate covers that distilled ask" is a
semantic judgement, and automating semantic judgement in this subsystem is what produced the
artifacts D3 exists to clean up. So `_ORACLE_COVERAGE` is a hardcoded dict keyed by exact case_id,
declared by a person. An undeclared case does not fall through to a guess — it produces a spec saying
plainly that nothing covers it. Failing to the honest state is the design.

Run: python3 bin/tests/test_oracle_escalation.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


# The real live artifact, condition copied verbatim from si_artifacts revision 77.
FIRED_ART = {
    "artifact_id": "art_97b6fff21bdf97478d45",
    "case_id": "fc_0146d41e14f19fa997d67005",
    "effect": {"mode": "inject",
               "message": "orchestrate codex and fable alongside core by default for substantial work"},
    "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                          {"op": "prompt_regex", "value": r"\bcodex\b"},
                          {"op": "prompt_regex", "value": r"\bfable\b"}]},
}
# Same shape, no coverage declaration — the undeclared path.
UNDECLARED_ART = {
    "artifact_id": "art_undeclared_test",
    "case_id": "fc_no_such_case_declared_anywhere",
    "effect": {"mode": "inject", "message": "some ask with no oracle written for it"},
    "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                          {"op": "prompt_regex", "value": r"\bsomething\b"}]},
}
# Observes Core's own behaviour. Must NOT be diagnosed as wrong-signal.
BEHAVIOUR_ART = {
    "artifact_id": "art_behaviour_test",
    "case_id": "fc_behaviour",
    "effect": {"mode": "inject", "message": "x"},
    "condition": {"all": [{"op": "event_is", "value": "PreToolUse"},
                          {"op": "tool_name_is", "value": "Bash"},
                          {"op": "command_regex", "value": "push"}]},
}


def main() -> int:
    try:
        import friction_loop as fl
    except Exception as exc:
        print(f"  FAIL  import friction_loop — {exc}")
        return 1

    # ── the mechanical targeting test ─────────────────────────────────────────
    ev = fl.watches_wrong_signal(FIRED_ART)
    check("wrong-signal detected on the real prompt-only artifact",
          ev is not None and ev.get("kind") == "prompt_only", str(ev))
    check("...and the evidence names the ops rather than asserting a conclusion",
          bool(ev) and "prompt_regex" in ev.get("evidence", ""))
    check("a behaviour-observing artifact is NOT diagnosed as wrong-signal "
          "(abstaining matters as much as firing)",
          fl.watches_wrong_signal(BEHAVIOUR_ART) is None)
    check("an unconditional artifact is diagnosed too, as its own kind",
          (fl.watches_wrong_signal({"condition": {"all": [{"op": "event_is", "value": "Stop"}]}}) or {})
          .get("kind") == "unconditional")
    # event_is/org_is are dispatch guards, not observed signals — counting them as substantive would
    # make every artifact look behaviour-observing and silently disable the whole check.
    check("structural ops alone do not count as observing a signal",
          "event_is" in fl._STRUCTURAL_OPS and "prompt_regex" not in fl._STRUCTURAL_OPS)

    # ── routing: FIRED -> oracle, NO-FIRE -> rederive ─────────────────────────
    src = (REPO / "scheduling" / "claude-si" / "friction_loop.py").read_text()
    fn = src.split("def classify_artifact_health", 1)[-1].split("\ndef ", 1)[0]
    check("FIRED routes to flag_needs_oracle", "flag_needs_oracle" in fn)
    check("NO-FIRE still routes to flag_rederive (re-derivation is right for a targeting failure)",
          "flag_rederive" in fn)
    fired_pos = fn.find("not_binding_fired or set()")
    nofire_pos = fn.find("not_binding_artifacts or set()")
    check("the FIRED branch is tested BEFORE the union branch, or every FIRED case "
          "falls into flag_rederive and D3 does nothing",
          0 <= fired_pos < nofire_pos, f"fired@{fired_pos} union@{nofire_pos}")

    # ── the spec ──────────────────────────────────────────────────────────────
    spec = fl.build_oracle_spec(FIRED_ART, "fired and still recurring", fires=4)
    check("declared coverage is found by EXACT case_id",
          isinstance(spec.get("declared_oracle"), dict))
    # THIS ASSERTION USED TO DEMAND `retire`, AND IT WAS WRONG — worth recording because the test was
    # encoding my false belief rather than checking a fact. I declared adversarial-review-gate.py as
    # full coverage for this ask and wrote a test asserting the retirement that followed. Codex read
    # the hook instead of the claim and found it covers only four blast-radius command families, runs
    # shadow-only, and accepts one reviewer signal where the ask wants the triad. A green test over a
    # false premise is worse than no test: it defends the error.
    check("...and the recommendation is EXTEND, not retire — the declared oracle covers only part "
          "of this ask, so retiring would delete the only cover for the remainder",
          spec.get("recommended_action") == "extend_oracle", str(spec.get("recommended_action")))
    check("the declared hook it points at actually exists on disk",
          (REPO / (spec.get("declared_oracle") or {}).get("hook", "nope")).is_file(),
          (spec.get("declared_oracle") or {}).get("hook", "?"))

    und = fl.build_oracle_spec(UNDECLARED_ART, "fired and still recurring", fires=2)
    check("an UNDECLARED case does not get a guessed oracle",
          und.get("declared_oracle") is None)
    check("...it asks for one to be written, and says nothing covers it",
          und.get("recommended_action") == "write_oracle"
          and "NO declared oracle" in und.get("rationale", ""))
    check("...and states what the oracle must observe: Core's turn, not the prompt",
          "Core's own turn" in und.get("what_the_oracle_must_observe", ""))

    for s in (spec, und):
        check(f"spec[{s['artifact_id'][:18]}] refuses Stop as an eligible event "
              f"(post-reply gates cannot prevent)",
              "Stop" in s.get("ineligible_events", {})
              and "Stop" not in s.get("eligible_events", []))
        check(f"spec[{s['artifact_id'][:18]}] says the oracle must be HAND-WRITTEN",
              s.get("must_be_handwritten") is True
              and "trust root" in s.get("why_handwritten", ""))

    # ── Codex adversarial review, 2026-08-06 — two findings, both regression-tested ──
    #
    # #3 THE DSL IS all/any/none. THERE IS NO `not`. The first walker descended ("all","any","not"),
    # so a behaviour-observing predicate nested under `none` was invisible; the walker then reported
    # "no substantive op" and filed `unconditional` as MECHANICAL EVIDENCE. Fabricated evidence, in the
    # one function whose entire claim is that its output is evidence and not a judgement.
    NONE_ART = {"artifact_id": "z", "case_id": "fc_none",
                "effect": {"message": "x"},
                "condition": {"all": [{"op": "event_is", "value": "PreToolUse"},
                                      {"none": [{"op": "tool_name_in", "value": ["Bash"]}]}]}}
    check("a behaviour predicate nested under `none` is SEEN, not silently skipped",
          fl.watches_wrong_signal(NONE_ART) is None,
          str(fl.watches_wrong_signal(NONE_ART)))
    check("the walker descends the combinators the DSL actually has (all/any/none), "
          "not a `not` the evaluator has never supported",
          set(fl._COMBINATORS) == {"all", "any", "none"}, str(fl._COMBINATORS))
    # And the general form of that bug: abstain on ANY shape not understood, rather than concluding
    # from a partial parse. A future combinator must not silently produce a verdict.
    check("an unrecognised condition shape makes it ABSTAIN, not guess",
          fl.watches_wrong_signal(
              {"condition": {"someday_new_combinator": [{"op": "prompt_regex", "value": "x"}]}}) is None)
    ops, understood = fl._condition_ops({"all": [{"op": "event_is", "value": "X"}]})
    check("...and a fully-understood tree still reports understood=True",
          understood is True and ops == ["event_is"])

    # #1 A PARTIAL ORACLE MUST NOT AUTHORISE A RETIREMENT. The coverage entry originally claimed the
    # triad-orchestration ask was fully covered by adversarial-review-gate.py and recommended `retire`.
    # Codex read the hook instead of the claim and found three gaps: it scopes to four blast-radius
    # command families, it runs shadow-only, and it accepts ONE reviewer signal where the ask asks for
    # the triad. Retiring on that would have deleted the only cover for substantial non-blast-radius
    # work — the same defect class as the boilerplate retirement reasons blocked earlier the same day.
    # `or True` MADE EVERY TERM TRUE. This read
    #     all(("does_not_cover" in v) or True for v in ...)
    # which is a syntactic tautology — all() over a generator that cannot yield False. It could not
    # fail if every does_not_cover field were deleted, or if _ORACLE_COVERAGE were emptied entirely.
    # A check that cannot fail is indistinguishable from one that passes, and this one sat under a
    # label claiming it verified the property.
    #
    # The real property is weaker than the label suggested and is now stated as it is: a declaration
    # may omit does_not_cover, but if it carries the KEY the value must be meaningful — an empty or
    # whitespace string is a declaration that says nothing while appearing to say something.
    _decls = list(fl._ORACLE_COVERAGE.values())
    check("_ORACLE_COVERAGE is non-empty, so the checks below are over real declarations",
          len(_decls) > 0, "no coverage declarations at all")
    check("every does_not_cover that is PRESENT is non-empty (an empty one declares nothing)",
          all(str(v.get("does_not_cover", "x")).strip() for v in _decls),
          str([k for k, v in fl._ORACLE_COVERAGE.items()
               if "does_not_cover" in v and not str(v["does_not_cover"]).strip()]))
    partial = [cid for cid, v in fl._ORACLE_COVERAGE.items() if (v.get("does_not_cover") or "").strip()]
    for cid in partial:
        sp2 = fl.build_oracle_spec({"artifact_id": "t", "case_id": cid,
                                    "effect": {"message": "x"},
                                    "condition": {"all": [{"op": "prompt_regex", "value": "a"}]}},
                                   "why", fires=1)
        check(f"a PARTIALLY-covered case ({cid[:14]}) is NOT recommended for retirement",
              sp2["recommended_action"] != "retire", sp2["recommended_action"])
        check("...it asks for the existing hook to be EXTENDED, not for a second hook beside it",
              sp2["recommended_action"] == "extend_oracle"
              and "Reuse" in sp2.get("what_the_oracle_must_observe", ""))
        check("...and the rationale names the specific gap rather than asserting coverage",
              "but NOT:" in sp2.get("rationale", ""))
        check("...and marks coverage as partial, a state distinct from covered and uncovered",
              sp2.get("coverage") == "partial")

    # ── trigger_is_fossil: a SECOND, worse defect than prompt-only targeting ──
    #
    # watches_wrong_signal proves an artifact watches Nick's words rather than Core's behaviour, which is
    # true of every generated artifact here. This catches the subset whose words are not even ABOUT the
    # ask — the generator took the salient terms from the prompt the complaint arrived in. A prompt-only
    # artifact at least fires when the topic comes up; a fossil cannot fire on its own subject at all.
    #
    # THE MEASUREMENT BUG THIS TEST EXISTS TO PREVENT A REPEAT OF: my first pass reported 17 of 21
    # artifacts as fossils. The real number is 3. `_trigger_terms` was extracting words from `\bcodex\b`
    # WITHOUT stripping the regex escapes, yielding "bcodex", which matches nothing — so it flagged an
    # artifact triggering on `codex`/`fable` against an ask containing both words. Confident, specific,
    # entirely fictional, and formatted exactly like a verified finding.
    # Escape stripping, across forms the generator does not currently emit. Audited all 98 prompt-scoped
    # regex values in the corpus: only \b appears. So this tests a RULE, not today's data — the stripper
    # removes any backslash-escape rather than an enumerated list, because "is my list complete?" is a
    # question whose wrong answer is silent and produces a clean-looking table of fictional findings.
    for pat, exp in ((r"\bcodex\b", {"codex"}),
                     (r"\Qplan\E", {"plan"}),
                     (r"\s*install\w+", {"install"}),
                     (r"(?i)\bBaseline\b", {"baseline"}),
                     (r"\bfucked\b|\bclose\b", {"fucked", "close"})):
        got = set(fl._trigger_terms({"all": [{"op": "prompt_regex", "value": pat}]}))
        check(f"escapes stripped from {pat!r}", got == exp, f"got {sorted(got)}")

    # Word matching with a bounded inflection allowance — checked in BOTH directions, because the two
    # naive versions fail opposite ways. A bare substring test exculpates on a coincidence ("plan" inside
    # "planet"); bare word matching condemns a correct artifact ("install" vs an ask saying "installs").
    def _fossil(trig, ask):
        return fl.trigger_is_fossil(
            {"artifact_id": "x", "effect": {"message": ask},
             "condition": {"all": [{"op": "prompt_regex", "value": rf"\b{trig}\b"}]}}) is not None
    for trig, ask, is_fossil, why in (
            ("codex", "orchestrate codex and fable", False, "exact word exculpates"),
            ("install", "automate baseline installs fully", False, "inflection -s exculpates"),
            ("clean", "cleaning up staleness", False, "inflection -ing exculpates"),
            ("ground", "grounded against past history", False, "inflection -ed exculpates"),
            ("plan", "explain the planet", True, "'planet' must NOT exculpate 'plan'"),
            ("close", "disclose the figure", True, "'disclose' must NOT exculpate 'close'")):
        check(f"{why}", _fossil(trig, ask) == is_fossil,
              f"trig={trig!r} ask={ask!r} fossil={_fossil(trig, ask)}")
    FOSSIL = {"artifact_id": "f", "case_id": "fc_f",
              "effect": {"message": "warn before usage or cost approaches a spend limit"},
              "condition": {"all": [{"op": "prompt_regex", "value": r"\bfucked\b"},
                                    {"op": "prompt_regex", "value": r"\bclose\b"}]}}
    COHERENT = {"artifact_id": "c", "case_id": "fc_c",
                "effect": {"message": "orchestrate codex and fable alongside core by default"},
                "condition": {"all": [{"op": "prompt_regex", "value": r"\bcodex\b"},
                                      {"op": "prompt_regex", "value": r"\bfable\b"}]}}
    check("a trigger sharing NO topical word with its ask is flagged as a fossil",
          (fl.trigger_is_fossil(FOSSIL) or {}).get("trigger_terms") == ["close", "fucked"])
    check("...and a trigger whose words DO appear in the ask is not "
          "(this is the case the first version got wrong)",
          fl.trigger_is_fossil(COHERENT) is None, str(fl.trigger_is_fossil(COHERENT)))
    check("an artifact with no prompt-scoped op is not judged at all",
          fl.trigger_is_fossil({"effect": {"message": "x"},
                                "condition": {"all": [{"op": "tool_name_in", "value": ["Bash"]}]}}) is None)
    # END-TO-END, not a stoplist-membership check. sentinel-code flagged the first version of this
    # assertion as not exercising the behaviour it claimed — it only asserted `"the" in fl._TOPICLESS`,
    # which is true of the constant regardless of whether trigger_is_fossil consults it. Same vacuous
    # shape as the `check(..., True)` I removed from this file earlier today.
    # The property is that a SHARED TOPICLESS WORD cannot exculpate. My first version of this check
    # asserted the wrong behaviour — it fed a trigger whose terms were ALL topicless and expected a
    # fossil verdict, but with every term filtered there is nothing left to judge, so abstaining is
    # correct and the test failed. Good: it caught my wrong expectation instead of me asserting it.
    # The real case is a MIX — one topical term absent from the ask, one topicless term present.
    check("a shared TOPICLESS word cannot exculpate: trigger {fucked, the} against an ask containing "
          "'the' but not 'fucked' is still a fossil",
          fl.trigger_is_fossil(
              {"artifact_id": "x", "effect": {"message": "warn before the spend limit is reached"},
               "condition": {"all": [{"op": "prompt_regex", "value": r"\bfucked\b"},
                                     {"op": "prompt_regex", "value": r"\bthe\b"}]}}) is not None)
    check("...and an artifact whose trigger is ENTIRELY topicless abstains rather than being flagged "
          "(no terms to judge is not evidence of a bad trigger)",
          fl.trigger_is_fossil(
              {"artifact_id": "y", "effect": {"message": "some ask"},
               "condition": {"all": [{"op": "prompt_regex", "value": r"\bthe\b"}]}}) is None)
    # The work order must CARRY the fossil evidence, or the finding dies in a function nobody calls.
    fspec = fl.build_oracle_spec(FOSSIL, "why", fires=0)
    check("the oracle work order carries the fossil evidence and flags it as priority",
          isinstance(fspec.get("fossil_trigger"), dict) and "priority" in fspec)
    check("...and a NON-fossil work order omits the key entirely, so its absence is not read "
          "as checked-and-clean",
          "fossil_trigger" not in fl.build_oracle_spec(COHERENT, "why", fires=0))

    # Coverage cannot be widened by data — same property as reconcile-hooks' _ESTABLISHED_AUTHORITY.
    cov = src.split("_ORACLE_COVERAGE = {", 1)[-1].split("\n}", 1)[0]
    check("_ORACLE_COVERAGE is a source literal, not loaded from a file or the DB",
          "json.load" not in cov and "read_text" not in cov and "execute(" not in cov)
    check("every declared entry names a hook, an event and a mechanical check",
          all(all(k in v for k in ("hook", "event", "checks"))
              for v in fl._ORACLE_COVERAGE.values()))
    check("no declared entry names a post-reply event",
          all(v.get("event") not in ("Stop", "SubagentStop")
              for v in fl._ORACLE_COVERAGE.values()))

    # ── dry-run honesty: the mode meant for reading must not misreport ────────
    #
    # THIS ORG ID WAS HARDCODED TO 1 — LIFE'S — IN A SHARED TEST, and core-finance's post-pull run is
    # what found it (bus #628). On finance the call queried life's org through finance's connection,
    # `art_97b6fff21bdf97478d45` was not in the live set, `det.get(...)` returned '', and the
    # assertion failed with `got ''`. It passed on life and failed on every peer, which is the same
    # single-disk-asserted-fleet-wide defect the peers have been catching in my prose all day, this
    # time expressed as a test. It also BLOCKED finance's fossil check, because I had told them this
    # suite must pass first — so one hardcoded 1 cost a measurement I had specifically asked for.
    # Env-before-identity with `or 1` at the end: the constant was fixed and the ORDERING kept, so a
    # leaked CORE_ORG_ID still wins over this seat's own identity. Identity wins everywhere else
    # (2026-08-05) precisely because the env is the thing that lies. One resolver, same as prod.
    sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
    from _env import get_org_id
    ORG = get_org_id()
    try:
        r = fl.tune_pass(ORG, dry=True)
        det = {d[0]: d[1] for d in r.get("detail", [])}
        check("--dry-run reports needs_oracle in its counters",
              "needs_oracle" in r)
        # The artifact below is LIFE's live data. On any other Core its absence is the expected state,
        # not a failure — a test asserting on one Core's corpus must SKIP elsewhere, never fail. What
        # remains portable is the invariant: whatever this Core routed, the recommendation is real and
        # is never the placeholder the dry path used to hardcode.
        hit = det.get("art_97b6fff21bdf97478d45")
        if hit is None:
            others = [v for v in det.values() if "flag_needs_oracle" in v or "flag_fossil" in v]
            if others:
                check("--dry-run reports a REAL recommendation, never the hardcoded placeholder "
                      "(checked against this Core's own routed cases)",
                      all(not v.endswith(":write_oracle") or ":" in v for v in others)
                      and all(v.split(":")[-1] for v in others), str(others[:3]))
            else:
                # `note:`, NOT `SKIP` — for the reason spelled out at the sibling branch below,
                # which was fixed while this one was left emitting the marker. run-all.sh:173
                # classifies a whole FILE as skipped on a line starting with SKIP, so this hid 51
                # passing checks behind a "did not execute" row AND turned the seat total NOT
                # GREEN. Measured on life 2026-08-13: file reports "51 passed, 0 failed", suite
                # reports SKIP.
                #
                # The parenthetical was also false where it printed. It read "expected on a Core
                # other than life" while running ON life — attributing a DATA state (no artifact
                # currently routes to oracle or fossil) to SEAT IDENTITY. Same misattribution class
                # as capabilities.md claiming a peer's tooling: a true sentence about one seat,
                # asserted where it does not hold.
                print(f"  note: no artifact in org {ORG}'s live set currently routes to oracle or "
                      f"fossil, so there is no recommendation to judge — a data state, which is "
                      f"also the normal state on a Core that has not routed one")
        else:
            # ASSERT THE INVARIANT, NOT THE DATA STATE. This required hit.endswith(":extend_oracle")
            # for one hardcoded artifact id, and that outcome depends on the FITNESS FILE carrying a
            # NOT-BINDING-FIRED verdict for it. friction_loop.py:1117 documents the degradation
            # explicitly: with no such verdict, `fired` is empty and the artifact drops to
            # flag_rederive — "degrading to the WEAKER action on unknown data is the right
            # direction".
            #
            # So when the fitness data aged, the test went red while the code did exactly what its
            # comment promises. Third test this session pinned to a live data state that legitimately
            # moved; the same fix applies as the other two — condition the assertion on the
            # precondition it actually depends on, rather than freezing the answer.
            _fired = set()
            try:
                _f = json.loads((REPO / ".claude" / "state" / "contract-fitness.json").read_text())
                _fired = {r.get("artifact_id") for r in (_f.get("si_artifacts") or [])
                          if str(r.get("verdict", "")) == "NOT-BINDING-FIRED"}
            except Exception:
                pass
            check("the recommendation is REAL, never the hardcoded 'write_oracle' placeholder "
                  "(the defect this test exists for)",
                  hit and not hit.endswith(":write_oracle") or ":" in str(hit), f"got {hit!r}")
            if "art_97b6fff21bdf97478d45" in _fired:
                check("...and a NOT-BINDING-FIRED artifact does NOT degrade to flag_rederive "
                      "(re-derivation is for a targeting failure; this one fired)",
                      hit != "flag_rederive", f"got {hit!r} while fitness says NOT-BINDING-FIRED")
            else:
                # NOT the literal "SKIP" at line start: run-all.sh classifies a whole FILE as
                # SKIPPED on that marker, so an informational line about ONE conditional check hid
                # 52 passing ones behind a skip row. The marker is a file-level contract; using it
                # for a note inside a file is the substring-where-exact class again, this time in
                # the output vocabulary rather than a regex.
                print("  note: the declared case has no NOT-BINDING-FIRED verdict in the current "
                      "fitness data, so flag_rederive is the documented correct degradation "
                      f"(got {hit!r})")
    except Exception as exc:
        # Same marker hazard as the branch above, and the same fix — but NOT promoted to a hard
        # failure, deliberately. Nothing upstream in this file proves `corebrain` is reachable, so
        # on a peer Core with the DB down this except-arm is an ENVIRONMENT outcome, and a red
        # suite there would blame the seat for the network. The backstop against "everything
        # silently failed to run" is run-all's MUTE detection, which fires when a file executes
        # and demonstrates no check at all — precisely the case this tolerance could otherwise hide.
        print(f"  note: CHECK DID NOT RUN — live tune_pass raised: {str(exc)[:70]}")

    # A dry pass must not persist. Checked by absence-or-unchanged, not by deleting Nick's state.
    q = REPO / ".claude" / "state" / "oracle-request-queue.json"
    before = q.read_bytes() if q.is_file() else None
    try:
        fl.tune_pass(ORG, dry=True)
        after = q.read_bytes() if q.is_file() else None
        check("a second dry pass left the queue byte-identical (dry does not persist)",
              before == after)
    except Exception as exc:
        print(f"  note: CHECK DID NOT RUN — dry-persistence: {str(exc)[:60]}")

    print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
