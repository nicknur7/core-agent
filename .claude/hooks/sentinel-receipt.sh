#!/usr/bin/env bash
# SubagentStop hook — evidence that Sentinel actually ran, and what it decided.
#
# WHY THIS EXISTS
# ---------------
# Until 2026-07-27 the approval chain had no link to Sentinel at all. sentinel-approve.sh
# touched an empty file named sha256(command); pretooluse-guard.sh checked only that the
# file existed, then deleted it. Any Bash call minted one. The gate that guards every
# outward action could be satisfied by the agent it was guarding — and was, twice in one
# session by this Core, once deliberately to prove the point and once in good faith by
# deciding a review was unnecessary. The guard cannot tell those apart.
#
# This is the missing link, built on the same pattern reconcile-subagent-receipt.sh
# already uses: a subagent's termination is a control-plane event the model does not
# author, so it is the best available evidence that a specific agent genuinely ran.
#
# WHAT THIS IS AND IS NOT — read before trusting it
# --------------------------------------------------
# Against an attacker who IS this agent, with Bash under the same UID, NO local mechanism
# survives. It can write this receipt directly. HMAC does not help (it can read the key);
# a "hook-only" directory does not help (if a hook can write it, so can Bash). Surviving
# that threat needs a broker holding a key outside the agent's OS authority, which this
# runtime does not have.
#
# So this is an ACCIDENT AND DRIFT BARRIER, not a security boundary. It defeats the
# failure that actually happened — an agent talking itself into skipping review — and it
# makes skipping review require deliberate forgery rather than a decision. That is a real
# improvement and it is not the same as being secure. The README says so in those words.
#
# Fail-open by design: never blocks. A missing receipt means approval is refused later,
# which is the safe direction.
set -uo pipefail

"$(dirname "${BASH_SOURCE[0]}")/lib/hookinvoke.sh" sentinel-receipt SubagentStop "" 2>/dev/null || true

INSTANCE="${CORE_INSTANCE:-${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}"
STATE_DIR="$INSTANCE/.claude/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

INPUT="$(cat 2>/dev/null || true)"

# FAIL-CLOSED on agent_type, exactly as reconcile-subagent-receipt.sh learned to on
# 2026-07-18: an empty or unexpected agent_type must NOT mint. The matcher in
# settings.json is configuration, not authentication, and is not trusted here.
AGENT_TYPE="$(printf '%s' "$INPUT" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("agent_type",""))
except Exception: print("")' 2>/dev/null || echo "")"
# A HOOK THAT DECLINES SILENTLY IS INDISTINGUISHABLE FROM A HOOK THAT WORKS.
#
# This fired 13 times in one session and minted nothing, and there was no way to tell:
# every path exited 0 with no output. Telemetry showed invocations, the tests passed, and
# the mechanism was inert. Now every non-mint path says which condition it failed on, into
# the same state dir, so "it ran" and "it did something" stop looking alike.
_decline() {
  printf '%s | sentinel-receipt DECLINED: %s | agent_type=%s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "${AGENT_TYPE:-<none>}" \
    >> "$STATE_DIR/.sentinel-receipt-declines.log" 2>/dev/null || true
  exit 0
}

# NOT-APPLICABLE IS NOT A DECLINE, AND CONFLATING THEM BURIED THE SIGNAL (2026-08-28).
#
# The comment above is right that a silent hook and a working hook look identical, and that is why
# every non-mint path logs. But SubagentStop fires for EVERY subagent in the system, and the
# not-a-sentinel branch used _decline, so the log filled with rows about agents this hook was never
# meant to act on.
#
# Measured when a real failure finally needed reading: 1,347 rows, of which 1,337 were
# "agent_type is not a sentinel agent" and TEN were genuine — five verdict-extraction failures,
# four missing VERDICT markers, one no-verdict-found. A 142 KB log in which 99.3% of the entries
# are no-ops, hiding the ten rows that mean a review was thrown away.
#
# That is the same shape as the dead counters this Core keeps finding: the record existed, was
# technically complete, and was unreadable at the moment it mattered. Keeping BOTH facts, in one
# file, under distinguishable labels, so the not-applicable rows still prove the hook fires.
#
# FORWARD-LOOKING ONLY, and the first draft of this comment overstated it. I wrote "`grep DECLINED`
# now returns only real failures" and then measured: it returns 1,343, because every historical row
# still carries the old label. The split applies to rows written from here on. History is left
# intact rather than relabelled — rewriting a log to make a claim true is worse than the claim
# being narrow. To read the real failures today: grep DECLINED and ignore rows whose reason is
# "agent_type is not a sentinel agent".
_not_applicable() {
  printf '%s | sentinel-receipt NOT-APPLICABLE: %s | agent_type=%s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "${AGENT_TYPE:-<none>}" \
    >> "$STATE_DIR/.sentinel-receipt-declines.log" 2>/dev/null || true
  exit 0
}

case "$AGENT_TYPE" in
  sentinel|sentinel-code) ;;
  *) _not_applicable "agent_type is not a sentinel agent" ;;
esac

# Extract the verdict from what Sentinel actually said. A receipt records the DECISION,
# not merely that the agent ran — "Sentinel ran" and "Sentinel approved" are different
# facts and conflating them is how a BLOCK becomes an approval.
# NOTE the absence of `|| true` and of `2>/dev/null`. Both were here and both defeated the
# diagnostic this block exists to produce: `|| true` forced $? to 0 so the decline branch
# below could never fire, and 2>/dev/null discarded the reason string itself. Errors are
# captured to the decline log instead of silenced — the whole failure being debugged was a
# hook that declined invisibly.
python3 - "$STATE_DIR" "$AGENT_TYPE" "$INPUT" 2>>"$STATE_DIR/.sentinel-receipt-declines.log" <<'PY'
import hashlib, json, os, secrets, sys, time

# INPUT arrives as argv[3], not stdin — stdin is the heredoc carrying this script.
state_dir, agent_type, raw = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)

report = (data.get("last_assistant_message") or "").strip()
if not report:
    print("DECLINE: no last_assistant_message in payload; keys=" + ",".join(sorted(data.keys())),
          file=sys.stderr)
    sys.exit(3)

# Verdict must appear as a standalone decision, not merely be mentioned. A review that
# discusses "this would be BLOCK if ..." has not returned BLOCK. Take the LAST explicit
# verdict line, which is where these agents put their conclusion.
# PARSE THE FORMATS THE AGENT ACTUALLY USES, not the one it was assumed to use.
#
# The first version matched only a bare "APPROVE" line. sentinel-code writes its verdict
# several ways depending on the review — "APPROVE", "`BLOCK`", "Verdict: **APPROVE**" —
# and the assumed-format matcher caught none of the decorated ones. Consequence: this hook
# fired 13 times in a session and minted ZERO receipts, so every real review silently
# failed to produce the approval it had earned, and the only receipts that ever existed
# were the synthetic ones in its own test. A verdict parser that only passes its own
# fixtures is not a verdict parser.
#
# Strategy: strip markdown decoration and an optional "verdict:" label, then require the
# REMAINING content to be exactly one verdict word. That accepts the real formats without
# accepting a verdict merely mentioned inside a sentence — "this would be BLOCK if..." has
# other words left over and is correctly ignored.
import re

def _verdict_of(line: str) -> str:
    """The verdict this line ASSERTS, or "" if it merely mentions one.

    The original required the whole line to reduce to exactly one verdict word. That is the
    right instinct — "this would be BLOCK if..." must not count — but it was too strict to
    survive real output. core-business measured it against what sentinel-code actually emits:

      "**ASK** — one specific gate needs Nick's explicit confirmation..."   -> ''
      "**ASK — unchanged.** `bash bin/sync-from-baseline.sh` does not..."   -> ''

    Every real verdict failed; every synthetic fixture passed. Their pulls were blocked all day
    with rc=4 "verdict extraction failed" while the reviewer had returned a perfectly good ASK.
    (On life the same parser happened to work, purely because my reviews put the verdict on its
    own line — the chain was one formatting choice away from being broken here too.)

    So the rule is now POSITIONAL rather than exclusive: the verdict must OPEN the line, after
    optional markdown decoration and an optional "Verdict:" label, and be followed by end-of-line
    or a separator — space-dash, em-dash, colon, comma, period. A verdict word appearing later in
    a sentence is still ignored, which is the property that mattered.
    """
    # A quoted / heading / list line is not this reviewer asserting anything — same rule as
    # _bare(). Without this, "> APPROVE" read as an assertion and locked line 1. (sentinel-code)
    if re.match(r"^\s*(?:>|#{1,6}\s|[-+*]\s)", line or ""):
        return ""
    s = line.strip()
    s = re.sub(r"^\s*(?:\*\*|__)+\s*", "", s)                                # inline emphasis
    s = re.sub(r"^\s*(?:final\s+)?verdict\s*[:\-—]\s*", "", s, flags=re.I)  # "Verdict:" label
    s = re.sub(r"^\s*(?:\*\*|__|`)+\s*", "", s)                             # decoration again
    up = s.upper()
    # (?![\w-]) not \b — \b would let "BLOCK-LISTED" count as BLOCK, which sentinel-code
    # showed could escalate a legitimate approval to a refusal ("BLOCK-listed patterns absent
    # from the diff"). Fails safe, but wrong, and it makes the parser fragile on ordinary prose.
    m = re.match(r"(APPROVE|BLOCK|ASK)(?![\w-])", up)
    if not m:
        return ""

    # WHAT MAY FOLLOW THE VERDICT: anything, EXCEPT another verdict word.
    #
    # My first fix required the remainder to start with punctuation. That parsed
    # "**ASK** — one specific gate..." and "BLOCK: trust-root touched", and REJECTED
    #
    #     "**APPROVE** for `bash bin/sync-from-baseline.sh` at baseline `61f0491`."
    #
    # because the next character is the "f" of "for". core-business caught it against the
    # shipped code at db04bc3, and named why it was the worst possible half to fix: ASK and
    # BLOCK change nothing — the action stays blocked either way, so their receipts are
    # cosmetic. APPROVE is the ONLY verdict that unblocks anything, and it was the one still
    # failing. I had fixed the harmless two and shipped it as a repair of the chain.
    #
    # The protection against a verdict merely MENTIONED comes from re.match anchoring at the
    # start of the line — "this would be BLOCK if..." never matches. It never came from the
    # separator. The one genuinely ambiguous shape is a line that OPENS with a verdict and
    # then names another ("APPROVE and BLOCK are both possible outcomes"), which is
    # enumeration rather than assertion. So that is the single exclusion.
    rest = up[m.end():]
    # ONLY the enumeration shape is excluded — a conjunction or list separator IMMEDIATELY
    # after the opening verdict, naming another one: "APPROVE and BLOCK are both possible".
    #
    # My previous version excluded ANY later mention, and sentinel-code proved that is
    # catastrophic. A refusal that explains itself by naming the alternative —
    #
    #     "BLOCK — Rule 1 violated, so it cannot be marked APPROVE."
    #
    # was DISCARDED ENTIRELY. It never entered the ranking, so a later line opening
    # "APPROVE is blocked pending confirmation" (whose "BLOCKED" does not match \bBLOCK\b)
    # became the only verdict in the report. A review that refused minted an approval.
    #
    # Verified before and after: that report yielded APPROVE, and now yields BLOCK.
    #
    # The rule is now: the word a line OPENS with is what that line asserts. Explaining your
    # refusal cannot erase it. Only genuine enumeration is set aside.
    if re.match(r"\s*(?:AND|OR|/|,|VS\.?|VERSUS)\s+(?:APPROVE|BLOCK|ASK)(?![\w-])", rest):
        return ""          # enumerating verdicts, not issuing one
    return m.group(1)

# AN APPROVAL MUST BE BARE. A REFUSAL MAY EXPLAIN ITSELF.
#
# Revision 6. Five previous versions, four of them BLOCKED by a reviewer, and every one failed
# the same way: I tried to decide whether ambiguous prose meant approval.
#
#   r1  line must reduce to one verdict word     real inline verdicts unreadable, chain dead
#   r2  allow a separator after the verdict      "APPROVE for X" still failed
#   r3  discard lines naming a 2nd verdict       a refusal explaining itself was ERASED
#   r4  narrow that exclusion                    "APPROVE is withheld" MINTED
#   r5  blocklist of negation words              six hedged approvals still MINTED:
#                                                "APPROVE — needs Nick's confirmation first."
#                                                "APPROVE, awaiting Nick's sign-off."
#                                                "APPROVE contingent on Nick's confirmation."
#                                                "APPROVE requires Nick's sign-off before use."
#                                                "APPROVE if Nick confirms."
#                                                "APPROVE — Nick still needs to confirm this."
#
# r5 was a BLOCKLIST, and a blocklist of ways to hedge is unbounded. sentinel-code found six in
# one pass; there are always more.
#
# So the rule is inverted and asymmetric:
#
#   APPROVE   line 1 must reduce to EXACTLY "APPROVE". Nothing after it. No fallback.
#   BLOCK/ASK line 1 may carry reasoning inline, and may also be found standing alone in the
#             body (the preamble-then-verdict shape sentinel-code called natural).
#
# This is not a new constraint — it is what both agent specs already require
# (sentinel.md:112, sentinel-code.md:148: "Line 1: APPROVE, BLOCK, or ASK"). A reviewer who
# writes "APPROVE for X" has not followed the format, and the answer is that they restate it,
# not that this code guesses. The cost of being strict is one re-review. The cost of guessing
# wrong is an unapproved push to a baseline three Cores and an external fork pull from.
#
# Refusals stay permissive because misreading one costs nothing: the action stays blocked
# either way.
# Helpers retained for the TRANSITIONAL fallback below. Block-level markers (">", "#", "- ")
# disqualify a line rather than being stripped from it: a quoted or listed verdict is someone
# else's words, not this reviewer asserting anything. (sentinel-code, r6 regression.)
_BLOCKLEVEL = re.compile(r"^\s*(?:>|#{1,6}\s|[-+*]\s)")


def _bare(line: str) -> str:
    """The verdict this line consists OF — nothing else on it, and not quoted or listed."""
    raw = (line or "")
    if _BLOCKLEVEL.match(raw):
        return ""
    t = re.sub(r"^\s*(?:\*\*|__|`)+\s*", "", raw.strip())
    t = re.sub(r"^\s*(?:final\s+)?verdict\s*[:\-—]\s*", "", t, flags=re.I)
    t = t.strip(" *_`—–.:!").upper()
    return t if t in ("APPROVE", "BLOCK", "ASK") else ""


# THE VERDICT IS A MACHINE-READABLE MARKER. PROSE IS NOT PARSED.
#
# Seven revisions tried to infer the verdict from a reviewer's prose. THREE of them would have
# minted an APPROVAL for a review that REFUSED. core-business named why the whole approach was
# doomed, and they are right:
#
#     "APPROVE for `bash bin/sync-from-baseline.sh`"     a verdict
#     "BLOCK is not warranted here"                      NOT a verdict
#     "ASK Nick to confirm at his convenience"           NOT a verdict
#
# Same shape — VERB, space, word. Any rule loose enough to accept the first accepts the others;
# any rule tight enough to reject the others rejects the first. That is exactly the oscillation
# across r2, r3 and r4. The information needed to tell them apart is NOT IN THE STRING, so no
# amount of regex could have worked. Four revisions each passing their own tests was the signal
# that the approach was wrong, not the implementation.
#
# So the contract changed instead of the parser. The agent specs now require a final line:
#
#     VERDICT: APPROVE
#
# and only that line is read. Everything above it is for the human — a reviewer can quote a
# prior verdict, discuss what approval would require, or name a rule that did not fire, and
# none of it can be mistaken for a decision.
_MARKER = re.compile(r"^[ \t]*VERDICT[ \t]*:[ \t]*(APPROVE|ASK|BLOCK)[ \t]*$", re.I)

# THE MARKER MUST BE THE LAST NON-BLANK LINE. Not "anywhere in the report".
#
# Scanning for it anywhere was a forgery vector, and sentinel-code minted a real receipt
# proving it: a reviewer who genuinely BLOCKS and then helpfully EXPLAINS the new format —
#
#     BLOCK -- Rule 2 failed: credential pattern found at deploy.sh:44.
#     Note: reviewers must now close with a marker line, e.g.:
#     ```
#     VERDICT: APPROVE
#     ```
#
# — mints APPROVE. The illustrative example becomes load-bearing. Worse, the agent spec I wrote
# in the same commit explicitly INVITES that prose ("you can quote a prior review, discuss what
# approval would require"), so this was not a hypothetical shape; it was one the contract asks
# for. My claim that the marker path was "strictly safer than r7 alone" was FALSE — under r7
# that same report correctly yields BLOCK.
#
# Anchoring to the last line matches what the spec already demands ("REQUIRED, LAST LINE") and
# makes the position, not the content, the thing that authorises. A fenced example in the middle
# of a report cannot be the last line, so it cannot forge anything. Escalation across multiple
# markers becomes moot — exactly one position is read.
_REVIEWED = re.compile(r"^[ \t]*REVIEWED[ \t]*:[ \t]*(\S.*?)[ \t]*$", re.I)
# Skip leading code-fence lines. A reviewer that opens with ``` would otherwise push the
# REVIEWED line out of first position, silently degrading to the heuristic fallback. That is
# fail-safe, not fail-open, but it is a false refusal in waiting and those get gates disabled.
def _outside_fence(text):
    """Lines a HUMAN READER would consider ordinary prose — everything else is not authoritative.

    THE FIRST VERSION OF THIS HELPER TOGGLED ON ANY LINE STARTING ``` OR ~~~, and Codex (routed here
    per codex-routing.md, read-only) showed that is not how fences close. Every mismatch below puts
    content the reader sees as QUOTED into the parser's authoritative set, which is the same
    authorisation bypass this helper was written to close, reachable by a different marker:

        ```                          reader: still fenced        old parser: CLOSED by the ~~~
        REVIEWED: <quoted example>
        ~~~

        ```                          reader: still fenced        old parser: CLOSED by ```bash
        ```bash

    So closing is now strict, per CommonMark: SAME character as the opener, at least as long, and
    nothing after it but whitespace. An info string (```bash) may only OPEN.

    Two more regions are excluded outright, conservatively, because a reader plainly reads them as
    quotation rather than statement:

        blockquotes       "> VERDICT: APPROVE" is the reviewer quoting, not deciding
        4-space indents   an indented code block, which `strip()` used to erase entirely

    Being conservative here is deliberate and one-directional: everything excluded in error costs a
    re-review, and everything INCLUDED in error can authorise a push.
    """
    import re as _re
    fence_char = None       # "`" or "~" while open
    fence_len = 0
    for raw in text.splitlines():
        ln = raw.expandtabs(4)
        m = _re.match(r"^( {0,3})([`~]{3,})(.*)$", ln)
        if fence_char is None:
            if m:
                fence_char, fence_len = m.group(2)[0], len(m.group(2))
                continue
            # Outside a fence: a blockquote or an indented code block is quotation, not statement.
            if _re.match(r"^ {0,3}>", ln) or _re.match(r"^ {4,}\S", ln):
                continue
            yield raw
        else:
            # Inside: only a same-character run at least as long, with nothing after it, closes.
            if m and m.group(2)[0] == fence_char and len(m.group(2)) >= fence_len \
                    and not m.group(3).strip():
                fence_char, fence_len = None, 0
            continue


_first_nb = next((ln for ln in _outside_fence(report) if ln.strip()), "")
_m_rev = _REVIEWED.match(_first_nb)
_reviewed_cmd = _m_rev.group(1).strip() if _m_rev else ""

# A PROSE PREAMBLE IS THE SAME DEFECT AS A CODE FENCE, and it is no longer hypothetical: on
# 2026-08-10 the reviewer opened with a summary sentence above its REVIEWED line in TWO of THREE
# reviews of `git push origin main` — after the contract had been moved to the top of its own spec
# AND restated prominently in the brief. Each time the receipt was written with an empty
# reviewed_command, the report-recovery fallback found no bindable tokens in a command whose every
# word is short and carries no path or flag, and a CORRECT APPROVE became unmintable.
#
# That is the "false refusal in waiting" the fence-skip above was added to prevent, arriving through
# a different door. Two of three correct approvals blocked is exactly how a gate gets disabled.
#
# THE WIDENING IS POSITIONAL, NOT PROSE-MATCHING, and the distinction is the whole safety argument.
# What v1 did — and what core-business demonstrated was exploitable — was SEARCH THE REPORT for the
# tokens of the command being approved, so dropping a flag made a command EASIER to bind. This does
# not search for the requested command at all. It reads a line the reviewer explicitly wrote to
# declare what it reviewed, and then the comparison against the requested command happens exactly
# where it did before, unchanged.
#
# EXACTLY ONE, or nothing. Multiple REVIEWED lines are ambiguous — a quoted example, a correction,
# two commands — and ambiguity here must fail toward refusal, never toward picking one. So a second
# match abandons the recovery and leaves reviewed_command empty, which is the pre-existing behaviour.
#
# FENCED BLOCKS ARE EXCLUDED FROM THE FALLBACK SCAN. Caught by the Sentinel reviewing this very
# change: the first-position path skips code fences, the fallback did not, so a `REVIEWED:` line
# inside a ``` block — a quoted example of the contract, which the spec itself contains — would have
# counted. It could not have minted a wrongful approval, because the downstream comparison against
# the requested command is unchanged and a mismatch still fails closed. But it could turn a report
# with one REAL line plus one QUOTED line into "two hits, refuse", which is a false refusal, and
# false refusals are the thing this whole change exists to remove.
#
# The reviewer called it "worth hardening later". Later is now: the asymmetry was introduced in the
# same edit, and a documented gap in a trust path is one nobody re-reads.
if not _reviewed_cmd:
    def _scan(skip_fences):
        hits, in_fence = [], False
        for ln in report.splitlines():
            if ln.strip().startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if skip_fences and in_fence:
                continue
            m = _REVIEWED.match(ln)
            if m:
                hits.append(m.group(1).strip())
        return hits

    # BOTH SCANS MUST AGREE, and this is the second version of this block — the first was BLOCKED by
    # the Sentinel reviewing it, which DEMONSTRATED the attack instead of asserting it.
    #
    # v1 skipped fenced lines and bound whenever exactly one hit remained OUTSIDE the fences. Given
    # a report whose fence markers desynchronise the toggle, the REAL line lands "inside" a fence and
    # is swallowed, while a different line lands outside and becomes the sole candidate:
    #
    #     ```                                   <- toggles ON
    #     REVIEWED: git push origin main        <- the real one, now "inside", skipped
    #     ```                                   <- toggles OFF
    #     REVIEWED: git push origin main --force   <- the only survivor, bound
    #
    # That is the flag-ESCALATION class core-business found in the v1 token matcher, reappearing in
    # the parser. Note it does not require an UNBALANCED fence — the markers above are balanced. Any
    # report where a REVIEWED line can be placed inside a fence and a variant outside it is enough,
    # so "count the fences and refuse if odd" does not close it either.
    #
    # The fix is to stop trusting fence structure to decide WHICH line counts. Scan twice — once
    # honouring fences, once ignoring them entirely — and bind only when both see exactly one hit and
    # it is the SAME string. Any report where the fence structure changes the answer is ambiguous by
    # construction, and ambiguity in a trust path fails toward refusal.
    #
    # THE COST, STATED: a report that quotes the contract in a fence AND states it for real now
    # refuses rather than binding, which the previous version handled. That is a false refusal, and
    # false refusals are what this whole block exists to reduce. It is accepted here because the
    # first-position path is unaffected — a reviewer that follows the contract never reaches this
    # code — and because a false refusal costs a re-review while a wrong bind can authorise a
    # force-push.
    _aware, _blind = _scan(True), _scan(False)
    if len(_aware) == 1 and len(_blind) == 1 and _aware[0] == _blind[0]:
        _reviewed_cmd = _aware[0]

# THE VERDICT IS READ OUTSIDE FENCES TOO, and this one is the worse of the pair. Fence state was
# ignored entirely here, so a report that REFUSED — "This push is UNSAFE. It contains a credential.
# I am blocking it." — and then showed an approving example minted APPROVE. Dosed against this hook
# before the fix: verdict='APPROVE', from a review whose prose was a refusal. The reviewer's actual
# decision was discarded in favour of its own illustration.
#
# A report written ENTIRELY inside a fence now yields no verdict line and falls through to the
# recovery path below, which is the safe direction: no verdict is a refusal to mint, not a mint.
_lines = [ln for ln in _outside_fence(report) if ln.strip()]
_m = _MARKER.match(_lines[-1]) if _lines else None
if _m:
    verdict = _m.group(1).upper()
else:
    # ⚠ DO NOT REMOVE THIS FALLBACK ON THE SIGNAL "the specs now carry the marker".
    #
    # THAT SIGNAL IS WRONG AND REMOVING IT WOULD KILL THE CHAIN FLEET-WIDE.
    #
    # Agent definitions are read into the registry at SESSION START. A mid-session edit to
    # .claude/agents/sentinel*.md does NOT reach subagents spawned in that same session.
    # core-business proved it and I reproduced it: with the marker subsection sitting on disk,
    # a freshly spawned sentinel asked about its own instructions answered
    #
    #     "NOT PRESENT"
    #
    # and quoted the pre-edit Output Format section back. So as of this commit the marker path
    # has NEVER been exercised on either Core — every receipt minted today, including
    # core-business's first-ever one, came through THIS fallback.
    #
    # The correct removal condition is therefore not "specs updated" but:
    #
    #     every Core has STARTED A NEW SESSION since its specs were updated, AND a receipt
    #     has been observed minting via the marker path in that new session.
    #
    # Until both hold, this fallback is not transitional — it is the only working path, and
    # deleting it would block every push and pull everywhere, with no way to fix it, because
    # the fix itself needs a review to land.
    #
    # r7's rules are what run here: the hardest-tested prose reader, APPROVED by sentinel-code
    # after hostile execution against homoglyphs, zero-width characters, CRLF, BOM, fenced
    # code, tables and nested quotes.
    _first = next((ln for ln in report.splitlines() if ln.strip()), "")
    verdict = ""
    _bare_first = _bare(_first)
    if _bare_first:
        verdict = _bare_first
        if verdict == "APPROVE":
            _refusals = {v for v in (_verdict_of(ln) for ln in report.splitlines())
                         if v in ("BLOCK", "ASK")}
            if _refusals:
                print("DECLINE: line 1 is APPROVE but the report also refuses "
                      + str(sorted(_refusals)) + "; refusing to mint. line1=" + _first[:120],
                      file=sys.stderr)
                sys.exit(4)
    else:
        _inline = _verdict_of(_first)
        if _inline in ("BLOCK", "ASK"):
            verdict = _inline
        elif _inline == "APPROVE":
            print("DECLINE: line 1 opens APPROVE but carries other text, and no VERDICT: marker "
                  "was found. line1=" + _first[:160], file=sys.stderr)
            sys.exit(4)
    if not verdict:
        _found = {v for v in (_verdict_of(ln) for ln in report.splitlines())
                  if v in ("BLOCK", "ASK")}
        if len(_found) == 1:
            verdict = _found.pop()
        elif len(_found) > 1:
            print("DECLINE: report states more than one refusal " + str(sorted(_found))
                  + "; refusing to guess. line1=" + _first[:120], file=sys.stderr)
            sys.exit(4)
    if not verdict:
        print("DECLINE: no VERDICT: marker and no verdict on line 1. line1=" + _first[:200],
              file=sys.stderr)
        sys.exit(4)

nonce = secrets.token_hex(8)
receipt = {
    "nonce": nonce,
    "agent_type": agent_type,
    "verdict": verdict,
    "session_id": data.get("session_id") or "",
    "ts": int(time.time()),
    "report_sha256": hashlib.sha256(report.encode("utf-8", "replace")).hexdigest(),
    "report_head": report[:400],
    # COMMAND BINDING (2026-08-08). The receipt recorded WHO reviewed and WHAT THEY DECIDED but
    # never WHAT THEY REVIEWED. sentinel-approve.sh then selected the newest fresh APPROVE receipt
    # and used the command only to NAME the output token file — so any APPROVE inside the 900s TTL
    # could mint a token for a DIFFERENT command. Review A, approve B.
    #
    # This is not hypothetical and it is not new. The brain holds the incident verbatim:
    #   "Sentinel approval is per-EXACT-URL, not per-intent — never run sentinel-approve.sh on a
    #    URL Nick hasn't seen. I ran it on a URL Nick hadn't approved. Nick caught me. Stale token
    #    nuked."
    # Nick caught it BEHAVIOURALLY in 2026-05 and the structural hole was never closed.
    #
    # The SubagentStop payload carries only last_assistant_message and session_id — the brief never
    # reaches this hook — so the command cannot be recorded from the harness. It can be recovered
    # from the REPORT, because every reviewer quotes the command it reviewed. Storing a bounded
    # normalised copy lets sentinel-approve.sh require that the command it is asked to approve
    # actually appears in the review that authorises it. A reviewer that does not quote the command
    # yields a refusal, which is the safe direction and the law this repo now runs on: fail toward
    # UNDECIDABLE, never toward PASS.
    "report_text": report[:8000],
    # THE REVIEWED COMMAND, PARSED POSITIONALLY — never inferred from prose.
    #
    # This repo already solved "do not infer from prose" once, for the verdict: _MARKER reads ONE
    # line in ONE required position and refuses to guess. core-business pointed out that the same
    # mechanism was sitting in this file unused for the COMMAND, while sentinel-approve searched
    # the whole report for it. That search failed three ways, all demonstrated by execution:
    # flag-stripping, argument-dropping, and — worst — a reviewer quoting a command it explicitly
    # DISCLAIMED, which minted a token for it. The hole widened with reviewer quality.
    #
    # First non-blank line, exact form, or nothing. Position authorises, not content.
    "reviewed_command": _reviewed_cmd,
}
path = os.path.join(state_dir, f".sentinel-receipt-{nonce}.json")
tmp = path + ".partial"
with open(tmp, "w") as fh:
    json.dump(receipt, fh, indent=2)
os.replace(tmp, path)
PY
_PYRC=$?
if [[ "$_PYRC" -ne 0 ]]; then
  _decline "verdict extraction failed (rc=$_PYRC)"
fi
exit 0
