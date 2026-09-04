#!/usr/bin/env python3
"""Every registered applier must be shadow-safe, and shadow must mean SHADOW.

The apply path is `in_safe AND trusted AND has_applier`. Since 2026-08-26 trust can be earned by
EVIDENCE — a shadow run of the applier that verifies preconditions and mutates nothing. That makes
the shadow contract load-bearing in a way it was not before: **if an applier mutates under
shadow=True, the loop silently starts acting on fixes it has not yet been trusted with**, using the
very mechanism meant to prove it safe first.

So this pins the contract itself rather than any one applier:

  1. every entry in APPLIERS accepts `shadow=True` (a TypeError here means the loop crashes at the
     moment it tries to earn trust, and fails closed forever without ever saying why)
  2. calling with shadow=True mutates NOTHING — checked by hashing the whole of .claude/state/ and
     the working tree's git status before and after
  3. no applier reaches for an outward verb. Pushing, sending, spending and force-pushing are
     Nick's floor from CLAUDE.md, not a judgment call, and an applier is exactly the place where
     "just one small push" would look locally reasonable.

The third check is deliberately a source scan and not a behavioural one. A behavioural test can
only catch an outward action by PERFORMING it.

Run: python3 bin/tests/test_appliers_are_safe.py
"""
import hashlib
import importlib.util
import inspect
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
CLOSE = ROOT / "bin" / "core-si-close.py"
STATE = ROOT / ".claude" / "state"

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


# Paths under .claude/state/ that OTHER processes legitimately write while this test runs — the
# live session's own hooks, the suite's sibling tests, the statusline. A whole-directory hash is
# state-dependent for that reason: this test passed alone and failed inside run-all.sh, which is
# flakiness in the TEST, not a caught mutation. Diffing per file and excluding known-volatile
# writers keeps the real property (an applier's shadow mutated something) provable while removing
# the false positive.
VOLATILE_PREFIXES = (
    "hook-events", "event-probe", ".session-", ".core-si-", ".si-", "core-si-",
    ".stop-signal-", ".preempt-", ".recall-", ".brain-", ".last-", ".design-swings",
    "friction-action-log", "reply-", ".steering", ".learned-", ".full-close-",
    ".claude-si", ".sync-failures", ".retire-", ".truth-", ".rot-", "oracle-",
)


def _is_volatile(rel: str) -> bool:
    name = rel.split("/")[-1]
    return any(name.startswith(x) for x in VOLATILE_PREFIXES) or rel.endswith(".log")


def _state_snapshot() -> dict:
    """{relative_path: sha256} for every non-volatile file under .claude/state/."""
    out = {}
    for f in sorted(STATE.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(STATE).as_posix()
        if _is_volatile(rel):
            continue
        try:
            out[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
        except Exception:
            out[rel] = "<unreadable>"
    return out


def _git_status() -> str:
    try:
        return subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


spec = importlib.util.spec_from_file_location("si_close", CLOSE)
mod = importlib.util.module_from_spec(spec)
# core-si-close.py runs work only under `if __name__ == "__main__"`; importing is inert.
spec.loader.exec_module(mod)
APPLIERS = mod.APPLIERS

print(f"=== {len(APPLIERS)} registered applier(s): {sorted(APPLIERS)} ===")
check("at least one applier is registered", len(APPLIERS) >= 1)

print()
print("=== 1. every applier accepts shadow= ===")
for key, fn in sorted(APPLIERS.items()):
    sig = inspect.signature(fn)
    check(f"{key} accepts shadow=", "shadow" in sig.parameters, str(sig))

print()
print("=== 2. shadow=True mutates NOTHING (state dir + git status hashed before/after) ===")
before, before_git = _state_snapshot(), _git_status()
results = {}
for key, fn in sorted(APPLIERS.items()):
    try:
        results[key] = fn(shadow=True)
    except TypeError as e:
        results[key] = f"__TYPEERROR__{e}"
    except Exception as e:
        results[key] = f"__RAISED__{e}"
after, after_git = _state_snapshot(), _git_status()

changed = sorted(set(before) ^ set(after)) + sorted(
    k for k in (set(before) & set(after)) if before[k] != after[k])
check("no non-volatile state file changed during the shadow runs", not changed,
      f"changed: {changed[:5]}")
check("the working tree is unchanged", before_git == after_git,
      "a shadow wrote into the repo")
for key, r in sorted(results.items()):
    # None is LEGAL and load-bearing: it means "not applicable, no claim made". Requiring a bool
    # here is what forced appliers to answer False on a quiet close, which wrote evidence_fail and
    # reset the trust streak — the defect that made narrow appliers unable to ever graduate.
    check(f"{key} shadow returned bool|None, not an error",
          r is None or isinstance(r, bool), str(r)[:80])

print()
print("=== 3. no applier reaches for an outward verb (Nick's floor, not a judgment call) ===")
src = CLOSE.read_text()
bodies = []
for key, fn in APPLIERS.items():
    try:
        bodies.append((key, inspect.getsource(fn)))
    except Exception:
        pass
OUTWARD = [
    (r"\bgit\s+push\b", "git push"),
    (r"--force\b", "--force"),
    (r"\bcurl\b", "curl"),
    (r"\bosascript\b", "osascript"),
    # The sync script is handled separately in 3b: it is legitimate to REFERENCE it (the
    # read-only check mode needs no Sentinel token) and illegitimate to INVOKE it without that
    # flag. A line-local negative lookahead cannot tell those apart, because the path is
    # assigned on one line and invoked on another. It failed on exactly that, and the tempting
    # 'fix' was to delete the check rather than sharpen it.
    (r"gmail|send_message|sendmail", "an outward send"),
]
for key, body in sorted(bodies):
    # strip comments/docstrings so prose ABOUT the floor does not read as a violation —
    # the same defect that made two earlier checkers in this suite fail on their own explanations.
    code = re.sub(r'"""[\s\S]*?"""', "", body)
    code = "\n".join(l.split("#", 1)[0] for l in code.splitlines())
    for pat, name in OUTWARD:
        check(f"{key} does not use {name}", re.search(pat, code) is None)

print()
print("=== 3b. an applier touching the sync script must be a CHECKER, never a pusher ===")
FLAG = "--" + "check"
for key, body in sorted(bodies):
    code = re.sub(r'"""[\s\S]*?"""', "", body)
    code = "\n".join(l.split("#", 1)[0] for l in code.splitlines())
    if "sync-to-baseline" not in code:
        continue
    # FUNCTION-scoped, not line-scoped: every subprocess invocation inside a function that touches
    # the sync script must carry the read-only flag somewhere in its argv.
    invocations = re.findall(r"subprocess\.(?:run|Popen)\(\s*\[([^\]]*)\]", code, re.S)
    check(f"{key} touches the sync script and does invoke it", bool(invocations),
          "references it but never calls it")
    for inv in invocations:
        check(f"{key} invocation carries the read-only flag", FLAG in inv, inv.strip()[:90])

print()
print("=== 4. no applier may name a TRUST-ROOT path as a write target ===")
# Until now the trust-root floor rested entirely on prose in auto-safe.txt and on reviewers
# remembering it. These four paths are the recursion guard: an agent that can edit its own gate can
# approve its own future edits. Make it a scan, exactly like check 3.
TRUST_ROOTS = [
    ".claude/hooks/pretooluse-guard.sh",
    "sentinel-approve.sh",
    "sentinel-receipt.sh",
    "bin/sync-manifest.json",
]
for key, body in sorted(bodies):
    code = re.sub(r'"""[\s\S]*?"""', "", body)
    code = "\n".join(l.split("#", 1)[0] for l in code.splitlines())
    for tr in TRUST_ROOTS:
        check(f"{key} does not reference trust root {tr.split('/')[-1]}", tr not in code)

print()
print("=== 5. the tri-state contract is documented where an author will see it ===")
close_src = CLOSE.read_text()
check("core-si-close.py explains True/False/None",
      "None = NOT APPLICABLE" in close_src or "None = not applicable" in close_src.lower(),
      "an applier author who does not know None is legal will return False on a quiet close")

print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
