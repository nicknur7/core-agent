#!/usr/bin/env python3
"""An irreversible sweep may not decide from the cache.

WHY THIS EXISTS (2026-08-12). `friction_watchdog._sweep_orphan_payloads` retires procedure payload
files that no artifact points at. It built its "live" set from `active.json` — the PROJECTION — and
then moved files off disk on the strength of it. si_project's own header states the rule that
violates:

    "Postgres si_artifacts is the source of truth ... active.json must never be both truth and cache"

Absence from the projection is not absence from the system.

MEASURED, and it accounts for the whole of the master plan's "fix the hooked_skill payload-hash bug
(8/8 payload_mismatch)":

    08-10 22:34   the orphan sweep retired art_wf4e24d222a3d9b9a7's payload
    08-11 12:13   the artifact — still live — matched a prompt and could not verify its payload
          ...     seven more times, through 17:41
    08-12 12:37   the payloadless sweep caught it and quarantined the artifact

38 hours live and structurally unable to fire. There was no hash bug: the quarantined copy still
hashes to its pinned sha256 byte-for-byte (878/878). No path divergence — installer and dispatcher
resolve the same procedures dir. No projection field loss — `_clean_spec` keeps `payload`. The
sweep deleted a working artifact's body because a rebuild of the cache had not yet listed it.

WHAT THIS ASSERTS. Not "never retire orphans" — a real orphan means the procedures dir has stopped
being an accurate picture of what is live, and the next artifact reusing that id would inherit a
stale body. The assertion is that the CANONICAL store decides, and that the check fails CLOSED.

THE FAIL-CLOSED DIRECTION IS THE ONE THAT MATTERS and it inverts this module's usual posture.
Everywhere else in the watchdog a failure means work is skipped; here it would mean a live
artifact's body is destroyed. An orphan left in place is inert — nothing references it, there is no
discovery surface. A wrongly-retired payload silently breaks a working rule for as long as nobody
looks, which was 38 hours the one time it happened.
"""
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


def _db_absent_reason() -> str:
    """'' when corebrain answers (or refuses — a refusal is a defect, not absence); the classified
    reason when there is NO database to talk to. _canonical_orphans fails closed on ANY exception,
    so its checked=False cannot tell the two apart — this probe can (Codex review, 2026-09-04)."""
    try:
        from _env import connect_corebrain, db_absent, describe_db_failure
    except Exception as exc:  # noqa: BLE001
        return f"cannot import _env ({exc.__class__.__name__})"
    try:
        connect_corebrain().close()
        return ""
    except Exception as exc:  # noqa: BLE001
        return describe_db_failure(exc) if db_absent(exc) else ""


def main() -> int:
    print("test_orphan_sweep_asks_the_source_of_truth")
    try:
        import friction_watchdog as fw
    except Exception as e:  # noqa: BLE001  # privacy-ok: noqa linter directive, not a course code
        print(f"  FAIL  cannot import friction_watchdog: {e}")
        return 1

    # THE CALL SITE, NOT THE FILE. The first version of this assertion searched the whole module for
    # "_is_orphan_canonically" — which the function's own `def` line satisfies forever. Deleting the
    # CALL from the sweep left the test green, so the dose could not fail. Sixth time today a
    # textual assertion matched a name where it was DEFINED rather than where it was USED, and this
    # one was written into the file whose subject is a check that decided from the wrong source.
    import re
    src = Path(fw.__file__).read_text()
    body = re.search(r"\ndef _sweep_orphan_payloads\(.*?\n(.*?)(?=\ndef )", src, re.S)
    body_txt = body.group(1) if body else ""
    check("the canonical check is called INSIDE _sweep_orphan_payloads",
          callable(getattr(fw, "_canonical_orphans", None))
          and re.search(r"_canonical_orphans\s*\(", body_txt) is not None,
          "the sweep is deciding from active.json again — an irreversible move authorised by a "
          "cache. Note the guard existing is not enough; it has to be reached before the retire.")
    check("...and the call precedes the retire, not follows it",
          body_txt.find("_canonical_orphans") != -1
          and body_txt.find("_canonical_orphans") < body_txt.find("_retire_payload"),
          "checking after the move would document the deletion rather than prevent it")
    if not callable(getattr(fw, "_canonical_orphans", None)):
        return 1

    # --- FAIL CLOSED. Asserted FIRST because it is the direction that destroys data. ------------
    import _env
    real = _env.connect_corebrain
    try:
        _env.connect_corebrain = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
        orphans, checked = fw._canonical_orphans(["art_totally_fabricated"])
        check("an unreachable DB means NOTHING is an orphan — 'cannot check' never authorises a delete",
              orphans == set() and checked is False,
              "this is the inverse of the module's usual fail-open posture, deliberately: a skipped "
              "sweep costs nothing, a wrongly-retired payload silently breaks a live rule")
    finally:
        _env.connect_corebrain = real

    # --- and it must still be able to say YES, or the guard is just an off switch ---------------
    # NEEDS A LIVE DB, DISTINCT FROM THE FAIL-CLOSED CHECK ABOVE. The block above monkeypatches
    # connect_corebrain to force a controlled failure and needs no real database. This one asks
    # _canonical_orphans to actually FIND the id absent, which only a real query can answer — with
    # corebrain unreachable, `_c` (checked) comes back False by the function's own documented
    # fail-closed contract, and that is "could not test this", not "the property is false".
    try:
        _o, _c = fw._canonical_orphans(["art_definitely_not_in_the_db_xyz"])
    except Exception as e:  # noqa: BLE001  # privacy-ok: noqa linter directive, not a course code
        check("an id the canonical store has never seen IS an orphan", False,
              f"raised {type(e).__name__}: {e} — _canonical_orphans is documented to fail closed, "
              f"never to raise")
    else:
        if _c is False and (_why := _db_absent_reason()):
            print(f"  SKIP  {_why} — cannot exercise the canonical YES path without a live DB")
        elif _c is False:
            check("an id the canonical store has never seen IS an orphan", False,
                  "corebrain is REACHABLE but _canonical_orphans reported checked=False — its bare "
                  "`except Exception` turned a schema/query error into 'unavailable'; that is a broken "
                  "sweep, not an absent database")
        else:
            orphan_unknown = "art_definitely_not_in_the_db_xyz" in _o
            check("an id the canonical store has never seen IS an orphan",
                  orphan_unknown is True,
                  f"got {orphan_unknown!r} — if nothing is ever an orphan the sweep is disabled, and "
                  f"the procedures dir stops reflecting what is live, which is what it exists to "
                  f"prevent")

    # --- the live case: a DB-active artifact must be protected ---------------------------------
    # Read a genuinely active id from the canonical store rather than hardcoding one, so this keeps
    # testing the property after the fixture artifacts change.
    live_id = None
    try:
        from _env import connect_corebrain, get_org_id
        conn = connect_corebrain()
        cur = conn.cursor()
        cur.execute("SELECT artifact_id FROM si_artifacts WHERE org_id=%s AND active "
                    "AND NOT COALESCE(quarantined,false) LIMIT 1", (get_org_id(),))
        row = cur.fetchone()
        conn.close()
        live_id = row[0] if row else None
    except Exception:
        pass

    if live_id:
        _o, _c = fw._canonical_orphans([live_id])
        check(f"a DB-active artifact ({live_id[:18]}…) is NOT an orphan — its payload is protected",
              _c is True and live_id not in _o,
              "this is the exact case that broke art_wf4e24d222a3d9b9a7 for 38 hours")
    else:
        print("  SKIP  no active artifact in the canonical store to test the protected case with")

    # THE NO-OP MUST ANNOUNCE ITSELF (core-finance counter 1). Returning an empty list made
    # "swept, nothing orphaned" and "could not sweep" the same result, and the caller reports that
    # list as `orphan_payloads` — so a permanently-inert sweep on a flaky DB read as clean.
    src2 = Path(fw.__file__).read_text()
    check("an unrunnable canonical check is LOGGED, not silently empty",
          "orphan_sweep_undecidable" in src2,
          "this is the same cannot-tell-recorded-as-a-verdict shape fixed in friction_test_gate "
          "hours earlier, and committed here while fixing it there")

    # ONE QUERY, NOT ONE PER FILE (counter 2). Per-file connections make the sweep's reliability
    # degrade as 1-(1-q)^N, and N grows precisely because failures leave payloads un-retired.
    fn = re.search(r"\ndef _canonical_orphans\(.*?\n(.*?)(?=\ndef )", src2, re.S)
    check("the canonical query is batched — one connection for the whole sweep",
          fn is not None and fn.group(1).count("connect_corebrain(") == 1
          and "ANY(%s)" in fn.group(1),
          "a per-file connection converts each transient failure into a separate silent "
          "'not an orphan', and leaves the failure with no single place to be reported")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
