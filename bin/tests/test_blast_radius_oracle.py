#!/usr/bin/env python3
"""The blast-radius oracle must fire on actions, not on mentions or previews.

WHY THIS EXISTS
---------------
`adversarial_review_before_blast_radius` is a learned contract minted from Nick's own directive
(17 recorded moments) to orchestrate Claude + Fable + Codex on substantial work. Its oracle answers
one mechanical question about a turn: did this turn take a fleet-reaching action without an
adversarial review running? The contract is correct. Its oracle was not.

On 2026-08-05 it fired on a purely CONVERSATIONAL turn — the reply contained no diff, no edit and no
outward action. The only matching command was `bash bin/sync-to-baseline.sh --check`, the read-only
preview, run to report a file count. Measured against the pattern of the day, four false-positive
classes:

    bash bin/sync-to-baseline.sh --check     read-only preview   -> matched
    git push --dry-run origin main           dry run             -> matched
    bash bin/run-migrations.sh --status      status only         -> matched
    grep -n "git push" docs/PULL-NOTES.md    the string as DATA  -> matched

That last one is the same defect the codex fence carried in pretooluse-guard.sh: a literal matching
where it appears as data rather than as an invocation. The guard, meanwhile, already treated
`sync-to-baseline.sh --check` as ungated — so the oracle was out of step with the gate it mirrors.

Then the worse half. `git\\s+push` does not match `git -C "<dir>" push origin main`, which is the form
this Core actually uses, and the form BOTH of that day's real pushes used. So the oracle was blind to
the action it exists to detect while firing on the read-only preview of that same action. Over- and
under-firing simultaneously, which is exactly why its fires looked plausible: it was never reporting
what it claimed to report.

POLARITY IS OPPOSITE TO THE FENCE'S, ON PURPOSE
-----------------------------------------------
pretooluse-guard's codex fence must fail CLOSED — a block it misses is a security hole. This oracle
only decides whether to inject a reminder, so the harm inverts: over-firing is noise that trains Nick
to ignore the advisory, while a missed reminder costs one un-reviewed diff. It biases toward NOT
firing. Both choices are right for their own gate, and conflating them would be the error.

WHY A TEST AND NOT A TUNER JOB
------------------------------
An oracle lives in a .py file, and the self-improvement loop is forbidden from writing .py
(parameters-not-code, enforced by _PAYLOAD_FORBIDDEN). Faced with this over-fire the tuner's only
available move is to demote the CONTRACT to shadow — retiring a correct contract because its oracle
was wrong. So an over-firing oracle sits outside the autonomous loop by construction, and the only
thing that can protect it is a test like this one.

Run: python3 bin/tests/test_blast_radius_oracle.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

# (expected, command, why it matters)
CASES: list[tuple[bool, str, str]] = [
    # ---- must NOT fire: inspects, previews, or merely mentions -------------------------------
    (False, "bash bin/sync-to-baseline.sh --check",
     "read-only preview — the exact command that produced the 2026-08-05 false fire"),
    (False, "git push --dry-run origin main", "dry run changes nothing"),
    (False, "git push -n origin main", "-n is --dry-run"),
    (False, "git -C /x push --dry-run origin main", "global option AND dry run"),
    (False, "bash bin/run-migrations.sh --status", "status only"),
    (False, "bash bin/reconcile-hooks.sh", "no --apply, so it only reports"),
    (False, 'grep -n "git push" docs/PULL-NOTES.md', "the literal as DATA, not an invocation"),
    (False, "git show HEAD --stat | grep run-migrations", "reader piped to reader"),
    (False, 'echo "remember to run sync-to-baseline"', "a mention in prose"),
    (False, 'git commit -m "push it later"', "'push' inside a commit message is not the subcommand"),
    (False, "git log --grep=push", "searching for the word"),
    (False, "git log --oneline -1", "plain read"),

    # ---- MUST fire: the action actually happens ---------------------------------------------
    (True, "bash bin/sync-to-baseline.sh", "the real baseline push"),
    (True, "git push origin main", "the plain form"),
    (True, 'git -C "/Users/n/AI Projects/core-life" push origin main',
     "the form this Core actually uses — was MISSED entirely before 2026-08-05"),
    (True, "git -C /x/y push origin main", "global option, unquoted path"),
    (True, "git --git-dir=/x/.git push origin main", "option with = value"),
    (True, "bash bin/run-migrations.sh", "applies migrations to the SHARED database"),
    (True, "bash bin/reconcile-hooks.sh --apply", "--apply is the mutating form"),
    (True, "bash bin/si-unify-cutover.sh", "cutover"),
    (True, "bash bin/sync-to-baseline.sh --check && bash bin/sync-to-baseline.sh",
     "preview AND real push in one string — the real clause must still count"),
    (True, "cd /x && git push origin main 2>&1 | tail -5",
     "wrapped in cd and piped, as it is actually written"),

    # ---- QUOTE-AWARE CLAUSE SPLITTING (2026-08-12, core-finance DOSE 33) ---------------------
    # The splitter was a plain re.split on shell separators, which is not quote-aware, so a grep
    # ALTERNATION was torn in half and the halves lost each other:
    #
    #     grep -n "093a285|si-unify-cutover" f.md
    #     -> ['grep -n "093a285',  'si-unify-cutover" f.md']
    #          reader, no token      token, NO reader prefix
    #
    # _TEXT_READER anchors on the clause's LEADING command word, so it could not exempt the second
    # fragment: the exemption and the trigger ended up in different pieces. oracle_adapter's own
    # docstring names the case it was failing — "Matched a name but is not an action ... a grep
    # that mentions it."
    #
    # It mattered even though MODE="shadow" blocks nothing, because the same predicate feeds
    # review_signals() — the numerator of the violations-per-opportunity objective. It corrupted
    # the metric while blocking nothing, which is the harder kind to notice.
    (False, 'grep -n "093a285|si-unify-cutover" memory/current-state.md',
     "the real 2026-08-08 review-gate row: a read-only grep whose alternation split in two"),
    (False, 'grep -nE "run-migrations|sync-to-baseline" docs/notes.md',
     "a grep naming two blast commands is still a grep"),
    (False, 'sed -n 1,5p f.md ; echo hi ; grep -n "si-unify-cutover|x" f.md',
     "reader chain with an alternation in the final clause"),

    # ---- the direction that must NOT regress: quoting may not HIDE a real action -------------
    (True, 'grep "sync-to-baseline" f.md && bash bin/sync-to-baseline.sh',
     "an exempt reader clause must not launder the real invocation beside it"),
    (True, 'bash bin/si-unify-cutover.sh "a;b"',
     "a separator inside quotes no longer splits the clause, and it must still fire — the old "
     "splitter cut this in two"),
    (True, 'echo "safe" ; bash bin/run-migrations.sh',
     "quoted echo followed by a genuine migration"),

    # ---- QUOTE-ESCAPE ASYMMETRY (2026-08-12) — sentinel-code BLOCKED the first fix over this ----
    # The first quote-aware splitter applied ONE escape rule to both quote characters:
    # `ch == quote and s[i-1] != "\\"`. POSIX single quotes have NO escape mechanism — a backslash
    # inside them is literal and the very next ' closes the string. So the line below closes its
    # quote right after `foo\`, and everything after it is LIVE SHELL. Under the buggy rule the
    # quote never closed, the whole line collapsed into one clause, and _TEXT_READER exempted it on
    # the leading `echo`: a command that genuinely runs a migration was classified as harmless.
    # More permissive than the regex splitter it replaced, in the one direction this predicate must
    # never move — and contradicting that version's own docstring claim to be "strictly more
    # conservative". Confirmed by sentinel-code against shlex.split(posix=True).
    (True, "echo 'foo\\' && bash bin/run-migrations.sh",
     "backslash before a closing SINGLE quote does not escape it — the migration is live shell"),
    (True, "echo 'a\\' ; bash bin/si-unify-cutover.sh",
     "same hole, semicolon separator"),

    # Inside DOUBLE quotes a backslash IS an escape, so this string never terminates. shlex raises
    # "No closing quotation" on it. An unparseable command must not earn the reader exemption —
    # pretooluse-guard.sh:658 set that precedent ("unterminated -> cannot reason -> do not
    # withdraw"). Withhold the exemption, not the detection.
    (True, 'echo "foo\\" && bash bin/run-migrations.sh',
     "unterminated quote may not hide a real invocation behind a leading reader"),
]


def main() -> int:
    try:
        from oracle_adapter import _is_blast_radius
    except Exception as exc:
        print(f"  FAIL  import oracle_adapter._is_blast_radius — {exc}")
        return 1

    npass = 0
    fails: list[str] = []
    for want, cmd, why in CASES:
        try:
            got = _is_blast_radius(cmd)
        except Exception as exc:
            fails.append(f"{cmd!r} raised {exc}")
            continue
        if got == want:
            npass += 1
        else:
            fails.append(f"want={want} got={got}  {cmd}   ({why})")

    for f in fails:
        print(f"  FAIL  {f}")
    if not fails:
        print(f"  PASS  blast-radius oracle: {npass} cases "
              f"({sum(1 for w, _, _ in CASES if not w)} must-not-fire, "
              f"{sum(1 for w, _, _ in CASES if w)} must-fire)")

    print(f"\n=== Results: {npass} passed, {len(fails)} failed ===")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
