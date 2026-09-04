#!/usr/bin/env python3
"""estate-sweep.py — one maintenance pass over the WHOLE estate: hooks, artifacts, skills, commands.

WHY
---
The operator, 2026-07-27: everything should be autonomously maintained and tuned — old hooks,
anything untracked — clean, with a genuine reason for being there.

What the audit found that day:
- all 36 curated hook dispositions said `keep`. A review that has never once said `remove` is not a
  review.
- six hooks were registered in settings.json but absent from hook-registry.json, so nothing
  maintained them, they never propagated to other Cores, and a settings rebuild would drop them.
- roughly one hook in five emitted no telemetry at all, so nothing could judge them either way.
- the approval-gate token list has now been patched FIVE separate times, each after it falsely
  blocked work. Maintained by after-the-fact patching, which is why it keeps missing.

THE RULE THIS ENCODES
---------------------
Everything installed must be either (a) demonstrably earning its place, or (b) explicitly protected.
There is no third state called "nobody has looked at it since it shipped".

SAFETY
------
Proposals are computed from evidence; ACTIONS are deliberately narrow. Nothing is ever deleted —
adoption edits a registry, retirement archives. Protected hooks, legacy_* artifacts, sentinel-flagged
hooks and hand-authored skills are excluded by construction, not by a filter someone might reorder.

The FIRST run reports and applies nothing, so the one pass most likely to contain a judgement call
(a hook that looks dead but exists for a rare case) is visible before anything moves.

  python3 bin/estate-sweep.py            # report only
  python3 bin/estate-sweep.py --apply    # act on the safe classes
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def repo() -> Path:
    env = os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    try:
        return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                   capture_output=True, text=True, check=True).stdout.strip())
    except Exception:
        return Path(__file__).resolve().parent.parent


REPO = repo()
STATE = REPO / ".claude" / "state"
DISPO = STATE / "hook-dispositions.json"
SETTINGS = REPO / ".claude" / "settings.json"
REGISTRY = REPO / "bin" / "hook-registry.json"
FIRST_RUN_MARKER = STATE / ".estate-sweep-ran"

# Mirrors reconcile-hooks.PROTECTED_HOOKS. A security gate that happens to be quiet is still a
# security gate — quietness is what a working guard looks like.
PROTECTED = {"pretooluse-guard", "sentinel-approve", "shared-write-guard"}

UNUSED_DAYS = 30
MIN_AGE_DAYS = 30


def _hook_name(cmd: str):
    m = re.search(r"([A-Za-z0-9_-]+)\.(sh|py)", cmd or "")
    return m.group(1) if m else None


def registered_hooks() -> dict:
    """{hook_name: {(event, matcher, command), ...}}.

    Identity includes the MATCHER (Codex review, 2026-07-27). Keying on (name, event) alone collapsed
    a hook registered under two different matchers on the same event into one registry entry, so the
    other machines would silently lose that coverage on the next settings rebuild. No hook has two
    matchers on one event today — this is a latent loss, fixed before it can happen rather than after.
    """
    out: dict = {}
    try:
        s = json.loads(SETTINGS.read_text())
    except Exception:
        return out
    for ev, blocks in s.get("hooks", {}).items():
        for b in blocks:
            for h in b.get("hooks", []):
                n = _hook_name(h.get("command", ""))
                if n:
                    out.setdefault(n, set()).add((ev, b.get("matcher", "") or "", h.get("command", "")))
    return out


def managed_hooks() -> set:
    """{(name, event, matcher)} — full registration identity, not just names.

    Collapsing to name meant a hook already managed for matcher A, then registered live under a
    second matcher B, was never seen as untracked at all — B would never propagate (Codex 2nd round).
    """
    try:
        return {(h["name"], h["event"], h.get("matcher", "") or "")
                for h in json.loads(REGISTRY.read_text())["hooks"]}
    except Exception:
        return set()


GRADES = STATE / "gate-grades.json"


def _grades() -> dict:
    """Measured fire rates from bin/grade-gate.py --all, written at every close.

    Kept separate from the disposition file on purpose. Dispositions are derived from FIRE
    COUNTS — "ran 62x, matched 0" — which answers "did this ever match" and can never answer
    "does this fire on one turn in twenty", because a count does not know how many chances it
    had. recall-gate sat at 4.8% and was indistinguishable here from a healthy hook until the
    grader measured it against the real corpus.
    """
    try:
        return json.loads(GRADES.read_text()).get("gates", {})
    except Exception:
        return {}


def sweep() -> dict:
    findings = {"untracked": [], "unmeasured": [], "tune": [], "over_broad": [], "watch": [],
                "retire": [], "protected": []}
    try:
        dispo = json.loads(DISPO.read_text()).get("hooks", {})
    except Exception:
        dispo = {}
    reg, managed = registered_hooks(), managed_hooks()
    grades = _grades()

    # 1) UNTRACKED — live but unmanaged, compared on FULL registration identity.
    for name in sorted(reg):
        missing = sorted({(ev, m, c) for ev, m, c in reg[name]
                          if (name, ev, m) not in managed})
        if not missing:
            continue
        if name in PROTECTED:
            findings["protected"].append({"hook": name, "why": "protected; registered outside registry"})
            continue
        findings["untracked"].append({
            "hook": name, "registrations": missing,
            "events": sorted({ev for ev, _m, _c in missing}),
            "why": "registered in settings.json but absent from hook-registry.json — nothing "
                   "maintains it, it never reaches other Cores, and a settings rebuild drops it",
            "action": "adopt into hook-registry.json"})

    # 2) EVIDENCE verdicts, computed by refresh-hook-dispositions from the telemetry bus.
    for name, e in sorted(dispo.items()):
        if not isinstance(e, dict):
            continue
        ev = (e.get("evidence") or {})
        verdict, why = ev.get("verdict"), ev.get("why", "")
        live = e.get("live") or {}
        if name in PROTECTED:
            continue
        if verdict == "unmeasured":
            findings["unmeasured"].append({"hook": name, "why": why,
                                           "action": "instrument before any verdict is trusted"})
        elif verdict == "tune":
            findings["tune"].append({"hook": name, "why": why,
                                     "fires": live.get("fires"),
                                     "invocations": live.get("invocations"),
                                     "action": "narrow or broaden the trigger"})
        # RETIRE is proposed only with positive evidence of disuse — never from silence, because
        # silence here usually means missing instrumentation rather than a dead hook.
        inv, fires = live.get("invocations") or 0, live.get("fires") or 0
        last = live.get("last_fired") or live.get("last_invoked")
        if inv > 0 and fires == 0 and verdict == "tune":
            age_ok = True
            if last:
                try:
                    age_ok = (time.time() - time.mktime(time.strptime(last[:10], "%Y-%m-%d"))) / 86400 >= MIN_AGE_DAYS
                except Exception:
                    age_ok = False
            if age_ok:
                findings["retire"].append({
                    "hook": name, "why": f"ran {inv}x, matched 0 times, >={MIN_AGE_DAYS}d old",
                    "action": "archive to .claude/hooks/archive/"})

    # 3) MEASURED rates — the class fire counts are structurally blind to.
    #
    # OVER_BROAD is asserted only from the LIVE rate. A pattern rate is a ceiling: the gate
    # stands down when the turn already did the work (a same-turn Write for say-do-gap, a
    # same-turn read for the recall gates), so tuning on the ceiling would weaken gates that
    # are fine in practice. WATCH exists to carry that distinction instead of collapsing it —
    # "high, but unproven live" is genuinely different from "measured too broad", and the
    # first is not a mandate to touch anything.
    for name, g in sorted(grades.items()):
        if name in PROTECTED:
            continue
        v = g.get("verdict")
        if v == "over_broad":
            findings["over_broad"].append({
                "hook": name,
                "why": f"live rate {g.get('live_rate', 0):.1%} exceeds the {json.loads(GRADES.read_text()).get('bar', 0.03):.0%} bar "
                       f"({g.get('live_blocks')} blocks in {(g.get('live_blocks') or 0) + (g.get('live_invocations') or 0)} invocations)",
                "action": "narrow toward its intent examples — positives must still pass"})
        elif v == "watch":
            findings["watch"].append({
                "hook": name,
                "why": f"pattern rate {g.get('pattern_rate', 0):.1%} is above the bar but only "
                       f"{g.get('live_invocations')} live invocation(s) exist — a ceiling, not a rate",
                "action": "no change; the pattern rate is an upper bound. Wait for live telemetry."})
    return findings


def apply_safe(findings: dict, dry: bool = True) -> dict:
    """Apply ONLY the unambiguously-safe class: adopting untracked hooks into the registry.

    Tuning a trigger and retiring a hook are deliberately NOT automated here. Both need a judgement
    about intent that telemetry alone does not carry — a hook can be silent because it is broken, or
    because the thing it guards against genuinely stopped happening, and those look identical from
    fire counts. Adoption has no such ambiguity: a live hook belongs in the registry either way.
    """
    done = {"adopted": [], "skipped": []}
    if not findings["untracked"]:
        return done
    # LOCKED read-modify-write (Codex 3rd/4th round). A hash check before os.replace still leaves a
    # check-to-replace window, so an exclusive advisory lock is held across read, modify AND replace.
    # The whole post-acquisition body sits in try/finally: relying on CPython closing the file when
    # the frame unwinds is an implementation side effect, not a critical-section design, and one
    # early return already skipped the explicit unlock. A stale .lock FILE cannot deadlock anything —
    # flock attaches to the open descriptor, not to the path.
    lockf = None
    try:
        import fcntl
        import hashlib as _h0
        lockf = open(str(REGISTRY) + ".lock", "w")
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
    except Exception as e:
        if lockf:
            try:
                lockf.close()
            except Exception:
                pass
        done["skipped"].append(f"could not acquire registry lock: {e}")
        return done
    try:
        try:
            baseline_hash = _h0.sha256(REGISTRY.read_bytes()).hexdigest()
            reg = json.loads(REGISTRY.read_text())
        except Exception as e:
            done["skipped"].append(f"registry unreadable: {e}")
            return done
        have = {(h["name"], h["event"], h.get("matcher", "") or "") for h in reg["hooks"]}
        try:
            settings = json.loads(SETTINGS.read_text())
        except Exception as e:
            done["skipped"].append(f"settings unreadable: {e}")
            return done
        for f in findings["untracked"]:
            name = f["hook"]
            # one registry entry per (event, matcher) — not per event — so a hook registered under two
            # matchers keeps both when the fleet rebuilds settings from the registry
            for ev, matcher, cmd in sorted(f.get("registrations") or []):
                if (name, ev, matcher) in have:
                    continue
                if not cmd:
                    done["skipped"].append(f"{name}@{ev}: command not found in settings")
                    continue
                entry = {"name": name, "event": ev, "matcher": matcher or "",
                         "command": cmd, "timeout": 5, "scope": "universal"}
                if not dry:
                    reg["hooks"].append(entry)
                done["adopted"].append({"hook": name, "event": ev, "dry": dry})
        if not dry and done["adopted"]:
            # UNIQUE temp name + re-read under the same pass (Codex review, 2026-07-27). Every invocation
            # used the identical `hook-registry.json.tmp`, so two concurrent runs — or a run overlapping a
            # human edit — would collide on it, and the last replace would silently erase the other's
            # change. A pid-scoped temp in the same directory keeps the rename atomic without sharing a
            # name, and re-reading immediately before replace catches a registry that changed underneath.
            tmp = REGISTRY.with_suffix(f".json.tmp.{os.getpid()}")
            try:
                # CONTENT hash, not a length compare (Codex 2nd round): a concurrent edit that changes
                # an entry without changing the count slipped past a length check and got overwritten.
                import hashlib as _h
                if _h.sha256(REGISTRY.read_bytes()).hexdigest() != baseline_hash:
                    done["skipped"].append("registry changed underneath this sweep — not writing")
                    raise RuntimeError("registry changed underneath this sweep")
                tmp.write_text(json.dumps(reg, indent=2) + "\n")
                json.loads(tmp.read_text())      # prove it parses before it can replace anything
                os.replace(tmp, REGISTRY)
            except Exception as e:
                try:
                    tmp.unlink()
                except Exception:
                    pass
                done["skipped"].append(f"registry write aborted, file untouched: {e}")
        return done
    finally:
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
            lockf.close()
        except Exception:
            pass
    return done


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    f = sweep()
    first_run = not FIRST_RUN_MARKER.exists()
    print(f"ESTATE SWEEP — {REPO.name}")
    for k in ("untracked", "unmeasured", "tune", "over_broad", "watch", "retire"):
        items = f[k]
        print(f"\n{k.upper()} ({len(items)})")
        for i in items[:20]:
            print(f"  · {i['hook']:30} {i['why'][:88]}")
    dry = True
    if a.apply and first_run:
        print("\nFIRST RUN — reporting only. Re-run with --apply to act.")
        FIRST_RUN_MARKER.write_text(f"first sweep {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    elif a.apply:
        dry = False
    res = apply_safe(f, dry=dry)
    if res["adopted"]:
        print(f"\n{'WOULD ADOPT' if dry else 'ADOPTED'}: "
              + ", ".join(f"{d['hook']}@{d['event']}" for d in res["adopted"]))
    for s in res["skipped"]:
        print(f"  skipped: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
