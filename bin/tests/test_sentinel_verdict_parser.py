#!/usr/bin/env python3
"""The Sentinel verdict reader, tested against every shape that has broken it.

WHAT THIS GUARDS
----------------
sentinel-receipt.sh turns a reviewer's verdict into the receipt sentinel-approve.sh requires
before any outward action. Read it wrong in one direction and the approval chain is dead
(pushes blocked while the reviewer sat there having answered). Read it wrong in the other and a
review that REFUSED mints an approval token for a push to a baseline three Cores and an external
fork pull from.

FIVE REVISIONS. Each of the first four passed its own tests and was wrong:

  r1  a line had to reduce to exactly one verdict word    -> real inline verdicts unreadable;
                                                             core-business blocked, rc=4
  r2  allowed a separator after the verdict               -> "APPROVE for X" still failed, so
                                                             ASK/BLOCK worked and the only
                                                             verdict that UNBLOCKS did not
  r3  discarded any line naming a second verdict          -> a refusal explaining itself was
                                                             ERASED, letting a later
                                                             APPROVE-opening line mint approval
  r4  narrowed that exclusion                             -> "APPROVE is withheld — ..." parsed
                                                             as APPROVE; a blockquoted "> BLOCK"
                                                             wedged re-reviews at BLOCK forever

Every one of those is a consequence of treating the whole report body as a verdict surface.
Both agent specs said the answer all along — sentinel.md:112, sentinel-code.md:148:

    Line 1: `APPROVE`, `BLOCK`, or `ASK`

r5 reads LINE 1 first. If line 1 is prose, it falls back to a verdict standing on a line of its
OWN — and refuses if the report contains more than one such line, rather than guessing which is
current. Prose can never satisfy the standalone rule, so the body cannot assert anything.

One deliberate asymmetry: a refusal may carry its reasoning inline ("BLOCK — credentials
found"), but an APPROVE whose own line negates it ("APPROVE is withheld") is refused. Approval
is the only verdict that unblocks anything, so it is the only one held to the stricter bar.

The whole block — parser, negation guard, line-1 selection — is EXECUTED out of the shipped
hook. Reimplementing any of it here is how r4's test stayed green while the aggregator was
broken; that already happened once, to this file.

    python3 bin/tests/test_sentinel_verdict_parser.py
"""
from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent.parent / ".claude" / "hooks" / "sentinel-receipt.sh"

# (report, expected verdict) — "" means DECLINE: no receipt minted, action stays blocked.
CASES = [
    # ---- r8: the VERDICT: marker is authoritative and prose is not consulted at all ----
    # core-business's diagnosis, which ended seven revisions of regex: "APPROVE for `bash x`"
    # (a verdict) and "BLOCK is not warranted here" (not one) are the same shape — VERB, space,
    # word. No rule separates them because the information is not in the string. So the CONTRACT
    # changed: the agent specs now require a final "VERDICT: X" line, and only that is read.
    ("APPROVE for `bash x`\n\nBLOCK is not warranted here.\n\nVERDICT: APPROVE", "APPROVE"),
    ("> APPROVE\n\nThe quote above was illustrative.\n\nVERDICT: BLOCK", "BLOCK"),
    ("APPROVE\n\nOn reflection rule 3 failed.\n\nVERDICT: BLOCK", "BLOCK"),
    ("APPROVE is withheld pending confirmation.\n\nVERDICT: ASK", "ASK"),
    ("verdict: approve", "APPROVE"),
    ("  VERDICT:   BLOCK  ", "BLOCK"),
    # only the LAST line is read, so a stray earlier marker cannot compete
    ("VERDICT: APPROVE\n\nVERDICT: BLOCK", "BLOCK"),
    # a marker must stand alone on its line — inside a table row it is not a marker
    ("A table row | VERDICT: APPROVE | is not a marker line", ""),
    # FORGERY, found by sentinel-code by MINTING A REAL RECEIPT with it. A reviewer who
    # genuinely BLOCKS and then helpfully explains the new format — with an illustrative
    # "VERDICT: APPROVE" in a code fence — minted APPROVE when the marker was scanned for
    # anywhere. The agent spec written in the same commit explicitly invites that prose.
    # Anchoring the marker to the LAST non-blank line (which is what the spec already demands)
    # makes position rather than content the thing that authorises.
    ("BLOCK -- Rule 2 failed: credential pattern found at deploy.sh:44.\n\n"
     "Note: the new contract requires reviewers to close with a marker line, e.g.:\n"
     "```\nVERDICT: APPROVE\n```\n"
     "This reviewer forgot to append the real closing marker due to truncation.", "BLOCK"),

    # ---- real reviewer output that must APPROVE ----
    ("APPROVE\nRules 1-8 pass on verified evidence.", "APPROVE"),
    ("**APPROVE**\n\nVerdict basis, rule by rule:", "APPROVE"),
    ("Verdict: **APPROVE**\nAll clean.", "APPROVE"),
    # core-business's original bug was that this returned NOTHING. It still declines — but now
    # DELIBERATELY, not accidentally. An approval must be the bare word; a reviewer who writes
    # "APPROVE for X" has not followed the format and restates it. r5 tried to accept this and
    # the same permissiveness let six hedged approvals mint ("APPROVE if the operator confirms").
    # A blocklist of ways to hedge is unbounded; "bare or nothing" is not.
    ("**APPROVE** for `bash bin/sync-from-baseline.sh` at baseline `61f0491`.", ""),

    # ---- refusals, which may state their reasoning inline ----
    ("BLOCK\nRule 3 failed: destructive op found.", "BLOCK"),
    ("**BLOCK** — credentials found in the diff.", "BLOCK"),
    ("ASK\nRule 6 flag: new shared.dirs entry.", "ASK"),
    ("**ASK** — one specific gate needs the operator's explicit confirmation...", "ASK"),

    # ---- CATASTROPHIC shapes: a refusal must never mint an approval ----
    # r4 parsed both of these as APPROVE (found by sentinel-code).
    ("APPROVE is withheld — Rule 1 trust-root change needs the operator's confirmation.", ""),
    ("APPROVE is blocked pending human confirmation.", ""),
    # r3 discarded the refusal line entirely, letting the second line win.
    ("BLOCK — Rule 1 violated, so it cannot be marked APPROVE.\n\n"
     "APPROVE is blocked pending.", "BLOCK"),

    # ---- prose in the BODY must not change the verdict, either direction ----
    # r4 escalated these to BLOCK (found by core-business and sentinel-code).
    ("APPROVE\n\nBLOCK-listed patterns absent from the diff. Rules 1-8 pass.", "APPROVE"),
    ("APPROVE\n\nI considered whether to BLOCK here but the finding was fixed.", "APPROVE"),
    ("APPROVE\n\nThe previous review said BLOCK; that finding is now fixed.", "APPROVE"),

    # ---- a buried verdict OVER-BLOCKS, deliberately ----
    # A re-review that quotes a prior "> BLOCK" and concludes APPROVE at the bottom is recorded
    # as BLOCK: line 1 is prose, so the refusal fallback finds the quoted BLOCK and the trailing
    # APPROVE is not recoverable. sentinel-code flagged this shape and wanted APPROVE.
    #
    # Keeping it as BLOCK is the trade-off, stated rather than hidden: over-blocking costs the
    # reviewer one restatement on line 1, which the spec asks for anyway. Under-blocking costs
    # an unapproved push to a baseline three Cores and an external fork pull from. Given five
    # prior revisions failed by trying to be clever about ambiguous prose, "refuse and make them
    # restate" is the only rule here I can actually defend.
    # Under r7 a QUOTED "> BLOCK" no longer asserts either, so a report whose only refusal is
    # quoted and whose approval is buried has NO valid verdict and declines. The reviewer
    # restates on line 1. Safe, and consistent: quoting someone else is not asserting.
    ("Following up: the previous review said\n\n> BLOCK\n> Rule 3 failed.\n\n"
     "That is fixed now.\n\nAPPROVE", ""),

    # ---- r6 REGRESSION: a QUOTED or LISTED approve locked line 1 before the real refusal ----
    # r6 stripped ">", "#" and list bullets as decoration, so a quoted/illustrative APPROVE was
    # indistinguishable from an asserted one and no later BLOCK could override it. sentinel-code
    # found all five and noted the OLD last-wins code got every one right — a true regression.
    # Block-level markers now disqualify a line instead of being stripped off it.
    ("> APPROVE\n\nBLOCK -- rule 3 failed, the quote above was illustrative only.", "BLOCK"),
    ("> **APPROVE**\n\nBLOCK -- actually rule 3 failed, ignore the quoted example.", "BLOCK"),
    ("- APPROVE\n\nBLOCK -- rule 1 violated.", "BLOCK"),
    ("# APPROVE\n\nBLOCK -- rule 2 failed, credentials found.", "BLOCK"),
    (">> APPROVE\n\nBLOCK -- rule 3.", "BLOCK"),
    # An approval cannot stand while the body refuses, even when line 1 is a clean bare APPROVE.
    ("APPROVE\n\nBLOCK -- on reflection rule 3 failed.", ""),
    # A preamble followed by a verdict on its OWN line IS accepted, via the narrow fallback:
    # exactly one standalone verdict in the report, so there is nothing to guess between.
    # sentinel-code called preamble-then-verdict a natural re-review shape and was right.
    # The body fallback now recovers REFUSALS ONLY. sentinel-code showed a merely illustrative
    # standalone APPROVE would otherwise mint: "a clean report looks like: APPROVE ... but this
    # diff is not clean."
    ("Rules 1-8 pass with no findings.\n\nAPPROVE", ""),
    ("Following up on the prior review.\n\nBLOCK", "BLOCK"),
    ("For reference, a clean report looks like:\n\n    APPROVE\n\nBut this diff is not clean.", ""),
    # sentinel-code's six hedged approvals — a blocklist missed every one.
    ("APPROVE — needs the operator's explicit confirmation first.", ""),
    ("APPROVE, awaiting the operator's sign-off.", ""),
    ("APPROVE contingent on the operator's confirmation.", ""),
    ("APPROVE requires the operator's explicit sign-off before use.", ""),
    ("APPROVE if the operator confirms.", ""),
    ("APPROVE — the operator still needs to confirm this.", ""),
    # ...but TWO different standalone verdicts (a quoted prior BLOCK plus this APPROVE) is
    # ambiguous, and the hook refuses rather than picking. That is the wedge sentinel-code
    # found, resolved by declining instead of guessing.
    ("Following up.\n\n> BLOCK\n\nFixed now.\n\nAPPROVE", ""),
]


def load_block():
    """The shipped verdict logic — parser, negation guard and line-1 selection — as one unit."""
    src = HOOK.read_text()
    start = src.index("def _verdict_of")
    end = src.index("nonce = secrets", start)
    return src[start:end]


def main() -> int:
    print("sentinel verdict reader (revision 7 — bare approval, quoted verdicts disqualified)")
    try:
        block = load_block()
    except Exception as e:
        print(f"  FAIL  could not lift the verdict block from the hook — {type(e).__name__}: {e}")
        return 1

    def read(report: str) -> str:
        ns = {"report": report, "re": re, "sys": sys}
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                exec(block, ns)
            return ns.get("verdict", "")
        except SystemExit:
            return ""          # the hook declined — no receipt

    fails = []
    for report, want in CASES:
        got = read(report)
        ok = got == want
        if not ok:
            fails.append(
                f"{report.splitlines()[0][:44]!r} -> {got or 'decline'} (want {want or 'decline'})")
        print(f"  {'PASS' if ok else 'FAIL'}  {got or '(decline)':10} want={want or '(decline)':10} "
              f"{report.splitlines()[0][:46]}")

    print(f"\n{'all verdict-reader checks pass' if not fails else 'FAILED: ' + '; '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
