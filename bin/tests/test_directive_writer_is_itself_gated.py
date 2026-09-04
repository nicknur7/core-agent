#!/usr/bin/env python3
"""The CLAUDE.md writer must refuse over-budget ITSELF, not rely on its callers to check.

WHY THIS EXISTS (2026-08-20, found by core-business, confirmed independently by school and ops).

Two functions reach `auto_apply_directive`, the only thing that writes CLAUDE.md:

    promote_proven_contract -> auto_apply_directive     budget-gated
    **generate()            -> auto_apply_directive     NO budget check of any kind**

`generate()` is the SI loop's own dispatch — the live path. So the morning's fix corrected the
arithmetic inside a gate guarding the door nobody used, and life published to four peers that
"claude_md_directive promotion is blocked on all five seats." **That was false for the path the
loop actually takes**, and it was published after the fix was reviewed, pushed to the baseline,
and announced.

A gate on one of two callers is not a gate. It is a gate plus an unguarded door, and the
unguarded one was the main entrance.

WHAT THIS ASSERTS — enforcement at the WRITE POINT, so a future caller inherits it without
knowing it exists. Caller-side checks are an optimisation; this is the guarantee.

  1. Over budget -> the writer refuses AND CLAUDE.md is byte-unchanged. Refusing while writing
     anyway is the failure this whole class of bug is made of.
  2. Under budget -> it still writes. A gate that blocks everything is not a gate either, and
     would have hidden itself behind "no directives were due."
  3. No recorded baseline -> refuses. Unknown ceiling means stop; that is the case a fresh seat
     or a fork is in, which is exactly where an unmetered directive-writer is least noticed.
"""
import sys
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
import steering_load as sl          # noqa: E402
import artifact_generator as ag     # noqa: E402

# THIS SEAT'S ORG AS THE ARGUMENT. A literal here is the same defect as a literal in a spec:
# on any seat whose org is not 1 the call operates on — and in promote/generate paths WRITES
# to — life's partition. Found by sweeping the ARGUMENT side after core-school showed the
# spec-side sweep had missed it.
import sys as _sys2
from pathlib import Path as _PP2
_sys2.path.insert(0, str(_PP2(__file__).resolve().parents[2] / "scheduling" / "brain-pg"))
from _env import get_org_id as _goi2  # noqa: E402
ORG = _goi2(_PP2(__file__).resolve().parents[2])

failures, checks = [], 0


def check(label, ok, detail=""):
    global checks
    checks += 1
    print(("  ok     " if ok else "  FAIL   ") + label + (("\n           " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(label)


CASE = {"user_wanted": "keep the explanation plain and short for Nick", "support": {"count": 9}}


def seat(tmp: Path, lessons_bytes: int, ceiling=None) -> Path:
    for rel in sl.ALWAYS_LOADED:
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * (lessons_bytes if rel == "tasks/lessons.md" else 400))
    (tmp / "CLAUDE.md").write_text("# Core\n\nsome steering prose\n")
    if ceiling is not None:
        b = sl.baseline_path(tmp)
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text(json.dumps({"ceiling": ceiling}))
    return tmp


def run_against(tmp: Path):
    """Point the generator at a synthetic seat and invoke the writer."""
    real_repo, real_md = ag.REPO, ag.CLAUDE_MD
    try:
        ag.REPO, ag.CLAUDE_MD = tmp, tmp / "CLAUDE.md"
        before = (tmp / "CLAUDE.md").read_text()
        res = ag.auto_apply_directive(ORG, CASE)
        after = (tmp / "CLAUDE.md").read_text()
        return res, before, after
    finally:
        ag.REPO, ag.CLAUDE_MD = real_repo, real_md


# --- 1. over budget: refuse, and do not write -------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    res, before, after = run_against(seat(Path(d), 60_000, ceiling=4_000))
    check("over budget, the writer itself refuses",
          res.get("action") == "directive_skipped" and "steering budget" in res.get("reason", ""),
          f"got {res}")
    check("over budget, CLAUDE.md is byte-unchanged",
          before == after,
          "refusing in the return value while writing anyway is the whole bug class")

# --- 2. under budget: it still writes ---------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    res, before, after = run_against(seat(Path(d), 400, ceiling=100_000))
    check("under budget, the writer still applies the directive",
          res.get("action") == "directive_applied" and after != before,
          f"got {res} — a gate that blocks everything hides behind 'nothing was due'")

# --- 3. no baseline: unknown means stop -------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    res, before, after = run_against(seat(Path(d), 400, ceiling=None))
    check("with no recorded ceiling, the writer refuses",
          res.get("action") == "directive_skipped" and before == after,
          f"got {res} — a fresh seat or fork is where an unmetered writer is least noticed")

print()
if failures:
    print("  FAIL=%d of %d" % (len(failures), checks))
    for f in failures:
        print("    - " + f)
    sys.exit(1)
print("  ok=%d  FAIL=0 — the write point enforces, not the callers" % checks)
