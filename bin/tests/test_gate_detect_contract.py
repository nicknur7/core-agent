#!/usr/bin/env python3
"""test_gate_detect_contract.py — every measurable gate keeps its detect() contract, and detect()
stays the SAME code path the live hook runs.

WHY THIS EXISTS
---------------
On 2026-07-27 nine gates were made measurable by extracting a `detect(text)` function so
bin/grade-gate.py could replay them against the real transcript corpus. That extraction is exactly
the kind of refactor that silently rots: someone tunes the regex inside main(), detect() keeps the
old one, and the grader then reports confident numbers about code that no longer runs. The measured
rate would stay reassuring while the live gate drifted.

So this pins two things:

  1. Each gate still EXPOSES detect() with the documented return shape, and detect() is pure —
     no stdin, no disk writes, no crash on adversarial input.
  2. detect() is the code main() actually uses. Enforced behaviourally: a sentinel string that
     detect() classifies one way must produce the matching live verdict when fed through the hook's
     real stdin interface. If someone re-inlines the matching logic in main(), these diverge.

Run: python3 bin/tests/test_gate_detect_contract.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".claude" / "hooks"

FAILS: list[str] = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
    return bool(cond)


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HOOKS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# (hook, corpus-kind, a string it SHOULD fire on, a string it should NOT)
#
# The positive cases are drawn from what these gates ACTUALLY caught in this Core's transcripts,
# not from invented phrasing. That distinction has teeth: the first draft of this file asserted
# say-do-gap should fire on "I'll go ahead and write that file", it didn't, and the apparent hole
# looked worth widening the pattern for. Measuring first showed the hedged form appears ZERO times
# in 4,534 real responses — the phrasing was the test author's, not Core's. Widening a gate to pass
# a made-up example is how a gate gets loose for nothing. Positives come from the corpus.
GATES = [
    ("state-claim-gate", "responses",
     "The promotion hasn't fired and the registry has no entry for it.",
     "Sure, happy to help with that."),
    ("say-do-gap", "responses",
     "Let me record that in the session log.",
     "That sounds like a reasonable approach overall."),
    ("time-claim-gate", "responses",
     "Good morning — we've been at this for about three hours now.",
     "Here is the summary you asked about."),
    ("financial-figure-gate", "responses",
     "Your checking account balance is $4,231.50 as of this morning.",
     "The weather looks clear for the rest of the week ahead."),
    ("recall-gate", "responses", None, None),
    ("decision-attribution-gate", "responses", None, None),
    ("stop-signal-gate", "prompts",
     "no stop, that is not what I asked you to do at all",
     "please continue with the next step when you get a chance"),
    ("verification-trigger", "prompts",
     "go look at the registry file and check whether it is current",
     "thanks, that all makes sense to me"),
    ("brain-recall-trigger", "prompts",
     "what did we decide about this in a previous session? go check the brain",
     "ok"),
]

ADVERSARIAL = [
    "", "   ", "\x00\x01", "a" * 40000, "```" * 500, '"' * 300,
    " unicode \u202e rtl-override", "?" * 2000, "\n\n\n", "'" + "x" * 199,
]

print("gate detect() contract\n" + "-" * 58)

for name, kind, yes, no in GATES:
    path = HOOKS / f"{name}.py"
    if not check(path.is_file(), f"{name}: missing"):
        continue
    try:
        mod = load(name)
    except Exception as e:
        check(False, f"{name}: import failed — {e}")
        continue

    det = getattr(mod, "detect", None)
    if not check(callable(det), f"{name}: exposes no detect() — grade-gate cannot measure it"):
        continue

    # 1. return shape: truthy/falsy, or (bool, detail)
    def fired(text):
        r = det(text)
        return bool(r[0] if isinstance(r, tuple) else r)

    # 2. purity + robustness — a grader replays this over thousands of strings
    for bad in ADVERSARIAL:
        try:
            det(bad)
        except Exception as e:
            check(False, f"{name}: detect() raised on adversarial input {bad[:16]!r} — {type(e).__name__}")
            break

    # detect() must not write anything (the grader runs it in bulk, outside a session)
    with tempfile.TemporaryDirectory() as td:
        before = set(Path(td).iterdir())
        cwd = os.getcwd()
        try:
            os.chdir(td)
            det(yes or "a routine sentence for purity checking purposes only.")
        except Exception:
            pass
        finally:
            os.chdir(cwd)
        check(set(Path(td).iterdir()) == before, f"{name}: detect() wrote to disk — must be pure")

    # 3. discrimination: fires on the positive, stands down on the negative
    if yes is not None:
        check(fired(yes), f"{name}: detect() missed its own positive case")
    if no is not None:
        check(not fired(no), f"{name}: detect() fired on a benign sentence")

    # 4. the shared mention discriminator is wired — a quoted trigger is being DISCUSSED.
    #    This is the defect that hit two gates on the same reply and is why lib/claimtext.py exists.
    if yes is not None:
        quoted = f'The gate fires on the phrase "{yes}" — that is what the pattern matches.'
        if fired(yes) and fired(quoted):
            print(f"  {name:26} NOTE: still fires when its trigger is quoted")

    print(f"  {name:26} detect() ok")

# ---------------------------------------------------------------------------------------------
# detect() is the SAME path main() runs — behavioural, not textual. A prompt-event hook is fed its
# real stdin payload; the live result must agree with detect(). If main() gets its own inlined copy
# of the matching logic, the grader's numbers stop describing the live gate and this catches it.
# ---------------------------------------------------------------------------------------------
print("\nlive main() agrees with detect()\n" + "-" * 58)

# These hooks write telemetry keyed off CLAUDE_PROJECT_DIR. Pointing that at the real repo would
# append test rows to the live fire log and state files — which is exactly how live hook-events.log
# got clobbered by a test earlier this same session. So the subprocess gets a SANDBOX root: the
# read-side directories (hooks, memory, trigger config) are symlinked in so the gates see real
# trigger data, while .claude/state is a fresh empty directory that absorbs every write.
def _sandbox(td: Path) -> Path:
    root = td / "core"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "state").mkdir()
    for rel in (".claude/hooks", ".claude/rules", "memory", "tasks"):
        src = REPO / rel
        if src.exists():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                dst.symlink_to(src)
    return root


with tempfile.TemporaryDirectory() as _td:
    SANDBOX = _sandbox(Path(_td))

    for name, kind, yes, no in GATES:
        if kind != "prompts" or yes is None:
            continue      # Stop hooks need a transcript file; the prompt hooks are the honest test
        mod = load(name)
        det = getattr(mod, "detect")

        def live(text, _n=name):
            p = subprocess.run(
                [sys.executable, str(HOOKS / f"{_n}.py")],
                input=json.dumps({"prompt": text, "session_id": "detect-contract-test"}),
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "CLAUDE_PROJECT_DIR": str(SANDBOX),
                     "CORE_INSTANCE": str(SANDBOX)})
            return bool(p.stdout.strip()) or p.returncode == 2, p

        for text, expect in ((yes, True), (no, False)):
            r = det(text)
            d_fired = bool(r[0] if isinstance(r, tuple) else r)
            l_fired, proc = live(text)
            # A gate may suppress a genuine detect() hit for stateful reasons (cooldown, brain
            # already queried this session). It must never do the reverse: firing live on something
            # detect() calls clean means main() matches on logic the grader cannot see.
            check(not (l_fired and not d_fired),
                  f"{name}: live main() fired where detect() did not — main() has an unmeasured "
                  f"path (stderr: {proc.stderr.strip()[:120]})")
        print(f"  {name:26} live path consistent with detect()")

    # the sandbox absorbed the writes, and the real state directory was never touched
    check(not (REPO / ".claude" / "state" / ".stop-signal-detect-contract-test.json").exists(),
          "test leaked telemetry into the live .claude/state — sandbox is not holding")

# ---------------------------------------------------------------------------------------------
# EVASION CORPUS — every one of these returned CLEAN from a live gate on 2026-07-27, found by an
# adversarial Codex review of the claimtext refactor. Two shared defects produced all of them:
# **bold** was classified as a quotation (so bolding any claim disabled every routed gate), and the
# negation lookbehind scanned across clause boundaries (so "No checks were run, but X" suppressed a
# live assertion of X). Both are fixed in lib/claimtext.py. They are pinned here because a
# suppression hole is invisible in normal use — the gate simply goes quiet, and nothing complains.
# ---------------------------------------------------------------------------------------------
print("\nevasions stay closed\n" + "-" * 58)

EVASIONS = [
    ("state-claim-gate",      "**The hook is broken and the registry has no entry.**"),
    ("say-do-gap",            "**Let me record that in the session log.**"),
    ("time-claim-gate",       "**Good morning — we have been at this for three hours.**"),
    ("financial-figure-gate", "**Your checking account balance is $4,231.50 now.**"),
    ("state-claim-gate",      "No checks were run, but the hook is broken."),
    ("say-do-gap",            "I will not delay, and let me record that in the session log."),
    ("time-claim-gate",       "No source was checked, but we have been at this for three hours."),
    ("financial-figure-gate", "No source was checked, but your checking account balance is $4,231.50 now."),
    # a decoy figure ahead of the real one: searching only the FIRST match dismissed the sentence
    ("financial-figure-gate", "The account is not at $100, but its balance is $50,000 now."),
]
for name, text in EVASIONS:
    r = load(name).detect(text)
    r = r[0] if isinstance(r, tuple) else r
    check(bool(r), f"{name}: EVADED by — {text[:70]}")
print(f"  {len(EVASIONS)} known evasions, all caught")

# The suppressions that claimtext exists for must SURVIVE the fix. Closing the holes by deleting
# the discriminator would pass every check above and reintroduce the original defect: gates firing
# on their own explanatory text.
print("\ngenuine mentions still suppressed\n" + "-" * 58)
MENTIONS = [
    ("state-claim-gate",      'The gate fires on the phrase "the hook is broken" — that is the pattern.'),
    ("state-claim-gate",      "I am not claiming the hook is broken."),
    ("financial-figure-gate", "The fixture `account balance is $4,231.50 now` is test data."),
]
for name, text in MENTIONS:
    r = load(name).detect(text)
    r = r[0] if isinstance(r, tuple) else r
    check(not r, f"{name}: fired on a genuine MENTION — {text[:70]}")
print(f"  {len(MENTIONS)} mentions, all suppressed")

# A broken or partial claimtext on a synced Core must cost suppression, never the whole gate. A
# crashed hook blocks nothing, which would turn a bad deploy into silent loss of enforcement.
print("\nbroken claimtext degrades, never crashes\n" + "-" * 58)
import importlib
_ct = importlib.import_module("claimtext") if "claimtext" in sys.modules else None
sys.path.insert(0, str(HOOKS / "lib"))
import claimtext as _live_ct
_saved = _live_ct._QUOTE_SPAN_RX
try:
    class _Exploding:
        def finditer(self, *a, **k):
            raise RuntimeError("simulated incompatible fleet copy")
    _live_ct._QUOTE_SPAN_RX = _Exploding()
    check(_live_ct.quoted_spans("anything") == [],
          "quoted_spans raised on a broken pattern — must degrade to []")
    check(_live_ct.is_mention("some claim text here", 5, 10) is False,
          "is_mention raised on a broken pattern — a crashed gate blocks nothing")
    print("  degrades to no-suppression without raising")
finally:
    _live_ct._QUOTE_SPAN_RX = _saved

print("\n" + "-" * 58)
if FAILS:
    print(f"{len(FAILS)} FAILURE(S):")
    for f in FAILS:
        print(f"  ✗ {f}")
    sys.exit(1)
print("ALL PASS")
