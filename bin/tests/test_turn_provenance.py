#!/usr/bin/env python3
"""THE MINERS MUST LEARN FROM NICK, NOT FROM THE MACHINES WRITING IN HIS ROLE.

Measured on core-life's real archive, comparing the shipped rule against the one it replaced:

    turns the OLD rule mined as Nick     1,127
    turns the NEW rule mines as Nick       658
    removed                                469   = 41.6% of the corpus was not him

    cron ticks admitted    OLD: 97    NEW: 0

The 469, classified: 331 xml/command payloads, 97 scheduler prompts, 24 hook-feedback, 13
compaction summaries, 3 sdk prompts, and exactly ONE unexplained — the literal text `/compact`,
which carries no preference information. So the allowlist drops nothing of his.

WHY THE OLD RULE FAILED, since it looked careful. It combined `userType == "external"` with a
HOOK_PREFIXES blocklist. `userType` is 'external' on Nick's turns AND on every scheduler prompt,
so it separated nothing. The blocklist was grown by hand after each incident — hook feedback added
2026-06-09, background-agent notices 2026-07-27 — and the scheduler prompts match none of its
entries because they open with ordinary prose. It also missed `<local-command-caveat>`,
`<bash-input>` and `<bash-stdout>` outright.

THE STAKES ARE NOT NOISE, THEY ARE INVERSION. The 97 admitted ticks say "do not stop, do not
summarize, do not wait for Nick" and "a report to Nick is not progress" — imperatives, in his role,
more consistent than anything he typed. Mining method from that corpus teaches the system the
opposite of what he wants, with 97 confirming instances. He said "stop" twice on the night this was
found and both Cores obeyed their crons over him.

BOTH DIRECTIONS ARE ASSERTED. A filter that admits nothing is trivially uncontaminated and would
silently end all learning, so the human turn must still pass — and an archive that cannot be
judged must REFUSE rather than return a clean empty result.

Run: python3 bin/tests/test_turn_provenance.py
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
sys.path.insert(0, str(ROOT / "bin"))
import turn_provenance as prov  # noqa: E402


def rec(text, origin=None, **kw):
    e = {"type": "user", "userType": "external", "message": {"content": text}}
    if origin is not None:
        e["origin"] = origin
    e.update(kw)
    return e


TICK = ("AUTONOMOUS WORK TICK — do not stop, do not summarize, do not wait for Nick.\n"
        "Ending a turn to report progress IS the stall he keeps calling out.")


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== turn provenance: whose words is the system learning from? ===\n")

    print("--- the allowlist ---")
    check("a human-stamped turn IS mined",
          prov.is_human_turn(rec("just fix it", origin={"kind": "human"})))
    check("a scheduler prompt is NOT, despite being in the user role",
          not prov.is_human_turn(rec(TICK, isMeta=True, promptSource="system")))
    check("a task-notification is NOT",
          not prov.is_human_turn(rec("BUS #1051 from business", origin={"kind": "task-notification"})))
    check("an untagged turn is NOT (absence is not permission)",
          not prov.is_human_turn(rec("do the thing")))

    # MULTIMODAL. Nick with a screenshot: content is a LIST of blocks, not a string.
    mm = {"type": "user", "userType": "external", "origin": {"kind": "human"},
          "message": {"content": [{"type": "image", "source": {}},
                                  {"type": "text", "text": "does this look clean to you?"}]}}
    check("a turn with an ATTACHMENT is still Nick",
          prov.is_human_turn(mm),
          "16 of his turns on this seat were silently discarded by an isinstance(c, str) check")
    check("...and its text is recovered for mining",
          "does this look clean" in prov.text_of(mm), prov.text_of(mm)[:60])
    # TWO QUESTIONS, TWO PREDICATES (2026-08-28). This check used to assert
    # `not is_human_turn(image_only)`, which contradicted the live-archive check below demanding
    # that EVERY stamped-human turn be admitted — and the live archive contains exactly this: one
    # image-only turn of Nick's, a screenshot sent with nothing typed. The suite held both
    # assertions and they could not both hold, so one of Nick's real turns was being dropped to
    # satisfy the other. Provenance answers WHO; mineability answers IS THERE ANYTHING TO LEARN.
    _img = {"type": "user", "origin": {"kind": "human"},
            "message": {"content": [{"type": "image", "source": {}}]}}
    check("an image-ONLY turn is still Nick (provenance)",
          prov.is_human_turn(_img),
          "it is stamped human; dropping it here is the B1 defect in the one shape the multimodal "
          "fix did not cover — a screenshot with nothing typed alongside it")
    check("...but it is NOT mineable",
          not prov.is_mineable_turn(_img),
          "nothing to learn from, and admitting it to the corpus would insert empty rows")
    check("a normal typed turn is BOTH",
          prov.is_human_turn(mm) and prov.is_mineable_turn(mm),
          "the split must not narrow the ordinary case")
    check("a tool_result still loses to the tool_result check, stamped or not",
          prov.turn_kind({"type": "user", "origin": {"kind": "human"},
                          "message": {"content": [{"type": "tool_result", "content": "x"}]}})
          == prov.TOOL_RESULT)
    check("a tool_result is not a turn at all",
          prov.turn_kind({"type": "user", "message": {"content": [{"type": "tool_result"}]}})
          == prov.TOOL_RESULT)

    print("\n--- what the OLD rule keyed on, and why neither field works ---")
    # These are the two fields a reasonable person reaches for first. Both are recorded here as
    # measured non-separators so nobody re-derives them.
    check("userType is 'external' on the tick too — separates nothing",
          rec(TICK, isMeta=True)["userType"] == rec("hi", origin={"kind": "human"})["userType"])
    check("a tick that opens with ordinary prose defeats any opening-string blocklist",
          not TICK.lstrip().startswith(("<system-reminder>", "<command-name>", "Stop hook feedback")))

    print("\n--- REFUSAL: an archive that cannot be judged must not report a clean empty ---")
    unstamped = [rec("older export, no origin field"), rec("another")]
    stamped = unstamped + [rec("hi", origin={"kind": "human"})]
    check("archive_has_provenance is False when nothing is stamped",
          not prov.archive_has_provenance(unstamped))
    check("...and True when anything is", prov.archive_has_provenance(stamped))
    check("tool_results alone do NOT count as provenance",
          not prov.archive_has_provenance(
              [{"type": "user", "message": {"content": [{"type": "tool_result"}]}}]),
          "an archive of pure tool output would otherwise read as judgeable")

    print("\n--- THE DOSE: both miners share ONE resolver, on the real archive ---")
    # Two sites decided whose words the system learns from, each with its own copy of the rule.
    # This asserts they now agree, by running the SHIPPED functions rather than a transcription.
    sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
    try:
        import friction_jsonl as fj
    except Exception as e:  # pragma: no cover
        print("  SKIP — friction_jsonl unavailable: %s" % e)
        print("\n=== Results: %d passed, %d failed ===" % (p, f))
        return 1 if f else 0

    check("friction_jsonl.is_external_user admits a human turn",
          fj.is_external_user(rec("just fix it", origin={"kind": "human"})))
    check("...and refuses the scheduler prompt",
          not fj.is_external_user(rec(TICK, isMeta=True, promptSource="system")))

    src = (ROOT / "scheduling" / "claude-si" / "learned-corpus-miner.py").read_text()
    check("learned-corpus-miner delegates to the shared resolver, not its own copy",
          "_prov.is_human_turn" in src and "turn_provenance" in src,
          "a second copy of this rule is how the two sites drift apart again")
    check("...and refuses an archive with no provenance rather than mining zero",
          "REFUSING: no transcript" in src)

    print("\n--- and it holds against the LIVE archive, not just fixtures ---")
    # DERIVED, not pinned — see test_frustration_split.py for the same fix.
    _repo = Path(__file__).resolve().parents[2]
    d = Path.home() / ".claude" / "projects" / str(_repo).replace("/", "-")
    if not d.is_dir():
        print("  SKIP — live transcript dir absent on this Core")
    else:
        ticks_admitted = human = 0
        for fp in list(d.glob("*.jsonl"))[:40]:
            for line in fp.open(errors="ignore"):
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") != "user":
                    continue
                # THE FILTER THAT WAS DELETED, AND WHY IT MATTERED MORE THAN THE BUG.
                #
                # This block used to `continue` on any non-string content. Every multimodal turn —
                # Nick with a screenshot attached — was skipped BEFORE being tallied, so the live
                # check could never observe the exact turns the filter was silently discarding.
                # 16/16 green while is_human_turn() dropped 16 of his turns on this seat.
                # core-business: "that filter line is the thing to delete first — it is what let a
                # broken filter and a green suite coexist."
                #
                # A test that pre-filters its corpus by the same assumption the subject makes cannot
                # falsify that assumption. It is the sweep-over-nothing failure wearing a shape that
                # looks like tidiness.
                c = prov.text_of(e)
                if fj.is_external_user(e):
                    human += 1
                    if "AUTONOMOUS WORK TICK" in c:
                        ticks_admitted += 1
        stamped = adm = 0
        for fp in list(d.glob("*.jsonl"))[:40]:
            for line in fp.open(errors="ignore"):
                try:
                    e2 = json.loads(line)
                except Exception:
                    continue
                if e2.get("type") != "user" or prov.is_tool_result(e2):
                    continue
                o2 = e2.get("origin")
                if isinstance(o2, dict) and o2.get("kind") == "human":
                    stamped += 1
                    adm += bool(prov.is_human_turn(e2))
        check("EVERY stamped-human turn on the live archive is admitted (%d/%d)" % (adm, stamped),
              stamped > 0 and adm == stamped,
              "%d of Nick's own turns are being dropped — the B1 defect" % (stamped - adm))

        check("ZERO scheduler prompts survive the filter on the live archive",
              ticks_admitted == 0, "%d admitted" % ticks_admitted)
        check("...while real turns still come through (the filter is not just 'no')",
              human > 50, "only %d human turns found — an over-tight filter ends learning" % human)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
