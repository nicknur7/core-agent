#!/usr/bin/env python3
"""si_seed_base.py — seed the 6 universal starter contracts into a Core's own org (step ④, per-Core SI).

Plan: PART 8. Peers had static snapshot files but EMPTY learned_contracts DB rows, so their SI could
never grow (mining/induction/fitness are DB-driven). This seeds the universal base 6 into a Core's own
org so it has a real starting point; from there its own corpus + si_induct grow its OWN contracts.

Content is fork-safe: required_shape/forbidden_moves come from the SHARED starter (learned-contracts-
starter.json, genericized to 'the operator'); triggers from si_snapshot.BASE_TRIGGERS. No life data.

Idempotent (skips keys already present). Own-org via connect_corebrain (RLS write-own); --org N uses
admin (BYPASSRLS) for one-time cross-org seeding by the writer Core.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "brain-pg"))
from _env import connect_corebrain, connect_corebrain_admin, get_org_id  # noqa: E402
from si_snapshot import BASE_TRIGGERS  # noqa: E402

STARTER = _HERE / "learned-contracts-starter.json"

# Generic situation text + mining labels for the 6 base contracts (fork-safe — 'the operator').
BASE_META = {
    "stop-and-plan": ("the operator says stop/no/wait; do not execute, lay out the plan first",
                      ["correction-stop-execution", "correction-explicit-no"]),
    "plan-not-execute": ("asked to find/diagnose/plan; deliver findings + a plan to approve, don't jump to fixing",
                         ["correction-not-what-i-want"]),
    "verify-dont-claim": ("verify state against the live source before asserting; name reversals",
                          ["correction-flip-flop", "correction-this-is-wrong"]),
    "recall-first": ("the operator references past work/people/projects or says 'you should know'; hit the brain before answering",
                     ["recall-miss", "correction-already-told-you"]),
    "model-routing-and-defaults": ("flag the tier before heavy work; apply implied preconditions without being told",
                                   ["correction-should-have"]),
    "frustration-deescalate": ("the operator is frustrated; stop, acknowledge plainly, address the real problem with data",
                               ["correction-frustration"]),
}


def seed(target_org: int, conn) -> int:
    starter = json.loads(STARTER.read_text())
    seeded = 0
    with conn.cursor() as cur:
        cur.execute("SELECT situation FROM learned_contracts WHERE org_id=%s", (target_org,))
        have = {s.split(" — ")[0].split(" - ")[0].strip() for (s,) in cur.fetchall()}
        for key, body in starter.items():
            if key in have:
                continue
            meta = BASE_META.get(key, (f"{key} — universal starter", []))
            triggers = [BASE_TRIGGERS[key]] if key in BASE_TRIGGERS else []
            cur.execute(
                """INSERT INTO learned_contracts
                     (situation, trigger_labels, required_shape, forbidden_moves, checkable,
                      example_prompts, triggers, org_id, active)
                   VALUES (%s,%s,%s,%s,'[]'::jsonb,%s,%s,%s,true)""",
                (f"{key} — {meta[0]}", meta[1],
                 body.get("required_shape", []), body.get("forbidden_moves", []),
                 [], triggers, target_org))
            seeded += 1
    conn.commit()
    return seeded


def main() -> int:
    # --org N seeds a specific org via admin (writer-Core cross-org bootstrap); default = own org.
    target = None
    for i, a in enumerate(sys.argv):
        if a == "--org" and i + 1 < len(sys.argv):
            target = int(sys.argv[i + 1])
    if target is None:
        target = get_org_id()
        conn = connect_corebrain()
    else:
        conn = connect_corebrain_admin()
    try:
        n = seed(target, conn)
        print(f"si_seed_base: seeded {n} base contract(s) into org {target} (skipped existing)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
