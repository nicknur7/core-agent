#!/usr/bin/env python3
"""Anything written into si_artifacts must survive the validator that guards the front door.

WHY
---
core-business, finding 8. `si_project.upsert` is a real trust boundary — it forces inject-only,
refuses to persist an enforced artifact, rejects unknown effect modes, and requires an explicit
allow_block that only the hash-pinned template path passes. Those are the high-stakes bypasses
and they are closed.

What it does NOT do is any schema validation. That lives one layer up, in
`friction_installer._validate_spec`, in the caller that used to be the only caller. So every
writer added afterwards inherits the mode guards and not the schema ones:

  · friction_tune wrote a top-level `_tune` key for narrowing history
  · friction_installer.install() wrote `positive_texts`/`negative_texts` into spec["tests"]
    AFTER _validate_spec had already run at install:493

Both are outside closed key sets (`spec` has 13 allowed keys; `spec["tests"]` allows exactly
{positive_ids, negative_ids}). Neither is a safety hole — upsert still forces inject-only and
the dispatcher does not enforce the key set, so such a spec simply runs. What breaks is the
invariant that si_artifacts contains only gate-passed, validated specs, and it breaks SILENTLY:
the artifact becomes un-reinstallable through the normal path, so any later re-gate, migration,
audit or re-derive sees an invalid spec.

Measured when found: 0 of 30 live artifacts carried either key, because neither writer had
fired yet. Both were latent and would have armed on the next install or the first narrowing —
the same shape as findings 3 and 7a. Latency is why this is a test and not a comment.

The fix was NOT to widen the allowed key sets to accommodate bookkeeping. The schema being
narrow is what makes it worth having. Evidence and history moved OUT of the spec, into the
evidence store, mirroring the split already made for hand-written gates: purpose is shared and
structured, evidence is local.

    python3 bin/tests/test_spec_schema_integrity.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

import friction_installer as inst   # noqa: E402
import friction_tune as ft          # noqa: E402

FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAIL.append(name)


def _base_spec() -> dict:
    """A spec fixture taken from a REAL live artifact, not hand-written.

    Hand-writing it meant iterating against the validator one rejection at a time — bad
    artifact_id, wrong generator_version type, missing effect keys — none of which had anything
    to do with finding 8. A fixture that does not match production shape turns a schema test
    into a test of my ability to guess the schema, and every failure reads as a code defect.

    Falls back to a minimal literal only if no live contract exists, so a fresh Core can still
    run this suite.
    """
    try:
        for a in inst._load_active().get("artifacts", []):
            # Skip legacy_* — hand-authored guardrails that predate the generator and are
            # deliberately outside its id charset (the watchdog skips them for the same reason).
            # Picking one as a fixture makes the validator reject for a reason that is correct
            # about the fixture and irrelevant to the test.
            if str(a.get("artifact_id", "")).startswith("legacy_"):
                continue
            if a.get("type") == "contract" and (a.get("effect") or {}).get("mode") == "inject":
                allowed = {"spec_version", "artifact_id", "case_id", "org_id", "type", "event",
                           "condition", "effect", "tests", "template", "scope", "lease",
                           "generator_version"}
                spec = {k: v for k, v in a.items() if k in allowed}
                if isinstance((spec.get("condition") or {}).get("all"), list):
                    spec = json.loads(json.dumps(spec))
                    # Keep the artifact's production-valid METADATA (id charset, effect shape,
                    # template pin, lease, generator_version) and substitute a condition this
                    # test controls. The metadata is what is hard to guess and easy to get
                    # wrong; the condition is what the test is actually about.
                    spec["condition"] = {"all": [
                        {"op": "event_is", "value": spec.get("event") or "UserPromptSubmit"},
                        {"op": "prompt_regex", "value": r"\breview\b"}]}
                    return spec
    except Exception:
        pass
    return {}


BASE = _base_spec()


def main() -> int:
    print("spec schema integrity (core-business finding 8)")

    # 1. A NARROWED spec must still pass the production validator. This is the whole finding:
    #    the tuner is a second writer into si_artifacts and its output has to clear the same
    #    bar as the first writer's.
    if not BASE:
        # UNDECIDABLE, NOT PASS. This returned 0, and run-all.sh records rc==0 as PASS — so on any
        # Core with no non-legacy inject artifact (a fresh Core, or one whose only artifacts carry
        # the `legacy_` prefix _base_spec skips) this suite certified schema safety while running
        # ZERO of its five checks, including the live-artifact scan this file's own docstring calls
        # "not hypothetical, it is data".
        #
        # The governing rule: every measurement fails toward UNDECIDABLE, never toward PASS. A
        # missing fixture is missing evidence, and missing evidence is not a clean result.
        print("  UNDECIDABLE  no live contract artifact to use as a fixture — 0 of 5 checks ran.")
        print("               Not a pass: this suite cannot certify a schema it never read.")
        return 2
    pos = ["please review the migration carefully", "review the migration before pushing"]
    r = ft.tune(json.loads(json.dumps(BASE)), pos, ["review the weather"],
                lambda spec, text: all(
                    __import__("re").search(c["value"], text, __import__("re").I)
                    for c in spec["condition"]["all"] if c.get("op") == "prompt_regex"))
    check("a narrowing is produced", r.get("ok"), r.get("reason", ""))
    if r.get("ok"):
        # Validate against the SPEC'S OWN org, not a hardcoded 1. The literal made this test fail
        # on every Core except life with "org mismatch" — the fixture is built from that Core's own
        # live artifact, so on core-business it carries org_id=2 and was checked against org 1.
        # Pre-dates today's work (last touched 2026-07-28); found by running the suite on a pulled
        # Core rather than reasoning about it, which is the same way three of my own fleet defects
        # surfaced in this push.
        ok, why = inst._validate_spec(r["spec"], int(r["spec"].get("org_id") or 1))
        check("narrowed spec passes the PRODUCTION validator", ok, why)
        check("narrowed spec carries no bookkeeping key",
              "_tune" not in r["spec"], f"keys={sorted(r['spec'])}")
        # The term must still be reachable — moving it out of the spec must not lose it.
        check("the narrowing term travels alongside the spec", bool(r.get("term")), str(r))

    # 2. The churn cap must still work now that the count is not stored in the spec. If `prior`
    #    were ignored, a rule could be narrowed forever — each step individually valid.
    capped = ft.tune(json.loads(json.dumps(BASE)), pos, [], lambda s, t: True,
                     prior=ft.MAX_NARROWINGS)
    check("churn cap still enforced when count comes from the caller",
          not capped.get("ok") and "already narrowed" in capped.get("reason", ""),
          capped.get("reason", ""))

    # 3. install() must not write keys into the spec that the validator rejects. Rather than
    #    run a real install (which needs a DB and a corpus), assert the source no longer writes
    #    the texts into spec["tests"] — the specific defect — and that the evidence store exists.
    src = (REPO / "scheduling" / "claude-si" / "friction_installer.py").read_text()
    check("install() no longer writes example texts into spec['tests']",
          't["positive_texts"]' not in src and 't["negative_texts"]' not in src)
    check("an out-of-spec evidence store exists",
          "_write_evidence" in src and "def read_evidence" in src)

    # 4. THE GENERAL VERSION. Whatever the allowed key sets are, the tuner's output must be a
    #    subset of them — so this keeps holding if the schema changes later.
    import re as _re
    allowed = set()
    m = _re.search(r'allowed = \{(.*?)\}', src, _re.S)
    if m:
        allowed = set(_re.findall(r'"([a-z_]+)"', m.group(1)))
    if allowed and r.get("ok"):
        extra = set(r["spec"]) - allowed
        check("narrowed spec introduces no key outside the allowed set",
              not extra, f"extra keys: {sorted(extra)}")
    else:
        check("allowed key set was parseable from source", bool(allowed))

    # 5. Live artifacts must be clean too — a regression here is not hypothetical, it is data.
    try:
        active = inst._load_active()
        bad = []
        for a in active.get("artifacts", []):
            if "_tune" in a:
                bad.append((a.get("artifact_id"), "_tune"))
            t = a.get("tests") or {}
            for k in ("positive_texts", "negative_texts"):
                if k in t:
                    bad.append((a.get("artifact_id"), f"tests.{k}"))
        check("no LIVE artifact carries an out-of-schema key", not bad, str(bad[:3]))
    except Exception as e:
        check("live artifact scan ran", False, f"{type(e).__name__}: {e}")

    print(f"\n{'FAILED: ' + ', '.join(FAIL) if FAIL else 'all spec-schema checks pass'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
