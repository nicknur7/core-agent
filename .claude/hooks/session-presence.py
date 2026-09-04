#!/usr/bin/env python3
"""session-presence.py — UserPromptSubmit hook. Step-away awareness WITHOUT saving.

Nick's model (2026-07-23): "I don't think we need to SAVE when I step away — just make you aware when I
step away and what is current when I come back." Stepping away is idle time INSIDE an open session; there
is no Claude Code 'idle' event, so we detect it on the next prompt: if the gap since the last activity
exceeds STEP_AWAY_SECS, Nick stepped away and is now back. We inject a brief "welcome back — here's what's
current" so Core re-orients to current state instead of continuing from possibly-stale in-context work.

NO save, NO commit, NO brain write — purely an awareness nudge (Nick explicitly does not want step-away
saves; crash-safety is handled by the close path, not here). Fail-open: any error → emit nothing, never block.
Timestamp lives in .claude/state/.last-activity (per-Core, gitignored state).
"""
from __future__ import annotations

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # operator name from identity.json, never hardcoded

import json
import os
import sys
import time
from pathlib import Path

STEP_AWAY_SECS = 45 * 60   # gap that counts as "stepped away and came back"


def _repo() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CORE_INSTANCE")
               or Path(__file__).resolve().parents[2])


def main() -> int:
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("session-presence", "UserPromptSubmit")
    except Exception:
        pass
    # The payload was DRAINED AND DISCARDED here until 2026-08-06 ("unused; presence is time-based"),
    # which was true until the clock supply needed session_id to anchor the session start per
    # conversation. Parsing it is still fail-soft: an unreadable payload leaves an empty dict and
    # everything downstream degrades to what it did before rather than raising.
    payload = {}
    try:
        _raw_in = sys.stdin.read()
        payload = json.loads(_raw_in) if _raw_in.strip() else {}
    except Exception:
        payload = {}
    try:
        repo = _repo()
        state = repo / ".claude" / "state"
        state.mkdir(parents=True, exist_ok=True)
        marker = state / ".last-activity"
        now = int(time.time())
        prev = None
        if marker.exists():
            try:
                prev = int(marker.read_text().strip().split()[0])
            except Exception:
                prev = None
        # stamp current activity FIRST (so a crash here still records the touch)
        marker.write_text(f"{now}\n")

        # ── THE CLOCK (2026-07-30, master plan Phase 1.1) ────────────────────────────────
        #
        # time-claim-gate is right essentially every time it fires, and it has fired 110 times.
        # That is the case the fitness function is sharpest about: being correct is not the same
        # as earning your keep. Every one of those blocks is a response already written, blocked,
        # and then continued — a Stop block is a CONTINUATION, not a regeneration, so both the
        # flawed text and the correction stay in the transcript for Nick to read.
        #
        # The clock is 30 characters. Supplying it makes the error impossible instead of catching
        # it, which is strictly better than any amount of catching: the gate can only ever
        # convert a wrong answer into a wrong answer plus a correction.
        #
        # This goes in session-presence rather than a new hook because there are already SEVEN
        # UserPromptSubmit hooks and this one is the one that is already about elapsed time.
        # Consolidate, do not add a second mechanism beside the first.
        # ── AND THE ELAPSED HALF, which is where the supply stopped short (2026-08-06).
        #
        # Supplying NOW closed exactly one claim class: what time is it. It could not close the
        # other one, because elapsed time is not derivable from a timestamp alone — and DURATION is
        # what the gate kept catching. Measured across this session: 11 of 22 Stop blocks were the
        # time-claim gate, and the exempt logic in time-claim-gate.py says so itself — "what the
        # injected clock CAN source is exactly one thing: what time it is NOW."
        #
        # So the gate was not being stubborn. It was correctly refusing a claim the supply genuinely
        # did not cover. Adding START and WALL costs about 25 more characters and closes the class,
        # which is the same trade the comment above already argued for and then only half-made.
        #
        # WALL is wall-clock first-to-last and INCLUDES IDLE GAPS. Labelled as such, because an
        # unlabelled 25h would be read as 25h of work and that is a worse error than not supplying
        # it at all — this session's real figure is 25h+ across two calendar days with long gaps.
        # SOURCE AND COST, both of which the first attempt got wrong.
        #
        # SOURCE: `.claude/state/.session-start` is NOT authoritative. It is rewritten whenever
        # SessionStart re-fires — including after a compaction — so on this session it read
        # 2026-08-06 00:38 while the real conversation began 2026-08-04 23:09. Supplying that as the
        # session start would have been worse than supplying nothing, which is the exact failure this
        # whole supply idea is meant to avoid. The authority is bin/compute-session-duration.sh,
        # which reads the JSONL transcript and finds the first real user message.
        #
        # COST: that script costs ~160ms, and this hook runs on EVERY prompt. Paying 160ms per turn
        # to supply a constant is the kind of friction Nick explicitly said not to add. But the START
        # is a constant WITHIN a session — only NOW moves — so it is computed once, anchored per
        # session_id, and every later turn derives elapsed locally for free.
        #
        # Keyed on session_id so a new conversation re-anchors automatically rather than inheriting
        # the previous session's start, which is the drift that made .session-start wrong.
        clock = None
        try:
            from datetime import datetime
            clock = datetime.now().astimezone().strftime("⏰ %Y-%m-%d %H:%M %Z (live, this turn)")
            sid = str(payload.get("session_id") or "")[:36] or "unknown"
            anchor_f = repo / ".claude" / "state" / f".session-anchor-{sid}"
            anchor = None
            if anchor_f.exists():
                try:
                    anchor = int(anchor_f.read_text().strip())
                except Exception:
                    anchor = None
            if anchor is None:
                import subprocess
                out = subprocess.run(["bash", str(repo / "bin" / "compute-session-duration.sh")],
                                     capture_output=True, text=True, timeout=8).stdout
                for line in out.splitlines():
                    if line.startswith("START:"):
                        from datetime import datetime as _dt
                        stamp = line.split("START:", 1)[1].strip()
                        # "2026-08-04 23:09 PDT" — drop the zone name and treat as local, which is
                        # what it is; %Z cannot round-trip a name like PDT through strptime.
                        parts = stamp.rsplit(" ", 1)
                        anchor = int(_dt.strptime(parts[0].strip(),
                                                  "%Y-%m-%d %H:%M").timestamp())
                        break
                if anchor:
                    try:
                        anchor_f.write_text(str(anchor))
                    except Exception:
                        pass
            if anchor and 0 < anchor <= now:
                el = now - anchor
                h, mi = el // 3600, (el % 3600) // 60
                wall = f"{h}h{mi:02d}m" if h else f"{mi}m"
                st_s = datetime.fromtimestamp(anchor).astimezone().strftime("%Y-%m-%d %H:%M %Z")
                # Labelled INCLUDES IDLE GAPS deliberately: an unlabelled 26h reads as 26h of work,
                # and that misreading is a worse error than not supplying the number at all.
                clock += (f" · session START {st_s} · WALL {wall} "
                          f"(first-to-last, INCLUDES idle gaps — not time worked)")
        except Exception:
            pass

        # ── PEER DIGEST — supply for the fleet-wide claim class (2026-08-06) ────────────────
        #
        # The top REAL violation the reply-observer found on its first day: 3 unsourced "fleet-wide"
        # claims. It is also the class with the worst track record — twice on 2026-08-05 a peer Core
        # opened its own tree and disproved something life had asserted about the whole fleet, and
        # the cross-core gate that used to catch it fired only AFTER Nick had read the claim.
        #
        # Same trade as the clock: supply the fact and the error becomes impossible. Four
        # `git rev-parse` calls cost ~93ms total, cached for 10 minutes — so the common case is free
        # and the number is never more than ten minutes stale.
        #
        # WHAT IT SUPPLIES IS DELIBERATELY NARROW: each peer's HEAD, its last commit date, and which
        # baseline it last synced. That is exactly enough to make "all Cores have X" checkable and
        # NOT enough to invite reasoning about a peer's contents from here — which is the other half
        # of the same failure, and what stay-scoped.py exists to discourage.
        peers = None
        try:
            import subprocess as _sp
            digest_f = repo / ".claude" / "state" / ".peer-digest"
            # FRESHNESS MUST BE A PROPERTY OF THE CONTENT, NOT OF THE FILE.
            #
            # This keyed on digest_f.stat().st_mtime, and an mtime says when the file was last
            # touched — not when the numbers inside it were computed. Caught live on 2026-08-11: the
            # digest was 407 seconds old by mtime, well inside the 600s window, and reported
            # `business baseline:1a2a69e` while business's own marker file (unchanged since 01:20)
            # said 6f36da2. A forced recompute produced the right answer immediately, so the
            # computation was never wrong — the cache was serving nine-hour-old content behind a
            # seven-minute-old timestamp.
            #
            # That is the supply line the retired cross-core gate was replaced BY. It exists so a
            # Core can answer "is everyone synced" from context, and it confidently told me a peer
            # was two baselines behind when it was current. I read that line every turn.
            #
            # The stamp now travels INSIDE the digest, so touching the file cannot make stale
            # content look current. An unparsable or missing stamp means NOT fresh — the safe
            # direction, since recomputing costs ~93ms and believing a wrong peer state costs a
            # wrong decision about the fleet.
            fresh, _cached = False, None
            if digest_f.exists():
                try:
                    _raw = digest_f.read_text()
                    _first, _, _rest = _raw.partition("\n")
                    if _first.startswith("computed_at="):
                        _age = now - int(_first.split("=", 1)[1].strip())
                        fresh = 0 <= _age < 600
                        _cached = _rest.strip() or None
                except Exception:
                    fresh, _cached = False, None
            if fresh and _cached:
                peers = _cached
            else:
                # PEERS ARE DERIVED, NOT LISTED. The literal here was
                # ("business", "school", "finance", "ops") — life's peer set, in a file that
                # SYNCS TO EVERY CORE. So on business this reported business as its own peer and
                # NEVER SHOWED LIFE, all session, every session. core-business found it with
                # Fable; it is structurally invisible from life's seat because life's peer set is
                # exactly the hardcoded list.
                #
                # Second instance of this class today: template/brain/_build/export.py routed all
                # of ops's exports into life's partition for the same reason — a hand-maintained
                # Core list that drifts. Any sibling directory with .claude/identity.json is a
                # Core; self is excluded by path, not by name.
                _self = repo.resolve()
                _peers = sorted(
                    p.name[len("core-"):] for p in repo.parent.glob("core-*")
                    if p.is_dir() and p.resolve() != _self
                    and (p / ".claude" / "identity.json").is_file())
                bits = []
                for name in _peers:
                    d = repo.parent / f"core-{name}"
                    if not d.is_dir():
                        bits.append(f"{name}:absent")
                        continue
                    try:
                        head = _sp.run(["git", "-C", str(d), "rev-parse", "--short", "HEAD"],
                                       capture_output=True, text=True, timeout=4).stdout.strip() or "?"
                    except Exception:
                        head = "?"
                    bl = "?"
                    try:
                        _ls = (d / ".claude" / "state" / ".last-baseline-sync")
                        if _ls.exists():
                            import re as _re
                            _m = _re.findall(r"baseline=([0-9a-f]{7,8})", _ls.read_text())
                            if _m:
                                bl = _m[-1][:7]
                    except Exception:
                        pass
                    bits.append(f"{name}@{head} baseline:{bl}")
                # SELF-LABEL DERIVED, NOT HARDCODED. This read "this Core is life" as a literal, so on
                # business the supply line asserted business WAS life. core-finance found it (bus #632)
                # and the placement makes it worse than a typo: every PEER name on the line above is
                # derived correctly, and this is the one field that is frozen. The line exists so a Core
                # can answer "do all Cores have X" from context — it was the replacement for the
                # cross-core gate retired the same day — so a wrong self-label corrupts precisely the
                # judgement it was shipped to enable. Same defect as the one the peers keep finding in
                # my prose, compiled into the supply itself.
                _n = repo.name
                # PREFIX-ANCHORED. replace(..., 1) still strips the token wherever it FIRST
                # appears, so a repo named `my-core-x` yields `myx`. Fifth instance of the
                # substring-where-exact habit core-business named; found by sweeping for it
                # rather than waiting to be told about it.
                _self = (_n[len("core-"):] if _n.startswith("core-") else _n) or "unknown"
                peers = f"PEERS (this Core is {_self}; ~10min cache): " + " · ".join(bits)
                try:
                    digest_f.write_text("computed_at=%d\n%s" % (int(now), peers))
                except Exception:
                    pass
        except Exception:
            peers = None
        if peers:
            clock = (clock + "\n" + peers) if clock else peers

        if prev is None or (now - prev) < STEP_AWAY_SECS:
            # No step-away, but the clock still ships — that is the whole point of supplying it.
            if clock:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit", "additionalContext": clock}}))
            return 0

        gap = now - prev
        h, m = gap // 3600, (gap % 3600) // 60
        away = f"{h}h {m}m" if h else f"{m}m"

        # "What is current" — line 1 of current-state (the stamp) + the pickup header if present.
        cur_line = ""
        cs = repo / "memory" / "current-state.md"
        if cs.exists():
            try:
                lines = [l.rstrip() for l in cs.read_text().splitlines() if l.strip()]
                cur_line = lines[0] if lines else ""
            except Exception:
                cur_line = ""

        msg = (f"⏳ STEP-AWAY DETECTED — ~{away} since the last prompt. Nothing was saved while you were away "
               f"(by design). Before continuing, re-orient to what's CURRENT rather than the in-context thread "
               f"from before the gap"
               + (f": {cur_line}" if cur_line else ".")
               + f" If the prior task is stale, confirm the current one with {_U.name()}.")
        if clock:
            msg = f"{clock}\n{msg}"
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                  "additionalContext": msg}}))
    except Exception:
        return 0  # fail-open — never block a prompt
    return 0


if __name__ == "__main__":
    sys.exit(main())
