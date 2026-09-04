#!/usr/bin/env python3
"""No shared module may reference a stdlib name it never bound — the aliased-import class.

WHY. On 2026-08-26 a one-line `sys.path.insert(...)` was added to `artifact_typer.py` using bare
`Path`. That module imports `from pathlib import Path as _Path`, so the bare name was never bound
and `main()` raised NameError before it reached anything. The 153/154 suite did not catch it: the
line lives in a `__main__`-only CLI entrypoint that no test imports. It was caught by sentinel-code
EXECUTING the entrypoint during review of a baseline push — one review away from shipping to five
seats and to forks.

No pyflakes / flake8 / ruff is installed on this machine, and `py_compile` cannot see it: an unbound
name is a runtime error, not a syntax error. So this is a deliberately narrow stand-in rather than a
general linter. It reports a Name loaded but never bound anywhere in the module, restricted to the
WATCHLIST of stdlib names that are commonly imported under an alias — which is exactly where this
failure lives. Narrow on purpose: a general undefined-name pass over 300+ files would produce false
positives nobody triages, and an unread red is the same as no test.
"""
import ast
import builtins
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHARED = ("bin", "scheduling", ".claude/hooks")

# Names whose import is routinely aliased, so a bare use is a real defect rather than a style note.
WATCHLIST = {
    "Path", "json", "os", "sys", "re", "subprocess", "time", "datetime", "hashlib",
    "tempfile", "shutil", "io", "csv", "argparse", "glob", "sqlite3", "psycopg2",
    "textwrap", "itertools", "collections", "random", "math", "pathlib",
}
BUILTINS = set(dir(builtins))
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def _scope_bindings(body) -> set:
    """Names bound IN THIS SCOPE ONLY — never descending into a nested function/class body.

    Scope-awareness is the whole point. The first version walked the entire module, so a
    `from pathlib import Path` inside ONE function marked `Path` bound for every other function.
    sentinel-code disproved it the right way: it reintroduced the exact aliased-import bug into a
    scratch copy of the REAL artifact_typer.py — which has a local `from pathlib import Path` in an
    unrelated helper — and this checker reported ALL PASS. A ratchet that cannot catch the bug it
    was written for, in the file it was written for, is worse than none, because it is trusted.

    Over-collects WITHIN a scope on purpose: a missed binding is a false positive, and a false
    positive in a ratchet nobody triages is how a red becomes background noise.
    """
    out: set = set()

    def visit(n):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)      # the NAME binds here; the BODY is a different scope
            return
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.asname or a.name.split(".")[0])
            return
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
            return
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
            return
        if isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        if isinstance(n, ast.arg):
            out.add(n.arg)
        for ch in ast.iter_child_nodes(n):
            visit(ch)

    for st in body:
        visit(st)
    return out


def _loads_here(body) -> set:
    """Watchlist names LOADED in this scope, not counting nested function/class bodies."""
    out: set = set()

    def visit(n):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # The BODY is another scope, but the DECORATORS, DEFAULT ARGUMENT VALUES and
            # ANNOTATIONS are evaluated HERE, in the enclosing scope, at def time. Skipping the
            # whole node meant an unbound name in any of them was invisible — named by
            # sentinel-code as a blind spot in this checker rather than found by its own controls.
            for d in getattr(n, "decorator_list", []):
                visit(d)
            a = getattr(n, "args", None)
            if a is not None:
                for d in list(a.defaults) + [k for k in a.kw_defaults if k is not None]:
                    visit(d)
                for arg in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs):
                    if arg.annotation is not None:
                        visit(arg.annotation)
            if getattr(n, "returns", None) is not None:
                visit(n.returns)
            for base in getattr(n, "bases", []):
                visit(base)
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in WATCHLIST:
            out.add(n.id)
        for ch in ast.iter_child_nodes(n):
            visit(ch)

    for st in body:
        visit(st)
    return out


def _nested_defs(body) -> list:
    """Function/class definitions belonging directly to this scope."""
    out: list = []

    def visit(n):
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append(ch)
            else:
                visit(ch)

    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(st)
        else:
            visit(st)
    return out


def _check_scope(body, visible: set, bad: set) -> None:
    vis = visible | _scope_bindings(body)
    bad.update(n for n in _loads_here(body) if n not in vis)
    for d in _nested_defs(body):
        if isinstance(d, ast.ClassDef):
            _check_scope(d.body, vis, bad)
            continue
        a = d.args
        names = {x.arg for x in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs)}
        for extra in (a.vararg, a.kwarg):
            if extra:
                names.add(extra.arg)
        _check_scope(d.body, vis | names, bad)


def undefined_watchlist_names(src: str):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    bad: set = set()
    _check_scope(tree.body, set(BUILTINS), bad)
    return sorted(bad)


def main() -> int:
    # POSITIVE CONTROL — prove the check sees the exact bug it was written for before trusting a
    # clean sweep. A checker that finds nothing looks identical to a clean tree.
    bug = "from pathlib import Path as _Path\ndef main():\n    x = Path('/tmp')\n"
    check("detects the aliased-import bug it exists for", undefined_watchlist_names(bug) == ["Path"])
    ok_src = "from pathlib import Path\ndef main():\n    x = Path('/tmp')\n"
    check("does not flag a correctly-bound import", undefined_watchlist_names(ok_src) == [])
    check("does not flag a locally-assigned name",
          undefined_watchlist_names("def f():\n    json = 1\n    return json\n") == [])

    # DECORATOR / DEFAULT-VALUE / ANNOTATION controls — the blind spot sentinel-code named. These
    # are evaluated in the ENCLOSING scope at def time, so a name unbound there is a real error.
    check("detects an unbound name in a decorator",
          undefined_watchlist_names("from pathlib import Path as _P\n@Path\ndef f():\n    pass\n") == ["Path"])
    check("detects an unbound name in a default argument value",
          undefined_watchlist_names("from pathlib import Path as _P\ndef f(x=Path('/tmp')):\n    pass\n") == ["Path"])
    check("does not flag a decorator whose name IS bound",
          undefined_watchlist_names("import functools\n@functools.cache\ndef f():\n    pass\n") == [])

    # REAL-FILE CONTROL, not a synthetic snippet. The synthetic two-line control above passed
    # while the checker was blind: sentinel-code reintroduced the original bug into a scratch copy
    # of the ACTUAL artifact_typer.py and got ALL PASS, because module-wide binding collection let
    # an unrelated function's local import mask it. A control has to reproduce the module STRUCTURE
    # that made the bug possible, or it only proves the checker works on code shaped like the test.
    _real = REPO / "scheduling" / "claude-si" / "artifact_typer.py"
    if _real.exists():
        _src = _real.read_text(encoding="utf-8", errors="replace")
        _good = 'str(_Path(__file__).resolve().parents[2] / "scheduling" / "brain-pg")'
        _bad = _good.replace("_Path(", "Path(")
        check("real file scans clean as shipped", undefined_watchlist_names(_src) == [])
        if _good in _src:
            check("reintroducing the ORIGINAL bug into the REAL file is caught",
                  "Path" in undefined_watchlist_names(_src.replace(_good, _bad, 1)))
        else:
            check("real-file control still has its anchor", False,
                  "artifact_typer.py no longer contains the line this control mutates")

    scanned, offenders = 0, {}
    for d in SHARED:
        for p in sorted((REPO / d).rglob("*.py")):
            rel = p.relative_to(REPO).as_posix()
            if "/archive/" in rel:
                continue
            scanned += 1
            bad = undefined_watchlist_names(p.read_text(encoding="utf-8", errors="replace"))
            if bad:
                offenders[rel] = bad

    check(f"scanned enough shared files to be meaningful (got {scanned})", scanned >= 50)
    check("no shared module loads an unbound stdlib name", not offenders, f"{offenders}")

    print("\n" + ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "ALL PASS"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
