#!/usr/bin/env python3
"""artifact_generator.py — the SELF-BUILDING generator (WS4). Turns a routed recurring-ask into the
RIGHT artifact, JSON-FILL ONLY from hash-pinned templates. No generated executable code, ever.

  generate(org, case, route) -> {action, ...}
    already_covered     -> {action: "skip"}
    inject_contract     -> {action: "install_contract", spec, examples}         (live, via existing gate)
    enforcement_block   -> {action: "install_shadow_block", spec, examples}      (enforced=false, template-locked)
    hooked_skill        -> {action: "install_hooked_skill", spec, examples}   (live; gated trigger + payload)
                        -> {action: "hooked_skill_pending", artifact_id, ask}       (payload not authored yet)
    claude_md_directive -> {action: "directive", result}                          (auto-applied to local CLAUDE.md)

Enforcement condition/event/effect come VERBATIM from a hash-verified template — never from case data.
Only the identity + message + tests are filled. enforced is forced False here and again at install.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# bin/ holds steering_load, the single source for the always-loaded set the budget gate below
# compares against. Resolved from THIS file's location, not CORE_INSTANCE, so the module and the
# Core it measures cannot come from different seats.
sys.path.insert(0, str(HERE.parents[1] / "bin"))
import ask_miner  # noqa: E402

TPL_DIR = HERE / "templates"

# The TRUST ANCHOR lives in REVIEWED CODE, not the adjacent (writable) manifest file — so editing the
# JSON template can't change enforcement behavior without also editing this constant in a code review
# (Codex WS4 blocker 4). Regenerating a template is a deliberate code change, not a data edit.
EXPECTED_TEMPLATE_HASHES = {
    "deliverable_as_artifact": "26e32f59b3d6f943cff2c4d62299f9c1a068ebb57ce465d7fda2698c2e3d19ec",
    "adversarial_review_before_blast_radius": "ddc071b65d7aac3d8d51e82b5ba29c40d921940dc0fbaef1d83311385a15c1d4",
}


def _template_hash(v: dict) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()


def _load_templates() -> dict:
    tpl = json.loads((TPL_DIR / "enforcement-templates.json").read_text())
    if set(tpl) != set(EXPECTED_TEMPLATE_HASHES):
        raise ValueError("enforcement template set differs from the code trust-anchor — refusing")
    for k, v in tpl.items():
        if _template_hash(v) != EXPECTED_TEMPLATE_HASHES[k]:
            raise ValueError(f"template {k} hash mismatch vs code trust-anchor — refusing (tamper)")
    return tpl


def _aid(case_id: str, kind: str) -> str:
    return "art_" + hashlib.sha256(f"{kind}|{case_id}".encode()).hexdigest()[:20]


def _gen_block(org: int, case: dict, route: dict) -> tuple[dict, dict]:
    """Build a SHADOW block spec from the hash-pinned template for route['oracle']. Condition/event/effect
    are copied verbatim from the template; only identity + tests are filled. enforced forced False."""
    tpl = _load_templates()[route["oracle"]]
    real_hash = EXPECTED_TEMPLATE_HASHES[route["oracle"]]  # persist the VERIFIED digest, not "pinned"
    aid = _aid(case["case_id"], "block")
    spec = {
        "spec_version": 1, "artifact_id": aid, "case_id": case["case_id"], "org_id": org,
        "type": "contract", "event": tpl["event"], "condition": tpl["condition"],
        "effect": {"mode": "block", "message": tpl["message"][:2000], "skill_id": None},
        "enforced": False,  # SHADOW — never enforces until friction_promote window + explicit flip
        "tests": {"positive_ids": ["p1"], "negative_ids": ["n_delivered", "n_notreq", "n_evt"]},
        "template": {"id": tpl["template_id"], "sha256": real_hash}, "scope": "org_local",
        "lease": {"max_fires_per_session": 2, "expires_at": None},
        "generator_version": "artifact-generator/1", "_provenance": "enforcement",
    }
    # truth-table examples (oracle values pinned via _test) — the block's real safety proof. Negative
    # provenances include event_mismatch + polarity_mutation (the gate's mandatory block diversity).
    def ex(eid, expected, ev, prov, **flags):
        return {"id": eid, "event": ev, "expected": expected, "provenance": prov,
                "hook_input": {"event": ev, "_test": True, "session_id": "t", **flags}}
    # Examples are ORACLE-SPECIFIC. This builder originally hardcoded the deliverable oracle's flags,
    # which was invisible while there was only one oracle — the moment a second was added, its block
    # failed the gate with "positive p1 did NOT fire", because the positive was asserting the wrong
    # oracle's state. Each oracle owns its truth table, keyed on condition_kind.
    kind = route.get("condition_kind") or "artifact_delivery"
    if kind == "adversarial_review":
        # block iff a blast-radius action happened AND no adversarial review ran
        pos = [ex("p1", "fire", "Stop", "real_positive",
                  state_flags={"blast_radius_action": True}, adversarial_review=False)]
        neg = [ex("n_reviewed", "no_fire", "Stop", "polarity_mutation",
                  state_flags={"blast_radius_action": True}, adversarial_review=True),
               ex("n_noblast", "no_fire", "Stop", "real_neighbor",
                  state_flags={"blast_radius_action": False}, adversarial_review=False),
               ex("n_evt", "no_fire", "UserPromptSubmit", "event_mismatch",
                  state_flags={"blast_radius_action": True}, adversarial_review=False)]
    else:
        pos = [ex("p1", "fire", "Stop", "real_positive", state_flags={"deliverable_requested": True}, artifact_delivered=False)]
        neg = [ex("n_delivered", "no_fire", "Stop", "polarity_mutation", state_flags={"deliverable_requested": True}, artifact_delivered=True),
               ex("n_notreq", "no_fire", "Stop", "real_neighbor", state_flags={"deliverable_requested": False}, artifact_delivered=False),
               ex("n_evt", "no_fire", "UserPromptSubmit", "event_mismatch", state_flags={"deliverable_requested": True}, artifact_delivered=False)]
    spec["tests"] = {"positive_ids": [p["id"] for p in pos], "negative_ids": [n["id"] for n in neg]}
    return spec, {"positive": pos, "negative": neg}


REPO = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[1])
CLAUDE_MD = REPO / "CLAUDE.md"
_AUTO_START = "<!-- AUTO-DIRECTIVES:START (self-building — git-reversible) -->"
_AUTO_END = "<!-- AUTO-DIRECTIVES:END -->"


def _sanitize_directive(ask: str) -> str:
    """One safe single line of plain text — corpus data can NEVER become markup/markers/instructions.
    Collapse whitespace, strip markdown/HTML-comment control chars, bound length (Codex WS4)."""
    s = re.sub(r"\s+", " ", ask or "").strip()
    s = re.sub(r"[<>`*_#\[\]{}|]", "", s)      # no markdown/HTML control chars (kills marker injection)
    s = re.sub(r"AUTO-DIRECTIVES", "", s, flags=re.I)  # never echo the section marker keyword
    return s[:160]


def promote_proven_contract(org: int, contract: dict) -> dict:
    """Graduate a PROVEN inject-mode contract into a standing CLAUDE.md directive.

    Nick approved this 2026-08-17 ("3 yes"), choosing the CLAUDE.md target over inject->block: the
    directive path is append-only, git-reversible, and already carries his 2026-07-23 ruling that
    reversibility IS the safety. A block can stop his own work and a git revert does not give the
    turn back.

    THREE GATES, ALL REQUIRED, ALL FAIL-CLOSED:

      1. verdict == DECAYING. The correction this contract addresses is recurring measurably less
         since it installed. Computed from pattern_observations, which is LIVE on every seat —
         deliberately NOT fire_count, which on life is frozen at 2026-08-05 because the classifier
         was disabled 2026-07-23 (T067). A promoter keyed on fire_count would promote on a dead
         counter.

      2. key in live_trigger_keys(). The 2026-06-23 L4 dedup removed 'stop-and-plan' and
         'frustration-deescalate' from the classifier because stop-signal-gate.py already covers
         those signals — measured at 40/73 double-injection. Both STILL read DECAYING on live data,
         so gate 1 alone would promote exactly the two contracts a human deliberately retired. This
         is the guard that stops the loop re-creating a fix someone removed on purpose.

      3. auto_apply_directive's own dedupe. It refuses when the text is already present and refuses
         a corrupt marker section.

    Returns a dict; never raises. Any failure returns a skip rather than proceeding."""
    try:
        verdict = str(contract.get("verdict") or "")
        key = str(contract.get("contract") or "").split("—")[0].strip()
        if not verdict.startswith("DECAYING"):
            return {"action": "promote_skipped", "key": key, "reason": f"verdict {verdict!r} is not DECAYING"}
        live = live_trigger_keys()
        if not live:
            return {"action": "promote_skipped", "key": key, "reason": "trigger list unreadable — failing closed"}
        if key not in live:
            return {"action": "promote_skipped", "key": key,
                    "reason": "deliberately retired from learned-classifier TRIGGERS — never promote"}
        # GATE 3 — STEERING BUDGET. CLAUDE.md is inside an always-loaded token budget whose
        # ratchet can only fall (bin/tests/test_steering_budget.py). A promotion ADDS to that
        # file, so an unbudgeted promoter breaches the ratchet on its first success — measured:
        # two directives took life from 10,559 (ceiling+tolerance) to 10,606 and turned the suite
        # red. Refuse and SAY SO rather than promote into a breach; a silent refusal would make
        # this look like the loop simply never finding candidates, which is the exact invisibility
        # this fleet spent 2026-08-17 diagnosing elsewhere.
        # Computed IN-PROCESS, deliberately. test_ws4_generator.py asserts this module never
        # shells out — it greps the WHOLE FILE for the forbidden call's name, so even naming it in
        # a comment turns the suite red (learned twice: a comment containing "spawn" broke
        # test_pipeline_exhaust_filter.py earlier the same day). My first attempt ran the budget
        # test as a child process and failed on exactly that. Reading the same two inputs the test
        # reads is cheaper and permitted.
        try:
            import json as _j
            _bl = REPO / ".claude" / "state" / ".steering-budget-baseline.json"
            # FAIL-CLOSED ON A MISSING BASELINE (2026-08-20, found by sentinel-code, twice,
            # independently). This read `... if _bl.is_file() else None`, and a None ceiling skipped
            # the entire `if` body — the only place that can block or raise — so execution fell
            # through to auto_apply_directive with NO budget check at all. The docstring said "THREE
            # GATES, ALL REQUIRED, ALL FAIL-CLOSED" and the commit said "refuse and SAY SO rather
            # than promote into a breach"; both were false for the one input that makes the gate
            # unevaluable.
            #
            # Dormant on all five seats today — every one has recorded a baseline (business 16368,
            # school 12888, finance 11064). It is live for a FRESH seat or a fork, which is exactly
            # where an unmetered directive-writer is least wanted and least likely to be noticed.
            # An absent baseline is not "no limit", it is "the limit is unknown", and the whole
            # point of a ratchet is that unknown means stop.
            if not _bl.is_file():
                return {"action": "promote_skipped", "key": key,
                        "reason": "no steering-budget baseline recorded on this seat — the ceiling "
                                  "is unknown, not absent; run the budget test to record one"}
            # MEASURED BY bin/steering_load.py, WHICH THE RATCHET TEST ALSO USES.
            #
            # This block used to read the nine-file ceiling and subtract CLAUDE.md ALONE, under a
            # comment claiming "same coarse tok proxy the test uses". The proxy was the same; the
            # SCOPE was not. Found by core-business 2026-08-20: the gate reported 15,669 tok of
            # headroom on a seat 7,441 tok IN BREACH, and business had already reproduced that
            # arithmetic to tell Nick a directive write was cleared. school: 12,166 reported free
            # while 3,739 over. life: ~9,500 reported against a true 23.
            #
            # It failed OPEN in precisely the case it exists to catch, under a docstring promising
            # "THREE GATES, ALL REQUIRED, ALL FAIL-CLOSED". The list and the arithmetic now live in
            # one module both callers import, because a second copy is what drifted.
            import steering_load as _sl
            _headroom, _total, _ceiling = _sl.headroom(REPO)
            if _ceiling is not None:
                if _headroom < 80:                            # a directive line costs ~30-60 tok
                    return {"action": "promote_blocked", "key": key,
                            "reason": f"steering budget has {_headroom} tok headroom against its "
                                      f"ratchet — compress a steering file first, or raise the "
                                      f"ceiling deliberately"}
        except Exception:
            return {"action": "promote_skipped", "key": key,
                    "reason": "steering budget unverifiable — failing closed"}
        shape = contract.get("required_shape") or contract.get("situation") or ""
        if isinstance(shape, (list, tuple)):
            shape = shape[0] if shape else ""
        case = {"user_wanted": str(shape),
                "support": {"count": int(contract.get("pre_count") or 0)}}
        return {"action": "promote_attempted", "key": key,
                "result": auto_apply_directive(org, case)}
    except Exception as e:
        return {"action": "promote_error", "reason": f"{type(e).__name__}: {e}"}


def live_trigger_keys() -> set:
    """The inverse of retired_contract_keys, and the one callers should use. Empty set on any
    failure, which makes every promotion refuse rather than proceed on a bad parse."""
    import re as _re
    try:
        src = (Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "learned-classifier.py").read_text()
        m = _re.search(r"TRIGGERS\s*=\s*\{(.*?)\n\}", src, _re.S)
        if not m:
            return set()
        return set(_re.findall(r'"([a-z0-9-]+)"\s*:', m.group(1)))
    except Exception:
        return set()


def auto_apply_directive(org: int, case: dict) -> dict:
    """AUTONOMOUSLY append a data-grounded standing directive to a managed, clearly-marked section of the
    LOCAL life CLAUDE.md (never the shared base; append-only; deduped). Reversible via git — that IS the
    safety (Nick 2026-07-23: reversibility, not a human gate). Sanitized single line; atomic unique
    write; refuses a non-regular/symlinked target. Fail-open."""
    import os as _os
    import stat as _stat

    # UNATTENDED RUNS DO NOT EDIT STEERING (2026-08-28, found by core-school on bus #5707 and
    # independently confirmed by ops, business and finance within minutes).
    #
    # Nick approved this function on 2026-08-17 ("3 yes"), choosing CLAUDE.md over inject->block.
    # That approval was for SESSION CLOSE: a session running, him reachable, the result in front of
    # him. On 2026-08-28 I added turn_the_crank() to bin/si-drain.sh, which calls
    # friction_loop.run() from a 03:10 LaunchAgent — making this same path reachable with nobody
    # present, on five seats.
    #
    # I ASKED NICK FOR THE WRONG THING. The question I put to him was whether the Cores could
    # "install and retire their own artifacts nightly, with nobody watching." He said yes to that.
    # He was never told it also rewrites CLAUDE.md — the steering file that loads on EVERY turn, on
    # a seat he is not looking at. school's framing is exact: "Nick approved the mechanism does not
    # carry over to Nick approved it firing at 03:10 while he is asleep." The consent I obtained
    # was real; the description I obtained it on was incomplete, and that is on me, not on him.
    #
    # This does not remove the capability — it withholds it from the context Nick did not authorise
    # and keeps it in the one he did. The proposal is RETURNED rather than dropped, so an unattended
    # run still surfaces exactly what it would have written and nothing is silently lost.
    #
    # Set by bin/si-drain.sh only. A session close never sets it, so close behaves as before.
    if _os.environ.get("CORE_SI_UNATTENDED") == "1":
        _proposed = " ".join(str(case.get("canonical_ask") or case.get("user_wanted") or "").split())
        return {"action": "directive_withheld_unattended",
                "reason": ("unattended run (CORE_SI_UNATTENDED=1) — CLAUDE.md is steering that loads "
                           "every turn; the operator's 2026-08-17 approval covers session close, not a 03:10 "
                           "job with nobody present. Proposal surfaced, not applied."),
                "proposed": _proposed[:200]}

    # THE BUDGET GATE LIVES HERE, AT THE WRITE POINT, BECAUSE THE OTHER ONE GUARDED A DOOR NOBODY
    # USED (2026-08-20, found by core-business).
    #
    # There are two ways a directive reaches CLAUDE.md:
    #   promote_proven_contract -> here     budget-gated since this morning
    #   **generate() -> here**              the SI loop's own dispatch. **UNGATED.**
    #
    # I fixed the arithmetic in the first one, verified it, shipped it to the baseline, and told
    # four peers "claude_md_directive promotion is blocked on all five seats." That was false for
    # the path the loop actually takes. A gate on one of two callers is not a gate; it is a gate
    # plus an unguarded door, and the unguarded one was the main entrance.
    #
    # So the check is at the single write point rather than repeated per caller. Any future caller
    # inherits it without knowing it exists, which is the only version of this that stays true.
    try:
        import steering_load as _sl
        _hr, _tot, _ceil = _sl.headroom(REPO)
        if _ceil is None:
            return {"action": "directive_skipped",
                    "reason": "no steering-budget baseline on this seat — the ceiling is unknown, "
                              "not absent; run the budget test to record one"}
        if _hr < 80:                                   # a directive line costs ~30-60 tok
            return {"action": "directive_skipped",
                    "reason": f"steering budget has {_hr} tok headroom ({_tot} against ceiling "
                              f"{_ceil}) — compress a steering file, or raise the ceiling "
                              f"deliberately; not silently by writing into a breach"}
    except Exception as _e:
        # UnstableMeasurement included: unknown means stop, same as an absent baseline.
        return {"action": "directive_skipped",
                "reason": f"steering budget unverifiable — failing closed ({str(_e)[:90]})"}

    safe = _sanitize_directive(case.get("user_wanted", ""))
    if len(safe) < 8:
        return {"action": "directive_skipped", "reason": "ask too thin to encode"}
    support = case.get("support", {}).get("count", 0)
    line = f"- {safe} (recurring {support}x) — apply by default."
    try:
        # target must be a real, regular, non-symlink file inside the Core repo
        if CLAUDE_MD.is_symlink() or not CLAUDE_MD.is_file():
            return {"action": "directive_error", "reason": "CLAUDE.md missing/symlink — refusing"}
        txt = CLAUDE_MD.read_text()
        if safe.lower() in txt.lower():
            return {"action": "directive_skipped", "reason": "already present"}
        if _AUTO_START in txt and _AUTO_END in txt:
            txt = txt.replace(_AUTO_END, f"{line}\n{_AUTO_END}", 1)  # insert before END (both markers present)
        elif _AUTO_START not in txt and _AUTO_END not in txt:
            txt = txt.rstrip() + f"\n\n## Auto-generated standing directives\n{_AUTO_START}\n{line}\n{_AUTO_END}\n"
        else:
            return {"action": "directive_error", "reason": "exactly one marker present — refusing (corrupt section)"}
        tmp = CLAUDE_MD.with_name(f".CLAUDE.md.tmp.{_os.getpid()}")  # unique tmp — no concurrent-write race
        tmp.write_text(txt)
        _os.replace(tmp, CLAUDE_MD)
        return {"action": "directive_applied", "line": line}
    except Exception as e:
        return {"action": "directive_error", "reason": str(e)[:150]}


def _render_procedure_body(case: dict, spec: dict, work_shape: bool) -> str:
    """A procedure payload rendered from the ask's own evidence. Deterministic — no LLM.

    WHY THIS EXISTS (2026-08-20). `_gen_procedure` returned `hooked_skill_pending` with the reason
    "payload not authored yet — close directive will draft it", and NOTHING DRAFTED IT. No caller in
    `core-si-close.py`, none in the close command, none anywhere. So every skill the loop ever routed
    would have stopped at `pending` forever — the fifth break of the same shape found today, a
    comment describing a step that was never built.

    The original design deferred authoring to an LLM step precisely because this path is
    deterministic and LLMs are not called from it. That constraint is right and this keeps it:
    `_render_workflow_body` already renders a workflow body deterministically from mined evidence,
    so this is the same move one layer over, not a new idea.

    WHAT IT DELIBERATELY DOES NOT CONTAIN. No verbatim correction text. `write_procedure`'s own
    docstring is explicit that `fj.redact` masks known secret SHAPES and is NOT a declassification
    boundary — prose passwords, connection strings and PII pass straight through it. The real control
    is upstream: a payload is a PROCEDURE, not a transcript. So the evidence appears here as COUNTS
    and DATES only. Nick's words stay in the corpus where they are org-scoped.
    """
    ask = str(case.get("user_wanted", "")).strip()
    sup = case.get("support", {}) or {}
    n = sup.get("count", 0)
    when = ("You are about to change a file (Edit / Write / MultiEdit / NotebookEdit)."
            if work_shape else
            "The incoming prompt matched this procedure's trigger.")
    lines = [
        f"# {ask[:120]}",
        "",
        f"Mined from **{n} distinct moments** in this Core's own correction corpus. Installed by the",
        "self-improvement loop; reversible by retiring the artifact that points here.",
        "",
        "## When this fires",
        "",
        when,
        "",
        "## Do",
        "",
        f"- {ask}",
        "- Satisfy it BEFORE the action, not in a footnote afterwards.",
        "- If you cannot satisfy it, say so plainly in the reply rather than proceeding quietly.",
        "",
        "## Why it exists",
        "",
        f"This became a procedure rather than a reminder because the same correction recurred {n}",
        "times. A reminder that has been repeated that often is evidence the reminder did not hold.",
        "",
        "## Provenance",
        "",
        f"- artifact: `{spec.get('artifact_id','')}`",
        f"- event: `{spec.get('event','')}`",
        f"- support: {n} distinct (correction, session) moments",
    ]
    return "\n".join(lines) + "\n"


def _gen_procedure(org: int, case: dict, work_shape: bool = False) -> dict:
    """Build a `procedure` spec: the SAME gated trigger a contract gets, plus a payload pointer.

    The payload file IS the draft cache — deliberately, so there is no second store to keep in sync.
    Authoring is an LLM step and LLMs are not called from this deterministic path, so the flow mirrors
    the one ask_miner already uses for canonical_ask: this function reports the draft as PENDING, the
    close directive dispatches a subagent to write it via friction_installer.write_procedure(), and
    the next pass finds the file and installs. Nothing installs until the payload exists, is redacted,
    is size-bounded, and its hash re-verifies against disk (friction_installer._validate_payload).
    """
    import friction_installer as fi
    r = ask_miner.route_ask_case(org, case)
    if not r:
        # NO PROMPT TRIGGER IS NOT A DEAD END FOR A SKILL (2026-08-20). `route_ask_case` builds a
        # PROMPT-keyed spec and returns None when the ask cannot ground one — and this bailed there,
        # which meant a work-shaped procedure never reached the work_shape branch twenty lines below
        # that exists precisely to key on the TOOL instead of on words. A prerequisite for one shape
        # was gating both.
        #
        # It is the sixth break of the same kind found today and the one that actually terminated the
        # skill path: after the upstream gate was moved below the router, four asks routed to
        # hooked_skill on life and ALL FOUR died here with "no usable trigger" —
        #   11x keep the architecture docs in sync · 6x autonomously encode recurring workflows
        #    4x execute the plan fully end-to-end   · 4x clarify which tools are in use
        #
        # So: fall through work-shaped. The condition below is the same closed mutating-tool
        # vocabulary, which is what makes it specific without a prompt corpus to measure against.
        # This does NOT widen anything — a triggerless ask previously produced nothing at all.
        work_shape = True
        aid0 = "art_hs" + hashlib.sha256(
            f"skill|{org}|{case.get('user_wanted','')}".encode()).hexdigest()[:16]
        # The envelope matches ask_miner's exactly (:628-634) — template / scope / lease /
        # generator_version are REQUIRED by _validate_spec, and the first version of this omitted
        # all four. The installer refused every skill with
        # "spec missing ['generator_version','lease','scope','template']", which is the validator
        # doing its job on a spec I synthesized by hand instead of copying the one shape that works.
        spec = {"spec_version": 1, "artifact_id": aid0, "case_id": case.get("case_id"),
                "org_id": org, "type": "hooked_skill", "event": "PreToolUse",
                "condition": {"all": [{"op": "event_is", "value": "PreToolUse"}]},
                "effect": {"mode": "inject", "message": "", "skill_id": None},
                "tests": {"positive_ids": [], "negative_ids": []},
                "template": {"id": "ask-procedure-v1", "sha256": "pending"}, "scope": "org_local",
                "lease": {"max_fires_per_session": 2, "expires_at": None},
                "generator_version": "artifact-generator/work-shaped-skill-1"}
        ex = {"positive": [], "negative": []}
    else:
        spec, ex = r
    aid = spec["artifact_id"]
    p = fi._procedure_path(aid)
    if not p.is_file():
        # AUTHOR IT NOW, DETERMINISTICALLY (2026-08-20). This used to return `pending` and wait for
        # a close-time drafting step that does not exist anywhere in the tree — so the skill terminal
        # could route but never complete. `pending` survives below as the honest fallback for a body
        # the installer refuses (it lints for guard-surface names and bounds size), because a
        # refused payload IS a case for a human, while an unwritten one was just a dead end.
        try:
            fi.write_procedure(aid, _render_procedure_body(case, spec, work_shape))
        except Exception as e:
            return {"action": "hooked_skill_pending", "type": "hooked_skill", "artifact_id": aid,
                    "ask": case.get("user_wanted", "")[:300],
                    "reason": f"payload refused by the installer, needs a human: {e}"}
        if not p.is_file():
            return {"action": "hooked_skill_pending", "type": "hooked_skill", "artifact_id": aid,
                    "ask": case.get("user_wanted", "")[:300],
                    "reason": "payload write reported success but the file is absent"}
    try:
        raw = p.read_bytes()
    except Exception as e:
        return {"action": "skip", "type": "hooked_skill", "reason": f"payload unreadable: {e}"}
    spec = {**spec, "type": "hooked_skill",
            "payload": {"path": p.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}
    if work_shape:
        # A procedure about a moment of WORK fires when the work is about to happen, not when the
        # prompt happens to contain a word. The condition keys on the tool instead of the prompt —
        # a closed vocabulary of mutating tools, which is what makes it specific without a prompt
        # corpus to measure against (see friction_installer: the corpus check is skipped for these
        # precisely because tool fields do not exist in the prompt corpus).
        spec["event"] = "PreToolUse"
        spec["condition"] = {"all": [
            {"op": "event_is", "value": "PreToolUse"},
            {"op": "tool_name_in", "value": ["Edit", "Write", "MultiEdit", "NotebookEdit"]},
            {"op": "tool_mutability_is", "value": "mutating"}]}
        import friction_router as _fr
        ex = {"positive": [{"id": "p1", "event": "PreToolUse", "expected": "fire",
                            "provenance": "real_positive",
                            "hook_input": _fr._hook_input("PreToolUse", tool="Edit")}],
              "negative": [
                  {"id": "n_evt", "event": "UserPromptSubmit", "expected": "no_fire",
                   "provenance": "event_mismatch",
                   "hook_input": _fr._hook_input("UserPromptSubmit", prompt=case.get("user_wanted", ""))},
                  {"id": "n_tool", "event": "PreToolUse", "expected": "no_fire",
                   "provenance": "polarity_mutation",
                   "hook_input": _fr._hook_input("PreToolUse", tool="Read")}]}
        spec["tests"] = {"positive_ids": ["p1"], "negative_ids": ["n_evt", "n_tool"]}
    # The injected message is a POINTER, never the payload body: the body stays out of every prompt
    # until its gated trigger actually fires, and the file is the single copy that rollback retires.
    spec["effect"] = {**spec["effect"],
                      "message": (f"Recurring ask ({case['support']['count']}x): "
                                  f"{case.get('user_wanted','')}. Follow the procedure at "
                                  f".claude/state/friction-artifacts/procedures/{p.name}")[:2000]}
    return {"action": "install_hooked_skill", "spec": spec, "examples": ex}


def _gen_work_hook(org: int, case: dict, route: dict) -> dict:
    """The frustration terminal — which is NOT a new artifact type, and the installer taught me why.

    I first built this as its own PreToolUse `contract` spec. `_validate_spec` refused it:

        event must be one of ['UserPromptSubmit'] for type=contract

    That fence is deliberate and its reason is written at friction_installer.py:493 — a contract's
    specificity is proven against a corpus of PROMPTS, and a tool-shaped condition has no grounding
    there, so only `hooked_skill` may fire on PreToolUse. Correct, and it means the separate type I
    invented duplicated something the system already models: a work-moment artifact IS a hooked_skill
    with a work-shaped condition. The frustration share selects that FORM; it does not need a form of
    its own.

    So this delegates, and the ~40 lines of hand-written spec that used to live here are gone. That
    is Nick's standing directive applied to my own hour-old code: when a change would create a second
    parallel mechanism, unify instead. It also means a work hook inherits the payload requirement,
    the specificity fence and the rollback path for free, rather than needing each re-argued.
    """
    return _gen_procedure(org, case, work_shape=True)


# --- slash_command (2026-08-31) ------------------------------------------------------------------
#
# Words too generic to name a command. Overlaps _AMBIGUOUS_NAMES in artifact_typer (same failure
# mode: "the ask happens to contain an ordinary word" is not evidence of anything), kept as its own
# list here because this one filters CANDIDATE SLUG WORDS, not a whole-ask phrase match.
_SLUG_STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with", "by", "is",
              "are", "be", "do", "dont", "not", "that", "this", "it", "its", "you", "your", "from",
              "then", "them", "they", "make", "keep", "get", "run", "all", "any", "still", "just"}


def _slugify_ask(ask: str, existing: set[str]) -> str | None:
    """Deterministic kebab-case command name from the distilled ask. None if nothing distinctive
    survives or the name collides with an existing command file.

    route_type already ran _duplicates_existing on the ASK TEXT before this terminal is ever
    reached (artifact_typer.py) — that check protects an EXISTING capability from being duplicated.
    This is the mirror check on the NEW name we are about to mint, which the earlier check never
    saw because it does not exist until this function derives it."""
    words = [w for w in re.findall(r"[a-z0-9]+", (ask or "").lower())
             if w not in _SLUG_STOP and len(w) > 2]
    if len(words) < 2:
        return None
    slug = "-".join(words[:4])[:48].strip("-")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,48}", slug):
        return None
    if slug in existing:
        return None
    return slug


def _existing_command_slugs() -> set[str]:
    try:
        return {f.stem for f in (REPO / ".claude" / "commands").glob("*.md")}
    except Exception:
        return set()


def _render_command_body(case: dict, slug: str) -> str:
    """A `.claude/commands/<slug>.md` body rendered from the ask's own evidence — deterministic,
    same discipline as _render_procedure_body: counts and dates, never a verbatim quote of what
    Nick typed (fj.redact is not a declassification boundary; see friction_installer.write_procedure).
    Frontmatter format matched against the real files in .claude/commands/ (description [+
    argument-hint], then a `# /name` heading) rather than invented."""
    ask = str(case.get("user_wanted", "")).strip()
    n = (case.get("support") or {}).get("count", 0)
    desc = _sanitize_directive(f"Recurring ask, mined by the self-improvement loop ({n}x): {ask}")
    lines = [
        "---",
        f"description: {desc}",
        "---",
        "",
        f"# /{slug}",
        "",
        f"Mined from **{n} distinct moments** in this Core's own correction corpus. Installed by",
        "the self-improvement loop as a command the operator runs BY NAME — unlike a hooked_skill",
        "procedure, it does not fire on its own.",
        "",
        "## Do",
        "",
        f"- {ask}",
        "- Work through it end to end in this turn; say plainly if any part cannot be satisfied.",
        "",
        "## Provenance",
        "",
        f"- support: {n} distinct (correction, session) moments",
        "- reversible: retiring this artifact (friction_installer.rollback) removes this file",
    ]
    return "\n".join(lines) + "\n"


def _gen_slash_command(org: int, case: dict, route: dict) -> dict:
    """Build a `slash_command` spec: a bookkeeping trigger plus a REAL `.claude/commands/<slug>.md`
    payload. Nick invokes it BY NAME — the trigger fires on the literal `/<slug>` invocation text,
    which exists so the artifact rides the SAME test-gate/install/quarantine/rollback rails as
    every other artifact type, not because anything needs to fire automatically. Because nobody has
    ever typed a brand-new slug before, the trigger clears the corpus-specificity bar for free: its
    real-prompt fire rate is 0%."""
    import friction_installer as fi
    import friction_router as fr
    ask = str(case.get("user_wanted", "")).strip()
    slug = _slugify_ask(ask, _existing_command_slugs())
    if not slug:
        return {"action": "skip", "type": "slash_command",
                "reason": "could not derive a distinctive, collision-free command name from the ask"}
    aid = _aid(case.get("case_id") or ask, "cmd")
    body = _render_command_body(case, slug)
    try:
        payload = fi.write_command_file(slug, body)
    except Exception as e:
        return {"action": "skip", "type": "slash_command",
                "reason": f"command payload refused by the installer: {e}"}
    trig = r"^/" + re.escape(slug) + r"\b"
    cond = {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                    {"op": "prompt_regex", "value": trig}]}
    # NEIGHBORS, not members (2026-08-31) — same distinction ask_miner._neighbor_prompts makes for
    # a contract: a member of THIS ask's own cluster is an instance of the ask, so labelling it
    # no_fire would be testing the wrong thing even though it happens to pass here (nobody has
    # typed a brand-new /<slug> before, so any real text — member or not — clears it). Drawing
    # from OUTSIDE the cluster is the semantically correct negative and, as a side effect, does not
    # depend on this specific ask having tracked member_ids.
    neighbors = ask_miner._neighbor_prompts(org, (case.get("support") or {}).get("member_ids") or [])
    neg = [{"id": "n_evt", "event": "Stop", "expected": "no_fire", "provenance": "event_mismatch",
            "hook_input": fr._hook_input("Stop", assistant=ask)},
           {"id": "n_pol", "event": "UserPromptSubmit", "expected": "no_fire",
            "provenance": "polarity_mutation",
            "hook_input": fr._hook_input("UserPromptSubmit", prompt="a completely unrelated topic entirely")}]
    for i, m in enumerate(neighbors[:3]):
        neg.append({"id": f"n_nb{i}", "event": "UserPromptSubmit", "expected": "no_fire",
                    "provenance": "real_neighbor", "hook_input": fr._hook_input("UserPromptSubmit", prompt=m[:300])})
    if not any(x["provenance"] == "real_neighbor" for x in neg):
        return {"action": "skip", "type": "slash_command",
                "reason": "no real corpus neighbor to prove specificity"}
    pos = [{"id": "p1", "event": "UserPromptSubmit", "expected": "fire", "provenance": "real_positive",
            "hook_input": fr._hook_input("UserPromptSubmit", prompt=f"/{slug}")}]
    n = (case.get("support") or {}).get("count", 0)
    spec = {
        "spec_version": 1, "artifact_id": aid, "case_id": case.get("case_id"), "org_id": org,
        "type": "slash_command", "event": "UserPromptSubmit", "condition": cond,
        "effect": {"mode": "inject",
                   "message": (f"Recurring ask ({n}x): {ask}. Available on demand as /{slug} — "
                               f".claude/commands/{slug}.md")[:2000],
                   "skill_id": None},
        "tests": {"positive_ids": ["p1"], "negative_ids": [x["id"] for x in neg]},
        "template": {"id": "ask-command-v1", "sha256": "pending"}, "scope": "org_local",
        "lease": {"max_fires_per_session": 2, "expires_at": None},
        "generator_version": "artifact-generator/slash-command-1",
        "payload": payload,
    }
    return {"action": "install_slash_command", "spec": spec, "examples": {"positive": pos, "negative": neg}}


# --- workflow (2026-08-31) — the honest subset ----------------------------------------------------
#
# DECIDED AFTER READING friction_dispatch.py and _validate_spec, not assumed: v1's effect DSL is
# INJECT-ONLY (`_validate_spec`: "v1 is INJECT-ONLY, got mode=..."), there is no "run a tool" effect
# mode anywhere in the schema, and test_static_no_codegen locks every module in this pipeline
# against eval/exec/child-process spawning/__import__ on artifact data. So "auto-generate AND auto-run a
# multi-agent Workflow script" is not a feature this loop happens to be missing — it is a capability
# the schema is structurally incapable of expressing, on purpose, since v1 (2026-07-20 governing
# plan: "ambiguous always falls to a non-blocking inject_contract, never a blocker").
#
# What CAN be built honestly, and is built here: a PROPOSAL. Same payload-pointer shape
# `_gen_procedure` already uses for hooked_skill — a gated trigger plus a markdown payload — so this
# rides the same test gate, install, quarantine and rollback rails rather than a parallel mechanism.
# The payload is a brief Nick reviews and runs himself with the Workflow tool; the artifact's own
# effect can never do that, by construction, not by promise.
def _render_workflow_proposal_body(case: dict, aid: str) -> str:
    ask = str(case.get("user_wanted", "")).strip()
    n = (case.get("support") or {}).get("count", 0)
    lines = [
        f"# Workflow proposal — {ask[:100]}",
        "",
        "**PROPOSAL ONLY. Nothing here runs by itself.** A generated workflow is a bigger claim",
        "than a generated command or hook — orchestrating multiple agents against a live Core is",
        "not something the self-improvement loop auto-approves. Review this, then invoke the",
        "Workflow tool yourself if you want it built or run.",
        "",
        "## The recurring ask",
        "",
        f"- {ask}",
        "",
        "## Why this became a workflow proposal, not a hooked_skill",
        "",
        f"The ask names multiple coordinating agents/steps rather than a single trigger-and-remind",
        f"action, mined from {n} distinct moments in this Core's own correction corpus.",
        "",
        "## Suggested shape (fill in before running)",
        "",
        "1. Decide the concrete agent roles this needs (research / execution / review).",
        "2. Draft the Workflow script per the `workflow-authoring` skill.",
        "3. Dry-run it and read the output before trusting it against real state.",
        "",
        "## Provenance",
        "",
        f"- artifact: `{aid}`",
        f"- support: {n} distinct (correction, session) moments",
        "- reversible: retiring this artifact removes this proposal file",
    ]
    return "\n".join(lines) + "\n"


def _gen_workflow_proposal(org: int, case: dict, route: dict) -> dict:
    """A `workflow` route becomes a PROPOSAL — see the module comment above for why that is the
    honest subset rather than a compromise. Trigger derivation mirrors `_gen_procedure` exactly:
    try to ground a real prompt trigger first, fall back to the same closed mutating-tool
    work-shape vocabulary when the ask cannot ground one, because a multi-step ask is exactly the
    shape `_gen_procedure`'s fallback already exists for."""
    import friction_installer as fi
    import friction_router as fr
    ask = str(case.get("user_wanted", "")).strip()
    aid = _aid(case.get("case_id") or ask, "wfp")
    body = _render_workflow_proposal_body(case, aid)
    try:
        payload = fi.write_workflow_proposal(aid, body)
    except Exception as e:
        return {"action": "skip", "type": "workflow", "reason": f"proposal payload refused: {e}"}
    n = (case.get("support") or {}).get("count", 0)
    msg = (f"Recurring ask ({n}x): {ask}. A workflow-tool PROPOSAL exists at "
           f".claude/state/friction-artifacts/workflow-proposals/{aid}.workflow.md — review it and "
           f"run it yourself with the Workflow tool; it never runs on its own.")[:2000]
    r = ask_miner.route_ask_case(org, case)
    if r:
        _rspec, ex = r
        event, cond = _rspec["event"], _rspec["condition"]
    else:
        # No prompt trigger groundable — the SAME work-shape fallback hooked_skill uses (same
        # closed mutating-tool vocabulary), because friction_installer skips the corpus-specificity
        # check for exactly this shape (its fields do not exist in the prompt corpus).
        event = "PreToolUse"
        cond = {"all": [{"op": "event_is", "value": "PreToolUse"},
                        {"op": "tool_name_in", "value": ["Edit", "Write", "MultiEdit", "NotebookEdit"]},
                        {"op": "tool_mutability_is", "value": "mutating"}]}
        ex = {"positive": [{"id": "p1", "event": "PreToolUse", "expected": "fire",
                            "provenance": "real_positive",
                            "hook_input": fr._hook_input("PreToolUse", tool="Edit")}],
              "negative": [
                  {"id": "n_evt", "event": "UserPromptSubmit", "expected": "no_fire",
                   "provenance": "event_mismatch",
                   "hook_input": fr._hook_input("UserPromptSubmit", prompt=ask)},
                  {"id": "n_tool", "event": "PreToolUse", "expected": "no_fire",
                   "provenance": "polarity_mutation",
                   "hook_input": fr._hook_input("PreToolUse", tool="Read")}]}
    spec = {
        "spec_version": 1, "artifact_id": aid, "case_id": case.get("case_id"), "org_id": org,
        "type": "workflow_proposal", "event": event, "condition": cond,
        "effect": {"mode": "inject", "message": msg, "skill_id": None},
        "tests": {"positive_ids": [p["id"] for p in ex["positive"]],
                  "negative_ids": [x["id"] for x in ex["negative"]]},
        "template": {"id": "ask-workflow-proposal-v1", "sha256": "pending"}, "scope": "org_local",
        "lease": {"max_fires_per_session": 2, "expires_at": None},
        "generator_version": "artifact-generator/workflow-proposal-1",
        "payload": payload,
    }
    return {"action": "install_workflow_proposal", "spec": spec, "examples": ex}



# --- workflow_run (2026-08-31, Gap B — judge-selected Candidate 1) --------------------------------
#
# THE HONEST UPGRADE FROM workflow_proposal, NOT A REPLACEMENT. A `workflow` route (see
# _gen_workflow_proposal above) still produces a proposal-only markdown brief for any multi-agent
# ask this loop has no pre-authored script for — that path is UNCHANGED and stays the fallback.
# `workflow_run` exists ONLY for the closed set of asks that match a CATALOG entry
# (workflow_catalog.match, wired into artifact_typer.route_type): a pre-authored, code-reviewed,
# hash-pinned Workflow script a human already wrote and vetted — the same discipline
# ORACLE_CATALOG uses to gate enforcement_block.
#
# WHAT MAKES THIS A REAL TERMINAL RATHER THAN ANOTHER PROPOSAL: the generated artifact IS a real
# `.claude/commands/<slug>.md` file — the exact same invocation model _gen_slash_command uses, and
# the one artifact_typer.py cites as its own justification ("/slug activates ONLY when Nick types
# it himself, which IS the invocation"). Nick still has to type /slug himself — nothing here fires
# unattended — but when he does, the command's own body names the EXACT catalog script, its pinned
# hash, and the closed, catalog-default params to run it with. There is no draft-a-script step
# left for the agent to get wrong at runtime, because nothing is drafted at runtime.
#
# WHAT NEVER RIDES ON MINED TEXT (the judge's most-worried failure mode): agent_cap and
# model_tiers come ONLY from the catalog entry and are re-verified byte-for-byte against
# workflow_catalog.EXPECTED_SCRIPT_HASHES again at install
# (friction_installer._validate_workflow_script_payload) — never trusted from this function's own
# say-so twice in a row. The only per-ask-shaped value this v1 catalog entry even declares is
# `glob`, and it stays pinned to the catalog's own default here (see params below) rather than
# derived from the ask — the deliberately conservative choice for a first entry, not a limitation
# of the mechanism: a future entry can accept a derived glob once that derivation has earned trust
# on its own, validated the same way (workflow_catalog.valid_glob) before it ever reaches a
# rendered body.
def _render_workflow_run_body(case: dict, slug: str, aid: str, cid: str, entry: dict, params: dict) -> str:
    ask = str(case.get("user_wanted", "")).strip()
    n = (case.get("support") or {}).get("count", 0)
    cap = entry["agent_cap"]
    tiers = entry["model_tiers"]
    tier_line = ", ".join(f"{k}: {v}" for k, v in sorted(tiers.items()))
    # STATED HERE, NOT COMPUTED AT RUN TIME (judge requirement #6). subagents.md's warn-before-
    # spend rule needs an explicit go BEFORE the spend, not a runtime narration of it — so the cost
    # goes in the text Nick reads before he ever types /slug. It is a static function of agent_cap
    # + tiers (both catalog-pinned, never ask-derived), not a live measurement.
    desc = _sanitize_directive(
        f"Recurring ask ({n}x): {ask}. Runs the catalog workflow {cid} — {cap} agents ({tier_line}).")
    lines = [
        "---",
        f"description: {desc}",
        "---",
        "",
        f"# /{slug}",
        "",
        f"Mined from **{n} distinct moments** in this Core's own correction corpus. Runs a REAL,",
        "pre-authored, hash-pinned Workflow script from the catalog — this command does not draft",
        "a workflow from your words; it points at one already reviewed.",
        "",
        f"**Cost before you run this: {cap} agents, tiers — {tier_line}.** Typing this command IS",
        "the explicit go for that spend (subagents.md's warn-before-spend rule) — nothing here",
        "fires without you typing it, and nothing fires a second time on its own after that.",
        "",
        "## Do, in order",
        "",
        "1. Append one line to `.claude/state/friction-artifacts/workflow-run-receipts.jsonl` —",
        "   run `date +%s` for the timestamp (never guess it) and write:",
        f'   `{{"artifact_id": "{aid}", "catalog_id": "{cid}", "ts": <that number>}}`',
        "   This is the ONLY usage signal retirement checks for this artifact — skipping it makes",
        "   this command look unused forever, even after you run it.",
        "2. Invoke the Workflow tool with the script at",
        f"   `scheduling/claude-si/workflow-catalog/{entry['script']}` (sha256 starts",
        f"   `{entry['sha256'][:16]}…` — re-read the file and confirm the hash yourself if you",
        "   have any doubt it drifted) and args:",
        f"   `{json.dumps(params, ensure_ascii=True, sort_keys=True)}`",
        "3. Read the result and report it plainly — this workflow is READ-ONLY review; it never",
        "   pushes, sends, or edits anything on its own (its own prompts say so, and the same",
        "   PreToolUse guard chain that gates this session gates its subagents too — verified, not",
        "   assumed; see workflow_catalog.py's module docstring for the evidence).",
        "",
        "## Provenance",
        "",
        f"- support: {n} distinct (correction, session) moments",
        f"- catalog: `{cid}` (`scheduling/claude-si/workflow-catalog/manifest.json`)",
        "- reversible: retiring this artifact (friction_installer.rollback) removes this file",
    ]
    return "\n".join(lines) + "\n"


def _gen_workflow_run(org: int, case: dict, route: dict) -> dict:
    """Build a `workflow_run` spec — see the module comment above for the full design. Falls back
    to the honest `workflow` proposal on ANY failure to verify the catalog (tampered manifest,
    missing script, unknown catalog_id) — a routing decision made this ask eligible, but nothing
    downstream is allowed to treat an unverifiable catalog as a green light."""
    import friction_installer as fi
    import friction_jsonl as fj
    import friction_router as fr
    import workflow_catalog as wc
    cid = route.get("catalog_id")
    try:
        catalog = wc.load_catalog()
    except Exception:
        return _gen_workflow_proposal(org, case, route)
    entry = catalog.get(cid) if cid else None
    if not entry:
        return _gen_workflow_proposal(org, case, route)
    ask = str(case.get("user_wanted", "")).strip()
    slug = _slugify_ask(ask, _existing_command_slugs())
    if not slug:
        return {"action": "skip", "type": "workflow_run",
                "reason": "could not derive a distinctive, collision-free command name from the ask"}
    aid = _aid(case.get("case_id") or ask, "wfr")
    # PARAMS — closed schema, catalog default for everything (see the module comment for why v1
    # stays maximally conservative rather than deriving `glob` from mined text).
    params = dict(entry.get("params_default") or {})
    body = _render_workflow_run_body(case, slug, aid, cid, entry, params)
    # JUDGE REQUIREMENT #2: _hardened_write pushes the body through fj.redact before it ever
    # touches disk, and a body containing anything shaped like a secret would come back MUTATED —
    # silently disagreeing with the pre-redact string this function already committed to. Checked
    # BEFORE writing: refuse rather than install a body two different readers would see two
    # different ways. (This body is built to avoid tripping the check at all — the script hash is
    # truncated to 16 hex chars, well under the 40-char secret-shape floor in both fj._SECRET_RX
    # and friction_dispatch._SECRET_RX — this assertion is the proof that held, not a hope.)
    redacted = fj.redact(body)
    if redacted != body:
        return {"action": "skip", "type": "workflow_run",
                "reason": "generated command body was mutated by secret redaction — refusing "
                          "rather than installing a body the install-time validator would "
                          "disagree with"}
    try:
        payload = fi.write_command_file(slug, body)
    except Exception as e:
        return {"action": "skip", "type": "workflow_run",
                "reason": f"command payload refused by the installer: {e}"}
    # RE-RENDER BYTE-COMPARE (judge requirement #2): prove the file the installer just wrote is
    # BYTE-IDENTICAL to the body this function rendered — compared against POST-redact bytes
    # (`redacted`, a no-op here since `redacted == body` was just checked), never the pre-redact
    # string, or an install that legitimately redacted something would permanently disagree with
    # every future re-render. Also catches _hardened_write silently truncating for
    # MAX_COMMAND_BYTES: a body cut for length must not be treated as if it said what was asked.
    on_disk = (fi.COMMANDS_DIR / f"{slug}.md").read_bytes().decode("utf-8", "ignore")
    if on_disk != redacted:
        return {"action": "skip", "type": "workflow_run",
                "reason": "on-disk command body does not byte-match its deterministic render "
                          "(likely truncated for size) — refusing"}
    # WFSCRIPTDIR run-manifest: the artifact's OWN copy of exactly what it is bound to, re-verified
    # at install by friction_installer._validate_workflow_script_payload — never trusted from the
    # spec alone, the same discipline every other payload type uses. ensure_ascii=True per judge
    # requirement #3: U+2028/2029-safe, so a mined `glob` value (none here — pure catalog defaults
    # — but the same call site a future entry's derived glob would use) can never smuggle a raw
    # line/paragraph separator into what reads as valid JSON but is not valid inside a bare JS
    # string literal.
    run_manifest = {"catalog_id": cid, "script_sha256": entry["sha256"], "agent_cap": entry["agent_cap"],
                    "model_tiers": entry["model_tiers"], "params": params}
    try:
        rm_payload = fi.write_workflow_run_manifest(
            aid, json.dumps(run_manifest, ensure_ascii=True, sort_keys=True))
    except Exception as e:
        return {"action": "skip", "type": "workflow_run",
                "reason": f"run-manifest payload refused by the installer: {e}"}
    trig = r"^/" + re.escape(slug) + r"\b"
    cond = {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                    {"op": "prompt_regex", "value": trig}]}
    # NEIGHBORS, not members — same discipline _gen_slash_command uses: nobody has ever typed a
    # brand-new /<slug> before, so the trigger's real-prompt fire rate is 0% regardless, and a
    # corpus NEIGHBOR (outside this ask's own cluster) is the semantically correct negative.
    neighbors = ask_miner._neighbor_prompts(org, (case.get("support") or {}).get("member_ids") or [])
    neg = [{"id": "n_evt", "event": "Stop", "expected": "no_fire", "provenance": "event_mismatch",
            "hook_input": fr._hook_input("Stop", assistant=ask)},
           {"id": "n_pol", "event": "UserPromptSubmit", "expected": "no_fire",
            "provenance": "polarity_mutation",
            "hook_input": fr._hook_input("UserPromptSubmit", prompt="a completely unrelated topic entirely")}]
    for i, m in enumerate(neighbors[:3]):
        neg.append({"id": f"n_nb{i}", "event": "UserPromptSubmit", "expected": "no_fire",
                    "provenance": "real_neighbor", "hook_input": fr._hook_input("UserPromptSubmit", prompt=m[:300])})
    if not any(x["provenance"] == "real_neighbor" for x in neg):
        return {"action": "skip", "type": "workflow_run",
                "reason": "no real corpus neighbor to prove specificity"}
    pos = [{"id": "p1", "event": "UserPromptSubmit", "expected": "fire", "provenance": "real_positive",
            "hook_input": fr._hook_input("UserPromptSubmit", prompt=f"/{slug}")}]
    n = (case.get("support") or {}).get("count", 0)
    spec = {
        "spec_version": 1, "artifact_id": aid, "case_id": case.get("case_id"), "org_id": org,
        "type": "workflow_run", "event": "UserPromptSubmit", "condition": cond,
        "effect": {"mode": "inject",
                   "message": (f"Recurring ask ({n}x): {ask}. Runs the catalog workflow '{cid}' as "
                               f"/{slug} — .claude/commands/{slug}.md")[:2000],
                   "skill_id": None},
        "tests": {"positive_ids": ["p1"], "negative_ids": [x["id"] for x in neg]},
        "template": {"id": f"workflow-catalog-{cid}", "sha256": entry["sha256"]}, "scope": "org_local",
        "lease": {"max_fires_per_session": 2, "expires_at": None},
        "generator_version": "artifact-generator/workflow-run-1",
        "payload": payload,
        "workflow_ref": {"catalog_id": cid, "run_manifest": rm_payload},
    }
    return {"action": "install_workflow_run", "spec": spec, "examples": {"positive": pos, "negative": neg}}


def generate(org: int, case: dict, route: dict) -> dict:
    t = route["type"]
    if t == "already_covered":
        return {"action": "skip", "type": t, "reason": route.get("reason")}
    if t == "enforcement_block":
        spec, ex = _gen_block(org, case, route)
        return {"action": "install_shadow_block", "spec": spec, "examples": ex}
    if t == "claude_md_directive":
        return {"action": "directive", "result": auto_apply_directive(org, case)}
    if t == "work_hook":
        return _gen_work_hook(org, case, route)
    if t == "hooked_skill":
        return _gen_procedure(org, case, work_shape=bool(route.get("work_shape")))
    if t == "slash_command":
        return _gen_slash_command(org, case, route)
    if t == "workflow":
        return _gen_workflow_proposal(org, case, route)
    if t == "workflow_run":
        return _gen_workflow_run(org, case, route)
    # skill + inject_contract → an autonomous, test-gated reminder installs now. (Authoring a NEW rich
    # SKILL.md is the agentic-runtime frontier — an LLM step, not a human gate; the reminder to USE the
    # capability is autonomous today.)
    r = ask_miner.route_ask_case(org, case)
    if not r:
        return {"action": "skip", "type": t, "reason": "no usable trigger"}
    spec, ex = r
    return {"action": "install_contract", "spec": spec, "examples": ex}
