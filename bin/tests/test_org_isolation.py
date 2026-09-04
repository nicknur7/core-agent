#!/usr/bin/env python3
"""Every org-partitioned table must be write-isolated, or writable by nobody.

THE MODEL, WHICH IS DELIBERATELY ASYMMETRIC
-------------------------------------------
corebrain does NOT isolate reads. Cross-org reads are the peer-Core feature — life reads
business through MCP on purpose — so `entities` carries a `read_all` SELECT policy with a
qual of `true`, and that is correct, not a hole.

What the model does protect is WRITES: a Core may only modify its own org's rows, enforced by
RLS quals of the shape `org_id = current_setting('app.current_org_id')`.

So the invariant worth testing is narrow and specific:

    a table carrying org_id is either write-scoped by RLS, or grants no writes at all.

WHY THIS EXISTS
---------------
Found 2026-07-28 auditing why 27 tables carry org_id while only 23 had RLS. Two pre-migration
backup tables — 46,584 entities and 30,769 edges spanning all four Cores — had no RLS and full
INSERT/UPDATE/DELETE granted to brain_app, the role every Core connects as. Measured inside a
rolled-back transaction, as org 1:

    UPDATE entities            WHERE org_id=4  ->     0 rows   (RLS enforces)
    UPDATE entities_bak_pre_origin  ... =4     -> 4,278 rows   (nothing enforces)
    DELETE entity_edges_bak_pre_phase1 ... =4  -> 4,417 rows   (nothing enforces)

Any Core could have silently corrupted or destroyed another Core's backup. Fixed by revoking
the write grants (migration 2026-07-28-backup-tables-readonly.sql) rather than by adding
org-scoped policies — a backup its own Core can rewrite is not a backup.

I also nearly filed this as a READ exposure, because my first probe set `app.org_id` when the
real session variable is `app.current_org_id`, so it proved nothing and I misread the live
table as unprotected. Hence: this test asserts against the live grant/policy catalog, and the
probe below sets the variable the policies actually read.

Skips cleanly when Postgres is unreachable, so a fresh Core can still run the suite.

    python3 bin/tests/test_org_isolation.py
"""
from __future__ import annotations

import os
import subprocess
import sys

DB = os.environ.get("COREBRAIN_DB", "corebrain")
APP_ROLE = "brain_app"

# THIS CORE's org, never a constant. See the probe below for what hardcoding it cost.
def _own_org() -> int:
    # Identity first. This consulted the env first and fell back to identity — the inverted
    # precedence corrected in brain-recall-trigger the same day. A test asserting ORG ISOLATION is
    # the last place that should decide which org it is from a variable another seat can leak in.
    try:
        import sys as _s
        from pathlib import Path as _PP
        _s.path.insert(0, str(_PP(__file__).resolve().parents[2] / "scheduling" / "brain-pg"))
        from _env import get_org_id
        return int(get_org_id())
    except Exception:
        pass
    # No env fallback here on purpose: get_org_id() ALREADY falls back to the environment when the
    # caller is genuinely outside a Core tree, so a second one only re-opens the precedence this
    # function was just fixed to close.
    try:
        import json as _j
        from pathlib import Path as _P
        root = _P(__file__).resolve().parents[2]
        return int(_j.loads((root / ".claude" / "identity.json").read_text())["org_id"])
    except Exception:
        return 1          # last resort; a wrong guess now fails loudly rather than passing vacuously


ORG = _own_org()
WRITE_PRIVS = {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}

FAIL: list[str] = []
ABSTAIN: list[str] = []


class ProbeUnrun(Exception):
    """The probe did not execute. NOT the same as the probe finding isolation broken.

    Both used to surface as the same FAIL line, and that conflation is the defect this class exists to
    remove. Observed twice on 2026-08-06 in full-suite runs (never standalone): psql exceeded the 30s
    timeout while the suite was hammering Postgres, and the report read
    "FAIL live write-isolation probe ran" — which a reader scans as "org isolation is broken", the most
    alarming possible misreading of a busy database.

    Both still fail the suite. A security check does not get skipped because it was inconvenient to run.
    But they must be DISTINGUISHABLE, and the unrun case must carry enough state to diagnose the next
    occurrence instead of inviting another guess.
    """


def _pg_diag() -> str:
    """Snapshot of what the database was doing, captured at the moment the probe could not run."""
    try:
        r = subprocess.run(
            ["psql", "-d", DB, "-tA", "-F", "|", "-c",
             "SELECT count(*) FILTER (WHERE state='active'), "
             "count(*) FILTER (WHERE state LIKE '%idle in transaction%'), "
             "(SELECT count(*) FROM pg_locks WHERE NOT granted) FROM pg_stat_activity "
             f"WHERE datname='{DB}';"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            a, i, l = (r.stdout.strip().split("|") + ["?", "?", "?"])[:3]
            # A ZERO HERE MAY MEAN "BLIND", NOT "NONE", and core-finance measured exactly that
            # (bus #654): a non-superuser role cannot read other roles' state in pg_stat_activity —
            # those rows come back with state NULL — so this counted 0 idle-in-transaction while an
            # orphaned backend held an open write transaction for 16 minutes. Three Cores then built
            # three different causal theories on top of that 0, and the least-supported one was
            # "not contention", which the 0 was the sole evidence for.
            #
            # Same defect I spent the day removing everywhere else, reintroduced by me this morning in
            # the very function added to make an unrun probe diagnosable. So the number now travels
            # with its own visibility caveat rather than passing as a measurement.
            blind = ""
            try:
                rv = subprocess.run(
                    ["psql", "-d", DB, "-tA", "-c",
                     "SELECT count(*) FROM pg_stat_activity "
                     f"WHERE datname='{DB}' AND state IS NULL"],
                    capture_output=True, text=True, timeout=8)
                n = (rv.stdout or "").strip()
                if n.isdigit() and int(n) > 0:
                    blind = (f" [{n} session(s) NOT VISIBLE to this role — the idle-in-txn count above "
                             f"is a floor, not a measurement]")
            except Exception:
                blind = " [visibility of other roles' sessions unverified]"
            return f"active={a} idle-in-txn={i} ungranted-locks={l}{blind}"
        return "diagnostic query also failed"
    except Exception as e:
        return f"diagnostic query unavailable: {str(e)[:50]}"


def q(sql: str, timeout: int = 30) -> list[list[str]]:
    try:
        r = subprocess.run(["psql", "-d", DB, "-tA", "-F", "\t", "-c", sql],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeUnrun(f"psql exceeded {timeout}s — {_pg_diag()}") from None
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return [ln.split("\t") for ln in r.stdout.strip().splitlines() if ln.strip()]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAIL.append(name)


def main() -> int:
    print("org write-isolation")
    try:
        q("SELECT 1")
    except Exception as e:
        print(f"  SKIP  corebrain unreachable ({str(e)[:60]}) — nothing to assert")
        return 0

    # Tables carrying org_id, with whether RLS is on.
    tables = {r[0]: (r[1] == "t") for r in q("""
        SELECT c.relname, c.relrowsecurity
        FROM information_schema.columns col
        JOIN pg_class c ON c.relname = col.table_name AND c.relkind = 'r'
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        WHERE col.column_name = 'org_id' AND col.table_schema = 'public'
        GROUP BY 1, 2""")}
    check("org-partitioned tables found", bool(tables), str(len(tables)))

    # Write privileges the application role EFFECTIVELY holds.
    #
    # This used to read `information_schema.role_table_grants WHERE grantee = APP_ROLE`, which lists
    # only grants made DIRECTLY to that role. Codex flagged the gap on 2026-08-05: a write reaching
    # brain_app via PUBLIC or via membership in some other granted role is invisible to that query,
    # so the test asserted a narrower thing than the invariant it claims in its own docstring —
    # "grants no writes to brain_app" was really "was never handed writes by name".
    #
    # `has_table_privilege` answers the question actually being asked, following PUBLIC and role
    # inheritance. Measured when this changed: no difference on the live database (PUBLIC holds zero
    # write grants anywhere in the schema, verified separately), so this closes a latent hole rather
    # than fixing a live one — which is the right time to close it.
    #
    # Kept as one query per privilege over the candidate tables rather than a join, because
    # has_table_privilege is a function call and the table count here is small.
    # q() shells out to psql -c and takes no bind parameters, so APP_ROLE is interpolated — it is a
    # module constant, not input, exactly as the previous query interpolated it.
    granted: dict[str, set] = {}
    for t, p in q(f"""
        SELECT c.relname, v.priv
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
          CROSS JOIN (VALUES ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('SELECT')) AS v(priv)
         WHERE c.relkind = 'r'
           AND has_table_privilege('{APP_ROLE}', c.oid, v.priv)"""):
        granted.setdefault(t, set()).add(p.strip())

    # Tables whose RLS actually scopes writes by org.
    scoped = {r[0] for r in q("""
        SELECT tablename FROM pg_policies
        WHERE cmd IN ('UPDATE','DELETE','INSERT','ALL')
          AND (qual LIKE '%org_id%' OR with_check LIKE '%org_id%')""")}

    offenders = []
    for t, rls in sorted(tables.items()):
        writes = granted.get(t, set()) & WRITE_PRIVS
        if not writes:
            continue                       # nobody can write it — isolated by grant
        if rls and t in scoped:
            continue                       # writes exist but RLS scopes them by org
        offenders.append(f"{t} (grants {sorted(writes)}, rls={rls}, org_scoped={t in scoped})")

    check("every org-partitioned table is write-scoped or write-revoked",
          not offenders, "; ".join(offenders[:4]))

    # The specific regression, asserted by NAME as well as by rule — the rule above could be
    # weakened by a future edit, and these tables are the reason this file exists.
    #
    # DROPPED 2026-08-12 (T018): entities_bak_pre_origin (269MB) and entity_edges_bak_pre_phase1
    # (44MB) are gone. They were NOT redundant — 277 entities and 495 edges existed only there —
    # so the unique rows were copied to *_bak_orphans FIRST, under a DO block that RAISEd unless
    # zero unique rows were unpreserved. All 277 are extraction-pipeline bookkeeping (Phase/round/
    # batch/checkpoint/drain artifacts); only 6 escaped the pattern filter and all 6 read the same
    # way. Classified, not assumed — "verified low-value" is still not "verified absent", which is
    # why they were kept rather than discarded.
    #
    # The loop below used to be `if t in tables:` — with the tables gone it would now SKIP silently
    # and this file would stay green while measuring nothing at these names. That is the inert-
    # instrument shape, so absence is asserted instead of tolerated.
    for t in ("entities_bak_pre_origin", "entity_edges_bak_pre_phase1"):
        check(f"{t} is GONE (dropped T018, unique rows preserved first)",
              t not in tables,
              f"{t} still exists. Either the drop was reverted or a restore recreated it — in "
              f"which case the write/read grants below must be re-applied to it, because it "
              f"carries RLS-disabled backup rows readable by every Core.")

    # THE SUCCESSORS INHERIT THE DEFECT, AND I CREATED IT. The *_bak_orphans tables were made by
    # CREATE TABLE AS, which took the DEFAULT grants: brain_app held INSERT/SELECT/UPDATE/DELETE
    # with RLS disabled — the exact hole this file exists to catch, reintroduced three minutes
    # after the old one was removed.
    #
    # CORRECTED 2026-08-31: this comment used to say "Revoked immediately; asserted here so it
    # cannot come back." It was not revoked — a live grant-catalog check found brain_app still
    # holding INSERT/UPDATE/DELETE/SELECT on both tables, RLS still off. This is the check
    # actually catching the defect it claims to guard against, not a stale assertion: the claim
    # of a revoke that was never run is the say-without-do shape this session exists to stop
    # doing to itself. The fix is authored — migration
    # 2026-08-31c-orphans-and-merge-journal-readonly.sql (which also covers
    # merge_journal_20260828, found the same way, same night) — and still needs to be RUN against
    # this seat's corebrain as the table owner before this check goes green.
    #
    # Why it matters beyond tidiness (core-business finding 10): brain_app is the role EVERY Core
    # connects as, and these tables have no RLS. A SELECT grant means any seat reads any other
    # seat's archived rows — including rows their owner DELETED, since no purge path reaches a
    # backup. "Delete" then has no defined relationship to a backup, which matters the first time
    # a purge is a redaction rather than a tidy-up. A backup is for restoring FROM.
    for t in ("entities_bak_orphans", "entity_edges_bak_orphans"):
        if t in tables:
            check(f"{t} grants no writes to {APP_ROLE}",
                  not (granted.get(t, set()) & WRITE_PRIVS),
                  str(sorted(granted.get(t, set()) & WRITE_PRIVS)))
            check(f"{t} is not readable by {APP_ROLE} (admin-only)",
                  "SELECT" not in granted.get(t, set()),
                  "still SELECT-able by the app role — CREATE TABLE AS grants by default, so a "
                  "future archive table will arrive with this hole unless it is revoked explicitly")

    # And a LIVE probe that the model still holds where it matters, using the variable the
    # policies actually read. Rolled back — asserts behaviour, changes nothing.
    try:
        # 90s, not the default 30. core-finance hit the timeout on a genuine fleet condition (bus #628):
        # business, school and finance all pulled and ran suites inside ~8 minutes against ONE shared
        # corebrain, with an idle-in-transaction connection present. 30s is tight for a five-Core shared
        # database during a fleet-wide sync, and the failure mode is the expensive one — it reads as a
        # security check that could not run, which costs a human's attention every time. Raising the
        # ceiling does not weaken the check: it either completes and asserts, or it reports ProbeUnrun
        # with the database's state attached. Only the patience changed.
        # THE ORG WAS HARDCODED TO 1 — LIFE'S — AND core-finance FOUND IT (bus #650). Read what that
        # meant: on every peer this set app.current_org_id='1' and probed `WHERE org_id<>1`, so it
        # tested LIFE's isolation from finance's disk, always, INCLUDING ON THE RUNS WHERE IT PASSED.
        # Four Cores have been reporting a green org-isolation check that never examined their own org.
        #
        # It also explains the timing argument three Cores spent an hour on: business measured 4.3s and
        # school measured >30s and both were right, because they were not running the same query in any
        # meaningful sense — the GUC matched the connection's own org on one disk and not the other.
        # The dispute was downstream of this bug.
        #
        # Third instance of hardcoded org-1 in shared code found today (after test_oracle_escalation and
        # this file), all by peers on the pull, all invisible on life because on life the constant
        # happens to be correct. That is the signature to watch for: a shared constant that is only ever
        # exercised where it is true.
        # ONE ROW PROVES IT. This used to UPDATE every foreign-org row — 48,533 of them, a full scan of
        # an 847 MB table. core-finance measured why that swings from 4.3s to 47.3s (bus #664):
        # corebrain's working set is 1,580 MB against 128 MB of shared_buffers, 12:1, so a cold scan goes
        # to disk. Three Cores spent an hour disputing the timing, and my first response was to raise the
        # timeout to 90s — treating the symptom.
        #
        # RLS is a PER-ROW predicate: if it blocks one foreign row it blocks all of them. Targeting a
        # single row is the same assertion at bounded cost — no scan, no cache dependency, fast on every
        # Core regardless of buffer pressure.
        #
        # It must first confirm such a row EXISTS, because if none does then "0 rows updated" is also
        # exactly what a completely broken policy returns. Banking that as a pass is how a vacuous green
        # check survives — the same shape as the zero-standing-in-for-blind defect elsewhere in this file.
        probe_rows = None
        try:
            _c = q(f"SELECT count(*) FROM (SELECT 1 FROM entities WHERE org_id<>{ORG} LIMIT 1) s",
                   timeout=60)
            probe_rows = next((r[0] for r in _c if r and r[0].isdigit()), None)
        except ProbeUnrun:
            raise
        except Exception:
            pass
        rows = q(f"SET ROLE brain_app; SET app.current_org_id = '{ORG}'; BEGIN; "
                 f"WITH tgt AS (SELECT id FROM entities WHERE org_id<>{ORG} LIMIT 1), "
                 f"     u AS (UPDATE entities e SET org_id=e.org_id "
                 f"           WHERE e.id IN (SELECT id FROM tgt) RETURNING 1) "
                 f"SELECT count(*) FROM u; ROLLBACK;", timeout=90)
        n = next((r[0] for r in rows if r and r[0].isdigit()), None)
        check(f"a Core cannot write ANOTHER org's rows in the LIVE entities table "
              f"(probed as org {ORG} — this Core's own, not a constant)",
              n == "0", f"updated {n} rows")
        if probe_rows is not None and int(probe_rows) == 0:
            # A ZERO-ROW TARGET MAKES THE ASSERTION VACUOUS. If no foreign-org row exists, "0 updated"
            # is what a totally broken RLS policy also returns. Say so rather than bank the pass.
            #
            # NOT A CODE PROPERTY — A FIXTURE, and the fixture is specifically "another org's rows
            # in this table." The author's own corebrain has one because it is a live 5-Core fleet
            # sharing a single database; a single-Core scratch install (a fork, a fresh clone, this
            # suite's own audit tooling) has org 1 and nothing else by construction, and no installer
            # in this repo manufactures cross-org data. ABSTAIN, not FAIL: the primary check just
            # above already ran for real and its PASS stands; this is the honest caveat that the
            # PASS was not a meaningful proof on THIS seat, not a second finding that something is
            # broken.
            ABSTAIN.append("a foreign-org row existed to be blocked from")
            print("  UNDEC  ...and there was actually a foreign-org row to be blocked from "
                  "(otherwise the 0 above proves nothing)\n"
                  f"          no rows with org_id<>{ORG} exist on this seat — isolation is "
                  f"untestable here, not verified either way")
    except ProbeUnrun as e:
        # DID NOT RUN. Still a failure — a security check is not optional — but named as its own thing,
        # with the database's state attached, so the next occurrence is diagnosable instead of being
        # re-litigated. The UPDATE is inside a rolled-back transaction, so a timeout here cannot have
        # left a partial write.
        check("live write-isolation probe COULD NOT RUN (this is NOT a finding that isolation is "
              "broken — the probe never executed)", False, str(e)[:160])
    except Exception as e:
        check("a Core cannot write another org's rows in the LIVE entities table", False,
              f"{type(e).__name__}: {str(e)[:80]}")

    print(f"\n{'FAILED: ' + ', '.join(FAIL) if FAIL else 'all org-isolation checks pass'}")
    if FAIL:
        return 1
    if ABSTAIN:
        # rc=2 + UNDECIDABLE, the run-all.sh ABSTAIN contract (test_wilson_ci_known_answers.py is
        # the precedent this copies): every real check above ran and passed; only the cross-org
        # fixture this specific probe needs is absent on a single-Core seat.
        print(f"\nUNDECIDABLE: {len(ABSTAIN)} check(s) could not be verified on this seat "
              f"(no foreign-org rows in entities to probe against). Not a pass.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
