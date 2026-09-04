#!/usr/bin/env python3
"""WHEN A GATE EXISTS, THE WINDOW SPLITS AT THE GATE'S SINCE-DATE — NOT THE CONTRACT'S CREATION.

AUTHORED AND RUN ON core-finance. NOT INSTALLED HERE — finance is a puller and bin/tests/ is
baseline-shared, so per the routing life decided 2026-08-12 the source is returned on the bus and
life commits it. Intended install path: bin/tests/test_split_uses_gate_since_date.py

Fences the LAST unfenced postmortem in measure-contract-fitness.py. Its words:

    "Fix 1 (2026-06-18) — when a gate exists, split the pre/post window at the GATE's since-date,
     not the contract's creation. Else pre-gate misses get counted as gate failures (the
     model-routing NOT-BINDING measurement artifact)."

WHAT IT COSTS WHEN IT REGRESSES. A contract created on day 1 and gated on day 30 has 29 days of
misses that predate any enforcement. Split at CREATION and every one of those lands in the "post"
window, so the gate is charged for failures that happened before it existed — the measurement reads
NOT-BINDING for a gate that may be working perfectly. The comment names the real casualty:
model-routing's NOT-BINDING was this artifact.

The failure is silent and flattering in the wrong direction — it does not error, it produces a
confident verdict that the enforcement layer is not binding, which invites retuning a gate that was
never the problem.

WHY THIS IS NOT A GREP. core-life wrote source-text checks against this same file twice on
2026-08-12 and both passed against a neutered predicate, because the string survived in a comment.
This drives main() with a fake cursor and asserts on the SPLIT VALUE actually bound into the query
parameters at runtime. A comment cannot reach a cursor, and a date cannot be faked into a bound
parameter by being mentioned nearby.

STRUCTURE, per the conventions this suite converged on tonight:
  1. CONTROL — the recurrence query was actually issued with bound parameters. My probe-7 harness
     took four iterations and a must-pass control caught three of them, twice reporting "no queries
     issued at all" as though it were a finding about the code.
  2. GATED — with a gate present, the bound split equals the GATE's since-date.
  3. UNGATED — with no gate, the bound split falls back to the contract's creation date. Both
     directions matter: asserting only the gated case would pass against an implementation that
     ignored `created` entirely.
  4. MUTATION, in-band — force `split = created` unconditionally and confirm assertion 2 flips.

Fake cursors only. `main(dry_run=True)` plus OUT_FILE redirected to temp, belt-and-braces.
Read-only, no DB, no live state. TemporaryDirectory, never mkdtemp.

Run: python3 tasks/si-verification/probes/test_split_uses_gate_since_date.py
"""
import importlib.util
import sys
import tempfile
from datetime import date
from pathlib import Path


def _root() -> Path:
    p = Path(__file__).resolve()
    for cand in p.parents:
        if (cand / "scheduling" / "claude-si").is_dir() and (cand / "bin").is_dir():
            return cand
    raise SystemExit("SKIP - could not locate Core root")


ROOT = _root()
MCF = ROOT / "scheduling" / "claude-si" / "measure-contract-fitness.py"

CREATED = date(2026, 6, 1)      # contract created
GATE_SINCE = date(2026, 7, 15)  # gate installed 44 days later
OBS_MIN, OBS_MAX = date(2026, 5, 1), date(2026, 8, 1)


class ParamCursor:
    """Records (sql, params) for every execute, and answers with the SHAPE each caller unpacks."""

    def __init__(self):
        self.calls = []

    def _last(self):
        return self.calls[-1][0] if self.calls else ""

    def execute(self, q, params=None, *a, **k):
        self.calls.append((" ".join(str(q).split()), params))

    def fetchall(self):
        if "learned_contracts" in self._last():
            # NO HYPHEN and NO em-dash on purpose. main() derives the gate lookup key as
            # `situation.split("—")[0].split("-")[0].strip()`, so "probe-contract" becomes
            # "probe" and a gate dict keyed on the full name never matches. v1 of this probe
            # did exactly that and read as a live regression.
            #
            # FIVE-TUPLE, not three (2026-08-31, same defect as
            # test_recurrence_anchors_on_session_date.py). main()'s learned_contracts SELECT is
            # `situation, trigger_labels, created_at::date, required_shape, checkable` and
            # `for situation, labels, created, _req_shape, _checkable in contracts:` raises
            # ValueError on a 3-tuple — swallowed by run()'s blanket `except Exception: pass`, so
            # every assertion below was checking dates that never got bound because main() never
            # reached the contracts loop at all. Empty lists are a valid required_shape/checkable.
            return [("probecontract", ["some-label"], CREATED, [], [])]
        return []

    def fetchone(self):
        # The corpus-bounds query is unpacked as a PAIR (`obs_min, obs_max = cur.fetchone()`).
        # Returning a 1-tuple for everything kills main() with "not enough values to unpack"
        # BEFORE the contracts loop — which is exactly how probe 7's third iteration failed.
        if "min(" in self._last() and "max(" in self._last():
            return (OBS_MIN, OBS_MAX)
        return (0,)

    def close(self):
        pass


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


def load(path=None):
    sys.path.insert(0, str(ROOT / "scheduling" / "claude-si"))
    sys.path.insert(0, str(ROOT / "scheduling" / "brain-pg"))
    spec = importlib.util.spec_from_file_location("mcf_gatesplit_probe", path or MCF)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def run(module, gate: bool):
    """Drive main() and return the date values bound into recurrence queries."""
    cur = ParamCursor()
    module.connect_corebrain = lambda *a, **k: FakeConn(cur)
    module._gates = (lambda *a, **k: {"probecontract": {"since": GATE_SINCE.isoformat()}}) if gate \
        else (lambda *a, **k: {})
    module._unenforceable = lambda *a, **k: set()
    with tempfile.TemporaryDirectory() as td:
        module.OUT_FILE = Path(td) / "contract-fitness.json"
        try:
            module.main(dry_run=True)
        except Exception:
            pass
    bound = []
    for sql, params in cur.calls:
        if "pattern_observations" in sql and params:
            for v in (params if isinstance(params, (list, tuple)) else [params]):
                if isinstance(v, date):
                    bound.append(v)
    return bound


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== a gated contract splits at the GATE's since-date, not its creation ===\n")
    if not MCF.is_file():
        print("  SKIP - measure-contract-fitness.py absent")
        return 0

    m = load()
    gated = run(m, gate=True)
    ungated = run(m, gate=False)

    check("CONTROL - recurrence queries were issued with bound date parameters",
          bool(gated),
          "no dates reached the cursor; every assertion below is vacuous")

    check("GATED - the bound split is the gate's since-date (%s)" % GATE_SINCE,
          GATE_SINCE in gated,
          "bound dates were %r, expected to contain %s. Splitting at creation charges the gate for "
          "44 days of misses that predate it — the model-routing NOT-BINDING artifact."
          % (gated, GATE_SINCE))

    check("GATED - the contract's creation date (%s) is NOT used as the split" % CREATED,
          CREATED not in gated,
          "bound dates were %r — creation leaked into the window despite a gate being present"
          % (gated,))

    check("UNGATED - with no gate, the split falls back to creation (%s)" % CREATED,
          CREATED in ungated,
          "bound dates were %r. Without this the gated assertion would also pass against an "
          "implementation that ignored `created` entirely." % (ungated,))

    # ---- MUTATION ---------------------------------------------------------------------------
    print("\n--- mutation control: the gated assertion must be able to FAIL ---")
    src = MCF.read_text()
    old = "                split = _date.fromisoformat(gate[\"since\"])"
    new = "                split = created"
    if old not in src:
        check("mutation site located", False,
              "could not find the gate since-date assignment; the greens above are unverified")
    else:
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "measure-contract-fitness.py"
            mp.write_text(src.replace(old, new, 1))
            mutated = run(load(mp), gate=True)
            # COMPARE AGAINST BASELINE. Asserting only "the mutant binds created" passes vacuously
            # when the baseline ALSO binds created — which is precisely what happened in v1 of this
            # probe, where a harness bug made baseline and mutant identical and the mutation control
            # reported PASS while proving nothing. A mutation control must show a DIFFERENCE.
            check("forcing split=created CHANGES the bound split (mutation is discriminating)",
                  GATE_SINCE in gated and GATE_SINCE not in mutated,
                  "baseline bound %r, mutant bound %r — if these are identical the mutation control "
                  "is vacuous and the gated assertions above prove nothing" % (gated, mutated))

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
