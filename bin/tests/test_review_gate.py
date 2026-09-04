#!/usr/bin/env python3
"""The adversarial-review gate must cover the gap and nothing else — and must not block yet.

WHY THIS EXISTS
---------------
`.claude/hooks/adversarial-review-gate.py` is the PreToolUse prevention hook that
`artifact_typer.ORACLE_CATALOG` scoped in its own comment and nobody built:

    This oracle observes at Stop, by which point the push or migration has ALREADY RUN...
    Prevention belongs at PreToolUse, on the command itself, before it executes.

It sits in front of database migrations, so the tests that matter most are the ones proving it
does NOT fire where it shouldn't, and does not block while it is still in shadow.

THE THREE THINGS THAT WOULD MAKE IT HARMFUL, each pinned below:
  1. Blocking while MODE == "shadow" — it would stop real work before Nick ever saw the log.
  2. Firing on read-only invocations (--status / --check / --dry-run) — the same over-firing defect
     found in the blast-radius oracle earlier the same day.
  3. Double-gating what pretooluse-guard already sends to Sentinel — friction with no added safety,
     which is how a guard suite turns into noise people learn to ignore.

Run: python3 bin/tests/test_review_gate.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".claude" / "hooks" / "adversarial-review-gate.py"

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def run(cmd: str, transcript: str = "", env_extra: dict | None = None):
    """Invoke the hook exactly as Claude Code would. Returns (exit_code, log_events)."""
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / ".claude" / "state"
        state.mkdir(parents=True)
        tpath = Path(td) / "t.jsonl"
        tpath.write_text(transcript)
        payload = {"session_id": "gate-test", "transcript_path": str(tpath),
                   "cwd": str(REPO), "hook_event_name": "PreToolUse",
                   "tool_name": "Bash", "tool_input": {"command": cmd}}
        env = dict(os.environ)
        # CLAUDE_PROJECT_DIR redirects the hook's log into the temp dir so the real state file is
        # never polluted by tests — and so each case reads only its own events.
        env["CLAUDE_PROJECT_DIR"] = td
        env.pop("CORE_REVIEW_GATE_OFF", None)
        env.update(env_extra or {})
        # The hook imports from the REAL repo, so point it at the real scheduling/ tree.
        env["PYTHONPATH"] = str(REPO / "scheduling" / "claude-si")
        p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=60)
        log = state / "review-gate-log.jsonl"
        events = []
        if log.is_file():
            for line in log.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        return p.returncode, events, p.stderr


REVIEWED = json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "tool_use", "name": "Agent",
         "input": {"subagent_type": "sentinel-code", "prompt": "review this"}}]}})
NOT_REVIEWED = json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "working on it"}]}})


def main() -> int:
    if not HOOK.is_file():
        print(f"  FAIL  hook missing at {HOOK}")
        return 1

    # ---- 1. SHADOW MUST NOT BLOCK. The single most important assertion in this file.
    rc, ev, _ = run("bash bin/run-migrations.sh", NOT_REVIEWED)
    kinds = [e.get("event") for e in ev]
    check("shadow mode NEVER blocks (exit 0) on an unreviewed migration", rc == 0, f"rc={rc}")
    check("...and records would_block so the log shows what it caught",
          "would_block" in kinds, str(kinds))
    check("...and records an opportunity (the denominator for violations-per-opportunity)",
          "opportunity" in kinds, str(kinds))

    # ---- 2. A REVIEWED turn is clean: no would_block, but still an opportunity.
    rc, ev, _ = run("bash bin/run-migrations.sh", REVIEWED)
    kinds = [e.get("event") for e in ev]
    check("a reviewed turn does not trip the gate", rc == 0 and "would_block" not in kinds,
          f"rc={rc} {kinds}")
    check("...and still logs the opportunity", "opportunity" in kinds, str(kinds))

    # ---- 3. READ-ONLY INVOCATIONS ARE NOT ACTIONS (the over-firing defect, pinned)
    for ro in ("bash bin/run-migrations.sh --status",
               "bash bin/reconcile-hooks.sh",
               "bash bin/estate-sweep.sh --dry-run"):
        rc, ev, _ = run(ro, NOT_REVIEWED)
        kinds = [e.get("event") for e in ev]
        check(f"read-only is not an action: {ro[:44]}",
              rc == 0 and "would_block" not in kinds, f"rc={rc} {kinds}")

    # ---- 4. NO DOUBLE-GATING what Sentinel already covers
    for gated in ('bash bin/sync-to-baseline.sh',
                  'git push origin main',
                  'git -C "/Users/n/AI Projects/core-life" push origin main'):
        rc, ev, _ = run(gated, NOT_REVIEWED)
        kinds = [e.get("event") for e in ev]
        check(f"stands down where Sentinel already gates: {gated[:40]}",
              rc == 0 and "would_block" not in kinds, f"rc={rc} {kinds}")

    # ---- 5. IN-SCOPE ACTIONS ARE ACTUALLY COVERED
    for act in ("bash bin/reconcile-hooks.sh --apply",
                "bash bin/estate-sweep.sh --apply",
                "bash bin/si-unify-cutover.sh"):
        rc, ev, _ = run(act, NOT_REVIEWED)
        kinds = [e.get("event") for e in ev]
        check(f"covers the previously ungated action: {act[:42]}",
              "would_block" in kinds, str(kinds))

    # ---- 6. Irrelevant commands cost nothing and log nothing
    for irrelevant in ("git status", "ls -la", "python3 bin/correction-rate.py --by week"):
        rc, ev, _ = run(irrelevant, NOT_REVIEWED)
        check(f"silent on unrelated command: {irrelevant[:38]}",
              rc == 0 and not ev, f"rc={rc} events={[e.get('event') for e in ev]}")

    # ---- 7. The escape hatch works and is loud
    rc, ev, _ = run("bash bin/run-migrations.sh", NOT_REVIEWED,
                    env_extra={"CORE_REVIEW_GATE_OFF": "1"})
    kinds = [e.get("event") for e in ev]
    check("CORE_REVIEW_GATE_OFF=1 bypasses", rc == 0 and "would_block" not in kinds, str(kinds))
    check("...and the bypass is logged, never silent", "bypassed" in kinds, str(kinds))

    # ---- 8. Fails open on garbage input — a broken guard must not become an outage
    p = subprocess.run([sys.executable, str(HOOK)], input="not json at all",
                       capture_output=True, text=True, timeout=60)
    check("malformed payload fails open (exit 0)", p.returncode == 0, f"rc={p.returncode}")

    # ---- 9. Still in shadow. This guards against flipping MODE without reading the log first.
    src = HOOK.read_text()
    check('MODE is still "shadow" — do not flip without Nick reading the log',
          'MODE = "shadow"' in src)

    # ---- 10. STAND-DOWN IS DECIDED ON COMMAND STRUCTURE, NOT ON SUBSTRING
    # This was `any(tok in cmd for tok in ALREADY_GATED)` over the whole command line, so ANY
    # argument containing "git -c", "git push" or "sync-to-baseline" — a commit message, a note, a
    # grep pattern — waived the review requirement. The file's own docstring says Phase 2 flips MODE
    # to "block" as a ONE-LINE change with nothing else moving, so this collision is one line away
    # from letting a real migration skip a mandatory adversarial review.
    #
    # Seventh instance of SUBSTRING WHERE EXACT IS REQUIRED in three days, and the second one found
    # on a gate rather than a test.
    import importlib.util as _iu
    _s = _iu.spec_from_file_location("arg", str(HOOK))
    _arg = _iu.module_from_spec(_s)
    _s.loader.exec_module(_arg)
    STANDDOWN = [
        ("bash bin/sync-to-baseline.sh", True),
        ("git push origin main", True),
        ("git -c user.name=x commit", True),
        ("git -C /repo push", True),
        # THE FALSE STAND-DOWNS. Each is a real blast-radius action whose ARGUMENTS mention a gated
        # command; every one waived the review before this was tokenised.
        ("bash bin/run-migrations.sh --apply  # note about git push", False),
        ('python3 bin/reconcile-hooks.py --apply --reason "after git -c change"', False),
        ('bash bin/si-unify-cutover.sh --apply --note "replaces git push flow"', False),
        ('grep -rn "sync-to-baseline" docs/', False),
        ("bash bin/estate-sweep.sh --apply", False),
    ]
    wrong = [c for c, want in STANDDOWN if _arg._already_gated(c) != want]
    check("stand-down matches the command HEAD, not any substring of its arguments",
          not wrong, "misjudged: %s" % [c[:60] for c in wrong])
    check("...and a trailing shell comment is not part of the command",
          _arg._already_gated("bash bin/run-migrations.sh --apply  # git push later") is False)
    check("an unparseable command does NOT stand down (failing closed means reviewing)",
          _arg._already_gated('bash bin/x.sh --note "unterminated') is False)

    # ---- A TRUNCATED LOG RECORD MUST SAY SO ----------------------------------------------------
    # This gate is shadow-only until "Nick has read the shadow log and agreed", so the log IS the
    # deliverable. Read after 100 records it could not support that decision, and was misleading
    # rather than merely thin: every record stored cmd[:200], and for 6 of 8 would_block rows the
    # deciding text sat past character 200.
    #
    # Worse than missing — replayable to the WRONG answer. A cut mid-string can leave a quote
    # unterminated; shlex then refuses the command; and the read-only exemption is correctly denied
    # to something unparseable. Measured on a real row:
    #
    #     logged (cut at 200)  cd … && wc -l bin/run-migrations.sh && grep -n "declared_tables\|…
    #     _is_blast_radius     True    <- the cut left the quote open
    #     same + closing "     False   <- what the live gate actually saw
    #
    # So replaying the log invents violations. Two wrong conclusions were drawn from those rows
    # before anyone tested them. The fix is not to store more — the full command carries paths and
    # prompt text and this log has no redaction pass — it is to make truncation VISIBLE, so a
    # reader knows a row is unreplayable instead of replaying it and believing the answer.
    short = "bash bin/run-migrations.sh"
    _, ev_s, _ = run(short, NOT_REVIEWED)
    rec_s = next((e for e in ev_s if e.get("event") == "would_block"), None)
    check("a short command is logged as NOT truncated, with its true length",
          bool(rec_s) and rec_s.get("cmd_truncated") is False and rec_s.get("cmd_len") == len(short),
          f"got {rec_s}")
    check("every record carries a hash of the FULL command",
          bool(rec_s) and isinstance(rec_s.get("cmd_sha12"), str) and len(rec_s["cmd_sha12"]) == 12,
          f"no usable cmd_sha12 in {rec_s} — two rows cannot be told apart, and a truncated row "
          "cannot be matched back to the command that produced it")

    pad = ' --note "' + "x" * 400 + '"'
    long_cmd = short + pad
    _, ev_l, _ = run(long_cmd, NOT_REVIEWED)
    rec_l = next((e for e in ev_l if e.get("event") == "would_block"), None)
    check("a long command is FLAGGED truncated and reports the length it really had",
          bool(rec_l) and rec_l.get("cmd_truncated") is True and rec_l.get("cmd_len") == len(long_cmd),
          f"got {rec_l} — without this a reader replays a partial string and trusts the verdict")
    check("...and the stored text really is the cut version, not the whole command",
          bool(rec_l) and len(rec_l.get("cmd", "")) <= 200 and rec_l["cmd_len"] > 200,
          f"stored cmd length {len(rec_l.get('cmd','')) if rec_l else '-'}")
    check("the full command is NOT written to the log",
          bool(rec_l) and "x" * 400 not in json.dumps(rec_l),
          "the untruncated command reached the log — it can carry paths and prompt text, and this "
          "log has no redaction pass")
    check("two different commands get different hashes",
          bool(rec_s) and bool(rec_l) and rec_s.get("cmd_sha12") != rec_l.get("cmd_sha12"),
          "identical hashes for different commands — the field identifies nothing")

    print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
