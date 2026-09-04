#!/usr/bin/env python3
"""system-inventory.py — what this Core ACTUALLY is, derived from the live system.

WHY THIS EXISTS. The operator, 2026-08-08: make sure every Core fully understands its own system.

This is right and the evidence is the whole evening. Two Cores spent hours DISCOVERING things that
had always been there — a source pattern shaped for finance shipped fleet-wide, a peer list written
from life's seat, a retirement loop that has never once fired, a hook file present on disk but
never registered. None of those were new. They were all found by accident, one at a time, while
looking for something else.

The failure is not that the system is complex. It is that nobody could ANSWER a question about it
without going and reading code, so nobody asked, so the answers drifted for months.

`tasks/system-rundown.md` is prose, hand-maintained, and therefore subject to the exact defect the
staleness plan is about: it RESTATES the system instead of DERIVING it. This does the opposite —
every line is computed from settings.json, the hook registry, the ledger, the manifest, the state
directory and Postgres. It can be wrong (a bug here) but it cannot go STALE, because there is
nothing to update.

  python3 bin/system-inventory.py            # human readout
  python3 bin/system-inventory.py --json      # machine-readable
  python3 bin/system-inventory.py --dead      # only the things that are not working
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import ast
import sys
import os
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
STATE = REPO / ".claude" / "state"


def _j(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def registered_hooks() -> dict:
    """event -> [hook names], straight from settings.json. The ONLY authority on what runs."""
    s = _j(REPO / ".claude" / "settings.json", {}) or {}
    out = {}
    for event, entries in (s.get("hooks") or {}).items():
        names = []
        for e in entries or []:
            for h in e.get("hooks", []) or []:
                m = re.search(r"hooks/([a-z0-9_-]+)\.(?:py|sh)", h.get("command", "") or "")
                if m:
                    names.append(m.group(1))
        out[event] = sorted(set(names))
    return out


def on_disk_hooks() -> set:
    d = REPO / ".claude" / "hooks"
    return {p.stem for p in d.glob("*.py")} | {p.stem for p in d.glob("*.sh")} if d.is_dir() else set()


def registry() -> tuple[set, dict]:
    reg = _j(REPO / "bin" / "hook-registry.json", {}) or {}
    hooks = reg if isinstance(reg, list) else reg.get("hooks", reg)
    if isinstance(hooks, dict):
        hooks = [dict(name=k, **(v if isinstance(v, dict) else {})) for k, v in hooks.items()]
    known = {h["name"] for h in hooks if h.get("name")}
    retired = {h["name"]: (h.get("retired_reason") or "")[:90]
               for h in hooks if h.get("retired_reason") or h.get("retired_at")}
    return known, retired


def ledger_verdicts() -> dict:
    d = _j(STATE / "steering-ledger.json", {}) or {}
    return {c.get("hook"): {"verdict": c.get("verdict"), "fires": c.get("fires") or 0}
            for c in d.get("components", [])}


def retire_state() -> dict:
    return _j(STATE / "retire-observations.json", {}) or {}


def artifact_reconciliation() -> dict:
    """DB artifact counts vs the on-disk projection, with the predicate spelled out.

    WHY THIS IS A SECTION AND NOT AN ANSWER. An auditor reported "active.json shows 21 live
    artifacts, the DB shows 23 — never root-caused" and left it as an open discrepancy. There is no
    discrepancy: 21 are active AND NOT quarantined, and 2 more are active AND quarantined. Counting
    `active` alone gives 23. The projection is correct and always was.

    That question has now been asked at least twice. Answering it once in prose would leave it to be
    asked a third time, so the count is DERIVED here with the predicate visible — which is the same
    reason this whole file exists rather than tasks/system-rundown.md.
    """
    q = ("select coalesce(sum((active and not quarantined)::int),0),"
         " coalesce(sum((active and quarantined)::int),0),"
         " coalesce(sum((not active)::int),0) from si_artifacts where org_id=%s")
    try:
        org = json.loads((REPO / ".claude" / "identity.json").read_text()).get("org_id", 1)
        r = subprocess.run(["psql", "-d", os.environ.get("COREBRAIN_DB", "corebrain"), "-At", "-F", "|", "-c", q % int(org)],
                           capture_output=True, text=True, timeout=30)
        live, quar, inactive = (int(x or 0) for x in r.stdout.strip().split("|"))
    except Exception:
        return {}
    proj = None
    try:
        f = REPO / ".claude" / "state" / "friction-artifacts" / "active.json"
        proj = len(json.loads(f.read_text()).get("artifacts", []))
    except Exception:
        pass
    return {"db_live": live, "db_quarantined": quar, "db_inactive": inactive, "projection": proj}


def db_tables() -> dict:
    try:
        r = subprocess.run(
            ["psql", "-d", os.environ.get("COREBRAIN_DB", "corebrain"), "-At", "-F", "|", "-c",
             "select relname, n_live_tup from pg_stat_user_tables order by n_live_tup desc"],
            capture_output=True, text=True, timeout=30)
        out = {}
        for line in r.stdout.splitlines():
            if "|" in line:
                n, c = line.rsplit("|", 1)
                out[n] = int(c or 0)
        return out
    except Exception:
        return {}


def scheduling_subsystems() -> dict:
    d = REPO / "scheduling"
    if not d.is_dir():
        return {}
    return {p.name: len(list(p.glob("*.py")) + list(p.glob("*.sh")))
            for p in sorted(d.iterdir()) if p.is_dir() and not p.name.startswith("_")}


# ── ACTUATORS: what this Core can change by itself, and what is switched off ─────────────────────
#
# FOLDED IN, NOT SHIPPED SEPARATELY. I built this as a standalone bin/actuator-map.py and then found
# THIS FILE, which exists for the same reason and says so: the operator, 2026-08-08, make sure
# every Core fully understands its own system. I wrote a second inventory tool in the very
# session he said we had not mapped what we already had — the exact failure, committed while
# responding to it. So the one genuinely new column lives here and there is ONE inventory, per his
# standing directive to consolidate rather than add a parallel mechanism.
#
# The new column is intra-code REACHABILITY. The hook sections above answer "is this hook
# REGISTERED", which is a different question from "does anything CALL this function". A write-capable
# function that nothing calls is capability the system believes it has and does not.

_ACT_SKIP = ("/tests/", "/archive/", "/.venv/", "/node_modules/")
_WRITE_CALLS = {"write_text", "write_bytes", "writelines", "mkdir", "unlink", "rename", "replace"}
_SQL_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.I)
_GIT_WRITE = re.compile(r"\bgit\b.{0,40}?\b(commit|push|checkout|reset)\b")


def _act_durable_write(node):
    """Does this function body perform a durable write? Returns 'file' / 'db' / 'git', else None."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            fn = n.func
            if isinstance(fn, ast.Attribute) and fn.attr in _WRITE_CALLS:
                return "file"
            if isinstance(fn, ast.Name) and fn.id == "open":
                for a in n.args[1:]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        if any(c in a.value for c in "wax"):
                            return "file"
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if _SQL_WRITE.search(n.value):
                return "db"
            if _GIT_WRITE.search(n.value):
                return "git"
    return None


def _act_strip(src):
    """Source with comments and docstrings removed — a call named in a comment is NOT a caller.

    This is how skill_graduate.promote() reads as live on this seat: its only appearance outside its
    own module is inside a comment saying it "can later graduate a proven workflow".
    """
    txt = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    txt = re.sub(r'"""(?:.|\n)*?"""', "", txt)
    txt = re.sub(r"'''(?:.|\n)*?'''", "", txt)
    return txt


def actuators():
    """Write-capable functions, and which of them nothing calls.

    CALLER MATCHING TOLERATES A `module.` PREFIX AND COUNTS SAME-MODULE CALLS. Both were wrong in
    the first cut and both produced FALSE DARK — the expensive direction, because a false "dark"
    sends someone to rebuild a working mechanism while a false "wired" only costs a second look.

    The count went 63 -> 8 -> 2 -> 1 and every step was a defect in this code, not a change in the
    tree: requiring a caller in a DIFFERENT module flagged CLI entry points invoked by their own
    main(); a negative lookbehind excluding a preceding dot flagged route() while friction_loop.py
    calls `fr.route(...)`; and @mcp.tool() functions are registered rather than called.
    """
    found, sources = [], {}
    for d in ("scheduling", "bin", ".claude/hooks"):
        base = REPO / d
        if not base.is_dir():
            continue
        for q in base.rglob("*.py"):
            if any(s in str(q) for s in _ACT_SKIP):
                continue
            try:
                raw = q.read_text(errors="ignore")
                tree = ast.parse(raw)
            except Exception:
                continue
            rel = str(q.relative_to(REPO))
            sources[rel] = _act_strip(raw)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith("_"):
                        continue
                    kind = _act_durable_write(node)
                    if kind:
                        found.append({"file": rel, "name": node.name, "kind": kind,
                                      "decorated": bool(node.decorator_list)})
    dark = []
    for a in found:
        pat = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
                         + re.escape(a["name"]) + r"\s*\(")
        hits = 0
        for rel, txt in sources.items():
            n = len(pat.findall(txt))
            if rel == a["file"]:
                n -= len(re.findall(r"def\s+" + re.escape(a["name"]) + r"\s*\(", txt))
            hits += max(0, n)
        if hits == 0 and not a["decorated"]:
            dark.append(a)
    return {"total": len(found), "dark": dark}


def build() -> dict:
    reg_hooks = registered_hooks()
    live = {n for names in reg_hooks.values() for n in names}
    disk = on_disk_hooks()
    known, retired = registry()
    ledger = ledger_verdicts()
    retire = retire_state()

    # THE FOUR STATES A HOOK CAN BE IN. Conflating them is what hid time-claim-gate for two days:
    # the file existed, so every existence check passed, and it was never registered.
    orphaned = sorted(disk - live - set(retired))        # on disk, not registered, not tombstoned
    ghost = sorted(live - disk)                          # registered but the file is GONE
    tombstoned_present = sorted(set(retired) & disk)     # retired but still on disk
    unregistered_known = sorted((known - live) - set(retired))

    never_fired = sorted(h for h, v in ledger.items() if not v["fires"])
    cost_blind = sorted(h for h, v in ledger.items() if v["verdict"] == "COST-BLIND")
    decided = sorted(h for h, v in ledger.items()
                     if v["verdict"] in ("EARNING", "EXPENSIVE", "PRE-EMPTED", "RARE-VALUABLE"))
    retirable = sorted(h for h, v in ledger.items() if v["verdict"] in ("PRE-EMPTED", "EXPENSIVE"))
    streaks = {k: (v or {}).get("streak", 0) for k, v in retire.items() if isinstance(v, dict)}

    return {
        "core": REPO.name,
        "hooks": {
            "registered_by_event": reg_hooks,
            "live_count": len(live),
            "on_disk": len(disk),
            "tombstoned": len(retired),
            "ORPHANED_on_disk_not_registered": orphaned,
            "GHOST_registered_but_file_missing": ghost,
            "tombstoned_still_on_disk": tombstoned_present,
            "known_but_unregistered": unregistered_known,
        },
        "measurement": {
            "components": len(ledger),
            "never_fired": never_fired,
            "cost_blind": cost_blind,
            "decided": decided,
            "retirable_now": retirable,
            "proof_streaks_started": sum(1 for v in streaks.values() if v),
            "proof_window_required": 3,
            "ever_retired": bool([1 for v in streaks.values() if v >= 3]),
        },
        "scheduling": scheduling_subsystems(),
        "db": db_tables(),
        "sync": {
            "shared_dirs": (_j(REPO / "bin" / "sync-manifest.json", {}) or {})
                            .get("shared", {}).get("dirs", []),
            "per_core_keep": len((_j(REPO / "bin" / "sync-manifest.json", {}) or {})
                                 .get("per_core_keep", [])),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dead", action="store_true", help="only what is not working")
    a = ap.parse_args()
    inv = build()
    if a.json:
        print(json.dumps(inv, indent=2))
        return 0

    h, m = inv["hooks"], inv["measurement"]
    print(f"\n  SYSTEM INVENTORY — {inv['core']}   (derived, not documented)\n")
    print(f"  HOOKS   {h['live_count']} registered · {h['on_disk']} on disk · {h['tombstoned']} tombstoned")
    for ev, names in sorted(h["registered_by_event"].items()):
        print(f"    {ev:16s} {len(names):2d}  {', '.join(names)[:88]}")

    print(f"\n  DISCREPANCIES — the four states a hook can be in, kept apart on purpose:")
    for label, key in (("on disk, NEVER REGISTERED", "ORPHANED_on_disk_not_registered"),
                       ("registered, FILE MISSING", "GHOST_registered_but_file_missing"),
                       ("tombstoned but still on disk", "tombstoned_still_on_disk"),
                       ("in registry, not registered", "known_but_unregistered")):
        v = h[key]
        print(f"    {label:32s} {len(v):2d}  {', '.join(v)[:80] if v else '—'}")

    print(f"\n  MEASUREMENT — {m['components']} components in the steering ledger")
    print(f"    never fired in window          {len(m['never_fired']):2d}  {', '.join(m['never_fired'])[:78]}")
    print(f"    fire but COST-BLIND            {len(m['cost_blind']):2d}  {', '.join(m['cost_blind'])[:78]}")
    print(f"    have a real verdict            {len(m['decided']):2d}  {', '.join(m['decided'])[:78]}")
    print(f"    ELIGIBLE to retire now         {len(m['retirable_now']):2d}  {', '.join(m['retirable_now']) or '—'}")
    print(f"    proof streaks started          {m['proof_streaks_started']:2d} (need {m['proof_window_required']})")
    print(f"    anything EVER retired?         {'yes' if m['ever_retired'] else 'NO'}")

    print(f"\n  SCHEDULING  {len(inv['scheduling'])} subsystems")
    for k, n in inv["scheduling"].items():
        print(f"    {k:26s} {n:3d} scripts")

    _act = actuators()
    print(f"\n  ACTUATORS  {_act['total']} write-capable functions (file / db / git)")
    if _act["dark"]:
        print(f"    NO CALLER ANYWHERE  {len(_act['dark']):3d}  <- capability this Core believes it has")
        for a in sorted(_act["dark"], key=lambda x: x["file"]):
            print(f"      {a['name'] + '()':26s} {a['file']:44s} writes:{a['kind']}")
    else:
        print("    NO CALLER ANYWHERE    0  — every write path is reachable from some code path")
    print("    (reachability only; a shell/cron/hook caller is counted in the HOOKS section above)")

    _rec = artifact_reconciliation()
    if _rec:
        _match = "MATCHES" if _rec["projection"] == _rec["db_live"] else "MISMATCH"
        print(f"\n  ARTIFACTS  db active&not-quarantined {_rec['db_live']} · "
              f"quarantined {_rec['db_quarantined']} · inactive {_rec['db_inactive']}")
        print(f"    on-disk projection {_rec['projection']}  -> {_match}")
        print("    (counting `active` alone gives db_live+quarantined and looks like a discrepancy;"
              " it is not)")

    if inv["db"]:
        print(f"\n  BRAIN DB  {len(inv['db'])} tables (top by rows)")
        for k, n in list(inv["db"].items())[:8]:
            print(f"    {k:34s} {n:>8,}")

    print(f"\n  SYNC  {len(inv['sync']['shared_dirs'])} shared dirs · "
          f"{inv['sync']['per_core_keep']} per_core_keep patterns\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
