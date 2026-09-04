#!/usr/bin/env python3
"""verify-brain-synced.py — mint .brain-synced-<sid> ONLY from recomputed evidence.

Why this exists (2026-07-25)
----------------------------
`.brain-synced-this-session` used to be a bare `touch` issued by the model inside the /close-core
markdown flow, after it had judged its own prior steps successful. Unlike `.reconcile-ran` — which a
SubagentStop hook mints and the model cannot forge — nothing could contradict that claim. On
2026-07-24 the close reported "everything's certified current" while its own log recorded
`NO sync marker`; the two disagreed and no mechanism noticed.

This script replaces the touch. It does not accept any assertion about what happened; it re-runs the
detectors and mints the marker only if all of them independently agree the brain is current.

Checks (ALL must pass — fail-closed, exactly like the marker gate it replaces):
  1. extraction   `extract-pending.sh --phase close` reports EXTRACT-STATUS: none-pending.
                  `skipped (...)` and `detect-error` are NOT passes — they mean "couldn't check",
                  which is the state that must never mint a marker.
  2. capture      no vault .md newer than its ledger capture (i.e. discover/capture_worker drained).
  3. embed        zero NULL embeddings on entities+evidence for this org.
  4. status       brain_status.py verdict is READY.

Usage:  verify-brain-synced.py --session-id <sid> [--mint]
        exit 0 = all checks pass (marker minted with --mint); exit 1 = not synced.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import pathlib
import sys
from pathlib import Path

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[1])
BRAIN = Path(os.environ.get("CORE_BRAIN") or (INSTANCE.parent / "core-brain"))
STATE = INSTANCE / ".claude" / "state"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))


def check_extraction() -> None:
    script = INSTANCE / "scheduling" / "graphify-brain" / "extract-pending.sh"
    if not script.is_file():
        check("extraction", False, "extract-pending.sh missing — cannot verify")
        return
    env = {**os.environ, "CORE_INSTANCE": str(INSTANCE), "CORE_BRAIN": str(BRAIN)}
    try:
        out = subprocess.run(["bash", str(script), "--phase", "close"], capture_output=True,
                             text=True, env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("extraction", False, "detection timed out — NOT a pass")
        return
    tail = (out.stdout or "").strip().splitlines()
    last = tail[-1] if tail else ""
    if "none-pending" in last:
        check("extraction", True, "none-pending (all evidence extracted)")
    elif "skipped" in last or "detect-error" in last:
        check("extraction", False, f"could-not-check → NOT synced: {last}")
    else:
        pending = last.split("—")[-1].strip() if "—" in last else last
        check("extraction", False, f"pending evidence remains: {pending[:80]}")


def check_capture() -> None:
    """Check 2. Documented in this file's own header since 2026-07-25; NEVER IMPLEMENTED until
    2026-08-12, when core-finance dosed the gate and found it minting on an undrained worker.

    THE HOLE THIS LEAVES IS THE EXACT ONE THE FILE EXISTS TO CLOSE. The header promises four
    fail-closed checks. `main()` called three. The word "capture" appeared once in 209 lines — in
    the docstring — and `extract-pending.sh` does not absorb it either (no occurrence of capture,
    ledger, or discover). So the gate advertised a detector it did not run.

    And the missing one is not interchangeable with the others. Checks 1, 3 and 4 all key on ROWS:
    pending evidence, NULL embeddings, a status verdict. A transcript that was never captured has
    no row ANYWHERE — not pending, no NULL embedding, invisible to a status verdict. The single
    condition check 2 exists to catch is the single condition the other three structurally cannot
    see. Finance measured the consequence: with an uncaptured file present the gate minted at
    rc=0, and session-lifecycle.sh:389 reads that marker as proof the close ran capture+embed and
    stands the nightly down — reproducing the 2026-07-24 "certified current while its own log said
    NO sync marker" failure this file was written to make impossible.

    IMPLEMENTED AGAINST THE REAL LEDGER, not against mtimes. `stage_jobs` is where discover.py
    enqueues capture work and capture_worker.py completes it, so "the worker drained" is a fact the
    ledger already holds. Two conditions, because they fail differently:

      • a capture job not in `done`  — enqueued and not finished (pending/retry_wait/leased), or
        permanently failed (dead). Either way the transcript is not in the brain.
      • a transcript on disk with no `sources` row — never discovered at all. This is the invisible
        case above, and it is why the check cannot be a job-queue query alone.

    REUSES discover.py's OWN `_is_queue_operation` predicate rather than reimplementing it. Queue-op
    files are deliberately never registered (discover.py:82), so a reimplementation that drifted
    would report them as uncaptured forever — and a fail-closed gate that cries wolf gets disabled,
    which is worse than the hole it replaced. Importing the shipped predicate is also the lesson
    from the fitness floor earlier today: a copy of the rule stops being the rule.

    Ordering is safe: session-lifecycle.sh runs discover.py then capture_worker.py (:494-498)
    BEFORE minting, so the in-flight session is registered and drained by the time this runs. If it
    is not drained, refusing is the correct and intended outcome — that is the bug, not a false
    alarm.
    """
    sys.path.insert(0, str(INSTANCE / "scheduling" / "brain-pg"))
    try:
        from _env import connect_corebrain, get_org_id  # type: ignore
        import discover as _disc  # type: ignore
    except Exception as e:
        check("capture", False, f"cannot load ledger/discover → NOT synced: {e}")
        return

    try:
        conn = connect_corebrain()
        org = get_org_id()
        cur = conn.cursor()
        cur.execute("""SELECT status, count(*) FROM stage_jobs
                       WHERE org_id=%s AND stage='captured' AND status <> 'done'
                       GROUP BY status ORDER BY status""", (org,))
        undone = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("""SELECT source_key FROM sources
                       WHERE org_id=%s AND source_kind='session_jsonl'""", (org,))
        known = {row[0] for row in cur.fetchall()}
        conn.close()
    except Exception as e:
        check("capture", False, f"ledger query failed → NOT synced: {e}")
        return

    # SystemExit, not Exception: discover.transcript_dir() raises SystemExit("CORE_INSTANCE
    # required") when the env is bare, and `except Exception` does not catch it — SystemExit
    # derives from BaseException. Uncaught it would kill the gate mid-report instead of failing a
    # named check, so the operator would see a traceback where a verdict belongs. It happens to
    # exit non-zero, which is the safe direction, but "crashes in the right direction" is not the
    # contract this file states. Found while dosing this very check in a shell without
    # CORE_INSTANCE set — the same bare-env condition that silently defeated the peer-msg identity
    # guard earlier today.
    try:
        tdir = _disc.transcript_dir()
    except (Exception, SystemExit) as e:
        check("capture", False, f"cannot resolve transcript dir → NOT synced: {e}")
        return
    if not tdir.is_dir():
        check("capture", False, f"transcript dir not found ({tdir}) — cannot check, so NOT a pass")
        return

    unseen: list[str] = []
    for jsonl in sorted(tdir.glob("*.jsonl")):
        if jsonl.stem in known:
            continue
        try:
            if _disc._is_queue_operation(jsonl.read_bytes()):
                continue  # never registered BY DESIGN — discover.py:82
        except Exception:
            # Unreadable is NOT "fine". It is an uncollected observation, and the whole point of
            # this gate is that "couldn't check" never mints.
            unseen.append(f"{jsonl.name} (unreadable)")
            continue
        unseen.append(jsonl.name)

    ok = not undone and not unseen
    bits = []
    if undone:
        bits.append("capture jobs not done: " + ", ".join(f"{k}={v}" for k, v in undone.items()))
    if unseen:
        bits.append(f"{len(unseen)} transcript(s) never discovered: " + ", ".join(unseen[:3])
                    + ("…" if len(unseen) > 3 else ""))
    check("capture", ok, "; ".join(bits) if bits else "worker drained, all transcripts registered")


def check_db() -> None:
    sys.path.insert(0, str(INSTANCE / "scheduling" / "brain-pg"))
    try:
        from _env import connect_corebrain, get_org_id  # type: ignore
    except Exception as e:  # pragma: no cover
        check("embed", False, f"cannot reach brain-pg env: {e}")
        return
    try:
        conn = connect_corebrain()
        org = get_org_id()
        cur = conn.cursor()
        cur.execute("""SELECT count(*) FROM entities
                       WHERE org_id=%s AND embedding IS NULL AND kind <> 'Source'""", (org,))
        ent_null = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM evidence WHERE org_id=%s AND embedding IS NULL", (org,))
        ev_null = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        check("embed", False, f"DB check failed → NOT synced: {e}")
        return
    ok = (ent_null == 0 and ev_null == 0)
    check("embed", ok, f"{ent_null} entity + {ev_null} evidence NULL embeddings")


def check_status() -> None:
    script = INSTANCE / "scheduling" / "brain-pg" / "brain_status.py"
    if not script.is_file():
        check("status", False, "brain_status.py missing — cannot verify")
        return
    env = {**os.environ, "CORE_INSTANCE": str(INSTANCE), "CORE_BRAIN": str(BRAIN)}
    try:
        out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                             env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("status", False, "brain_status timed out — NOT a pass")
        return
    blob = (out.stdout or "") + (out.stderr or "")
    check("status", "READY" in blob, (blob.strip().splitlines() or ["<no output>"])[0][:100])


def nightly_debt() -> list[str]:
    """Debt the NIGHTLY can actually fix. Returns a list of reasons; empty = nothing to do.

    Nick's 2026-07-24 spec for the 02:00 job: "a nightly job that is just a fall back ... it
    shouldn't be used." It was firing a full `heavy` rebuild every night regardless, which is
    routine heavy work, not a fallback. This is the gate.

    Deliberately EXCLUDES pending graph/assertion extraction: those need a live `Agent()`, so the
    nightly cannot fix them and waking a full rebuild for debt it is structurally incapable of
    draining is exactly the pointless work being removed. That debt is surfaced at SessionStart and
    drained by the next /close-core instead.
    """
    reasons: list[str] = []

    # 0. UNPUSHED BRAIN COMMITS. Added 2026-08-26, and it is the reason Nick kept doing this by
    #    hand.
    #
    #    The brain vault auto-COMMITS fine — update-brain.sh does `git add -A && git commit` on any
    #    dirty tree. The push is in that same script. But this probe is what decides whether the
    #    nightly RUNS that script at all, and it only ever measured graph/embedding staleness. So a
    #    push that failed once, followed by no new brain changes, produced: no graph debt -> gate
    #    closed -> update-brain.sh never invoked -> the retry inside it never reached. Commits just
    #    accumulated. Measured on this machine today: core-brain sat at SIX unpushed commits while
    #    the 02:00 nightly logged "no debt -> no-op" and reported success.
    #
    #    Fixing the retry inside update-brain.sh (which I did this morning) was necessary and NOT
    #    sufficient — a repair inside a script the gate never opens is unreachable code. The gate
    #    has to be able to SEE the condition.
    #
    #    Deliberately reason-only: this reports debt so the nightly runs the chain that contains the
    #    already-authorized, destination-guarded push. It does not push anything itself.
    try:
        brain = pathlib.Path(os.environ.get("CORE_BRAIN") or (pathlib.Path.home() / "AI Projects/core-brain"))
        if (brain / ".git").is_dir():
            r = subprocess.run(["git", "-C", str(brain), "rev-list", "--count", "@{u}..HEAD"],
                               capture_output=True, text=True, timeout=20)
            n = int((r.stdout or "0").strip() or 0) if r.returncode == 0 else 0
            if n > 0:
                reasons.append(f"{n} unpushed brain commit(s) — a prior push failed and never retried")
    except Exception:
        pass

    # 0b. STALE CONTRACT-FITNESS. Added 2026-08-26, same shape as 0 above and found the same way.
    #
    #     measure-contract-fitness.py writes .claude/state/contract-fitness.json, which the core-si
    #     detector reads to decide whether a learned contract is BINDING. It runs ONLY in the
    #     `heavy` branch of run-brain-update.sh (:286). The close runs `fast`. So the only thing
    #     that ever recomputes it is a heavy nightly — and whether a heavy nightly runs is decided
    #     HERE, by a gate that measured graph and embedding staleness and nothing else.
    #
    #     Result: the measurement froze at 2026-08-18 while the detector kept emitting a 🔴 from it
    #     for eight days. The stop-and-plan item read "gate live 70d but correction STILL recurs"
    #     on evidence that predated the gate work being done — an item that cannot clear no matter
    #     what is fixed, because the evidence behind it is not being re-measured.
    #
    #     This is the same defect as the unpushed-brain-commit case directly above: a repair or a
    #     measurement living inside a script the gate never opens is unreachable by construction.
    #     Reason-only, like the rest of this probe — it reports debt so the nightly runs the chain.
    try:
        cf = pathlib.Path(os.environ.get("CORE_INSTANCE") or REPO) / ".claude" / "state" / "contract-fitness.json"
        if not cf.exists():
            reasons.append("contract-fitness.json missing — the BINDING verdicts have never been measured")
        else:
            # AGE FROM `measured_at`, NOT mtime. The file was rewritten 2026-08-20 carrying a
            # measurement taken 2026-08-18, so mtime read two days fresher than the evidence
            # actually was — and it is the EVIDENCE the detector quotes back. Any unrelated
            # rewrite resets mtime while the verdicts inside stay exactly as old as they were.
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            meta = _json.loads(cf.read_text())
            ts = (meta.get("measured_at") or "").replace("Z", "+00:00")
            if not ts:
                # A PRESENT FILE WITH NO TIMESTAMP IS NOT A FRESH FILE. Without this, an absent or
                # malformed `measured_at` raised into the outer `except: pass` and the probe
                # reported no debt — the same silent freeze this block exists to end, one level in.
                # Named by sentinel-code on review rather than found by me. Fails toward reporting.
                reasons.append("contract-fitness.json has no readable `measured_at` — its age, and "
                               "therefore whether core-si's BINDING verdicts are current, is unknown")
                raise ValueError("no measured_at")
            try:
                when = _dt.fromisoformat(ts)
            except ValueError:
                # An UNPARSEABLE stamp is not a fresh one either. Caught separately because the
                # outer handler swallows it into "no debt" — verified by test, after the missing-key
                # fix alone still left this path silent. Same defect, one branch over.
                reasons.append(f"contract-fitness.json `measured_at` is unparseable ({ts[:40]!r}) — "
                               f"core-si's BINDING verdicts cannot be dated")
                raise
            if when.tzinfo is None:
                when = when.replace(tzinfo=_tz.utc)
            age_d = (_dt.now(_tz.utc) - when).total_seconds() / 86400.0
            if age_d > 7:
                reasons.append(f"contract-fitness measured {age_d:.0f}d ago — core-si is emitting "
                               f"BINDING verdicts from evidence nothing has refreshed since")
    except Exception:
        pass

    # 1. A prior chain failed or was skipped on a busy lock — always re-attempt.
    for marker, why in ((".brain-update-failed", "last brain-update chain exited non-zero"),
                        (".brain-update-deferred", "a brain-update was skipped on a busy lock")):
        if (STATE / marker).exists():
            reasons.append(why)

    # 2. Embed debt — deterministic, squarely the nightly's job.
    sys.path.insert(0, str(INSTANCE / "scheduling" / "brain-pg"))
    try:
        from _env import connect_corebrain, get_org_id  # type: ignore
        conn = connect_corebrain()
        org = get_org_id()
        cur = conn.cursor()
        cur.execute("""SELECT count(*) FROM entities
                       WHERE org_id=%s AND embedding IS NULL AND kind <> 'Source'""", (org,))
        n_ent = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM evidence WHERE org_id=%s AND embedding IS NULL", (org,))
        n_ev = cur.fetchone()[0]
        conn.close()
        if n_ent or n_ev:
            reasons.append(f"{n_ent} entity + {n_ev} evidence rows lack embeddings")
    except Exception as e:
        # Fail OPEN: if we cannot tell, run the nightly. A skipped fallback is worse than a
        # redundant one — the whole point of this job is to catch what the close missed.
        reasons.append(f"debt probe could not reach the DB ({e}) — running as a precaution")

    # 3. Merge debt — checkpoints written since the graph was last built.
    graph = BRAIN / "_build" / "output" / "graphify-out" / "graph.json"
    ckpt_dir = BRAIN / "_build" / "output" / "checkpoints"
    try:
        if ckpt_dir.is_dir():
            if not graph.exists():
                reasons.append("graph.json missing")
            else:
                gmt = graph.stat().st_mtime
                newer = sum(1 for p in ckpt_dir.glob("chunk-body-*.json") if p.stat().st_mtime > gmt)
                if newer:
                    reasons.append(f"{newer} checkpoint(s) newer than graph.json")
    except Exception:
        pass

    return reasons


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", default="", help="scopes the marker filename")
    ap.add_argument("--mint", action="store_true", help="write the marker when all checks pass")
    ap.add_argument("--nightly-debt", action="store_true",
                    help="probe only nightly-fixable debt; exit 0 = debt exists (run), 1 = clean (skip)")
    args = ap.parse_args()

    if args.nightly_debt:
        reasons = nightly_debt()
        if reasons:
            print("BRAIN-DEBT: work to do —")
            for r in reasons:
                print(f"  • {r}")
            return 0
        print("BRAIN-DEBT: none — nightly is a fallback, not a routine job; skipping heavy rebuild.")
        return 1

    check_extraction()
    check_capture()   # check 2 — promised in the header since 2026-07-25, wired 2026-08-12
    check_db()
    check_status()

    all_ok = all(ok for _, ok, _ in results)
    print("═══ BRAIN-SYNC VERIFICATION ═══")
    for name, ok, detail in results:
        print(f"  {'✅' if ok else '❌'} {name:11} {detail}")

    if not all_ok:
        print("\n  NOT SYNCED — marker withheld. A close that cannot PROVE sync must not claim it.")
        return 1

    if args.mint:
        sid = args.session_id.strip()
        marker = STATE / (f".brain-synced-{sid}" if sid else ".brain-synced-this-session")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("verified\n")
        print(f"\n  ✅ VERIFIED — minted {marker.name}")
    else:
        print("\n  ✅ VERIFIED (dry run — pass --mint to write the marker)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
