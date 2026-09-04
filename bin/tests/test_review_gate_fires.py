#!/usr/bin/env python3
"""THE SHADOW GATE MUST BE PROVEN ABLE TO FIRE, not merely present and quiet.

`adversarial-review-gate.py` is the PreToolUse shadow gate over unguarded blast-radius commands
(`run-migrations`, `reconcile-hooks`, `estate-sweep`, `si-unify-cutover`). It logs `opportunity` and
`would_block` and never blocks; Phase 2 flips MODE to "block" as a one-line change. So the day it
starts enforcing, whatever it does now is what it will do then.

WHY THIS FILE EXISTS — a false alarm of my own, worth recording rather than deleting. Its log stopped
at 13:04 on 2026-08-10 while work continued for four more hours, and the file itself had been edited
at 13:01. That reads exactly like an edit that broke a gate, and I nearly reported it as one.

It was correct behaviour. `git push` is NOT in IN_SCOPE — it lives in ALREADY_GATED, which is only
consulted AFTER the in-scope prefilter passes — so the five pushes in those four hours were filtered
out before any logging, silently and rightly. The gate had simply not been handed an in-scope command
since 13:04.

But "silence is correct here" and "this gate can no longer fire" produce IDENTICAL evidence from the
outside, which is core-business's standing point: a detector that has never fired is
indistinguishable from one that cannot. The only way to tell them apart is to hand it a case that
must fire and watch. That is what this does, in both directions, so the next four-hour silence is
answerable in one command instead of an investigation.

ISOLATED. Every case runs against a throwaway CLAUDE_PROJECT_DIR so the real
`.claude/state/review-gate-log.jsonl` is never written — a test that pollutes the artifact it reads
would corrupt the very history someone uses to answer "has this ever fired".

Run: python3 bin/tests/test_review_gate_fires.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
HOOK = ROOT / ".claude" / "hooks" / "adversarial-review-gate.py"


def fire(cmd, env_extra=None):
    """Run the REAL hook against a throwaway project dir; return (exit, log entries)."""
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / ".claude" / "state"
        state.mkdir(parents=True)
        # THE ISOLATION WAS TOO AGGRESSIVE ON ITS FIRST RUN AND CHANGED THE PATH UNDER TEST. A bare
        # temp dir has no scheduling/claude-si, so the hook's `from oracle_adapter import ...` fails
        # and it logs `skipped: import failed` instead of reaching the blast-radius decision at all.
        # That is CORRECT degradation — every skip reason is logged, so failing open is never silent
        # — but it means the test was exercising the dependency-missing branch while claiming to
        # exercise the firing one. Symlinking the real module keeps the LOG isolated (which is the
        # only thing that must not be polluted) while letting the actual decision run.
        sched = Path(td) / "scheduling"
        sched.mkdir(parents=True, exist_ok=True)
        try:
            (sched / "claude-si").symlink_to(ROOT / "scheduling" / "claude-si")
        except OSError:
            pass
        env = dict(os.environ, CLAUDE_PROJECT_DIR=td)
        env.pop("CORE_REVIEW_GATE_OFF", None)
        if env_extra:
            env.update(env_extra)
        r = subprocess.run([sys.executable, str(HOOK)],
                           input=json.dumps({"tool_input": {"command": cmd}}),
                           text=True, capture_output=True, env=env, timeout=120)
        log = state / "review-gate-log.jsonl"
        entries = []
        if log.is_file():
            for ln in log.read_text().splitlines():
                try:
                    entries.append(json.loads(ln))
                except Exception:
                    pass
        return r.returncode, entries


def events(entries):
    return [e.get("event") for e in entries]


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== adversarial-review-gate: proven able to fire ===\n")

    rc, ent = fire("bash bin/reconcile-hooks.sh --apply")
    check("an in-scope blast-radius command IS logged", bool(ent), "no entries written")
    check("...and logs would_block", "would_block" in events(ent), str(events(ent)))
    check("...while still exiting 0 (shadow mode never blocks)", rc == 0, "exit %d" % rc)

    print("\n--- THE CONTROL: silence must be selective, not constant ---")
    # Without this, every PASS above is consistent with a hook that logs everything, and the
    # four-hour silence that prompted this file would still be unexplained.
    rc2, ent2 = fire("git status")
    check("an out-of-scope command writes NOTHING", not ent2, str(events(ent2)))
    check("...and also exits 0", rc2 == 0, "exit %d" % rc2)

    print("\n--- git push is out of scope BY DESIGN, which is what the false alarm turned on ---")
    # ALREADY_GATED is consulted only AFTER the IN_SCOPE prefilter, so a push never reaches it.
    # Pinning this stops the next reader re-deriving "the gate is broken" from its absence.
    rc3, ent3 = fire("git push origin main")
    check("a bare push is filtered before any logging", not ent3, str(events(ent3)))

    print("\n--- the OFF switch must be loud, never silent ---")
    rc4, ent4 = fire("bash bin/reconcile-hooks.sh --apply", {"CORE_REVIEW_GATE_OFF": "1"})
    check("CORE_REVIEW_GATE_OFF=1 still logs that it bypassed", "bypassed" in events(ent4),
          str(events(ent4)))

    print("\n--- and it must not have quietly flipped to blocking ---")
    src = HOOK.read_text()
    check('MODE is still "shadow" (Phase 2 is Nick\'s call, not a drift)',
          'MODE = "shadow"' in src,
          "MODE changed — if Nick approved Phase 2 this check needs updating deliberately")

    print("\n=== stay-scoped.py — the other advisory, and the last hook never proven to fire ===")
    # Same class, same file on purpose. A sweep of hooks wired in settings.json but never seen in
    # hook-events.log left seven candidates; six were explained (they write their own logs, or fire
    # on commands not run recently) and this was the last one with no evidence either way. Silence
    # from an advisory is indistinguishable from an advisory that can no longer speak.
    ss = ROOT / ".claude" / "hooks" / "stay-scoped.py"
    if not ss.is_file():
        print("  SKIP — stay-scoped.py not present on this Core")
    else:
        # ISOLATED STATE, AND MY OWN HAND-RUN IS WHY. The advisory is once-per-(session, peer): it
        # writes a marker keyed on sha1(session_id|peer) and stays silent if that marker exists.
        # Dosing this hook by hand minutes earlier — with no session_id, so the key was
        # sha1("|peer-business") — left that marker in the LIVE state dir, and the first version of
        # this check then ran with the same empty session and was suppressed by its own author's
        # earlier test. It read as "the advisory cannot fire".
        #
        # So: a throwaway CLAUDE_PROJECT_DIR (the hook resolves its state dir from it) AND a unique
        # session per call. Either alone would fix today; both are needed for a test that can be run
        # twice in a row.
        import uuid

        def scoped(tool, td):
            (Path(td) / ".claude" / "state").mkdir(parents=True, exist_ok=True)
            payload = {"tool_name": tool, "tool_input": {}, "session_id": uuid.uuid4().hex}
            r = subprocess.run([sys.executable, str(ss)], input=json.dumps(payload),
                               text=True, capture_output=True, timeout=60,
                               env=dict(os.environ, CLAUDE_PROJECT_DIR=str(td)))
            return (r.stdout or "").strip(), r.returncode

        with tempfile.TemporaryDirectory() as sd:
            out_p, rc_p = scoped("mcp__peer-business__read_file", sd)
            check("a peer-Core MCP read raises the scoping advisory", "Cross-Core read" in out_p,
                  out_p[:200] or "(no output)")
            check("...names the peer it is about", "peer-business" in out_p, out_p[:200])
            check("...and never blocks (exit 0, advisory only)", rc_p == 0, "exit %d" % rc_p)

            out_n, _ = scoped("Read", sd)
            check("an ordinary tool raises NOTHING", not out_n, out_n[:200])

        # PREFIX-ANCHORED, and this is the SUBSTRING-WHERE-EXACT-IS-REQUIRED class that produced
        # seven instances in three days. A server named `x-peer-business` must not match.
            out_l, _ = scoped("mcp__x-peer-business__read", sd)
            check("a look-alike server name does NOT match (prefix-anchored, not substring)",
                  not out_l, out_l[:200])

            # THE SUPPRESSION IS REAL AND MUST BE SHOWN, not assumed: a SECOND read of the same peer
            # in the SAME session is silent by design. Without this the isolation above looks like
            # superstition rather than a property.
            same = uuid.uuid4().hex
            def twice(sess):
                r = subprocess.run([sys.executable, str(ss)],
                                   input=json.dumps({"tool_name": "mcp__peer-school__read",
                                                     "tool_input": {}, "session_id": sess}),
                                   text=True, capture_output=True, timeout=60,
                                   env=dict(os.environ, CLAUDE_PROJECT_DIR=str(sd)))
                return (r.stdout or "").strip()
            first, second = twice(same), twice(same)
            check("first read in a session advises", "Cross-Core read" in first, first[:150])
            check("...second read of the SAME peer in the SAME session is silent (once per pair)",
                  not second, second[:150])

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
