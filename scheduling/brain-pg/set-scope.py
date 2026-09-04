#!/usr/bin/env python3
"""set-scope.py — mark an entity (and its evidence) shared/private (Phase 3 write primitive).

The scope framework's write path. The Core OS settings tab (Phase 5) drives this;
for now it's a CLI. Propagates scope to the entity's evidence (B4 — evidence inherits
its parent entity's scope, so a 'private' concept is hidden at BOTH the hub and the
raw-fact layer, not just the summary).

Usage:  set-scope.py <entity_id> <shared|private> [--hub-only]

=== THE PROPAGATION IN THAT DOCSTRING HAS NEVER ONCE HAPPENED (found by core-business, bus #967) ===

Both evidence UPDATEs key on `evidence.entity_id`, and that column is NULL on every row —
4,377 of 4,377 on life, 286 of 286 on business. `col = x` and `col IN (…)` are NULL-rejecting,
so those statements are not "empty today", they are PROVABLY UNSATISFIABLE. Cause located to
embed.py:581, which appends `(None, …)` where the entity id belongs; the None is locally correct
(no entity exists at that point in the pipeline) which is exactly why nobody reviewing that line
ever changed it.

The consequence reaches Nick's data: **marking a concept private hid the summary and left the raw
excerpt shared.** query.py applies a scope clause to evidence, so those excerpts stayed
returnable. And the function reported `(n_ent, n_ev)` with n_ent > 0, so the process exited 0, so
core-ux's `scope:set` handler took its success branch and rendered "private". The failure path was
already wired in the UI and this script simply never used it.

WHAT THIS FIX DOES AND DOES NOT DO. It does NOT repair the linkage — that needs a backfill pass
deciding which evidence justifies which entity, which is a design question for Nick, not a
fill-in-the-blank. It makes the unkeepable promise VISIBLE instead of silent:

  · a `private` mark that cannot reach the raw-fact layer ROLLS BACK and exits nonzero
  · `--hub-only` performs it anyway, and states precisely what is left unprotected
  · `shared` never refuses — evidence staying private is a fail-toward-MORE-private, which is safe

Refusing by default rather than half-applying is the coherent choice: a partial write that the UI
reports as failure would leave the toggle reading "shared" while the row said "private". Nothing
changes unless the whole promise can be kept, or the caller explicitly accepts less.
"""
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from _env import connect_corebrain_admin


class ScopePropagationUnavailable(RuntimeError):
    """A private mark could not reach the raw-fact layer. Raised INSTEAD OF returning a count."""


def propagation_status(scope: str, n_ev: int, linked_rows: int) -> str:
    """OK | UNAVAILABLE | NOT_REQUIRED — the whole decision, as a pure function.

    Kept pure and separate from the SQL on purpose: it is the part that has to be DOSED. A test
    that only ever sees the live database can watch this refuse forever and learn nothing, because
    a stable verdict is not evidence the instrument works — the refusal has to be shown to depend
    on the input, and `linked_rows` is that input.

    `linked_rows` is the count of evidence rows with a non-NULL entity_id. Zero means the join
    column carries no information at all, so every keyed UPDATE against it is unsatisfiable rather
    than merely unmatched — that distinction is the finding, and it is why a row count alone would
    not have caught this.
    """
    if scope != "private":
        return "NOT_REQUIRED"          # marking shared never under-protects
    if n_ev > 0:
        return "OK"                    # it genuinely reached the raw-fact layer
    if linked_rows == 0:
        return "UNAVAILABLE"           # structurally impossible, not coincidentally empty
    return "OK"                        # linkage works; this entity simply has no evidence rows


def _linked_rows(cur) -> int:
    cur.execute("SELECT count(entity_id) FROM evidence")
    return int(cur.fetchone()[0])


def _guard(cur, conn, scope, n_ev, hub_only, what):
    """Roll back and raise unless the private promise held — or the caller accepted less."""
    status = propagation_status(scope, n_ev, _linked_rows(cur))
    if status != "UNAVAILABLE":
        return status
    if hub_only:
        return status
    conn.rollback()
    raise ScopePropagationUnavailable(
        "REFUSING TO REPORT A PRIVATE MARK THAT DOES NOT HIDE THE RAW FACTS.\n"
        "  requested : %s -> private\n"
        "  hub layer : would have been marked private\n"
        "  raw facts : CANNOT BE REACHED — evidence.entity_id is NULL on every row, so the\n"
        "              propagation UPDATE is unsatisfiable, not merely unmatched. The excerpts\n"
        "              stay returnable by query.py's scope clause.\n"
        "  cause     : embed.py:581 writes None where the entity id belongs; the backfill that\n"
        "              would link them is an unbuilt design decision, not a bug fix.\n"
        "Nothing was changed. Re-run with --hub-only to mark ONLY the hub private and accept\n"
        "that the raw excerpts remain shared." % what)


def set_scope(entity_id: int, scope: str, hub_only: bool = False) -> tuple:
    conn = connect_corebrain_admin()  # BYPASSRLS: can update any org's rows
    try:
        cur = conn.cursor()
        cur.execute("UPDATE entities SET scope = %s WHERE id = %s", (scope, entity_id))
        n_ent = cur.rowcount
        cur.execute("UPDATE evidence SET scope = %s WHERE entity_id = %s", (scope, entity_id))
        n_ev = cur.rowcount
        status = _guard(cur, conn, scope, n_ev, hub_only, "entity %s" % entity_id)
        conn.commit()
        return n_ent, n_ev, status
    finally:
        conn.close()


def set_scope_by_name(name: str, org: int, scope: str, hub_only: bool = False) -> tuple:
    """Resolve an entity by (name, org) — the path the Core OS brain tab uses when
    a human toggles a node's sharing. Case-insensitive, excludes Source hubs."""
    conn = connect_corebrain_admin()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE entities SET scope = %s WHERE lower(name) = lower(%s) AND org_id = %s AND kind <> 'Source'",
                    (scope, name, org))
        n_ent = cur.rowcount
        # This UPDATE was split across two string literals, which put quote-space-quote between IN
        # and `(` and hid it from business's `IN\s*\(` matcher on that matcher's first run — a
        # sibling of this file's own motivating defect, in the same function, four lines down.
        # I kept it split so its control would keep exercising the concatenated form; business
        # asked me not to, and is right: a load-bearing oddity preserved for someone else's test is
        # a trap for the next reader, who cannot see why it must stay ugly. Its control carries the
        # concatenated case as a compiled-in fixture now and needs no live specimen. Joined.
        cur.execute("UPDATE evidence SET scope = %s WHERE entity_id IN (SELECT id FROM entities WHERE lower(name) = lower(%s) AND org_id = %s)",
                    (scope, name, org))
        n_ev = cur.rowcount
        status = _guard(cur, conn, scope, n_ev, hub_only, "'%s' (org %s)" % (name, org))
        conn.commit()
        return n_ent, n_ev, status
    finally:
        conn.close()


def _report(label, scope, n_ent, n_ev, status):
    """Print the outcome. A partial result must never render as an unqualified success.

    core-ux's `scope:set` handler branches on the EXIT CODE and shows stdout as the message, so
    this text is what Nick reads after flipping the toggle. `--hub-only` still exits 0 — he asked
    for it — but the line says what is unprotected rather than counting rows that were not touched.
    """
    if status == "UNAVAILABLE":
        print(f"[set-scope] {label} -> {scope} (HUB ONLY — {n_ent} entity row)")
        print("[set-scope] WARNING: the raw excerpts are NOT private and remain returnable. "
              "evidence.entity_id is NULL on every row, so nothing links them to this concept.")
        return
    print(f"[set-scope] {label} -> {scope} ({n_ent} entity, {n_ev} evidence rows)")


def main():
    args = sys.argv[1:]
    hub_only = "--hub-only" in args
    args = [a for a in args if a != "--hub-only"]
    try:
        # Legacy form: set-scope.py <entity_id> <shared|private>
        if len(args) == 2 and args[0].isdigit() and args[1] in ("shared", "private"):
            n_ent, n_ev, status = set_scope(int(args[0]), args[1], hub_only)
            _report(f"entity {args[0]}", args[1], n_ent, n_ev, status)
            return
        # Name form: set-scope.py --name NAME --org N --scope shared|private  (UI path)
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--name", required=True)
        ap.add_argument("--org", type=int, required=True)
        ap.add_argument("--scope", choices=["shared", "private"], required=True)
        a = ap.parse_args(args)
        n_ent, n_ev, status = set_scope_by_name(a.name, a.org, a.scope, hub_only)
        if n_ent == 0:
            sys.exit(f"[set-scope] no entity named '{a.name}' in org {a.org}")
        _report(f"'{a.name}' (org {a.org})", a.scope, n_ent, n_ev, status)
    except ScopePropagationUnavailable as e:
        # NONZERO ON PURPOSE. This is the whole fix: the UI's ok:false branch already existed and
        # was simply never reachable, because n_ent > 0 made the process exit 0 no matter what
        # happened at the raw-fact layer.
        sys.exit(str(e))


if __name__ == "__main__":
    main()
