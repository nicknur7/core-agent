#!/usr/bin/env python3
"""si_project.py — Workstream 1: the canonical→runtime PROJECTION (one spine).

Postgres `si_artifacts` is the source of truth. This rebuilds the DISPOSABLE runtime file
(.claude/state/friction-artifacts/active.json) that the ONE dispatcher reads. Never the reverse
(Codex 2026-07-23: active.json must never be both truth and cache).

  project(org)         -> rebuild active.json from si_artifacts (active, not quarantined); returns summary
  migrate_legacy(org)  -> translate live learned_contracts into si_artifacts (provenance='legacy'),
                          WITHOUT re-gating (they are already trusted/live)
  upsert(org, spec)    -> canonical write of one friction artifact + bump revision (used by installer)
  quarantine(org, aid) -> mark one artifact quarantined in the DB, then re-project. Reached via
                          friction_installer.rollback(aid, reason=...) — the watchdog's path as of
                          2026-08-12. Before that this line said "(used by watchdog)" and the
                          function had no caller anywhere: a docstring describing an integration
                          nobody had wired. Durable by design — project() selects `active AND NOT
                          quarantined`, and install() now preserves the flag across a re-install of
                          an IDENTICAL spec, so a quarantine cannot be laundered by re-running.
  verify_invariants(org) -> WS3 hooks: canonical vs projected parity, orphans, etc.

Org-scoped via connect_corebrain (brain_app + RLS). Fail-open callers wrap this.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "brain-pg"))
from _env import connect_corebrain, get_org_id  # noqa: E402

STATE = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[1]) / ".claude" / "state"
ACTIVE = STATE / "friction-artifacts" / "active.json"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with tmp.open("w") as f:
        f.write(json.dumps(data, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(str(path.parent), os.O_DIRECTORY)
        os.fsync(dfd)
        os.close(dfd)
    except Exception:
        pass


def _bump_canonical(cur, org: int) -> int:
    cur.execute(
        """INSERT INTO si_projection_state (org_id, canonical_rev)
           VALUES (%s, 1)
           ON CONFLICT (org_id) DO UPDATE SET canonical_rev = si_projection_state.canonical_rev + 1
           RETURNING canonical_rev""", (org,))
    return cur.fetchone()[0]


def upsert(org: int, spec: dict, allow_block: bool = False) -> None:
    """Canonical write of one artifact spec into si_artifacts + bump the org revision. The projection
    is rebuilt separately (install() calls project() after commit).

    The canonical writer is a TRUST BOUNDARY (Codex WS4): a block-mode spec may be written ONLY via
    allow_block=True (which only friction_installer.install_shadow_block passes, after it has verified
    the condition against a hash-pinned template), and `enforced` is ALWAYS forced False here — so no
    caller can persist an enforced block, even by calling upsert directly."""
    if not (isinstance(org, int) and org > 0):
        raise ValueError("refusing to write si_artifacts with a non-positive org")
    mode = (spec.get("effect") or {}).get("mode")
    if mode == "block":
        if not allow_block:
            raise ValueError("block specs must go through install_shadow_block, not a bare upsert")
        spec = {**spec, "enforced": False}  # canonical writer forces shadow — belt beyond the installer
    elif mode not in ("inject", None):  # invoke_skill removed 2026-07-27 (dead scaffolding)
        raise ValueError(f"refusing unknown effect mode {mode!r}")
    aid = spec["artifact_id"]
    con = connect_corebrain()
    try:
        cur = con.cursor()
        cur.execute("SELECT spec FROM si_artifacts WHERE artifact_id=%s", (aid,))
        row = cur.fetchone()
        prior = json.dumps(row[0]) if row else None
        cur.execute(
            """INSERT INTO si_artifacts (artifact_id, org_id, provenance, event, spec, active,
                                          quarantined, prior_spec, revision, updated_at)
               VALUES (%s,%s,%s,%s,%s,true,false,%s,1,now())
               ON CONFLICT (org_id, artifact_id) DO UPDATE SET
                 spec=EXCLUDED.spec, event=EXCLUDED.event, active=true,
                 -- A QUARANTINE MUST NOT BE LAUNDERED BY RE-RUNNING THE INSTALLER.
                 -- This was an unconditional `quarantined=false`, which made the flag un-durable
                 -- against the one thing it exists to stop. artifact_id = sha256(kind|case_id)
                 -- (artifact_generator.py:55) — it does NOT depend on the spec — so a re-authored
                 -- artifact for the same case reuses the SAME row, and the upsert cleared its
                 -- quarantine on the way through. Two rows on life still carry a quarantine_reason
                 -- with the flag cleared; that is how this was found, not by reading the code.
                 --
                 -- Cannot simply never-clear: same-id re-authoring is the NORMAL path (one row is at
                 -- revision 525), so a permanent flag would suppress a case forever after one bad
                 -- artifact, and the generator retires a whole case only when EVERY artifact under it
                 -- is quarantined (friction_loop.py:194). That trades a silent revival for a silent
                 -- deadlock — the same defect wearing the other costume.
                 --
                 -- So: a genuinely DIFFERENT spec earns a fresh chance; an IDENTICAL one does not.
                 -- The generator changing its mind is evidence; re-running the installer is not.
                 quarantined = CASE WHEN si_artifacts.spec IS DISTINCT FROM EXCLUDED.spec
                                    THEN false ELSE si_artifacts.quarantined END,
                 prior_spec=%s::jsonb, revision=si_artifacts.revision+1, updated_at=now()""",
            (aid, org, spec.get("_provenance", "friction"), spec.get("event", "UserPromptSubmit"),
             json.dumps(spec), prior, prior))
        _bump_canonical(cur, org)
        con.commit()
    finally:
        con.close()


def quarantine(org: int, artifact_id: str, reason: str = "") -> bool:
    con = connect_corebrain()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE si_artifacts SET quarantined=true, quarantine_reason=%s, updated_at=now() "
            "WHERE artifact_id=%s AND NOT quarantined", (reason[:500], artifact_id))
        changed = cur.rowcount > 0
        if changed:
            _bump_canonical(cur, org)
        con.commit()
        return changed
    finally:
        con.close()


def _clean_spec(spec: dict) -> dict:
    """Strip engine-only keys (leading _) so the runtime file carries only what the dispatcher reads."""
    return {k: v for k, v in spec.items() if not k.startswith("_")}


def _unified_spine() -> bool:
    """Cutover switch: once the marker exists, legacy contracts project into the runtime file too (one
    injector). Before cutover, legacy is EXCLUDED so the still-live classifier owns them (no double-fire)."""
    return (STATE / ".si-unified-spine").exists()


def _enforceable(spec: dict) -> bool:
    """May THIS spec ever be enforced? Fail-closed.

    The condition is authoritative and the label is not, so identity is re-derived here rather than
    trusted: the spec's template.id must resolve to a pinned enforcement template, that template must
    not belong to a never_promote oracle, and the spec's own event/condition/effect must match it.
    Anything unresolvable returns False — this is the last line before a block can stop real work.
    """
    try:
        import sys as _s
        from pathlib import Path as _P
        _s.path.insert(0, str(_P(__file__).resolve().parent))
        import artifact_generator as ag
        import artifact_typer as at
        tpls = ag._load_templates()
        tid = (spec.get("template") or {}).get("id")
        key = next((k for k, t in tpls.items() if t.get("template_id") == tid), None)
        if key is None:
            return False
        if at.ORACLE_CATALOG.get(key, {}).get("never_promote"):
            return False
        t = tpls[key]
        return (spec.get("event") == t["event"]
                and spec.get("condition") == t["condition"]
                and (spec.get("effect") or {}).get("mode") == t.get("effect_mode"))
    except Exception:
        return False


def set_enforced(org: int, artifact_id: str, enforced: bool) -> bool:
    """Flip a block artifact's `enforced` flag inside its stored spec (autonomous enforcement promotion,
    reversibly). Only touches provenance='enforcement' rows. Returns True if changed."""
    if not (isinstance(org, int) and org > 0):
        raise ValueError("set_enforced needs a positive org")
    con = connect_corebrain()
    try:
        cur = con.cursor()
        # EXPLICITLY org-scoped (belt beyond RLS): the composite PK is (org_id, artifact_id), so two orgs
        # can share an artifact_id — never touch another org's row (Codex WS4).
        # FOR UPDATE (Codex 4th round): the row is read, validated, then written. Without it a concurrent
        # writer between SELECT and UPDATE is silently overwritten — not an identity bypass, since the
        # validated stored copy is what gets written, but a real lost update on an enforcement flag.
        cur.execute("SELECT spec FROM si_artifacts WHERE org_id=%s AND artifact_id=%s "
                    "AND provenance='enforcement' FOR UPDATE",
                    (org, artifact_id))
        row = cur.fetchone()
        if not row:
            return False
        # IDENTITY IS CHECKED AT THE MUTATION BOUNDARY, not only in the caller (Codex 3rd round,
        # 2026-07-27). friction_promote.auto_promote checks never_promote and re-derives identity from
        # the condition — but it is only the CURRENT caller. This primitive would enforce anything
        # handed to it, so the invariant lived one level too high: any future caller that forgot the
        # gate could enforce a forbidden oracle. An enforcement primitive must be identity-aware
        # itself. Turning enforcement OFF is always allowed; only turning it ON is gated.
        if enforced and not _enforceable(dict(row[0])):
            return False
        spec = dict(row[0]); spec["enforced"] = bool(enforced)
        # stamp/renew the enforcement lease at PROMOTION time (not install) so the watchdog's lease-age
        # measures from when it started enforcing — re-calling this while eligible RENEWS it (Codex WS4).
        if enforced:
            import time as _t
            spec["enforced_at"] = int(_t.time())
        else:
            spec.pop("enforced_at", None)
        cur.execute("UPDATE si_artifacts SET spec=%s, prior_spec=%s::jsonb, revision=revision+1, updated_at=now() "
                    "WHERE org_id=%s AND artifact_id=%s", (json.dumps(spec), json.dumps(row[0]), org, artifact_id))
        changed = cur.rowcount > 0
        if changed:
            _bump_canonical(cur, org)
        con.commit()
        return changed
    finally:
        con.close()


def deactivate(org: int, artifact_id: str) -> bool:
    """Reversibly remove one artifact from the live set (active=false) + bump revision. Used by rollback."""
    con = connect_corebrain()
    try:
        cur = con.cursor()
        cur.execute("UPDATE si_artifacts SET active=false, updated_at=now() "
                    "WHERE artifact_id=%s AND active", (artifact_id,))
        changed = cur.rowcount > 0
        if changed:
            _bump_canonical(cur, org)
        con.commit()
        return changed
    finally:
        con.close()


def project(org: int) -> dict:
    """Rebuild active.json from si_artifacts (active, not quarantined). Legacy artifacts are included
    ONLY after cutover (see _unified_spine) so the classifier and the dispatcher never both fire them.
    Sets projected_rev=canonical_rev. Returns {artifacts, canonical_rev, projected_rev, checksum}."""
    con = connect_corebrain()
    try:
        cur = con.cursor()
        if _unified_spine():
            cur.execute("SELECT spec, provenance, installed_at FROM si_artifacts WHERE org_id=%s AND active "
                        "AND NOT quarantined ORDER BY installed_at", (org,))
        else:
            cur.execute("SELECT spec, provenance, installed_at FROM si_artifacts WHERE org_id=%s AND active "
                        "AND NOT quarantined AND provenance <> 'legacy' ORDER BY installed_at", (org,))
        specs = []
        for spec_json, provenance, installed_at in cur.fetchall():
            s = _clean_spec(spec_json)
            # Trust is PROJECTOR-controlled, derived from the DB provenance column — NOT from the
            # artifact's own template.id (which an imported/corrupt spec could spoof). Only a
            # genuinely-legacy row gets the trusted-regex bypass (Codex WS1 review).
            s["trusted_regex"] = (provenance == "legacy")
            # `enforced` is AUTHORITATIVE from the DB: ONLY a provenance='enforcement' row may carry it;
            # every other row is forced enforced=false at projection, so a non-enforcement or hand-edited
            # spec can never hard-block even if its stored spec claims enforced=true (Codex WS4).
            s["enforced"] = bool(s.get("enforced")) if provenance == "enforcement" else False
            # carry the canonical install time so the watchdog's lease-expiry works on the DB path
            try:
                s["_installed_at"] = int(installed_at.timestamp()) if installed_at else None
            except Exception:
                s["_installed_at"] = None
            specs.append(s)
        cur.execute("SELECT canonical_rev FROM si_projection_state WHERE org_id=%s", (org,))
        row = cur.fetchone()
        canonical_rev = row[0] if row else 0
        payload = {"artifacts": specs}
        checksum = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

        # REFUSE TO PROJECT NOTHING OVER SOMETHING.
        #
        # active.json is described as "disposable, rebuildable" and that is true exactly when the
        # database is the source of truth for this org. It is FALSE when artifacts exist only in
        # the file — and core-business found that is the live state on every Core except the
        # writer: si_artifacts holds 36 rows, all org_id=1, and zero for orgs 2-5. Business's five
        # firing artifacts have no canonical row at all.
        #
        # So the next project() call on any of those Cores — from a tune, a watchdog sweep, or a
        # close — would SELECT zero rows and atomically write {"artifacts": []} over a file
        # holding live rules. Not corrupt them. Delete them, cleanly, with a valid checksum.
        #
        # An org whose canonical store is genuinely empty and whose projection is also empty is
        # normal and passes. An org that would go from N to 0 has almost certainly lost its rows,
        # not retired them all in one step, so this refuses and says so rather than completing.
        # Set SI_PROJECT_ALLOW_EMPTY=1 for the rare legitimate case (a real mass retirement).
        if not specs:
            try:
                prior = json.loads(ACTIVE.read_text()).get("artifacts", [])
            except Exception:
                prior = []
            if prior and os.environ.get("SI_PROJECT_ALLOW_EMPTY", "") != "1":
                return {"artifacts": len(prior), "canonical_rev": canonical_rev,
                        "projected_rev": None, "checksum": None,
                        "refused": (f"REFUSED to project 0 artifacts over {len(prior)} live ones "
                                    f"for org {org} — si_artifacts has no rows for this org, so "
                                    f"this would DELETE the projection rather than rebuild it. "
                                    f"The artifacts likely never reached the canonical store. "
                                    f"Override with SI_PROJECT_ALLOW_EMPTY=1 only if a mass "
                                    f"retirement is genuinely intended.")}

        _atomic_write(ACTIVE, payload)  # runtime projection — disposable, rebuildable
        cur.execute(
            """INSERT INTO si_projection_state (org_id, canonical_rev, projected_rev, projected_at)
               VALUES (%s,%s,%s,now())
               ON CONFLICT (org_id) DO UPDATE SET projected_rev=%s, projected_at=now()""",
            (org, canonical_rev, canonical_rev, canonical_rev))
        con.commit()
        return {"artifacts": len(specs), "canonical_rev": canonical_rev,
                "projected_rev": canonical_rev, "checksum": checksum}
    finally:
        con.close()


SNAPSHOT = STATE / "learned-contracts.json"  # what learned-classifier.py actually reads (situation-keyed)


def _snapshot_to_spec(key: str, c: dict, org: int) -> dict | None:
    """Translate one snapshot contract (situation-keyed, with regex `triggers` + required_shape +
    forbidden_moves) into an inject-only DSL artifact — EXACT parity with learned-classifier.py's
    matching (same regexes) and message shape (DO/DON'T, shape[:3]/forbidden[:2]). Returns None if the
    contract has no trigger (deliberately dormant — covered by a dedicated gate; must NOT start firing)."""
    triggers = [t for t in (c.get("triggers") or []) if isinstance(t, str) and t]
    if not triggers:
        return None
    trig_legs = [{"op": "prompt_regex", "value": t} for t in triggers]
    legs = [{"op": "event_is", "value": "UserPromptSubmit"},
            ({"any": trig_legs} if len(trig_legs) > 1 else trig_legs[0])]
    lines = [f"📋 learned contract '{key}' — shape your response accordingly:"]
    for s in (c.get("required_shape") or [])[:3]:
        lines.append(f"  DO: {s}")
    for s in (c.get("forbidden_moves") or [])[:2]:
        lines.append(f"  DON'T: {s}")
    return {
        "artifact_id": f"legacy_{key}", "spec_version": 1, "case_id": f"legacy_{key}",
        "org_id": org, "type": "contract", "event": "UserPromptSubmit",
        "condition": {"all": legs},
        "effect": {"mode": "inject", "message": "\n".join(lines)[:2000], "skill_id": None},
        "tests": {"positive_ids": ["legacy"], "negative_ids": ["legacy"]},
        "template": {"id": "legacy-learned-contract", "sha256": "legacy"},
        "scope": "org_local", "lease": {"max_fires_per_session": 2, "expires_at": None},
        "generator_version": "legacy-migration/1", "_provenance": "legacy",
    }


try:
    from friction_test_gate import OVERBROAD_RATE as _OVERBROAD
except Exception:
    _OVERBROAD = 0.03   # mirrors friction_test_gate; only reached if the gate is unimportable


def _bare_token_share(spec: dict) -> "float | None":
    """Fraction of a trigger's alternation branches that are BARE SINGLE WORDS.

    THE SECOND AXIS, AND THE REASON IT EXISTS (2026-08-28, core-school, bus #5579). The first version
    of this gate used rate alone. school refused to run it and showed why with its own measurements:
    a rate gate cannot separate

        (a) fires often because the TRIGGER is junk        instruction emphatic   44.6%
        (b) fires often because the BEHAVIOUR recurs       recall-first            5.4%

    Only (a) is a defect. Rate alone would have quarantined recall-first — a contract school rates
    28 fires, 1.2/wk, WORSENING, and calls the one this seat most needs. It would have failed in the
    expensive direction, fleet-wide, with a computed reason that reads authoritative and is wrong.

    I also gave school 1.8% for recall-first and was wrong: I measured an ABBREVIATED copy of the
    regex I had pasted into a test, not the real one. school measured the spec and got 3.6% on its
    corpus; life's real spec measures 5.4%. The number I used to call it safe came from my own
    truncation.

    Shape separates them cleanly where rate does not. Measured on the real specs:

        legacy_verify-dont-claim      0% bare   1.0% rate   good
        legacy_recall-first           6% bare   5.4% rate   good, and rate-gate would have killed it
        legacy_plan-not-execute      22% bare   1.4% rate   good
        legacy_model-routing         38% bare   0.1% rate   good
        instruction emphatic/directive/preference/standing (school, ops)
                                    100% bare  29-45% rate  degenerate

    A bare alternation of common single tokens carries no behavioural content. A trigger built from
    multi-word phrases names a speech act. The gap between 38% and 100% is where the floor goes.
    """
    import re as _re
    pats = [op.get("value") for op in ((spec.get("condition") or {}).get("all") or [])
            if op.get("op") == "prompt_regex" and op.get("value")]
    if not pats:
        return None
    parts = []
    for rx in pats:
        inner = _re.sub(r"^\\b\(|\)\\b$", "", rx)
        parts += [x for x in inner.split("|") if x]
    if not parts:
        return None
    bare = sum(1 for x in parts if _re.fullmatch(r"[a-z']+", x))
    return bare / len(parts)


# Floor for _bare_token_share. 0.70 sits in the measured gap between the worst good contract (38%)
# and the degenerate class (100%). Deliberately NOT tuned to the boundary.
_DEGENERATE_SHARE = 0.70


def _overbroad(cur, org: int, spec: dict) -> "float | None":
    """Fraction of this org's REAL corpus prompts a spec's prompt_regex ops would fire on.

    Returns None when the corpus is too small to judge (MIN_CORPUS), which callers must treat as
    UNDECIDABLE rather than as a pass. The threshold is imported from friction_test_gate, never
    redeclared here — si_induct.py:130 makes the same point about the same constant: "THE FLOOR IS
    NOT INVENTED HERE."
    """
    import re as _re
    try:
        import friction_test_gate as _tg
    except Exception:
        return None
    pats = []
    for op in ((spec.get("condition") or {}).get("all") or []):
        if op.get("op") == "prompt_regex" and op.get("value"):
            pats.append(op["value"])
    if not pats:
        return None
    cur.execute("SELECT prompt_text FROM pattern_observations "
                "WHERE org_id=%s AND prompt_text IS NOT NULL", (org,))
    rows = [r[0] for r in cur.fetchall()]
    if len(rows) < getattr(_tg, "MIN_CORPUS", 30):
        return None
    try:
        comp = [_re.compile(x, _re.I) for x in pats]
    except _re.error:
        return None
    hits = sum(1 for t in rows if all(c.search(t or "") for c in comp))
    return hits / len(rows)


def migrate_legacy(org: int) -> dict:
    """Bring the live learned-contracts SNAPSHOT (what the classifier actually fires) into si_artifacts
    (provenance='legacy'), WITHOUT re-gating — they are already trusted/live. Idempotent (upsert by
    legacy_<key>). Trigger-less contracts are skipped (deliberately dormant). Returns {migrated, skipped}."""
    # RAISE on a missing/malformed snapshot — a silent "0 migrated" would let the cutover disable the
    # classifier with no legacy guardrails projected (Codex WS1 review). Callers must fail hard.
    snap = json.loads(SNAPSHOT.read_text())
    expected = sum(1 for k, c in snap.items() if _snapshot_to_spec(k, c, org) is not None)
    con = connect_corebrain()
    migrated, skipped, quarantined = [], [], []
    # Judged degenerate but NOT persisted as quarantined. Should always be empty; it exists so
    # that if it ever is not, the caller sees the shortfall instead of a report that quietly
    # overstates what landed.
    quarantine_misses: list = []
    try:
        cur = con.cursor()
        for key, c in snap.items():
            spec = _snapshot_to_spec(key, c, org)
            if spec is None:
                skipped.append(key)
                continue
            cur.execute("SELECT spec FROM si_artifacts WHERE artifact_id=%s", (spec["artifact_id"],))
            ex = cur.fetchone()
            prior = json.dumps(ex[0]) if ex else None
            # OVER-BROAD LEGACY CONTRACTS MIGRATE QUARANTINED, NOT ACTIVE (2026-08-28).
            # Found by core-school (bus #5577) immediately after I cut it over and told it the
            # cutover "retires the degenerate instruction_* contracts". It does not. It MIGRATED
            # them, active, with their triggers intact — they fired through learned-classifier
            # before and through friction-dispatch after. school: "it did not go away, it changed
            # hooks."
            #
            # Measured against each seat's own corpus, versus the 3% bar this system ALREADY
            # enforces at induction (friction_test_gate.OVERBROAD_RATE, si_induct.py:159):
            #     school  instruction emphatic   \b(everything|stage|want|need|done|fucking)\b   44.6%
            #     ops    instruction directive  \b(want|email|look|back|contact|test)\b         41.5%
            #     ops    instruction preference \b(needs|need|right|find|website|correct)\b     29.7%
            #     school  instruction directive  \b(anything|everything|done|below|while|dont)\b 29.0%
            #     GOOD    recall-first                                                            1.8%
            #     GOOD    verify-dont-claim                                                       0.4%
            # The existing bar separates them cleanly at 10-15x over. So this is not a new heuristic
            # — it is the bar that already governs induction, finally applied at migration, which
            # was the one path that minted live steering without consulting it.
            #
            # WHY LIFE NEVER SAW THIS: life's contracts came from learned-contracts-seed.py, six
            # hand-written rules, all specific. The peers INDUCED theirs from their own corpora and
            # induction produced bare word-alternations. Life cut over on 2026-07-23 and migrated
            # only good ones, so the gap sat unexposed until I ran the cutover on a seat that had
            # induced its own.
            #
            # QUARANTINED, NOT DROPPED: the column exists for exactly this, the row stays auditable,
            # and re-activating is a one-field update once a trigger is re-derived. Dropping would
            # discard evidence; migrating active ships noise into always-on steering.
            # UNDECIDABLE (corpus too small) is treated as over-broad — fail closed.
            # BOTH CONDITIONS, NOT EITHER (2026-08-28, core-school refused to run the rate-only
            # version and was right). A contract is quarantined only when it fires too often AND its
            # trigger is a bare alternation of common tokens. Over-rate alone means the behaviour
            # genuinely recurs — that is a contract EARNING its keep, not a defect.
            #
            # UNDECIDABLE (corpus below MIN_CORPUS) no longer quarantines on its own either. school
            # flagged that two of its good contracts measure 0.0% on a 224-prompt corpus because the
            # historical prompts simply do not contain what they fire on — plan-not-execute
            # demonstrably fired on its seat tonight while measuring zero. Failing closed on an
            # unmeasurable rate would have taken those too. An unmeasurable rate now quarantines ONLY
            # if the shape is independently degenerate, which needs no corpus to judge.
            _rate = _overbroad(cur, org, spec)
            _share = _bare_token_share(spec)
            _degenerate_shape = (_share is not None and _share >= _DEGENERATE_SHARE)
            _over_rate = (_rate is not None and _rate > _OVERBROAD)
            _quar = _degenerate_shape and (_over_rate or _rate is None)
            _qreason = (None if not _quar else
                        (f"degenerate trigger: {_share:.0%} of its alternation branches are bare common "
                         f"tokens (floor {_DEGENERATE_SHARE:.0%}), and it fires on "
                         + (f"{_rate:.1%} of this org's corpus vs a {_OVERBROAD:.0%} bar "
                            f"(friction_test_gate.OVERBROAD_RATE)" if _rate is not None
                            else "an unmeasurable share — corpus below MIN_CORPUS")))
            cur.execute(
                """INSERT INTO si_artifacts (artifact_id, org_id, provenance, event, spec, active,
                                              quarantined, quarantine_reason, prior_spec, revision, updated_at)
                   VALUES (%s,%s,'legacy','UserPromptSubmit',%s,%s,%s,%s,%s,1,now())
                   ON CONFLICT (org_id, artifact_id) DO UPDATE SET spec=EXCLUDED.spec,
                     -- A QUARANTINE MUST NOT BE LAUNDERED BY RE-RUNNING THE MIGRATION.
                     -- Mirrors upsert() above, line 112. sentinel-code caught that this INSERT
                     -- overwrote active/quarantined/quarantine_reason UNCONDITIONALLY from a freshly
                     -- recomputed heuristic, carrying none of the guard its sibling has carried since
                     -- 2026-08-12 — and that test_quarantine_is_durable.py exercises upsert() and
                     -- rollback() but has never called migrate_legacy(), so the regression test for
                     -- this exact bug class did not cover the one path that reintroduced it.
                     --
                     -- The cost was concrete and immediate: core-school hand-quarantined three
                     -- degenerate contracts tonight on Nick's direct go, using its OWN measured rates
                     -- and a category-error argument for 'instruction standing' that this gate cannot
                     -- see (all-bare tokens but under the rate bar, so it passes the AND). NOTE: no
                     -- literal percent signs in this SQL comment -- psycopg2 parses a bare percent
                     -- sign as a parameter marker, and writing one inline here raised
                     -- "IndexError: tuple index out of range" at execute() time. Caught by the
                     -- durability test below, not by review. migrate_legacy
                     -- is re-runnable — `--migrate-legacy`, or simply re-running si-unify-cutover.sh,
                     -- which has no post-cutover idempotency guard. The next run would have silently
                     -- resurrected all three ACTIVE and overwritten school's reasoning with my
                     -- heuristic's. school predicted exactly this path and asked me to check it.
                     --
                     -- Same rule as upsert(), for the same reason: a genuinely DIFFERENT spec earns a
                     -- fresh judgement; an IDENTICAL one does not. Re-running a migration is not
                     -- evidence about an artifact. A human quarantine outranks a recomputed one.
                     -- QUARANTINE IS A RATCHET, NOT A SPEC-CHANGE TEST (2026-08-28, found by
                     -- core-business on bus #5706 hours after I wrote the version above).
                     --
                     -- The spec-change CASE was mine, added the same day to stop a re-run
                     -- laundering a HUMAN quarantine with a recomputed one. It does that. It also
                     -- blocks a FIRST-TIME quarantine, which is the far more common case: if the
                     -- row already exists with an IDENTICAL spec and quarantined=false, the guard
                     -- preserves the false and the correct new decision is silently dropped.
                     --
                     -- MEASURED on core-business's cutover: the heuristic judged all four
                     -- legacy_instruction contracts degenerate and the run REPORTED all four
                     -- quarantined. The DB got two. `emphatic` and `tooling` had differing specs so
                     -- EXCLUDED applied; `directive` and `preference` matched byte-for-byte and
                     -- kept their false. `directive` then kept dispatching on a business Core with
                     -- the trigger \b(want|company|they|core|really|work)\b — 1,184 recorded fires,
                     -- 94 percent of every classifier fire in that corpus, at 59.7 percent of
                     -- prompts against a 3 percent bar. The cutover MOVED the worst artifact on the
                     -- seat onto the new spine instead of stopping it, and told me it had handled
                     -- it. business found it, quarantined both by hand, and asked why a gate they
                     -- both fail on both axes half-fired.
                     --
                     -- OR is the correct shape and it preserves the original intent exactly: true
                     -- can never become false here, so no re-run launders a human quarantine; false
                     -- can become true, so a first correct judgement lands. Un-quarantining stays
                     -- what it always was — a deliberate, explicit act, never a migration
                     -- side effect.
                     active = CASE WHEN si_artifacts.spec IS DISTINCT FROM EXCLUDED.spec
                                   THEN EXCLUDED.active ELSE si_artifacts.active END,
                     quarantined = si_artifacts.quarantined OR EXCLUDED.quarantined,
                     -- The FIRST reason to land is kept: a human's wording outranks the heuristic's,
                     -- and re-running a migration is not new evidence about an artifact.
                     quarantine_reason = COALESCE(si_artifacts.quarantine_reason,
                                                  EXCLUDED.quarantine_reason),
                     prior_spec=%s::jsonb, revision=si_artifacts.revision+1, updated_at=now()
                   RETURNING quarantined, quarantine_reason""",
                (spec["artifact_id"], org, json.dumps(spec), not _quar, _quar, _qreason, prior, prior))
            # REPORT WHAT THE DATABASE HOLDS, NEVER WHAT WAS DECIDED. The old line appended on
            # `if _quar:` — the decision — so the return value said four were quarantined while two
            # were. That divergence is what made the defect above invisible: I read the count off
            # this report and relayed it to Nick and to business without checking the rows, which is
            # the "reporting a value is not checking it" class this Core keeps rediscovering.
            # RETURNING makes the two impossible to disagree.
            _row = cur.fetchone()
            _persisted = bool(_row[0]) if _row else False
            if _persisted:
                quarantined.append({"id": spec["artifact_id"], "rate": _rate, "bare_share": _share,
                                    "why": (_row[1] if _row else None) or _qreason})
            elif _quar:
                # Judged degenerate, did NOT persist. Must never be silent again.
                quarantine_misses.append({"id": spec["artifact_id"], "rate": _rate,
                                          "bare_share": _share, "why": _qreason})
            migrated.append(spec["artifact_id"])
        if migrated:
            _bump_canonical(cur, org)
        con.commit()
        return {"migrated": len(migrated), "ids": migrated, "skipped": skipped,
                "expected": expected, "quarantined": quarantined,
                "quarantine_misses": quarantine_misses}
    finally:
        con.close()


def import_active_file(org: int) -> dict:
    """One-time cutover safety: fold any artifacts that currently live ONLY in active.json (installed
    via the pre-cutover legacy path) into the canonical DB, so the switch to DB-first loses nothing."""
    if not ACTIVE.exists():
        existing = []  # no file-only artifacts — nothing to import (fine)
    else:
        # An existing file MUST decode to {"artifacts": [...]}. A malformed object (e.g. {} with no
        # artifacts key, or a non-list) is NOT silently treated as empty — that could drop live
        # artifacts and let cutover proceed as if staging succeeded (Codex WS1 review). Raise instead.
        doc = json.loads(ACTIVE.read_text())
        if not isinstance(doc, dict) or not isinstance(doc.get("artifacts"), list):
            raise ValueError("active.json exists but is not {'artifacts': [...]} — refusing to import")
        existing = doc["artifacts"]
    imported = []
    for spec in existing:
        aid = spec.get("artifact_id")
        if not aid or aid.startswith("legacy_"):
            continue  # legacy is (re)created from the snapshot by migrate_legacy, not imported
        upsert(org, _clean_spec(spec))
        imported.append(aid)
    return {"imported": len(imported), "ids": imported}


def verify_invariants(org: int) -> dict:
    """WS3 seed: parity between canonical DB and the runtime projection. Returns a dict of checks."""
    con = connect_corebrain()
    try:
        cur = con.cursor()
        # the projection includes legacy only post-cutover, so the parity target depends on the flag
        if _unified_spine():
            cur.execute("SELECT count(*) FROM si_artifacts WHERE org_id=%s AND active AND NOT quarantined", (org,))
        else:
            cur.execute("SELECT count(*) FROM si_artifacts WHERE org_id=%s AND active AND NOT quarantined "
                        "AND provenance <> 'legacy'", (org,))
        canonical_live = cur.fetchone()[0]
        cur.execute("SELECT canonical_rev, projected_rev FROM si_projection_state WHERE org_id=%s", (org,))
        row = cur.fetchone()
        crev, prev = (row[0], row[1]) if row else (0, 0)
        try:
            projected = len(json.loads(ACTIVE.read_text()).get("artifacts", []))
        except Exception:
            projected = -1
        return {
            "canonical_live": canonical_live, "projected": projected,
            "count_parity": canonical_live == projected,
            "canonical_rev": crev, "projected_rev": prev, "rev_in_sync": crev == prev,
        }
    finally:
        con.close()


def main() -> int:
    org = get_org_id()
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--migrate-legacy", action="store_true")
    ap.add_argument("--project", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    out = {}
    if a.migrate_legacy:
        out["migrate"] = migrate_legacy(org)
    if a.project or a.migrate_legacy:
        out["project"] = project(org)
    if a.verify or not out:
        out["invariants"] = verify_invariants(org)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
