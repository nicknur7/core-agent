#!/usr/bin/env python3
"""A pipeline worker declares what it is; the detector stops guessing from prose.

WHY THIS EXISTS (2026-08-28). core-business's design, bus #5837, after it measured that THIRTEEN of
the fifteen files its close was about to extract as evidence were pipeline workers — 87% of the
batch was the pipeline reading its own exhaust. Confirmed independently on core-life: 21 transcripts
matched an unregistered opening, every one a Sentinel brief hand-worded that same day.

THE STANDING DEFECT the token replaces: every exhaust mechanism before this matched PROSE the
producer happened to write. extract-pending.sh's own history records the cost — "only the EXTRACTION
producer was ever told its required phrase — the hub-refresh producer was told none, invented three
phrasings, and this regex matched none of them", leaking 22 of 42 pending files for two days,
silently. business hit it from the other side: it hand-wrote three briefs in its own wording and
each became a new unrecognised opening.

And the asymmetry that made it unfixable downstream: only the WRITER Core can register a phrase, so
the Core producing the leak structurally cannot close it. A token is not paraphrasable — you can
reword a sentence by accident, you cannot accidentally reword CORE-PIPELINE-EXHAUST/v1.

THE BOUNDARY THIS FILE GUARDS HARDEST. The marker changes HOW a worker is recognised, never WHICH
producers are excluded. Stamping a Sentinel, census or research brief would delete knowledge and
silently reverse Nick's 2026-07-25 extractor-as-gate decision — C12 measured that 62-89% of subagent
graph nodes are unique and roughly half are genuine signal. extract-pending.sh:145 names those
producers as explicitly still flowing through the gate.
"""
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
CFG = REPO / "scheduling" / "graphify-brain" / "pipeline-exhaust.json"
EXTRACT = REPO / "scheduling" / "graphify-brain" / "extract-pending.sh"

passes: list = []
failures: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    cfg = json.loads(CFG.read_text())
    dm = cfg.get("dispatch_marker") or {}
    tok = dm.get("token") or ""
    src = EXTRACT.read_text()

    check("the marker is defined in the single source of truth",
          bool(tok), "pipeline-exhaust.json has no dispatch_marker.token")
    print(f"\n  token: {tok!r}\n")

    check("the token is not paraphrasable prose",
          bool(tok) and " " not in tok and tok.upper() == tok.replace("/v1", "/v1").upper(),
          f"{tok!r} — a token with spaces or casing drift is just a phrase again")

    # --- the detector consumes it, BEFORE the prose matchers ---------------------------------
    check("the detector reads the token from config, not a literal",
          '(_EX.get("dispatch_marker") or {}).get("token")' in src,
          "a second hardcoded copy is the two-lists-must-agree drift this file's config exists to end")

    mark_pos = src.find("if _MARK and _MARK in head")
    re_pos = src.find("if _WORKER_RE.search(head)")
    check("the marker is checked BEFORE the prose regex",
          mark_pos != -1 and re_pos != -1 and mark_pos < re_pos,
          "declared identity should not sit behind an inference that can miss")

    # --- IT MUST NOT MATCH EVERYTHING WHEN UNSET. This is the dangerous direction ------------
    check("an empty token cannot match every head",
          "_MARK and _MARK in head" in src,
          "`_MARK in head` alone is True for '' — an unreadable config would exclude the ENTIRE "
          "corpus unread, which is the exact failure the fail-toward-extraction rule forbids")

    # --- producers actually emit it ----------------------------------------------------------
    producers = [
        ".claude/commands/refresh-truth.md",
        "scheduling/graphify-brain/auto-pipeline.sh",
        "scheduling/graphify-brain/extract-pending.sh",
        "scheduling/brain-pg/session-start-truth-drift.sh",
    ]
    missing = [p for p in producers if tok not in (REPO / p).read_text()]
    check("every shared brief-dispatching producer mandates the token",
          not missing, f"not mandated in: {missing}")

    # --- and the boundary: no Sentinel/research producer may stamp it ------------------------
    forbidden = []
    for p in (".claude/agents/sentinel.md", ".claude/agents/sentinel-code.md",
              ".claude/agents/close-reconciler.md"):
        f = REPO / p
        if f.is_file() and tok in f.read_text():
            forbidden.append(p)
    check("no Sentinel / close-reconciler spec stamps the token",
          not forbidden,
          f"{forbidden} would be excluded from the evidence pool — C12 measured those carry real "
          f"signal, and excluding them reverses Nick's 2026-07-25 call")

    check("the config states that boundary where a future editor will read it",
          "never be stamped on a Sentinel" in json.dumps(dm),
          "the rule has to live next to the token, or the next producer stamps whatever it likes")

    # --- the older mechanisms survive --------------------------------------------------------
    ph = (cfg.get("opening_phrases") or {}).get("phrases") or []
    sig = (cfg.get("structural_signatures") or {}).get("groups") or []
    check("opening phrases are retained for the back catalogue",
          len(ph) >= 14, f"{len(ph)} phrases — every brief written before the token relies on these")
    check("structural signatures are retained",
          len(sig) >= 5, f"{len(sig)} groups")

    # --- FUNCTIONAL: actually run the classifier, do not just read the source -----------------
    # Every check above reads structure. A file that only reads structure is the vacuous-test
    # pattern this Core hit twice on 2026-08-28 alone — a test asserting a docstring instead of the
    # SQL, and a suite whose functions were never called. So the classifier is reconstructed from
    # the SAME config the detector loads and exercised on real head shapes.
    pats = (cfg.get("opening_phrases") or {}).get("patterns") or []
    wre = re.compile(r'first_message:\s*"(?:' + "|".join([re.escape(x) for x in ph] + pats) + r')')
    sigs = (cfg.get("structural_signatures") or {}).get("groups") or []

    def classify(head, mark=tok):
        if mark and mark in head:
            return "exhaust"
        if wre.search(head):
            return "exhaust"
        low = head.lower()
        return "exhaust" if any(all(t.lower() in low for t in g) for g in sigs) else "evidence"

    check("a REWORDED worker brief carrying the token is caught",
          classify(f'first_message: "wording nobody registered. {tok} go"') == "exhaust",
          "this is the entire point — the leak that escaped every prose matcher")

    check("a registered phrase still works without the token (back catalogue)",
          classify('first_message: "Brain-graph extraction worker — batch 3"') == "exhaust",
          "removing phrase support would re-leak every brief written before the token existed")

    check("a SENTINEL brief is still EVIDENCE, not exhaust",
          classify('first_message: "Review this outward-facing action for core-life: git push"') == "evidence",
          "C12 measured these carry real signal; excluding them reverses Nick's 2026-07-25 call")

    check("one of Nick's own turns is EVIDENCE",
          classify('first_message: "ok so where are we with the loop"') == "evidence",
          "the corpus exists for exactly these")

    check("an EMPTY token classifies nothing as exhaust",
          classify('first_message: "anything at all"', mark="") == "evidence"
          and classify('first_message: "Review this outward-facing action"', mark="") == "evidence",
          "an unreadable config must never exclude the corpus unread")

    # HONEST LIMIT, pinned so nobody reads more into the token than it does: a reworded brief that
    # does NOT carry the token still leaks. The token only helps producers that stamp it. That is
    # why the phrase list and structural signatures are retained rather than replaced.
    check("a reworded brief WITHOUT the token still leaks — the known residual",
          classify('first_message: "wording nobody registered"') == "evidence",
          "if this ever returns exhaust, something started matching by producer type, which is the "
          "filename/producer blacklist Nick's 2026-07-25 decision ruled out")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
