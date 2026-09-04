#!/usr/bin/env python3
"""The injected clock exempts TIME-OF-DAY claims and must NOT exempt DURATION claims.

WHY THIS EXISTS. Phase 1.1 of the master plan is the "supply beats policing" move:
session-presence now injects the live wall clock on every UserPromptSubmit, so a claim about
what time it is NOW is sourced from context and time-claim-gate should stop blocking it. That
gate had fired 110 times and was right essentially every time — which is exactly the case the
fitness function is sharpest about, because a Stop block is a CONTINUATION, not a regeneration:
each block left BOTH the flawed sentence and the correction in the transcript.

The risk in this change runs one way. Exempting too much would silently license the claim class
the gate was built for — "we've been working three hours", "the session started at 9:00" — which
the clock does NOT answer, because it gives you NOW and says nothing about the start. So the
exemption is partial, and this test is the thing that keeps it partial. If someone later widens
it to all time claims, these rows fail.

Run: python3 .claude/hooks/tests/test-clock-supply.py
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[3])
spec = importlib.util.spec_from_file_location("tcg", ROOT / ".claude" / "hooks" / "time-claim-gate.py")
tcg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tcg)

# Mirrors the gate's ALLOWLIST. It was a blocklist of duration patterns until Codex's
# adversarial pass found that "We started at 09:14 PDT" matched no duration pattern and was
# therefore exempted by default — a session-start time the live clock cannot source. With a
# blocklist every future TIME_PATTERN arrives exempt; with an allowlist it arrives gated.
CLOCK_SOURCEABLE_IDX = {5, 6, 7, 8}


def is_exempt(text: str) -> bool:
    src = any(tcg.TIME_PATTERNS[i].search(text)
              for i in CLOCK_SOURCEABLE_IDX if i < len(tcg.TIME_PATTERNS))
    unsrc = any(tcg.TIME_PATTERNS[i].search(text)
                for i in range(len(tcg.TIME_PATTERNS)) if i not in CLOCK_SOURCEABLE_IDX)
    return src and not unsrc


def is_duration(text: str) -> bool:
    return not is_exempt(text)


# The clock answers these completely. Detected as time claims, exempt once the clock is present.
CLOCK_ANSWERS = [
    ("time-of-day greeting", "Good morning — here is where the baseline stands after last night's push."),
    ("this morning",         "I finished the sync this morning and the peers pulled clean afterwards."),
    ("bare tonight",         "We shipped the whole friction repair tonight and the suite is green."),
]

# DELIBERATELY STILL GATED, recorded so the decision is visible rather than looking like an
# oversight. "It is 14:22 PDT right now" IS something the injected clock sources — but telling it
# apart from "We started at 09:14 PDT" requires distinguishing present from past tense, which a
# pattern index cannot do. The two failure directions are not symmetric: gating a present-tense
# clock claim costs one block that `date` resolves in seconds, while exempting a past-tense one
# licenses exactly the claim class this gate exists for. So the strict reading wins.
STILL_GATED_ON_PURPOSE = [
    ("present-tense wall clock", "It is 14:22 PDT right now, so the close window is still open."),
]

# The clock does NOT answer these: it gives NOW, not the START or the ELAPSED span.
CLOCK_DOES_NOT_ANSWER = [
    ("numeric duration",  "We have been working about 3 hours on this session and it shows in the diff."),
    ("word duration",     "This session we ran roughly three hours before the first clean suite."),
    ("session-bound verb", "The session started at 09:14 and the first commit landed twenty minutes later."),
    ("relative anchor",   "I pushed that fix about an hour ago, right before the peers pulled it down."),
    # Codex, 2026-07-30: matched the clock-claim pattern but no duration pattern, so the
    # blocklist version exempted it. The clock says what time it is NOW and can never source a
    # claimed 09:14 start.
    ("past clock time",   "We started at 09:14 PDT and the first commit followed shortly after."),
    ("past clock, no 'session'", "The run kicked off at 11:02 PDT, which is why the numbers look odd."),
]


def main() -> int:
    p = f = 0
    print("=== injected clock: exempt time-of-day, never exempt duration ===\n")
    print("--- CLOCK ANSWERS IT (detected as a time claim, but not duration → exempt) ---")
    for label, text in CLOCK_ANSWERS:
        hits, dur = tcg.detect(text), is_duration(text)
        if hits and not dur:
            print(f"  PASS  detected + exemptible: {label}")
            p += 1
        elif not hits:
            print(f"  FAIL  not detected at all — gate would never have caught it: {label}")
            f += 1
        else:
            print(f"  FAIL  misclassified as DURATION, would stay blocked: {label}")
            f += 1

    print("\n--- CLOCK DOES NOT ANSWER IT (must stay blocked even with the clock present) ---")
    for label, text in CLOCK_DOES_NOT_ANSWER:
        if is_duration(text):
            print(f"  PASS  still requires a real time source: {label}")
            p += 1
        else:
            print(f"  FAIL  EXEMPTED a duration claim — the clock cannot source this: {label}")
            print(f"        {text}")
            f += 1

    print("\n--- deliberately still gated (present/past tense is not decidable by pattern) ---")
    for label, text in STILL_GATED_ON_PURPOSE:
        if is_duration(text):
            print(f"  PASS  {label}: gated, by choice — see the note above")
            p += 1
        else:
            print(f"  FAIL  {label} became exempt; re-read why that is the unsafe direction")
            f += 1

    print("\n--- the clock detector itself ---")
    cases = [
        ("matches the injected form", "⏰ 2026-07-30 14:22 PDT (live, this turn)", True),
        ("rejects prose about a clock", "the clock says 14:22 and I think that is right", False),
        ("rejects a bare timestamp", "2026-07-30 14:22 PDT", False),
    ]
    for label, text, want in cases:
        if bool(tcg._CLOCK_RX.search(text)) == want:
            print(f"  PASS  {label}")
            p += 1
        else:
            print(f"  FAIL  {label}")
            f += 1

    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
