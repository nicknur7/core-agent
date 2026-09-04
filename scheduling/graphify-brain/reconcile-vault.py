#!/usr/bin/env python3
"""reconcile-vault.py — dedup the brain vault by session/agent identity.

Root cause (see tasks/research/brain-vault-dedup-2026-06-02.md): routing churn +
the 2026-05-19 core→life migration left the SAME session filed under multiple
project folders, each copy stamped with that folder's slug. export.py is
write-only (no GC), so stale-slug copies orphaned.

This reconciles: group every projects/*/{sessions,subagents}/*.md by identity
(session_id for sessions, agent_id for subagents). For each group, compute the
canonical slug via export.py's (now-fixed) categorize_cwd(cwd-from-frontmatter),
keep/relocate ONE canonical file, quarantine the rest. NEVER drops the last copy.

Default = DRY RUN (prints plan, touches nothing). --apply to execute (moves to
quarantine dir; reversible — nothing is deleted).
"""
import argparse, json, re, shutil, sys, importlib.util
from pathlib import Path
from collections import defaultdict

BRAIN = Path(__file__).resolve().parents[2]  # placeholder; overridden below
import os
BRAIN = Path(os.environ.get("CORE_BRAIN", str(Path.home() / "AI Projects/core-brain")))
PROJECTS = BRAIN / "projects"
EXPORT = BRAIN / "_build" / "export.py"

# Load the live (fixed) categorize_cwd so reconciliation uses identical routing.
spec = importlib.util.spec_from_file_location("exp", EXPORT)
exp = importlib.util.module_from_spec(spec); spec.loader.exec_module(exp)

def parse_fm(text):
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m: return {}
    out = {}
    for line in m.group(1).split("\n"):
        mm = re.match(r"^([a-zA-Z_]\w*):\s*(.*)$", line)
        if mm: out[mm.group(1)] = mm.group(2).strip().strip('"').strip("'")
    return out

SESSION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_([^_]+(?:_[^_]+)*)_([a-f0-9]{6,})\.md$")
AGENT_RE   = re.compile(r"^(\d{4}-\d{2}-\d{2})_(agent-[a-f0-9]+)\.md$")

def collect(kind):
    """kind in {sessions, subagents}. Returns identity -> list of file records."""
    groups = defaultdict(list)
    for proj in sorted(PROJECTS.iterdir()):
        d = proj / kind
        if not d.is_dir(): continue
        for p in sorted(d.glob("*.md")):
            text = p.read_text(errors="replace")
            fm = parse_fm(text)
            cwd = fm.get("cwd") or ""
            if kind == "sessions":
                sid = fm.get("session_id")
                m = SESSION_RE.match(p.name)
                if not sid and m: sid = m.group(3)
                ident = ("S", sid or p.name)
                date = fm.get("date") or (m.group(1) if m else "1970-01-01")
                sid8 = (sid or "")[:8]
            else:
                m = AGENT_RE.match(p.name)
                ident = ("A", m.group(2) if m else p.name)
                date = fm.get("date") or (m.group(1) if m else "1970-01-01")
                sid8 = None
            canon = exp.categorize_cwd(cwd) if cwd else proj.name
            groups[ident].append({
                "path": p, "proj": proj.name, "cwd": cwd, "canon": canon,
                "date": date, "sid8": sid8, "bytes": len(text),
            })
    return groups

def canonical_name(rec, kind):
    if kind == "sessions":
        return f"{rec['date']}_{rec['canon']}_{rec['sid8']}.md"
    # subagents keep agent-id filename
    return rec["path"].name

def plan(kind):
    groups = collect(kind)
    keeps, moves, quarantines = [], [], []
    for ident, recs in groups.items():
        # canonical slug = mode of canon across copies that have a cwd; prefer a
        # live core slug; fall back to any.
        canons = [r["canon"] for r in recs]
        # ops (org 5) was missing, so a ops file's canon matched nothing in LIVE and fell through
        # to `canons[0]` — filing ops vault data under whatever slug happened to sort first. Same
        # harm as the org-map fallbacks fixed alongside this, different SHAPE: a tuple of bare slugs
        # rather than a dict, which is why the registry checker walked past it (core-business,
        # bus #1020). Fifth Core added 2026-07-18; this copy never was.
        LIVE = ("life", "school", "business", "finance", "ops")
        canon = next((c for c in canons if c in LIVE), canons[0])
        for r in recs: r["canon"] = canon
        target_dir = PROJECTS / canon / kind
        target_name = canonical_name(recs[0], kind)
        target = target_dir / target_name
        # choose the survivor: the copy already at target, else the largest in the canonical folder, else the largest overall
        # Survivor = the most-complete copy (largest bytes) across ALL copies,
        # regardless of which folder it currently sits in. Tie-break: prefer a
        # copy already in the canonical folder, then one already at the target
        # path (avoids a needless rename). Quarantine never beats a bigger copy.
        survivor = max(
            recs,
            key=lambda r: (r["bytes"], r["proj"] == canon, r["path"] == target),
        )
        for r in recs:
            if r is survivor:
                if r["path"] == target:
                    keeps.append(r)
                else:
                    moves.append((r, target))
            else:
                quarantines.append(r)
    return keeps, moves, quarantines, len(groups)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--quarantine", default=str(BRAIN / "_quarantine-dedup-2026-06-02"))
    args = ap.parse_args()
    qroot = Path(args.quarantine)
    total_q = total_m = 0
    for kind in ("sessions", "subagents"):
        keeps, moves, quarantines, ngroups = plan(kind)
        nfiles = len(keeps)+len(moves)+len(quarantines)
        print(f"\n=== {kind}: {nfiles} files → {ngroups} distinct identities ===")
        print(f"  keep in place : {len(keeps)}")
        print(f"  relocate      : {len(moves)}  (move to canonical Core folder)")
        print(f"  quarantine    : {len(quarantines)}  (redundant copies)")
        # safety: every identity retains exactly one survivor
        assert len(keeps)+len(moves) == ngroups, "INVARIANT BROKEN: identity without survivor!"
        # show where quarantines come from
        from collections import Counter
        q_by = Counter(r["proj"] for r in quarantines)
        m_by = Counter((r["proj"]+"→"+tgt.parts[-3]) for r,tgt in moves)
        if q_by: print("  quarantine by source folder:", dict(q_by.most_common()))
        if m_by: print("  relocations:", dict(m_by.most_common(12)))
        total_q += len(quarantines); total_m += len(moves)
        if args.apply:
            for r, tgt in moves:
                tgt.parent.mkdir(parents=True, exist_ok=True)
                # re-stamp project/title/parent slug to canonical
                t = r["path"].read_text(errors="replace")
                t = re.sub(r'(?m)^project:\s*.*$', f'project: {r["canon"]}', t)
                tgt.write_text(t, encoding="utf-8")
                if r["path"] != tgt: r["path"].unlink()
            for r in quarantines:
                rel = r["path"].relative_to(PROJECTS)
                dst = qroot / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(r["path"]), str(dst))
    print(f"\nTOTAL: relocate {total_m}, quarantine {total_q}")
    print("APPLIED (reversible — quarantine dir holds all removed copies)" if args.apply
          else "DRY RUN — nothing changed. Re-run with --apply to execute.")

if __name__ == "__main__":
    sys.exit(main() or 0)
