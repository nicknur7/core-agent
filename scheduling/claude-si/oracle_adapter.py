#!/usr/bin/env python3
"""oracle_adapter.py — STATIC safety substrate for enforcement (WS4). The single riskiest thing in
autonomous enforcement is a WRONG/UNAVAILABLE satisfying-oracle (Codex): a block whose "was it
satisfied?" check is wrong will systematically block correct work. So enforcement conditions may ONLY
use oracles defined here, each a real signal PROVEN behaviorally-equivalent to a live hand-built hook.

The deliverable oracle replicates `.claude/hooks/deliverable-format-gate.py` VERBATIM — same DELIVERABLE
regex, same satisfying tool set, same deliverable extensions, same forward-scan (reset on a real user
turn, skip tool-result-only user records, accumulate delivery across the turn). test_ws4_generator.py
locks these constants against the gate's source so they can NEVER drift.

I/O is bounded: read only the transcript named in the hook payload (`transcript_path`), tail it, and
share the parse between the request- and delivery-oracle (Codex WS4 blocker 6). No project-wide scan.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

# ---- VERBATIM from deliverable-format-gate.py (locked by test) ----
DELIVERABLE_ASK = re.compile(
    r"(?:"
    r"\b(?:give|send|make|build|draft|write|put|prepare)\s+(?:me\s+|us\s+)?(?:it\s+|this\s+|that\s+|the\s+|a\s+|an\s+)?"
    r"(?:up\s+)?(?:in(?:to)?\s+)?(?:a\s+|an\s+|the\s+)?(?:doc|docx|document|report|memo|letter|write[- ]?up|writeup|spreadsheet|slide|deck|pdf|word\s+doc)"
    r"|\bin\s+(?:a\s+)?(?:docx?|pdf|markdown|word|excel|spreadsheet|powerpoint|pptx?|xlsx?)\b"
    r"|\bas\s+(?:a\s+)?(?:docx?|pdf|document|report|write[- ]?up|writeup|spreadsheet|deck)\b"
    r"|\.(?:docx?|pdf|xlsx?|pptx?|csv)\b"
    r"|\bcopy[\s/-]?paste(?:able|-ready)?\b"
    r"|\bnot\s+(?:in\s+)?the\s+terminal\b"
    r")",
    re.IGNORECASE,
)
SATISFYING_NAMES = {"SendUserFile", "Artifact"}
DELIVERABLE_EXT = re.compile(r"\.(?:md|docx?|pdf|xlsx?|csv|html?|pptx?)$", re.IGNORECASE)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
TAIL_BYTES = 512 * 1024  # bounded read — a single turn is far under this; longer transcripts fail SAFE
                         # (a truncated turn yields no user record → not-requested → never blocks)


def _scan(records: list[dict]) -> tuple[str, bool]:
    """Forward-scan (gate-identical): returns (current-turn user text, delivered?). Resets on a real
    user turn; a tool-result-only user record does NOT reset; accumulates satisfying deliveries."""
    user_text_parts: list[str] = []
    satisfied = False
    for d in records:
        dtype = d.get("type")
        content = (d.get("message") or {}).get("content")
        if dtype == "user":
            is_tool_result_only = (
                isinstance(content, list) and len(content) > 0
                and all(isinstance(p, dict) and p.get("type") == "tool_result" for p in content))
            if not is_tool_result_only:
                user_text_parts = []
                satisfied = False
                if isinstance(content, str):
                    user_text_parts.append(content)
                elif isinstance(content, list):
                    for p in content:
                        if isinstance(p, dict) and p.get("type") == "text":
                            user_text_parts.append(p.get("text", "") or "")
            continue
        if dtype != "assistant" or not isinstance(content, list):
            continue
        for p in content:
            if not isinstance(p, dict) or p.get("type") != "tool_use":
                continue
            nm = p.get("name") or ""
            if nm in SATISFYING_NAMES:
                satisfied = True
            elif nm in WRITE_TOOLS:
                fp = str((p.get("input") or {}).get("file_path", "") or "")
                if DELIVERABLE_EXT.search(fp):
                    satisfied = True
    return " ".join(user_text_parts).strip(), satisfied


def deliverable_signals(records: list[dict]) -> tuple[bool, bool]:
    """ONE scan → (deliverable_requested, artifact_delivered). Shared by the dispatcher so the transcript
    is parsed once per Stop, not twice (Codex WS4)."""
    text, delivered = _scan(records)
    return (bool(text) and bool(DELIVERABLE_ASK.search(text)), delivered)


def deliverable_requested_from_records(records: list[dict]) -> bool:
    return deliverable_signals(records)[0]


def artifact_delivery_from_records(records: list[dict]) -> bool:
    return deliverable_signals(records)[1]


def records_for(payload: dict) -> list[dict]:
    """Bounded, payload-scoped transcript read: ONLY the file named by the hook payload, and only its
    LAST TAIL_BYTES (seek — never loads the whole file). No project-wide scan (Codex WS4 blocker 6).
    [] if unavailable → callers fail safe."""
    try:
        path = payload.get("transcript_path") or payload.get("transcriptPath")
        if not isinstance(path, str) or not path:
            return []
        # O_NONBLOCK so opening a fifo/device never blocks; verify the OPENED fd is a regular file
        # (fstat on the descriptor — no TOCTOU window); hard-cap the read at TAIL_BYTES (Codex WS4).
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except Exception:
            return []
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return []
            if st.st_size > TAIL_BYTES:
                os.lseek(fd, st.st_size - TAIL_BYTES, os.SEEK_SET)
                partial = True
            else:
                partial = False
            data = os.read(fd, TAIL_BYTES)  # HARD byte cap
        finally:
            os.close(fd)
        text = data.decode("utf-8", "ignore")
        if partial:
            nl = text.find("\n")  # bounded partial-line discard (no readline)
            text = "" if nl == -1 else text[nl + 1:]  # no newline in the tail → discard (never parse a fragment)
        out = []
        for ln in text.splitlines():
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


# --- ORACLE 2: adversarial review before a blast-radius action (2026-07-27) ------------------
# Nick's most-repeated directive is to orchestrate with Codex and Fable on substantial work — 17
# recorded moments, 5 of them AFTER the rule documenting it shipped. A rule that keeps being
# restated is not working, so this makes it CHECKABLE rather than advisory.
#
# The oracle answers one question about the current turn: did the turn take a blast-radius action
# WITHOUT an adversarial review having run in the same turn? Both halves are mechanical — a
# blast-radius action is a specific command shape, and a review is a specific subagent call — so
# there is no judgement in the signal, which is the bar every oracle here has to meet.
#
# Fail-safe direction is deliberate and matches the deliverable oracle: when the transcript is
# unavailable or truncated we report "no blast-radius action, review ran", so a block can never fire
# on missing evidence.

# Commands whose blast radius reaches beyond this Core: the shared baseline, the fleet, or the DB.
#
# `git push` accepts GLOBAL OPTIONS BEFORE THE SUBCOMMAND, and the original `git\s+push` missed every
# one of them. `git -C "<dir>" push origin main` is the form this Core actually uses for a real push —
# both of today's pushes used it — so the oracle was not merely noisy, it was BLIND to the very action
# it exists to detect, while firing on the read-only preview of that same action. Over- and
# under-firing at once, which is why the fires it produced looked plausible and were not.
#
# The option prefix is enumerated rather than made a lazy wildcard so that `git commit -m "push it"`
# still does not match: after `git`, the next token must be an option (or an option plus its value)
# for the loop to continue, and `commit` is neither, so the pattern then requires `push` and fails.
BLAST_RADIUS_CMD = re.compile(
    r"(sync-to-baseline"
    r"|git\s+(?:(?:-[cC]|--git-dir|--work-tree|--namespace|--exec-path)(?:\s+|=)"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)\s+|--\S+\s+|-[a-zA-Z]\s+)*push\b"
    r"|run-migrations|reconcile-hooks[^\n]*--apply"
    r"|estate-sweep[^\n]*--apply|si-unify-cutover)", re.I)

# ...but the name alone is not the action. Narrowed 2026-08-05 after this oracle fired on a turn that
# shipped nothing: the contract's advisory appeared on a purely conversational turn whose only
# matching command was `bash bin/sync-to-baseline.sh --check`, the READ-ONLY preview, run twice
# solely to report a file count. Four distinct false-positive classes, all measured:
#
#   bash bin/sync-to-baseline.sh --check      read-only preview      -> matched
#   git push --dry-run origin main            dry run                -> matched
#   bash bin/run-migrations.sh --status       status only            -> matched
#   grep -n "git push" docs/PULL-NOTES.md     the string as DATA     -> matched
#
# That last class is the same defect the codex fence had in pretooluse-guard.sh: a pattern matching a
# literal that appears as data rather than as an invocation. Note also that the guard already treats
# `sync-to-baseline.sh --check` as ungated, so this oracle was out of step with the gate it mirrors.
#
# POLARITY HERE IS THE OPPOSITE OF THE FENCE'S, DELIBERATELY. That fence must fail CLOSED, because a
# block it misses is a security hole. This oracle only decides whether to inject a reminder, so the
# harm is inverted: over-firing is noise that trains Nick to ignore the advisory, while a missed
# reminder costs one un-reviewed diff. So it biases toward NOT firing, consistent with the fail-safe
# direction already documented above.
#
# Why this is fixed in CODE and not by the tuner: an oracle lives in a .py file, and the loop is
# forbidden from writing .py (Fable's parameters-not-code line, enforced by _PAYLOAD_FORBIDDEN). Faced
# with this over-fire the tuner's only available move is to demote the CONTRACT to shadow — which
# would retire a correct contract because its oracle was wrong. Recorded in decisions-log: an
# over-firing oracle is outside the self-tuning loop by construction and needs a human-or-agent fix.
_READ_ONLY_FLAG = re.compile(r"(--check|--dry-run|--dryrun|--status|--list|--verify|--plan)\b", re.I)
# Command words that can only read text, so a match inside their arguments is a mention, not a run.
_TEXT_READER = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:/\S+/)?(grep|egrep|fgrep|rg|ag|ack|echo|printf|cat|head|tail|less|more|awk|sed|jq|wc|"
    r"git\s+log|git\s+show|git\s+diff|git\s+grep)\b", re.I)


def _split_clauses(cmd: str) -> list:
    """Split a command string on shell separators that are NOT inside quotes.

    WHY THIS EXISTS (2026-08-12, found by core-finance, DOSE 33). The split was
    `re.split(r"&&|\\|\\||[;|&\\n]", cmd)`, which is not quote-aware — so a grep ALTERNATION is torn
    in half and the halves lose each other:

        grep -n "093a285|si-unify-cutover" memory/current-state.md

        -> ['grep -n "093a285',                          reader prefix, no blast token
            'si-unify-cutover" memory/current-state.md']  blast token, NO reader prefix

    `_TEXT_READER.match()` anchors on the clause's leading command word, so it cannot exempt the
    second fragment — the exemption and the trigger end up in different pieces. This module's own
    docstring at :182 names exactly this case as one that must NOT count: "Matched a name but is not
    an action — --status, --check, a grep that mentions it." The intent was explicit; the splitter
    defeated it. Reproduced on the real 2026-08-08 review-gate row and on a second case.

    WHY IT MATTERS EVEN THOUGH MODE="shadow" AND NOTHING IS BLOCKED: the same predicate feeds
    `review_signals()`, the numerator of the violations-per-opportunity objective this module's
    comment at :200 calls the denominator the system kept losing. So it corrupts the metric today
    while blocking nothing.

    STRICTLY MORE CONSERVATIVE, not more permissive. Ignoring separators inside quotes yields FEWER,
    LONGER clauses — and a real action outside quotes still matches BLAST_RADIUS_CMD in whatever
    clause holds it, while `_TEXT_READER` still only exempts a clause that BEGINS with a reader. A
    command like `git push origin "main;x"` now stays one clause and still trips, where the old
    splitter would have cut it.
    """
    out, buf = [], []
    quote = None
    s = cmd or ""
    i = 0
    while i < len(s):
        ch = s[i]
        # SINGLE AND DOUBLE QUOTES ESCAPE DIFFERENTLY, and treating them alike opened a real hole.
        #
        # The first version applied one rule to both: `ch == quote and s[i-1] != "\\"`. POSIX single
        # quotes have NO escape mechanism at all — a backslash inside them is a literal backslash,
        # and the very next `'` closes the string. So `echo 'foo\' && <a real push>` closes its quote
        # right after `foo\`, and everything after it is live shell.
        #
        # Under the buggy rule the quote never closed, the whole line collapsed into ONE clause, and
        # _TEXT_READER exempted it on the leading `echo` — so a command that genuinely pushes was
        # classified as not blast-radius. More permissive than the splitter it replaced, in exactly
        # the direction this function must never move, and it contradicted the docstring's own claim
        # to be strictly more conservative.
        #
        # Caught by sentinel-code on the baseline-push review, which BLOCKED it: it built the
        # counter-example, confirmed against `shlex.split(..., posix=True)` that real lexing closes
        # the quote there, and showed the OLD regex splitter correctly flagged the same string. The
        # 28 cases in the table did not cover a backslash before a closing single quote.
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            # Inside double quotes a backslash DOES escape the next character, so consume both and
            # never let an escaped `\"` be read as the closing quote.
            if ch == "\\" and i + 1 < len(s):
                buf.append(ch)
                buf.append(s[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if s.startswith("&&", i) or s.startswith("||", i):
            out.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|&\n":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def _is_blast_radius(cmd: str) -> bool:
    """True only when a clause actually PERFORMS a fleet-reaching action.

    Per-clause, like the guard's own detectors: a turn may pair a read-only preview with a real push
    in one command string, and the real one must still count.
    """
    # AN UNPARSEABLE COMMAND MAY NOT EARN AN EXEMPTION (2026-08-12).
    #
    # `echo "foo\" && <real action>` is UNTERMINATED — shlex.split(posix=True) raises "No closing
    # quotation" on it. Under quote-aware splitting the open quote swallows the whole line into one
    # clause, and _TEXT_READER then exempts it on the leading `echo`. So a malformed string could
    # hide a real invocation behind a reader, which is the same hole sentinel-code blocked in the
    # single-quote case, one step over.
    #
    # pretooluse-guard.sh:658 already set the precedent for this exact situation — "unterminated ->
    # cannot reason -> do not withdraw". Withholding the exemption rather than withholding the
    # detection is the conservative half: a malformed command that names a fleet-reaching action is
    # counted as one.
    _malformed = False
    try:
        import shlex as _shlex
        _shlex.split(cmd or "", posix=True)
    except ValueError:
        _malformed = True
    except Exception:
        _malformed = True

    for clause in _split_clauses(cmd):
        if not clause.strip():
            continue
        if not BLAST_RADIUS_CMD.search(clause):
            continue
        if _READ_ONLY_FLAG.search(clause):
            continue          # --check / --dry-run / --status: inspects, changes nothing
        if re.search(r"git\s+push[^\n]*\s-n\b", clause, re.I):
            continue          # `git push -n` is --dry-run
        if _TEXT_READER.match(clause) and not _malformed:
            continue          # the command name appears as data in a read, not as an invocation
        return True
    return False

# A genuine adversarial review: an independent model asked to attack the work. Sentinel-code and the
# Codex reviewers count; a plain Read or a same-model helper does not.
# Matched against the AGENT TYPE, not free text. Both reviewers flagged that a plain substring
# search over the whole tool-call blob counted an Agent() call whose PROMPT merely mentioned
# "adversarial review" as a review having happened — an artifact describing the work could satisfy
# its own oracle. Only a genuine reviewer subagent counts now.
REVIEW_AGENTS = ("sentinel-code", "codex:codex-rescue", "codex:adversarial-review", "codex-rescue")
REVIEW_MARKER = re.compile(
    r"\"(?:subagent_type|agentType|agent_type)\"\s*:\s*\"(" +
    "|".join(re.escape(a) for a in REVIEW_AGENTS) + r")\"", re.I)


def review_signals(records: list[dict]) -> tuple[bool, bool]:
    """ONE scan → (blast_radius_action, adversarial_review_ran) for the CURRENT turn.

    Same forward-scan discipline as _scan: a real user turn resets both, a tool-result-only user
    record does not. Reviews count when they appear anywhere in the turn — before or after the
    action — because the useful signal is "was this work reviewed at all", and requiring strict
    ordering would make an after-the-fact review look identical to none.
    """
    blast = False
    reviewed = False
    for d in records:
        dtype = d.get("type")
        content = (d.get("message") or {}).get("content")
        if dtype == "user":
            is_tool_result_only = (
                isinstance(content, list) and len(content) > 0
                and all(isinstance(p, dict) and p.get("type") == "tool_result" for p in content))
            if not is_tool_result_only:
                blast = False
                reviewed = False
            continue
        if dtype != "assistant" or not isinstance(content, list):
            continue
        for p in content:
            if not isinstance(p, dict) or p.get("type") != "tool_use":
                continue
            name = str(p.get("name") or "")
            blob = json.dumps(p.get("input") or {})[:4000]
            if name in ("Bash", "BashOutput") and _is_blast_radius(blob):
                blast = True
            if name in ("Agent", "Task") and REVIEW_MARKER.search(blob):
                reviewed = True
    return (blast, reviewed)


def blast_radius_from_records(records: list[dict]) -> bool:
    return review_signals(records)[0]


def adversarial_review_from_records(records: list[dict]) -> bool:
    return review_signals(records)[1]
