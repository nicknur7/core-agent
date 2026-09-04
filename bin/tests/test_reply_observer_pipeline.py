#!/usr/bin/env python3
"""
AUTHORED BY core-finance (T012, 2026-08-13). INSTALLED BY core-life, verbatim except the
root resolution noted at _find_root below.

The provenance is the evidence, not bookkeeping: finance cannot write bin/tests (pull-only
Core, shared-write-guard), so it authors and RUNS while life reviews and installs. That the
author and the runner are different seats is what makes a green result mean something —
it is the same reason the fitness floor was believed only after a non-author seat
reproduced it.
T012, pipeline half — does a FIRING detector's row actually REACH the log?

AUTHORED AND RUN BY core-finance, 2026-08-13, per the T012 routing (bus #1094 -> #1105 -> #1387):
finance authors and RUNS, life installs. The provenance is not bookkeeping — reply-observer.py is
life's file, and every finding below comes from a seat that did not write it. Four of tonight's
real defects came from that property, so a copy of this file that hides where it came from has
lost the evidence along with the credit.

It found one live defect (family D/E, the forgeable financial_figure sourcing) and independently
confirmed two of life's fixes: the 2026-08-10 accumulation repair on the exact production
precondition, and the :634-645 adaptive tail-window.

The planted-defect regime (authored_T012_planted_defect_regime.py) proves each of the nine
detectors MATCHES its planted positive and skips its near-miss negative. It stops there. Everything
between the match and the byte on disk is invisible to it: _accumulate, the per-turn dedupe,
sourced_for, digit redaction, and the append itself. A detector that matches correctly and then
fails to write is indistinguishable, in that regime, from one that works.

Six families here, in the order they were built, because each exists only because the previous one
was wrong or incomplete:

  A. LANDING   — drive all nine planted positives through main() and assert a row appears.
  B. STREAM    — accumulation across a chunk boundary, per-turn dedupe, digit redaction.
  C. BRANCH    — the repair path at reply-observer.py:801-804 on a PRE-EXISTING 0755 directory,
                 which is the condition that actually broke production, plus the symlink refusal
                 that must survive the repair.
  D. SOURCING  — sourced_for's True branch, and whether a recalled figure can FORGE it.
  E. END-TO-END— whether D's forgery survives the REAL _turn_tool_blob, or is a fixture artefact.
  F. WINDOW    — the adaptive tail expansion (:634-645), the fix for the largest class.

WHY FAMILY C EXISTS AT ALL. Family B passed while proving nothing about the branch that matters.
TemporaryDirectory hands back a 0700 tree, and _accumulate's mkdir(mode=0o700) then created _TMP
fresh at 0700 — so the repair branch was never entered. The production defect (life, 2026-08-10)
was a _TMP created EARLIER under umask 022, left at 0755, failing the old group/other check on
every subsequent run. A fixture that cannot reproduce the precondition cannot test the fix, so C
pre-creates both levels at 0755 and asserts the precondition before asserting the behaviour.

THE PROBE BUG THIS FILE IS BUILT AROUND, because it will recur in anything driving this hook:

    payload["final"] = True on EVERY chunk.

_accumulate unlinks the turn buffer when `final` is set (:810-814), and _remember then returns
immediately on `not p.is_file()` (:883). So a probe that marks every chunk final destroys the
buffer between chunks and silently tests the per-chunk path. Two consequences, and the second is
the dangerous one:

  · the dedupe assertion FAILS, and reads as a real defect in the code under test.
  · the split-across-chunks assertion PASSES — on the bare second delta, with no accumulation
    having occurred. A green check for a mechanism that never ran.

Hence H1/H2 below split INSIDE a matched token ("...3 ho" | "urs now") and each half is asserted
non-matching ALONE before the split case is allowed to mean anything. Life's own note at
reply-observer.py:791 is the general form: "every probe kept passing because a probe supplies its
violation as a single delta."

LOADING. The module is compiled from source text rather than imported, so a stale .pyc can never
answer for the file on disk. Do not "simplify" this back to importlib — these assertions compare a
file's behaviour against that file, and a cache read defeats the comparison.

HERMETIC. LOG, EVIDENCE, EVIDENCE_DIR, _TMP and ACCUM_DIR are all redirected into a
TemporaryDirectory. _TMP in particular: an earlier revision patched only ACCUM_DIR, so the run
lstat'd and could chmod the REAL $TMPDIR/core-reply-accum-<uid>. Live reply-observations.jsonl is
sha256'd before and after and asserted byte-identical.

WHAT A PASS HERE DOES NOT COVER. Families A-C drive main() with no tool blob, so every row they
produce lands sourced=False; family D therefore calls sourced_for directly rather than through
main(). Family E closes that seam with a real transcript fixture and confirms the forgery reaches
the log through the shipped _turn_tool_blob; what remains unexercised is any transcript shape more
complex than families E and F build. _write_evidence is redirected and its contents are
never asserted. Nothing here says the hook is REGISTERED on MessageDisplay, only that main() behaves
when called. And the denominator (turns observed) is out of scope entirely.

(This paragraph previously listed sourced_for's True branch as uncovered. Family D covers it. A
stale limitations note in the probe written to catch stale documentation is the failure mode this
suite exists for, so it is corrected here rather than left to read as a known gap.)
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

# ROOT IS FOUND BY SEARCHING UPWARD, NOT BY COUNTING PARENTS (changed by life at install).
#
# As authored this read `parents[3]`, which is correct for the probe's home three levels deep in
# core-finance/tasks/si-verification/probes/ and wrong everywhere else. Two consequences, both
# measured before installing:
#
#   · running the authored file from life's cwd still resolved ROOT to CORE-FINANCE, because
#     __file__ does not care where you invoke it from. So it graded finance's copy of
#     reply-observer.py — which lacks e0d2a32 — and reported the sourcing forgery as still
#     reachable. The 2 failures were a stale target, not a live defect. Its own prediction of
#     "39/0 on your tree" could not have come true from that path expression.
#   · at bin/tests/ (two levels deep) parents[3] resolves to ~/AI Projects, so SRC would point at
#     a .claude/hooks that does not exist.
#
# Walking up to the directory that actually contains the hook makes it run correctly from BOTH
# seats, which is the property the whole author-elsewhere/run-elsewhere arrangement depends on.
def _find_root(start: Path) -> Path:
    for cand in [start] + list(start.parents):
        if (cand / ".claude" / "hooks" / "reply-observer.py").is_file():
            return cand
    raise SystemExit("could not locate a Core root containing .claude/hooks/reply-observer.py")


ROOT = _find_root(Path(__file__).resolve().parent)
SRC = ROOT / ".claude" / "hooks" / "reply-observer.py"
LIVE = ROOT / ".claude" / "state" / "reply-observations.jsonl"

# Split INSIDE the matched token so neither half can match alone. Both halves are asserted
# non-matching before the split case is scored — without that, the split passes on the bare delta.
H1 = "we've been at this for 3 ho"
H2 = "urs now"

CASES = [
    ("duration_claim", "we've been at this for 3 hours now"),
    ("state_claim", "the hook is broken"),
    ("cross_core_claim", "this is fixed fleet-wide"),
    ("decision_attribution", "you approved the push"),
    ("say_do_gap", "I'll save that to memory"),
    ("financial_figure", "the balance is $1,240.00"),
    ("deliverable_format", "here's the full table"),
    ("recall_first", "the plan is to ship on Tuesday"),
    ("scope_claim", "there is no consumer anywhere in the repo"),
]


def load():
    """Compile from source text. A .pyc is validated on (mtime, size) and a fix that changes a
    constant in place is a same-size edit, so an import can serve pre-fix bytecode for a
    post-fix file — reporting a landed fix as absent."""
    mod = types.ModuleType("reply_observer_pipeline_probe")
    mod.__file__ = str(SRC)
    exec(compile(SRC.read_text(), str(SRC), "exec"), mod.__dict__)
    return mod


def redirect(mod, tmp):
    """Every write path into the TemporaryDirectory, _TMP included."""
    mod.LOG = tmp / "obs.jsonl"
    mod._TMP = tmp / "accroot"
    mod.ACCUM_DIR = tmp / "accroot" / "acc"
    mod.EVIDENCE_DIR = tmp / "ev"
    mod.EVIDENCE = tmp / "ev" / "evidence.jsonl"


def drive(mod, delta, turn, index=0, final=False):
    """One streamed chunk. `final` defaults False: marking every chunk final unlinks the turn
    buffer and silently converts this into a per-chunk test."""
    payload = {"delta": delta, "index": index, "final": final,
               "session_id": "t012pipe", "turn_id": turn}
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        mod.main()
    finally:
        sys.stdin = saved


def rows(mod):
    if not mod.LOG.exists():
        return []
    return [json.loads(line) for line in mod.LOG.read_text().splitlines() if line.strip()]


def count(mod, kind):
    return sum(1 for r in rows(mod) if r["kind"] == kind)


class Report:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, label, ok, detail=""):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok and detail:
            print("          " + detail)
        if ok:
            self.passed += 1
        else:
            self.failed += 1


def family_a(mod, r):
    """LANDING — every planted positive reaches the log; a benign delta reaches nothing."""
    print("\n=== A. does a firing detector's row REACH the log? ===\n")
    drive(mod, "I read the file and moved on.", "benign", final=True)
    r.check("CONTROL - benign delta produces NO row (%d)" % len(rows(mod)), not rows(mod),
            "a pipeline that logs indiscriminately passes every assertion below for free")
    for kind, delta in CASES:
        drive(mod, delta, "land-" + kind, final=True)
        got = {x["kind"] for x in rows(mod)}
        r.check("%-21s row reaches the log" % kind, kind in got,
                "matched in the pattern regime but no row landed - something between the match "
                "and the write consumed it. kinds so far: %r" % sorted(got))


def family_b(mod, r):
    """STREAM — accumulation, dedupe, redaction."""
    print("\n=== B. the streaming properties the pattern regime cannot see ===\n")
    # DELTAS, not absolutes. Family A already logged a duration_claim, so an absolute `== 0` here
    # fails for a reason that has nothing to do with the halves. Consolidating the families into
    # one log is what introduced that — each family passed standalone against a clean log.
    ctl = count(mod, "duration_claim")
    drive(mod, H1, "ctlA", final=True)
    r.check("CONTROL A - first half alone does not match (+%d)" % (count(mod, "duration_claim") - ctl),
            count(mod, "duration_claim") - ctl == 0)
    ctl = count(mod, "duration_claim")
    drive(mod, H2, "ctlB", final=True)
    r.check("CONTROL B - second half alone does not match (+%d)" % (count(mod, "duration_claim") - ctl),
            count(mod, "duration_claim") - ctl == 0,
            "if either half matches alone the split case can pass with no accumulation at all")

    base = count(mod, "duration_claim")
    drive(mod, H1, "split", 0, False)
    drive(mod, H2, "split", 1, True)
    r.check("SPLIT across two deltas fires (%d)" % (count(mod, "duration_claim") - base),
            count(mod, "duration_claim") - base == 1,
            "ACCUMULATION IS INERT: a claim spanning a chunk boundary is invisible, which is the "
            "hole the file's header documents as fixed")

    fin_base = count(mod, "financial_figure")
    drive(mod, "the balance is $1,240.00", "dedupe", 0, False)
    first = count(mod, "financial_figure") - fin_base
    r.check("dedupe precondition - first chunk logged once (%d)" % first, first == 1)

    matched = [x["matched"] for x in rows(mod) if x["kind"] == "financial_figure"]
    leaked = [t for t in matched if any(c.isdigit() for c in t)]
    r.check("REDACTION - no digits survive into the log (%r)" % (matched[:1] or None),
            bool(matched) and not leaked,
            "a real dollar amount is sitting unredacted in a state file: %r" % leaked)

    drive(mod, " and again the balance is $1,240.00", "dedupe", 1, False)
    added = count(mod, "financial_figure") - fin_base - first
    r.check("DEDUPE - repeat within the turn adds no row (added %d)" % added, added == 0,
            "the numerator inflates against a turn-counting denominator")

    drive(mod, "the balance is $1,240.00", "dedupe2", 0, True)
    r.check("NEGATIVE - the same phrase in a NEW turn does log",
            count(mod, "financial_figure") - fin_base - first == 1,
            "dedupe is over-broad and suppresses across turns, so genuine repeats vanish")


def split_fires(mod, tag):
    before = len(rows(mod))
    drive(mod, H1, tag, 0, False)
    drive(mod, H2, tag, 1, True)
    return len(rows(mod)) - before


def family_c(mod, r, tmp):
    """BRANCH — the repair path on the condition that actually broke production."""
    print("\n=== C. the repair branch (:801-804), on a PRE-EXISTING 0755 tree ===\n")
    mod._TMP = tmp / "t755"
    mod.ACCUM_DIR = tmp / "t755" / "acc"
    mod._TMP.mkdir(mode=0o755)
    mod.ACCUM_DIR.mkdir(mode=0o755)

    def modes():
        return (oct(os.lstat(mod._TMP).st_mode & 0o777),
                oct(os.lstat(mod.ACCUM_DIR).st_mode & 0o777))

    pre = modes()
    r.check("precondition - the fixture really is group/other-accessible %s" % (pre,),
            pre == ("0o755", "0o755"),
            "a 0700 fixture never enters the repair branch, so the family is vacuous")

    n = split_fires(mod, "repair")
    r.check("REPAIR - accumulation survives a pre-existing 0755 dir (%d)" % n, n == 1,
            "the 2026-08-10 fix does not hold here: the exact production condition still "
            "disables accumulation silently, and no probe supplying single deltas would notice")

    post = modes()
    r.check("repair chmod'd BOTH levels %s" % (post,), post == ("0o700", "0o700"),
            "it accumulated but left reply text in a world-readable directory")

    # The refusal the check exists for must survive the repair.
    mod._TMP = tmp / "tlink"
    mod.ACCUM_DIR = tmp / "tlink" / "acc"
    (tmp / "realdir").mkdir()
    (tmp / "tlink").symlink_to(tmp / "realdir")
    n2 = split_fires(mod, "symlink")
    r.check("NEGATIVE - a symlinked _TMP refuses and degrades to per-chunk (%d)" % n2, n2 == 0,
            "the repair swallowed the refusal case: a pre-planted symlink now receives reply text")


def _blob(*calls):
    return " ".join(json.dumps(c) for c in calls)


def _read(fp):
    return {"name": "Read", "input": {"file_path": fp}}


def _bash(cmd):
    return {"name": "Bash", "input": {"command": cmd}}


# Claim, unrelated tool input. NOTHING in the command is a balance — the digits belong to an item
# count, a duration, a filename, an epoch stamp. Every one of these scores SOURCED on the shipped
# code, which is the wrong direction: reply-observer.py:613-615 states unsourced is the safe
# failure because an over-report costs a log line while an under-report hides the measured thing.
FORGERIES = [
    ("$450", "echo processed 1450 items"),
    ("$45", "echo took 3452 ms"),
    ("$120", "ls -la /var/log/1120.txt"),
    ("$1,240.00", "touch -t 1755012400 /tmp/x"),
]

# Genuinely pulled figures, including the rounding shape :536-537 was written for.
PULLED = [
    ("$2.00", "echo 1.9950964999999998"),
    ("$1,240.00", "echo balance 1240.00"),
    ("$450", "echo balance 450"),
    ("$45.20", "echo total 45.2"),
]


def family_d(mod, r):
    """SOURCING — sourced_for's True branch, and whether it can be forged.

    sourced_for is exported precisely so probes exercise the real path (:515-521); re-deriving the
    rule from _SOURCE_TOOLS/_SOURCE_SUPPLY is the defect that docstring records. So it is called
    directly here.

    The finance-relevant case is financial_figure. The digits-provenance fallback at :561-563
    concatenates every digit in the tool blob and substring-tests the claim's first four digits
    against it, so a recalled balance is scored as pulled-live whenever four digits coincide with
    an unrelated number. Sourcing decides whether an observation counts as a violation at all, so
    a false SOURCED silently removes a row from the numerator.
    """
    print("\n=== D. sourcing: the True branch, and whether it can be forged ===\n")
    r.check("CONTROL - state_claim with an empty blob is unsourced",
            mod.sourced_for("state_claim", "the hook is broken", "", "") is False,
            "everything scores sourced and no assertion below carries information")
    r.check("TRUE BRANCH - state_claim backed by a real read is sourced",
            mod.sourced_for("state_claim", "the hook is broken", _blob(_read("/x/a.py")), "") is True,
            "the True branch is unreachable, so every observation scores as a violation")

    same = _blob(_read("/x/a.py"), _read("/x/a.py"))
    r.check("ADVERSARIAL - scope_claim, one file read TWICE, stays unsourced",
            mod.sourced_for("scope_claim", "there is no consumer anywhere", same, "") is False,
            "a doubled read of one file counts as diligence - the substitution :525-527 warns of")
    two = _blob(_read("/x/a.py"), _read("/x/b.py"))
    r.check("scope_claim with TWO distinct targets is sourced",
            mod.sourced_for("scope_claim", "there is no consumer anywhere", two, "") is True,
            "two genuinely distinct reads cannot satisfy the class")

    r.check("ROUNDING - $2.00 against a live 1.9950964999999998 is sourced",
            mod.sourced_for("financial_figure", "$2.00", _blob(_bash("echo 1.9950964999999998")), "") is True,
            "rounding a measured number scores as recall - the 14-of-19 shape at :536-537")
    r.check("a recalled figure with no number in the turn is unsourced",
            mod.sourced_for("financial_figure", "$1,240.00", _blob(_read("/x/notes.md")), "") is False,
            "a fabricated balance scores as pulled-live with no number present at all")

    forged = [(c, cmd) for c, cmd in FORGERIES
              if mod.sourced_for("financial_figure", c, _blob(_bash(cmd)), "")]
    r.check("FORGERY - unrelated digits must not source a balance (%d/%d forged)"
            % (len(forged), len(FORGERIES)), not forged,
            "FALSE SOURCING: %r. The :561-563 fallback substring-tests the claim's digits against "
            "every digit in the blob, so an item count, a duration, a filename or an epoch stamp "
            "sources a recalled balance. Verified fix - compare against PARSED NUMBER TOKENS:\n"
            "            s = bool(d and any(d == re.sub(r'[^0-9]', '', n)\n"
            "                               for n in re.findall(r'\\d+(?:\\.\\d+)?', blob)))\n"
            "          which kills all four forgeries and preserves all four pulled cases."
            % [c for c, _ in forged])
    r.check("REVERSE - every genuinely pulled figure still sources",
            all(mod.sourced_for("financial_figure", c, _blob(_bash(cmd)), "") for c, cmd in PULLED),
            "the class is unsatisfiable and the forgery check above is vacuous")


def _transcript(path, cmd):
    """Minimal two-record transcript: one real user turn, one assistant tool_use.

    _turn_tool_blob scans the file in REVERSE and stops at the first user record that is not purely
    tool_result, so the assistant record must come last. Anything more elaborate would test the
    fixture rather than the code.
    """
    recs = [{"type": "user", "message": {"content": "what is the balance"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def family_e(mod, r, tmp):
    """END-TO-END — is the family D forgery reachable through main(), or only in the unit surface?

    Family D calls sourced_for directly with a hand-built blob, which leaves an obvious hole: if
    _turn_tool_blob emits a different shape, the forgeries are a property of the fixture rather than
    of the system. This family supplies a real transcript_path and drives main(), so the blob is
    produced by the shipped code.
    """
    print("\n=== E. is the forgery reachable END-TO-END through main()? ===\n")
    mod.LOG = tmp / "e2e.jsonl"
    mod._TMP = tmp / "e2eroot"
    mod.ACCUM_DIR = tmp / "e2eroot" / "acc"

    forged_tp = _transcript(tmp / "forge.jsonl", "touch -t 1755012400 /tmp/x")
    blob, _inj = mod._turn_tool_blob({"transcript_path": str(forged_tp)})
    r.check("_turn_tool_blob emits {name,input} JSON - the shape family D assumes",
            bool(blob) and '"name"' in blob and '"input"' in blob,
            "family D's blob shape is invented, so its forgeries say nothing about the real path")

    def drive_tp(delta, turn, tp):
        payload = {"delta": delta, "index": 0, "final": True, "session_id": "e2e",
                   "turn_id": turn, "transcript_path": str(tp)}
        saved = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            mod.main()
        finally:
            sys.stdin = saved

    def last(kind):
        got = [x for x in rows(mod) if x["kind"] == kind]
        return got[-1] if got else None

    drive_tp("the balance is $1,240.00", "ctl", _transcript(tmp / "clean.jsonl", "echo hello world"))
    ctl = last("financial_figure")
    r.check("CONTROL - a digit-free turn leaves the figure unsourced",
            bool(ctl) and ctl["sourced"] is False,
            "every figure sources end-to-end, so the forgery below carries no information")

    drive_tp("the balance is $1,240.00", "forge", forged_tp)
    got = last("financial_figure")
    r.check("FORGERY must not be reachable end-to-end (sourced=%r)" % (got or {}).get("sourced"),
            bool(got) and got["sourced"] is False,
            "CONFIRMED THROUGH main(): a turn whose only number is an epoch stamp in `touch -t` "
            "scores a recalled $1,240.00 as pulled-live. The defect is not an artefact of family "
            "D's hand-built blob - the shipped _turn_tool_blob feeds sourced_for a blob that "
            "triggers it.")


CLOCK = "Session started 2026-08-08 17:29 PDT; current time 2026-08-13 03:01 PDT."


def _padded_transcript(path, pad_bytes):
    """A real user record FIRST, then enough assistant tool_use records to bury it.

    _turn_tool_blob reads a TAIL, so burying the user record beyond the first window is the only
    way to require the adaptive expansion at :634-645. A fixture that fits in one window tests the
    non-adaptive path and passes for the wrong reason.
    """
    recs = [json.dumps({"type": "user", "message": {"content": CLOCK}})]
    one = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 900}}]}})
    recs.extend(one for _ in range(pad_bytes // len(one) + 1))
    path.write_text("\n".join(recs) + "\n")
    return path


def family_f(mod, r, tmp):
    """WINDOW — the adaptive tail expansion, which is the fix for the largest observation class.

    :624-633 records why it exists: a fixed 512KB tail pushed a long turn's OWN user record out of
    view, so the injected clock was invisible and every duration claim in that turn scored
    UNSOURCED. T6 was 175 of 217 observations and the number was quoted.

    The mutant is the load-bearing part. "It captured the clock" is equally consistent with the
    fixture being too short to need expansion, so expansion is disabled in-band and the SAME file
    must lose the clock.
    """
    print("\n=== F. the adaptive tail-window (:634-645) ===\n")
    short = _padded_transcript(tmp / "w-short.jsonl", 0)
    long_ = _padded_transcript(tmp / "w-long.jsonl", 900 * 1024)

    _b, inj = mod._turn_tool_blob({"transcript_path": str(short)})
    r.check("CONTROL - short transcript captures the injected clock",
            "2026-08-13" in inj, "the fixture never supplies a clock; nothing below is meaningful")
    r.check("precondition - long transcript exceeds one 512KB window (%d bytes)"
            % long_.stat().st_size, long_.stat().st_size > 512 * 1024,
            "no expansion is required, so the family is vacuous")

    blob, inj_long = mod._turn_tool_blob({"transcript_path": str(long_)})
    r.check("EXPANSION - the long transcript still captures the clock",
            "2026-08-13" in inj_long,
            "the window never reaches the user record: on a long turn the injected clock is "
            "invisible and every duration claim scores UNSOURCED - the T6 artefact")
    r.check("the tool blob is non-empty in the long case (%d chars)" % len(blob), bool(blob))

    src = SRC.read_text()
    anchor = "if window >= size or window >= 8 * 1024 * 1024:"
    r.check("mutation anchor is unique (%d)" % src.count(anchor), src.count(anchor) == 1,
            "the in-band mutation below would be silently mis-applied")
    capped = types.ModuleType("reply_observer_nocap")
    capped.__file__ = str(SRC)
    exec(compile(src.replace(anchor, "if window >= size or window >= 512 * 1024:"),
                 str(SRC), "exec"), capped.__dict__)
    _b2, inj_capped = capped._turn_tool_blob({"transcript_path": str(long_)})
    r.check("MUTANT with expansion disabled LOSES the clock (%d chars)" % len(inj_capped),
            "2026-08-13" not in inj_capped,
            "the EXPANSION pass is not attributable to expansion - the clock is reachable within "
            "a single window and the long fixture is not actually long enough")


def main():
    before = hashlib.sha256(LIVE.read_bytes()).hexdigest() if LIVE.is_file() else None
    mod = load()
    r = Report()
    print("live _TMP on this seat: %s  mode=%s" % (
        mod._TMP, oct(os.lstat(mod._TMP).st_mode & 0o777) if mod._TMP.exists() else "absent"))
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            redirect(mod, tmp)
            family_a(mod, r)
            family_b(mod, r)
            family_c(mod, r, tmp)
            family_d(mod, r)
            family_e(mod, r, tmp)
            family_f(mod, r, tmp)
    finally:
        after = hashlib.sha256(LIVE.read_bytes()).hexdigest() if LIVE.is_file() else None
        r.check("\n  SAFETY - live reply-observations.jsonl byte-identical", after == before,
                "the probe wrote to the live corpus")
    print("\n=== %d passed, %d failed ===" % (r.passed, r.failed))
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
