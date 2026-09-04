#!/usr/bin/env python3
"""Prompt text must never be evaluated as assistant text, or the reverse.

WHY THIS TEST EXISTS RATHER THAN A COMMENT
------------------------------------------
core-business, finding 7a: friction_installer collapsed each stored example to a bare string,
discarding whether it was a PROMPT or an ASSISTANT text. Both friction_watchdog call sites then
did {"prompt_text": text, "assistant_text": text} — the same string in both channels.

An assistant_regex clause could therefore match a string that was captured as a prompt. That
does not merely produce a wrong answer; it MANUFACTURES the evidence the tuner acts on.
_has_wrong_fire_evidence would report "this rule fires on something it was gated to reject"
about a fire that never happened, and authorise a narrowing on it. The mirror case lets a
positive satisfy the invariant through a channel it was never observed in, so "all positives
still match" becomes true for the wrong reason.

The two fixes that made it reachable were each correct alone: persisting example texts (F1)
gave the tuner evidence to work from, and requiring wrong-fire evidence before narrowing (F4)
stopped it punishing rules for succeeding. Together they routed persisted prompt text into an
assistant-channel matcher.

business measured the live estate: 0 of 30 artifacts currently use assistant_regex, so 7a is
LATENT — it arms the first time the generator emits one, silently. That is exactly the shape of
finding 3, which was called latent and armed the same night the producer was built. A comment
would not have caught that. This runs in the suite and fails the moment the channel collapses
again, whether or not any artifact exercises it yet.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

# REDIRECT THE SEAT BEFORE TOUCHING THE SUBJECT. Neither of these tests writes to .claude/state
# itself — the guard and the friction modules they exercise do, resolving their root from
# CORE_INSTANCE and falling back to their own repo root when it is unset. So running this suite
# appended to the live seat's telemetry and left a bypass-shaped fixture in .sentinel-last-blocked,
# which core-business once read back as an attack by the Core under review. Caught by run-all.sh's
# state-digest check on its first full run.
import os          # noqa: E402
import tempfile    # noqa: E402

_SEAT_TMP = tempfile.mkdtemp(prefix="channel-iso-seat-")
os.environ["CORE_INSTANCE"] = _SEAT_TMP
os.environ["CLAUDE_PROJECT_DIR"] = _SEAT_TMP

import friction_watchdog as fw          # noqa: E402
import friction_installer as inst       # noqa: E402

FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAIL.append(name)


def main() -> int:
    print("channel isolation (core-business finding 7a)")

    # 1. The context builder routes each example to the field it came from, and to NOTHING else.
    ctx, ch, txt = fw._ctx_for({"text": "ship it", "channel": "prompt"}, "UserPromptSubmit")
    check("prompt example -> prompt_text only",
          ctx["prompt_text"] == "ship it" and ctx["assistant_text"] == "",
          f"assistant_text={ctx['assistant_text']!r}")

    ctx, ch, txt = fw._ctx_for({"text": "I have updated", "channel": "assistant"}, "Stop")
    check("assistant example -> assistant_text only",
          ctx["assistant_text"] == "I have updated" and ctx["prompt_text"] == "",
          f"prompt_text={ctx['prompt_text']!r}")

    # 2. Legacy bare strings carry no provenance. Guessing a channel for them is what created
    #    the bug, so they must report unknown and be refused by callers rather than defaulted.
    ctx, ch, txt = fw._ctx_for("legacy string", "UserPromptSubmit")
    check("legacy bare string -> channel unknown, both fields empty",
          ch == "unknown" and ctx["prompt_text"] == "" and ctx["assistant_text"] == "",
          f"channel={ch}")

    # 3. THE REGRESSION ITSELF. An artifact whose condition is an assistant_regex, whose stored
    #    negative was captured as a PROMPT containing text the regex would match.
    #    Pre-fix this returned True — fabricated evidence. It must return False.
    art = {
        "artifact_id": "art_test_7a",
        "event": "Stop",
        "condition": {"all": [{"op": "assistant_regex", "value": r"I have (updated|written)"}]},
    }

    # Evidence now lives in the evidence store, not in spec["tests"] — spec["tests"] is a closed
    # {positive_ids, negative_ids} set and writing texts there put the persisted spec out of
    # schema (finding 8). Patch the reader rather than writing to this Core's real store, so the
    # test cannot leave state behind; the store's own I/O is round-tripped separately below.
    _real_read = inst.read_evidence
    try:
        inst.read_evidence = lambda _aid: {"negative_texts": [
            {"text": "did you check whether I have updated the file?", "channel": "prompt"}]}
        check("prompt-captured negative cannot fabricate assistant_regex wrong-fire evidence",
              fw._has_wrong_fire_evidence(art) is False)

        # 3b. The same text, genuinely captured as assistant output, IS evidence. Without this
        #     the fix could have been "return False always", which passes 3 and breaks the
        #     feature entirely while looking green.
        inst.read_evidence = lambda _aid: {"negative_texts": [
            {"text": "I have updated the file", "channel": "assistant"}]}
        check("assistant-captured negative IS still evidence",
              fw._has_wrong_fire_evidence(art) is True)
    finally:
        inst.read_evidence = _real_read

    # 3c. The store lives in si_artifacts.evidence now (finding 9), so there is no file to
    #     round-trip in a temp dir. Assert the two properties that matter and are checkable
    #     without writing to the live table: an unknown artifact reads as {}, and a failed
    #     write reports False rather than silently succeeding — 9b was exactly a write that
    #     could no-op in silence.
    check("unknown artifact reads as empty", inst.read_evidence("art_definitely_not_real") == {})
    check("a write that matches no artifact row returns False, not None",
          inst._write_evidence("art_definitely_not_real", {"positive_texts": []}) is False)

    # 4. The producer keeps the channel. If install() ever collapses it again, 1-3 pass
    #    vacuously because nothing reaches them with a channel at all.
    ex = {"positive": [{"hook_input": {"prompt": "push the migration"}}],
          "negative": [{"hook_input": {"assistant_text": "I have updated it"}}]}
    src = (REPO / "scheduling" / "claude-si" / "friction_installer.py").read_text()
    check("installer no longer collapses the channel",
          "_ex(e)" in src and '"channel": "prompt"' in src and '"channel": "assistant"' in src)

    # 5. Untunable is explicit, not a bogus invariant failure (7b).
    import friction_tune as ft
    missing = ft.unsupplied_ops({"all": [{"op": "prompt_regex", "value": "x"},
                                         {"any": [{"op": "state_flag_is", "value": "r"}]}]})
    check("unsupplied_ops walks nested any/all", missing == {"state_flag_is"}, str(missing))
    r = ft.tune({"effect": {"mode": "inject"},
                 "condition": {"all": [{"op": "adversarial_review_present"}]}},
                ["a b", "a c"], [], lambda s, t: True)
    check("untunable condition reports why", not r["ok"] and "untunable" in r,
          r.get("reason", ""))

    # THE BOUNDARY (core-business): enforcement is untunable BY DESIGN, so a block on oracle
    # ops must report expected=True. If it ever reported as a fault it would fire on every
    # block forever and train the reader to ignore it. The same op on an inject is a genuine
    # surprise and must NOT be marked expected — that asymmetry is the whole point.
    blk = ft.tune({"effect": {"mode": "block"},
                   "condition": {"all": [{"op": "state_flag_is", "value": "reviewed"}]}},
                  ["a b", "a c"], [], lambda s, t: True)
    check("block on oracle ops -> expected boundary", blk.get("expected") is True)
    inj = ft.tune({"effect": {"mode": "inject"},
                   "condition": {"all": [{"op": "state_flag_is", "value": "reviewed"}]}},
                  ["a b", "a c"], [], lambda s, t: True)
    check("inject on oracle ops -> NOT expected", inj.get("expected") is False)

    print(f"\n{'FAILED: ' + ', '.join(FAIL) if FAIL else 'all channel-isolation checks pass'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
