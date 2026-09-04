#!/usr/bin/env python3
"""friction_promote.py — Workstream 4: the enforcement PROOF-GATE.

Enforcement (block-mode) artifacts are generated and installed in SHADOW: the dispatcher logs a
`shadow_block` would-block event but never actually blocks (double gate: global BLOCKS_ENABLED AND
per-artifact `enforced`, both off by default). This evaluates whether a shadow artifact has EARNED
enforcement — enough clean shadow-fires over a long-enough window on a specific-enough rule — so the
promotion decision rests on lived evidence, not a guess.

AUTONOMOUS promotion (Nick 2026-07-23: "no more human approval gates — test-gate + reversibility"): a
shadow block that (a) uses a template-locked VERIFIED oracle proven equivalent to a live, already-accepted
hand-built gate, and (b) has run clean through the proof window, is flipped to `enforced` automatically.
The safety is NOT a human — it is: verified-oracle-only + the shadow-proof window + full reversibility
(the watchdog auto-quarantines a misbehaving block; BLOCKS_ENABLED is the global kill-switch). This
mirrors how inject contracts already install autonomously.

  evaluate(artifact_id) -> {eligible, stats, reasons}
  auto_promote(org)     -> flip every eligible verified-oracle shadow block to enforced (reversible)
  python3 friction_promote.py [--org N]        # report readiness + (default) auto-promote
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import friction_installer as inst  # noqa: E402

STATE = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[1]) / ".claude" / "state"
ACTION_LOG = STATE / "friction-action-log.jsonl"

MIN_PROOF_DAYS = 7       # a would-block must survive a real work-week of shadow before it can enforce
MIN_PROOF_FIRES = 5      # on at least this many distinct sessions' worth of genuine matches
MIN_DISTINCT_SESSIONS = 3


def _shadow_events() -> list[dict]:
    try:
        lines = ACTION_LOG.read_text().splitlines()
    except Exception:
        return []
    # TWO actions, one window. `shadow_block` is a would-block from an unproven enforcement
    # artifact earning its way UP; `shadow_observe` is a would-fire from a demoted artifact earning
    # its way BACK. Both are "the pattern occurred and we did not act on it", which is precisely
    # what the proof window measures, so they share it rather than getting a second standard.
    #
    # This read filtered on `shadow_block` alone, and it would have made rearm_shadowed() dead on
    # arrival: shadow_observe rows exist, evaluate() could not see them, so every demoted artifact
    # would have reported "no valid shadow fires yet" forever and never re-armed. Same
    # write-one-surface / read-another shape as the defects this whole sweep has been finding —
    # caught here by checking what the reader accepts before trusting the writer.
    #
    # Safe to merge because evaluate() filters by artifact_id and a given artifact only ever emits
    # one of the two: a block-mode spec emits shadow_block, a shadow-mode spec emits shadow_observe.
    _PROOF_ACTIONS = ("shadow_block", "shadow_observe")
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
            if r.get("action") in _PROOF_ACTIONS:
                out.append(r)
        except Exception:
            continue
    return out


def evaluate(artifact_id: str) -> dict:
    """Is this shadow (block) artifact ready for autonomous enforcement? 'eligible' = enough clean
    shadow-fires across enough DISTINCT sessions over enough days. Proof events are DEDUPED by
    (session_id, ts) so replayed/duplicate log rows can't inflate eligibility, and implausible
    timestamps (future / epoch-0) are dropped (Codex WS4 — the ledger is Core-written, not signed;
    dedup + distinct-session + monotonic bounds defeat accidental/replayed inflation)."""
    import time as _t
    now = int(_t.time())
    seen, uniq = set(), []
    for e in _shadow_events():
        if e.get("artifact_id") != artifact_id:
            continue
        ts = e.get("ts")
        sid = e.get("session_id")
        if not isinstance(ts, int) or ts <= 0:
            continue  # implausible timestamp
        if now and ts > now + 3600:
            continue  # future-dated → forged/clock-skew, drop
        key = (sid, ts)
        if key in seen:
            continue  # replayed/duplicate row
        seen.add(key)
        uniq.append(e)
    if not uniq:
        return {"eligible": False, "stats": {"fires": 0}, "reasons": ["no valid shadow fires yet"]}
    ts_list = [e["ts"] for e in uniq]
    sessions = {e.get("session_id") for e in uniq if isinstance(e.get("session_id"), str) and e.get("session_id")}
    span_days = (max(ts_list) - min(ts_list)) / 86400 if len(ts_list) >= 2 else 0.0
    fires = len(uniq)
    reasons = []
    if fires < MIN_PROOF_FIRES:
        reasons.append(f"only {fires}/{MIN_PROOF_FIRES} deduped shadow fires")
    if len(sessions) < MIN_DISTINCT_SESSIONS:
        reasons.append(f"only {len(sessions)}/{MIN_DISTINCT_SESSIONS} distinct sessions")
    if span_days < MIN_PROOF_DAYS:
        reasons.append(f"only {span_days:.1f}/{MIN_PROOF_DAYS}d shadow window")
    eligible = not reasons
    return {"eligible": eligible,
            "stats": {"fires": fires, "sessions": len(sessions), "span_days": round(span_days, 1)},
            "reasons": reasons or ["meets shadow-proof bar — auto-promoting to enforced"]}


MAX_REARMS_PER_CLOSE = 2   # symmetric with friction_loop._MAX_TUNES_PER_CLOSE


def rearm_shadowed(org: int, dry: bool = False, cap: int = MAX_REARMS_PER_CLOSE) -> dict:
    """Restore a SHADOWED artifact to enforcing when its pattern demonstrably came back.

    THIS IS WHAT MAKES DEMOTION SAFE, AND WITHOUT IT DEMOTION IS A SLOW RETIREMENT. Codex, on
    review of the first tuning attempt: *"I find no re-arm path for effect.mode=='shadow' —
    promotion only handles mode=='block'. This is retirement, not reversible shadowing."* Exactly
    right, and it is why nothing may be demoted until this exists.

    The bar is the SAME shadow-proof window an unproven block must clear to earn enforcement
    (MIN_PROOF_FIRES / MIN_DISTINCT_SESSIONS / MIN_PROOF_DAYS), reused rather than reinvented — a
    second recurrence standard is how two halves of one loop start disagreeing about what
    "recurring" means. Symmetry is the point: the same evidence that would promote a novel block
    restores a demoted one.

    The evidence is `shadow_observe` rows, which only exist because the shadow branch evaluates the
    condition before logging. If that ordering regresses, this function silently re-arms on noise —
    so the two are a pair and the comment in friction_dispatch says so.
    """
    out = {"rearmed": [], "holding": [], "errors": []}
    try:
        import friction_installer as fi
        active = fi._load_active().get("artifacts", [])
        for art in active:
            if (art.get("effect") or {}).get("mode") != "shadow":
                continue
            aid = art.get("artifact_id", "?")
            ev = evaluate(aid)          # same window function the block path uses
            stats = ev.get("stats", {})
            if not ev.get("eligible"):
                out["holding"].append({"artifact_id": aid, "stats": stats,
                                       "why": "; ".join(ev.get("reasons", []))})
                continue
            if len(out["rearmed"]) >= cap:
                # Capped for the same reason tuning is: an unbounded acting arm can change every
                # rule in one close. Codex flagged that tune_pass was capped and re-arm was not, so
                # a single close could restore guidance injection across an arbitrary set at once.
                out["holding"].append({"artifact_id": aid, "stats": stats,
                                       "why": f"eligible, deferred by per-close cap {cap}"})
                continue
            if dry:
                out["rearmed"].append({"artifact_id": aid, "stats": stats, "applied": False})
                continue
            restored = {**art}
            eff = dict(restored.get("effect") or {})
            # Back to INJECT, never straight to block. A rule that lost enforcement re-earns the
            # advisory tier first; regaining teeth is a separate decision with its own window.
            eff["mode"] = "inject"
            restored["effect"] = eff
            restored["enforced"] = False
            # BOTH STORES. Writing only active.json left si_artifacts holding the shadow spec, so
            # the next si_project.project() run would faithfully restore the demotion and silently
            # undo the re-arm (Codex: "demotion is therefore not reliably reversible"). The DB is the
            # projection source; a re-arm that does not update it is a re-arm with an expiry date.
            _persist_rearm_db(org, aid, restored)
            _replace_active(aid, restored)
            _log_rearm(aid, org, stats)
            out["rearmed"].append({"artifact_id": aid, "stats": stats, "applied": True})
    except Exception as e:
        out["errors"].append(str(e)[:200])
    return out


def _persist_rearm_db(org: int, aid: str, restored: dict) -> None:
    """Mirror the re-arm into si_artifacts so the projector cannot undo it."""
    import json as _j
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parents[1] / "brain-pg"))
    from _env import connect_corebrain as _cc
    conn = _cc()
    try:
        cur = conn.cursor()
        cur.execute("SET app.current_org_id = %s", (str(org),))
        cur.execute("UPDATE si_artifacts SET prior_spec = spec, spec = %s, "
                    "revision = COALESCE(revision,0)+1, updated_at = now() "
                    "WHERE artifact_id=%s AND org_id=%s", (_j.dumps(restored), aid, org))
        if cur.rowcount != 1:
            conn.rollback()
            raise RuntimeError(f"no si_artifacts row for {aid} (rowcount={cur.rowcount}) — "
                               f"refusing to re-arm in active.json alone; the next projection "
                               f"would restore the shadow spec and silently undo it")
        conn.commit()
    finally:
        conn.close()


def _replace_active(aid: str, new_spec: dict) -> None:
    import json as _j, tempfile, os as _os
    import friction_installer as fi
    p = fi.ACTIVE
    d = _j.loads(p.read_text())
    d["artifacts"] = [new_spec if a.get("artifact_id") == aid else a for a in d.get("artifacts", [])]
    tmp = tempfile.NamedTemporaryFile("w", dir=str(p.parent), delete=False)
    try:
        _j.dump(d, tmp, indent=1); tmp.flush(); _os.fsync(tmp.fileno()); tmp.close()
        _os.replace(tmp.name, p)
    except Exception:
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass
        raise


def _log_rearm(aid: str, org: int, stats: dict) -> None:
    import json as _j
    import friction_installer as fi
    try:
        with open(fi.ACTION_LOG, "a") as f:
            f.write(_j.dumps({"action": "rearm_from_shadow", "artifact_id": aid,
                              "org_id": org, "stats": stats}) + "\n")
    except Exception:
        pass


def _verified_template_ids() -> set:
    """Template ids whose oracle is verified-equivalent to a live accepted gate — the ONLY blocks
    eligible for autonomous enforcement. Sourced from the hash-pinned enforcement templates."""
    try:
        import artifact_generator as ag
        import artifact_typer as at
        # An oracle may be sound as an OBSERVATION yet wrong as an ENFORCEMENT — the review oracle
        # observes at Stop, after the push has already run, so enforcing it could only fail the turn
        # afterwards and invite a retry of a non-idempotent operation (Codex review, 2026-07-27).
        # Such entries carry never_promote and are excluded here, so no proof window can flip them.
        blocked = {k for k, v in at.ORACLE_CATALOG.items() if v.get("never_promote")}
        tpls = ag._load_templates()
        return {t.get("template_id") for k, t in tpls.items() if k not in blocked}
    except Exception:
        return set()


def _identity_ok(art: dict) -> bool:
    """Does this artifact's CONDITION actually belong to the template it claims, and is that template
    one that may be enforced?

    The condition is authoritative; the label is not. An artifact could otherwise carry a
    never_promote oracle's condition under a promotable template id and be enforced on that basis.
    Fail-closed: anything unresolvable returns False, because this is the last gate before a block
    gains the power to stop work.
    """
    try:
        import artifact_generator as ag
        import artifact_typer as at
        tpls = ag._load_templates()
        tid = (art.get("template") or {}).get("id")
        key = next((k for k, t in tpls.items() if t.get("template_id") == tid), None)
        if key is None:
            return False
        if at.ORACLE_CATALOG.get(key, {}).get("never_promote"):
            return False
        t = tpls[key]
        return (art.get("event") == t["event"]
                and art.get("condition") == t["condition"]
                and (art.get("effect") or {}).get("mode") == t.get("effect_mode"))
    except Exception:
        return False


def auto_promote(org: int, dry: bool = False) -> dict:
    """Flip every ELIGIBLE, verified-oracle shadow block to enforced (autonomous, reversible). A block
    is eligible only if its template is a verified-oracle one AND friction_promote.evaluate() clears the
    shadow-proof window. Never promotes a novel/unverified oracle. Fail-open."""
    out = {"promoted": [], "pending": [], "skipped": 0}
    try:
        import si_project
        verified = _verified_template_ids()
        active = inst._load_active().get("artifacts", [])
        for a in active:
            if a.get("effect", {}).get("mode") != "block":
                continue
            aid = a.get("artifact_id")
            if a.get("template", {}).get("id") not in verified:
                out["skipped"] += 1  # unverified oracle → never auto-enforce
                continue
            # RE-VERIFY IDENTITY AT PROMOTION, not just at install (Codex 2nd round, 2026-07-27).
            # Trusting the stored template.id alone was the other half of the critical: an artifact
            # whose condition belongs to a never_promote oracle but whose label says otherwise would
            # pass the check above. The condition itself is authoritative, so it is re-matched
            # against the pinned template here — the last gate before something gains teeth.
            if not _identity_ok(a):
                out["skipped"] += 1
                inst._log("auto_promote_identity_fail", artifact_id=aid)
                continue
            already = a.get("enforced") is True
            ev = evaluate(aid)
            if not ev.get("eligible"):
                # an enforced block that LOSES eligibility is left to the watchdog's lease to expire
                # (auto-off) — never renewed here; a shadow block just keeps waiting.
                out["pending"].append((aid, ev.get("reasons")))
                continue
            if dry:
                out["promoted"].append((aid, "would_renew" if already else "would_promote", ev.get("stats")))
                continue
            # promote a newly-eligible shadow block, OR renew the lease of a still-eligible enforced one
            if si_project.set_enforced(org, aid, True):
                si_project.project(org)
                inst._log("auto_promote_renew" if already else "auto_promote_enforce",
                          artifact_id=aid, stats=ev.get("stats"))
                out["promoted"].append((aid, "renewed" if already else "enforced", ev.get("stats")))
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def report(org: int) -> dict:
    """Readiness of every active block-mode artifact (from the runtime projection)."""
    active = inst._load_active().get("artifacts", [])
    blocks = [a for a in active if a.get("effect", {}).get("mode") == "block"]
    return {"block_artifacts": len(blocks),
            "readiness": {a.get("artifact_id"): evaluate(a.get("artifact_id")) for a in blocks}}


def main() -> int:
    from _env import get_org_id  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
    org = get_org_id()
    out = report(org)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
