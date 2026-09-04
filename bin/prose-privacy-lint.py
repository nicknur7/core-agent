#!/usr/bin/env python3
"""Does anything that SHIPS to the baseline carry personal-life PROSE, not just an identifier?

WHY THIS EXISTS (2026-09-02, the public-repo incident). bin/strip-check.py hunts identifiers —
name, email, path — derived from your own .claude/identity.json. Every leak found in the incident
that made this repo private again had passed that scan clean, because every one of them had its
identifiers already stripped. What was left was PROSE ABOUT A LIFE, in this repo's own long
WHY-comment convention: the operator's household relationships and school named as the reason for
a partition threshold; a named person plus their citation count; real course codes with assignment
specifics; an outside application's headline metric. No identifier-scrubber can see any of that,
because none of it is an identifier — it is a FACT, in a sentence, that happens to name no one.

So this checks a different thing: not "does this string match a known identifier" but "does this
COMMENT OR DOCSTRING read as a fact about somebody's actual life." Four narrow, deliberately
over-inclusive classes:

  1. RELATIONSHIP-NOUN, stated as a fact in prose — see RELATIONSHIP_RE below for the exact word
     list.  # privacy-ok: naming the mechanism's own trigger words, not a fact about anyone
     (A grammar regex listing those same words as CODE — e.g. brain-recall-trigger.py's
     determiner-reference list — is not prose and is never scanned; see "what counts as prose"
     below.)
  2. COURSE-CODE SHAPE — see COURSE_CODE_RE below.  # privacy-ok: describes the regex, not a course
     Real or invented, this fires; an
     invented fixture that intentionally preserves the shape (this file's own docstring above
     needed one — see hub_ownership.py, verification-trigger.py) is the expected, correct case
     for the `privacy-ok` suppression below, not a design flaw in the check.
  3. A DOLLAR FIGURE near comp/salary/offer/range/pay (within 40 characters).
  4. interview/applied/rejected near a capitalised word (within 40 characters) — a proper noun is
     usually a company or a person, and this class has no way to tell "Anthropic" (fine) from a
     real employer (not fine) without a human reading the line.

WHAT COUNTS AS PROSE, PER FILE TYPE (deliberately narrow, matching the incident):
  .py    — docstrings (via `ast`, so a dict/regex string is never mistaken for one) + real
           comments (via `tokenize`, so a `#` inside a string literal is never mistaken for one).
  .sh    — lines that ARE a `#` comment. Not every line: a shell script's quoted arguments and
           heredocs are code and data, not the narrating prose this incident was made of.
  .md    — every line. A markdown file has no non-prose to exclude.
  .json  — string values under a key whose name contains "comment" (this repo's own convention:
           `_comment`, `_retired_comment`, `_baseline_writer_comment`, ...). Structured fields
           (a regex `pattern`, a `slug`) are DATA, not the narrated-incident prose this check
           targets, and reviewing them is a human job (this pass did it by hand for every hit the
           2026-09-02 audit found — see verification-triggers.json's course_patterns).

SAME FILE SET AS strip-check.py: shared.dirs + shared.files, MINUS per_core_keep — reusing
`shipped_files()` from there rather than re-deriving it, so the two checks can never disagree
about what ships. (strip-check.py itself measured 16,145 false hits from skipping that exclusion
once; no reason to re-earn that lesson here.)

MUST FALSE-POSITIVE. A miss here goes public permanently; a false positive costs one inline
`# privacy-ok: <reason>` marker (or, for .md/.json where an inline `#` would corrupt the example
text itself, the bare substring `privacy-ok:` anywhere on the line) on the flagged line. Exits 1
on any unsuppressed hit, 0 otherwise — never a warning, because a check that can be read as
optional is one people learn to skip past.

Usage:  python3 bin/prose-privacy-lint.py [--list]
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

REPO = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parent.parent))
MANIFEST = REPO / "bin" / "sync-manifest.json"


def _load_strip_check():
    """strip-check.py, imported by path (the filename has a hyphen, so `import` cannot name it).

    Reused for exactly one thing: `shipped_files()`, the manifest-driven enumeration that already
    handles per_core_keep sitting INSIDE a shared dir (scheduling/brain-pg/compile-truth-work and
    friends). Re-deriving that logic here risks the two checks disagreeing about what ships.
    """
    path = Path(__file__).resolve().parent / "strip-check.py"
    spec = importlib.util.spec_from_file_location("strip_check_for_prose_lint", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SUPPRESS_RE = re.compile(r"privacy-ok\s*:", re.I)

RELATIONSHIP_RE = re.compile(
    r"\b(girlfriend|boyfriend|partner|wife|husband|dad|father|mom|mother|sister|brother)\b",
    re.I)
COURSE_CODE_RE = re.compile(r"\b[A-Z]{2,5}\s?\d{3}\b")
MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kKmM]?\b")
MONEY_CONTEXT_RE = re.compile(r"\b(comp|salary|offer|range|pay)\b", re.I)
JOB_EVENT_RE = re.compile(r"\b(interview|applied|rejected)\b", re.I)
# TITLE CASE ONLY: first letter capital, REST lowercase. This repo shouts in ALL-CAPS for
# emphasis constantly (APPLIED, REJECTED, MENTIONED, WHY, PEP) and an all-caps emphasis word is
# not a proper noun — a real name (Anthropic, Nick, Oregon) is Title Case. Excluding
# all-caps is not tuning around false positives, it is a correct reading of "proper noun"; the
# class still fires on any genuine Title-Case name near the trigger word.
PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z][a-zA-Z]*\b")
# Sentence-starters and this repo's own vocabulary that would otherwise swamp class 4 with noise.
# Kept short deliberately: the class is supposed to over-fire, this just keeps it USEFUL.
PROPER_NOUN_STOPLIST = {
    "The", "This", "That", "It", "If", "When", "After", "Before", "So", "And", "But", "We",
    "You", "They", "He", "She", "Not", "Never", "Always", "Because", "Since", "For", "With",
    "From", "Then", "Here", "There", "What", "Which", "Who", "Why", "How", "Nick", "Core",
    "Cores", "Sentinel", "Only", "Same", "One", "Two", "Every", "Each", "Its", "Their", "Was",
    "Were", "Been", "Has", "Have", "Had", "Did", "Does", "Do", "Is", "Are", "Be", "Being",
}

WINDOW = 40


def _classes_for_line(text: str) -> list[str]:
    """Which of the four classes this one physical line of PROSE trips."""
    if SUPPRESS_RE.search(text):
        return []
    hits: list[str] = []
    if RELATIONSHIP_RE.search(text):
        hits.append("relationship-noun")
    if COURSE_CODE_RE.search(text):
        hits.append("course-code-shape")
    for m in MONEY_RE.finditer(text):
        window = text[max(0, m.start() - WINDOW): m.end() + WINDOW]
        if MONEY_CONTEXT_RE.search(window):
            hits.append("comp-figure-near-money-word")
            break
    for m in JOB_EVENT_RE.finditer(text):
        lo, hi = max(0, m.start() - WINDOW), m.end() + WINDOW
        window = text[lo:hi]
        for pm in PROPER_NOUN_RE.finditer(window):
            # A capitalised word cannot be evidence of itself: "Rejected even if..." (the trigger
            # word, sentence-capitalised) must not count as "a nearby proper noun" for its own
            # match — that pattern alone was most of this class's noise.
            abs_start = lo + pm.start()
            if abs_start >= m.start() and abs_start < m.end():
                continue
            if pm.group(0) not in PROPER_NOUN_STOPLIST:
                hits.append("job-event-near-proper-noun")
                break
        else:
            continue
        break
    return hits


def _prose_lines_py(path: Path) -> list[tuple[int, str]]:
    """Docstrings (ast) + real comments (tokenize). Never a code string, dict value, or regex.

    Reads the docstring's RAW SOURCE LINES (via the Expr node's lineno/end_lineno span), not
    `ast.get_docstring(...).splitlines()`. A docstring containing prose that quotes a literal
    escape sequence — e.g. describing a captured value as `"2\\n0"` — has that `\\n` decoded into
    a REAL newline by ast, so the parsed string has one more line than the source file does and
    every line number after it is off by one. Slicing the source by line span is immune to this:
    it reports exactly the physical line the source has, regardless of what any escape decodes to.
    """
    src = path.read_text(errors="replace")
    src_lines = src.splitlines()
    out: list[tuple[int, str]] = []
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if not doc:
                    continue
                body = getattr(node, "body", None)
                if not body:
                    continue
                start = body[0].lineno
                end = getattr(body[0], "end_lineno", None) or start
                for lineno in range(start, end + 1):
                    if 1 <= lineno <= len(src_lines):
                        out.append((lineno, src_lines[lineno - 1]))
    except SyntaxError:
        pass
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string))
    except Exception:
        pass  # a comment-scan that cannot tokenize degrades to docstrings-only, never crashes CI
    return out


def _prose_lines_sh(path: Path) -> list[tuple[int, str]]:
    out = []
    for i, ln in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        stripped = ln.strip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            out.append((i, ln))
    return out


def _prose_lines_md(path: Path) -> list[tuple[int, str]]:
    return list(enumerate(path.read_text(errors="replace").splitlines(), start=1))


def _prose_lines_json(path: Path) -> list[tuple[int, str]]:
    """Only string values under a *comment*-named key — see the docstring's JSON rationale."""
    try:
        text = path.read_text(errors="replace")
        data = json.loads(text)
    except Exception:
        return []
    lines = text.splitlines()
    out: list[tuple[int, str]] = []

    def locate(snippet: str) -> int:
        needle = snippet[:30]
        for i, ln in enumerate(lines, start=1):
            if needle and needle in ln:
                return i
        return 1

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and "comment" in k.lower():
                    out.append((locate(v), v))
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return out


def scan_file(path: Path) -> list[tuple[str, int, str, str]]:
    if path.suffix == ".py":
        lines = _prose_lines_py(path)
    elif path.suffix == ".sh":
        lines = _prose_lines_sh(path)
    elif path.suffix == ".md":
        lines = _prose_lines_md(path)
    elif path.suffix == ".json":
        lines = _prose_lines_json(path)
    else:
        return []
    rel = str(path.relative_to(REPO))
    findings = []
    for lineno, text in lines:
        for cls in _classes_for_line(text):
            findings.append((rel, lineno, cls, text.strip()[:160]))
    return findings


def main() -> int:
    if not MANIFEST.exists():
        print(f"prose-privacy-lint: {MANIFEST} missing — cannot determine what ships")
        return 1

    sc = _load_strip_check()
    files = [p for p in sc.shipped_files() if p.suffix in {".py", ".sh", ".md", ".json"}]

    if "--list" in sys.argv:
        for p in files:
            print(p.relative_to(REPO))
        return 0

    all_findings: list[tuple[str, int, str, str]] = []
    for p in files:
        try:
            all_findings.extend(scan_file(p))
        except Exception as e:
            print(f"prose-privacy-lint: could not scan {p.relative_to(REPO)}: {e}",
                  file=sys.stderr)

    if not all_findings:
        print(f"prose-privacy-lint: clean — 0 hits across {len(files)} shipped file(s)")
        return 0

    print(f"prose-privacy-lint: {len(all_findings)} hit(s) — prose in shipped code that reads "
          "as a fact about a real person's life, not a mechanism:\n")
    for rel, lineno, cls, text in sorted(all_findings):
        print(f"  {rel}:{lineno}  [{cls}]  {text}")
    print("\nEach hit is a LEAD, not a verdict — this check is designed to false-positive (a miss "
          "here ships to a public repo permanently; a false positive costs one marker). If a hit "
          "is not a real leak, suppress it with an inline `# privacy-ok: <reason>` on the same "
          "line (or the bare text `privacy-ok:` anywhere on the line, for .md/.json where a `#` "
          "would corrupt the example itself).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
