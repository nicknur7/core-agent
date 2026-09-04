#!/usr/bin/env python3
"""EVERY CORE→ORG REGISTRY MUST NAME ALL FIVE CORES — because the map is duplicated many times over.

The fleet identity map is duplicated across the tree, and it has gone stale one copy at a time.
`scheduling/graphify-brain/extract-pending.sh:89` carries the receipt in its own comment:

    ops/org5 added 2026-07-18 (was absent → DB-down fallback misrouted ops dirs to org 1)

That fix landed in ONE file. Two siblings in the SAME DIRECTORY kept the stale copy for three weeks,
and `scheduling/brain-pg/_env.py`'s fallback still named three of five. core-business found them on
this tree (bus #1018) minutes before a baseline push that would have propagated all three to every
Core.

WHY THE FAILURE MODE IS THE BAD KIND. These are fallbacks; the primary path queries the `tenants`
table and is correct. So they bite only when the DB is unreachable — and then an unlisted slug does
not error, it falls through to the CALLER's own org. A finance or ops lookup silently resolves to
whoever asked. Wrong-tenant-silently is the worst available answer in a multi-tenant brain, and it
only appears in exactly the conditions where nobody is watching closely.

THE REAL DEFECT IS THE DUPLICATION and this file does not pretend otherwise — it cannot merge the
copies into one, so it does the next best thing: makes the next stale copy loud instead of latent.
A consolidation would be better and is a larger change than a pre-push fix should be. The count is
PRINTED on every run rather than written here, because a number in a docstring is the thing this
whole session has been removing.

Run: python3 bin/tests/test_core_registries_complete.py
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()

# The canonical set. Verified against `SELECT org_id, name FROM tenants` on 2026-08-10; the live
# table is the authority and this list is asserted against it below when the DB is reachable.
CORES = {"life": 1, "business": 2, "school": 3, "finance": 4, "ops": 5}

SKIP = (".venv", "site-packages", "/archive/", "/_archive/", "bin/tests/")

# THREE shapes in use, and the third was found by a peer AFTER this file reported all-clear.
#
# The first version matched two DICT literals and printed "every registry names all five Cores" over
# eight files while a NINTH sat in a shape it could not see — a bare TUPLE of slugs in
# reconcile-vault.py, where a ops file fell through to canons[0] and was filed under whatever slug
# sorted first (core-business, bus #1020).
#
# THE CLAIM WAS WIDER THAN THE MATCHER UNDERNEATH IT. That is the same gap as a docstring promising
# more than its code, and it is worse in a checker, because the whole output is the claim. A
# registry is a LIST OF CORE SLUGS in any container, so the matcher now takes the container shapes
# actually in use and the summary says which shapes it covers.
SLUG_TO_ORG = re.compile(r'\{\s*"life"\s*:\s*1\s*,[^}]{0,300}\}')
ORG_TO_SLUG = re.compile(r'\{\s*1\s*:\s*"life"\s*,[^}]{0,300}\}')
# a bare sequence of slugs: ("life","school",...) or ["life", ...]
SLUG_SEQ = re.compile(r'[\(\[]\s*"(?:life|business|school|finance|ops)"\s*,[^\)\]]{0,300}[\)\]]')


def _literal_core_slugs(node):
    """For a Dict/Tuple/List/Set AST node, return the CORE slugs it names as REAL code --
    dict keys, dict values, or bare sequence elements -- whichever slot the literal uses.

    FOURTH shape found by a peer AFTER this file reported all-clear (2026-09-01, core-business).
    The regex matchers below read raw file TEXT, so they cannot tell a live registry from the same
    five words sitting in a docstring or inside an ordinary string literal. tasks/casebook/
    registry_guard.py -- a business-local self-test suite FOR a stale-registry detector -- got 5
    false FAILs from exactly that: two lines of docstring narrating an ALREADY-FIXED historical
    measurement ("_env.py:313 ... missing finance, ops"), a docstring example using `...` that
    is not even valid Python, and two Python STRING literals used as deliberately-incomplete
    fixture input to registry_guard's own detector (proving IT catches a stale registry -- the
    string is meant to look stale; it is not itself a routing table).

    AST walking cannot make this mistake: a string constant is a leaf (ast.Constant), never a
    Dict/Tuple/List/Set node, so text living inside one is structurally unreachable here --
    the same technique tasks/casebook/registry_guard.py (TASK 8) already proved on this exact
    file: run against itself and its sibling, it reports zero findings for both.
    """
    def strs(elts):
        return {e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}

    if isinstance(node, ast.Dict):
        keys = strs(k for k in node.keys if k is not None)
        vals = strs(node.values)
        for slot in (keys, vals):
            hit = slot & set(CORES)
            if len(hit) >= 2:
                return hit
        return set()
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        hit = strs(node.elts) & set(CORES)
        if len(hit) >= 2:
            return hit
    return set()


# NOT A REGISTRY, ONE NAMED EXCEPTION. AST alone caught template/brain/_build/export.py's
# CWD_PROJECT_RULES as "missing ops" -- a real dict, real Core names, but the wrong shape of
# false positive to fix by tightening the general matcher (tried: requiring the matched slot be a
# pure subset of CORES broke TWO real, currently-complete registries that legitimately carry extra
# non-Core entries alongside all five Cores -- hub_ownership.py's SLUG_TO_ORG, whose keys add
# "core-nick"/"job-hunter"/etc alias routes, and retire-orphan-hubs.py's ORG_NAME, whose values add
# the org-0 "shared" tenant this file's own DB check already treats as OK. Purity is not the axis;
# this one dict is.)
#
# CWD_PROJECT_RULES maps directory-name PATTERNS to project-category slugs -- most entries aren't
# Cores at all ('core-ui' -> 'core-ui-server', 'career-ops' -> 'job-hunter', 'core-example' ->
# 'example'). Its five Core slugs among its values are incidental overlap, not a roster. Its own
# file explains the layered design: `_discovered_core_rules()`, defined a few lines below it,
# derives live Cores from sibling `.claude/identity.json` files and runs FIRST in
# `categorize_cwd()` -- but degrades to {} with no sibling layout on disk (a fork, a CI checkout,
# any stranger's standalone clone), and this static map is the documented fallback for exactly
# that case. 'core-ops' WAS absent here (fixed 2026-09-03, bin/tests/test_cwd_routing.py caught
# it on a fresh clone with no sibling core-* dirs -- the derived path masked the gap on every seat
# that happens to have the full sibling layout, which is every seat that had ever verified this
# comment "live"). Kept in _NOT_A_REGISTRY regardless: still not a Core roster, just a map whose
# values now happen to cover all five.
_NOT_A_REGISTRY = {("template/brain/_build/export.py", "CWD_PROJECT_RULES")}


def registries():
    out = []
    for p in sorted(list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.sh"))):
        s = str(p)
        if any(x in s for x in SKIP):
            continue
        try:
            txt = p.read_text(errors="replace")
        except Exception:
            continue

        if p.suffix == ".py":
            # AST FIRST for .py -- only an actual literal Dict/Tuple/List/Set counts as a
            # registry. Falls through to the regex path below ONLY if the file fails to parse,
            # so a malformed .py file still gets scanned rather than silently dropping out.
            try:
                tree = ast.parse(txt)
            except SyntaxError:
                tree = None
            if tree is not None:
                rel = str(p.relative_to(ROOT))
                py_lines = txt.splitlines()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Dict, ast.Tuple, ast.List, ast.Set)):
                        slugs = _literal_core_slugs(node)
                        if not slugs:
                            continue
                        # _NOT_A_REGISTRY is keyed on (path, assignment-target name), checked
                        # against the literal's own source line rather than hardcoding a lineno --
                        # a lineno drifts the next time someone edits above it; the variable name
                        # naming this exact dict does not.
                        src_line = py_lines[node.lineno - 1] if node.lineno - 1 < len(py_lines) else ""
                        if any(rel == f and name in src_line for f, name in _NOT_A_REGISTRY):
                            continue
                        snip = "{" + ", ".join('"%s"' % c for c in sorted(slugs)) + "}"
                        out.append((rel, node.lineno, snip))
                continue

        # .sh files (bash cannot be ast-parsed), and any .py that failed to parse above, still go
        # through the original text-regex scan.
        lines = txt.splitlines()
        for rx in (SLUG_TO_ORG, ORG_TO_SLUG, SLUG_SEQ):
            for m in rx.finditer(txt):
                line = txt[:m.start()].count("\n") + 1
                # SKIP COMMENTS. The first run accused scheduling/brain-pg/mcp-server.py:64 — which
                # is a COMMENT quoting the old stale value while documenting that this very defect
                # was fixed on 2026-07-29. The live literal three lines below is complete.
                #
                # Matched-prose-as-code, in the checker written about registries, on its first run.
                # core-business hit the identical shape with its substring gate (a docstring naming
                # ALREADY_GATED) and I called that a false positive at the time. Same class, mine now.
                src_line = lines[line - 1] if line - 1 < len(lines) else ""
                if src_line.lstrip().startswith("#"):
                    continue
                # A REGISTRY NAMES SEVERAL CORES; A TOPIC LIST HAPPENS TO CONTAIN ONE. The bare
                # -sequence matcher above fires on any list whose first element is one of the five
                # words, and its first run accused make-3d.py:432 —
                # `["school", "people", "career", "projects", ...]` — a JavaScript CATEGORY list
                # where "school" is a subject, not a seat. Requiring two DISTINCT Core slugs
                # separates a fleet registry from a coincidence, and keeps every real one (the
                # smallest genuine registry here names four).
                if len({c for c in CORES if '"%s"' % c in m.group(0)}) < 2:
                    continue
                out.append((str(p.relative_to(ROOT)), line, m.group(0)))
    return out


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== every Core→org registry must name all five Cores ===\n")

    # THE CANONICAL SET IS CHECKED AGAINST THE DB, not just asserted. A test that hardcodes five
    # names would itself go stale on the day a sixth Core appears — which is the exact defect it
    # exists to catch, one level up.
    r = subprocess.run(["psql", "corebrain", "-U", "brain_app", "-tAc",
                        "SELECT org_id, name FROM tenants ORDER BY org_id;"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode == 0 and r.stdout.strip():
        live = {}
        for ln in r.stdout.strip().splitlines():
            if "|" in ln:
                oid, name = ln.split("|", 1)
                live[name.strip()] = int(oid)
        # SUBSET, not equality (2026-08-31). `hub_ownership.py`'s SHARED_ORG fix landed the SAME
        # day this line went red: a 6th tenants row, (0, "shared") — the shared-hub tier "read by
        # every Core and owned by none", not a Core with a directory to route requests to. CORES
        # is the routing table checked against every per-file registry below; "shared" belongs in
        # NEITHER, because there is no `core-shared/` directory for a fallback dict to route into.
        # Equality made a deliberate, correct addition (a real tenant row, 863 real entities under
        # it) read as the exact stale-registry shape this file exists to catch. The five real
        # Cores must still be present and correctly numbered — that part stays exact — but the
        # live table is now allowed to carry non-Core tenants CORES was never meant to enumerate.
        # SCOPED TO THIS INSTALL (2026-09-04, instance #11 of "shared code assumes the writer"):
        # CORES is the example fleet's registry, and every registry LITERAL in the code must name all
        # of it — that part is install-independent. But the live tenants table on a fresh single-seat
        # clone holds one row, so demanding all five there failed every stranger on first run. A seat
        # is expected in the table if it is on disk beside this one or already in the table; those
        # must agree on org_id. On the writer all five are on disk, so this stays exactly as strict.
        expected = {k: v for k, v in CORES.items()
                    if k in live or (ROOT.parent / ("core-" + k)).is_dir() or ROOT.name == "core-" + k}
        missing_or_wrong = {k: v for k, v in expected.items() if live.get(k) != v}
        extra = {k: v for k, v in live.items() if k not in CORES}
        check("every Core in this file's canonical set is present in the live tenants table "
              "with the same org_id (extra non-Core tenants, e.g. the org-0 shared tier, are OK)",
              not missing_or_wrong,
              "missing/wrong=%s  live=%s  expected-here=%s — update CORES, the table is the authority"
              % (missing_or_wrong, live, expected))
        if extra:
            print("    note: live tenants table also carries %s (not in CORES — expected to be "
                  "non-routable, e.g. the shared hub tier)" % extra)
    else:
        print("  SKIP  corebrain unreachable; cannot confirm the canonical set against the table")

    found = registries()
    check("registries were actually found (an empty scan is not a clean scan)", bool(found),
          "no Core→org map matched — if the shape changed, re-point this rather than deleting it")
    print("  %d registry literal(s) across the tree\n" % len(found))

    stale = []
    for rel, line, lit in found:
        missing = [name for name in CORES if '"%s"' % name not in lit]
        if missing:
            stale.append((rel, line, missing))
    check("every registry names all five Cores", not stale,
          "\n          ".join("%s:%d missing %s" % (r, l, m) for r, l, m in stale))

    _bad = {(r, l) for r, l, _ in stale}
    for rel, line, _ in found:
        if (rel, line) not in _bad:
            print("    ok  %s:%d" % (rel, line))

    print("\n--- THE DOSE: a stale registry must be detected ---")
    # Without this, "0 stale" is satisfied by a matcher that finds nothing or a check that never
    # compares. Uses the exact literal that was live in two files until today.
    old = '{1: "life", 2: "business", 3: "school", 4: "finance"}'
    missing = [n for n in CORES if '"%s"' % n not in old]
    check("the pre-fix literal is flagged as missing ops", missing == ["ops"],
          "detector reports %s for a literal known to be missing ops" % missing)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
