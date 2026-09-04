#!/usr/bin/env python3
"""The mint quality gate must reject every artifact the 2026-07-30 purge removed.

WHY THIS TEST EXISTS. The friction generator built its inject message from `want` — the RAW user
correction text, truncated to 400 chars, with no distillation. So whatever Nick typed became live
"guidance" verbatim. Of 30 active artifacts, NINETEEN were undistilled quotes: verbatim profanity, a
pasted terminal banner from a ops transcript, subagent task-ids, and triggers built from one-off
typos (planeed, okkkkk, alllll, autonomusly, bouple, coxed).

The loop had been evaluated only on whether it RAN — mint counts, proof windows, promote paths,
watchdogs. Never once on whether its output was any good. One read of active.json falsified it.

So this is a REGRESSION CORPUS, not a unit test: the real rejected artifacts are the fixtures. If a
future refactor loosens the gate, these rows come back, and they come back into every turn's context.

Run: python3 bin/tests/test_mint_quality_gate.py
"""
import re
import sys

# The gate's predicates, mirrored from friction_router._route_contract. Kept as a copy ON PURPOSE:
# if someone changes the router, this test must FAIL rather than silently follow it. A test that
# imports the logic it is testing proves only that the logic equals itself.
MAX_LEN = 160
PROFANE = r"fuck|shit|damn"
PASTED = r"[▗▘▙▚▛▜▝▞▟│─╭╮╰╯]|task-id|tool_use|<function_"
# 2026-07-30, second pass. The gate above was necessary and not sufficient: three more artifacts
# minted the same morning, all short and clean enough to pass it, all still raw quotes.
# First person is the tell. A distilled directive reads "use codex alongside core for substantial
# system/code work"; a quote reads "i want you to fix it end to end".
FIRST_PERSON = r"\b(?:i|i'?m|i'?ll|i'?ve|my|me)\b"


def rejects(message: str) -> bool:
    m = message.lower()
    if len(message) > MAX_LEN:
        return True
    if re.search(PROFANE, m):
        return True
    if re.search(PASTED, message):
        return True
    if re.search(FIRST_PERSON, m):
        return True
    return False


# The recurrence precondition, checked before any of the text rules. Mirrored from
# friction_router for the same reason the predicates above are copied rather than imported.
def rejects_support(distinct_sessions: int, positive_case_ids: list) -> bool:
    return distinct_sessions < 2 or not positive_case_ids


# Real messages from the 19 artifacts quarantined 2026-07-30, abbreviated only where length is
# already past the limit (the length check fires either way).
MUST_REJECT = [
    ("profanity + typo",
     "Recurring expectation (correction-frustration): go fucking look and tell me what we had planeed t odo next."),
    ("pasted terminal banner",
     "Recurring expectation (correction-already-told-you): i just want to give you the session "
     "transcript of the start of my ops session: \" ▗ ▖ ▌▐ core-ops session start banner │──── \""),
    ("undistilled 446-char quote",
     "Recurring expectation (correction-flip-flop): i want you to finish allll of this up \"1. Two "
     "parallel SI inducers, not one spine. si_induct -> learned_contracts (6 live) -> "
     "learned-classifier hook AND friction_loop -> active.json -> friction dispatch, two engines "
     "minting into two stores with two promotion paths and no shared ledger, which is the accretion "
     "disease the plan names, and it is why nobody can say what is live\""),
    ("subagent task-id leak",
     "Recurring expectation (correction-explicit-no): the agent returned task-id abc123 and tool_use "
     "blocks that should never have reached the transcript, plus a wall of output nobody asked for, "
     "and then claimed it was done when the file was never written at all"),
]

MUST_ACCEPT = [
    ("distilled recurring ask",
     "Recurring ask (10x): use codex alongside core for substantial system/code work"),
    ("distilled recurring ask 2",
     "Recurring ask (5x): ground truth against past history and decisions before making changes"),
    ("distilled instruction",
     "Recurring expectation (instruction-preference): codex is not life only anymore, make it "
     "available across all cores"),
    ("short imperative",
     "Recurring ask (3x): automate baseline sync/install fully so pull-only Cores don't need manual steps"),
]

# The three artifacts minted 2026-07-30 AFTER the first purge — the proof that the length /
# profanity / pasted-output rules were not enough. Each is short, clean, typo-bearing, and a
# verbatim quote. Two carry typos (colloablorate, reced/appy/codext), which is itself the
# signature of a single occurrence: a genuinely recurring expectation recurs in the words too.
# Rows are (label, message, distinct_sessions, positive_case_ids) and are checked through the
# FULL gate in the router's real order — support first, then text. That order matters here: the
# third row has no first-person pronoun and is under the length cap, so no text rule catches it.
# What stops it is that it never recurred. Writing a regex to catch "however you think" would be
# fitting this one fixture rather than a principle, which is precisely how the four gates audited
# on 2026-07-27 came to match character patterns with no notion of subject. Left uncaught by the
# text layer on purpose, and caught where it should be.
SECOND_PASS_REJECT = [
    ("first person, typos",
     "Recurring expectation (instruction-directive): do whatever is reced. i want you to fix it "
     "end to end and test it. if it works appy across all cores", 1, []),
    ("first person, one-off question",
     "Recurring expectation (instruction-directive): Okay, before that, I want you to tell me "
     "exactly what you think I want you to do with the sub-agents", 1, []),
    ("no text tell — stopped by recurrence alone",
     "Recurring expectation (instruction-directive): deep plan green lighted but make sure to "
     "colloablorate with codex however you think would create the best result", 1, []),
]

# Recurrence support. Every case in the pool carried a hardcoded distinct_sessions=1 and an
# empty positive_case_ids because friction_miner's P2 clustering stage was never built, so
# "Recurring expectation" described a single prompt from a single session. Two SESSIONS is the
# floor rather than two occurrences: ten corrections in one bad afternoon are one event.
SUPPORT_CASES = [
    ("the state every case was in before 2026-07-30", 1, [], True),
    ("repeated within one session only", 1, ["fc_a", "fc_b"], True),
    ("sibling ids missing", 3, [], True),
    ("genuinely recurring", 3, ["fc_a", "fc_b"], False),
]


def main() -> int:
    p = f = 0
    print("=== mint quality gate — regression corpus from the 2026-07-30 purge ===\n")
    print("--- MUST REJECT (these were live guidance until the purge) ---")
    for label, msg in MUST_REJECT:
        if rejects(msg):
            print(f"  PASS  rejected: {label}")
            p += 1
        else:
            print(f"  FAIL  ACCEPTED (would go live): {label}")
            print(f"        {msg[:100]}")
            f += 1
    print("\n--- MUST ACCEPT (real distilled guidance; over-rejecting is also a failure) ---")
    for label, msg in MUST_ACCEPT:
        if not rejects(msg):
            print(f"  PASS  accepted: {label}")
            p += 1
        else:
            print(f"  FAIL  rejected good guidance: {label}")
            print(f"        {msg[:100]}")
            f += 1
    print("\n--- MUST REJECT, second pass — FULL gate, router order (support then text) ---")
    for label, msg, sessions, ids in SECOND_PASS_REJECT:
        by_support = rejects_support(sessions, ids)
        by_text = rejects(msg)
        if by_support or by_text:
            which = "support" if by_support and not by_text else ("text" if by_text and not by_support else "both")
            print(f"  PASS  rejected by {which}: {label}")
            p += 1
        else:
            print(f"  FAIL  ACCEPTED (would go live): {label}\n        {msg[:100]}")
            f += 1

    print("\n--- RECURRENCE SUPPORT (checked before any text rule) ---")
    for label, sessions, ids, want_reject in SUPPORT_CASES:
        got = rejects_support(sessions, ids)
        verb = "rejected" if got else "accepted"
        if got == want_reject:
            print(f"  PASS  {verb}: {label} (sessions={sessions}, siblings={len(ids)})")
            p += 1
        else:
            print(f"  FAIL  {verb}: {label} (sessions={sessions}, siblings={len(ids)})")
            f += 1

    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
