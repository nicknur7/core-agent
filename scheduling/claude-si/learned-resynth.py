#!/usr/bin/env python3
"""learned-resynth.py — the auto-resynthesis ("dream") pass for the learned-workflow
layer. Regenerates contract GUIDANCE from the grown corpus instead of leaving it
hand-authored. Phase 4+ of spec-learned-workflow-layer-2026-06-05.

Split into two deterministic halves so the LLM step can run either in-session (a
subagent, Max-sub) or from a scheduled routine — and so the apply step is auditable:

  --prepare         -> dump a synthesis brief (current contracts + fresh corpus
                       samples per situation) to stdout/--out. Feed this to an LLM.
  --apply FILE      -> read the LLM's regenerated guidance JSON and reseed.

SAFETY BOUNDARY (the "don't fire blind" rule): --apply ONLY updates required_shape /
forbidden_moves (inject-only guidance). It NEVER touches `checkable` (the blocking
clauses) — those stay hand-defined in learned-contracts-seed.py. If the LLM proposes
a new blocking clause it must go in the brief's "proposals" channel for Nick's ok,
not through --apply. So an auto-run can never change what BLOCKS, only what's injected.

  CORE_ORG_ID=1 python3 learned-resynth.py --prepare --out /tmp/brief.json
  CORE_ORG_ID=1 python3 learned-resynth.py --apply /tmp/regenerated.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "brain-pg"))
from _env import load_secrets, connect_corebrain  # noqa: E402

load_secrets()

SAMPLES_PER_SITUATION = 8


def _key(situation: str) -> str:
    return situation.split(" — ")[0].strip()


def prepare(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT situation, trigger_labels, required_shape, forbidden_moves FROM learned_contracts WHERE org_id = current_setting('app.current_org_id', true)::bigint ORDER BY id")
    contracts = cur.fetchall()
    brief = {"situations": [], "corpus_total": 0,
             "instructions": (
                 "For each situation, rewrite required_shape (<=4 bullets) and "
                 "forbidden_moves (<=3 bullets) to best fit the corpus examples below — "
                 "the operator's ACTUAL corrections. Keep them concrete and imperative. Return ONLY "
                 "JSON: {situation_key: {required_shape:[...], forbidden_moves:[...]}}. "
                 "If you think a situation needs a NEW deterministic blocking rule, do NOT "
                 "put it in the JSON — list it under a separate 'proposals' key for human review.")}
    cur.execute("SELECT count(*) FROM pattern_observations WHERE org_id = current_setting('app.current_org_id', true)::bigint")
    brief["corpus_total"] = cur.fetchone()[0]
    for situation, labels, req, forb in contracts:
        key = _key(situation)
        cur.execute(
            """SELECT prompt_text, evidence_excerpt, correction_text
               FROM pattern_observations
               WHERE org_id = current_setting('app.current_org_id', true)::bigint
                 AND pattern_label = ANY(%s) AND prompt_text IS NOT NULL AND prompt_text <> ''
               ORDER BY session_date DESC LIMIT %s""",
            (labels, SAMPLES_PER_SITUATION),
        )
        samples = [{"prompt": p[:300], "response": (r or "")[:200], "correction": (c or "")[:200]}
                   for p, r, c in cur.fetchall()]
        brief["situations"].append({
            "key": key, "trigger_labels": labels,
            "current_required_shape": req, "current_forbidden_moves": forb,
            "corpus_examples": samples,
        })
    return brief


def apply(conn, regenerated: dict) -> int:
    # Accept both shapes: {situation_key: {...}} (canonical) and
    # {"situations": [{"situation": ..., ...}]} (what the trigger hook's brief asks for).
    if isinstance(regenerated.get("situations"), list):
        keyed = {s["situation"]: s for s in regenerated["situations"] if isinstance(s, dict) and "situation" in s}
        if "proposals" in regenerated:
            keyed["proposals"] = regenerated["proposals"]
        regenerated = keyed
    cur = conn.cursor()
    cur.execute("SELECT id, situation FROM learned_contracts WHERE org_id = current_setting('app.current_org_id', true)::bigint")
    by_key = {_key(s): cid for cid, s in cur.fetchall()}
    updated = 0
    for key, body in regenerated.items():
        if key == "proposals" or key not in by_key:
            continue
        req = body.get("required_shape")
        forb = body.get("forbidden_moves")
        if not isinstance(req, list) or not isinstance(forb, list) or not req:
            continue
        # NOTE: checkable is intentionally NOT in this UPDATE — blocking logic is hand-gated.
        #
        # THE org_id PREDICATE HERE IS REDUNDANT TODAY AND STAYS ANYWAY (2026-08-18). `by_key` is
        # built at :83 from this org's rows only, so `id` cannot address a foreign row — the write is
        # safe BY CONSTRUCTION, which is exactly the coupling that failed before 543aab7 scoped that
        # SELECT. An UPDATE whose safety lives in a different statement twenty lines away is one edit
        # from unsafe.
        #
        # WHAT THE UNSCOPED VERSION ACTUALLY DID — verified against pg_policy, not inferred. business
        # first reported it as cross-org CONTAMINATION and retracted that within the hour; the
        # retraction is correct and the real behaviour is worse in a quieter way:
        #
        #     learned_contracts_select  USING true                       <- all 42 rows, every org
        #     learned_contracts_update  USING org_id = current_org
        #                               WITH CHECK org_id = current_org  <- foreign row: 0 matched
        #     relrowsecurity/forced True/True, role brain_app rolbypassrls=False
        #
        # So the unscoped SELECT built `by_key` across all five orgs (42 rows, 11 keys, 10 of them
        # shared), the dict comprehension kept the LAST cid per key — and the UPDATE then aimed at a
        # foreign id, where RLS matched zero rows. **The resynthesised body was silently DISCARDED,
        # not redirected.** Nothing was contaminated; the work was thrown away.
        #
        # That is why three seats sat frozen at si_induct's hardcoded placeholder while ops's
        # contracts carried real bodies: ops holds the highest ids, so ops's own cid won every
        # collision and ops's writes were the only ones that ever landed. Not a seat receiving
        # everyone's rewrites — the one seat whose rewrites survived.
        #
        # `updated += cur.rowcount` rather than `+= 1` is the load-bearing half of this fix. A
        # discarded write previously still incremented the counter, so every seat reported success
        # for work RLS had dropped on the floor — the same invisibility as the directive terminal and
        # the trigger gate, in a third place.
        cur.execute("UPDATE learned_contracts SET required_shape=%s, forbidden_moves=%s "
                    "WHERE id=%s AND org_id = current_setting('app.current_org_id', true)::bigint",
                    ([str(x) for x in req][:4], [str(x) for x in forb][:3], by_key[key]))
        updated += cur.rowcount
    conn.commit()
    # Regenerate the classifier snapshot from the freshly-updated DB.
    cur.execute("SELECT situation, required_shape, forbidden_moves FROM learned_contracts WHERE org_id = current_setting('app.current_org_id', true)::bigint")
    snap = {_key(s): {"required_shape": req, "forbidden_moves": forb} for s, req, forb in cur.fetchall()}
    # Record the resynth watermark (corpus size now) + clear the 'due' marker, so the
    # auto-trigger in the miner only re-fires after the corpus grows again.
    cur.execute("SELECT count(*) FROM pattern_observations WHERE org_id = current_setting('app.current_org_id', true)::bigint")
    total = cur.fetchone()[0]
    state = HERE.parents[1] / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / ".learned-resynth-at").write_text(str(total))
    due = state / ".learned-resynth-due"
    if due.exists():
        due.unlink()
    return updated, snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--apply", metavar="FILE")
    ap.add_argument("--out", metavar="FILE")
    args = ap.parse_args()
    conn = connect_corebrain()
    try:
        if args.prepare:
            brief = prepare(conn)
            out = json.dumps(brief, indent=2)
            if args.out:
                Path(args.out).write_text(out)
                print(f"[resynth] brief written -> {args.out} "
                      f"({len(brief['situations'])} situations, corpus={brief['corpus_total']})")
            else:
                print(out)
        elif args.apply:
            regenerated = json.loads(Path(args.apply).read_text())
            updated, snap = apply(conn, regenerated)
            # write snapshot
            from pathlib import Path as _P
            import os
            inst = _P(os.environ.get("CORE_INSTANCE") or HERE.parents[1])
            (inst / ".claude" / "state" / "learned-contracts.json").write_text(json.dumps(snap, indent=2))
            print(f"[resynth] applied guidance updates to {updated} contracts; snapshot refreshed "
                  f"(checkable/blocking clauses untouched)")
        else:
            ap.error("specify --prepare or --apply FILE")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
