#!/usr/bin/env python3
"""refresh-hook-dispositions.py — stamp LIVE hook telemetry onto hook-dispositions.json.

The hook-health dashboard reads .claude/state/hook-dispositions.json. Its `disposition`,
`rationale`, and `action` fields are CURATED judgment (kept as-is). But the fire-counts that
used to live only inside the rationale prose went stale — e.g. a "DORMANT, loop broken" line
that was actually fixed 2026-06-23, and "0 fires in 50 sessions" frozen from ~06-05.

This adds/updates a `live` block per hook, computed fresh from .claude/state/hook-events.log:
    "live": {"fires": N, "last_fired": ISO|null, "verdict_mix": {"inject":N,"block":N},
             "refreshed_at": ISO, "events_window": "<first>..<last>"}
so the dashboard can render true numbers and "silent · never fired" can't be a stale claim.

Idempotent, deterministic, no network. Run on demand or at close.
  CORE_INSTANCE=$(git rev-parse --show-toplevel) python3 bin/refresh-hook-dispositions.py
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def repo() -> Path:
    env = os.environ.get("CORE_INSTANCE")
    if env:
        return Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(out)
    except Exception:
        return Path(__file__).resolve().parent.parent


REPO = repo()
DISPO = REPO / ".claude" / "state" / "hook-dispositions.json"
EVENTS = REPO / ".claude" / "state" / "hook-events.log"

def _rotated_text(_p):
    """Delegates to core_paths.read_rotated — the canonical rotation resolver.

    This file carried its own copy of the rotation rule. So did steering-ledger.py and
    refresh-hook-dispositions.py, all three byte-identical, and core_paths.rotated_set was written
    on 2026-08-12 to BE the single resolver for exactly these readers — its docstring names them by
    line number. The consolidation was stated and the copies shipped anyway.

    Verified equivalent before delegating, on a fixture covering the cases the local copy called out:
    numeric suffix ordering (.10 is OLDER than .2, where a lexical sort gets it backwards) and
    non-numeric siblings (.log.gz) ignored rather than guessed at. Same output, same order.

    Same argument as core_paths' own: "four copies of a rotation rule is four places for the next
    generation suffix to be missed".
    """
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    from core_paths import read_rotated as _rr
    return _rr(_P(_p))



def parse_events() -> tuple[dict, str, str]:
    """Return ({hook: {fires, last_fired, verdict_mix}}, first_ts, last_ts)."""
    stats: dict[str, dict] = defaultdict(
        lambda: {"fires": 0, "last_fired": None, "verdict_mix": defaultdict(int),
                 "invocations": 0, "last_invoked": None})
    first_ts = last_ts = None
    if not EVENTS.exists():
        return stats, "", ""
    for line in _rotated_text(EVENTS).splitlines():
        if line.startswith("#") or "|" not in line:
            continue
        fields = [p.strip() for p in line.split("|")]
        ts = fields[0]
        kv = {}
        for f in fields[1:]:
            if "=" in f:
                k, _, v = f.partition("=")
                kv[k.strip()] = v.strip()
        hook = kv.get("hook")
        if not hook:
            continue
        s = stats[hook]
        verdict = kv.get("verdict")
        # verdict=invoke means "this hook RAN", not "this hook matched" (hooklog.invoked, 2026-07-27).
        # Counting it as a fire would erase the exact distinction it was added to make: a hook that
        # runs constantly and never matches vs. one that never runs at all.
        if verdict == "invoke":
            s["invocations"] = s.get("invocations", 0) + 1
            s["last_invoked"] = ts
            if s.get("first_invoked") is None:
                s["first_invoked"] = ts
        else:
            s["fires"] += 1
            s["last_fired"] = ts
            if verdict:
                s["verdict_mix"][verdict] += 1
            # Fires have been recorded since 2026-05-23; invocations only since instrumentation on
            # 2026-07-27. Dividing lifetime fires by a few minutes of invocations produced rates like
            # 717%. fire_rate is therefore computed ONLY over the overlapping window — fires at or
            # after this hook's first recorded invocation.
            if s.get("first_invoked") and ts >= s["first_invoked"]:
                s["fires_in_window"] = s.get("fires_in_window", 0) + 1
        if first_ts is None:
            first_ts = ts
        last_ts = ts
    return stats, first_ts or "", last_ts or ""


# Hooks that must never be proposed for retirement regardless of what telemetry says. Mirrors
# reconcile-hooks.PROTECTED_HOOKS: a security gate that happens to be quiet is still a security gate.
PROTECTED = {"pretooluse-guard", "sentinel-approve", "shared-write-guard"}

# A hook firing on more than this share of its invocations is suspiciously broad. Calibrated against
# live data 2026-07-27: verification-trigger, the busiest legitimate hook, sits far below 1.0 while
# genuinely over-broad generated artifacts approach it.
OVERBROAD_FIRE_RATE = 0.60


def _logs_fires(name: str) -> bool:
    """Does this hook emit a FIRE event at all, or only an invocation?

    Some hooks are instrumented for invocation but have no fire-logging call — recall-satisfied, for
    instance, clears a marker and returns. Without this check they read as 'ran N times, matched 0
    times' and get proposed for tuning, when in truth their matches were simply never recorded.
    Mislabelling here is not cosmetic: the sweep acts on these verdicts.
    """
    for ext in (".py", ".sh"):
        f = repo() / ".claude" / "hooks" / f"{name}{ext}"
        if f.is_file():
            src = f.read_text(errors="ignore")
            src = src.replace("hooklog.invoked", "").replace("hookinvoke.sh", "")
            if "hooklog.log(" in src or "hooklog\n" in src and "log(" in src:
                return True
            if "hook-events.log" in src:
                return True
    return False


def _evidence_verdict(name: str, inv: int, fires: int, entry: dict) -> dict:
    """Derive a disposition FROM EVIDENCE rather than restating the curated one.

    Written 2026-07-27 because all 36 curated dispositions said `keep` — a review that has never
    once said `remove` is not a review. This does NOT overwrite the human `disposition` field; it
    sits beside it as `evidence`, so a disagreement between the two is visible instead of silently
    resolved. Phase 7's sweep consumes this; a human still owns the curated verdict.
    """
    if name in PROTECTED:
        return {"verdict": "keep", "why": "protected — never auto-retired"}
    if inv == 0 and fires == 0:
        return {"verdict": "unmeasured",
                "why": "no invocations and no fires recorded — either not registered, or emits no "
                       "telemetry. Cannot be judged; instrument it before trusting any verdict."}
    if inv == 0 and fires > 0:
        return {"verdict": "keep",
                "why": f"{fires} fires recorded but no invocation signal — fires on a different bus"}
    if fires == 0:
        if not _logs_fires(name):
            return {"verdict": "unmeasured",
                    "why": f"ran {inv}x but emits no FIRE event — matches are not recorded, so "
                           f"'never matched' cannot be distinguished from 'never instrumented'"}
        return {"verdict": "tune",
                "why": f"ran {inv}x and matched 0 times — the trigger is not reaching its case"}
    # rate over the OVERLAPPING window only (see parse_events) — lifetime fires over a few minutes
    # of invocations is not a rate, it is an artefact.
    win = entry.get("_fires_in_window")
    if win is None or inv == 0:
        return {"verdict": "keep",
                "why": f"{fires} fires recorded; no overlapping window yet to compute a rate"}
    rate = win / inv
    if rate > OVERBROAD_FIRE_RATE:
        return {"verdict": "tune",
                "why": f"fires on {rate:.0%} of invocations since instrumentation — likely over-broad"}
    return {"verdict": "keep", "why": f"{win} fires over {inv} invocations ({rate:.0%}) since instrumentation"}


def artifact_stats() -> dict:
    """Fire counts for generated ARTIFACTS, from the friction engine's own log.

    Hooks and artifacts were measured on two different buses, so nothing could rank them on one
    axis — a generated contract firing 19 times and a hand-written hook firing 19 times were not
    comparable. Folding them here is what lets one sweep reason about the whole estate.
    """
    out: dict[str, dict] = {}
    f = REPO / ".claude" / "state" / "friction-action-log.jsonl"
    if not f.is_file():
        return out
    for line in f.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("action") not in ("fire_inject", "fire_block", "shadow_block"):
            continue
        aid = r.get("artifact_id")
        if not aid:
            continue
        e = out.setdefault(aid, {"fires": 0, "sessions": set(), "last_fired": None})
        e["fires"] += 1
        if r.get("session_id"):
            e["sessions"].add(r["session_id"])
        ts = r.get("ts")
        if ts and (e["last_fired"] is None or ts > e["last_fired"]):
            e["last_fired"] = ts
    return {k: {"fires": v["fires"], "distinct_sessions": len(v["sessions"]),
                "last_fired": v["last_fired"]} for k, v in out.items()}


def main() -> int:
    if not DISPO.exists():
        print(f"no dispositions file at {DISPO}")
        return 0
    dispo = json.loads(DISPO.read_text())
    stats, first_ts, last_ts = parse_events()
    now = datetime.now().isoformat(timespec="seconds")
    window = f"{first_ts[:10]}..{last_ts[:10]}" if first_ts and last_ts else ""

    hooks = dispo.get("hooks", {})
    stamped = 0
    for name, entry in hooks.items():
        if not isinstance(entry, dict):
            continue
        s = stats.get(name, {"fires": 0, "last_fired": None, "verdict_mix": {},
                             "invocations": 0, "last_invoked": None})
        inv, fires = s.get("invocations", 0), s["fires"]
        win = s.get("fires_in_window")
        entry["_fires_in_window"] = win
        entry["live"] = {
            "fires": fires,
            "fires_since_instrumentation": win,
            "invocations": inv,
            "fire_rate": round(win / inv, 3) if inv and win is not None else None,
            "last_fired": s["last_fired"],
            "last_invoked": s.get("last_invoked"),
            "verdict_mix": dict(s["verdict_mix"]),
            "refreshed_at": now,
            "events_window": window,
        }
        entry["evidence"] = _evidence_verdict(name, inv, fires, entry)
        entry.pop("_fires_in_window", None)
        stamped += 1

    # refresh the summary block's live totals if present
    summ = dispo.setdefault("summary", {})
    summ["live_total_events"] = sum(s["fires"] for s in stats.values())
    summ["live_events_window"] = window
    summ["live_refreshed_at"] = now
    summ["artifacts"] = artifact_stats()

    # ATOMIC + VALIDATED (2026-07-27). This ran as a bare write_text, so a crash or a full disk
    # mid-write left hook-dispositions.json truncated — and estate-sweep.py reads that exact file
    # moments later in the same close (session-lifecycle.sh runs the refresh, then the sweep), so a
    # torn write would feed the sweep garbage and it would act on it. Same failure class already
    # fixed in reconcile-hooks.py; the safety was ported there but not here.
    # pid-unique: a shared .json.tmp let concurrent refreshes clobber each other's
    # temp file and one could replace the destination with the other's payload.
    tmp = DISPO.with_suffix(f".json.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(dispo, indent=2, ensure_ascii=False))
        json.loads(tmp.read_text())          # prove it re-parses before it can replace anything
        os.replace(tmp, DISPO)
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        print(f"[refresh-hook-dispositions] ABORTED, file untouched ({e})", file=sys.stderr)
        return 1
    # Split 0-fire hooks honestly: infra/heartbeat hooks RUN every session but leave no
    # JSONL-detectable trace (defensive-save, stop-hook, recall-satisfied, sentinel-approve),
    # so 0 is expected — NOT a "silent/broken" signal. Only signal-class 0-fire hooks are dormant.
    zero = [(n, e) for n, e in hooks.items()
            if isinstance(e, dict) and e.get("live", {}).get("fires", 0) == 0]
    infra = sorted(n for n, e in zero if e.get("telemetry_class") == "infra")
    dormant = sorted(n for n, e in zero if e.get("telemetry_class") != "infra")
    print(f"stamped {stamped} hook(s) · {summ['live_total_events']} live events · window {window}")
    if infra:
        print(f"infra/heartbeat (run every session, no JSONL trace — 0 is expected): {', '.join(infra)}")
    if dormant:
        print(f"dormant signal hooks (0 fires, but DO leave a trace when they fire): {', '.join(dormant)}")
    return 0


if __name__ == "__main__":
    # SEAT-LOCKED: REFUSE AN ARGUMENT RATHER THAN DISCARD IT. core-business, bus #971. It aimed a
    # seat-locked scanner at life's tree; Python accepted the stray argv silently, the tool scanned
    # its OWN seat, and the two runs "converged" — which reads as a regressed fix rather than as a
    # tool that never moved. A WRONG NUMBER, not no number. That is the whole defect: not the
    # seat-locking, which is fine, but claiming to honour a tree it discards.
    if len(sys.argv) > 1:
        sys.exit("REFUSING: %s is seat-locked and cannot scan %r.\n"
                 "It resolves its own Core from __file__. To measure another Core, run it from "
                 "that Core's tree or use seat_matrix staging." % (Path(__file__).name, sys.argv[1]))
    raise SystemExit(main())
