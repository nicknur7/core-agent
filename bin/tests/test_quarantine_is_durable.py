#!/usr/bin/env python3
"""A quarantine must survive a re-install, and the watchdog must actually set one.

WHY THIS EXISTS (2026-08-12). Two independent gaps combined into a loop that could not converge, and
each half looked correct on its own:

  1. friction_watchdog logged "quarantine" and then called inst.rollback(aid), which called
     si_project.deactivate() — active=false, quarantined UNTOUCHED. The log line named the intent;
     the call did not carry it. si_project.quarantine() had NO caller anywhere in the tree, while its
     own module docstring said "(used by watchdog)".

  2. The generator retires a case only when EVERY artifact under it is QUARANTINED
     (friction_loop.py:194) — it reads the flag, not `active`. So a deactivated artifact never
     reached the signal that stops its case being re-authored.

  3. And upsert() ended with an unconditional `quarantined=false`. artifact_id = sha256(kind|case_id)
     (artifact_generator.py:55) — it does NOT depend on the spec — so a re-authored artifact reuses
     the SAME row and cleared its own quarantine on the way in.

Net: remove -> re-author -> re-install -> remove, with nothing accumulating. Found on live data, not
by reading code: two rows on life carry a quarantine_reason with quarantined=false. One of them
(art_97b6fff21bdf97478d45, "raw-quote purge 2026-07-30: undistilled 352-char verbatim quote") is
active right now — though benignly, because its re-authoring genuinely fixed the defect it was
quarantined for. That is luck. The mechanism cannot tell a corrected re-author from an identical one.

WHICH IS WHY THE FIX IS NOT "never clear". Same-id re-authoring is the normal path — one row sits at
revision 525 — so a permanent flag would deadlock a case forever after one bad artifact. A silent
deadlock is the same defect as a silent revival, wearing the other costume. The rule is: a
genuinely DIFFERENT spec earns a fresh chance, an IDENTICAL one does not.

ISOLATION: this repoints CLAUDE_PROJECT_DIR at a temp dir BEFORE importing, so project() rewrites a
throwaway active.json instead of the live one (T013). It writes one row to the real corebrain under
a reserved test id and deletes it in a finally.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

failures: list[str] = []
passes: list[str] = []

TEST_AID = "art_deadbeefdeadbeefdead"   # reserved; 20 hex chars, matches _ROLLBACK_ID_RE
TEST_CASE = "fc_selftest_quarantine_durability"


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


# SEAT-RESOLVED, NOT org 1. Caught by sentinel-code on the baseline push, and it is the right
# catch: this file ships to every Core and to a fork. A hardcoded org_id=1 makes a peer write
# into life's partition — which RLS rejects, so the peer gets a confusing failure instead of a
# clean pass, and the fork owner sees a test asserting someone else's seat. Each Core must be
# able to run this against its OWN corpus.
ORG = None   # bound in main() once _env is importable


def _spec(msg: str) -> dict:
    return {
        "artifact_id": TEST_AID, "case_id": TEST_CASE, "type": "contract",
        "event": "UserPromptSubmit", "spec_version": 1, "org_id": ORG,
        "condition": {"all": [{"op": "event_is", "value": "UserPromptSubmit"}]},
        "effect": {"mode": "inject", "message": msg, "skill_id": None},
        "_provenance": "selftest",
    }


def _row(sp, aid: str):
    con = sp.connect_corebrain()
    try:
        cur = con.cursor()
        cur.execute("SELECT quarantined, quarantine_reason, active, revision "
                    "FROM si_artifacts WHERE artifact_id=%s", (aid,))
        return cur.fetchone()
    finally:
        con.close()


def main() -> int:
    print("test_quarantine_is_durable")
    tmp = tempfile.mkdtemp(prefix="si-quarantine-test-")
    state = Path(tmp) / ".claude" / "state"
    (state / "friction-artifacts").mkdir(parents=True)
    (state / ".si-unified-spine").write_text("")      # force the DB path in rollback()
    # BOTH env vars, not just CLAUDE_PROJECT_DIR. friction_installer.py's STATE (and therefore its
    # OWN _unified_spine() — the check inst.rollback() uses to pick DB-path vs legacy active.json)
    # deliberately prefers CORE_INSTANCE over CLAUDE_PROJECT_DIR (bin/friction_installer.py:41,
    # "Explicit operator intent (CORE_INSTANCE) therefore outranks the harness default" — added
    # after CORE_INSTANCE=2..5 + an ambient CLAUDE_PROJECT_DIR silently mixed four peers' artifacts).
    # That precedence is correct production behaviour and must not change. But it means isolating
    # only CLAUDE_PROJECT_DIR is a HALF-isolation: any session that already has CORE_INSTANCE set
    # (every normal Core session does, exported at SessionStart) makes inst.rollback() resolve
    # _unified_spine() against the REAL repo's .claude/state, not this temp dir — so on any seat
    # that has not itself done the SI-unified-spine cutover, rollback() silently falls back to the
    # legacy active.json path, finds nothing (this test never wrote anything there), and returns
    # "not active" — while si_project's own direct calls (which don't gate on _unified_spine())
    # keep working, so only the rollback()-routed checks fail. This is invisible on the author's own
    # live seat, which DID do that cutover 2026-07-23 — CORE_INSTANCE's real marker happened to
    # agree with what the test wanted, by coincidence of that seat's history, not by isolation.
    # Found 2026-09-03 auditing a fresh clone, which has not cut over. Fix: override both.
    _saved_core_instance = os.environ.get("CORE_INSTANCE")
    os.environ["CLAUDE_PROJECT_DIR"] = tmp
    os.environ["CORE_INSTANCE"] = tmp

    sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
    sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
    try:
        import si_project as sp
        import friction_installer as inst
        global ORG
        ORG = sp.get_org_id()
    except Exception as exc:
        print(f"  SKIP  cannot import the SI modules ({exc.__class__.__name__}: {exc})")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    if str(sp.STATE).startswith(str(REPO)):
        print(f"  FAIL  isolation broke — si_project.STATE is {sp.STATE}, inside the live repo. "
              f"Refusing to run: this test would rewrite the live active.json.")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    try:
        try:
            sp.upsert(ORG, _spec("original message"))
        except Exception as exc:
            print(f"  SKIP  corebrain unwritable ({exc.__class__.__name__}) — this test needs a live DB")
            return 1

        # --- durability across re-install -------------------------------------------------
        sp.quarantine(ORG, TEST_AID, "selftest: pretend the watchdog caught this misbehaving")
        q0, r0, _, _ = _row(sp, TEST_AID)
        check("precondition: quarantine() sets the flag and records a reason",
              q0 is True and r0 and "selftest" in r0, f"quarantined={q0!r} reason={r0!r}")

        sp.upsert(ORG, _spec("original message"))          # IDENTICAL spec
        q1, r1, _, rev1 = _row(sp, TEST_AID)
        check("an IDENTICAL re-install does NOT launder the quarantine",
              q1 is True,
              f"quarantined={q1!r} after re-upserting the same spec (revision {rev1}). Re-running the "
              f"installer would clear a quarantine, so nothing the watchdog does can persist.")
        check("...and the reason survives for audit", bool(r1) and "selftest" in (r1 or ""),
              f"reason={r1!r}")

        sp.upsert(ORG, _spec("GENUINELY DIFFERENT message — the generator changed its mind"))
        q2, _, _, _ = _row(sp, TEST_AID)
        check("a genuinely DIFFERENT spec DOES earn a fresh chance (no deadlock)",
              q2 is False,
              f"quarantined={q2!r} after a real re-author. If this stays True, one bad artifact "
              f"suppresses its case forever — a silent deadlock, not a safer system.")

        # --- the watchdog's path actually marks -------------------------------------------
        out = inst.rollback(TEST_AID, reason="selftest: block budget exceeded")
        q3, r3, act3, _ = _row(sp, TEST_AID)
        check("rollback WITH a reason quarantines (the watchdog's path)",
              q3 is True and out.get("ok") is True,
              f"quarantined={q3!r} rollback={out!r} — the watchdog logs 'quarantine' and this is the "
              f"call that must make it true; the generator's case retirement reads this flag")
        check("...and records why", "block budget" in (r3 or ""), f"reason={r3!r}")

        # a plain rollback (no reason) must still be the reversible deactivate, not a quarantine
        sp.upsert(ORG, _spec("reset for the no-reason case"))
        inst.rollback(TEST_AID)
        q4, _, act4, _ = _row(sp, TEST_AID)
        check("rollback WITHOUT a reason still only deactivates (reversible, unchanged behaviour)",
              q4 is False and act4 is False,
              f"quarantined={q4!r} active={act4!r} — a caller that cannot say why is not quarantining")

        # --- migrate_legacy: THE PATH THIS FILE NEVER COVERED ------------------------------
        # Added 2026-08-28. sentinel-code, reviewing a baseline push, found that migrate_legacy's
        # INSERT...ON CONFLICT overwrote active/quarantined/quarantine_reason UNCONDITIONALLY from a
        # freshly recomputed heuristic — carrying none of the `spec IS DISTINCT FROM EXCLUDED.spec`
        # guard upsert() has carried since 2026-08-12 for this exact bug class. Its words: this file
        # "exercises upsert()/rollback() but never calls migrate_legacy()", so the regression test
        # for the bug did not cover the second path that could reintroduce it.
        #
        # The cost was not hypothetical. core-school hand-quarantined three degenerate contracts on
        # its own seat, with its own measured rates and a category-error argument the automatic gate
        # cannot see. migrate_legacy is re-runnable (--migrate-legacy, or re-running
        # si-unify-cutover.sh, which has no post-cutover idempotency guard), so the next run would
        # have silently resurrected all three ACTIVE and overwritten a human's reasoning with a
        # recomputed one. school predicted this exact path before it was found.
        #
        # A SOURCE ASSERTION, not a live migration: migrate_legacy reads the seat's real
        # learned-contracts snapshot and writes real legacy_* rows, so calling it here would mutate
        # live steering on whatever Core runs the suite. The invariant is structural — the three
        # columns must be CASE-guarded on spec-change — so it is checked where it lives.
        sp_src = (REPO / "scheduling" / "claude-si" / "si_project.py").read_text()
        mig = sp_src[sp_src.index("def migrate_legacy("):]
        mig = mig[:mig.index("\ndef ")] if "\ndef " in mig else mig
        check("migrate_legacy guards `active` against a re-run laundering a quarantine",
              "active = CASE WHEN si_artifacts.spec IS DISTINCT FROM EXCLUDED.spec" in mig,
              "migrate_legacy's ON CONFLICT sets active unconditionally. Re-running the migration "
              "would overwrite a hand or watchdog quarantine with a recomputed verdict — the defect "
              "upsert() was fixed for on 2026-08-12, in the sibling that never got it.")

        # THE SPEC-CHANGE CASE IS NOT ENOUGH FOR `quarantined`, AND REQUIRING IT HERE CERTIFIED THE
        # INVERSE BUG (2026-08-28, found by core-business on bus #5706 the same day I wrote the
        # assertion it replaces).
        #
        # This loop demanded `CASE WHEN spec IS DISTINCT FROM EXCLUDED.spec` on all three columns.
        # That guard blocks a re-run from laundering true -> false, which is what it was for. It
        # ALSO blocks false -> true when the spec is byte-identical — so a FIRST, CORRECT quarantine
        # is silently dropped on any row that already exists un-quarantined.
        #
        # On business's cutover the heuristic judged all four legacy_instruction contracts
        # degenerate and the run REPORTED four quarantined. Two landed. `directive` and
        # `preference` matched their stored specs exactly and kept quarantined=false; `directive`
        # then went on dispatching at 1,184 fires, 94 percent of every classifier fire on that
        # corpus. This file passed the whole time, because it was checking the SQL's SHAPE rather
        # than what the shape does.
        #
        # OR is the correct invariant and is strictly stronger: true can never become false (the
        # original protection, now unconditional rather than contingent on the spec matching), and
        # false can become true (a first judgement lands). Un-quarantining stays an explicit act.
        check("migrate_legacy makes `quarantined` a RATCHET, not a spec-change test",
              "quarantined = si_artifacts.quarantined OR EXCLUDED.quarantined" in mig,
              "a spec-change CASE on this column blocks a FIRST-TIME quarantine as well as a "
              "laundering one — measured live on core-business, where two of four correctly-judged "
              "degenerate contracts kept quarantined=false and stayed dispatching.")

        check("migrate_legacy keeps the FIRST `quarantine_reason` that landed",
              "quarantine_reason = COALESCE(si_artifacts.quarantine_reason," in mig,
              "a human's stated reason must outrank a recomputed one; re-running a migration is not "
              "new evidence about an artifact.")

        # AND THE REPORT MUST COME FROM THE DATABASE, NOT FROM THE DECISION. The old code appended to
        # the returned `quarantined` list on `if _quar:` — the heuristic's verdict — so the return
        # value claimed four while the table held two. That divergence is what hid the defect above:
        # the count was read off the report and relayed to Nick and to business without anyone
        # querying the rows. RETURNING makes the two structurally incapable of disagreeing.
        check("migrate_legacy reports what was PERSISTED, via RETURNING",
              "RETURNING quarantined, quarantine_reason" in mig and "_persisted = bool(" in mig,
              "a quarantine count computed from the decision rather than read back from the write "
              "can overstate silently — it did, by 2 of 4.")
        check("a judged-but-unpersisted quarantine is surfaced, never dropped",
              "quarantine_misses" in mig,
              "if the write ever fails to take, the caller must see the shortfall instead of a "
              "report that quietly overstates what landed.")
        # THE SQL, NOT THE DOCSTRING. The first version of this assertion read
        # mig.split('"""')[1] — which is migrate_legacy's DOCSTRING (~300 chars), not the SQL inside
        # cur.execute("""...""") (~3.3k chars) where the defect actually lives. sentinel-code caught
        # it by reinserting a bare percent sign into the SQL comment block and re-running the check:
        # it still returned True. A test that passes either way is worse than no test, and I had
        # asked sentinel-code to check for exactly that class in my own brief.
        #
        # Extract every triple-quoted block inside a cur.execute( in this function and assert on all
        # of them, so adding a second statement later cannot slip past by being the third block.
        import re as _re
        _sql_blocks = _re.findall(r'cur\.execute\(\s*"""(.*?)"""', mig, _re.S)
        _bare = {i: len(_re.findall(r"%(?!s)", blk)) for i, blk in enumerate(_sql_blocks)}
        check("migrate_legacy's SQL carries no bare percent sign",
              bool(_sql_blocks) and not any(_bare.values()),
              f"found {_sql_blocks and sum(_bare.values())} bare percent sign(s) across "
              f"{len(_sql_blocks)} SQL block(s) {_bare}. psycopg2 parses a bare percent sign in SQL "
              f"— INCLUDING inside a -- comment — as a parameter marker, raising "
              f"IndexError: tuple index out of range at execute(). If zero blocks were found the "
              f"extraction itself broke and this assertion is inert, which is the defect it replaced.")
        check("migrate_legacy's placeholder count matches its parameter tuple",
              bool(_sql_blocks) and sum(b.count("%s") for b in _sql_blocks) == 8,
              f"expected 8 %s placeholders across the INSERT, found "
              f"{sum(b.count('%s') for b in _sql_blocks)}. A mismatch is the same IndexError with a "
              f"different cause.")

        # --- the wiring itself, so a future edit cannot silently unwire it -----------------
        wd = (REPO / "scheduling" / "claude-si" / "friction_watchdog.py").read_text()
        check("friction_watchdog passes its reason through to rollback",
              "inst.rollback(aid, reason=reason)" in wd,
              "the watchdog computes a reason for every quarantine decision; dropping it on the call "
              "is exactly the defect this file documents")
    finally:
        try:
            con = sp.connect_corebrain()
            cur = con.cursor()
            cur.execute("DELETE FROM si_artifacts WHERE artifact_id=%s", (TEST_AID,))
            con.commit()
            con.close()
        except Exception as exc:
            print(f"  WARN  could not clean up {TEST_AID}: {exc}")
        shutil.rmtree(tmp, ignore_errors=True)
        if _saved_core_instance is None:
            os.environ.pop("CORE_INSTANCE", None)
        else:
            os.environ["CORE_INSTANCE"] = _saved_core_instance

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
