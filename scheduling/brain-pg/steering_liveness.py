#!/usr/bin/env python3
"""steering_liveness — report hooks that are REGISTERED but have never been DISPATCHED.

WHY THIS EXISTS. On 2026-07-30 `capability-usage-log` was found to have produced zero telemetry
lines since it was registered on 2026-07-27, across at least nine real Skill tool calls. The script
was fine — piping stdin through it wrote correctly. The harness simply never dispatched it. Nothing
noticed, because a hook that is never called and a hook that is called and never matches look
IDENTICAL from outside: both emit nothing.

The consequence was not cosmetic. `skill_graduate.demote()` retires a generated skill that has gone
unused for 30 days, and it reads exactly that telemetry. With the recorder dead it can never see a
use, so it fails closed forever — the retirement half of the skill lifecycle did not exist in
practice, and would have kept not existing silently.

This is the same shape as every defect found on 2026-07-28/29: the system fails LOUD when something
is broken and SILENT when something is merely absent. `hooklog.invoked()` was added to tell "ran but
did not match" apart from "never ran" — but nothing ever READ it and complained. This does.

Deliberately report-only. It surfaces rows for core-si's Steering domain; it never edits settings,
never unregisters anything, and never decides that a quiet hook is a dead hook — some hooks are
genuinely rare, and the difference is Nick's call, not a script's.

Usage:
    python3 steering_liveness.py [--json] [--min-sessions N]
    from steering_liveness import find_silent; find_silent()
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import connect_corebrain, get_org_id  # noqa: E402


def _instance() -> Path:
    return Path(os.environ.get("CORE_INSTANCE")
                or os.environ.get("CLAUDE_PROJECT_DIR")
                or Path(__file__).resolve().parents[2])


def registered_hooks() -> dict[str, list[str]]:
    """{hook script stem -> [events it is registered on]} from settings.json."""
    out: dict[str, list[str]] = {}
    try:
        s = json.loads((_instance() / ".claude" / "settings.json").read_text())
    except Exception:
        return out
    for event, items in (s.get("hooks") or {}).items():
        for item in items:
            for hk in item.get("hooks", []):
                cmd = hk.get("command", "")
                for tok in re.findall(r"[\w.-]+\.(?:py|sh)", cmd):
                    stem = Path(tok).name.rsplit(".", 1)[0]
                    out.setdefault(stem, [])
                    if event not in out[stem]:
                        out[stem].append(event)
    return out


def _calls_hooklog(stem: str) -> bool:
    """Does this hook actually emit telemetry, or is zero rows simply expected?

    ADDED 2026-07-30 (master plan Phase 0.5). The comment inside find_silent() described this
    function in detail and the function did not exist. The NameError was then swallowed by the
    surrounding `except Exception` into res["note"], so the tool printed a truncated report with
    a stray "NameError" line and otherwise looked like it had worked — which is exactly the
    failure class it was written to detect, occurring inside the detector.

    A hook's `.sh` wrapper is what settings.json registers, but the instrumentation lives in the
    `.py` it wraps, so both are checked. Anything unreadable counts as NOT instrumented: that
    routes the hook to "uninstrumented" (expected zero rows) rather than "silent" (a real
    defect), which is the direction that avoids a false accusation.
    """
    hooks = _instance() / ".claude" / "hooks"
    for cand in (hooks / f"{stem}.py", hooks / f"{stem}.sh"):
        try:
            if cand.exists() and re.search(r"\bhooklog\b|\bhl\.(?:invoked|fired)\b",
                                           cand.read_text(errors="ignore")):
                return True
        except Exception:
            continue
    return False


# What a hook DOES when it succeeds. The fire counter is `verdict <> 'invoke'`, which can only
# see hooks whose effect IS a verdict — an injection or a block. A hook whose effect is a file
# write, a marker touch, or an unlink registers zero fires no matter how hard it is working.
#
# 2026-07-30, master plan Phase 0.7. The plan said "retire the 8 zero-output hooks". Read one at
# a time, NONE of the eight should be retired, and four of them are not even quiet:
#
#   recall-satisfied   40 invocations, 0 fires — it UNLINKS .recall-required and TOUCHES
#                      .brain-queried-<session>. Its whole job is a side effect. Retiring it
#                      would have left the recall gate permanently armed.
#   capability-usage-log  its only effect is hooklog.log(...) — it IS the telemetry, and
#                      skill_graduate.demote() reads it to retire unused skills. Removing it
#                      removes the retirement half of the skill lifecycle.
#   tracking-orphan-guard, learned-stopguard  2 and 6 invocations, 0 blocks — a guard that has
#                      never HAD to fire is a guard doing its job. Rarity x severity is the
#                      term, not rarity, and cross-core-completion-gate (3 fires in 74 sessions)
#                      caught a real overclaim in me this same session.
#
# So the fix is the counter, not the hooks. This classifies by effect kind so "no effect" and
# "effect the counter cannot see" stop looking identical — the same distinction _calls_hooklog
# makes for dispatch, applied to outcome.
_SIDE_EFFECT_RX = re.compile(
    r"\.unlink\(|\.touch\(|\.write_text\(|\.mkdir\(|json\.dump\(|hooklog\.log\(|"
    r"\bhl\.log\(|open\([^)]*['\"][wa]", re.M)
_VERDICT_RX = re.compile(r"additionalContext|permissionDecision|sys\.exit\(2\)|hookSpecificOutput", re.M)


def _effect_kind(stem: str) -> str:
    """'verdict' (inject/block — the counter sees it), 'side-effect', 'both', or 'unknown'."""
    hooks = _instance() / ".claude" / "hooks"
    src = ""
    for cand in (hooks / f"{stem}.py", hooks / f"{stem}.sh"):
        try:
            if cand.exists():
                src += cand.read_text(errors="ignore")
        except Exception:
            continue
    if not src:
        return "unknown"
    v, s = bool(_VERDICT_RX.search(src)), bool(_SIDE_EFFECT_RX.search(src))
    return "both" if v and s else "verdict" if v else "side-effect" if s else "unknown"


def find_silent(min_sessions: int = 3) -> dict:
    """Hooks registered in settings.json with no steering_events rows at all.

    min_sessions guards against calling a hook dead on thin evidence: if the sensor itself has
    only just started recording, everything looks silent. Below the threshold this returns a
    'insufficient evidence' verdict rather than a list of accusations.
    """
    # "uninstrumented" was written to by find_silent and never initialised here nor printed —
    # the second half of the same 2026-07-30 defect as the missing _calls_hooklog.
    res = {"sessions_observed": 0, "registered": 0,
           "silent": [], "uninstrumented": [], "quiet": [], "unmeasured": [], "note": ""}
    try:
        reg = registered_hooks()
        res["registered"] = len(reg)
        if not reg:
            res["note"] = "could not read settings.json"
            return res

        org = get_org_id()
        conn = connect_corebrain()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM steering_events "
                    "WHERE org_id=%s AND session_id <> ''", (org,))
                res["sessions_observed"] = int(cur.fetchone()[0] or 0)

                cur.execute(
                    "SELECT hook, COUNT(*), COUNT(*) FILTER (WHERE verdict <> 'invoke') "
                    "FROM steering_events WHERE org_id=%s GROUP BY hook", (org,))
                seen = {h: (int(n), int(fired)) for h, n, fired in cur.fetchall()}
        finally:
            conn.close()

        if res["sessions_observed"] < min_sessions:
            res["note"] = (f"only {res['sessions_observed']} session(s) of telemetry — "
                           f"need {min_sessions} before calling anything silent")

        for stem, events in sorted(reg.items()):
            rows, fired = seen.get(stem, (0, 0))
            if rows == 0:
                # ZERO ROWS HAS TWO CAUSES AND THEY NEED OPPOSITE TREATMENT. Either the harness
                # never dispatched the hook (the capability-usage-log class — a real defect), or
                # the hook simply never calls hooklog (an instrumentation gap — expected zero,
                # nothing wrong with the hook itself).
                #
                # Reporting both as "never dispatched" would be a false accusation, and the first
                # run of this tool did exactly that: it listed defensive-save and stop-hook, both
                # of which demonstrably run — defensive-save's commits are in git history and the
                # Stop gates block responses many times a session. A diagnostic that cries wolf
                # gets ignored, which is how the real one hides.
                instrumented = _calls_hooklog(stem)
                res["silent" if instrumented else "uninstrumented"].append({
                    "hook": stem, "events": events,
                    "reason": ("calls hooklog but has produced no rows — harness may not be "
                               "dispatching it" if instrumented else
                               "never calls hooklog — zero rows is expected, not a defect"),
                })
            elif fired == 0:
                # Dispatched, zero non-invoke verdicts. Split by what the hook DOES: a gate whose
                # effect is a verdict and never produced one is genuinely quiet (legitimate for a
                # rare guard, but also what an over-narrow trigger looks like). A hook whose
                # effect is a file write or a marker is INVISIBLE to this counter no matter how
                # hard it works, and calling it quiet is a measurement error, not a finding.
                kind = _effect_kind(stem)
                bucket = "quiet" if kind in ("verdict", "both", "unknown") else "unmeasured"
                res[bucket].append({
                    "hook": stem, "events": events, "invocations": rows, "effect": kind,
                    "reason": ("runs but has never produced a non-invoke verdict"
                               if bucket == "quiet" else
                               "effect is a side effect (write/marker/log) — the fire counter "
                               "cannot see it; zero fires is not evidence of zero work")})
    except Exception as e:
        res["note"] = f"{type(e).__name__}: {e}"
    return res


def find_inert_artifacts() -> list:
    """Installed artifacts that CANNOT fire, whatever their telemetry says.

    A `hooked_skill` pins its payload by sha256 and `friction_dispatch._payload_verified` re-checks
    it at fire time, failing CLOSED — correct behaviour, and completely silent. Measured 2026-08-05:
    `art_6f0e2e344c4bf5a13879` pinned a 944-byte payload while `procedures/` was EMPTY. So the
    artifact sat in active.json looking healthy, `/core-si` counted it, and `skill_graduate` counted
    it 5 fires toward promoting a real SKILL.md — for an artifact that had not been able to fire at
    all. skill_graduate does check the payload before promoting, so nothing broken would have
    shipped; the defect is that nothing ANYWHERE said the artifact was inert.

    This is the same shape as the rest of the 2026-08-05 sweep: a value maintained on one surface
    while every consumer reads another. An artifact that cannot fire is worse than a missing one,
    because its presence is counted as coverage.
    """
    import hashlib
    inst_dir = _instance() / ".claude" / "state" / "friction-artifacts"
    out = []
    try:
        arts = json.loads((inst_dir / "active.json").read_text()).get("artifacts", [])
    except Exception:
        return out
    for a in arts:
        if a.get("type") != "hooked_skill":
            continue
        pl = a.get("payload") or {}
        aid = a.get("artifact_id", "?")
        name = pl.get("path")
        if not name:
            out.append({"artifact_id": aid, "why": "hooked_skill with no payload block"})
            continue
        p = inst_dir / "procedures" / str(name)
        if not p.is_file():
            out.append({"artifact_id": aid, "why": f"payload file MISSING ({name}) — cannot fire, "
                                                   f"and its fire counts are being credited anyway"})
            continue
        try:
            raw = p.read_bytes()
        except Exception as e:
            out.append({"artifact_id": aid, "why": f"payload unreadable: {e}"})
            continue
        if pl.get("sha256") and hashlib.sha256(raw).hexdigest() != pl["sha256"]:
            out.append({"artifact_id": aid, "why": "payload sha256 MISMATCH — edited after install, "
                                                   "so it fails closed at fire time"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-sessions", type=int, default=3)
    a = ap.parse_args()
    r = find_silent(min_sessions=a.min_sessions)
    r["inert_artifacts"] = find_inert_artifacts()
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    print(f"steering liveness — {r['registered']} registered hooks, "
          f"{r['sessions_observed']} sessions of telemetry")
    if r["note"]:
        print(f"  note: {r['note']}")
    if r.get("inert_artifacts"):
        print(f"\n  INERT ARTIFACTS ({len(r['inert_artifacts'])}) — installed, counted as coverage, "
              f"CANNOT FIRE:")
        for h in r["inert_artifacts"]:
            print(f"    {h['artifact_id']:26s} {h['why']}")
    if r["silent"]:
        print(f"\n  NEVER DISPATCHED ({len(r['silent'])}) — registered, zero telemetry:")
        for h in r["silent"]:
            print(f"    {h['hook']:32s} {','.join(h['events'])}")
    if r["uninstrumented"]:
        print(f"\n  NOT INSTRUMENTED ({len(r['uninstrumented'])}) — zero rows is EXPECTED, "
              f"these hooks never call hooklog:")
        for h in r["uninstrumented"]:
            print(f"    {h['hook']:32s} {','.join(h['events'])}")
    if r["quiet"]:
        print(f"\n  RUNS BUT NEVER MATCHES ({len(r['quiet'])}):")
        for h in r["quiet"]:
            print(f"    {h['hook']:32s} {h['invocations']} invocations, 0 fires")
    if r["unmeasured"]:
        print(f"\n  EFFECT NOT VERDICT-SHAPED ({len(r['unmeasured'])}) — zero fires is a "
              f"measurement limit, NOT evidence of zero work:")
        for h in r["unmeasured"]:
            print(f"    {h['hook']:32s} {h['invocations']} invocations · effect={h['effect']}")
    if not r["silent"] and not r["quiet"] and not r["uninstrumented"] and not r["unmeasured"]:
        print("  all registered hooks have dispatched and fired at least once.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
