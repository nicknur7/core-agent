#!/usr/bin/env python3
"""The recall-eval refresh must be WEEKLY-gated, owner-gated, and must never use --ablation.

Three separate ways this job can be wrong, each of which fails quietly and expensively:

1. THE GLOB THROUGH A VARIABLE. Every Core lives under "~/AI Projects/<core>", a path containing a
   space. `NEWEST="$(ls -t $REPORT_GLOB | head -1)"` word-splits that into "/Users/<you>/AI"
   and "Projects/..." and matches nothing, so the staleness gate reports "no benchmark on disk at
   all" while one is sitting in that directory — and a job whose whole purpose is to run at most
   weekly then runs a full Voyage-backed eval EVERY NIGHT. The first cut of this script did exactly
   that. Caught by running it rather than reading it.

2. --ablation. `eval.py --ablation` branches early: it writes .claude/state/.brain-leg-ablation.json
   and returns WITHOUT ever calling write_report(). A refresh using that flag runs for tens of
   minutes, exits 0, and leaves the benchmark exactly as stale as it found it — so the 🟡 never
   clears and the failure looks like success. This happened on 2026-08-25.

3. A NEW SCHEDULER. The operator, 2026-08-25: each Core should only have a bus monitor, nothing else. The
   refresh must ride the existing com.nick.brain-pipeline nightly, never install a cron of its own.

Run: python3 bin/tests/test_recall_eval_refresh_gates.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SCRIPT = ROOT / "bin" / "recall-eval-refresh.sh"
LIFECYCLE = ROOT / ".claude" / "hooks" / "session-lifecycle.sh"

_passed = 0
_failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


check("the refresh script exists and is executable",
      SCRIPT.is_file() and os.access(SCRIPT, os.X_OK))
src = SCRIPT.read_text() if SCRIPT.is_file() else ""

print()
print("=== 2. it must never invoke the branch that skips the report ===")
# CHECK THE INVOCATION, NOT THE FILE. The first cut of this assertion scanned the whole script for
# the substring and failed on the script's own log message and on the comment WARNING about the
# flag — a checker that reads prose about code as code, the same defect this suite caught once
# already in test_binding_gate_lookup_resolves.py. What matters is the argv handed to eval.py.
invocations = [ln for ln in src.splitlines()
               if "eval.py" in ln and not ln.lstrip().startswith("#") and "log " not in ln]
check("the eval.py invocation is present", any("$EVAL_PY" in ln or "eval.py" in ln
                                               for ln in invocations), str(invocations))
check("no invocation passes the leg-ablation flag",
      not any("--ablation" in ln for ln in invocations),
      "that branch returns before write_report(), so the benchmark never refreshes")
check("the invocation passes --eval-set", any("--eval-set" in ln for ln in invocations))

print()
print("=== 3. it must not install a scheduler ===")
for bad in ("crontab", "launchctl load", "launchctl bootstrap", "CronCreate"):
    check(f"does not use {bad!r}", bad not in src)
check("it is wired into the EXISTING nightly instead",
      "recall-eval-refresh.sh" in LIFECYCLE.read_text())

print()
print("=== 1. the staleness gate must survive a space in the Core path ===")
# Build a fake Core whose path contains a space, with a benchmark that is FRESH.
with tempfile.TemporaryDirectory() as td:
    fake = Path(td) / "AI Projects" / "core-spacetest"
    (fake / "tasks" / "research").mkdir(parents=True)
    (fake / "scheduling" / "brain-pg").mkdir(parents=True)
    (fake / ".claude").mkdir(parents=True)
    (fake / ".claude" / "identity.json").write_text('{"recall_eval_owner": true}')
    (fake / "scheduling" / "brain-pg" / "eval.py").write_text("# stub\n")
    (fake / "scheduling" / "brain-pg" / "eval-set-v2.json").write_text("[]")
    report = fake / "tasks" / "research" / "brain-primitives-benchmark-2026-08-25.md"
    report.write_text("# fresh\n")   # mtime = now, i.e. NOT stale

    env = dict(os.environ, CORE_INSTANCE=str(fake))
    log = Path("/tmp") / f"recall-eval-refresh-{fake.name}.log"
    log.unlink(missing_ok=True)
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    text = log.read_text() if log.exists() else ""
    check("exits 0 on a fresh benchmark", r.returncode == 0, f"rc={r.returncode}")
    check("SEES the benchmark despite the space in the path — does not claim 'no benchmark'",
          "no benchmark on disk at all" not in text, text.strip()[-160:])
    check("...and correctly reports it FRESH, so no eval is launched",
          "fresh" in text and "starting eval.py" not in text, text.strip()[-160:])

    # Now age it past the 7-day bar and confirm the gate flips.
    old = report.stat().st_mtime - (9 * 86400)
    os.utime(report, (old, old))
    log.unlink(missing_ok=True)
    subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    text = log.read_text() if log.exists() else ""
    check("a >7d-old benchmark IS detected as stale",
          "stale:" in text, text.strip()[-160:])
    log.unlink(missing_ok=True)

print()
print("=== owner gate: a non-owner seat must do nothing ===")
with tempfile.TemporaryDirectory() as td:
    fake = Path(td) / "AI Projects" / "core-notowner"
    (fake / "tasks" / "research").mkdir(parents=True)
    (fake / "scheduling" / "brain-pg").mkdir(parents=True)
    (fake / ".claude").mkdir(parents=True)
    (fake / ".claude" / "identity.json").write_text('{"recall_eval_owner": false}')
    (fake / "scheduling" / "brain-pg" / "eval.py").write_text("# stub\n")
    (fake / "scheduling" / "brain-pg" / "eval-set-v2.json").write_text("[]")
    env = dict(os.environ, CORE_INSTANCE=str(fake))
    log = Path("/tmp") / f"recall-eval-refresh-{fake.name}.log"
    log.unlink(missing_ok=True)
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    text = log.read_text() if log.exists() else ""
    check("non-owner seat skips", r.returncode == 0 and "not the recall_eval_owner" in text,
          text.strip()[-120:])
    log.unlink(missing_ok=True)

print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
