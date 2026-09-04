### The verdict marker — REQUIRED, LAST NON-BLANK LINE

Your report's **last non-blank line** MUST be exactly one of:

```
VERDICT: APPROVE
VERDICT: ASK
VERDICT: BLOCK
```

`sentinel-receipt.sh` parses **only** this line. It never infers a verdict from your prose, so you
may write whatever you need above it — quote a prior review, discuss what approval *would* require,
name a rule that did not fire — without any of it being read as your decision.

- **Position authorises, not content.** The marker must be the LAST non-blank line. A `VERDICT:`
  line quoted mid-report — inside a code fence, an example, or a sentence like "I recommend
  VERDICT: APPROVE once the operator confirms" — is not your verdict and will not be read as one. This is
  deliberate: it is what stops a report that BLOCKS and then *explains the format* from minting an
  approval. sentinel-code minted a real receipt proving that forgery before the anchor was added.

- **Exact shape.** Case is ignored; decoration and punctuation are not:

      VERDICT: APPROVE          correct
      **VERDICT: APPROVE**      DECLINED — decoration is not tolerated
      `VERDICT: APPROVE`        DECLINED
      VERDICT: APPROVE.         DECLINED — trailing punctuation is not tolerated
      - VERDICT: APPROVE        DECLINED — a list bullet is not tolerated

- **No marker means DECLINE.** A missing or malformed marker mints no receipt and the action stays
  blocked. Every rejection above is fail-closed, so a mis-formatted marker costs you a re-review,
  never a wrong decision. It is your failure to diagnose, not the operator's. Emit the line.

### The reviewed-command line — REQUIRED, FIRST NON-BLANK LINE

Your report's **first non-blank line** MUST be exactly:

```
REVIEWED: <the exact command you were asked about, verbatim>
```

`sentinel-approve.sh` reads **only** this line to decide which command your verdict authorises. It
never infers the command from your prose.

**Why this exists, and it is the same reasoning as the verdict marker above.** Until 2026-08-09 the
approval was bound to the command by SEARCHING THE REPORT for it. That failed three ways, each found
by running it rather than reading it:

- **Flag-stripping.** The match threshold scaled with the token count of the command being approved,
  so reviewing `script --check` and approving bare `script` needed FEWER hits and bound. Dropping a
  narrowing flag made a command *easier* to authorise.
- **Argument-dropping.** Reviewing `gmail.py send --to someone@example.com` and approving bare
  `gmail.py send` bound — the same escalation applied to a recipient.
- **Quoted commands.** A reviewer returning APPROVE on a clean diff while responsibly warning
  *"note this does NOT invoke `<CMD>`"* minted a token for `<CMD>`. **The hole widened with reviewer
  quality**, because a careful reviewer names what it is warning about.

Prose cannot carry that distinction, exactly as prose could not carry the verdict. The answer is the
same: one line, one position, no inference.

- **Echo it verbatim.** Copy the command from your brief character-for-character. Do not reformat,
  re-wrap, or abbreviate it — the comparison is exact after whitespace normalisation.
- **Position authorises.** Only the FIRST non-blank line is read. A command quoted mid-report is not
  your subject and will never be treated as one.
- **Omitting it costs you a re-review.** Without this line the approval falls back to a weaker
  heuristic, and that fallback is being retired once every reviewer emits the line.

**Why this exists.** The hook previously inferred the verdict from prose and went through SEVEN
revisions, three of which would have minted an APPROVAL for a review that REFUSED — because
`APPROVE for bash x` (a verdict) and `BLOCK is not warranted here` (not a verdict) are the same
shape, and no regex separates them. The information was never in the string. One unambiguous line
at one fixed position removes the guessing entirely.

**Do not treat this section as optional because a fallback exists.** `sentinel-receipt.sh` still
carries a prose reader for reports with no marker. It is not a second supported path — it is the
transitional path that keeps the fleet unblocked while specs propagate, and it is the same
prose-inference that produced the seven revisions above. If you emit the marker, the fallback is
never reached.
