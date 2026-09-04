#!/usr/bin/env python3
"""The judge must be physically unable to edit the telemetry it is scored against.

THE THREAT. A judge that can write to steering_events can make any gate look good — either by
rewriting what happened, or by deleting the fires it judged badly. That is reward hacking, and it
is not a hypothetical concern for a loop Nick has explicitly unlocked to run without human gates.
The whole value of the ledger, the retirement rule and the fitness function collapses if the
scorer can edit the scorecard.

THE DEFENCE IS A GRANT, NOT A CODE PATH. The `steering_judge` role holds:

    steering_events      SELECT
    steering_judgments   INSERT, SELECT

and nothing else. No UPDATE, no DELETE, on either table. A judge that tried would get a
permission error from Postgres, not a code review. That is the right tier for this: an invariant
enforced by the database survives a refactor, a bad merge, and an agent editing its own source.

This test asserts the grants directly rather than trusting the migration file, because a
migration that ran once is not evidence about the database as it stands today — the same
reasoning that made every other check in this suite read live state.

Skips cleanly when Postgres is unreachable so CI stays green on a bare runner.

Run: python3 bin/tests/test_judge_cannot_write_telemetry.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CORE_INSTANCE") or Path(__file__).resolve().parents[2])
ROLE = "steering_judge"
FORBIDDEN = {"UPDATE", "DELETE", "TRUNCATE"}


def q(sql: str):
    try:
        r = subprocess.run(["psql", "-d", "corebrain", "-tA", "-F", "|", "-c", sql],
                           capture_output=True, text=True, timeout=10,
                           env={**os.environ, "PGCONNECT_TIMEOUT": "3"})
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return [ln for ln in r.stdout.strip().splitlines() if ln.strip()]


def main() -> int:
    probe = q("SELECT 1")
    if probe is None:
        print("  SKIP — corebrain not reachable (expected on a CI runner)")
        return 0

    rows = q(f"""SELECT table_name, privilege_type FROM information_schema.role_table_grants
                  WHERE grantee = '{ROLE}' ORDER BY 1,2""")
    if rows is None:
        print(f"  SKIP — could not read grants for {ROLE}")
        return 0

    grants = {}
    for ln in rows:
        t, p_ = ln.split("|", 1)
        grants.setdefault(t, set()).add(p_.strip())

    p = f = 0
    print(f"=== {ROLE} may read telemetry and write only its own judgments ===\n")

    if not grants:
        print(f"  FAIL  role {ROLE} has no grants at all — the judge has no identity to run as")
        return 1

    ev = grants.get("steering_events", set())
    if ev == {"SELECT"}:
        print("  PASS  steering_events: SELECT only")
        p += 1
    else:
        print(f"  FAIL  steering_events grants are {sorted(ev)} — expected exactly ['SELECT']")
        f += 1

    jud = grants.get("steering_judgments", set())
    if jud and not (jud & FORBIDDEN):
        print(f"  PASS  steering_judgments: {sorted(jud)} — append-only, no UPDATE/DELETE")
        p += 1
    else:
        print(f"  FAIL  steering_judgments grants are {sorted(jud)}")
        f += 1

    leaked = {t: sorted(v & FORBIDDEN) for t, v in grants.items() if v & FORBIDDEN}
    if not leaked:
        print(f"  PASS  no mutating grant on any table ({len(grants)} table(s) granted)")
        p += 1
    else:
        print(f"  FAIL  mutating grants present: {leaked}")
        f += 1

    # A judgment must point at a real event. Without the FK the judge could score events that
    # never happened, which is the same reward hack by a different route.
    fk = q("""SELECT COUNT(*) FROM information_schema.table_constraints
               WHERE table_name='steering_judgments' AND constraint_type='FOREIGN KEY'""")
    if fk and int(fk[0]) >= 1:
        print("  PASS  judgments are foreign-keyed to real events")
        p += 1
    else:
        print("  FAIL  no foreign key from steering_judgments to steering_events")
        f += 1

    # THE ASSERTION THAT WAS MISSING, and whose absence made every check above cosmetic.
    #
    # The grants were correct. The role was correct. And the judge connected as `brain_app`,
    # which holds DELETE and UPDATE on both tables — so the property held for a role nothing ran
    # as. Codex found it in an adversarial pass before the baseline push; the checks above had
    # verified the lock while the door stood open.
    #
    # A capability test must exercise the CODE PATH, not the configuration the code path is
    # supposed to use. This one connects exactly as steering_judge.run() does and then tries to
    # write.
    print("\n--- the JUDGE'S OWN CONNECTION must be unable to write (the missing assertion) ---")
    try:
        import importlib.util
        sp = importlib.util.spec_from_file_location(
            "sj", ROOT / "scheduling" / "brain-pg" / "steering_judge.py")
        sj = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(sj)
        con, restricted = sj._connect_as_judge()
        cur = con.cursor()
        cur.execute("SELECT current_user")
        eff = cur.fetchone()[0]
        if not restricted or eff != ROLE:
            print(f"  FAIL  judge connects as {eff!r} (restricted={restricted}), not {ROLE!r}")
            f += 1
        else:
            print(f"  PASS  judge's effective role is {eff}")
            p += 1
            try:
                cur.execute("UPDATE steering_events SET verdict = verdict WHERE false")
                print("  FAIL  the judge's own connection CAN update steering_events")
                f += 1
            except Exception:
                print("  PASS  the judge's own connection is refused UPDATE on steering_events")
                p += 1
        try:
            con.rollback(); con.close()
        except Exception:
            pass
    except (ImportError, OSError) as e:
        print(f"  SKIP  environment: {str(e)[:70]}")
    except Exception as e:
        # NOT a skip. A bare `except` here turned a NameError in this very test into a cheerful
        # SKIP line, which is the silent-absence pattern appearing inside the test written to
        # catch it. A defect in the harness is a failure, not an excused absence.
        print(f"  FAIL  harness error while exercising the judge connection: {type(e).__name__}: {e}")
        f += 1

    print(f"\n=== Results: {p} passed, {f} failed ===")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
