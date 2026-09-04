#!/usr/bin/env python3
"""grade-gate.py — measure a hook's fire rate against the REAL transcript corpus.

WHY
---
The operator, 2026-07-27: the data exists, so tuning should be possible. That was right, and it
exposed how the previous tuning was done: eleven hand-written examples and three lines of fire log.
That is guesswork wearing the costume of measurement.

Meanwhile the artifact engine has held ITSELF to a corpus bar since day one — a generated contract
must fire on ≤3% of a random sample of real past prompts or it is rejected as over-broad. The
hand-written gates were never held to the same standard, which is precisely why four of them were
found this session firing on character patterns with no notion of subject:

  · art_9e5498bd        fired on the words "specification" + "expectation"
  · approval-gate       read "why you stop" as a redirect (five patches deep)
  · financial-figure-gate  matched "era" inside "generated"
  · recall-gate         fired on "you asked for" about the current turn, then on its own
                        quoted example string while explaining the fix

Same root shape every time: the pattern matched TEXT where it needed to match MEANING. And every
time the evidence was already on disk — nobody was reading it back.

This makes that reading a one-liner. Point it at a hook, get its true fire rate over thousands of
real assistant responses or user prompts, and a sample of what it actually caught.

  python3 bin/grade-gate.py recall-gate
  python3 bin/grade-gate.py recall-gate --against prompts --sample 12
  python3 bin/grade-gate.py recall-gate --compare HEAD~3     # before/after a change

A hook is gradeable if it exposes a `detect(text)` returning something truthy on a match. Hooks
without that contract are reported as ungradeable rather than silently scored zero.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import random
import subprocess
import sys
import types
from pathlib import Path

# The same bar the artifact gate applies to itself (friction_test_gate.OVERBROAD_RATE). A
# hand-written gate has no more right to fire on 1 turn in 20 than a generated one does.
OVERBROAD_RATE = 0.03

# A read-demanding advisory is not judged by frequency (see the long note above fires()). The
# obvious next metric is redundancy — what share of its fires were followed by a read Core was
# going to do anyway — and the first cut of this tool turned that into a NOISY verdict at 75%.
#
# That verdict was withdrawn the same day, because the number means nothing without its floor.
# Measured across this Core's transcripts: 58% of ALL substantive prompts are followed by a
# read/recall turn, hook or no hook. Against that baseline, a 78% figure is not a gate crying
# wolf — it is a gate firing slightly above the ambient rate at which Core reads.
#
# Worse, the measurement is CONFOUNDED at the root: the injection happens BEFORE the turn whose
# tool calls are being scored. When a fire is followed by a read, observational data cannot tell
# whether the read was redundant or whether the reminder is what produced it. Every fire the metric
# scores as "wasted" is equally consistent with "worked." Tuning a hook down on that basis would be
# the same mistake as the flat 3% bar, one layer deeper.
#
# So redundancy is reported as a descriptive figure against its baseline, and no pass/fail verdict
# is drawn from it. Separating cause from correlation here needs an intervention — firing the hook
# on a random half of eligible prompts and comparing read rates — not more reading of the logs.
BASELINE_READ_RATE = 0.58   # measured 2026-07-27 over 462 substantive prompts, corrected pairing


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


def corpus(kind: str = "responses", limit_files: int = 60) -> list:
    """Real text from this Core's own transcripts — assistant responses or user prompts."""
    base = Path.home() / ".claude" / "projects"
    out = []
    want_user = kind == "prompts"
    for d in base.glob(f"*{REPO.name}*"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit_files]:
            try:
                for line in f.read_text(errors="ignore").splitlines():
                    r = json.loads(line)
                    if want_user:
                        if r.get("type") != "user" or r.get("userType") != "external":
                            continue
                        c = (r.get("message") or {}).get("content")
                        t = c if isinstance(c, str) else ""
                    else:
                        if r.get("type") != "assistant":
                            continue
                        c = (r.get("message") or {}).get("content")
                        if not isinstance(c, list):
                            continue
                        t = " ".join(x.get("text", "") for x in c
                                     if isinstance(x, dict) and x.get("type") == "text")
                    t = (t or "").strip()
                    # hook-injected and system text is not the model's or Nick's own words
                    if len(t) > 80 and not t.lstrip().startswith(("<", "[SYSTEM", "Stop hook")):
                        out.append(t)
            except Exception:
                continue
    return out


def load_hook(name: str, ref: str | None = None):
    """Import a hook module, optionally as it existed at a git ref (for before/after)."""
    path = REPO / ".claude" / "hooks" / f"{name}.py"
    if ref:
        src = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:.claude/hooks/{name}.py"],
                             capture_output=True, text=True).stdout
        if not src.strip():
            return None
        m = types.ModuleType(f"{name}@{ref}")
        m.__dict__["__file__"] = str(path)
        exec(compile(src, f"{name}@{ref}", "exec"), m.__dict__)
        return m
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def fires(mod, texts: list) -> list:
    det = getattr(mod, "detect", None)
    if det is None:
        return []
    hit = []
    for t in texts:
        try:
            r = det(t)
            got = r[0] if isinstance(r, tuple) else r
            if got:
                hit.append(t)
        except Exception:
            continue
    return hit


# ---------------------------------------------------------------------------------------------
# WHY THE RAW PATTERN RATE IS NOT THE VERDICT
# ---------------------------------------------------------------------------------------------
# The first version of this tool applied one flat 3% bar to every gate, and that was wrong in two
# separate ways — both caught by running it across the whole estate on 2026-07-27.
#
# 1. BLOCKING GATES HAVE AN ESCAPE HATCH. say-do-gap matches action-claim language, then stands
#    down if the same turn contained a Write/Edit. A corpus replay sees only the text, so it counts
#    every match including the ones the live gate forgives. Measured: say-do-gap's raw pattern rate
#    is 3.8% while its LIVE block rate over the same period is 1.5%. The raw number is an upper
#    bound, not the thing to tune against — tuning the pattern down to hit 3% raw would have
#    weakened a gate that was already inside the bar in practice.
#
# 2. ADVISORY INJECTS ARE NOT BLOCKS. verification-trigger fires on 43.6% of real prompts, because
#    Nick genuinely asks Core to go look at something in nearly half of them. That is the gate
#    working, not over-firing. Same for stop-signal-gate at 15.9% — hand-reading twelve of its
#    catches found twelve real redirects. A block that stops work on 1 turn in 3 is intolerable; a
#    one-line reminder on 1 prompt in 3 is only a problem if the reminder was UNNECESSARY.
#
# So the bar comes from the effect mode, and for injects the question asked is redundancy — did the
# turn that followed already do the thing the reminder asked for? — rather than frequency.

BLOCK_HOOKS = {"say-do-gap", "state-claim-gate", "time-claim-gate", "financial-figure-gate",
               "recall-gate", "decision-attribution-gate", "recall-first-gate", "approval-gate",
               "recall-before-grep"}

HOOK_LOG = REPO / ".claude" / "state" / "hook-events.log"

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



def live_verdicts(hook: str) -> dict:
    """Verdict counts this hook has actually emitted (post-escape-hatch). The pipe-delimited fire
    log is the only record of what the gate really did, as opposed to what its regex matches.

    Counts are LIFETIME. Dividing them by a corpus built from the newest 60 transcript files would
    put a numerator and denominator from different time ranges — and different units, since a Stop
    hook fires once per TURN while the corpus counts assistant records — into the same percentage.
    Codex review 2026-07-27 caught exactly that: the first version printed `blocks / n` and called it
    a block rate. It is not one, so live_rate() below derives the denominator from the same log."""
    out = {}
    try:
        for ln in _rotated_text(HOOK_LOG).splitlines():
            if ln.startswith("#"):
                continue
            f = dict(p.split("=", 1) for p in (x.strip() for x in ln.split("|")[1:]) if "=" in p)
            if f.get("hook") == hook:
                out[f.get("verdict") or "?"] = out.get(f.get("verdict") or "?", 0) + 1
    except Exception:
        pass
    return out


# A live rate needs OPPORTUNITIES in the denominator, and the fire log historically recorded only
# fires. The first attempt at a denominator here took the busiest Stop hook's total record count as
# a proxy for "Stop turns" — but that hook's records are its own BLOCKS, so the ratio came out as
# recall-gate 94/100 = 94% and every gate read OVER-BROAD. A denominator built from the numerator's
# own population is not a measurement.
#
# The honest denominator is `verdict=invoke`: a record written every time the hook RUNS, regardless
# of outcome. That telemetry was only added on 2026-07-27, so it has barely accumulated. Rather than
# substitute a proxy, this reports insufficient-telemetry and falls back to naming the pattern rate
# a ceiling — which is what it is.
MIN_INVOCATIONS_TO_RATE = 40


def live_rate(hook: str, verdicts: dict) -> tuple:
    """(rate, blocks, invocations) or (None, blocks, invocations) when telemetry is too thin."""
    blocks = verdicts.get("block", 0)
    invokes = verdicts.get("invoke", 0)
    # An invocation that ended in a block may be logged as the block only, so opportunities are at
    # least invokes + blocks. Under-counting opportunities biases the rate UP, i.e. toward flagging
    # a gate as over-broad — the safe direction for a number that decides whether to loosen a gate.
    opportunities = invokes + blocks
    if invokes < MIN_INVOCATIONS_TO_RATE:
        return None, blocks, invokes
    return (blocks / opportunities if opportunities else 0.0), blocks, invokes


def redundancy(hook_mod, limit_files: int = 60) -> tuple:
    """For an ADVISORY hook: of the prompts it fires on, how often did the very next assistant turn
    already make the tool call the reminder exists to demand? Those fires are redundant — harmless,
    but pure noise. A high redundancy rate is the real signal that an inject should be narrowed.

    Returns (fired, redundant) or (0, 0) when the corpus can't be paired.
    """
    det = getattr(hook_mod, "detect", None)
    if det is None:
        return 0, 0
    base = Path.home() / ".claude" / "projects"
    fired = redundant = 0
    READ_TOOLS = {"Read", "Grep", "Glob", "Bash", "NotebookRead"}
    for d in base.glob(f"*{REPO.name}*"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit_files]:
            pending = False
            try:
                for line in f.read_text(errors="ignore").splitlines():
                    r = json.loads(line)
                    if r.get("type") == "user" and r.get("userType") == "external":
                        # ANY new external prompt closes the previous window, fired or not. Leaving
                        # it open let a read from three turns later be credited to a prompt that had
                        # already been answered (Codex review 2026-07-27) — which inflates the
                        # redundancy figure with reads that had nothing to do with the fire.
                        pending = False
                        c = (r.get("message") or {}).get("content")
                        t = (c if isinstance(c, str) else "").strip()
                        if len(t) > 80 and not t.lstrip().startswith(("<", "[SYSTEM")):
                            try:
                                got = det(t)
                                got = got[0] if isinstance(got, tuple) else got
                            except Exception:
                                got = None
                            if got:
                                fired += 1
                                pending = True
                    elif r.get("type") == "assistant" and pending:
                        c = (r.get("message") or {}).get("content")
                        if isinstance(c, list):
                            names = {x.get("name") for x in c
                                     if isinstance(x, dict) and x.get("type") == "tool_use"}
                            # Only a TOOL-BEARING record settles the window. One turn is often split
                            # across several assistant records — a sentence of prose, then the tool
                            # call — so closing on the first text-only record would score a genuine
                            # read as "did not read". The next external prompt closes it regardless,
                            # which is what stops the attribution from running past the turn.
                            if names:
                                if names & READ_TOOLS or any(
                                        str(n).startswith(("mcp__core-brain", "mcp__peer-")) for n in names):
                                    redundant += 1
                                pending = False
            except Exception:
                continue
    return fired, redundant


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hook", nargs="?")
    ap.add_argument("--all", action="store_true",
                    help="grade every gradeable hook and write verdicts to .claude/state/")
    ap.add_argument("--against", choices=["responses", "prompts"], default="responses")
    ap.add_argument("--sample", type=int, default=6)
    ap.add_argument("--compare", help="git ref to grade the OLD version against, for before/after")
    a = ap.parse_args()

    texts = corpus(a.against)
    if not texts:
        print("no corpus found — is this Core's transcript directory present?")
        return 1
    mod = load_hook(a.hook)
    if mod is None:
        print(f"no such hook: {a.hook}")
        return 1
    if not hasattr(mod, "detect"):
        print(f"{a.hook} exposes no detect() — not gradeable by this tool "
              f"(report it rather than scoring it zero)")
        return 1

    hit = fires(mod, texts)
    n, k = len(texts), len(hit)
    rate = k / n if n else 0.0
    live = live_verdicts(a.hook)
    blocks = live.get("block", 0)
    blocking = a.hook in BLOCK_HOOKS or blocks > 0

    print(f"gate      : {a.hook}   [{'block' if blocking else 'advisory'}]")
    print(f"corpus    : {n} real {a.against} from this Core's transcripts")
    print(f"pattern   : {k} ({rate:.1%}) of {a.against} match the raw pattern")

    if blocking:
        # The live rate is the one that matters: it is what survived the gate's own escape hatch
        # (a same-turn Write, a same-turn read, a live figure pull). The pattern rate is a ceiling.
        # Denominator is invocation telemetry, not a proxy — see live_rate().
        lr, nblocks, ninvokes = live_rate(a.hook, live)
        if lr is None:
            print(f"live      : {nblocks} blocks, {ninvokes} logged invocations "
                  f"(need {MIN_INVOCATIONS_TO_RATE} to rate)")
            print(f"verdict   : {'over the pattern ceiling' if rate > OVERBROAD_RATE else 'within the pattern ceiling'}"
                  f" — no live rate yet. The pattern figure is an upper bound (the gate stands down "
                  f"when the turn already did the work), so treat it as a watch signal, not a "
                  f"mandate to loosen.")
        else:
            print(f"live      : {nblocks} blocks in {nblocks + ninvokes} invocations ({lr:.1%})"
                  f"   bar: {OVERBROAD_RATE:.0%}")
            print("verdict   : " + ("OVER-BROAD — tune it" if lr > OVERBROAD_RATE
                                    else "within the bar"))
    else:
        # Frequency is not the fault line for an advisory. Redundancy is — but ONLY for an advisory
        # whose advice is "go read/recall before answering." stop-signal-gate's advice is "halt and
        # acknowledge the redirect"; whether the next turn made a tool call says nothing about
        # whether the redirect was real, and scoring it that way rated a gate NOISY on evidence that
        # had no bearing on it. A hook declares ADVICE = "read" to opt in; anything else is reported
        # as unscoreable rather than scored by the wrong yardstick.
        inj = live.get("inject", 0)
        print(f"live      : {inj} injections logged")
        if getattr(mod, "ADVICE", None) != "read":
            print("verdict   : advisory — no redundancy yardstick applies to this hook's advice; "
                  "judge it by whether its catches are real (sample below)")
        else:
            fired, redun = redundancy(mod)
            if fired:
                rr = redun / fired
                lift = rr - BASELINE_READ_RATE
                print(f"followed  : {redun}/{fired} fires ({rr:.0%}) had a read/recall in the next turn "
                      f"— baseline {BASELINE_READ_RATE:.0%}, lift {lift:+.0%}")
                print("verdict   : descriptive only — this figure cannot separate 'redundant reminder' "
                      "from 'reminder that worked' (see BASELINE_READ_RATE note)")
            else:
                print("verdict   : advisory — no paired turns to score against")

    if a.compare:
        old = load_hook(a.hook, a.compare)
        if old is not None and hasattr(old, "detect"):
            ok = len(fires(old, texts))
            print(f"\nat {a.compare}: {ok} ({ok/n:.1%})  →  now {k} ({rate:.1%})  "
                  f"[{ok - k:+d} responses]")

    if hit:
        random.seed(7)   # deterministic sample so two runs are comparable
        print(f"\nsample of what it catches ({min(a.sample, len(hit))} of {k}):")
        for t in random.sample(hit, min(a.sample, len(hit))):
            print(f"  · {' '.join(t.split())[:132]}")
    return 0


# ---------------------------------------------------------------------------------------------
# THE SWEEP MODE — why this exists at all
# ---------------------------------------------------------------------------------------------
# This tool could measure a gate's true fire rate from day one, and NOTHING EVER RAN IT. Grep the
# repo before 2026-07-27 and `grade-gate` appears only inside docstrings of the hooks it grades.
# Every tune this session was a human running it by hand and noticing.
#
# That is the gap between the estate sweep (which runs at every close, automatically, and classifies
# on FIRE COUNTS) and real measurement. A fire count answers "did this ever match". It cannot answer
# "does this fire on one turn in twenty", because it never knows how many chances it had. recall-gate
# sat at 4.8% — one interruption in twenty — and looked identical to a healthy hook in the sweep.
#
# So: grade everything, write the verdicts where the sweep can read them, and run it at close. The
# measurement leg stops depending on somebody remembering.
def sweep() -> int:
    """Grade every hook exposing detect() and write verdicts to .claude/state/gate-grades.json."""
    hooks_dir = REPO / ".claude" / "hooks"
    live = {}
    responses = corpus("responses")
    prompts = corpus("prompts")
    out = {"measured_at": None, "corpus": {"responses": len(responses), "prompts": len(prompts)},
           "bar": OVERBROAD_RATE, "gates": {}}

    for path in sorted(hooks_dir.glob("*.py")):
        name = path.stem
        # load_hook EXECUTES the module. A hook that calls sys.exit() at import — the
        # retired approval-gate stub is literally `import sys; sys.exit(0)` — would
        # otherwise terminate this whole process silently, mid-sweep, with exit 0 and no
        # output. Catching BaseException is deliberate and narrow in effect: the only
        # thing being protected against is a module that ends the interpreter on import.
        # load_hook EXECUTES the module, so a hook can do two things that break a sweep:
        # call sys.exit() at import (the retired approval-gate stub is literally
        # `import sys; sys.exit(0)`), which silently terminates this whole process; or
        # read sys.stdin at import, which blocks forever waiting for input that will
        # never arrive. Both were hit here on 2026-07-27 — the first produced exit 0 with
        # no output, the second hung past two minutes.
        #
        # So: stdin is swapped for an empty stream during import, and BaseException is
        # caught because SystemExit does not inherit from Exception.
        _saved_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("")
            mod = load_hook(name)
        except BaseException:
            continue
        finally:
            sys.stdin = _saved_stdin
        if mod is None or not hasattr(mod, "detect"):
            continue

        verdicts = live_verdicts(name)
        blocking = name in BLOCK_HOOKS or verdicts.get("block", 0) > 0
        texts = responses if blocking else prompts
        if not texts:
            continue
        hit = fires(mod, texts)
        rate = len(hit) / len(texts)
        lr, nblocks, ninvokes = live_rate(name, verdicts)

        # A pattern rate is a CEILING, not a verdict: the gate stands down when the turn already did
        # the work (a same-turn Write, a same-turn read). Only call something over-broad when the
        # live rate says so; otherwise flag it as worth watching and say which number is missing.
        if lr is not None:
            verdict = "over_broad" if lr > OVERBROAD_RATE else "within_bar"
        elif rate > OVERBROAD_RATE:
            verdict = "watch"          # ceiling is high but unproven live — do NOT tune blind
        else:
            verdict = "within_bar"

        out["gates"][name] = {
            "mode": "block" if blocking else "advisory",
            "pattern_rate": round(rate, 4), "pattern_hits": len(hit), "corpus_n": len(texts),
            "live_blocks": nblocks, "live_invocations": ninvokes,
            "live_rate": round(lr, 4) if lr is not None else None,
            "verdict": verdict,
        }

    import datetime
    out["measured_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    dest = REPO / ".claude" / "state" / "gate-grades.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.partial")
    tmp.write_text(json.dumps(out, indent=2) + "\n")
    tmp.replace(dest)

    tally = {}
    for g in out["gates"].values():
        tally[g["verdict"]] = tally.get(g["verdict"], 0) + 1
    print(f"[grade-gate] graded {len(out['gates'])} gate(s): " +
          ", ".join(f"{v}={n}" for v, n in sorted(tally.items())))
    for name, g in sorted(out["gates"].items()):
        if g["verdict"] != "within_bar":
            shown = g["live_rate"] if g["live_rate"] is not None else g["pattern_rate"]
            which = "live" if g["live_rate"] is not None else "pattern"
            print(f"  {g['verdict']:10} {name:28} {which} {shown:.1%}")
    return 0


if __name__ == "__main__":
    if "--all" in sys.argv:
        sys.exit(sweep())
    sys.exit(main())
