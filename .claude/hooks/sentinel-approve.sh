#!/usr/bin/env bash
# sentinel-approve.sh — Write a one-time Sentinel approval token.
#
# Usage:
#   bash sentinel-approve.sh "<exact command string Sentinel approved>"
#
# The pretooluse-guard.sh hook SHA-256-hashes the command it intercepts.
# This script hashes the same string and drops a token file the hook will
# consume (one-time, 120-second TTL). The strings must match exactly.
#
# Called by Core immediately after Sentinel returns APPROVE.
set -uo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$(dirname "$HOOKS_DIR")/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# LIVENESS ROW — OBSERVABILITY ONLY, NOT A TRUST-SEMANTICS CHANGE (2026-08-12, DOSE 35).
#
# This script had ZERO rows in hook-events.log. Not zero invocations logged — zero rows of any kind,
# so nothing anywhere recorded that the token minter had ever run. estate-sweep PROTECTS this file on
# the reasoning that quietness is what a working guard looks like; quietness is equally what an
# absent one looks like, and for the component that mints trust-root tokens those are the two states
# most worth telling apart.
#
# THE DISTINCTION I AM DRAWING, because the standing decision on this file is "do NOT patch the trust
# root, in either direction" (decisions-log 2026-08-12, re: narrowing _peer_approval). That decision
# is about AUTHORITY — what may mint a token, and for whom. This changes none of it: the row is
# written before any argument is examined, reads and writes nothing the minting path consults,
# cannot mint or withhold a token, and is `|| true` throughout so a telemetry failure cannot alter
# the outcome. If that reasoning is wrong, the right response is to revert this hunk alone; it is
# self-contained and touches no decision.
#
# NO PER-SESSION DEDUPE HERE, unlike the PreToolUse guard. This runs only when Core explicitly mints
# an approval — a handful of times a session at most — so every invocation is signal, and collapsing
# them would discard the count of how often the trust root was exercised.
CORE_INSTANCE="${CORE_INSTANCE:-$(dirname "$(dirname "$HOOKS_DIR")")}" \
  python3 - "$HOOKS_DIR/lib" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, sys.argv[1])
try:
    import hooklog
    hooklog.log("sentinel-approve", "Manual", verdict="invoke",
                trigger="token mint attempted — the minter ran")
except Exception:
    pass
PY

if [[ $# -lt 1 ]]; then
  echo "Usage: sentinel-approve.sh \"<command>\"" >&2
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# THERE IS NO SELF-SERVICE PATH FOR A TRUST-ROOT CHANGE. THIS IS DELIBERATE.
#
# sentinel-code's Rule 1 returns ASK — never APPROVE — for any trust-root change, so an
# APPROVE receipt cannot exist for exactly the changes that most need human review. That
# leaves a real gap: Nick reviews, Nick agrees, and there is no way for that to become a
# token.
#
# Two attempts at closing it were built and both were rejected, the second by measurement
# rather than opinion:
#
#   1. A --human-confirmed flag taking a free-text quote. sentinel-code called it "a
#      deliberate, articulate, well-engineered new bypass" — correctly. The quote was a
#      string Core supplied to itself, so "Nick confirmed" and "Core decided" were the
#      same keystrokes.
#
#   2. The same flag, but verifying the quote against the session transcript so it had to
#      be something Nick actually said. Scoped to this Core, the current transcript, a
#      30-minute window, and a 25-character minimum fragment.
#
#      That one died on the data. Nick's real approvals are SHORT — "go", "do 1", "yes".
#      A fragment long enough to be specific rejects the way he actually approves; one
#      short enough to accept it matches ordinary conversation and authorizes nothing in
#      particular. The mechanism cannot separate "approved THIS command" from "said a
#      short thing recently", because the input it depends on does not carry that
#      distinction.
#
# So the answer is not a cleverer check. It is that a trust-root push is a command NICK
# RUNS HIMSELF — in his own terminal, with `! git push ...`. Core cannot type there, which
# makes it the one confirmation that is genuinely un-forgeable rather than merely
# inconvenient to forge. It costs one command on a rare action.
#
# The receipt path below is unaffected and remains the route for everything else: a real
# Sentinel run returning APPROVE mints exactly one token.
# ─────────────────────────────────────────────────────────────────────────────

if [[ "$1" == "--last" ]]; then
  LAST_FILE="$STATE_DIR/.sentinel-last-blocked"
  if [[ ! -s "$LAST_FILE" ]]; then
    echo "sentinel-approve.sh --last: no .sentinel-last-blocked found" >&2
    exit 1
  fi
  CMD="$(cat "$LAST_FILE")"
else
  CMD="$1"
fi
# HASHED OVER THE NORMALISED COMMAND, NOT THE RAW ONE. Codex, 2026-08-09: the approval
# MATCHER normalises (quotes, case, whitespace, trailing comment) but the replay ledger hashed the
# RAW string — so `bash bin/x.sh` and `bash "bin/x.sh"` matched the SAME approval while writing
# DIFFERENT ledger files. One peer approval could mint repeatedly, once per spelling, for the whole
# TTL. The two normalisations must be the same normalisation or replay protection is decorative.
HASH=$(printf '%s' "$CMD" | python3 -c '
import re, sys
s = re.sub(r"\s+#.*$", "", sys.stdin.read().strip()).replace(chr(34), "").replace(chr(39), "")
sys.stdout.write(re.sub(r"\s+", " ", s).strip().lower())
' | shasum -a 256 | awk '{print $1}')
TOKEN_FILE="$STATE_DIR/.sentinel-approved-$HASH"

# ─────────────────────────────────────────────────────────────────────────────
# A TOKEN NOW REQUIRES A RECEIPT — Sentinel must actually have run, and approved.
#
# Until 2026-07-27 this script did `touch "$TOKEN_FILE"` and nothing else. The guard
# checked only that the file existed. So the gate protecting every outward action could
# be satisfied by the agent it was guarding, with one Bash call and no review. This Core
# did exactly that twice in one session — once deliberately to demonstrate it, and once
# in good faith, having decided a review was unnecessary. The guard could not tell the
# difference, because there was no difference to see.
#
# Now: sentinel-receipt.sh (SubagentStop) writes a receipt when the sentinel or
# sentinel-code subagent terminates, recording WHAT IT DECIDED. This script consumes one
# APPROVE receipt per token, atomically, and refuses without one.
#
# HONEST LIMIT, stated here because the README states it too: an agent with Bash under
# this UID can write a receipt file directly. No local mechanism survives that — not
# HMAC (it can read the key), not a "hook-only" path (if a hook can write it, Bash can).
# This is an ACCIDENT AND DRIFT BARRIER. It makes skipping review require deliberate
# forgery instead of a judgement call, which is precisely the failure that occurred.
# Calling it a security boundary would be the overclaim; making it harder to drift past
# is the real and worthwhile thing.
# ─────────────────────────────────────────────────────────────────────────────
RECEIPT_TTL="${SENTINEL_RECEIPT_TTL:-900}"   # 15 min — a review older than this is stale
# ── PEER APPROVAL: THE SECOND SEAT IS THE CONFIRMATION (2026-08-09) ──────────────────────────
#
# The operator's requirement: two Cores need to be able to work all night — updating, pushing,
# testing — without friction and without the operator present.
#
# The header above concludes there is no self-service path for a trust-root change, and it is right
# about SELF. Two prior attempts and one of mine tonight all failed the same way: Core supplied the
# confirmation to itself, so "the operator confirmed" and "Core decided" were the same keystrokes.
#
# THE PROPERTY THAT WAS MISSING IS NOT A CLEVERER CHECK. IT IS A SECOND PARTY. core-business is a
# different process, in a different session, in a different repo, that life does not write. Tonight
# it produced things life could not: it demonstrated the flag-strip exploit, refuted life's
# "structurally dead" claim, found that a report quoting a command it DISCLAIMED still minted a
# token for it, and caught a heredoc rule that cleaved the wrong way. Five of six measurement
# defects tonight were only visible from a non-authoring seat. That is not collegiality, it is a
# structural property, and it is the one thing available here that self-approval never had.
#
# So: a trust-root push may proceed on an APPROVE FROM A DIFFERENT CORE, delivered over core-bus,
# naming this exact command.
#
# WHAT MAKES IT MEANINGFULLY DIFFERENT FROM THE REJECTED ATTEMPTS: life cannot author it. peer-msg
# send() now refuses to post as another Core (added the same hour as this), so the sanctioned path
# cannot produce a business-attributed approval from life.
#
# SCOPE: PUSHES ONLY. A peer approval cannot authorise a PULL — on a pull the approver is the AUTHOR
# of the content, which is one seat wearing the label of two. Enforced in code at the mint site, not
# here. core-business found this within minutes of the mechanism being proven, while holding three
# broken rules of its own that this exact path would have fixed, and declined to use it.
#
# TWO LIMITS THAT ARE LOAD-BEARING AND DO NOT LOOK IT:
#
#   THE REPLAY LEDGER IS AN ORDINARY DELETABLE FILE. `.peer-approval-consumed-<hash>` lives in
#   .claude/state/, is in no sync manifest, and is guarded by nothing. Delete it and the same
#   approval mints again inside its TTL. So it bounds ACCIDENTAL reuse — re-running a command and
#   silently consuming a stale yes, which is the failure that actually happens — and bounds
#   deliberate reuse not at all. Do not mistake it for a security control.
#
#   THE 1800s TTL IS DOING TWO JOBS AT ONCE. It is the only thing making a stale approval inert AND
#   the only real bound on replay once the ledger is understood to be deletable. Observed live on
#   2026-08-09: approvals #847 and #851 were genuine and correctly refused to mint at ~3000s. A
#   future change raising this "for convenience" would silently weaken two properties, not one.
#
# WHAT A PEER APPROVAL ACTUALLY BINDS TO — core-business, 2026-08-09, verified through mint AND
# consume. The chain matches: sender != me, sender in the roster, a VERDICT: APPROVE line, a
# REVIEWED line equal to this command, and a TTL. THERE IS NO SHA, NO DIFF HASH AND NO FILE LIST
# ANYWHERE IN IT.
#
# So "a peer approved the push" means THE PEER'S sentinel-code WAS TRUSTED TO HAVE REVIEWED
# TRUST-ROOT CONTENT — it does NOT mean a second Core reviewed THIS diff. Those read the same in a
# summary and they are not the same guarantee. Nick must not read the stronger one, and the earlier
# phrasing here invited it.
#
# Not a hole: it is inside the stated threat model, and the TTL bounds how stale the reviewed state
# can be. It is a claim-size correction, which is the kind this session kept having to make.
#
# HONEST LIMIT, stated because every prior attempt here failed by overclaiming: thread.jsonl is a
# plain file at the same UID, so a Core determined to forge could append to it directly, bypassing
# peer-msg entirely. This is NOT cryptographic. What it is: the accidental and careless escalation
# that has actually happened — twice tonight, once in May — becomes impossible through every path
# anyone would take by mistake, and deliberate forgery requires knowingly circumventing the tool.
# Nick has the full statement of that trade; this is his call, not an inference.
_peer_approval() {
  python3 -c '
import json, os, re, sys, time, unicodedata
me, cmd, ttl = sys.argv[1], sys.argv[2], int(sys.argv[3])
thread = os.path.expanduser("~/AI Projects/core-bus/thread.jsonl")
if not os.path.isfile(thread):
    sys.exit(0)
def norm(s):
    s = re.sub(r"\s+#.*$", "", str(s or "").strip()).replace(chr(34), "").replace(chr(39), "")
    return re.sub(r"\s+", " ", s).strip().lower()

# The declared fleet. A sender outside it is not a second seat, whatever it calls itself.
KNOWN_CORES = {"life", "business", "school", "finance", "ops"}

def norm_id(s):
    """Canonical form of a Core name — NFKC, stripped, casefolded, zero-width removed.

    Codex 2026-08-09: identities were compared as raw strings, so " life", "Life" and unicode
    confusables all read as "not me" and self-approved. Canonicalising is only half the fix; the
    roster above is the other half, because normalisation alone still accepts any invented name.
    """
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")   # zero-width / directional
    return s.strip().casefold()

want = norm(cmd)
now = time.time()
best = None
near = []   # named THIS command but failed a later requirement — logged, never silent
for line in open(thread, errors="ignore"):
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if norm_id(d.get("to")) != norm_id(me):
        continue
    frm = d.get("from") or ""
    # IDENTITY IS COMPARED CANONICALLY AND AGAINST A ROSTER. Codex, 2026-08-09: exact string
    # comparison rejected only the literal `me`, so "Life", " life", "life " and any unicode  # privacy-ok: generic engineering vocabulary
    # confusable self-approved, and an arbitrary sender like "review-bot" passed too — the check
    # established "a different string", not "a different Core". Roster membership is what the
    # property actually requires, and it is derivable: the Cores are declared, not free text.
    if not frm or norm_id(frm) == norm_id(me):        # a Core cannot approve itself
        continue
    if norm_id(frm) not in KNOWN_CORES:               # ...nor may a non-Core approve at all
        continue
    txt = d.get("text") or ""
    # REVIEWED FIRST, THEN VERDICT — the order is the whole point of the near-miss log.
    #
    # These were the other way round, so a message that NAMED this exact command but lacked the
    # anchored VERDICT line was discarded before anything knew it was about this command. That is
    # precisely what core-business hit twice: correct decision, correct command, no trace at either
    # end. Establishing relevance FIRST is what makes a rejection reportable — you cannot say "this
    # approval was unusable" until you know it was an approval OF THIS THING.
    m = re.search(r"(?mi)^\s*REVIEWED\s*:\s*(\S.*?)\s*$", txt)
    if not m or norm(m.group(1)) != want:
        continue
    if not re.search(r"(?mi)^\s*VERDICT\s*:\s*APPROVE\s*$", txt):
        near.append((d.get("seq"), frm,
                     "names this command but has no standalone VERDICT:APPROVE line"))
        continue
    try:
        ts = time.mktime(time.strptime(str(d.get("ts"))[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        near.append((d.get("seq"), frm, "unparseable timestamp %r" % (d.get("ts"),)))
        continue
    if now - ts > ttl:
        near.append((d.get("seq"), frm, "expired: %d min old, TTL is %d min"
                     % ((now - ts) / 60, ttl / 60)))
        continue
    if best is None or ts > best[0]:
        best = (ts, frm, d.get("seq"), txt)
if best:
    # STDOUT CONTRACT UNCHANGED. PEER_OK is written into the access log and the replay ledger, so
    # adding a field here would silently change both. The digest goes to a sidecar file instead —
    # the caller reads it only for the flagged form.
    try:
        dm = re.search(r"(?mi)^[ \t]*DIGEST[ \t]*:[ \t]*([0-9a-f]{64})[ \t]*$", best[3])
        side = os.path.join(os.path.expanduser("~"), ".claude", "peer-approval-digest")
        with open(side, "w") as fh:
            fh.write((dm.group(1) if dm else "") + "\n")
    except Exception:
        pass
    print("%s:bus#%s" % (best[1], best[2]))
elif near:
    # A SEEN-BUT-REJECTED APPROVAL MUST LEAVE A TRACE. core-business asked directly (bus #1023)
    # whether a malformed peer approval logs anything here, having watched two of its own correct
    # approvals vanish. The answer was NO: the loop `continue`d past every near-miss and produced no
    # artifact anywhere, so the failure was SILENT AT BOTH ENDS — silent where it was emitted and
    # silent where it was read. That is why it took two rounds and a written contract to notice.
    #
    # Its own framing: a refusal that produces no signal at the emitting end is indistinguishable
    # from success. Both ends is worse, because there is then nothing for either party to read.
    #
    # Only near-misses are recorded — a message that named a DIFFERENT command is not a rejected
    # approval, it is an unrelated message, and logging those would bury the signal.
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude",
                               "peer-approval-near-misses.log"), "a") as fh:
            for seq, frm, why in near:
                fh.write("%s | bus#%s from %s | %s | cmd=%s\n"
                         % (time.strftime("%Y-%m-%dT%H:%M:%S"), seq, frm, why, cmd[:120]))
    except Exception:
        pass
    for seq, frm, why in near:
        sys.stderr.write("peer-approval: bus#%s from %s named this command but was NOT usable — %s\n"
                         % (seq, frm, why))
' "$1" "$2" "$3"
}
# NO `2>/dev/null` ON THAT CALL ANY MORE. It hid a break I introduced minutes ago: a single quote
# inside the single-quoted bash body terminated the string, the truncated script died, and the
# mint reported "no fresh APPROVE receipt" — indistinguishable from a peer who never approved.
# A trust-path scan that swallows its own errors cannot tell "no approval" from "I am broken", and
# the whole evening has been about exactly that distinction. A missing bus already exits 0 on its
# own, so the suppression was buying nothing and costing the difference.


# SECOND SEAT FIRST. If a different Core has reviewed THIS EXACT command and posted APPROVE on the
# bus, that is the confirmation — no local receipt needed, and no Nick needed. This is what makes
# unattended overnight work possible without reintroducing self-approval.
PEER_OK="$(_peer_approval "$(python3 -c 'import sys,os;sys.path.insert(0,os.path.expanduser("~/AI Projects/core-bus"));import importlib.util as i;s=i.spec_from_file_location("pm",os.path.expanduser("~/AI Projects/core-bus/peer-msg.py"));m=i.module_from_spec(s);s.loader.exec_module(m);print(m.whoami() or "")' 2>/dev/null)" "$CMD" 1800)"

CONSUMED=$(python3 - "$STATE_DIR" "$RECEIPT_TTL" "$CMD" <<'PY'
import glob, json, os, re, sys, time
state_dir, ttl = sys.argv[1], int(sys.argv[2])
# argv[3] is the command being approved. It was NOT passed in before this change,
# which is the mechanical reason the matcher could not bind: it never saw the command.
CMD = sys.argv[3] if len(sys.argv) > 3 else ""
now = int(time.time())
# Words that turn a quoted command into one the reviewer is NOT authorising. This is a
# HEURISTIC and it will rot, which is exactly the criticism I levelled at core-business's
# negative-lookaround earlier tonight. It is defence in depth, not the guarantee -- see the
# HONEST LIMIT note on _cmd_in_report.
_NEGATED = re.compile(r"\b(not|never|without|decline[ds]?|declining|refus\w*|instead of|"
                      r"rather than|replaced|replaces|removing|removed|avoid\w*|no longer)\b"
                      r"[^.]{0,80}$", re.I)

_ARGISH = re.compile(r"^(--?[A-Za-z0-9][\w-]*|[A-Za-z0-9_./~-]*/[A-Za-z0-9_./~-]+|[\w.-]+@[\w.-]+)$")


def _cmd_in_report(cmd, report):
    """True only if the review authorises THIS EXACT command and nothing broader.

    v1 COUNTED TOKENS AND WAS EXPLOITABLE. core-business demonstrated it rather than arguing it:
    the threshold was hits >= max(1, min(2, len(toks))), which scales with the token count of the
    command BEING APPROVED, not the one that was REVIEWED. So

        reviewed:  script --check   -> 2 tokens -> needed 2 hits
        approved:  script           -> 1 token  -> needed 1 hit   -> BOUND

    DROPPING A FLAG MADE A COMMAND EASIER TO BIND. And the read-only variant is exempt from the
    guard by design, so an approving review of the safe form was trivially obtainable and then
    minted for the real one. That inverts the entire point.

    ITS PROPOSED FIX -- require ALL tokens of the approved command -- DOES NOT CLOSE IT, because
    the narrow command is a strict PREFIX of the broad one: every token of "script" appears in a
    review of "script --check". Token counting cannot separate them in either direction.

    MY OWN SECOND ATTEMPT ALSO FAILED, on a case neither of us had listed. It anchored on the last
    DISTINCTIVE token, so for "python3 gmail.py send" it inspected what followed "gmail.py" (the
    word "send") rather than what followed the whole command -- and approving that against a review
    of "python3 gmail.py send --to example@example.com" passed. Dropping the RECIPIENT is the same
    escalation as dropping a flag and it is worse.

    SO: anchor on the WHOLE command, whitespace-flexibly, and inspect what follows THAT. If the
    reviewed invocation carried a further argument, the review was of a NARROWER action than the
    approval, and it refuses.

    Approving something NARROWER than reviewed is refused too. "Narrower" cannot be judged from
    argument shape -- --check is harmless, --force is not, and nothing here tells them apart.
    Per-EXACT-command, which is what the brain recorded in May: approval is per-EXACT-URL, not
    per-intent.

    HONEST LIMIT, stated here so nobody later mistakes this for the intended design. Recovering
    the reviewed command FROM THE REPORT is a workaround for a missing platform field: the
    SubagentStop payload carries only last_assistant_message and session_id, so the brief -- which
    names the command -- never reaches the receipt hook. Report-recovery raises the cost of
    CARELESS and ACCIDENTAL escalation, which is what actually happened in May and again tonight.
    It is NOT sound against a deliberately adversarial parent, because Core mediates the whole
    channel: it composes the brief, it reads the verdict, and it invokes this script. Any binding
    Core constructs, Core can misconstruct.

    The guarantee for trust roots is the one this file's header already reached and that two
    rejected attempts confirmed: Nick runs those commands himself, because Core cannot type in his
    terminal.
    """
    cmd_n = re.sub(r"\s+", " ", (cmd or "").strip())
    rep = report or ""
    if not cmd_n or not rep:
        return False
    toks = [x for x in re.findall(r"[A-Za-z0-9_./@:-]{2,}", cmd_n)
            if not x.isdigit() and x not in ("bash", "python3", "sudo", "sh", "env")]
    toks = [x for x in toks if "/" in x or "." in x or x.startswith("-") or "@" in x or len(x) >= 8]
    if not toks:
        return False                       # an unbindable command must never pass

    pat = r"\s+".join(re.escape(p) for p in cmd_n.split(" "))
    for m in re.finditer(pat, rep, re.I):
        tail = rep[m.end():].lstrip()
        nxt = tail.split(None, 1)[0] if tail else ""
        nxt = nxt.strip(",.;:)\"'")
        if nxt and _ARGISH.match(nxt):
            continue                       # reviewed WITH an argument the approval drops
        # A REPORT QUOTES COMMANDS IT IS NOT APPROVING. core-business's lead, which reproduced on
        # all three of its cases: a reviewer can return APPROVE for a diff while quoting a command
        # it explicitly did NOT approve --
        #     "note it does NOT invoke <CMD>"  /  "I am declining <CMD>"  /  "replaced <CMD> with"
        # -- and mere presence bound all three. Look back at the preceding words for a negation or
        # decline before treating an occurrence as authorising.
        before = rep[max(0, m.start() - 90):m.start()].lower()
        if _NEGATED.search(before):
            continue
        return True
    return False


best = None
for p in sorted(glob.glob(os.path.join(state_dir, ".sentinel-receipt-*.json"))):
    try:
        r = json.load(open(p))
    except Exception:
        os.unlink(p)          # unreadable receipt is not evidence of anything
        continue
    age = now - int(r.get("ts", 0))
    if age > ttl:
        os.unlink(p)          # stale — a review from an hour ago did not see this command
        continue
    if r.get("verdict") != "APPROVE":
        continue              # BLOCK and ASK are left in place; they are not approvals
    # THE APPROVAL MUST BE FOR *THIS* COMMAND.
    #
    # Until now CMD/HASH were used ONLY to name the output token file — they played no part in
    # selecting which receipt to consume. The matcher took the newest fresh APPROVE, full stop. So
    # reviewing command A and then invoking this script with command B minted a valid token for B,
    # inside the 900s TTL, with no forgery and no error. The brain already held the incident from
    # 2026-05: "Sentinel approval is per-EXACT-URL, not per-intent... I ran sentinel-approve.sh on a
    # URL Nick hadn't approved. Nick caught me." Caught behaviourally, never fixed structurally.
    #
    # Binding is against the REPORT because the brief never reaches the receipt hook. Every reviewer
    # quotes the command it reviewed; a distinctive token from the command must appear in that
    # review. No match -> this receipt does not authorise this command -> keep looking, then refuse.
    # NO REPORT MEANS NO BINDING MEANS NO APPROVAL. This used to SKIP the check when a receipt
    # carried no report, so a schema migration could not wedge the fleet mid-rollout.
    # core-business retired that justification by arithmetic: RECEIPT_TTL is 900 seconds, so every
    # pre-change receipt expired fifteen minutes after the fix landed. A trust root must not carry
    # a fail-open path whose reason expired the same afternoon.
    # STRUCTURED BINDING FIRST. When the reviewer emitted the required REVIEWED: line, that line
    # IS the answer and no heuristic runs: exact equality after whitespace normalisation. This is
    # the verdict-marker discipline applied to the command, which core-business identified as the
    # mechanism already proven in this codebase and unused here.
    def _cmp(x):
        """Normalise for comparison WITHOUT loosening what is compared.

        Attacked the parse directly and found two FALSE-REFUSAL paths — no security holes, but
        both would block a legitimate approval, and a gate that blocks correct behaviour is one
        someone disables. That is the failure mode this whole session has hit from five
        directions, so it gets fixed rather than tolerated:
          - a reviewer echoing the command with different quoting than the brief used
            (bash "bin/x.sh" vs bash bin/x.sh)
          - a trailing comment after the command on the REVIEWED line
        Quotes and a trailing #comment carry no authority, so removing them changes nothing about
        WHICH command is named. Arguments, flags and recipients are untouched.
        """
        x = re.sub(r"\s+#.*$", "", str(x or "").strip())      # trailing comment
        x = x.replace('"', "").replace("'", "")                # quoting style
        return re.sub(r"\s+", " ", x).strip().lower()

    _rc = str(r.get("reviewed_command") or "").strip()
    if _rc:
        if _cmp(_rc) != _cmp(CMD):
            continue                      # the review was of a DIFFERENT command. No inference.
    else:
        # FALLBACK, and it is being retired. Agent specs load at SESSION START, so reviewers in
        # sessions that began before the contract shipped cannot emit the line yet. Refusing here
        # would wedge every Core until it restarts. Logged so the retirement is EARNED by evidence
        # rather than assumed — when this counter stops moving, the branch goes.
        try:
            with open(os.path.join(state_dir, ".binding-fallback.log"), "a") as _fh:
                _fh.write(json.dumps({"ts": now, "agent": r.get("agent_type"),
                                      "cmd": CMD[:200]}) + "\n")
        except Exception:
            pass
        _rt = (r.get("report_text") or r.get("report_head") or "")
        if not _cmd_in_report(CMD, _rt):
            continue
    if best is None or int(r.get("ts", 0)) > int(best[1].get("ts", 0)):
        best = (p, r)
if best is None:
    print("")
    sys.exit(0)
os.unlink(best[0])            # one-time use: consumed here, cannot approve a second command
print(f"{best[1].get('agent_type','?')}:{best[1].get('nonce','?')}")
PY
)

# PEER_OK WAS COMPUTED AND THROWN AWAY. Fixed 2026-08-09, found by trying to USE it.
#
# The whole peer-approval design above shipped with its result assigned to a variable that
# appears exactly once in this file — the assignment. Every "peer approval" this session
# described as working was calculated correctly and discarded, and the refusal that exposed it
# came after seven review rounds conducted on the assumption that it functioned. It is the same
# defect class as the four rules promising retired hooks, one level deeper: not a document
# claiming a mechanism exists, but a mechanism computing an authorisation and dropping it.
#
# ONE-TIME USE IS NOT OPTIONAL HERE. A local receipt is a file that gets unlinked when consumed,
# so it cannot approve twice. A bus message is not consumed by being read — without the marker
# below, one peer APPROVE would mint a token for its command on every invocation for the whole
# 1800s window. That asymmetry would have made the peer path strictly weaker than the local one
# it stands in for, which is the opposite of what a second seat is supposed to buy.
if [[ -z "$CONSUMED" && -n "$PEER_OK" ]]; then
  # DIRECTION FENCE — a peer approval authorises a PUSH, never a PULL. core-business, 2026-08-09,
  # within minutes of the mechanism being proven working, and it declined to exploit it first.
  #
  # `frm == me` refuses SELF-approval. On a push that is the property we want:
  #     life authors -> life requests -> BUSINESS approves -> life pushes     approver != author
  # Invert it for a pull and the SAME code path means the opposite thing:
  #     life authors -> BUSINESS requests -> LIFE approves -> business pulls  APPROVER *IS* AUTHOR
  # `frm == me` is satisfied — life is not business — so the check passes while the property it
  # exists to guarantee, THAT SOMEONE OTHER THAN THE AUTHOR LOOKED, is not satisfied at all. One
  # seat wearing the label of two, and the log would have recorded it as a second-seat authorisation.
  #
  # THE FIX IS NOT "ALSO CHECK THE AUTHOR": author identity is not recoverable from a bus message,
  # so it would have to be ASSERTED, and an asserted author is the presence-for-fact defect again.
  # DIRECTION is unforgeable from the receiving side and already in $CMD, which has already been
  # matched against the REVIEWED line. So refuse the pull case outright.
  # ALLOWLIST, NOT DENYLIST. The first cut grepped for 'sync-from-baseline' and refused it.
  # Codex, 2026-08-09: that classifies the command's TEXT, not its effective action, so
  # `make pull`, `bash ./pull-link`, a wrapper script, a symlink, or
  # `python3 -c 'runpy.run_path("bin/"+"sync-from-"+"baseline.sh")'` all pull while evading the
  # substring. It is the same enumeration mistake the interpreter flag-list made five times
  # yesterday, and denylists lose that race by construction.
  #
  # Peer approval exists for exactly ONE action — the baseline push — so name it and refuse
  # everything else. An allowlist that is wrong fails CLOSED: an unlisted command is refused, and
  # the cost is a message asking me to widen it deliberately.
  # EQUALITY, NOT SUBSTRING. core-business ran this and it minted for `push && pull`,
  # `push ; pull`, `push | tee`, `push && curl` and `push --force` — five compounds, each
  # authorising an ENTIRE command string on one token the guard then consumes whole. The
  # `push && pull` case is THE EXACT THING THIS FENCE EXISTS TO REFUSE: a pull whose approver is
  # the AUTHOR of the content, recorded in the access log as a legitimate second-seat push.
  #
  # I had already swapped denylist -> allowlist here and written that it "classifies the command's
  # effective action, not its TEXT". IT CLASSIFIED TEXT. Changing the polarity of a substring test
  # does not stop it being a substring test, and the comment asserted a property the code lacked.
  #
  # AND THE SUITE WAS 14/14 GREEN WITH THIS LIVE: every negative case was a PURE pull, so nothing
  # exercised push-plus-anything. That is my own rule — a stable verdict is not evidence the
  # instrument works — landing one layer above where I wrote it.
  _peer_cmd_norm="$(printf '%s' "$CMD" | sed -e 's/[[:space:]]\{1,\}/ /g' -e 's/^ //' -e 's/ $//')"
  case "$_peer_cmd_norm" in
    "bash bin/sync-to-baseline.sh"|"bin/sync-to-baseline.sh"|"bash sync-to-baseline.sh"|"sync-to-baseline.sh"|"./bin/sync-to-baseline.sh") : ;;
    # THE FLAGGED FORM IS PERMITTED ONLY WHEN THE APPROVAL NAMES THE CONTENT.
    #
    # A >50-file push needs --allow-large, and the allowlist could not express it, so the sanctioned
    # second-seat path could not authorise the only form of the operation that applies. I proposed
    # widening it and core-business refused (bus #1025), on an argument better than mine:
    #
    #   "I did not review 112 files. I reviewed the security-relevant subset."   -- so treating its
    #   APPROVE as satisfying a ceiling whose purpose is "someone looked at a mass change" converts a
    #   targeted review into a claim of breadth it did not make. Nominally satisfied, actually
    #   bypassed, which is worse than being in the way because it LOOKS like coverage.
    #
    #   "the receipt would attest that business approved a COMMAND, at a TIME. It would say nothing
    #   about which 112 files rode along."   -- the structural half, and the reason the flag was
    #   never the real problem.
    #
    # Both objections are about the approval being unable to name WHAT it approved. So the flag is
    # accepted only alongside a DIGEST the approval carries and that still matches at mint time:
    #
    #   `--check` prints DIGEST over path+blob-hash of every staged change (needs no approval)
    #   the reviewer is handed that digest with the file list and repeats it in the approval
    #   the mint RECOMPUTES it here and refuses on any mismatch
    #
    # That makes "business approved this push" a statement about a specific tree rather than a verb
    # at a timestamp, and it makes structural the discipline I applied by hand three times tonight --
    # holding because the approval named a SHA while the sync copies the WORKING TREE.
    #
    # It does NOT answer business'"'"'s first objection, and must not be read as answering it: a digest
    # proves WHICH files, never that anyone read them. The 50-file ceiling still means what it meant.
    # What changes is that a reviewer who DID look can now say so about a specific set.
    "bash bin/sync-to-baseline.sh --allow-large"|"bin/sync-to-baseline.sh --allow-large"|"./bin/sync-to-baseline.sh --allow-large")
      _WANT_DIGEST=1 ;;
    *)
    echo "sentinel-approve.sh: REFUSED — peer approval covers the baseline PUSH only." >&2
    echo "  Got: $CMD" >&2
    echo "  On a PULL the approver is the AUTHOR of the content: one seat labelled as two." >&2
    echo "  Anything else is unlisted, and an allowlist refuses what it does not name." >&2
    echo "  Use the operator's own command, or the SessionStart auto-pull." >&2
    echo "  EXACT MATCH REQUIRED — no operators, no extra arguments, no compounds." >&2
    exit 1 ;;
  esac

  # DIGEST ENFORCEMENT — required only for the flagged form, and recomputed, never trusted.
  #
  # Two independent things must hold, and checking only one of them is the usual way this kind of
  # binding turns out to be decorative:
  #
  #   1. the APPROVAL carries a DIGEST line          — the reviewer named a tree
  #   2. that digest still matches the tree NOW      — the tree has not moved since
  #
  # (2) is the half that does the work. Without it the digest is a number copied between two
  # messages, and a working tree that changed after review would still push. That is exactly the
  # failure I prevented BY HAND three times tonight, and hand-discipline is not a mechanism.
  if [[ "${_WANT_DIGEST:-0}" -eq 1 ]]; then
    _APPROVAL_DIGEST="$(grep -oE '^[0-9a-f]{64}$' "$HOME/.claude/peer-approval-digest" 2>/dev/null | head -1)"
    if [[ -z "$_APPROVAL_DIGEST" ]]; then
      echo "sentinel-approve.sh: REFUSED — the flagged form requires a content digest." >&2
      echo "  The approval names a COMMAND and a TIME. For a >50-file push that is not enough:" >&2
      echo "  it cannot say WHICH files rode along (core-business, bus #1025)." >&2
      echo "  Get the digest with:  bash bin/sync-to-baseline.sh --check --allow-large" >&2
      echo "  and ask the reviewer to include, on its own line:  DIGEST: <64 hex>" >&2
      exit 1
    fi
    echo "sentinel-approve.sh: recomputing the content digest (this clones the baseline)..." >&2
    _NOW_DIGEST="$(bash "$(dirname "$HOOKS_DIR")/../bin/sync-to-baseline.sh" --check --allow-large --quiet 2>/dev/null \
                   | grep -aoE '^DIGEST: [0-9a-f]{64}' | grep -oE '[0-9a-f]{64}' | head -1)"
    if [[ -z "$_NOW_DIGEST" ]]; then
      echo "sentinel-approve.sh: REFUSED — could not recompute the content digest." >&2
      echo "  UNVERIFIABLE is not APPROVED. Refusing rather than minting on the reviewer's copy." >&2
      exit 1
    fi
    if [[ "$_APPROVAL_DIGEST" != "$_NOW_DIGEST" ]]; then
      echo "sentinel-approve.sh: REFUSED — the tree MOVED since the review." >&2
      echo "  approved : $_APPROVAL_DIGEST" >&2
      echo "  now      : $_NOW_DIGEST" >&2
      echo "  The approval describes a set of files that is no longer what would be pushed." >&2
      echo "  Re-run --check, send the new digest and file list, and ask for a fresh review." >&2
      exit 1
    fi
    echo "sentinel-approve.sh: content digest matches the reviewed tree." >&2
  fi

  PEER_SEEN="$STATE_DIR/.peer-approval-consumed-$HASH"
  if [[ -f "$PEER_SEEN" ]] && grep -qxF "$PEER_OK" "$PEER_SEEN" 2>/dev/null; then
    echo "sentinel-approve.sh: REFUSED — peer approval $PEER_OK was already consumed for this command." >&2
    echo "  A second push needs a second review. Ask for one." >&2
    exit 1
  fi
  printf '%s\n' "$PEER_OK" >> "$PEER_SEEN"
  CONSUMED="peer:$PEER_OK"
  echo "sentinel-approve.sh: authorised by PEER approval $PEER_OK (second seat, not self)." >&2
fi

if [[ -z "$CONSUMED" ]]; then
  # NAME THE ACTUAL CAUSE. "No fresh APPROVE receipt" is FALSE when a fresh APPROVE receipt is
  # sitting on disk and merely failed to BIND, and the difference changes what the operator does
  # next: re-run the review with the contract restated, versus accept a refusal that never happened.
  #
  # Observed 2026-08-10 on `git push origin main`. The reviewer returned APPROVE and put its
  # `REVIEWED:` line near the END of the report instead of first, so `reviewed_command` was empty and
  # the report-recovery fallback ran. That fallback then found NO BINDABLE TOKENS — every word of
  # `git push origin main` is short and carries no `/`, `.`, `-` or `@`, so the token filter emptied
  # and it refused, which is the correct direction. But the message blamed a missing approval.
  #
  # Same misdirection shape core-business documented for the non-ASCII binding bug and that
  # sync-to-baseline.sh's ceiling ABORT carried until today: the failure names the wrong cause, so
  # the operator retries the wrong thing. A gate that blocks correct behaviour for an unstated
  # reason is one someone eventually disables — the exact failure mode this file's own header names.
  _UNBOUND=$(ls -t "$STATE_DIR"/.sentinel-receipt-*.json 2>/dev/null | head -5 | while read -r _f; do
      python3 - "$_f" <<'PY' 2>/dev/null
import json, sys, time
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if r.get("verdict") == "APPROVE" and time.time() - int(r.get("ts", 0)) <= 900:
    print("%s|%s" % (r.get("nonce", "?"), (r.get("reviewed_command") or "").strip()))
PY
    done | head -1)

  if [[ -n "$_UNBOUND" ]]; then
    _NONCE="${_UNBOUND%%|*}"; _RC="${_UNBOUND#*|}"
    cat >&2 <<MSG
sentinel-approve.sh: REFUSED — a fresh APPROVE receipt EXISTS but could not be bound to this command.

  receipt          : $_NONCE (verdict APPROVE)
  reviewed_command : ${_RC:-<empty — the report had no REVIEWED: line in first position>}
  requested        : $CMD

THIS IS NOT A DECLINED REVIEW. The Sentinel approved; the receipt cannot be tied to the command.
Almost always the reviewer put its \`REVIEWED:\` line somewhere other than the FIRST non-blank line,
so the command had to be recovered from prose — and for a command whose every token is short and
carries no path, flag or address (\`git push origin main\`), recovery is refused by design rather
than guessed at.

Re-run the review and state the contract explicitly in the brief: the report's FIRST non-blank line
must be exactly \`REVIEWED: <command verbatim>\`. Re-running to fix FORMATTING is not shopping for a
verdict; re-running after a BLOCK or ASK is.
MSG
    exit 1
  fi

  cat >&2 <<'MSG'
sentinel-approve.sh: REFUSED — no fresh APPROVE receipt from the Sentinel subagent.

A token can no longer be minted by calling this script. Run the review first:
  invoke the `sentinel` subagent (or `sentinel-code` for a baseline sync) on the exact
  command, and if it returns APPROVE its SubagentStop writes the receipt this consumes.

If Sentinel returned BLOCK or ASK, that is the answer — do not retry for a different one.
MSG
  exit 1
fi

touch "$TOKEN_FILE"
printf 'receipt=%s\n' "$CONSUMED" > "$STATE_DIR/.sentinel-approved-$HASH.provenance" 2>/dev/null || true
echo "sentinel-approve.sh: token minted, backed by receipt $CONSUMED" >&2

# A2b — Auto-log APPROVE to access-log
#
# THE BACKING IS RECORDED, NOT JUST THE VERDICT. core-business's ask, and it closes a gap I had to
# admit I could not answer: asked which of tonight's pushes were agent-initiated and which were
# Nick's, I could not tell, because every path wrote the same line. A log that records WHAT was
# decided and not WHAT AUTHORISED IT cannot answer the one question you go to it for after an
# incident — and the archaeology it forces is how a decorative mechanism stays invisible.
#
# $CONSUMED already carries this: "sentinel-code:<nonce>" for a local subagent review,
# "peer:<core>:bus#<seq>" for a second-seat approval. It cost one field.
CORE_DIR="$(cd "$HOOKS_DIR/../.." && pwd)"
ACCESS_LOG="$CORE_DIR/memory/access-log.md"
TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')
LOG_LINE="$TIMESTAMP | APPROVE | Bash | via=$CONSUMED | $CMD"

if [[ ! -f "$ACCESS_LOG" ]]; then
  printf '# Access Log\n' >> "$ACCESS_LOG"
fi
printf '%s\n' "$LOG_LINE" >> "$ACCESS_LOG"

exit 0
