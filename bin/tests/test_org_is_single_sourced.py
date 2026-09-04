#!/usr/bin/env python3
"""No shared-code path may resolve this seat's org by reading CORE_ORG_ID directly.

WHY. `_env.get_org_id()` has been the hardened resolver since 2026-08-05 — identity wins over the
environment, and a disagreement between the two is reported loudly — because a session env var
leaked across a `cd` into another Core is the normal failure mode and the env is the thing that
lies. On 2026-08-26 twenty-two sites across the shared tree still bypassed it, and TWENTY of them
spelled the bypass `os.environ.get("CORE_ORG_ID", "1")`. That default is a SILENT FALLBACK TO LIFE:
on school, finance, ops or business, any invocation that did not inherit the variable read — and
in the promote / graduate / project / brain-extract paths WROTE — org 1's partition.

It surfaced as a red suite, not as a data bug, which is the only reason it was found. core-school
reported three tests in scheduling/claude-si/tests BROKEN under both interpreters while run-all.sh
on core-life reported the same directory ALL GREEN. Both readings were correct: the fixtures
stamped `"org_id": 1` into every spec, `install()` validates the spec's org against the RESOLVED
org, and so the suite could only pass on the one seat whose identity is 1 — the baseline writer,
which is also the seat that decides whether the baseline is green.

The same class is on record twice more in the files it touched. `extract-core-sessions.py` carries
a comment about a ops org-1 misroute where "the fix landed in one file and two siblings in this
directory kept the stale copy — one registry, four copies, corrected one at a time as each bit."
And this fixture's own header records core-business finding three artifacts "stamped org 1: each
seat carrying the other's test fixtures."

So the invariant is enforced rather than documented a fourth time. Only two bypasses are legitimate
and both are named below: `_env.py` itself, which IS the resolver, and one deliberate fail-OPEN
fallback in a hook that must degrade to local behaviour rather than fail.

STRIPS COMMENTS AND DOCSTRINGS BEFORE SCANNING. Four tests written earlier the same day matched
their own explanatory prose and passed while testing nothing; this file quotes the forbidden
pattern repeatedly and would be its own first false positive.
"""
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# INCLUDES THE TEST TREES. core-ops: "if the suite that certifies single-sourcing contains
# files that source it three other ways, the certification is scoped to production and silent
# about itself" -- the same shape as certifying green from the only seat where green was
# structurally guaranteed, one layer down. Three test files were bypassing when this scanner
# shipped excluding exactly the directory it lives in.
SHARED = ("bin", "scheduling", ".claude/hooks")
# WHITESPACE-TOLERANT ON PURPOSE. code_only() reassembles via tokenize.untokenize, which emits
# `os .environ .get (` -- a tight regex matches the raw file and misses the scanned form, so the
# offender scan passes on a tree full of offenders. Third vacuous-pass in this one file; the
# positive control below is what stops there being a fourth.
# READS ONLY -- a fixture that WRITES a controlled env (os.environ["CORE_ORG_ID"] = ...,
# dict(os.environ, CORE_ORG_ID=...)) is constructing a sandbox, not deciding which org it is.
# `setdefault` counts as a READ-and-decide: core-ops found one at module scope pinning the whole
# process to org 1 on any seat where the variable happened to be unset.
PATTERN = re.compile(
    r"os\s*\.\s*(?:environ\s*\.\s*(?:get|setdefault)\s*\(|getenv\s*\()\s*[\"']CORE_ORG_ID[\"']"
    r"|os\s*\.\s*environ\s*\[\s*[\"']CORE_ORG_ID[\"']\s*\](?!\s*=[^=])")

# The only legitimate readers. Anything else must call _env.get_org_id().
ALLOW = {
    "scheduling/brain-pg/_env.py":            "IS the resolver",
    ".claude/hooks/brain-recall-trigger.py":  "deliberate fail-OPEN fallback, identity checked first",
}

# A WRITE OF A LITERAL IS A PIN. The read-pattern above deliberately exempts writes, on the theory
# that a fixture assigning the env is constructing a sandbox rather than deciding which org it is.
# That exemption was too wide and core-school proved it live: they pulled the "fix", two of three
# tests went green, and test_procedure_artifact still failed on org mismatch because line 44 said
#     os.environ["CORE_ORG_ID"] = "1"
# — a hard assignment, which is the org pin wearing a sandbox's clothes. It survived the sweep that
# removed the `setdefault` pins because it does not read anything.
#
# The distinction that actually matters is not read-vs-write, it is LITERAL-vs-RESOLVED. Assigning
# `str(get_org_id())` is a sandbox; assigning `"1"` is a decision, and on four seats it is the wrong
# one. Four files carried it.
WRITE_PIN = re.compile(
    r"os\s*\.\s*environ\s*\[\s*[\"']CORE_ORG_ID[\"']\s*\]\s*=\s*[\"']\d+[\"']"
    r"|os\s*\.\s*environ\s*\.\s*setdefault\s*\(\s*[\"']CORE_ORG_ID[\"']\s*,\s*[\"']\d+[\"']")

# THE ARGUMENT SIDE. Two sweeps missed this and core-school found it: `_validate_spec(spec, org)`
# compares the spec's org against a TRUSTED org passed in, and seven call sites passed a literal 1
# while the specs had been fixed to resolve. Spec side right, argument side wrong, so the two
# disagreed on every seat whose org is not 1 — which is the original bug, re-entered by the door
# the fix left open. Same for `promote(1, ...)`, `generate(1, ...)`, `auto_apply_directive(1, ...)`.
#
# Caught structurally rather than by regex: a literal int where an org belongs is unambiguous, and
# unlike the read/write patterns above it cannot be spelled a dozen ways.
ORG_FIRST_ARG = {
    "install", "install_shadow_block", "upsert", "project", "verify_invariants", "tune_pass",
    "dedupe_active", "promote", "rearm_shadowed", "auto_promote", "generate",
    "auto_apply_directive", "refresh_workflow_payloads", "generate_from_workflows",
    "ask_cases", "extract_pending", "cache_asks",
}
ORG_LAST_ARG = {"_validate_spec"}


def literal_org_args(src: str) -> list:
    """Call sites passing a bare int where this seat's org belongs."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        fn = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
        if fn in ORG_FIRST_ARG:
            a = n.args[0]
        elif fn in ORG_LAST_ARG:
            a = n.args[-1]
        else:
            continue
        if isinstance(a, ast.Constant) and isinstance(a.value, int) and not isinstance(a.value, bool):
            out.append(f"{fn}(...{a.value}...) line {getattr(n, 'lineno', '?')}")
    return out


FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def code_only(src: str) -> str:
    """Source with comments and TRIPLE-QUOTED prose removed - ordinary literals KEPT.

    The first version stripped every STRING token, which deletes the CORE_ORG_ID literal that IS
    the pattern being hunted: the offender scan then matched nothing anywhere and passed for
    exactly the reason this file exists to prevent. Its own allowlist-staleness assertion caught
    that - which is the argument for asserting an allowlist is live rather than trusting it.

    So: comments go (they quote the pattern), triple-quoted blocks go (this docstring quotes it
    repeatedly), single-line literals stay - that is where a real offender lives."""
    out = []
    _TRIPLE = (chr(34) * 3, chr(39) * 3)  # spelled via chr() so this file never contains one
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.string.lstrip('rbufRBUF')[:3] in _TRIPLE:
                continue
            out.append((tok.type, tok.string))
        # REASSEMBLE, do not newline-join. Joining tokens with newlines splits `os` `.` `environ`
        # onto separate lines, so the pattern cannot match across them and every file scans clean --
        # the second way this scanner managed to pass while checking nothing, caught the same way.
        return tokenize.untokenize(out)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src  # unparseable -> scan raw rather than skip it


def main() -> int:
    scanned = 0
    offenders: list[str] = []
    for d in SHARED:
        for p in sorted((REPO / d).rglob("*.py")):
            rel = p.relative_to(REPO).as_posix()
            if "/archive/" in rel or rel in ALLOW:
                continue
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            scanned_src = code_only(src)
            if PATTERN.search(scanned_src) or WRITE_PIN.search(scanned_src):
                offenders.append(rel)
            elif literal_org_args(src):
                offenders.append(f"{rel} (literal org ARGUMENT: {literal_org_args(src)[:2]})")

    # POSITIVE CONTROL. A scanner that finds nothing is indistinguishable from a clean tree, and
    # this one reported clean twice while broken. Prove it can still see the thing it hunts.
    _KEY = chr(34) + "CORE_ORG_ID" + chr(34)  # split so THIS file is not its own offender
    _bait = 'x = os.environ.get(' + _KEY + ', "1")\n'
    check("scanner detects a known offender (positive control)", bool(PATTERN.search(code_only(_bait))))
    check("scanner survives the reassembly it actually uses",
          bool(PATTERN.search(code_only('def f():\n    return os.environ.get(' + _KEY + ', "1")\n'))))
    check("scanner detects a module-level setdefault pin",
          bool(PATTERN.search(code_only('os.environ.setdefault(' + _KEY + ', "1")\n'))))
    check("scanner does NOT flag a fixture WRITING a controlled env",
          not PATTERN.search(code_only('os.environ[' + _KEY + '] = str(get_org_id())\n')))
    check("scanner detects a HARD-ASSIGNED org literal (the pin school found)",
          bool(WRITE_PIN.search(code_only('os.environ[' + _KEY + '] = "1"\n'))))
    check("scanner still allows a RESOLVED assignment",
          not WRITE_PIN.search(code_only('os.environ[' + _KEY + '] = str(get_org_id())\n')))
    check("scanner detects a literal org ARGUMENT (the class school found)",
          literal_org_args("inst._validate_spec(spec, 1)\n") != []
          and literal_org_args("sg.promote(1, dry=True)\n") != [])
    check("scanner allows a RESOLVED org argument",
          literal_org_args("inst._validate_spec(spec, ORG)\nsg.promote(ORG, dry=True)\n") == [])
    check("scanner ignores the pattern in a comment",
          not PATTERN.search(code_only('# os.environ.get(' + _KEY + ', "1")\nx = 1\n')))
    check(f"scanned enough shared files to be meaningful (got {scanned})", scanned >= 50)
    check("no shared file resolves org by reading CORE_ORG_ID directly",
          not offenders, f"offenders: {offenders}")

    # The allowlist must stay honest: every entry has to still exist and still contain a read.
    for rel in sorted(ALLOW):
        p = REPO / rel
        check(f"allowlisted reader still exists: {rel}", p.exists())
        if p.exists():
            check(f"allowlist entry is not stale: {rel}",
                  bool(PATTERN.search(code_only(p.read_text(encoding='utf-8', errors='replace')))),
                  "no longer reads CORE_ORG_ID — drop it from ALLOW")

    # And the resolver it points everyone at must actually be importable and identity-first.
    sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
    try:
        from _env import get_org_id  # noqa: F401
        check("the single resolver _env.get_org_id is importable", True)
    except Exception as e:
        check("the single resolver _env.get_org_id is importable", False, str(e))

    print("\n" + ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "ALL PASS"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
