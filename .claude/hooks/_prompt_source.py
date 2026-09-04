#!/usr/bin/env python3
"""One definition of "is this text the user actually typed?" — shared by every prompt-stage hook.

WHY THIS EXISTS (2026-08-18, measured by core-finance, reproduced on life).

UserPromptSubmit fires for far more than the user's own messages: task-notifications from background
agents, monitor events, peer-bus traffic, command stdout. Several hooks INJECT on that stage, and two
of them inject a MANDATE rather than a suggestion:

    "Your FIRST tool call MUST be a grep across those for the topic he's referencing.
     Don't ask him to repeat info — find what he already said."

On a notification turn the premise is false by construction — the user referenced nothing; a
background task did. The compliant response is a search for a topic that does not exist, and the real
hazard is not the wasted call: it is that a MUST-directive with no valid referent creates pressure to
invent a plausible one. Every failure this fleet corrected on 2026-08-18 was a claim made to satisfy
an expectation on thin evidence, and this manufactures exactly that pressure on turns where the
premise cannot be true.

finance's session: 36 injections, **35 on turns with no user text at all — a 97% false-fire rate**,
against 8 real user messages in 237 hours. Life's current transcript: **217 of 301 user-type records
(72%) are `<task-notification>`.**

THE GUARD ALREADY EXISTED AND MISSED THE TAG THAT DOMINATES TODAY'S TRAFFIC. `HOOK_PREFIXES` was
defined FOUR times, byte-identical (sha a69e7ba31ca1) across learned-classifier, learned-stopguard,
learned-recallguard and learned-validator — and none of the four listed `<task-notification>`. Four
copies of one constant is the accretion Nick's standing directive names; four copies that are all
equally out of date is what that accretion costs. Hence one definition here, not a fifth patch.

FAILS TOWARD FIRING. `is_user_text` returns True for anything it does not positively recognise as
machine-generated, so a hook that cannot classify a prompt behaves exactly as it does today. The
dangerous direction is silently suppressing a real user turn; a false fire is the bug we are already
paying for, and it is the recoverable one.
"""

# Prefixes that mark text the runtime injected rather than the user typed. Matched after lstrip().
MACHINE_PREFIXES = (
    "<task-notification>",          # background agent / monitor events — the 2026-08-18 finding
    "[SYSTEM NOTIFICATION",         # bracket header form of the same
    "<system-reminder>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<user-prompt-submit-hook>",
)


def is_user_text(prompt) -> bool:
    """True unless `prompt` is positively recognised as runtime-injected text.

    Deliberately prefix-only. Content heuristics ("does this look automated?") would be the third
    instrument this fleet built that answers a different question than the one asked — a user can
    quote a notification, and misreading that as machine text costs a real turn. A prefix is a fact.
    """
    if not isinstance(prompt, str):
        return False
    head = prompt.lstrip()
    if not head:
        return False
    return not head.startswith(MACHINE_PREFIXES)
