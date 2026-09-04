#!/usr/bin/env python3
"""Drive the tune path end to end and see whether it actually narrows.

WHY THIS EXISTS
---------------
The tune path was written, then fixed five times in one night across findings 1, 3, 4, 6, 7,
7b, 8 and 9 — and every one of those defects was found by READING the code. Not one was found
by the code doing something wrong, because the tune path had never executed. core-business,
2026-07-28: "Every finding tonight was found by reading. Not one was found by something
breaking. That is a real limit on what ten findings prove."

A path that has never run is not a working path; it is a plausible one. This runs it.

WHAT IT DRIVES
--------------
The whole chain, against the production modules, in a sandbox org:

    evidence store -> _has_wrong_fire_evidence -> _try_narrow -> friction_tune.propose_narrowing
                   -> verify (production matcher) -> si_project.upsert -> evidence updated

The setup is the hard part and is deliberately literal about what the watchdog requires before
it will consider tuning at all: an ENFORCED block, over its per-session fire budget, that
matches one of its OWN recorded negatives, with at least two positives to generalise from.
Getting any of those wrong makes the test pass by not reaching the code.

Uses org 97 (a sandbox tenant) and a temp state dir, so it touches neither this Core's live
artifacts nor its action log. Cleans up after itself. Skips when Postgres is unreachable.

    python3 bin/tests/test_tune_path_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ORG = 97
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAIL.append(name)


def db_ok() -> bool:
    try:
        return subprocess.run(["psql", "-d", os.environ.get("COREBRAIN_DB", "corebrain"),
                               "-tAc", "SELECT 1"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


def main() -> int:
    print("tune path end-to-end")
    if not db_ok():
        print("  SKIP  corebrain unreachable")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="tune-e2e-"))
    os.environ["CLAUDE_PROJECT_DIR"] = str(tmp)
    # CORE_INSTANCE TOO — the modules resolve their state root from it and fall back to their own
    # repo root when unset, so this test wrote art_tune_e2e_probe into whichever seat ran it. It sits
    # in bin/tests, a SHARED dir, so it shipped to every Core: core-business found the probe in the
    # live active.json of school, finance AND ops (bus #1041), on seats that never authored it. The
    # DB rows for ORG 97 are cleaned up in the finally block below; the SEAT's projection was not.
    os.environ["CORE_INSTANCE"] = str(tmp)
    os.environ["CORE_ORG_ID"] = str(ORG)
    sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
    sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

    import friction_installer as inst
    import friction_watchdog as fw
    import si_project

    aid = "art_tune_e2e_probe"
    try:
        # ── 1. THE MECHANISM ────────────────────────────────────────────────────────────
        # Drive _try_narrow with real evidence and the production matcher. Persistence is
        # stubbed here so this measures the NARROWING, not the writer; reachability is a
        # separate question, asserted in part 2.
        evidence = {
            "positive_texts": [
                {"text": "push the migration once the tests are green", "channel": "prompt"},
                {"text": "push the migration to staging first", "channel": "prompt"}],
            "negative_texts": [
                {"text": "push the button on the coffee machine", "channel": "prompt"}],
        }
        art = {"artifact_id": aid, "event": "UserPromptSubmit",
               "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                                     {"op": "prompt_regex", "value": r"\bpush\b"}]},
               "effect": {"mode": "block", "message": "run the tests before pushing"}}

        real_read, real_write = inst.read_evidence, inst._write_evidence
        real_upsert, real_project = si_project.upsert, si_project.project
        persisted = {}
        try:
            inst.read_evidence = lambda a, o=None: evidence
            inst._write_evidence = lambda a, pl, o=None: persisted.__setitem__("ev", pl) or True
            si_project.upsert = lambda o, sp, **k: persisted.__setitem__("spec", sp)
            si_project.project = lambda o: None

            check("the artifact has wrong-fire evidence (matches its own negative)",
                  fw._has_wrong_fire_evidence(art) is True)

            note = fw._try_narrow(aid, art)
            check("_try_narrow returns a narrowing", bool(note), str(note))
            if note:
                print(f"        {note}")
            sp = persisted.get("spec") or {}
            clauses = (sp.get("condition") or {}).get("all") or []
            check("the condition gained exactly one conjunct", len(clauses) == 3, str(len(clauses)))
            check("the narrowed spec carries no bookkeeping key", "_tune" not in sp,
                  str(sorted(sp)))
            check("narrow_count recorded outside the spec",
                  (persisted.get("ev") or {}).get("narrow_count") == 1,
                  str((persisted.get("ev") or {}).get("narrow_count")))

            # The invariant must still REFUSE an unsafe narrowing. Without this the above
            # could pass by the tuner having stopped checking anything.
            bad = dict(art)
            bad["condition"] = {"all": [{"op": "prompt_regex", "value": r"\bzzz\b"}]}
            persisted.clear()
            check("a narrowing that would break a positive is refused",
                  fw._try_narrow(aid, bad) is None)
        finally:
            inst.read_evidence, inst._write_evidence = real_read, real_write
            si_project.upsert, si_project.project = real_upsert, real_project

        # ── 2. REACHABILITY THROUGH sweep(), WHICH WAS THE ACTUAL FINDING ───────────────
        # The mechanism worked and could not run: the only caller of _try_narrow was the
        # "over block budget" branch, which requires effect.mode == "block", and upsert
        # refuses to persist a block without allow_block=True. Every artifact that could
        # TRIGGER a tune was one the writer would not ACCEPT.
        #
        # That writer guard is correct and stays — this asserts it, so a future change that
        # loosened it would fail here.
        refused = False
        try:
            si_project.upsert(ORG, {"artifact_id": "art_probe", "org_id": ORG, "type": "contract",
                                    "event": "UserPromptSubmit",
                                    "condition": {"all": [{"op": "prompt_regex", "value": "x"}]},
                                    "effect": {"mode": "block", "message": "y"}})
        except Exception:
            refused = True
        check("the canonical writer still refuses a bare block spec", refused)

        # The gap is closed by giving CONTRACTS their own route in, triggered by purpose
        # rather than by rate. Driven through the real sweep() so this tests the wiring, not
        # just _try_narrow — the wiring is precisely what was missing.
        contract = {"artifact_id": aid, "event": "UserPromptSubmit",
                    "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"},
                                          {"op": "prompt_regex", "value": r"\bpush\b"}]},
                    "effect": {"mode": "inject", "text": "run the tests before pushing"}}
        inst.ARTDIR.mkdir(parents=True, exist_ok=True)
        inst.ACTIVE.write_text(json.dumps({"artifacts": [contract]}, indent=2))

        real_read2 = inst.read_evidence
        try:
            inst.read_evidence = lambda a, o=None: evidence
            res = fw.sweep(dry=True)
            tuned = [t for t in (res.get("tuned") or [])]
            check("a CONTRACT with contradicting evidence is now reachable by sweep()",
                  bool(tuned), f"tuned={tuned} quarantined={res.get('quarantined')}")
            check("tuning a contract never quarantines it",
                  not res.get("quarantined"), str(res.get("quarantined")))
        finally:
            inst.read_evidence = real_read2

        # And the negative case: a contract that does NOT contradict its evidence must be left
        # alone. Without this the trigger could be "tune every inject", which would pass the
        # check above and be badly wrong.
        clean_ev = {"positive_texts": evidence["positive_texts"],
                    "negative_texts": [{"text": "unrelated sentence about lunch",
                                        "channel": "prompt"}]}
        try:
            inst.read_evidence = lambda a, o=None: clean_ev
            res2 = fw.sweep(dry=True)
            check("a contract with NO contradiction is left alone",
                  not (res2.get("tuned") or []), str(res2.get("tuned")))
        finally:
            inst.read_evidence = real_read2

    finally:
        subprocess.run(["psql", "-d", os.environ.get("COREBRAIN_DB", "corebrain"), "-c",
                        f"DELETE FROM si_artifacts WHERE org_id={ORG}"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'FAILED: ' + ', '.join(FAIL) if FAIL else 'the tune path runs end to end'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
