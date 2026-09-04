#!/usr/bin/env python3
"""The pipeline-exhaust filter, and the agreement between the filter and the briefs.

WHAT THIS GUARDS
----------------
Extraction and hub-refresh subagents write vault transcripts. Those transcripts are the
pipeline's own exhaust, and extracting them is not merely wasteful:

  - extraction workers read the embedded brief and obey it as their own task, and the
    contract's rules land in the brain as Nick's decisions;
  - extraction spawns workers whose transcripts become NEW pending evidence, so "nothing
    pending after close" is unreachable by construction;
  - a hub-refresh transcript indexes as new evidence ON THE HUB IT JUST REFRESHED, so that
    hub re-drifts instantly and the next close refreshes it again.

extract-pending.sh keeps them out by matching the transcript's `first_message` opening
against a phrase list. That only works while the phrase the BRIEF is told to write equals
the phrase the FILTER looks for — and those lived in different files, in prose, with no
check that they agreed.

THE FAILURE THIS TEST EXISTS FOR (2026-07-28)
---------------------------------------------
Only the extraction producer was ever told a required phrase. Step 7.6 of close-core named
none, so every close invented one: "Compiled-truth hub refresh worker. Tools: Read only",
"...Tools: Read, Write only", "You are helping refresh 'compiled truth'". The filter had
been patched on 07-26 with a GUESSED phrase, "Compiled-truth refresh", which matches none of
them — one missing word, "hub". 24 leaked transcripts, 57% of the entire pending backlog,
silent for two days. A separate run the same day opened "Graph-node extraction" instead of
the mandated "Brain-graph extraction worker" and would have leaked 15 more.

Both are the same defect: a string in a directive and a string in a matcher, no test that
they are the same string. So the fix is not a longer regex — it is this file.

    python3 bin/tests/test_pipeline_exhaust_filter.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
CFG = REPO / "scheduling" / "graphify-brain" / "pipeline-exhaust.json"
DETECTOR = REPO / "scheduling" / "graphify-brain" / "extract-pending.sh"
CLOSE = REPO / ".claude" / "commands" / "close-core.md"


def _transcript(first_message: str, body: str = "") -> str:
    """A vault transcript head, in the shape the detector actually reads."""
    return f'---\nfirst_message: "{first_message}"\n---\n\n{body}\n'


def main() -> int:
    fails: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            fails.append(f"{label}{' — ' + detail if detail else ''}")

    cfg = json.loads(CFG.read_text())
    phrases = cfg["opening_phrases"]["phrases"]
    sigs = cfg["structural_signatures"]["groups"]

    # The filter as the detector builds it, so the test cannot pass against a reimplementation
    # that drifted from the shipped one.
    worker_re = re.compile(
        r'first_message:\s*"(?:' + "|".join(re.escape(p) for p in phrases) + r')')

    def excluded(head: str) -> bool:
        if worker_re.search(head):
            return True
        low = head.lower()
        return any(all(t.lower() in low for t in g) for g in sigs)

    print("pipeline-exhaust filter")

    # ---- every configured phrase must actually match a transcript that opens with it ----
    for p in phrases:
        check(excluded(_transcript(p + " Tools: Read only.")),
              f"phrase excludes its own transcript: {p[:44]!r}")

    # ---- the three real leaked phrasings, verbatim from the vault ----
    for real in ("Compiled-truth hub refresh worker. Tools: Read only. Return RAW JSON",
                 "Compiled-truth hub refresh worker. Tools: Read, Write only.  1. Re",
                 "You are helping refresh 'compiled truth' summaries for a personal "):
        check(excluded(_transcript(real)), f"leaked 07-26 phrasing now excluded: {real[:40]!r}")
    check(excluded(_transcript("Graph-node extraction. Allowed tools: Read, Write.")),
          "mis-briefed 07-28 extraction run excluded")

    # ---- the structural backstop catches a brief nobody has written yet ----
    check(excluded(_transcript(
        "Some entirely new wording invented by a future close",
        "read pending-batch-03.txt and write chunk-body-x.json")),
        "unknown wording still caught by pending-batch/chunk-body signature")
    check(excluded(_transcript(
        "Another new wording",
        "process refresh-batch-02.json and rewrite the compiled truth hub")),
        "unknown wording still caught by refresh-batch/compiled-truth signature")

    # ---- and REAL evidence must survive: the filter must not swallow sessions ----
    check(not excluded(_transcript(
        "Review a baseline PUSH from core-life to nicknur7/core-agent.",
        "Inspect the diff for backdoors and credential leaks.")),
        "sentinel-code review transcript NOT excluded (C12: carries real signal)")
    check(not excluded(_transcript(
        "Help me debug the close cycle.",
        "We talked about pending-batch files last week.")),
        "a session merely MENTIONING pipeline paths is not excluded")
    check(not excluded(_transcript(
        "Audit the SI spine.", "Measure si_artifacts rows per org.")),
        "ordinary work transcript not excluded")

    # ---- THE AGREEMENT CHECK: every phrase a directive mandates must be in the config ----
    # This is the one that would have caught 2026-07-28 before it shipped. A directive that
    # tells a brief to open with words the filter does not know is the whole defect.
    #
    # DISCOVERED, NOT LISTED (2026-08-04). This loop used to run over a hardcoded pair —
    # extract-pending.sh and close-core.md — which made it blind to exactly the failure it
    # exists to catch: a NEW producer is invisible to a fixed list, and every producer that has
    # leaked since was one this test never opened. On 08-04 core-ops reported two more leaking
    # producers and life's own filter run surfaced two others; the hub-refresh dispatcher
    # (session-start-truth-drift.sh) had never mandated a phrase at all, and nothing here looked
    # at it. Discovering dispatchers by shape means the next one is covered on the day it is
    # written rather than the day it leaks.
    print("\ndirective/config agreement")
    # Two conditions, both required. Dispatching alone is far too broad — a dozen files merely
    # MENTION subagents (session-start-check.sh, handoff.md, a README) and demanding a phrase of
    # them is noise that trains people to ignore this test. What matters is narrower: a producer
    # whose workers write transcripts into the BRAIN VAULT, because only those come back as
    # pending evidence. So it must also name a brain-producer artifact.
    DISPATCH_RX = re.compile(
        r"Spawn\s+\S+\s+.*?(subagent|Sonnet|Haiku)|subagent brief", re.I | re.S)
    BRAIN_PRODUCER_RX = re.compile(
        r"pending-batch-|refresh-batch-|assert-batch-|compiled_truth_md|resynth-brief|"
        r"chunk-body-|extract-pending", re.I)
    SKIP_PARTS = ("-work", "prev", "tests", "archive", "_archive", "node_modules", ".git")
    # Files that match by shape but do not EMIT a brief. Exemptions carry a reason and live here
    # rather than the covered-set living here: a list of what is covered silently omits the next
    # producer, which is the bug; a list of what is exempt makes each omission a decision someone
    # wrote down. Same inversion as the lint pragma — default-enforce, justify each escape.
    NOT_DISPATCHERS = {
        # The CONTRACT extraction workers read, not a thing that spawns them. It describes briefs
        # in detail, which is exactly why it matches by shape.
        "scheduling/graphify-brain/body-extraction-prompt.md",
        # Documentation of the pipeline.
        "scheduling/brain-pg/README.md",
        # Emits the SessionStart banner; the hub-refresh directive inside it is produced by
        # scheduling/brain-pg/session-start-truth-drift.sh, which IS covered and does mandate.
        ".claude/hooks/session-start-check.sh",
    }
    dispatchers = []
    for base in ("scheduling", ".claude/hooks", ".claude/commands", "bin"):
        root = REPO / base
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix not in (".sh", ".py", ".md") or not p.is_file():
                continue
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            try:
                text = p.read_text()
            except Exception:
                continue
            if str(p.relative_to(REPO)) in NOT_DISPATCHERS:
                continue
            if DISPATCH_RX.search(text) and BRAIN_PRODUCER_RX.search(text):
                dispatchers.append(p)
    check(bool(dispatchers), "dispatcher discovery found at least one producer",
          "the discovery regex matched nothing — it has drifted from how briefs are written")
    for doc in dispatchers:
        label = str(doc.relative_to(REPO))
        text = doc.read_text()
        mandated = set(re.findall(
            r'MUST OPEN with the exact words[^"`]*[`"]([^"`\n]+)[`"]', text))
        for m in mandated:
            check(any(m.strip().startswith(p) or p.startswith(m.strip()) for p in phrases),
                  f"{label} mandates {m[:40]!r} — known to the filter",
                  "add it to pipeline-exhaust.json opening_phrases.phrases")
        check(bool(mandated), f"{label} mandates an opening phrase at all",
              "a producer told no phrase invents one every run — the 07-26 hub-refresh bug")

    # ---- the detector must READ the config, not carry its own copy ----
    src = DETECTOR.read_text()
    check("pipeline-exhaust.json" in src, "detector reads pipeline-exhaust.json")

    print(f"\n{'all pipeline-exhaust checks pass' if not fails else 'FAILED: ' + '; '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
