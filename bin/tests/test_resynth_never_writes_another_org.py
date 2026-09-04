#!/usr/bin/env python3
"""Every statement in learned-resynth that touches learned_contracts is org-scoped — reads AND writes.

WHY THIS EXISTS (2026-08-18, found by core-business).

`learned-resynth.py` rewrites each contract's `required_shape` / `forbidden_moves` from that Core's
own corpus. It had **zero** occurrences of `org_id` in 148 lines. The read half was fixed in 543aab7
after the cross-org READ exposure; business then found the write half, which is worse:

    learned_contracts     42 rows, 11 distinct keys
    rows per org          {1: 6, 2: 11, 3: 9, 4: 6, 5: 10}
    keys shared           10 of 11

`apply()` built `{_key(situation): cid}` from an UNSCOPED, UNORDERED select. A dict comprehension
keeps the LAST value per key, so every shared key collapsed to exactly one cid and the UPDATE landed
there. **Whichever seat ran resynth, the rewritten body went into one org's row** — three seats
frozen at `si_induct`'s hardcoded placeholder body while one seat received everyone's rewrites and
ran them as its own behaviour contract.

The victim's identity was incidental, which makes it worse rather than better: with no `ORDER BY`,
which row wins is not stable across runs or after a VACUUM, so the target can move.

WHAT THIS ASSERTS.
  1. Every `learned_contracts` statement in the module carries an org predicate — including the
     UPDATE, whose safety would otherwise live in a SELECT twenty lines away. An UPDATE that is safe
     only by construction is one edit from unsafe, and that coupling is exactly what failed here.
  2. No bare `FROM learned_contracts` / `UPDATE learned_contracts` survives without a predicate.

Source-level, deliberately: the defect was a missing predicate in SQL text, and a behavioural test
would need a live multi-org database to reproduce a condition that must never be reproducible.
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "scheduling" / "claude-si" / "learned-resynth.py"
src = SRC.read_text()
checks = 0

assert "learned_contracts" in src, "module no longer references learned_contracts — retarget this test"
checks += 1

# Extract SQL via the AST, not a regex over source text. A regex has to guess where a string literal
# starts and ends, and the first version of this test did so wrongly the moment the UPDATE became an
# implicit two-part concatenation: quote parity drifted, one "statement" began mid-comment, and the
# UPDATE assertion reported "no UPDATE found" for code that plainly contains one. A test whose parser
# can silently mis-frame its input is the same defect class it exists to catch. `ast` folds implicit
# concatenation for free and cannot mis-frame.
tree = ast.parse(src)
stmts = [n.value for n in ast.walk(tree)
         if isinstance(n, ast.Constant) and isinstance(n.value, str) and "learned_contracts" in n.value]
assert stmts, "no learned_contracts SQL literal found via AST — retarget this test"
checks += 1

def is_sql(s: str) -> bool:
    head = s.lstrip()[:8].upper()
    return head.startswith(("SELECT", "UPDATE", "INSERT", "DELETE"))

sql = [s for s in stmts if is_sql(s)]
assert sql, "AST found learned_contracts strings but none are SQL — retarget this test"
checks += 1

PREDICATE = "org_id"
unscoped = [s for s in sql if PREDICATE not in s]
assert not unscoped, (
    "learned_contracts statement(s) with no org predicate:\n  " + "\n  ".join(unscoped) +
    "\n-> an unscoped read hands every org's contracts to the rewriter, and the UPDATE that follows "
    "then aims at a foreign id where RLS matches zero rows: the resynthesised body is silently "
    "discarded and the seat still reports success.")
checks += 1

updates = [s for s in sql if s.lstrip().upper().startswith("UPDATE")]
assert updates, "no UPDATE of learned_contracts found — retarget this test"
checks += 1
for u in updates:
    assert PREDICATE in u, "UPDATE learned_contracts without an org predicate: " + u
checks += 1

# The counter must reflect what the database actually changed, not what was attempted. Before
# 2026-08-18 this was `updated += 1`, so a write RLS dropped on the floor still counted as applied.
assert "cur.rowcount" in src, (
    "apply() no longer counts rowcount — a discarded UPDATE would be reported as a successful one, "
    "which is how this defect stayed invisible")
checks += 1

print(f"ok — {checks} checks: {len(sql)} learned_contracts statements, all org-scoped "
      f"({len(updates)} UPDATE), counter reads rowcount")
