#!/usr/bin/env python3
"""Lint code (.sh, .py) for hardcoded path strings that bypass the path registry.

The path registry lives in bin/core-paths.sh + bin/core_paths.py. Every stable
file/dir Core touches has ONE canonical path defined there. If any .sh or .py
hardcodes a path string that's tracked by the registry — and that file isn't
the registry itself — the lint blocks the commit.

Why this exists: the recurring "write here / read there" drift bug (CLAUDE.md /
session.md citing `.claude/hooks/.last-session-start` while stop-hook.sh wrote
to `.claude/state/.last-session-start`). Structural fix is: one source of truth.

USAGE:
  bash bin/lint-code-paths.sh             # human report, exit 1 if drift
  bash bin/lint-code-paths.sh --count     # count only
  bash bin/lint-code-paths.sh --paths f1 f2 ...   # check specific files

ALLOWED EXCEPTIONS (won't fire):
  - The registry files themselves (bin/core-paths.sh, bin/core_paths.py)
  - settings.json (Claude Code reads this raw — needs literal paths)
  - settings.json.template (template — needs literal paths)
  - Comments / docstrings that document the registry path
  - String matches inside *path expansion* of a registry constant
    (e.g., `$CORE_STATE_DIR/.last-session-start` is fine if CORE_STATE_DIR
    is in the registry; the suffix part is treated as registry-referenced)
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

# Load registry's tracked paths.
sys.path.insert(0, str(REPO / "bin"))
try:
    import core_paths
    REGISTRY = core_paths.ALL_TRACKED
except Exception as e:
    print(f"FATAL: cannot import core_paths.py — {e}", file=sys.stderr)
    sys.exit(2)

# Files exempt from the lint (registry itself, raw-string-required configs).
EXEMPT_FILES = {
    "bin/core-paths.sh",
    "bin/core_paths.py",
    "bin/lint-code-paths.py",
    "bin/lint-code-paths.sh",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/settings.json.template",
    # raw-string-required: source_uri "memory/decisions-log.md#<id>" is a ledger IDENTIFIER
    # (never opened/read at that literal), not a filesystem path op — the registry constant
    # (an absolute Path) doesn't fit. (2026-07-18. reconcile-inventory.py was ALSO exempted
    # here initially, but Sentinel-code caught that its scope entries ARE real read_bytes()
    # ops on registry paths — file-exempting them would silence real drift. Fixed at the
    # source instead: reconcile-inventory.py now derives them from core_paths, no literal.)
    "scheduling/brain-pg/decisions_segment.py",  # source_uri "memory/decisions-log.md#<id>" (ledger identifier)
}

# Line-level exemption. A registry path can appear on an executable line without being a path
# OPERATION: prose inside a user-facing message, or a SQL literal matching a stored ledger
# identifier. EXEMPT_FILES is the wrong tool for those — it silences the WHOLE file, and this
# lint has already been burned by exactly that (see the reconcile-inventory.py note above:
# file-exempting it would have hidden real read_bytes() drift on registry paths).
#
# So the exemption is per-line and must carry a reason after the marker, keeping it auditable
# at the point of use rather than in a list nobody re-reads:
#
#   print("... or .claude/identity.json — refusing")  # lint-code-paths: ignore — message text
#
# Both `#` and `--` open the comment, because the line needing the pragma is not always in the
# host language: a registry path inside an embedded SQL literal cannot take a `#` comment (that
# text would be sent to Postgres, which does not use `#`), so SQL's `--` has to work too.
#
# THE MARKER MUST BE IN COMMENT POSITION. The first version of this matched the marker anywhere
# on the raw line, which was a bypass — found by attacking it before it shipped:
#
#   MSG = "run with -- lint-code-paths: ignore"; L = pathlib.Path("tasks/lessons.md")
#
# read CLEAN, because a marker sitting inside a STRING silenced a real path op later on the same
# line. Same shape as the pretooluse-guard defect core-business reported on 2026-07-30, where
# _sync_tokens_clean withdrew an OUTWARD flag it had not raised: a guard must check that the
# thing asking for the exemption is in a position entitled to ask.
#
# So the `#` form is matched ONLY against the trailing-comment region that strip_trailing_comment
# identifies (it already tracks quote state and shell's word-boundary rule for '#'), never the
# code region. The `--` form cannot use that path — it appears inside an embedded SQL string,
# where the host language's quote tracking is meaningless — so it is anchored to end-of-line and
# refuses any quote character after the marker, which is what makes the bypass above fail.
#
# Use this only when the string is not opened, read, written or joined; if it is a real path op,
# fix it at the source with the registry constant instead.
PRAGMA_RE = re.compile(r"#\s*lint-code-paths:\s*ignore\b")

# THE `--` FORM IS GONE, and its removal is the fix rather than a fourth hardening of it.
# It existed for one thing: a registry path inside an embedded SQL literal, where a `#` comment
# would be sent to Postgres. It had exactly ONE user in the whole repo. Against that it was
# defeated three separate times in a single day:
#
#   1. marker inside a string literal          MSG = "-- lint-code-paths: ignore"; L = Path("tasks/lessons.md")
#   2. ordinary shell text (Codex)             cat tasks/lessons.md; echo -- lint-code-paths: ignore
#   3. any .py line at all (sentinel-code)     shutil.copy("memory/decisions-log.md", "/tmp/x")  -- lint-code-paths: ignore
#
# Each fix narrowed the rule and the next attacker walked around the new edge, because nothing
# ever verified the marker was inside a STRING — only which file type it was in and what followed
# it. The single call site now binds its pattern as a named constant carrying the ordinary `#`
# pragma (scheduling/brain-pg/backfill_effective_from.py), which needs no new mechanism at all.
# One user does not justify an escape hatch with three proven bypasses.


def pragma_exempt(raw_line: str, ext: str) -> bool:
    """True if this line carries a lint pragma in a position entitled to grant one.

    Only the trailing-comment region can grant it. strip_trailing_comment() already tracks quote
    state and shell's word-boundary rule for '#', so a marker sitting in code or inside a string
    is never consulted — which is the property every `--`-form bypass lacked.
    """
    code = strip_trailing_comment(raw_line, ext)
    return bool(PRAGMA_RE.search(raw_line[len(code):]))

EXCLUDE_DIR_PARTS = {".git", "node_modules", "archive", "_archive", "venv", ".venv", "tests"}


def is_comment_line(line: str, ext: str) -> bool:
    """True if the line is comment-only (or empty)."""
    s = line.lstrip()
    if not s:
        return True
    if ext == ".sh":
        return s.startswith("#")
    if ext == ".py":
        return s.startswith("#")
    return False


def strip_trailing_comment(line: str, ext: str) -> str:
    """Remove a TRAILING comment (# ...) that begins outside any string literal.
    Both .sh and .py use # for comments, and a comment is never an executable
    path operation — so a path documented in an end-of-line comment (e.g.
    `r"decisions?-log|"  # memory/decisions-log.md`) must not be flagged. Scans
    char-by-char tracking single/double quotes so a '#' INSIDE a string (a URL
    fragment, a literal) is preserved, not mistaken for a comment start."""
    if ext not in (".sh", ".py"):
        return line
    in_single = in_double = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        # A backslash escapes the next char (\" \' \\), so it can't open/close a
        # string. Skip both. Without this, an escaped quote desyncs the quote
        # state and an in-string '#' truncates the line, dropping a real path
        # after it (sentinel-code/Codex catch, 2026-07-12). Shell single-quotes
        # don't honor backslash-escapes, but treating '\' as an escape there only
        # ever keeps MORE of the line (fail toward false-positive, never toward a
        # dropped path), which is the safe direction for a blocking guard.
        if ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # In shell, '#' starts a comment ONLY at a word boundary. A mid-word '#' is NOT a
            # comment — the parameter expansion ${VAR#pat}, arithmetic base 16#ff, or a literal
            # a#b. Cutting there would silently DROP a real hardcoded path that follows on the
            # same line (false negative). Python has no such construct: '#' outside a string is
            # always a comment.
            #
            # A word boundary is not only whitespace. Bash begins a new token after the control
            # operators `;` `|` `&` and after `(`, so `cmd;# comment` is a real comment — and
            # treating it as code meant a legitimately-placed pragma could not be seen, failing
            # the save gate on valid shell. Codex found this 2026-08-04; verified with
            # `echo "See tasks/lessons.md";# lint-code-paths: ignore`, flagged as drift.
            #
            # DELIBERATELY NARROWER THAN THE SUGGESTED SET, which also included `)` and `}`.
            # Those are unsafe here: bash concatenates `$(date)#x` and `${V}#x` into ONE word, so
            # treating `#` after them as a comment start would cut the line early and silently
            # DROP a real path — the false-negative direction, which is the dangerous one for a
            # blocking drift gate. Every widening below is a place bash unambiguously ends a
            # token; anything ambiguous stays code, because over-reporting is recoverable and
            # under-reporting is not.
            if ext == ".sh" and i > 0 and line[i - 1] not in " \t;|&(":
                i += 1
                continue
            return line[:i]
        i += 1
    return line


def strip_inert_blocks(lines: list[str], ext: str) -> list[tuple[int, str]]:
    """Return [(line_no, line), ...] excluding docstrings (.py) and
    heredocs (.sh). Inert means the string content isn't executed code."""
    out = []
    if ext == ".py":
        in_doc = False
        doc_delim = None
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not in_doc:
                # Look for opening of a multi-line docstring
                m = re.match(r'^\s*[rR]?(\'\'\'|""")', line)
                if m:
                    doc_delim = m.group(1)
                    # Check if it closes on the same line
                    rest = line[m.end():]
                    if doc_delim in rest:
                        # single-line docstring — keep line ASSUMING it's not flagged
                        out.append((i, line))
                    else:
                        in_doc = True
                else:
                    out.append((i, line))
            else:
                # In docstring — look for closing delim
                if doc_delim and doc_delim in line:
                    in_doc = False
                    doc_delim = None
                # don't include this line — it's inside a docstring
        return out
    if ext == ".sh":
        in_heredoc = False
        heredoc_tag = None
        for i, line in enumerate(lines, start=1):
            if not in_heredoc:
                # Detect heredoc start: << 'TAG' or <<TAG or <<-TAG
                m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?\s*$", line)
                if m:
                    heredoc_tag = m.group(1)
                    in_heredoc = True
                    # the line itself contains executable code before the <<
                    out.append((i, line))
                else:
                    out.append((i, line))
            else:
                # In heredoc body
                if line.strip() == heredoc_tag:
                    in_heredoc = False
                    heredoc_tag = None
                # don't include heredoc body lines
        return out
    return [(i, l) for i, l in enumerate(lines, start=1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--paths", nargs="*")
    args = ap.parse_args()

    # Build search set
    if args.paths:
        # Apply the same EXCLUDE_DIR_PARTS filter the walk uses below, so an
        # explicitly-passed file (e.g. the staged-file commit guard) gets the
        # same verdict as a full scan. Without this, fixture files under
        # tests/ pass a full scan but fail --paths — path strings inside test
        # prose ("Saved to memory/decisions-log.md") are fixtures, not drift.
        files = [
            Path(p)
            for p in args.paths
            if Path(p).is_file()
            and p.endswith((".sh", ".py"))
            and not any(part in EXCLUDE_DIR_PARTS for part in Path(p).parts)
        ]
    else:
        files = []
        for ext in (".sh", ".py"):
            for p in REPO.rglob(f"*{ext}"):
                if any(part in EXCLUDE_DIR_PARTS for part in p.parts):
                    continue
                files.append(p)

    # For each tracked path, build the "look for this absolute path string OR
    # the repo-relative form" pattern.
    targets = []  # list of (key, absolute_str, relative_str)
    for key, abs_path in REGISTRY.items():
        try:
            rel = str(Path(abs_path).relative_to(REPO))
        except ValueError:
            rel = abs_path
        targets.append((key, abs_path, rel))

    # Scan
    hits = []  # list of (file, line_no, matched_string, registry_key)
    for path in files:
        rel_to_repo = str(path.resolve().relative_to(REPO))
        if rel_to_repo in EXEMPT_FILES:
            continue
        ext = path.suffix
        try:
            lines = path.read_text().splitlines()
        except Exception:
            continue
        for line_no, raw_line in strip_inert_blocks(lines, ext):
            if is_comment_line(raw_line, ext):
                continue
            if pragma_exempt(raw_line, ext):
                continue
            # Drop a trailing comment before matching — a path in a comment is
            # documentation, never an executable path operation.
            line = strip_trailing_comment(raw_line, ext)
            for key, abs_str, rel_str in targets:
                # Look for the relative form ('memory/current-state.md') or
                # absolute form ('/Users/.../memory/current-state.md').
                # Skip if the line uses an env-var or registry var (heuristic:
                # presence of $ or core_paths.).
                for needle in (rel_str, abs_str):
                    if needle not in line:
                        continue
                    # Documentation convention: a backtick-wrapped path inside a
                    # .py string (markdown code span in help/message text, e.g.
                    # grep `memory/decisions-log.md`) is a doc reference, not a
                    # path operation. Backticks are never Python syntax, so this
                    # only ever exempts prose. (.sh backticks = command sub, so
                    # NOT exempted there.) Check the COMMENT-STRIPPED line, not the
                    # raw line — otherwise a backtick mention in a trailing comment
                    # would suppress a real executable occurrence on the same line
                    # (Codex catch, 2026-07-12).
                    if ext == ".py" and f"`{needle}`" in line:
                        continue
                    # Registry-reference exemption — checks the RAW line (comment
                    # INCLUDED). A line that references a registry var/import, or
                    # is documented `# mirrors core_paths.X` / `# path mirrors
                    # $CORE_X`, is trusted: either a suffix under a registry dir
                    # ("$CORE_STATE_DIR/.session-start"), or an INTENTIONAL hardcode
                    # (peer-Core path, target-instance path — genuinely can't use
                    # THIS Core's constant) whose comment IS the author's
                    # suppression note. That note must survive comment-stripping,
                    # so this check uses raw_line, not the stripped line.
                    if "$CORE_" in raw_line or "core_paths." in raw_line:
                        continue
                    hits.append((rel_to_repo, line_no, needle, key))
                    break  # one hit per line is enough

    total = len(hits)
    if args.count:
        print(total)
        sys.exit(0)

    if args.json:
        print(json.dumps(
            {"scanned": len(files), "drift_total": total,
             "hits": [{"file": f, "line": ln, "match": m, "registry_key": k}
                      for (f, ln, m, k) in hits]},
            indent=2,
        ))
        sys.exit(1 if total else 0)

    print(f"Scanned {len(files)} .sh/.py files")
    print(f"Registry-tracked paths hardcoded outside the registry: {total}")
    if not total:
        print("CLEAN — every registry-tracked path is sourced via core-paths.sh / core_paths.py.")
        sys.exit(0)
    print()
    by_file = {}
    for f, ln, m, k in hits:
        by_file.setdefault(f, []).append((ln, m, k))
    for f, items in sorted(by_file.items()):
        print(f"  {f}:")
        for ln, m, k in items:
            print(f"    L{ln}  -->  {m}   (use ${k} or core_paths.{k})")
    print()
    print("Fix: source bin/core-paths.sh and use the constant, OR")
    print("     in Python: from core_paths import <KEY>")
    sys.exit(1)


if __name__ == "__main__":
    main()
