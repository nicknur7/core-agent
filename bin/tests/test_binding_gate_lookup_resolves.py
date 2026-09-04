#!/usr/bin/env python3
"""A FALLBACK BRANCH THAT WAS REACHABLE BY ACCIDENT, AND THEREFORE ALWAYS TAKEN.

`scheduling/core-si/detect.sh` decides the remedy text for the 🔴 not-binding SI item by looking up
which binding gates are still live in `bin/hook-registry.json`. Two branches:

    live gate found  ->  "wire the binding gate (<names>) for this class"
    none found       ->  "NO GATE EXISTS FOR THIS CLASS — every blocking gate that could bind it is
                          retired. This is a finding about the system, not a task: a new PreToolUse
                          mechanism has to be designed before this item can be actioned."

The lookup read `"$REPO/bin/hook-registry.json"`. **`REPO` is never defined in that file** — it
defines CORE_INSTANCE, STATE_DIR, BRAIN_REPO and BASELINE_REPO, and nothing else. So:

    set -uo pipefail (line 19)      ->  aborts the function on the unset variable
    _binding_gates 2>/dev/null      ->  hides the abort
    || true                         ->  turns the abort into an empty string
    empty string                    ->  selects the second branch

Three independent safety mechanisms each behaved correctly and their COMPOSITION produced a silent
unconditional fallback. No registry was ever opened, on any Core, since the branch shipped.

The cost was an instruction, not a string. `recall-first-gate` is live at PreToolUse in that very
registry and is not retired, so the honest remedy was always "wire the existing gate" — actionable.
Instead the item told every reader the class was un-actionable by construction and that new
machinery had to be designed first, which is exactly why it sat.

This test pins BOTH halves: the lookup must resolve a real path, and the no-gate wording must be
reachable only when the registry genuinely has no live candidate.

Run: python3 bin/tests/test_binding_gate_lookup_resolves.py
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
DETECT = ROOT / "scheduling" / "core-si" / "detect.sh"
REGISTRY = ROOT / "bin" / "hook-registry.json"

_passed = 0
_failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


src = DETECT.read_text()

# SCOPED TO THE GATE-LOOKUP, NOT THE WHOLE FILE, AND COMMENT-STRIPPED — both narrowings were forced
# by the test's own first run, which is the point of running it.
#
#   · It flagged `$REPO` as still present. It was: inside the comment this fix added, explaining
#     that `$REPO` was the bug. A scanner that reads prose about code as code will fail forever on
#     a correct file, and the "fix" would have been to stop documenting the bug.
#   · Its repo-wide sweep also flagged CORE_IDENTITY_JSON, CORE_MEM_ABOUT_ME, GRN, YEL and others.
#     Those are plausibly deliberate env knobs or defined in a sourced lib — I did not verify which,
#     so asserting they are defects would be exactly the unverified absence-claim the memory rule
#     warns about. A test may only assert what it checked.
#
# What remains is the claim this test was written for and can actually prove.
def _strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line.split(" #", 1)[0] if " #" in line else line)
    return "\n".join(out)


code = _strip_comments(src)
m = re.search(r"_binding_gates\(\)\s*\{(.*?)\n\}", code, re.S)

print("=== the gate-lookup must not reference an undefined variable ===")
check("the _binding_gates function is present and parseable", bool(m))
if m:
    body = m.group(1)
    used = set(re.findall(r'\$\{?([A-Z_][A-Z0-9_]*)\}?', body))
    assigned = set(re.findall(r'^\s*([A-Z_][A-Z0-9_]*)=', code, re.M))
    # CORE_INSTANCE is set at detect.sh line 21 with a git-rev-parse fallback.
    supplied = {"CORE_INSTANCE"}
    undefined = sorted(used - assigned - supplied)
    check("every variable the lookup dereferences is defined or supplied",
          not undefined, f"undefined in _binding_gates: {undefined}")
    check("the specific regression is gone: the lookup does not use $REPO",
          not re.search(r'\$\{?REPO\b', body), "the lookup still dereferences $REPO")

print()
print("=== the gate-finder must actually open the registry ===")
check("the lookup path is anchored on a defined variable",
      "$CORE_INSTANCE/bin/hook-registry.json" in src)
check("registry exists and parses", REGISTRY.is_file() and isinstance(
    json.loads(REGISTRY.read_text()), dict))

# Reproduce the finder exactly as detect.sh embeds it.
FINDER = r'''
import json, sys
try:
    reg = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
wanted = set(sys.argv[2:])
hooks = reg.get("hooks", reg) if isinstance(reg, dict) else reg
live = []
for h in (hooks if isinstance(hooks, list) else hooks.values()):
    if not isinstance(h, dict) or h.get("name") not in wanted:
        continue
    if h.get("retired"):
        continue
    live.append(h["name"])
print(" / ".join(sorted(set(live))))
'''
CANDIDATES = ["approval-gate", "recall-gate", "recall-first-gate"]


def run_finder(registry_path):
    r = subprocess.run([sys.executable, "-c", FINDER, str(registry_path), *CANDIDATES],
                       capture_output=True, text=True)
    return (r.stdout or "").strip()


live = run_finder(REGISTRY)
print()
print("=== on THIS Core's real registry ===")
check("the finder resolves at least one live binding gate",
      bool(live), "empty -> the no-gate branch would fire")
print(f"        resolved: {live!r}")

print()
print("=== the no-gate wording must be EARNED, not reachable by accident ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump({"hooks": [{"name": n, "event": "PreToolUse", "retired": True}
                         for n in CANDIDATES]}, fh)
    all_retired = fh.name
check("a registry where every candidate IS retired yields empty (branch is reachable when true)",
      run_finder(all_retired) == "")
check("a nonexistent registry ALSO yields empty — which is why the path must be right",
      run_finder("/nope/does-not-exist.json") == "")
Path(all_retired).unlink(missing_ok=True)

print()
print(f"=== Results: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
