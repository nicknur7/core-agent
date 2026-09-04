#!/usr/bin/env python3
"""AUTONOMY THAT IS SEEDED BY HUMAN APPROVAL CANNOT OUTRUN THE HUMAN.

Until 2026-08-26, `si-fix-admission.py` said so in its own docstring: *"A deterministic core-si fix
earns the right to auto-apply by being APPROVED K times running."* Trust had exactly one source —
Nick clicking approve — enforced by a CHECK constraint allowing only kind IN ('approve','reject').

That is not a threshold, it is a dependency. The loop could only ever act on fix classes Nick had
already performed by hand, repeatedly, which is the labour he has been asking to be rid of since
2026-07-16 (67 separate messages; the earliest already said "none of them are working"). It also
contradicted the standing rule in tasks/lessons.md:132, written 2026-07-23 and unchanged since:

    "Autonomous self-improvement = test-gate + reversibility, NOT a human approval gate"

The rule said test-gate for five weeks while the code said approval-gate.

WHAT THIS PINS. A fix can now reach TRUSTED on evidence alone — a shadow run of its registered
applier that verifies preconditions and mutates nothing — with zero human approvals. And the
safety properties that must survive that change:

  · a FAILED shadow run resets the streak; it must never read as progress toward autonomy
  · Nick's explicit approvals still count, and his reject still overrides accumulated evidence
  · the two roads mix

TWO CAVEATS, STATED SO THEY ARE NOT MISTAKEN FOR COVERAGE. (1) This proves the TRUST gate. The
apply path is `in_safe AND trusted AND has_applier`, and the other two terms are unchanged — on
2026-08-26 auto-safe.txt held 1 key and APPLIERS held 1 entry, against 17 detectors and 57
registered hooks. Fixing trust does not make the loop autonomous about anything that has no
actuator. (2) This test WRITES to the shared corebrain (a reserved __test__ key, org-scoped by RLS)
and deletes its rows on every path. It is in bin/tests/, which runs at close on every seat.

Run: python3 bin/tests/test_trust_is_evidence_seeded.py
"""
import importlib.util, sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

# core_root(), NOT a hardcoded path. bin/tests/ ships to every Core and runs at close on each of
# them, so a literal "/Users/<you>/AI Projects/<core>" here would make sibling Cores,
# finance and ops load LIFE's admission module and write their evidence rows against life's tree.
# That is precisely the defect _core.py was written about ("A TEST THAT WRITES INTO ANOTHER CORE'S
# TREE IS NOT A TEST, IT IS A CROSS-CORE WRITE CHANNEL that runs on a schedule") — and I wrote it
# again here, in the same session, until test_no_cross_core_paths.py refused the file.
REPO = core_root()
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
spec = importlib.util.spec_from_file_location("adm", REPO / "scheduling" / "core-si" / "si-fix-admission.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

KEY, FIX = "__test-evidence-path__", "__test fix action__"
conn = m.connect_corebrain()
cur = conn.cursor()


def clean():
    cur.execute("DELETE FROM core_si_fix_approvals WHERE signal_key=%s", (KEY,))
    cur.execute("DELETE FROM core_si_trusted_fixes WHERE signal_key=%s", (KEY,))
    conn.commit()


clean()
ok = True


def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not cond else ""))


print("=== a fix reaches TRUSTED on evidence alone, with zero human approvals ===")
check("starts untrusted", not m.is_trusted(conn, KEY, FIX))
r1 = m.record_evidence(conn, KEY, FIX, True)
check("evidence #1 -> streak 1, not yet admitted", r1["streak"] == 1 and not r1["admitted"], str(r1))
r2 = m.record_evidence(conn, KEY, FIX, True)
check("evidence #2 -> streak 2, ADMITTED (K=2)", r2["streak"] == 2 and r2["admitted"], str(r2))
check("now trusted — no human ever approved it", m.is_trusted(conn, KEY, FIX))

print()
print("=== a FAILED shadow run resets the streak — it must never count as progress ===")
clean()
m.record_evidence(conn, KEY, FIX, True)
r3 = m.record_evidence(conn, KEY, FIX, False)
check("evidence_fail -> streak back to 0", r3["streak"] == 0, str(r3))
check("still untrusted after a failure", not m.is_trusted(conn, KEY, FIX))
r4 = m.record_evidence(conn, KEY, FIX, True)
check("one success after a failure is only streak 1, not 2", r4["streak"] == 1, str(r4))

print()
print("=== Nick's approvals still work and still count the same ===")
clean()
m.record(conn, KEY, FIX, kind="approve")
r5 = m.record(conn, KEY, FIX, kind="approve")
check("two human approvals still admit", r5["admitted"], str(r5))

print()
print("=== the two roads mix: one approval + one evidence also admits ===")
clean()
m.record(conn, KEY, FIX, kind="approve")
r6 = m.record_evidence(conn, KEY, FIX, True)
check("approve + evidence -> admitted", r6["admitted"], str(r6))

print()
print("=== a human REJECT still resets, even against evidence ===")
clean()
m.record_evidence(conn, KEY, FIX, True)
m.record(conn, KEY, FIX, kind="reject")
r7 = m.record_evidence(conn, KEY, FIX, True)
check("reject wipes prior evidence; streak is 1 not 2", r7["streak"] == 1, str(r7))

clean()
conn.close()
print()
print("ALL PASS" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
