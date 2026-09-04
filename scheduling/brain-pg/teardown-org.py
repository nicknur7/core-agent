#!/usr/bin/env python3
"""teardown-org.py — remove a spawned Core's data (Phase 4 / §9 M8 reversibility).

The revert path for a spawned org: deletes its entities / evidence / entity_edges +
its tenants row. Cross-org same_as edges pointing at this org's entities cascade via
the entity FK (ON DELETE CASCADE). The markdown vault under $CORE_BRAIN/projects/<slug>/
is separate — this prints a reminder, it does NOT delete files.

DESTRUCTIVE. Snapshot first: scheduling/brain-pg/snapshot.sh pre-teardown-orgN.
Refuses orgs 1-4 (the live Cores: life/business/school/finance) as a hard guard.

Usage:  teardown-org.py <org_id> --confirm
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import connect_corebrain_admin

LIVE_ORGS = {1, 2, 3, 4}


def main():
    if len(sys.argv) != 3 or sys.argv[2] != "--confirm":
        sys.exit("usage: teardown-org.py <org_id> --confirm")
    org = int(sys.argv[1])
    if org in LIVE_ORGS:
        sys.exit(f"REFUSED: org {org} is a live Core (life/business/school/finance). Teardown is for spawned/test orgs only.")
    conn = connect_corebrain_admin()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM entity_edges WHERE org_id = %s", (org,))
        e_edges = cur.rowcount
        cur.execute("DELETE FROM evidence WHERE org_id = %s", (org,))
        e_ev = cur.rowcount
        cur.execute("DELETE FROM entities WHERE org_id = %s", (org,))  # cascades any remaining referencing edges
        e_ent = cur.rowcount
        cur.execute("DELETE FROM tenants WHERE org_id = %s", (org,))
        conn.commit()
        print(f"[teardown] org {org}: removed {e_ent} entities, {e_ev} evidence, {e_edges} edges, tenants row.")
        print(f"[teardown] REMINDER: archive/remove the org's markdown vault under "
              f"$CORE_BRAIN/projects/<slug>/ separately (this script only touches Postgres).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
