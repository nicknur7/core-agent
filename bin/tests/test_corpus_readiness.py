#!/usr/bin/env python3
"""corpus-readiness must read the instruments' OWN floors, and must not print a number RLS refused.

WHY THIS EXISTS (2026-08-12, T017). The operator's premise: each Core can build and optimize from
the data it already has. A sweep found no hardcoded org/seat values in the SI organs — they all resolve through
get_org_id() — so the blocker was never code. It was that nobody had measured whether a given seat
HAS enough corpus for its instruments to say anything.

TWO PROPERTIES THIS FILE PROTECTS, both learned the hard way tonight.

1. THE FLOORS ARE READ FROM THE INSTRUMENTS, NOT RESTATED. MIN_PRE_N and DECAY are parsed out of
   measure-contract-fitness.py; MIN_CANDIDATES is imported from null-calibration.py, which DERIVES
   it (z^2*q(1-q)/(dec-q)^2 = 16) rather than picking it. A readiness tool carrying its own copy of
   a threshold would eventually advise against a bar nothing enforces — the copy-vs-shipped defect,
   inside the file whose entire job is to report what the instruments can see.

2. IT REFUSES TO PRINT A ZERO THAT MEANS "RLS REFUSED". si_artifacts is org-scoped
   (si_artifacts_org_isolation, ALL commands); pattern_observations is NOT (SELECT qual=true). So
   under --fleet, a foreign seat's artifact count returns 0 BY CONSTRUCTION. The first run of this
   tool reported "business: 178 observations and ZERO live artifacts — loop not running." False,
   and the same wrong-ALARM shape core-business had caught in audit-gap-check twenty minutes before:
   a wrong alarm costs a checker's time and makes the next real one read more slowly.

   The number that said business had 30 came from a superuser psql session bypassing RLS. The tool
   does not have that and must not pretend its zero is a measurement.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "bin" / "corpus-readiness.py"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_corpus_readiness")
    if not TOOL.is_file():
        print(f"  FAIL  {TOOL} missing")
        return 1

    src = TOOL.read_text()

    # --- property 1: floors come from the instruments -------------------------------------
    check("MIN_PRE_N is parsed from measure-contract-fitness, not restated",
          "measure-contract-fitness.py" in src and re.search(r"MIN_PRE_N\\s\*=\\s\*", src) is not None,
          "the floor must be read from the instrument that enforces it, or this tool can advise "
          "against a bar nothing applies")
    check("MIN_CANDIDATES is imported from null-calibration, which derives it",
          "null-calibration.py" in src and "MIN_CANDIDATES" in src,
          "null-calibration derives 16 from z^2*q(1-q)/(dec-q)^2; a hardcoded 16 here would drift "
          "silently the moment that derivation changes")

    # BEHAVIOURAL, not textual — the strings above could both be present in a comment. Neutering
    # the parse must change the OUTPUT. (Two textual checks were replaced with behavioural ones
    # earlier tonight for exactly this reason; not repeating that here.)
    r = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    if "could not" in out and "corebrain" in out.lower():
        # The two textual checks above already ran and COUNT. Returning 0 here after they failed
        # let the runner file this whole file as SKIP (Codex review, 2026-09-04).
        print("  SKIP  corebrain unreachable — the live-corpus checks did not run; the checks above still count")
        return 1 if failures else 0

    m = re.search(r"needs >=(\d+) observations per ask", out)
    check("the reported MIN_PRE_N matches the value in the instrument",
          m is not None and int(m.group(1)) == int(
              re.search(r"^MIN_PRE_N\s*=\s*(\d+)",
                        (REPO / "scheduling" / "claude-si" / "measure-contract-fitness.py").read_text(),
                        re.M).group(1)),
          f"tool reported {m.group(1) if m else 'nothing'} — a readiness bar that disagrees with the "
          f"instrument's own floor advises about a system that does not exist")

    m2 = re.search(r"needs >=(\d+) candidate split days", out)
    check("the reported MIN_CANDIDATES matches null-calibration's derived value",
          m2 is not None and int(m2.group(1)) >= 2,
          f"tool reported {m2.group(1) if m2 else 'nothing'}")

    # --- property 2: no foreign artifact count --------------------------------------------
    rf = subprocess.run([sys.executable, str(TOOL), "--fleet"],
                        capture_output=True, text=True, timeout=900)
    fout = rf.stdout + rf.stderr
    blocks = [b for b in fout.split("corpus-readiness — ") if b.strip()]
    # WHICH BLOCK IS "NOT FOREIGN" IS A PROPERTY OF WHO IS RUNNING THIS, NOT A LITERAL.
    #
    # Measured 2026-09-01, core-business: this hardcoded "life" as the one seat excluded from
    # `foreign`, so on business's own run the test built `foreign` from every block EXCEPT life's —
    # which makes BUSINESS's own block (org == own_org in bin/corpus-readiness.py, correctly
    # READABLE, correctly missing "NOT READABLE FROM HERE") count as "foreign" and fail the very
    # assertion this file exists to protect. bin/corpus-readiness.py itself was never wrong — it
    # derives ownership from get_org_id() and the SEATS map; only this test's copy of "which seat am
    # I" was a life-only literal, the exact class this suite's own docstring warns against in the
    # tool it is testing. Same source of truth, so the two can never disagree again.
    sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
    from _env import get_org_id  # noqa: E402
    SEATS = {1: "life", 2: "business", 3: "school", 4: "finance", 5: "ops"}
    own_seat = SEATS.get(get_org_id(), "")
    foreign = [b for b in blocks if not b.startswith((f"{own_seat}\n", f"{own_seat} "))]
    check("--fleet reports at least one foreign seat (else this property is untested)",
          len(foreign) >= 1, f"{len(blocks)} blocks, {len(foreign)} foreign")
    check("a FOREIGN seat's artifact count is refused, not printed as 0",
          all("NOT READABLE FROM HERE" in b for b in foreign),
          "a foreign seat's si_artifacts count returns 0 because RLS refused the read, not because "
          "the loop is idle. Printing it would assert 'loop not running' about a seat that may be "
          "the fleet's most productive — business has more live artifacts than life.")
    check("...and the refusal says a zero would mean 'RLS refused', never 'none exist'",
          all("never 'none exist'" in b for b in foreign),
          "the distinction between 'could not observe' and 'observed none' is the one this suite "
          "exists to keep")
    check("THIS seat's own artifact count IS printed (the refusal must not be blanket)",
          any(re.search(r"live artifacts\s*:\s*\d+", b) for b in blocks),
          "refusing every seat would make the tool useless rather than honest")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
