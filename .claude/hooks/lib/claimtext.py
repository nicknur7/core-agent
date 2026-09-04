#!/usr/bin/env python3
"""claimtext — shared discriminators for "is this text ASSERTING a claim, or merely mentioning one?"

WHY THIS IS SHARED RATHER THAN COPIED
-------------------------------------
Four gates were caught on 2026-07-27 firing on a character pattern with no notion of subject:
art_9e5498bd on the words "specification"/"expectation", approval-gate reading "why you stop" as a
redirect, financial-figure-gate matching "era" inside "generated", and recall-gate firing on
"you asked for" about the current turn. Each was fixed in place, in its own file, in its own way.

Then recall-gate blocked the reply EXPLAINING its own fix, because that reply quoted the example
string it was describing — and minutes later decision-attribution-gate did exactly the same thing on
exactly the same reply. Two gates, one defect, fixed once each would have been two more patches.

The brain records this as a standing directive with 9 recurrences: consolidate patched/redundant
subsystems into one clean design rather than adding another patch beside the old one. So the
discriminators live here once and every gate imports them.

WHAT THESE ARE NOT
------------------
Tripwires, not semantics. They catch the two mechanical cases that dominate the measured false
positives — a phrase inside quotes is being discussed, and a phrase after a negation asserts the
opposite. They do not understand intent, and a gate that needs real intent detection needs an
oracle, not a regex.
"""
import re

# A phrase inside backticks or quotation marks is being DISCUSSED, not asserted. Bounded length so a
# stray unmatched quote cannot swallow a whole response and silence a gate entirely.
#
# **BOLD IS NOT A QUOTE.** The first version of this file included `\*\*[^*\n]{1,200}\*\*` on the
# reasoning that Nick's words get quoted back in bold. Codex review, same day, pointed at the
# consequence and it reproduced on the first try: `**The hook is broken.**` returned clean from
# state-claim-gate, `**Your checking account balance is $4,231.50 now.**` from financial-figure-gate,
# `**stop now**` from stop-signal-gate. Bold is ordinary emphasis in every reply Core writes, so
# treating it as quotation handed every routed gate a one-keystroke bypass — and worse, a silent
# accidental one, since Core bolds its own conclusions constantly. Quotation marks and backticks
# genuinely delimit mentioned text. Bold does not. It stays out.
_QUOTE_SPAN_RX = re.compile(
    r"`[^`\n]{1,200}`"          # `code`
    r"|\"[^\"\n]{1,200}\""      # "double"
    # A single quote must look like QUOTATION, not like English. The bare pair matched any two
    # apostrophes within 200 chars, so a possessive and a contraction ("both repos' ... We'd")
    # formed a 177-character fake quoted span that swallowed the claim between them and
    # silently suppressed the gate.
    #
    # Measured on this Core's real corpus: 33% of assistant texts contained at least one such
    # fake span, covering 8% of all characters. That is the same one-keystroke silent bypass as
    # the bold-as-quotation bug above, in a character that appears far more often than bold.
    #
    # So an opener may not follow a word character (rules out repos' / don't) and a closer may
    # not precede one (rules out the apostrophe in a contraction acting as a closer). A quoted
    # phrase that itself contains a contraction is no longer detected — that fails toward the
    # gate FIRING, which is the safe direction.
    r"|(?<![A-Za-z0-9])'[^'\n]{1,200}'(?![A-Za-z0-9])"   # 'single' 
    r"|“[^”\n]{1,200}”"         # smart quotes
)

# A claim verb preceded by a negation in the same sentence asserts the OPPOSITE of an attribution.
# Measured: "I'm not claiming you decided anything" matched the attribution pattern identically.
#
# The negation must GOVERN the match, and a clause boundary ends its reach. Codex review 2026-07-27:
# the original 60-character lookbehind scanned across conjunctions, so "No checks were run, but the
# hook is broken." suppressed a live claim on the strength of a negation belonging to a different
# clause. Every blocking gate was bypassable by prefixing a short negated preamble. The lookbehind
# now stops at a contrastive or coordinating boundary, which is exactly where the negation's scope
# ends in English: "not X, but Y" asserts Y.
#
# 2026-08-02 — `n't` could never match, so NO contraction has ever suppressed anything. The
# alternation was `\b(?:not|never|n't|no|nor)\b`, and the leading `\b` applies to every branch: in
# "isn't" the `n` is preceded by `s`, both word characters, so there is no boundary there and the
# `n't` branch is dead. Only the spelled-out forms worked. Measured:
#
#     "I will not write that"           -> negated (suppressed, correct)
#     "I won't write that"              -> NOT negated (blocks — same sentence, same meaning)
#     "that isn't something I'll write" -> NOT negated (blocks a REFUSAL as an action claim)
#
# The third case is how this was found: say-do-gap blocked a reply for promising to do the very
# thing the sentence declined to do, and the re-emission said the same thing in different words.
# That is the strictly-dominated category in the fitness function — cost with no possible benefit.
# Splitting the branch fixes it; the scope guards below (same sentence, <=60 chars, clause break)
# are untouched, so contractions gain exactly the reach "not"/"never" already had and no more.
# An opening parenthesis/bracket ends a negation's reach for the same reason a conjunction does: it
# starts a new assertion. Found by the replay-superset gate when the contraction fix below was made —
# `decision-attribution-gate` stopped catching one of its own confirmed positives:
#
#     "...a security-control weakening you didn't explicitly name (you approved the WebFetch drop...)"
#
# The `didn't` governs "explicitly name". The parenthetical is a separate, POSITIVE attribution, and
# it is exactly what that gate exists to catch. Without this boundary the negation scanned into it.
# Applies to every branch, so "not"/"never" gain the same correct scoping.
_CLAUSE_BREAK = r"(?:,\s*(?:but|yet|though|although|however|and|so|while|whereas)\b|;|\s[—–]\s|:\s|[(\[])"
_NEGATION_RX = re.compile(
    r"(?:\b(?:not|never|no|nor)\b|n't\b)(?:(?!" + _CLAUSE_BREAK + r")[^.!?\n]){0,60}$",
    re.I,
)

_SENT_DELIMS = ".!?\n"


def quoted_spans(text: str):
    """[(start, end)] of every quoted/code span. Never raises — see the note on is_mention()."""
    try:
        return [(m.start(), m.end()) for m in _QUOTE_SPAN_RX.finditer(text or "")]
    except Exception:
        return []


def inside_quote(spans, lo: int, hi: int) -> bool:
    """True when a match sits WHOLLY inside a quoted span. Deliberately requires containment: a quote
    elsewhere in the sentence must not shield a genuine claim beside it."""
    return any(a <= lo and hi <= b for a, b in spans)


def sentence_span(text: str, lo: int, hi: int):
    """The sentence containing [lo, hi)."""
    s = lo
    while s > 0 and text[s - 1] not in _SENT_DELIMS:
        s -= 1
    e = hi
    while e < len(text) and text[e] not in _SENT_DELIMS:
        e += 1
    return s, min(e + 1, len(text))


def negated(sentence: str, match_offset_in_sentence: int) -> bool:
    """True when a negation precedes the match within its own sentence."""
    return bool(_NEGATION_RX.search(sentence[:max(0, match_offset_in_sentence)]))


def is_mention(text: str, start: int, end: int, spans=None) -> bool:
    """The single question every gate actually needs: is this match a MENTION rather than a claim?

    A mention is either quoted (being discussed) or negated (asserting the opposite). Returns False
    on anything it cannot classify — a gate should keep firing when this is unsure, since the cost of
    a missed suppression is noise while the cost of a wrong suppression is an ungrounded claim.

    NEVER RAISES. The callers are hooks that BLOCK unsafe output, and they run on four other Core
    instances that receive this file by sync. If a partial or incompatible copy landed there, an
    exception escaping this function would crash the hook — and a crashed gate blocks nothing, so
    the failure mode of a bad deploy would be silent total loss of enforcement. Codex review
    2026-07-27 traced that path. Any internal failure degrades to False here: suppression is lost,
    the gate keeps firing, and the worst case is noise rather than an unguarded claim.
    """
    try:
        spans = quoted_spans(text) if spans is None else spans
        if inside_quote(spans, start, end):
            return True
        lo, hi = sentence_span(text, start, end)
        return negated(text[lo:hi], start - lo)
    except Exception:
        return False
