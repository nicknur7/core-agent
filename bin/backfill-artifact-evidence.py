#!/usr/bin/env python3
"""backfill-artifact-evidence.py — make already-installed artifacts TUNABLE.

WHY THIS EXISTS
---------------
The self-improvement loop was built, wired and tested end to end, and had never narrowed a
single rule in production. Not because it was broken — because nothing was ELIGIBLE.

Narrowing needs an artifact's own example texts: the positives it must keep catching, and the
negatives it was gated to reject. Those are written by friction_installer.install(), so every
artifact installed BEFORE the evidence store existed carries none. Measured on 2026-07-28:
0 of 26 on core-life, 0 of 5 on core-business. Both Cores armed, neither exercisable.

The operator, on hearing the loop was "armed but not exercised," asked why functionality keeps
being left unfinished — he wants everything implemented and working.

That is right: reporting the gap twice and closing it zero times is not delivery.

HOW IT REGENERATES RATHER THAN INVENTS
--------------------------------------
Each artifact carries the case_id of the correction it was mined from, and friction_cases still
holds that case. So the examples are re-derived by calling the PRODUCTION builder,
friction_router._make_examples — the same function that produced them at install — rather than
by writing plausible-looking text.

That matters for one specific reason: the evidence is what the tuner's safety invariant is
checked against. Every positive must still match after a narrowing, or the narrowing is
refused. Invented positives would make that invariant meaningless in the dangerous direction —
it would pass while protecting nothing. Regenerated positives are the actual corrections.

Channel is preserved ({text, channel}), because an assistant_regex clause evaluated against a
prompt string fabricates evidence the artifact never produced.

  python3 bin/backfill-artifact-evidence.py           # report what WOULD be written
  python3 bin/backfill-artifact-evidence.py --apply   # write it
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
from _env import get_org_id  # noqa: E402  (single org resolver)

DB = os.environ.get("COREBRAIN_DB", "corebrain")


def q(sql: str) -> list:
    r = subprocess.run(["psql", "-d", DB, "-tA", "-F", "\x1f", "-c", sql],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return [ln.split("\x1f") for ln in r.stdout.strip().splitlines() if ln.strip()]


def _as_evidence(examples: list, want: str) -> list:
    """Convert router examples to the evidence shape, preserving the CHANNEL.

    An example knows whether it was a prompt or an assistant text. Collapsing that is how a
    prompt-captured negative ends up evaluated against an assistant_regex clause, manufacturing
    wrong-fire evidence the artifact never produced.
    """
    out = []
    for e in examples:
        hi = e.get("hook_input") or {}
        if (hi.get("prompt") or "").strip():
            out.append({"text": hi["prompt"].strip(), "channel": "prompt"})
        elif (hi.get("assistant_text") or "").strip():
            out.append({"text": hi["assistant_text"].strip(), "channel": "assistant"})
    return out[:want]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; default is report-only")
    ap.add_argument("--org", type=int, default=get_org_id())  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
    a = ap.parse_args()
    org = a.org

    import friction_router as fr
    import friction_installer as inst

    rows = q(f"""SELECT a.artifact_id, a.spec->>'case_id', c.case_json,
                        (a.evidence IS NOT NULL)::text
                 FROM si_artifacts a
                 LEFT JOIN friction_cases c
                        ON c.case_id = a.spec->>'case_id' AND c.org_id = a.org_id
                 WHERE a.active AND a.org_id = {org} AND a.provenance = 'friction'""")

    # Corpus prompts double as the real-neighbour negatives, exactly as at install time.
    try:
        neighbours = inst._fetch_corpus_prompts(org) or []
    except Exception:
        neighbours = []

    have, done, skipped = 0, [], []
    for aid, case_id, case_json, has_ev in rows:
        if has_ev == "true":
            have += 1
            continue
        if not case_json:
            skipped.append((aid, "no case row — cannot regenerate, will not invent"))
            continue
        try:
            case = json.loads(case_json)
        except Exception:
            skipped.append((aid, "case_json unparseable"))
            continue

        moment = case.get("moment", {})
        correction = case.get("user_wanted") or moment.get("correction", "")
        words = fr._distinctive(correction)
        if len(words) < 2:
            skipped.append((aid, "correction has too few distinctive words to rebuild a key"))
            continue

        pos, neg = fr._make_examples(words[:2], correction, neighbours)
        payload = {
            "positive_texts": _as_evidence(pos, 6),
            "negative_texts": _as_evidence(neg, 12),
            "backfilled": True,
        }
        if not payload["positive_texts"]:
            skipped.append((aid, "regenerated no positive — refusing to write evidence with "
                                 "nothing for the invariant to check"))
            continue

        if a.apply:
            ok = inst._write_evidence(aid, payload, org)
            done.append((aid, len(payload["positive_texts"]), len(payload["negative_texts"]),
                         "written" if ok else "WRITE FAILED"))
        else:
            done.append((aid, len(payload["positive_texts"]), len(payload["negative_texts"]),
                         "would write"))

    print(f"org {org}: {len(rows)} friction artifacts, {have} already had evidence")
    for aid, np_, nn, state in done:
        print(f"  {state:12} {aid[:26]:26} +{np_} positives  -{nn} negatives")
    for aid, why in skipped:
        print(f"  {'SKIP':12} {aid[:26]:26} {why}")
    if not a.apply and done:
        print(f"\n{len(done)} artifact(s) would become tunable. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
