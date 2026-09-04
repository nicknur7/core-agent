#!/usr/bin/env python3
"""WHO WROTE THIS TURN — the one place that answers it, for miners and for hooks alike.

THE PROBLEM THIS EXISTS TO END. Transcript user-role records are not all from the user. Hook
feedback, system reminders, slash-command payloads, compaction summaries, tool results, background
notifications and scheduler prompts all arrive in the same role. Two Cores spent a night proving
what that costs:

  - core-life's own cron wrote 30 turns saying "do not stop, do not summarize, do not wait for
    Nick" — the INVERSE of what Nick wants, phrased as imperatives, more consistent than anything
    he actually typed. Both Cores obeyed their crons over him in real time.
  - core-business measured 9 of 111 mined "corrections" (8%) as machine text; one cluster was 57%
    not-him.

WHY A BLOCKLIST CANNOT WORK, stated once so nobody rebuilds one. The prior mechanism was
HOOK_PREFIXES, a tuple of opening strings grown by hand after each incident — hook feedback added
2026-06-09, background-agent notices 2026-07-27. Every entry is a generator someone met the hard
way. The cron ticks match NONE of them, because they open with ordinary prose.

And the deeper reason, which is not about coverage: **content-based filtering fails by
construction, because the contaminant is better-formed than the real thing.** Nick writes "Soooo
you can actually finish it"; the machine writes clean imperatives. Any filter scoring
well-formedness, consistency, or instruction-shape prefers the fake.

SO: PROVENANCE, NEVER CONTENT. Claude Code stamps `origin: {"kind": "human"}` on turns a person
typed, and `{"kind": "task-notification"}` on events. Measured across all four seats on this
machine, back to the earliest transcript each holds:

    seat       human  task-notif  untagged      and the untagged bucket is machine-generated:
    life         674         264       518      hook feedback, reminders, interrupts, cron, compaction
    school       482          75       231
    ops         487         169       301
    finance       27          63        31

An ALLOWLIST on origin.kind == "human", not a blocklist of shapes we have thought of. The next
generator gets excluded because of where it came from, without anyone editing this file.

REFUSING IS PART OF THE CONTRACT. If an archive carries no provenance at all — an older export, a
fork, a different client — an allowlist silently mines ZERO and the Core quietly stops learning
while every check stays green. `archive_has_provenance()` exists so a caller can tell "no human
turns" from "cannot tell", and REFUSE rather than report a clean empty result.
"""

from __future__ import annotations

# Kinds a caller may see. Only HUMAN is the user.
HUMAN = "human"
TASK_NOTIFICATION = "task-notification"
TOOL_RESULT = "tool_result"
UNTAGGED = "untagged"


def _content(entry: dict):
    return (entry.get("message") or {}).get("content")


def text_of(entry: dict) -> str:
    """The human-readable text of a turn, whether it arrived as a string or as content BLOCKS.

    A TURN WITH AN ATTACHMENT IS NOT A STRING. When Nick pastes a screenshot, message.content is a
    LIST of blocks — one image block, one text block — and the old check
    `isinstance(c, str) and c.strip()` discarded the whole turn despite origin.kind == "human".
    Silently: it did not appear as rejected, it simply never existed.  # privacy-ok: generic engineering vocabulary

    core-business caught it by running the filter over its own archive rather than reading it:
    585 stamped human, 582 admitted, 3 DROPPED — all three plainly the operator, including an
    emphatic, image-attached correction pointing out a missing option.

    A filter whose whole job is to decide whose words the system learns from was wrong by
    construction for every image-bearing turn, on every seat.
    """
    c = _content(entry)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_tool_result(entry: dict) -> bool:
    """Tool output is stored in the USER role and is not a turn at all.

    core-business counted 7,605 of its 7,985 "untagged user records" as these and concluded from
    that 90% that provenance was unrecoverable and mining could only start from a tagging date.
    The count was right and the population was wrong. Strip them before any ratio is computed.
    """
    c = _content(entry)
    return isinstance(c, list) and any(
        isinstance(x, dict) and x.get("type") == "tool_result" for x in c)


def turn_kind(entry: dict) -> str:
    """HUMAN / TASK_NOTIFICATION / TOOL_RESULT / UNTAGGED for one transcript record."""
    if entry.get("type") != "user":
        return UNTAGGED
    if is_tool_result(entry):
        return TOOL_RESULT
    o = entry.get("origin")
    kind = o.get("kind") if isinstance(o, dict) else None
    if kind == HUMAN:
        return HUMAN
    if kind == TASK_NOTIFICATION:
        return TASK_NOTIFICATION
    return UNTAGGED


def is_human_turn(entry: dict) -> bool:
    """The allowlist. Anything not positively stamped as human is not the user.

    Note what is deliberately NOT consulted: `userType`, which is 'external' on Nick's turns AND on
    every cron tick, so it separates nothing; `promptSource`, which is 'system' on 28 of 30 ticks
    but also on 134 other turns; and the message text, for the reason in the module docstring.
    """
    if turn_kind(entry) != HUMAN:
        return False
    # PROVENANCE IS ABOUT WHO, NOT ABOUT WHAT (2026-08-28).
    #
    # This used to end `return bool(text_of(entry).strip())`, which contradicted the docstring three
    # lines above: it says the message text is deliberately NOT consulted, and then consulted it as
    # an emptiness test. The disagreement was not cosmetic — it dropped one of Nick's real turns on
    # this seat's live archive, an IMAGE-ONLY message (content == [{"type": "image"}], no text
    # part), which is the same B1 class the multimodal fix was written for, surviving in the one
    # shape that fix did not cover: a screenshot sent with nothing typed alongside it.
    #
    # Admitting it cannot leak a scheduler tick — turn_kind() already refused those above, on
    # provenance, and ticks carry plenty of text so emptiness never separated them anyway.
    #
    # Callers that need text still handle empty correctly and were checked, not assumed:
    # learned-corpus-miner.py:389 takes text_of(), matches no CORRECTION_RX/INSTRUCTION_RX against
    # "", produces no labels, and `continue`s — so an image-only turn mines to nothing rather than
    # to a blank correction. That is the right outcome, and it is the miner's call to make, not
    # this function's.
    return True


def is_mineable_turn(entry: dict) -> bool:
    """Is this a human turn that has something to LEARN FROM? Provenance AND content.

    Split out of is_human_turn on 2026-08-28 because the suite held two checks that contradicted
    each other for one real case:

        "EVERY stamped-human turn on the live archive is admitted"   (479/479 required)
        "an image-ONLY turn with no text is not mineable"            (admitting it inserts empty rows)

    Both are right, about different questions. Nick sending a screenshot with nothing typed IS one
    of his turns — dropping it from provenance is the B1 defect, which is what the live-archive
    check exists to catch. It is also not something to mine — there is no text, and a corpus row
    built from it is empty. One predicate cannot answer both without being wrong about one of them,
    and the version that tried was wrong about the first: it dropped a genuine turn of his.

    So: is_human_turn answers WHO (provenance, the allowlist). This answers WHETHER THERE IS
    ANYTHING TO LEARN. Callers that mine text should use this one.
    """
    return is_human_turn(entry) and bool(text_of(entry).strip())


def archive_has_provenance(entries) -> bool:
    """Does this archive stamp provenance at all? Distinguishes 'none' from 'cannot tell'.

    Without this an allowlist over an unstamped archive returns zero human turns and every
    downstream count reads as a clean, healthy, empty result — the exact shape of failure the
    empty-suite refusal exists to prevent, arriving in the corpus instead of the tests.
    """
    for e in entries:
        if e.get("type") != "user" or is_tool_result(e):
            continue
        o = e.get("origin")
        if isinstance(o, dict) and o.get("kind"):
            return True
    return False


def split(entries) -> dict:
    """Bucket counts for a set of records — for reporting, so a filter can show its work."""
    out = {HUMAN: 0, TASK_NOTIFICATION: 0, TOOL_RESULT: 0, UNTAGGED: 0}
    for e in entries:
        out[turn_kind(e)] = out.get(turn_kind(e), 0) + 1
    return out
