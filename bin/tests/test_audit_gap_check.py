#!/usr/bin/env python3
"""audit-gap-check must find a planted gap and must not invent one.

WHY THIS EXISTS (2026-08-12). A `!` command never becomes a Claude tool call, so PreToolUse never
fires and sentinel-approve.sh never writes its access-log line. That is what makes Nick's hand-run
push unforgeable by Core — and it means the sanctioned route for the highest-risk class of change
leaves no audit record. Nothing can log it as it happens; there is no hook to hang on. But the apply
writes a line to .last-baseline-sync either way, so the gap is detectable afterward by reconciling
two records the seat already keeps.

THE PREDICTION THIS TOOL FALSIFIED, WHICH IS WHY IT IS WORTH HAVING. I expected life to be clean:
life is the WRITER, Core runs its own pushes through the tool path, so every apply should be logged.
Measured: 7 of 23 applies since 2026-08-06 have no audit entry, four of them clustered inside one
hour on 2026-08-09 — and the access log has NO entries of ANY kind in that window. The auto-logger
has existed since 2026-05-14, so "logging did not exist yet" does not explain it.

WHAT IS PROVEN AND WHAT IS NOT, stated because an over-broad sweep that reads as a finding is the
exact defect the neighbouring probes exist to catch: proven that those applies have no nearby audit
line. NOT proven that Nick ran them, or that anything improper occurred. The tool says so itself.
A detector that names a cause it cannot observe would be the confidently-wrong shape.

THE DOSE: plant a gap in a synthetic seat and require detection; then give every apply an entry and
require silence. Both directions, because a checker that always reports gaps is as useless as one
that never does.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "bin" / "audit-gap-check.py"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _seat(tmp: Path, applies: list[str], audit: list[str]) -> Path:
    root = tmp
    (root / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    (root / "memory").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "state" / ".last-baseline-sync").write_text("\n".join(applies) + "\n")
    (root / "memory" / "access-log.md").write_text("# Access Log\n" + "\n".join(audit) + "\n")
    return root


def _run(root: Path):
    r = subprocess.run([sys.executable, str(TOOL), "--root", str(root)],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


APPLY_A = "2026-08-06T11:59:25-0700 baseline=aaa111 via=sync-to-baseline.sh source=core-life"
APPLY_B = "2026-08-06T14:12:00-0700 baseline=bbb222 via=sync-to-baseline.sh source=core-life"
# 11:59:25 -0700 == 18:59 UTC ; 14:12:00 -0700 == 21:12 UTC
AUDIT_A = "2026-08-06 18:58 UTC | APPROVE | Bash | bash bin/sync-to-baseline.sh"
AUDIT_B = "2026-08-06 21:11 UTC | APPROVE | Bash | bash bin/sync-to-baseline.sh"


def main() -> int:
    print("test_audit_gap_check")
    if not TOOL.is_file():
        print(f"  FAIL  {TOOL} missing")
        return 1

    with tempfile.TemporaryDirectory() as td:
        # --- 1. clean seat: every apply has an entry -> silence, exit 0 --------------------
        root = _seat(Path(td) / "clean", [APPLY_A, APPLY_B], [AUDIT_A, AUDIT_B])
        rc, out = _run(root)
        check("a seat where every apply is logged reports NO gaps",
              rc == 0 and "NO AUDIT ENTRY" not in out,
              f"exit {rc}\n{out[:400]}")

        # --- 2. planted gap: drop ONE entry -> exactly that one is named -------------------
        root = _seat(Path(td) / "gap", [APPLY_A, APPLY_B], [AUDIT_A])
        rc, out = _run(root)
        check("THE DOSE: removing one audit entry is DETECTED",
              rc == 1 and out.count("NO AUDIT ENTRY") == 1,
              f"exit {rc}, {out.count('NO AUDIT ENTRY')} flagged (want exactly 1)\n{out[:400]}")
        check("...and it names the RIGHT apply (21:12, the one whose entry was removed)",
              "21:12" in out and "18:59" not in out.split("NO AUDIT ENTRY")[-1],
              f"flagged the wrong apply:\n{out[:400]}")

        # --- 3. a non-sync audit line must NOT vouch for an apply --------------------------
        # An unrelated approved action minutes earlier standing in for a trust-root apply is a
        # proxy trusted as the thing — the shape this whole suite refuses.
        root = _seat(Path(td) / "proxy", [APPLY_B],
                     ["2026-08-06 21:11 UTC | APPROVE | Bash | git status"])
        rc, out = _run(root)
        check("an unrelated approved action does NOT count as the apply's audit entry",
              rc == 1 and "NO AUDIT ENTRY" in out,
              f"exit {rc} — a `git status` approval vouched for a baseline apply\n{out[:300]}")

        # --- 4. an entry AFTER the apply is not evidence for it ----------------------------
        root = _seat(Path(td) / "after", [APPLY_B],
                     ["2026-08-06 21:30 UTC | APPROVE | Bash | bash bin/sync-to-baseline.sh"])
        rc, out = _run(root)
        check("an audit entry 18 min AFTER the apply does not vouch for it (window is asymmetric)",
              rc == 1,
              f"exit {rc} — the approve is written BEFORE the action runs, so a later entry "
              f"belongs to a different action\n{out[:300]}")

        # --- 4b. the two sources must not render identically -------------------------------
        # core-business's correction, from its own seat: settings.json registers
        # sync-from-baseline.sh --quiet as a SessionStart hook, and a hook-run script bypasses
        # PreToolUse exactly as a `!` command does. Same invisibility, different cause — and the
        # first version of this tool absorbed both into "the apply is real and authorised", which
        # is true of a hand-run and UNESTABLISHED for an automatic pull that nobody chose.
        root = Path(td) / "autopull"
        (root / ".claude" / "state").mkdir(parents=True)
        (root / "memory").mkdir(parents=True)
        (root / ".claude" / "state" / ".last-baseline-sync").write_text(
            "2026-08-12T00:39:08-0700 baseline=4cd0f9c via=sync-from-baseline.sh source=core-business\n"
            "2026-08-11T13:52:33-0700 baseline=cf252cb via=sync-to-baseline.sh source=core-business\n")
        (root / ".claude" / "state" / ".session-start").write_text("2026-08-12 07:38\n")  # UTC, 30s prior
        (root / "memory" / "access-log.md").write_text("# Access Log\n")
        rc, out = _run(root)
        check("an auto-pull near a session start is labelled AUTO-PULL, not lumped in",
              "AUTO-PULL" in out,
              f"both applies rendered identically; the one nobody chose is the one that matters\n{out[:500]}")
        check("...and a push on the same seat stays UNATTRIBUTED, not upgraded to auto-pull",
              "PUSH, unattributed" in out,
              f"the discriminator over-fired and claimed a cause it cannot observe\n{out[:500]}")
        check("...and the auto-pull note says nobody AUTHORISED it, not merely that it went unlogged",
              "no authoriser" in out,
              "an automatic pull has no authoriser at all — saying only that the record is missing "
              "is the reassuring reading business flagged")

        # --- 4c. A PULLER SEAT, IN ITS OWN FORMAT. This is the case the tool got WRONG. ----
        # `via=` is written only by sync-to-baseline.sh, the PUSH script. A pull-only Core never
        # runs it, so NOT ONE of business's 77 lines carries the field and every one fell to the
        # default branch — which was PUSH. The tool asserted that a seat which structurally cannot
        # push had pushed to the baseline 11 times with no audit entry. That is not an unlogged
        # event; it is an event that did not happen, and it reads as an incident.
        #
        # Worse than the interpretation defect it replaced: wrong reassurance goes unchecked, but a
        # wrong ALARM burns the checker's time and makes the next real one read more slowly.
        #
        # It survived because I verified only on life, where role=writer and every line HAS via= —
        # so the default branch never executed on the seat I tested. My own rule, from the commit
        # that shipped the bug: a discriminator that cannot be wrong on the seat you test it on is
        # not tested. Hence this fixture uses business's EXACT line format, not life's.
        root = Path(td) / "puller"
        (root / ".claude" / "state").mkdir(parents=True)
        (root / "memory").mkdir(parents=True)
        (root / ".claude" / "state" / ".last-baseline-sync").write_text(
            "2026-08-12T00:39:08-07:00 baseline=4cd0f9c changed=2 orphans=0\n"
            "2026-08-08T16:52:00-07:00 baseline=68f125f changed=7 orphans=0\n")   # no via= anywhere
        (root / ".claude" / "identity.json").write_text('{"hook_profile":{"role":"puller"}}\n')
        (root / ".claude" / "state" / ".session-start").write_text("2026-08-12 07:38\n")
        (root / "memory" / "access-log.md").write_text("# Access Log\n")
        rc, out = _run(root)
        check("a PULLER's applies are never labelled PUSH (it cannot push — role is the signal)",
              "PUSH" not in out,
              f"the tool claims a pull-only Core pushed to the baseline. That is a false FACT, not "
              f"a missing record.\n{out[:500]}")
        check("...and the one near a session start is still identified as the auto-pull",
              "AUTO-PULL" in out,
              f"role fixed the direction but lost the discriminator that matters\n{out[:400]}")

        # A WRITER with no via= must NOT be silently called a pull either — 3 of life's 140 lines
        # predate the field. Absence is not evidence of a direction; say so.
        root = Path(td) / "writer-legacy"
        (root / ".claude" / "state").mkdir(parents=True)
        (root / "memory").mkdir(parents=True)
        (root / ".claude" / "state" / ".last-baseline-sync").write_text(
            "2026-08-06T11:59:25-0700 baseline=aaa111 changed=3\n")
        (root / ".claude" / "identity.json").write_text('{"hook_profile":{"role":"writer"}}\n')
        (root / "memory" / "access-log.md").write_text("# Access Log\n")
        rc, out = _run(root)
        check("a WRITER's pre-via= line is DIRECTION UNKNOWN, not guessed either way",
              "UNKNOWN" in out,
              f"defaulting a writer's legacy line to a direction is the same guess in the other "
              f"costume\n{out[:400]}")

        # --- 5. empty input must not read as clean ----------------------------------------
        root = _seat(Path(td) / "empty", [], [])
        rc, out = _run(root)
        check("a seat with NO applies exits 2, not 0 — absence is not a clean bill",
              rc == 2 and "must not read as 'no gaps'" in out,
              f"exit {rc} — a zero meaning 'could not observe' must never render as "
              f"'observed none'\n{out[:300]}")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
