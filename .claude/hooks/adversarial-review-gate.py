#!/usr/bin/env python3
"""PreToolUse — PREVENT a blast-radius action taken without adversarial review.

Currently SHADOW ONLY. It logs what it would have blocked and exits 0 every time. Nothing here
blocks until Nick has read the shadow log and said so. See PHASE below.

WHY THIS EXISTS
---------------
Nick's most-repeated directive (17 recorded moments, 10x + 5x as distilled recurring asks) is to run
Codex/Fable adversarial review before shipping something with blast radius. It was already mined into
an artifact — `adversarial_review_before_blast_radius` in artifact_typer.ORACLE_CATALOG — and that
artifact is flagged `never_promote`, correctly, for the reason its own comment gives:

    This oracle observes at Stop, by which point the push or migration has ALREADY RUN. As a shadow
    signal that is fine and useful — it records "you shipped without review". As an ENFORCED block it
    would be actively harmful: it cannot prevent the action, only fail the turn afterwards, inviting
    a retry of a possibly non-idempotent operation.

    Prevention belongs at PreToolUse, on the command itself, before it executes. Until that redesign
    exists this entry stays shadow-only.

This file is that redesign. It moves the check from "notice afterwards" to "stop beforehand".

WHY IT IS HAND-WRITTEN AND NOT A LEARNED ARTIFACT
-------------------------------------------------
`friction_dispatch` structurally refuses block-mode on PreToolUse, because PreToolUse is the event
the security trust root lives on. That bar is correct and is not being worked around. Prevention at
PreToolUse is therefore always human-authored code — which is exactly why oracle authorship is the
real bottleneck of the self-improvement design rather than a mere inconvenience. The loop can notice
the pattern; only a person can put a gate in front of it.

WHY THE SCOPE IS NARROW — AND WHY THAT IS THE POINT
---------------------------------------------------
Only actions that have NO review requirement today:

    run-migrations.sh            mutates the SHARED corebrain that all five Cores use
    reconcile-hooks.sh --apply   rewrites hook registration
    estate-sweep.sh --apply      fleet-wide file operations
    si-unify-cutover.sh          one-way spine migration

`git push` and `sync-to-baseline.sh` are DELIBERATELY EXCLUDED. `pretooluse-guard.sh` already forces
a Sentinel review for those, and `sentinel-code` is already counted as an adversarial reviewer, so
the existing chain satisfies this requirement. Gating them again would add friction and no safety.
Covering the gap is the job; re-covering what is covered is how a guard suite becomes noise.

Read-only invocations (`--status`, `--check`, `--dry-run`) are not blast-radius actions and are
excluded by `oracle_adapter._is_blast_radius`, which was hardened on 2026-08-05 after it was found to
be simultaneously over-firing on `--check` and blind to `git -C <dir> push`. 22-case regression test
at bin/tests/test_blast_radius_oracle.py.

WHAT COUNTS AS A REVIEW
-----------------------
`oracle_adapter.review_signals()` — an Agent/Task call whose subagent_type is one of
{sentinel-code, codex:codex-rescue, codex:adversarial-review, codex-rescue}, matched on the AGENT
TYPE, never on free text. A prompt that merely mentions "adversarial review" does not count; that
loophole was closed deliberately so an artifact could not satisfy its own oracle.

PHASE
-----
    PHASE 1 (now)  MODE = "shadow". Logs `opportunity` and `would_block`. Never blocks. Exit 0.
    PHASE 2        MODE = "block", only after Nick has read the shadow log and agreed. Flipping this
                   constant is the entire change; nothing else needs to move.

Shadow-first is not caution theatre: this gate sits in front of database migrations, and a gate that
stops real work on a bad heuristic is worse than the problem it solves.

ESCAPE HATCH
------------
`CORE_REVIEW_GATE_OFF=1` bypasses, with a loud log line. Deliberately NOT a Sentinel token: a token
is the instrument for security review, and reusing it here would blur what the trust root means. This
is a correctness gate, and it gets its own, plainly-labelled switch.

Fail-open on any internal error, like every other gate here: a broken guard must not become an
outage. Every skip reason is logged, so failing open is never silent.

    events -> .claude/state/review-gate-log.jsonl
"""
from __future__ import annotations

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # operator name from identity.json, never hardcoded

import hashlib
import json
import os
import sys
import shlex
import time
from pathlib import Path

MODE = "shadow"          # "shadow" | "block"  — see PHASE above
REPO = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
LOG = REPO / ".claude" / "state" / "review-gate-log.jsonl"
# One place, so the writer and the truncation flag can never disagree about the cut.
CMD_LOG_CHARS = 200

# The unguarded blast-radius set. Anything already covered by pretooluse-guard's Sentinel chain is
# absent on purpose (see module docstring).
IN_SCOPE = ("run-migrations", "reconcile-hooks", "estate-sweep", "si-unify-cutover")
# pretooluse-guard already requires Sentinel for these, so this gate stands down.
ALREADY_GATED = ("sync-to-baseline", "sync-from-baseline", "git push", "git -c", "git -C")


def _already_gated(cmd: str) -> bool:
    """Is this command ALREADY governed by the Sentinel chain? Decided on COMMAND STRUCTURE.

    This was `any(tok in cmd for tok in ALREADY_GATED)` — a raw substring test over the whole line —
    so any argument that happened to contain "git -c", "git push" or "sync-to-baseline" stood the
    gate down. A commit message, a filename, a note, a grep pattern. The file's own docstring says
    Phase 2 flips MODE to "block" as a ONE-LINE CHANGE with nothing else moving, and at that point
    this collision lets a real `run-migrations` / `reconcile-hooks --apply` / `estate-sweep --apply`
    skip a mandatory adversarial review.

    SUBSTRING WHERE EXACT IS REQUIRED, seventh instance in three days, and this one is on a gate that
    is one line away from being load-bearing. The rule: on a classification path, compare the
    CAPTURED VALUE by equality or anchor at a boundary — never ask whether the surrounding text
    CONTAINS the thing.

    Tokenised with shlex, which this repo already relies on in pretooluse-guard's inspect_git_push,
    so a token inside a quoted argument is one token and simply is not the command head. A parse
    failure returns False — the SAFE direction here, because False means the review requirement
    STANDS rather than being waived.
    """
    try:
        # comments=True, because a TRAILING SHELL COMMENT is not part of the command. Without it,
        # `bash bin/run-migrations.sh --apply  # note about git push` tokenised "git" and "push" and
        # stood the gate down — the same false-stand-down the substring version produced, surviving
        # into the tokenised version because I changed the matcher and not the input to it.
        toks = shlex.split(cmd, comments=True)
    except ValueError:
        return False            # unparseable -> do not stand down. Failing closed means reviewing.
    if not toks:
        return False
    # git push / git -c … / git -C … : the head must actually be git, not a word inside an argument.
    for i, tok in enumerate(toks):
        base = tok.rsplit("/", 1)[-1]
        if base == "git":
            rest = toks[i + 1:]
            if any(r in ("push", "-c", "-C") for r in rest[:4]):
                return True
        if base in ("sync-to-baseline.sh", "sync-from-baseline.sh"):
            return True
        # `bash bin/sync-to-baseline.sh` — the script is the argument, matched on its BASENAME
        if base.endswith(".sh") and base.startswith(("sync-to-baseline", "sync-from-baseline")):
            return True
    return False


def log(rec: dict) -> None:
    """Append one shadow record. Marks TRUNCATION explicitly — see below.

    THE LOG COULD NOT ANSWER THE QUESTION IT EXISTS FOR (2026-08-13). This file is shadow-only
    until "Nick has read the shadow log and said so", so the log IS the deliverable. Reading it
    after 100 records, it could not support that decision, and it was actively misleading:

    Every record stored `cmd[:200]`. For 6 of 8 `would_block` rows the deciding text sat past
    character 200, so the record shows a command that looks harmless and gives no way to tell why
    it fired. Worse than missing — replayable to the WRONG answer:

        logged (truncated) : cd … && wc -l bin/run-migrations.sh && grep -n "declared_tables\\|…
        _is_blast_radius   : True     <- the cut left the quote UNTERMINATED
        same + closing "   : False    <- what the live gate actually saw

    The truncation MANUFACTURED a malformed command; `shlex` then refuses it, and the read-only
    exemption is correctly denied to a command that cannot be parsed. So replaying the log produces
    verdicts the gate never made, in the direction that invents violations. Reading these rows as
    evidence, I twice drew a conclusion the data did not support before checking.

    Fixed by recording what truncation destroys: the full length, a hash of the full command, and
    an explicit `cmd_truncated` flag. That is enough for a reader to know a row is unreplayable
    instead of replaying it and trusting the answer. The full command is deliberately NOT stored —
    it can carry paths and prompt text, and this log has no redaction pass.
    """
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        rec.setdefault("ts", int(time.time()))
        rec.setdefault("mode", MODE)
        full = rec.pop("_full_cmd", None)
        if full is not None:
            rec["cmd_len"] = len(full)
            rec["cmd_sha12"] = hashlib.sha256(full.encode("utf-8", "replace")).hexdigest()[:12]
            rec["cmd_truncated"] = len(full) > CMD_LOG_CHARS
        with LOG.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    cmd = ((data.get("tool_input") or data.get("toolInput") or {}).get("command") or "")
    if not cmd:
        return 0

    if os.environ.get("CORE_REVIEW_GATE_OFF") == "1":
        log({"event": "bypassed", "why": "CORE_REVIEW_GATE_OFF=1", "cmd": cmd[:CMD_LOG_CHARS], "_full_cmd": cmd})
        return 0

    # Cheap pre-filter before importing anything or reading a transcript: the overwhelming majority
    # of Bash calls are none of this gate's business and must cost nothing.
    if not any(tok in cmd for tok in IN_SCOPE):
        return 0

    try:
        sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
        from oracle_adapter import _is_blast_radius, records_for, review_signals
    except Exception as exc:
        log({"event": "skipped", "why": f"import failed: {exc}", "cmd": cmd[:CMD_LOG_CHARS], "_full_cmd": cmd})
        return 0

    try:
        if not _is_blast_radius(cmd):
            # Matched a name but is not an action — `--status`, `--check`, a grep that mentions it.
            log({"event": "not_action", "cmd": cmd[:CMD_LOG_CHARS], "_full_cmd": cmd})
            return 0
        # Defence in depth: never fire on something the Sentinel chain already governs, even if the
        # command also happens to contain an in-scope token.
        if _already_gated(cmd):
            log({"event": "stood_down", "why": "already gated by pretooluse-guard/Sentinel",
                 "cmd": cmd[:CMD_LOG_CHARS], "_full_cmd": cmd})
            return 0

        # records_for takes the PAYLOAD, not a path — it does the bounded tail read itself and
        # returns [] when the transcript is unavailable, so callers fail safe.
        recs = records_for(data)
        _blast, reviewed = review_signals(recs)
    except Exception as exc:
        log({"event": "skipped", "why": f"oracle error: {exc}", "cmd": cmd[:CMD_LOG_CHARS], "_full_cmd": cmd})
        return 0

    # An OPPORTUNITY is logged whether or not the gate would act. This is the denominator the new
    # objective needs — "violations per opportunity" is unmeasurable without counting the turns where
    # the oracle COULD have fired, and every metric in this system that lacked its denominator ended
    # up reporting a rate it could not actually compute.
    log({"event": "opportunity", "cmd": cmd[:CMD_LOG_CHARS], "_full_cmd": cmd, "reviewed": bool(reviewed)})

    if reviewed:
        return 0

    msg = (
        "ADVERSARIAL REVIEW GATE — blast-radius action with no review this turn.\n"
        f"  {cmd[:300]}\n"
        f"  {_U.name()}'s standing directive (17 recorded moments): run an adversarial reviewer before\n"
        "  shipping something with blast radius. This command mutates shared state and nothing\n"
        "  reviewed it in this turn.\n"
        "  Run sentinel-code, or a Codex review, then retry. Bypass: CORE_REVIEW_GATE_OFF=1."
    )

    if MODE == "block":
        log({"event": "blocked", "cmd": cmd[:CMD_LOG_CHARS], "_full_cmd": cmd})
        print(msg, file=sys.stderr)
        return 2

    log({"event": "would_block", "cmd": cmd[:CMD_LOG_CHARS], "_full_cmd": cmd})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)          # a broken guard must never become an outage
