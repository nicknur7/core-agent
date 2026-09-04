#!/usr/bin/env python3
"""hook-intent.py — propose an INTENT RECORD for a gate, from evidence rather than invention.

WHY THIS EXISTS
---------------
A gate can be measured (bin/grade-gate.py) but not JUDGED, because nothing records what it
was built to catch. So the estate sweep can say "this fired 62 times and matched nothing"
and can never say "this is catching the wrong kind of thing" — the distinction that
separates a rule needing a tighter trigger from a rule that has drifted off its purpose
entirely and needs re-deriving.

The generated half of the system already solves this: every si_artifact carries
spec.tests.{positive_ids, negative_ids}, and 24 of 36 carry prior_spec with a revision
number. Hand-written gates carry nothing. Of 28 Python hooks, 11 have even a prose note
about why they exist.

This proposes the missing record, in the same shape the generated artifacts already use, so
one comparison engine can serve both.

WHAT IT WILL NOT DO
-------------------
Invent examples. Positives and negatives come from the real transcript corpus, because the
alternative was measured and found harmful: an invented positive for say-do-gap ("I'll go
ahead and write that file") nearly justified widening the pattern, and that phrasing appears
ZERO times in 4,534 real replies. It was the author's phrasing, not Core's. A gate widened
to satisfy a hypothetical is a gate loosened for nothing.

So every positive is something the gate ACTUALLY caught in real history, and every negative
is a near-miss it correctly let through — text that shares the gate's vocabulary but is not
the thing it guards.

  python3 bin/hook-intent.py <hook>            # propose, print for review
  python3 bin/hook-intent.py --all             # propose for every gradeable gate
  python3 bin/hook-intent.py <hook> --write    # write into bin/hook-registry.json

Proposals are NEVER auto-written without --write. A record asserting what a gate is for is
a claim about design, and the corpus can supply evidence for it but not judgement.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import random
import re
import subprocess
import sys
import os
from pathlib import Path

def _core_root() -> Path:
    """This Core's root — env first, then git, then the file's own location.

    Anchoring on __file__ ALONE resolves the wrong Core the moment a peer runs this file from its
    own seat, which is exactly how core-business's clean enforcement-audit result turned out to be
    a second read of life. Correct in normal use, wrong during cross-Core review — and cross-Core
    review is the operation the fleet's autonomy depends on.
    """
    env = os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        p = Path(env).expanduser()
        if (p / ".claude").is_dir():
            return p.resolve()
    try:
        import subprocess as _sp
        top = _sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, timeout=10).stdout.strip()
        if top:
            return Path(top).resolve()
    except Exception:
        pass
    return Path(__file__).resolve().parents[1]


REPO = _core_root()
HOOKS = REPO / ".claude" / "hooks"
REGISTRY = REPO / "bin" / "hook-registry.json"

# Enough examples to constrain a tune, few enough to stay readable and curatable.
N_POSITIVE = 4
N_NEGATIVE = 4


def _grade_gate():
    """Reuse grade-gate's corpus + loader rather than reimplementing them — one definition of
    'the corpus' and one of 'how a hook is loaded' avoids the two drifting apart."""
    spec = importlib.util.spec_from_file_location("gg", REPO / "bin" / "grade-gate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def origin_from_source(path: Path) -> str:
    """Recover the stated reason from the hook's own docstring, if it has one."""
    try:
        src = path.read_text(errors="ignore")
    except Exception:
        return ""
    m = re.search(r'"""(.*?)"""', src, re.S)
    if not m:
        return ""
    body = m.group(1)
    # The first paragraph after a WHY-style heading is the reason; otherwise the first
    # paragraph that names a date or an incident.
    for pat in (r"WHY[^\n]*\n-+\n(.*?)\n\n", r"(\b(?:Added|Caught|Found|Built)\b[^\n]*20\d\d[^\n]*)"):
        mm = re.search(pat, body, re.S | re.I)
        if mm:
            return " ".join(mm.group(1).split())[:400]
    return " ".join(body.strip().splitlines()[0].split())[:400]


def origin_from_git(name: str) -> str:
    """The commit that introduced the file usually states the incident."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "log", "--diff-filter=A", "--format=%h %ad %s",
             "--date=short", "--", f".claude/hooks/{name}.py"],
            capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        return out[-1].strip() if out else ""
    except Exception:
        return ""


def near_misses(gate, texts: list, caught: set, n: int) -> list:
    """Text the gate did NOT catch but which shares its vocabulary — the hard negatives.

    A random non-match is a useless negative: it constrains nothing, because almost any
    tightening still excludes it. What constrains a tune is text the gate *nearly* fired on.
    Approximated by lexical overlap with what it did catch — cheap, and good enough to
    surface candidates a human then curates.
    """
    if not caught:
        return []
    vocab = {}
    for t in list(caught)[:60]:
        for w in set(re.findall(r"[a-z']{4,}", t.lower())):
            vocab[w] = vocab.get(w, 0) + 1
    keep = {w for w, c in vocab.items() if c >= max(2, len(caught) * 0.15)}
    if not keep:
        return []
    scored = []
    for t in texts:
        if t in caught:
            continue
        words = set(re.findall(r"[a-z']{4,}", t.lower()))
        overlap = len(words & keep)
        if overlap >= 3:
            scored.append((overlap, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:n]]



def _excerpt_that_fires(mod, text: str, width: int = 320) -> str:
    """A positive example must still FIRE the gate on its own. Storing a prefix does not.

    The first version stored text[:200]. Every stored positive then failed to reproduce its
    own catch, because whatever made the gate fire usually sat past character 200 — and
    grade-intent duly reported all nine gates as "rotted" on their first run. Nine
    simultaneous regressions is not a finding, it is a broken measuring instrument.

    So: find a window that actually fires, shortest first, and verify it before returning.
    A positive that does not fire is not evidence of anything.
    """
    det = getattr(mod, "detect", None)
    if det is None:
        return ""
    flat = " ".join(text.split())

    def fires(s: str) -> bool:
        try:
            r = det(s)
            return bool(r[0] if isinstance(r, tuple) else r)
        except Exception:
            return False

    if not fires(flat):
        return ""                       # cannot reproduce even in full — do not store it
    # Slide a window and keep the first that reproduces the catch; fall back to the whole
    # text rather than storing something that does not fire.
    step = max(width // 4, 40)
    for start in range(0, max(len(flat) - width, 0) + 1, step):
        w = flat[start:start + width]
        if fires(w):
            return w
    return flat[:1200]


def propose(name: str) -> dict | None:
    gg = _grade_gate()
    path = HOOKS / f"{name}.py"
    if not path.is_file():
        return None
    saved = sys.stdin
    try:
        sys.stdin = io.StringIO("")          # a hook that reads stdin at import must not hang
        mod = gg.load_hook(name)
    except BaseException:
        return None
    finally:
        sys.stdin = saved
    if mod is None or not hasattr(mod, "detect"):
        return None

    verdicts = gg.live_verdicts(name)
    blocking = name in gg.BLOCK_HOOKS or verdicts.get("block", 0) > 0
    texts = gg.corpus("responses" if blocking else "prompts")
    if not texts:
        return None

    caught = gg.fires(mod, texts)
    random.seed(11)                          # deterministic proposals — two runs agree
    raw_pos = random.sample(caught, min(N_POSITIVE, len(caught))) if caught else []
    positives = [_excerpt_that_fires(mod, t) for t in raw_pos]
    positives = [p for p in positives if p]
    negatives = near_misses(mod, texts, set(caught), N_NEGATIVE)

    return {
        "origin": origin_from_source(path) or "UNKNOWN — write this by hand",
        "origin_ref": origin_from_git(name) or "",
        "guards": "ONE SENTENCE — what behaviour this prevents. WRITE THIS BY HAND.",
        "effect": "block" if blocking else "inject",
        "positives": positives,
        "negatives": [" ".join(t.split())[:200] for t in negatives],
        "_evidence": {
            "corpus": len(texts),
            "caught": len(caught),
            "rate": round(len(caught) / len(texts), 4) if texts else 0,
            "note": "positives are real catches; negatives are real near-misses "
                    "(high lexical overlap, not caught). Neither is invented.",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hook", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write", action="store_true",
                    help="write the proposal into bin/hook-registry.json (never automatic)")
    a = ap.parse_args()

    names = []
    if a.all:
        for p in sorted(HOOKS.glob("*.py")):
            try:
                if re.search(r"^def detect\(", p.read_text(errors="ignore"), re.M):
                    names.append(p.stem)
            except Exception:
                continue
    elif a.hook:
        names = [a.hook]
    else:
        ap.error("give a hook name or --all")

    out = {}
    for n in names:
        pr = propose(n)
        if pr is None:
            print(f"{n}: not gradeable (no detect(), or no corpus)", file=sys.stderr)
            continue
        out[n] = pr
        ev = pr["_evidence"]
        print(f"\n=== {n}  [{pr['effect']}]  caught {ev['caught']}/{ev['corpus']} ({ev['rate']:.1%})")
        print(f"  origin    : {pr['origin'][:120]}")
        print(f"  origin_ref: {pr['origin_ref'][:100]}")
        print(f"  positives : {len(pr['positives'])}   negatives: {len(pr['negatives'])}")
        for t in pr["positives"][:2]:
            print(f"     + {t[:110]}")
        for t in pr["negatives"][:2]:
            print(f"     - {t[:110]}")

    if a.write and out:
        # PURPOSE to the shared registry, EVIDENCE to local state. The examples are verbatim
        # text from this Core's sessions and the registry syncs to every Core and every fork.
        reg = json.loads(REGISTRY.read_text())
        ev_path = REPO / ".claude" / "state" / "hook-intent-evidence.json"
        try:
            ev_doc = json.loads(ev_path.read_text())
        except Exception:
            ev_doc = {"_comment": "LOCAL ONLY — verbatim session text. Never sync.", "gates": {}}
        touched = 0
        for h in reg["hooks"]:
            pr = out.get(h.get("name"))
            if pr and not h.get("intent"):
                h["intent"] = {k: v for k, v in pr.items()
                               if k not in ("_evidence", "positives", "negatives")}
                h["intent"]["evidence"] = ".claude/state/hook-intent-evidence.json (per-Core)"
                ev_doc.setdefault("gates", {})[h["name"]] = {
                    "positives": pr.get("positives", []), "negatives": pr.get("negatives", [])}
                touched += 1
        REGISTRY.write_text(json.dumps(reg, indent=2) + "\n")
        ev_path.parent.mkdir(parents=True, exist_ok=True)
        ev_path.write_text(json.dumps(ev_doc, indent=2) + "\n")
        print(f"\nwrote purpose into {touched} registry entr(ies); examples to local state")
    elif out:
        print(f"\n{len(out)} proposal(s). Re-run with --write to record them.")
        print("Curate 'guards' and any UNKNOWN origin by hand first — the corpus supplies")
        print("evidence, not judgement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
