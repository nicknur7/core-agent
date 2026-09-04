#!/usr/bin/env python3
"""si-adopt-projection.py — give a Core's file-only artifacts a canonical row, then cut over.

THE PROBLEM THIS REPAIRS
------------------------
friction_installer.install() branches on _unified_spine(), which is just the existence of
.claude/state/.si-unified-spine:

    marker present -> si_project.upsert() then project()   canonical row created
    marker absent  -> "LEGACY pre-cutover atomic active.json path"  FILE ONLY, no DB write

That marker lives under .claude/state/**, which is in per_core_keep and therefore CANNOT sync.
It was created on core-life on 2026-07-23 and on no other Core. So life installs DB-first and
every other Core has been installing to a file and nowhere else.

Measured 2026-07-28: si_artifacts held 36 rows, all org_id=1. Business's five live artifacts —
all stamped 2026-07-27T02:29:55, one bulk write, four days AFTER the table existed — had no row
at all. Same shape on school, finance and ops.

The consequence is not cosmetic. Everything downstream of si_artifacts is inert without a row:
tuning, fitness measurement, retirement, revision history, prior_spec rollback. Those Cores were
running rules they could never measure or improve — the self-improvement loop, structurally
unable to self-improve, on four of five instances.

core-business ruled out the scarier explanation by test rather than argument: upsert() works
fine from a non-writer Core and RLS does not block it. The spine is not writer-only. The rows
were simply never written.

WHAT THIS DOES
--------------
1. Reads this Core's active.json (the artifacts that are actually firing).
2. For each one with no canonical row, INSERTs it into si_artifacts for this org.
3. Optionally creates the cutover marker so future installs go DB-first.

Order is deliberate: adopt first, cut over second. Creating the marker on a Core whose rows do
not exist yet would mean the next project() rebuilds active.json from nothing. That specific
deletion is now blocked by a fail-safe in si_project.project(), but relying on a guard to
survive a bad order is worse than using the right order.

Never touches another org — it reads CORE_ORG_ID / identity.json and writes only that org.

  python3 bin/si-adopt-projection.py                 # report what WOULD be adopted
  python3 bin/si-adopt-projection.py --apply         # insert the rows
  python3 bin/si-adopt-projection.py --apply --cutover   # ...and create the marker
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

def _core_root() -> Path:
    """This Core's root — env first, then git, then the file's own location.

    Anchoring on __file__ ALONE resolves the wrong Core the moment a peer runs this file from its
    own seat, which is exactly how core-business's clean enforcement-audit result turned out to be
    a second read of life. Correct in normal use, wrong during cross-Core review — and cross-Core
    review is the operation the fleet's autonomy depends on.
    """
    env = os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        p = Path(env).expanduser()
        if (p / ".claude").is_dir():
            return p.resolve()
    try:
        import subprocess as _sp
        top = _sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, timeout=10).stdout.strip()
        if top:
            return Path(top).resolve()
    except Exception:
        pass
    return Path(__file__).resolve().parents[1]


REPO = _core_root()
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write rows; default is report-only")
    ap.add_argument("--cutover", action="store_true",
                    help="also create .si-unified-spine so future installs go DB-first")
    a = ap.parse_args()

    import friction_installer as inst
    from _env import connect_corebrain, get_org_id

    try:
        org = get_org_id()
    except Exception:
        print("no trusted org (set CORE_ORG_ID or .claude/identity.json) — refusing", file=sys.stderr)  # lint-code-paths: ignore — error-message text, not a path op
        return 2

    active = inst._load_active().get("artifacts", [])
    if not active:
        print(f"org {org}: active.json holds no artifacts — nothing to adopt")
        return 0

    marker = inst.STATE / ".si-unified-spine"
    con = connect_corebrain()
    adopted, already, skipped = [], [], []
    try:
        cur = con.cursor()
        cur.execute("SET LOCAL app.current_org_id = %s", (str(org),))
        for art in active:
            aid = art.get("artifact_id")
            if not aid:
                skipped.append(("(no artifact_id)", "malformed entry"))
                continue
            cur.execute("SELECT 1 FROM si_artifacts WHERE org_id=%s AND artifact_id=%s", (org, aid))
            if cur.fetchone():
                already.append(aid)
                continue

            # Strip RUNTIME-ONLY keys. These are added by the projector/installer and are not part
            # of the validated spec; storing them would put the canonical row out of schema, which
            # is the defect that made tuned artifacts un-reinstallable in the first place.
            spec = {k: v for k, v in art.items()
                    if not k.startswith("_") and k not in ("trusted_regex", "enforced")}
            provenance = ("legacy" if str(aid).startswith("legacy_")
                          else art.get("_provenance") or "friction")
            if a.apply:
                cur.execute(
                    """INSERT INTO si_artifacts
                         (artifact_id, org_id, provenance, event, spec, active, quarantined,
                          revision, installed_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,true,false,1,
                               COALESCE(to_timestamp(%s), now()), now())
                       ON CONFLICT (org_id, artifact_id) DO NOTHING""",
                    (aid, org, provenance, art.get("event") or "UserPromptSubmit",
                     json.dumps(spec), art.get("_installed_at")))
            adopted.append((aid, provenance))
        if a.apply:
            con.commit()
    finally:
        con.close()

    print(f"org {org}: {len(active)} live artifacts · {len(already)} already canonical · "
          f"{len(adopted)} {'adopted' if a.apply else 'would be adopted'}")
    for aid, prov in adopted:
        print(f"  {'adopted' if a.apply else 'would adopt':14} {aid[:30]:30} provenance={prov}")
    for aid, why in skipped:
        print(f"  {'SKIP':14} {aid[:30]:30} {why}")

    if a.cutover:
        if not a.apply:
            print("\n--cutover requires --apply (refusing to cut over without adopting first)")
            return 2

        # THE MARKER AND THE CLASSIFIER'S UNREGISTRATION MUST MOVE TOGETHER.
        #
        # Writing .si-unified-spine gates si_snapshot.py, which is the only remaining writer of
        # .claude/state/learned-contracts.json. learned-classifier reads that file on EVERY prompt.
        # Set the marker without unregistering the classifier and the Core lands in the one broken
        # quadrant: writer gated, consumer live, steering every prompt off a snapshot frozen at the
        # moment of cutover — and nothing anywhere reports it, because both halves look healthy in
        # isolation. core-life sat with a 17-day-stale snapshot precisely because its cutover DID
        # unregister the classifier, which is what made the staleness harmless there.
        #
        # si-unify-cutover.sh does both, in order, with `|| die` on each. This script does only the
        # marker — core-business found it, core-ops confirmed it on its own disk as the Core that
        # would have walked into it (2026-08-05, core-bus #319/#325). Rather than duplicate the
        # unregistration here and create a second cutover path that can drift from the first, this
        # REFUSES and points at the one script that does the whole job. Consolidate, don't patch.
        settings = inst.STATE.parent / "settings.json"   # inst.STATE is <core>/.claude/state
        try:
            registered = "learned-classifier" in settings.read_text()
        except Exception:
            registered = False          # unreadable settings: do not invent a reason to block
        if registered:
            print("\nREFUSING --cutover: learned-classifier is still REGISTERED in "
                  f"{settings}.")
            print("Writing .si-unified-spine now would gate si_snapshot.py (the only writer of")
            print("learned-contracts.json) while the classifier keeps reading that file on every")
            print("prompt — steering off a snapshot frozen at this moment, with nothing to report it.")
            print("Run `bash bin/si-unify-cutover.sh` instead: it unregisters the classifier AND")
            print("writes the marker, in that order. Artifacts were still adopted by --apply above.")
            return 2

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("cutover: adopted file-only artifacts into si_artifacts\n")
        print(f"\ncutover marker created: {marker}")
        print("future installs on this Core now go DB-first.")
    elif a.apply and not marker.exists():
        print("\nNOTE: rows adopted, but .si-unified-spine is still ABSENT — new installs will "
              "keep going file-only. Re-run with --cutover when ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
