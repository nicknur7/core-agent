#!/usr/bin/env bash
# test_fresh_spawn_privacy.sh — what a STRANGER receives when they clone nicknur7/core-agent.
#
# WHY THIS EXISTS. Every other check in this repo runs against the WRITER Core's own working
# tree. None of them answer the actual publish-safety question: clone the baseline fresh, the
# way a stranger would, and see what lands. This does that for real — no dry-run, no assumption
# that the manifest matches the live repo.
#
# METHOD: clone https://github.com/nicknur7/core-agent.git into a scratch dir, then overlay this
# Core's shared paths on top of it (the same shared.dirs/shared.files/per_core_keep split
# sync-to-baseline.sh uses) so the assertions see TODAY's fixes, not whatever was last pushed.
# per_core_keep paths (.claude/identity.json, memory/, sessions/, tasks/, CLAUDE.md, .mcp.json,
# ...) are NEVER touched by the overlay — exactly like the real sync — so whatever the baseline
# repo already has there is exactly what a fresh clone gives a stranger, overlay or not.
#
# Self-cleaning (mktemp + trap), read-only against this repo, no git operations of any kind,
# no writes to any core-* dir or the live database. Safe to re-run any time.
#
# Usage: bash bin/tests/test_fresh_spawn_privacy.sh

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO/bin/sync-manifest.json"
BASELINE_URL="https://github.com/nicknur7/core-agent.git"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/fresh-spawn-privacy.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
CLONE="$WORK/baseline"

pass_n=0
fail_n=0
pass() { printf 'PASS  %s\n' "$1"; pass_n=$((pass_n + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '%s\n' "$2" | sed 's/^/      /'; fail_n=$((fail_n + 1)); }

echo "=== test_fresh_spawn_privacy ==="
echo "clone: $BASELINE_URL -> $CLONE"

if ! git clone --quiet "$BASELINE_URL" "$CLONE" 2>"$WORK/clone.err"; then
  fail "0. clone baseline" "$(cat "$WORK/clone.err")"
  echo
  echo "$pass_n passed, $fail_n failed"
  exit 1
fi
pass "0. clone baseline ($(cd "$CLONE" && git rev-parse --short HEAD))"

# ── Overlay: this Core's shared paths on top of the fresh clone (mirrors sync-to-baseline.sh's
# rsync logic, minus any git operation). per_core_keep entries nested inside a shared dir are
# excluded exactly as the real push excludes them.
#
# GITIGNORE-HONORING, same as sync-to-baseline.sh (2026-07-27 fix in the real script, missing
# HERE until now — core-business, 2026-09-01). Without it this overlay is a plain filesystem
# rsync of the whole shared dir, and rsync does not consult .gitignore, so a locally-gitignored
# runtime file sitting in a shared dir gets swept into the simulated clone and FAILS assertions
# 1/2 for content that could never actually reach the real baseline: sync-to-baseline.sh's own
# GITIGNORED_PATHS guard excludes exactly this file from every real push. Measured directly on
# business: `scheduling/claude-si/friction-cases.jsonl` (real transcript excerpts, real user
# paths) is gitignored (see `.gitignore`) and NOT git-tracked (`git ls-files` empty for it) —
# this overlay shipped it anyway before this fix, reproducing the exact bug the comment at
# sync-to-baseline.sh:133-139 already documents fixing in the real script. Fixing the SIMULATION
# to match what it claims to simulate, not weakening what it asserts once something IS shipped.
# macOS ships bash 3.2 (no mapfile) — read into arrays the portable way.
SHARED_DIRS=(); while IFS= read -r x; do [[ -n "$x" ]] && SHARED_DIRS+=("$x"); done < <(jq -r '.shared.dirs[]' "$MANIFEST")
SHARED_FILES=(); while IFS= read -r x; do [[ -n "$x" ]] && SHARED_FILES+=("$x"); done < <(jq -r '.shared.files[]' "$MANIFEST")
KEEP=(); while IFS= read -r x; do [[ -n "$x" ]] && KEEP+=("$x"); done < <(jq -r '.per_core_keep[]' "$MANIFEST")
GITIGNORED=$(git -C "$REPO" ls-files --others --ignored --exclude-standard --directory 2>/dev/null || true)

overlaid=()
for d in "${SHARED_DIRS[@]}"; do
  src="$REPO/$d/"
  [[ -d "$src" ]] || continue
  dst="$CLONE/$d/"
  mkdir -p "$dst"
  excl=(--exclude '__pycache__' --exclude '*.pyc')
  for p in "${KEEP[@]}"; do
    [[ "$p" == "$d/"* ]] || continue
    rel="${p#"$d"/}"; rel="${rel%/\*\*}"
    excl+=(--exclude "$rel")
  done
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    [[ "$p" == "$d/"* ]] || continue
    rel="${p#"$d"/}"; rel="${rel%/}"
    excl+=(--exclude "$rel")
  done <<< "$GITIGNORED"
  rsync -a "${excl[@]}" "$src" "$dst"
  overlaid+=("$d/")
done
for f in "${SHARED_FILES[@]}"; do
  src="$REPO/$f"
  [[ -f "$src" ]] || continue
  mkdir -p "$(dirname "$CLONE/$f")"
  cp "$src" "$CLONE/$f"
  overlaid+=("$f")
done
echo "overlaid ${#overlaid[@]} shared paths from $REPO onto the clone:"
printf '  %s\n' "${overlaid[@]}"
echo

# ── Assertion 1: strip-check.py, run FROM the shipped copy, against the shipped set ──────────
if CORE_INSTANCE="$CLONE" python3 "$CLONE/bin/strip-check.py" >"$WORK/sc1.out" 2>&1; then
  pass "1. bin/strip-check.py passes against the actual shipped set"
else
  fail "1. bin/strip-check.py passes against the actual shipped set" "$(cat "$WORK/sc1.out")"
fi
sed 's/^/      /' "$WORK/sc1.out"

# ── Assertion 2: independent re-scan, same PATTERNS/ALLOW as strip-check.py. Keeps its
# self-file skip (a checker necessarily contains the literals it searches for — the same
# principle that puts "nicknur7/core-agent" in ALLOW, just implemented as a separate line instead of
# a regex alternative) but drops the archive/ skip: strip-check.py's own comment calls that one
# "a NOTE not an exemption", i.e. a deliberately-flagged gap, not a considered exclusion — so an
# independent check should look there rather than inherit it.
python3 - "$CLONE" >"$WORK/sc2.out" <<'PY'
import importlib.util, os, re, sys
clone = sys.argv[1]
os.environ["CORE_INSTANCE"] = clone
spec = importlib.util.spec_from_file_location("strip_check", f"{clone}/bin/strip-check.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)
rx = re.compile("|".join(sc.PATTERNS))
hits = []
for p in sc.shipped_files():
    if p.suffix in sc.SKIP_SUFFIX or p.name == "strip-check.py":
        continue
    try:
        txt = p.read_text(errors="ignore")
    except OSError:
        continue
    for i, line in enumerate(txt.splitlines(), 1):
        if rx.search(line) and not sc.ALLOW.search(line):
            hits.append(f"{p.relative_to(clone)}:{i}: {line.strip()[:160]}")
print(f"scanned {len(sc.shipped_files())} shipped files, same PATTERNS/ALLOW as strip-check.py "
      f"(self-file skipped, archive/ NOT skipped)")
for h in hits:
    print(h)
print(f"__HITS__={len(hits)}")
PY
hits2=$(grep -c '^__HITS__=0$' "$WORK/sc2.out" || true)
sed 's/^/      /' "$WORK/sc2.out"
if [[ "$hits2" == "1" ]]; then
  pass "2. no shipped file carries author surname/email/real-homedir path (independent scan, strip-check's ALLOW respected)"
else
  fail "2. no shipped file carries author surname/email/real-homedir path (independent scan, strip-check's ALLOW respected)"
fi

# ── Assertion 3: .claude/identity.json in the shipped template is generic ────────────────────
IDJ="$CLONE/.claude/identity.json"
if [[ -f "$IDJ" ]]; then
  name=$(jq -r '.user.name // ""' "$IDJ")
  org=$(jq -r '.org_id // -1' "$IDJ")
  email=$(jq -r '.user.email // ""' "$IDJ")
  body=$(cat "$IDJ")
  id_ok=1
  [[ "$name" == YOUR_* ]] || { id_ok=0; echo "      user.name not a placeholder: $name"; }
  [[ "$org" == "1" ]] || { id_ok=0; echo "      org_id not 1: $org"; }
  [[ -z "$email" || "$email" == *"example.com" ]] || { id_ok=0; echo "      user.email looks real: $email"; }
  # Derive this Core's OWN real name/full_name at runtime (never hardcode it in this file —
  # the file itself ships under bin/, so a literal copy of the real name here would be a leak
  # strip-check.py would rightly flag) and confirm the shipped template doesn't carry it.
  #
  # GUARDED AGAINST AN UNPERSONALIZED $REPO (2026-09-03, found auditing a fresh clone). On any
  # seat that has not filled in its own .claude/identity.json — a fresh clone, a fork, this
  # suite's own scratch environment — $REPO/.claude/identity.json IS STILL the generic template
  # itself, so `.user.full_name` resolves to the placeholder string "YOUR FULL NAME", not a real
  # name. The old check then grepped the CLONE's own (also still-generic) identity.json for that
  # same placeholder text and — of course — found it, since both files are the identical
  # template: a false positive comparing the placeholder against itself, on every seat that has
  # not yet been personalized. `[[ "$real_full" != YOUR* ]]` mirrors the exact placeholder test
  # `name` already uses two lines above; only a REAL, personalized full_name is worth checking
  # the shipped clone for.
  real_full=$(jq -r '.user.full_name // empty' "$REPO/.claude/identity.json" 2>/dev/null)
  if [[ -n "$real_full" && "$real_full" != YOUR* ]] && grep -qF "$real_full" "$IDJ"; then
    id_ok=0; echo "      identity.json contains this Core's real full_name"
  fi
  if [[ "$id_ok" == "1" ]]; then
    pass "3. .claude/identity.json ships generic (name=$name org_id=$org email=${email:-<absent>})"
  else
    fail "3. .claude/identity.json ships generic"
  fi
else
  fail "3. .claude/identity.json ships generic" "identity.json missing from shipped template"
fi

# ── Assertion 4a: coreuser.py resolver — three states ─────────────────────────────────────────
CU_SRC="$CLONE/.claude/hooks/lib/coreuser.py"
if [[ -f "$CU_SRC" ]]; then
  # State 1: unconfigured fresh Core (identity.json still carries the shipped YOUR_FIRST_NAME placeholder)
  s1="$WORK/state1"; mkdir -p "$s1/.claude/hooks/lib"
  cp "$IDJ" "$s1/.claude/identity.json"
  cp "$CU_SRC" "$s1/.claude/hooks/lib/coreuser.py"
  out1=$(cd "$s1" && env -u CORE_USER_NAME python3 .claude/hooks/lib/coreuser.py)
  [[ "$out1" == "the operator" ]] && pass "4a-i. unconfigured fresh Core -> \"the operator\" (got \"$out1\")" \
    || fail "4a-i. unconfigured fresh Core -> \"the operator\"" "got: \"$out1\""

  # State 2: configured identity.json with a real name (deliberately NOT Nick)
  s2="$WORK/state2"; mkdir -p "$s2/.claude/hooks/lib"
  jq '.user.name = "Taylor"' "$IDJ" > "$s2/.claude/identity.json"
  cp "$CU_SRC" "$s2/.claude/hooks/lib/coreuser.py"
  out2=$(cd "$s2" && env -u CORE_USER_NAME python3 .claude/hooks/lib/coreuser.py)
  [[ "$out2" == "Taylor" ]] && pass "4a-ii. configured identity.json (name=Taylor) -> \"Taylor\" (got \"$out2\")" \
    || fail "4a-ii. configured identity.json -> configured name" "got: \"$out2\""

  # State 3: no identity.json findable anywhere up the tree (never a hardcoded real-name fallback)
  s3="$WORK/state3/deeply/nested"; mkdir -p "$s3"
  cp "$CU_SRC" "$s3/coreuser.py"
  out3=$(cd "$s3" && env -u CORE_USER_NAME python3 coreuser.py)
  [[ "$out3" == "the operator" ]] && pass "4a-iii. no identity.json anywhere in tree -> \"the operator\" (got \"$out3\")" \
    || fail "4a-iii. no identity.json anywhere in tree -> \"the operator\"" "got: \"$out3\""

  # Source-level guarantee: the ONLY fallback constant is the generic string, never a real name.
  fb=$(grep -c '^FALLBACK = "the operator"$' "$CU_SRC" || true)
  bad_fb=$(grep -n 'return "Nick"\|FALLBACK = "Nick"' "$CU_SRC" || true)
  if [[ "$fb" == "1" && -z "$bad_fb" ]]; then
    pass "4a-iv. FALLBACK constant is the generic string, no hardcoded-name return path in source"
  else
    fail "4a-iv. FALLBACK constant is the generic string" "$bad_fb"
  fi
else
  fail "4a. coreuser.py resolver present in shipped hooks" "missing: $CU_SRC"
fi

# ── Assertion 4b: independent sweep of every shipped hook for hardcoded "Nick" in a RUNTIME
# string (not a comment, not a docstring). AST-based for .py (module/class/function docstrings +
# tokenizer COMMENT tokens + any bare string-literal statement are treated as documentation,
# matching this repo's own stated convention — "comments keep the name, they are history not
# output" from the 2026-08-29 sweep commit); "#"-prefixed lines for .sh. This is a heuristic, not
# a proof — flagged hits still need a human read (see report), but it independently reproduces
# and extends the 2026-08-29 sweep's own claim instead of taking it on trust.
python3 - "$CLONE/.claude/hooks" >"$WORK/nick_sweep.out" <<'PY'
import ast, io, pathlib, re, sys, tokenize

root = pathlib.Path(sys.argv[1])
NAME_RE = re.compile(r'\bNick\b')

def doc_lines_py(src):
    lines = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return lines
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                n0 = body[0]
                for ln in range(n0.lineno, n0.end_lineno + 1):
                    lines.add(ln)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for ln in range(node.lineno, node.end_lineno + 1):
                lines.add(ln)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                lines.add(tok.start[0])
    except Exception:
        pass
    return lines

_HEREDOC_PY = re.compile(r'\bpython3\b.*<<-?\s*([\'"]?)(\w+)\1')

def doc_lines_sh(src):
    """'#'-prefixed lines, PLUS the AST-true doc/comment lines of any python3 heredoc embedded in
    this .sh file — a `.sh` script that pipes a script into `python3 - <<'PY' ... PY` carries real
    Python source, docstrings included, and the naive '#'-only rule cannot see a docstring line
    (no leading '#') as documentation.

    FOUND BY THIS TEST ITSELF, against ITS OWN trust-root targets: `_cmd_in_report()`'s docstring
    in .claude/hooks/sentinel-approve.sh and `_verdict_of()`'s in sentinel-receipt.sh both name
    "Nick" in prose explaining WHY a rule exists — exactly the "history not output" convention the
    2026-08-29 sweep already carved out for .py docstrings — but both files ship as .sh (they are
    bash wrappers with an embedded `python3 ... <<'PY'` body), so the .sh branch flagged them as
    live runtime hits. Verified byte-identical to the baseline repo's current copy before writing this fix —
    both the failing assertion and the two hook files it flags are the BASELINE's own copies, not
    local drift, so the false positive was not something tonight's changes introduced.
    """
    lines = src.splitlines()
    skip = {i for i, line in enumerate(lines, 1) if line.strip().startswith("#")}
    i = 0
    while i < len(lines):
        m = _HEREDOC_PY.search(lines[i])
        if not m:
            i += 1
            continue
        delim = m.group(2)
        start = i + 1  # first line of heredoc BODY (0-indexed into `lines`)
        end = start
        while end < len(lines) and lines[end].strip() != delim:
            end += 1
        body = "\n".join(lines[start:end])
        for ln in doc_lines_py(body):
            skip.add(start + ln)          # offset: body's line 1 is `lines[start]`, i.e. file line start+1
        i = end + 1
    return skip

hits = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.suffix not in (".py", ".sh"):
        continue
    if "__pycache__" in path.parts:
        continue
    src = path.read_text(errors="ignore")
    skip = doc_lines_py(src) if path.suffix == ".py" else doc_lines_sh(src)
    for i, line in enumerate(src.splitlines(), 1):
        if NAME_RE.search(line) and i not in skip:
            archived = "archive" in path.parts
            hits.append((str(path.relative_to(root.parent.parent)), i, line.strip()[:140], archived))

for f, i, l, archived in hits:
    print(f"{'[ARCHIVE] ' if archived else ''}{f}:{i}: {l}")
live = [h for h in hits if not h[3]]
print(f"__LIVE__={len(live)}")
print(f"__ARCHIVE__={len(hits) - len(live)}")
PY
sed 's/^/      /' "$WORK/nick_sweep.out"
live_n=$(grep -oE '__LIVE__=[0-9]+' "$WORK/nick_sweep.out" | cut -d= -f2)
if [[ "${live_n:-1}" == "0" ]]; then
  pass "4b. no hardcoded runtime \"Nick\" string in any live (non-archived) shipped hook"
else
  fail "4b. no hardcoded runtime \"Nick\" string in any live (non-archived) shipped hook" \
    "$live_n candidate runtime hit(s) above — the 2026-08-29 sweep did NOT catch all of them (see report)"
fi

# ── Assertion 5: no credential/token/API key ships, distinct from bin/tests/ fixtures ────────
python3 - "$CLONE" "$REPO" >"$WORK/cred.out" <<'PY'
import json, pathlib, re, subprocess, sys
clone, repo = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
man = json.loads((repo / "bin/sync-manifest.json").read_text())
keep = man["per_core_keep"]
def excluded(rel):
    import fnmatch
    for p in keep:
        if p.endswith("/**"):
            if rel == p[:-3] or rel.startswith(p[:-2]):
                return True
        elif fnmatch.fnmatch(rel, p) or rel == p:
            return True
    return False
files = []
for d in man["shared"]["dirs"]:
    base = clone / d
    if base.is_dir():
        files += [p for p in base.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
for f in man["shared"]["files"]:
    p = clone / f
    if p.is_file():
        files.append(p)
files = sorted({p for p in files if not excluded(str(p.relative_to(clone)))})

PATTERNS = re.compile(
    r"ghp_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|AKIA[A-Z0-9]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
real_secret = None
secrets_env = pathlib.Path.home() / ".claude" / "secrets.env"
if secrets_env.is_file():
    m = re.search(r'=["\']?([A-Za-z0-9._/+=-]{20,})["\']?', secrets_env.read_text(errors="ignore"))
    real_secret = m.group(1) if m else None

hits, fixtures = [], []
for p in files:
    try:
        txt = p.read_text(errors="ignore")
    except OSError:
        continue
    rel = str(p.relative_to(clone))
    for i, line in enumerate(txt.splitlines(), 1):
        if PATTERNS.search(line):
            is_fixture = "/tests/" in rel or rel.startswith("bin/tests/") or "test_" in p.name
            (fixtures if is_fixture else hits).append(f"{rel}:{i}: {line.strip()[:140]}")
        if real_secret and real_secret in line:
            hits.append(f"{rel}:{i}: [ACTUAL SECRET FROM ~/.claude/secrets.env FOUND]")

print(f"scanned {len(files)} shipped files for credential-shaped strings")
print(f"real-secret cross-check against ~/.claude/secrets.env: {'ran' if real_secret else 'no secrets.env / no key found, skipped'}")
for h in fixtures:
    print(f"[FIXTURE] {h}")
for h in hits:
    print(h)
print(f"__REAL__={len(hits)}")
print(f"__FIXTURES__={len(fixtures)}")
PY
sed 's/^/      /' "$WORK/cred.out"
real_n=$(grep -oE '__REAL__=[0-9]+' "$WORK/cred.out" | cut -d= -f2)
if [[ "${real_n:-1}" == "0" ]]; then
  pass "5. no real credential/token/API key ships (test fixtures in bin/tests|**/tests excluded and reported separately)"
else
  fail "5. no real credential/token/API key ships" "$real_n hit(s) above"
fi

# ── Assertion 6: memory/, sessions/, tasks/ carry no personal content in the shipped clone ───
# Heuristic: a bare scaffold file either declares itself with `Status: template` frontmatter,
# or is short (this repo's own empty scaffolds — access-log.md, pending.md, backlog.md,
# lessons.md/-archive.md, system-rundown.md — all measure under 700 bytes; real appended
# content in this baseline runs 1000-3500 bytes). Byte-size is crude but does not depend on
# guessing which English words a "generic" file will or won't contain — a marker-word
# allowlist silently swallowed real appended entries in decisions-log.md and current-state.md
# on the first pass of this script (both sit under a `## YYYY-MM-DD` format example, which
# matched a marker meant to catch templates). Flagged files still need a human read — this
# only narrows where to look.
python3 - "$CLONE" >"$WORK/percore.out" <<'PY'
import pathlib, re, sys
clone = pathlib.Path(sys.argv[1])
SIZE_THRESHOLD = 800
suspects = []
for d in ("memory", "sessions", "tasks"):
    base = clone / d
    if not base.is_dir():
        continue
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name == ".gitkeep":
            continue
        try:
            txt = p.read_text(errors="ignore")
        except OSError:
            continue
        if not txt.strip():
            continue
        is_template = bool(re.search(r'^Status:\s*template\s*$', txt, re.I | re.M))
        if is_template:
            continue
        if len(txt.encode("utf-8", "ignore")) > SIZE_THRESHOLD:
            suspects.append((str(p.relative_to(clone)), len(txt.encode("utf-8", "ignore"))))
for s, n in suspects:
    print(f"{s} ({n} bytes, no Status:template marker)")
print(f"__SUSPECTS__={len(suspects)}")
PY
sed 's/^/      /' "$WORK/percore.out"
susp_n=$(grep -oE '__SUSPECTS__=[0-9]+' "$WORK/percore.out" | cut -d= -f2)
if [[ "${susp_n:-1}" == "0" ]]; then
  pass "6. memory/, sessions/, tasks/ contain no non-template content in the shipped clone"
else
  fail "6. memory/, sessions/, tasks/ contain no non-template content in the shipped clone" \
    "$susp_n file(s) above look like real content, not the empty per_core_keep template — READ THEM, do not trust this heuristic alone"
fi

# ── Assertion 7: full git history of the baseline, searched for strip-check's OWN patterns,
# restricted to paths that are CURRENTLY shipped. Report only — no rewrite. Patterns are read
# from the shipped strip-check.py at RUNTIME (never typed as literals into this .sh file —
# this file ships too, under bin/, and a literal copy of the patterns it searches for would be
# exactly the leak assertion 2 exists to catch).
python3 - "$CLONE" >"$WORK/hist.out" <<'PY'
import importlib.util, os, subprocess, sys
clone = sys.argv[1]
os.environ["CORE_INSTANCE"] = clone
spec = importlib.util.spec_from_file_location("strip_check", f"{clone}/bin/strip-check.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)
shipped = [str(p.relative_to(clone)) for p in sc.shipped_files()]
n_commits = subprocess.run(["git", "-C", clone, "log", "--oneline"],
                            capture_output=True, text=True).stdout.count("\n")
total_hits = 0
for pat in sc.PATTERNS:
    out = subprocess.run(
        ["git", "-C", clone, "log", "--all", "--oneline", "-G", pat, "--"] + shipped,
        capture_output=True, text=True,
    ).stdout.strip()
    if out:
        shas = out.splitlines()
        total_hits += len(shas)
        print(f"pattern {pat!r} introduced/removed in commit(s) touching a currently-shipped path:")
        for line in shas:
            print(f"  {line}")
print(f"(history scope: {len(shipped)} currently-shipped paths, {n_commits} total commits searched)")
print(f"__HIST__={total_hits}")
PY
sed 's/^/      /' "$WORK/hist.out"
hist_hits=$(grep -oE '__HIST__=[0-9]+' "$WORK/hist.out" | cut -d= -f2)
if [[ "${hist_hits:-1}" == "0" ]]; then
  pass "7. no currently-shipped file's history ever carried the author's email/homedir (scoped pickaxe search)"
else
  fail "7. no currently-shipped file's history ever carried the author's email/homedir" \
    "$hist_hits commit(s) above — history is public; report only, no rewrite performed"
fi

echo
echo "$pass_n passed, $fail_n failed"
[[ "$fail_n" == "0" ]]
