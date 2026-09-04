#!/usr/bin/env python3
"""_CONFOUND_CACHE must separate orgs, because its cached value is org-dependent.

WHY THIS EXISTS (2026-08-12). _confounded() answers "are these two windows activity-skewed?" by
counting pattern_observations WHERE org_id = current_setting('app.current_org_id'). The answer
therefore depends on obs_min/split/obs_max AND on the org. The key held only the three dates.

core-finance named it while confirming T027, and the reasoning is worth keeping because the defect
is invisible today: one process holds one org for its whole lifetime, so the key was CORRECT — right
up until anything sets the GUC twice or a caller loops orgs. T017 ("each Core mines and optimizes
from its OWN corpus") grows directly toward that. This is GEN's shape exactly: an assumption that
holds until it quietly stops, in an instrument confident enough that nobody re-checks it.

The failure is not a crash. With one shared key, the SECOND org silently receives the FIRST org's
verdict — so a Core whose windows are perfectly balanced gets INSUFFICIENT-CONFOUNDED and every
contract it measures is suppressed, for a reason belonging to a different Core's corpus. A
suppressed verdict looks like caution, not like a bug, which is why it would survive review.

HOW THE KEY IS READ, and this part is not incidental. It uses get_org_id(), NOT a
`SELECT current_setting(...)` on the live cursor. connect_corebrain() SETs the GUC from exactly
that value (_env.py:244), so the two agree — and a cursor read here would consume a slot in
core-finance's order-dependent fake cursor and break the very probe that caught the T027 ordering
bug. Fixing one seat's organ must not break another seat's instrument.

The dose is in the file: it rebuilds the module with the org removed from the key and asserts the
bug COMES BACK. A test that only checks the fixed path cannot tell a working fix from a vacuous one.
"""
import datetime
import importlib.util
import sys
import types
import unittest.mock as mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scheduling" / "claude-si" / "measure-contract-fitness.py"
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


class FakeCur:
    """Order-dependent, matching core-finance's probe: recurrence queries name canonical_ask,
    activity queries do not. Two activity reads -> pre, then post."""

    def __init__(self, act_pre: int, act_post: int) -> None:
        self.ap, self.ap2 = act_pre, act_post
        self.a = 0
        self._q = ""

    def execute(self, q, *a):
        self._q = q

    def fetchall(self):
        return []

    def fetchone(self):
        if "canonical_ask" in self._q:
            return (0,)
        self.a += 1
        return (self.ap,) if self.a == 1 else (self.ap2,)


WINDOW = (datetime.date(2026, 5, 1), datetime.date(2026, 7, 1), datetime.date(2026, 8, 1))


def _load(patch_key_out: bool = False):
    src = SRC.read_text()
    if patch_key_out:
        before = src
        src = src.replace("key = (get_org_id(), obs_min, split, obs_max)",
                          "key = (obs_min, split, obs_max)")
        if src == before:
            return None  # the line moved; the dose would silently test nothing
    mod = types.ModuleType("mcf_dose")
    mod.__file__ = str(SRC)
    exec(compile(src, str(SRC), "exec"), mod.__dict__)
    return mod


def _two_orgs(mod):
    """Org 1 sees a 30x activity gap (confounded); org 4 sees a balanced window (not confounded).
    SAME three dates, so the old key collides on them."""
    mod._CONFOUND_CACHE.clear()
    with mock.patch.object(mod, "get_org_id", lambda: 1):
        a = mod._confounded(FakeCur(3000, 10), *WINDOW)
    with mock.patch.object(mod, "get_org_id", lambda: 4):
        b = mod._confounded(FakeCur(100, 100), *WINDOW)
    return a, b, len(mod._CONFOUND_CACHE)


def main() -> int:
    print("test_confound_cache_is_org_keyed")
    spec = importlib.util.spec_from_file_location("mcf", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    a, b, n = _two_orgs(mod)
    check("a 30x gap in org 1 is CONFOUNDED (the precondition — else this proves nothing)",
          a is True, f"org 1 returned {a!r}; the fixture no longer produces a skewed window")
    check("org 4's BALANCED window is judged on its own corpus, not org 1's",
          b is False,
          f"org 4 returned {b!r}. It inherited org 1's verdict, so every contract org 4 measures "
          f"is suppressed as INSUFFICIENT-CONFOUNDED for a reason belonging to another Core.")
    check("two orgs over one window occupy two cache entries",
          n == 2, f"{n} entries — the orgs collided on a shared key")

    dose = _load(patch_key_out=True)
    if dose is None:
        check("DOSE: the org-keyed line still exists to be removed", False,
              "could not find `key = (get_org_id(), obs_min, split, obs_max)` — if that line was "
              "renamed the dose silently tests nothing, which is worse than failing")
    else:
        _a, _b, _n = _two_orgs(dose)
        check("DOSE: removing the org from the key REPRODUCES the bug",
              _b is True and _n == 1,
              f"with the org removed, org 4 returned {_b!r} across {_n} cache entries. It should "
              f"have wrongly inherited True in exactly 1 entry. If it did not, this test passes "
              f"whether or not the fix is present, and proves nothing.")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
