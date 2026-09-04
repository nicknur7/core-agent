#!/usr/bin/env python3
"""Lint brain-pg SQL for unsafe org_id interpolation (multi-tenant scope guard).

The corebrain DB is multi-tenant: every row in entities / evidence / entity_edges
carries an org_id, and cross-org isolation rests on RLS + parameterized queries.
There are exactly TWO safe ways org_id may enter a SQL string in this codebase:

  1. The canonical self-scope literal, with NO interpolation:
        org_id = current_setting('app.current_org_id')::bigint
  2. An IN-list of placeholders, with the ints passed as bound params:
        org_id IN ({placeholders})    # placeholders = ",".join(["%s"] * n)

Anything else — an f-string that drops a variable straight next to org_id, or a
%/.format() built SQL fragment mentioning org_id — is a scope-bypass / injection
risk. This linter flags those so a future edit can't silently un-scope a query.

It does NOT flag missing org_id scoping: the default 'all' scope (RLS read_all)
is a deliberate design choice, not a bug.

USAGE:
  python3 bin/lint-org-scoping.py            # human report, exit 1 if violations
  python3 bin/lint-org-scoping.py --count    # just the count (for SessionStart/CI)
  python3 bin/lint-org-scoping.py --json      # machine output

WHEN IT FAILS:
  A SQL string interpolates org_id without binding it as a parameter. Rewrite to
  use current_setting(...) (self scope) or an IN ({placeholders}) + params list.
  Never f-string a raw org_id value into SQL.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

def _core_root() -> Path:
    """This Core's root — env first, then git, then the file's own location.

    Anchoring on __file__ ALONE resolves the wrong Core the moment a peer runs this file from its
    own seat, which is exactly how core-business's clean enforcement-audit result turned out to be
    a second read of life. Correct in normal use, wrong during cross-Core review — and cross-Core
    review is the operation the fleet's autonomy depends on.
    """
    env = os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        p = Path(env).expanduser()
        if (p / ".claude").is_dir():
            return p.resolve()
    try:
        import subprocess as _sp
        top = _sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, timeout=10).stdout.strip()
        if top:
            return Path(top).resolve()
    except Exception:
        pass
    return Path(__file__).resolve().parents[1]


REPO = _core_root()
os.chdir(REPO)

# Files that build SQL against the multi-tenant tables.
SCAN_GLOB = "scheduling/brain-pg/*.py"

# Variable names that legitimately hold a "%s,%s,..." placeholder string.
SAFE_PLACEHOLDER_VARS = {"placeholders", "ph", "in_clause", "qs"}

# The canonical safe self-scope literal (no interpolation).
SAFE_LITERAL_RX = re.compile(r"current_setting\(\s*['\"]app\.current_org_id['\"]\s*\)")

# org_id immediately followed (within ~40 chars, no closing quote) by an
# f-string interpolation {var}. The capture is the interpolated name.
FSTRING_INTERP_RX = re.compile(r"org_id[^'\"\n]{0,40}?\{([a-zA-Z_][\w\.\[\]]*)\}")

# org_id in a string that is then %-formatted or .format()'d on the same line.
PERCENT_FORMAT_RX = re.compile(r"org_id[^'\"\n]{0,60}['\"]\s*%\s*[\(\w]")
DOTFORMAT_RX = re.compile(r"org_id[^'\"\n]{0,60}['\"]\s*\)?\.format\(")
# org_id glued to a SQL fragment via string concatenation with a variable.
CONCAT_RX = re.compile(r"org_id\s*(?:=|<|>|<=|>=|IN)[^'\"\n]{0,20}['\"]\s*\+\s*[a-zA-Z_]")


# Lines that EMIT TEXT rather than execute it. A diagnostic mentioning org_id is not a SQL
# injection risk, and until 2026-08-09 this linter could not tell the two apart — it fired on
# `org_id` plus any f-string interpolation, with no notion of SQL at all.
#
# THE FALSE POSITIVES WERE INVISIBLE BECAUSE THE GATE WAS DEAD. session-lifecycle.sh consumed
# `--count` through a command substitution that mangled the value, so the pre-commit block had
# never once fired. Fixing only the deadness would have converted a silent no-op into a gate
# that blocks every commit staging _env.py — and a gate that blocks correct work is one someone
# disables, which is strictly worse than the no-op it replaced. A DEAD MECHANISM HIDES THE STATE
# OF WHAT IT WAS MEASURING.
#
# Deliberately narrow: it exempts only when the line emits AND does not execute. `print(sql)`
# beside `cur.execute(sql)` on one line is still flagged, because the execute is what matters.
EMITTER_RX = re.compile(r"\b(print|log(?:ger)?\.\w+|logging\.\w+|warn(?:ings)?\.\w+|"
                        r"raise\s+\w+|sys\.std(?:err|out)\.write)\s*\(")
EXECUTOR_RX = re.compile(r"\b(execute|executemany|executescript|cursor|copy_expert|"
                         r"execute_values|execute_batch|read_sql\w*)\s*\(")


def _emitter_lines(src: str) -> set:
    """Line numbers spanned by a text-EMITTING call that does not also execute SQL.

    Line-local regex is not enough and the real tree proves it: the two false positives were
    CONTINUATION LINES of a multi-line print(), so the line carrying `org_id={ident}` has no
    `print(` on it at all. Parsing gives the call's true span; a regex would have to guess at
    bracket continuation, and guessing at syntax is how the interpreter-flag enumeration failed
    five times tonight.

    Falls back to no exemptions when the file does not parse — a syntax error must never widen
    what this linter lets through.
    """
    try:
        import ast
        tree = ast.parse(src)
    except Exception:
        return set()

    def name_of(fn):
        if isinstance(fn, ast.Name):
            return fn.id
        if isinstance(fn, ast.Attribute):
            base = fn.value
            root = base.id if isinstance(base, ast.Name) else ""
            return "%s.%s" % (root, fn.attr)
        return ""

    EMIT = {"print"}
    EMIT_ATTR = {"write", "info", "debug", "warning", "warn", "error", "critical", "exception"}
    EXEC = {"execute", "executemany", "executescript", "copy_expert",
            "execute_values", "execute_batch"}

    out = set()
    for node in ast.walk(tree):
        span = None
        if isinstance(node, ast.Call):
            n = name_of(node.func)
            attr = n.split(".")[-1]
            if n in EMIT or attr in EMIT_ATTR:
                # An emitter that ALSO executes on the same expression is not exempt.
                if not any(isinstance(d, ast.Call) and name_of(d).split(".")[-1] in EXEC
                           for d in ast.walk(node)):
                    span = (node.lineno, getattr(node, "end_lineno", node.lineno))
        elif isinstance(node, ast.Raise):
            span = (node.lineno, getattr(node, "end_lineno", node.lineno))
        if span:
            out.update(range(span[0], span[1] + 1))
    return out


def scan_file(path: Path):
    violations = []
    src = path.read_text(encoding="utf-8")
    exempt = _emitter_lines(src)
    for lineno, raw in enumerate(src.splitlines(), 1):
        line = raw.strip()
        if "org_id" not in line:
            continue
        if line.startswith("#"):
            continue
        # AN EXECUTE ON THE LINE VETOES EVERY EXEMPTION. The AST span is per-LINE, so a line
        # carrying both `log.info(...)` and `cur.execute(...)` was exempted whole — dropping a
        # planted real violation from 5 detected to 4. Caught by the liveness probe and by
        # nothing else: the false-positive count and the baseline both looked perfect while the
        # detector had quietly gone blind to a real case. Silencing noise and silencing the
        # detector produce the same clean number, which is the entire lesson of this sweep.
        if not EXECUTOR_RX.search(line) and (lineno in exempt or EMITTER_RX.search(line)):
            continue

        # f-string interpolation adjacent to org_id
        for m in FSTRING_INTERP_RX.finditer(line):
            var = m.group(1)
            if var.split(".")[0].split("[")[0] not in SAFE_PLACEHOLDER_VARS:
                violations.append((lineno, raw.rstrip(), f"org_id interpolated with {{{var}}}"))
        if PERCENT_FORMAT_RX.search(line):
            violations.append((lineno, raw.rstrip(), "org_id SQL built via %-formatting"))
        if DOTFORMAT_RX.search(line):
            violations.append((lineno, raw.rstrip(), "org_id SQL built via .format()"))
        if CONCAT_RX.search(line):
            violations.append((lineno, raw.rstrip(), "org_id SQL built via string concat"))

    # ONE FINDING PER (LINE, REASON). A line carrying two interpolations of the same shape used to
    # emit two identical findings — same lineno, same code, same reason — which inflates --count
    # without naming anything new.
    #
    # The specimen is the liveness fixture's own `emits_AND_executes`:
    #
    #     log.info(f"scoping to org_id = {org}"); cur.execute(f"SELECT 1 WHERE org_id = {org}")
    #
    # Both f-strings match, so 5 planted violations reported as 6. The line IS a real violation —
    # the cur.execute half is unsafe — so the gate blocked correctly and nothing was ever
    # mis-blocked. What was wrong was the NUMBER, and the test asserted `hits >= 5`, which passes
    # at 6 and at 60 and cannot tell a double-count from a sixth defect.
    #
    # Deduping cannot mask a violation: `> 0` is unchanged by collapsing duplicates of the same
    # line, and two DIFFERENT reasons on one line still produce two findings.
    seen, deduped = set(), []
    for v in violations:
        key = (v[0], v[2])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(v)
    return deduped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", action="store_true", help="print only the violation count")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    findings = {}
    scanned = 0
    for path in sorted(REPO.glob(SCAN_GLOB)):
        scanned += 1
        v = scan_file(path)
        if v:
            findings[str(path.relative_to(REPO))] = v
    total = sum(len(v) for v in findings.values())

    # A CLEAN RESULT AND AN UNRUN SCAN MUST NOT LOOK IDENTICAL (2026-08-12, found by core-finance).
    #
    # `--json` printed `{}` and `--count` printed `0` whether this scanned 28 files or zero. There
    # was no denominator anywhere in the output, so "nothing unsafe found" and "nothing looked at"
    # were byte-identical strings. session-lifecycle.sh consumes --count for the ORG-SCOPE RISK
    # alarm, so a renamed directory — anything that makes SCAN_GLOB match nothing — silently
    # disables the multi-tenant guard while reporting a clean bill of health. Fail-open, and quiet.
    #
    # The sibling lint-doc-paths.py:469 already emits "scanned": len(md_files) for this reason. As
    # with the --count exit code above, the fix was recorded in this repo before this file needed
    # it. That is twice now that this linter re-learned a lesson its own sibling had already
    # written down, which is an argument about where these two files should live, not about the
    # patch.
    # AN EMPTY SCAN EMITS A NON-NUMERIC TOKEN ON STDOUT, EXIT 0.
    #
    # REVERSAL, recorded because the first reasoning was published and was wrong. I initially
    # refused to signal an empty scan through --count at all, on the grounds that every loud option
    # fails open:
    #
    #     ORG_LINT_COUNT=$(... --count 2>/dev/null || echo 0)
    #     if (( ORG_LINT_COUNT > 0 ))
    #
    # That is true of the EXIT-CODE channel and false of stdout, and the distinction is the entire
    # fix. `|| echo 0` fires ONLY on a non-zero exit. Three lines below the one I was staring at,
    # session-lifecycle.sh already pipes the value through a helper built for exactly this:
    #
    #     _count_or_report() { [[ $_val =~ ^[0-9]+$ ]] && { printf %s "$_val"; return 0; }
    #                          echo "INTERNAL: ${_label} produced no usable count … — guard is
    #                                INERT this pass, not clean" >&2; printf 0; }
    #
    # Five existing call sites. So a non-numeric token sails past `|| echo 0` untouched, lands in
    # the helper, and the operator is told the guard was INERT rather than clean — the exact
    # distinction I had called unreachable. Found by core-finance, who read one channel over from
    # where I was looking.
    #
    # Blocking semantics are still UNCHANGED: the helper substitutes 0, so an inert guard does not
    # block the close, it becomes loud instead of silent. Making inert BLOCK is a real semantics
    # change to a shared hook and still does not belong inside a linter. Two caveats worth keeping:
    # the message goes to stderr, so whether Nick sees it depends on how session-lifecycle routes
    # stderr at that point; and a positive integer remains the only value that blocks, which is why
    # emitting a fake count is still off the table — a guard that reports violations it did not
    # find is one nobody believes.
    if scanned == 0:
        print(f"lint-org-scoping: WARNING — SCAN_GLOB matched 0 files (glob={SCAN_GLOB!r} under "
              f"{REPO}). An empty scan is not a clean scan.", file=sys.stderr)
        if args.count:
            # Deliberately non-numeric, deliberately exit 0. Both halves are load-bearing.
            print("INERT-EMPTY-SCAN")
            return 0

    if args.count:
        print(total)
        # EXIT 0 EVEN WITH VIOLATIONS. --count is consumed by a command substitution in
        # session-lifecycle.sh:92 written as `$(... --count 2>/dev/null || echo 0)`. Exiting
        # non-zero fired that `|| echo 0`, so the caller captured the two-line string "2\n0",
        # which then failed its own `^[0-9]+$` guard — AND THE PRE-COMMIT BLOCK NEVER FIRED.
        # Not once, with two live violations in the tree at the time this was found.
        #
        # The count IS the signal here; the exit code is redundant with it and actively
        # destroys it. lint-doc-paths.py:450 and lint-code-paths.py already do exactly this
        # for exactly this reason — the fix was recorded twice in this repo before this file
        # needed it. Callers wanting a pass/fail should run without --count.
        return 0
    if args.json:
        # `scanned` and `violation_total` are the denominator this output had no way to express:
        # `{}` meant both "28 files, all clean" and "0 files, nothing looked at". Same key name as
        # lint-doc-paths.py:469 so one reader can consume both linters. `findings` is nested rather
        # than kept at the top level, which is a breaking shape change — deliberate, because the
        # old shape's whole problem was that a bare mapping had nowhere to put the denominator.
        print(json.dumps(
            {"scanned": scanned,
             "violation_total": total,
             "findings": {f: [{"line": ln, "code": code, "why": why} for ln, code, why in v]
                          for f, v in findings.items()}},
            indent=2))
        return 1 if total else 0

    if not total:
        print("lint-org-scoping: OK — no unsafe org_id interpolation found.")
        return 0
    print(f"lint-org-scoping: {total} violation(s) — unsafe org_id in SQL:\n")
    for f, v in findings.items():
        for ln, code, why in v:
            print(f"  {f}:{ln}  [{why}]")
            print(f"      {code.strip()}")
    print("\nFix: use current_setting('app.current_org_id')::bigint (self scope) "
          "or org_id IN ({placeholders}) with bound params. Never f-string a raw value.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
