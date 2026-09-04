#!/usr/bin/env python3
"""AN EXEMPTION SCOPED TO A FILE IS INHERITED BY EVERYTHING ADDED TO THAT FILE LATER.

core-business held this from bus #1036, filed as not-a-blocker: *"Your S5 exemption for
`.claude/rules/privacy.md` is FILE-level. The reasoning for declaring it in the item set inside the
TCB fence rather than as an in-file marker is right and I would not change it — the graded thing must
not control its own exemption. But a real dir-form citation added to privacy.md later would be
silently exempt too."*

Checking it made it worse than the report. The marker sits on **line 1**, which lands in
`head_exempt` and blankets the WHOLE FILE — in the one rules file whose subject is what Core is
allowed to read, and whose own text warns that citing a dir-form spec hands a reviewer the wrong
output contract.

THE DECLARED REASON IS ITSELF THE ARGUMENT FOR ANCHORING. The paths are exempt because they sit in a
TABLE that instructs the reader NOT to cite them. That is a property of three specific lines, not of
the file, so the item set now names those lines and a hit anywhere else is a violation.

Anchored by NORMALISED CONTENT, never a line number: an edit above the table would silently slide a
positional exemption onto different content, which is the same failure wearing a tighter shape.

BOTH DIRECTIONS ARE ASSERTED. A rule that stops exempting everything is trivially satisfied by a rule
that exempts nothing — and that version would fire on the three documented rows, turn S5 permanently
red, and get switched off. The point is a narrower exemption that still works, not a broken one.

Run: python3 bin/tests/test_exemption_is_line_scoped.py
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
RUNNER = ROOT / "bin" / "casebook-run.py"


def load():
    spec = importlib.util.spec_from_file_location("casebook_run", RUNNER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TABLE_ROW = "| `.claude/agents/sentinel/CLAUDE.md` (baseline-only) | **still present** | trust-root |"
REAL_CITE = "See `.claude/agents/sentinel/CLAUDE.md` for the full contract."


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== casebook exemptions are scoped to declared LINES, not to a file ===\n")

    m = load()
    rel = ".claude/rules/privacy.md"

    with tempfile.TemporaryDirectory() as td:
        doc = Path(td) / "privacy.md"
        # Line 1 carries the marker, exactly as the real file does — this is what blanketed everything.
        doc.write_text("<!-- casebook-exempt: S5 — table below documents dir-form specs -->\n"
                       "some prose\n" + TABLE_ROW + "\n" + REAL_CITE + "\n")

        m._DECLARED_EXEMPTIONS.clear()
        m._DECLARED_EXEMPTIONS[("S5", rel)] = {"file": rel, "reason": "table", "lines": [TABLE_ROW]}
        got = {i: ("S5" in ex) for i, ln, fence, ex in m._lines_with_context(doc, rel, "S5")}
        check("the DOCUMENTED table row is still exempt", got.get(3) is True, str(got))
        check("a REAL citation on another line is NOT exempt", got.get(4) is False, str(got))
        check("...even though the line-1 marker used to blanket the file", got.get(2) is False, str(got))

        print("\n--- the anchor is CONTENT, so edits above the table cannot move it ---")
        doc.write_text("<!-- casebook-exempt: S5 -->\nnew paragraph\nanother\n"
                       + "   " + TABLE_ROW + "  \n" + REAL_CITE + "\n")
        got = {i: ("S5" in ex) for i, ln, fence, ex in m._lines_with_context(doc, rel, "S5")}
        check("the row keeps its exemption after moving down two lines and gaining whitespace",
              got.get(4) is True, str(got))
        check("...and the real citation still is not exempt", got.get(5) is False, str(got))

        print("\n--- a declaration with NO lines stays file-level (backwards compatible) ---")
        # Other declarations predate this and must not silently lose their exemption, which would
        # turn unrelated items red for a reason nobody could trace to this change.
        m._DECLARED_EXEMPTIONS.clear()
        m._DECLARED_EXEMPTIONS[("S5", rel)] = {"file": rel, "reason": "whole file"}
        got = {i: ("S5" in ex) for i, ln, fence, ex in m._lines_with_context(doc, rel, "S5")}
        check("an unanchored declaration still exempts the whole file", got.get(5) is True, str(got))

        print("\n--- an UNDECLARED marker still does nothing at all ---")
        m._DECLARED_EXEMPTIONS.clear()
        got = {i: ("S5" in ex) for i, ln, fence, ex in m._lines_with_context(doc, rel, "S5")}
        check("no declaration means no exemption, marker or not",
              all(v is False for v in got.values()), str(got))

    print("\n--- and the SHIPPED item set is actually anchored ---")
    # Without this the mechanism can be correct while the one declaration it was built for quietly
    # reverts to file-level, which is the state this test exists to end.
    import json
    # SKIP, DO NOT CRASH, WHEN THE ITEM SET IS ABSENT (2026-08-12, reported by core-finance).
    #
    # This read `eval/casebook-v1.json` unguarded. On a puller Core that has not yet synced the
    # file, the operator got an uncaught FileNotFoundError and exit 1 — a traceback where a verdict
    # belongs. Exactly the defect I fixed in verify-brain-synced's transcript_dir() the same day,
    # and my own words there: "crashes in the right direction is not the contract this file states."
    #
    # `eval/**` is per_core_keep, not shared.dirs (2026-09-02: the item set is per-Core data,
    # never distributed by the baseline sync — see sync-manifest.json's per_core_keep and its
    # `retired` tombstone for this exact path). So absence here is the NORMAL case on a seat that
    # has not authored its own item set yet, not a sync failure — a shared test may not assume any
    # seat has one. `test_findings_are_arm_stable` already handles absence and is the model.
    #
    # SKIP rather than PASS: the assertions below are the only thing tying the mechanism to the one
    # live declaration it was built for, and silently passing them would let that declaration revert
    # to file-level unnoticed, which is the state this file exists to end. A skip says so out loud.
    _cb = ROOT / "eval" / "casebook-v1.json"
    if not _cb.is_file():
        # `note:` NOT `SKIP` — run-all.sh:173 reads a line starting with SKIP as a FILE-level
        # verdict, and EIGHT checks have already passed above this point. core-finance measured
        # it on a puller: 7 passed, reported as "did not execute".
        print("  note: no local item set on this seat (%s) — cannot verify the live S5\n"
              "        declaration is still line-anchored. Not a pass: eval/ is per-Core data, not"
              "\n        synced — this check only runs once a seat has authored its own casebook."
              % _cb)
        print("\n=== Results: %d passed, %d failed ===" % (p, f))
        return 1 if f else 0
    data = json.loads(_cb.read_text())
    items = data.get("items", data) if isinstance(data, dict) else data
    seq = items if isinstance(items, list) else list(items.values())
    anchored = [ex for it in seq if str(it.get("id", "")).upper() == "S5"
                for ex in (it.get("exemptions") or [])
                if ex.get("file") == rel and ex.get("lines")]
    check("the live S5/privacy.md exemption names its lines", len(anchored) == 1,
          "found %d anchored declarations" % len(anchored))
    if anchored:
        rows = anchored[0]["lines"]
        live = (ROOT / rel).read_text()
        check("...and every named line is still present in the file verbatim",
              all(" ".join(r.split()) in " ".join(live.split()) for r in rows),
              "a named line no longer matches — re-declare it in the item set rather than widening "
              "the exemption back to the file")

    print("\n--- THE DOSE: the real runner still passes S5, and fails on a planted citation ---")
    # In-process checks above cannot show that the live check still works end to end. This runs the
    # shipped runner against the real tree, then against the real tree plus one added citation.
    def s5_line():
        r = subprocess.run(["python3", str(RUNNER)], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=900)
        for ln in (r.stdout + r.stderr).splitlines():
            if " S5 " in ln:
                return ln.strip()
        return "(S5 row not found)"

    live = ROOT / rel
    # SELF-HEAL FIRST. This test appends a probe line to a file that LOADS ON EVERY PROMPT, and a
    # `finally` does not survive a killed process. An interrupted run left
    # "See `.claude/agents/sentinel/CLAUDE.md` for the full contract." in .claude/rules/privacy.md,
    # where it sat as a REAL dir-form citation in live steering — the precise thing S5 exists to
    # forbid, planted by the test that checks S5, and it turned the next suite run red for a reason
    # that had nothing to do with the code.
    #
    # So: strip any leftover probe line BEFORE measuring. Restoring in `finally` handles the clean
    # exit; this handles the one that was not clean, and it is the half that was missing.
    _pre = live.read_text()
    if REAL_CITE in _pre:
        live.write_text(_pre.replace("\n\n" + REAL_CITE + "\n", "").replace(REAL_CITE + "\n", ""))
        print("  NOTE  removed a leftover probe line from %s (an earlier run was interrupted)" % rel)

    original = live.read_bytes()
    try:
        clean = s5_line()
        check("S5 passes on the real tree", clean.startswith("PASS"), clean)
        live.write_bytes(original + b"\n" + REAL_CITE.encode() + b"\n")
        planted = s5_line()
        check("...and FAILS once a real citation is added to the exempted file",
              planted.startswith("FAIL"), planted)
    finally:
        live.write_bytes(original)
    check("the exempted file was restored byte-for-byte", live.read_bytes() == original)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
