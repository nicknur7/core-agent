#!/usr/bin/env python3
"""si-record.py — export the loop's REAL history, anonymized.

WHY THIS MATTERS MORE THAN THE DEMO
-----------------------------------
bin/si-demo proves the MECHANISM: a correction becomes a tested, installed, firing rule in
under two seconds. A sophisticated reader will correctly note that this proves the pipe
exists, not that the loop learns — the synthetic case is hand-fed, and "measured fitness"
inside a tempdir over 1.7 seconds is a function returning, not a measurement.

The evidence that it LEARNS is the history: rules mined from real corrections, revised when
they misfired, retired when they stopped earning their place. That history exists in one
place — this Core's Postgres — and has never been exportable.

So: the demo shows the pipe, this shows the water.

  python3 bin/si-record.py                 print
  python3 bin/si-record.py --write         write docs/si-track-record.md

WHAT IS AND IS NOT ANONYMIZED
-----------------------------
Rule TEXT is the thing that carries content — a contract message often quotes what Nick
said. So the export publishes STRUCTURE and COUNTS: how many rules, how many revisions,
which kinds, over what window. It does not publish rule bodies, correction text, or case
content. That is not squeamishness: the same corpus was already leaked to the shared
baseline once (commit 5ab58a6, 296KB of verbatim corrections), and a track record that
cannot be published without a second review is a track record nobody publishes.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
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
OUT = REPO / "docs" / "si-track-record.md"


def q(sql: str) -> list:
    """Read-only query against corebrain. Returns [] when the DB is unreachable, because a
    Core without a database must still be able to run this and get an honest empty answer
    rather than a traceback."""
    try:
        r = subprocess.run(["psql", "-d", os.environ.get("COREBRAIN_DB", "corebrain"),
                            "-tA", "-F", "\t", "-c", sql],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return []
        return [ln.split("\t") for ln in r.stdout.strip().splitlines() if ln.strip()]
    except Exception:
        return []


def _local_names() -> list:
    """Names to redact, read from LOCAL identity — never hardcoded here.

    The first version of _scrub listed real names as literal strings. That put a third
    party's name permanently into the git history of a repo that is explicitly a publishable
    template with an external fork — a redaction function leaking the very names it exists to
    remove. sentinel-code caught it: the same class of leak as before, moved from data into
    code.

    So the list comes from .claude/identity.json (per_core_keep, never syncs) plus anything in
    .claude/state/redact-names.txt. A Core with neither still redacts quoted spans, which is
    the bulk of the risk; it simply cannot redact bare names it was never told about. That is
    the correct failure: a shared file should not know who anyone is.
    """
    out = set()
    try:
        import json as _j
        ident = _j.loads((REPO / ".claude" / "identity.json").read_text())
        for v in (ident.get("user") or {}).values():
            if isinstance(v, str):
                out.update(w for w in v.replace("@", " ").split() if len(w) > 2)
        for sp in (ident.get("sister_projects") or []):
            lbl = sp.get("label") if isinstance(sp, dict) else None
            if isinstance(lbl, str) and len(lbl) > 2:
                out.add(lbl)
    except Exception:
        pass
    try:
        extra = (REPO / ".claude" / "state" / "redact-names.txt").read_text()
        out.update(w.strip() for w in extra.splitlines() if w.strip() and not w.startswith("#"))
    except Exception:
        pass
    return sorted(out)

def _scrub(text: str) -> str:
    """Strip quoted material and personal names from a reason line before publishing it.

    Retirement reasons are written for the fleet and often quote the correction that caused
    the retirement — which is exactly the content this export exists to keep out. The first
    generated copy of the track record carried a verbatim quote and a name, in a document
    whose own header promises "no rule text, no correction text". A policy the generator does
    not enforce is a policy that holds until the first interesting case.

    The EVIDENCE survives — rates, counts, the reason in the abstract. Only the quotation goes.
    """
    import re as _re
    t = _re.sub(r"['\u2018\u2019\"\u201c\u201d][^'\u2018\u2019\"\u201c\u201d]{6,}['\u2018\u2019\"\u201c\u201d]",
                "[quote redacted]", text)
    for name in _local_names():
        t = _re.sub(r"\b" + _re.escape(name) + r"\b:?\s*", "the operator ", t)
    t = _re.sub(r"\s{2,}", " ", t)
    return t.strip()

def build() -> dict:
    d: dict = {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}

    prov = q("SELECT provenance, count(*), count(*) FILTER (WHERE active), "
             "count(*) FILTER (WHERE prior_spec IS NOT NULL), max(revision) "
             "FROM si_artifacts GROUP BY provenance ORDER BY 2 DESC")
    d["by_provenance"] = [
        {"provenance": r[0].strip(), "total": int(r[1]), "active": int(r[2]),
         "revised": int(r[3]), "max_revision": int(r[4] or 0)} for r in prov if len(r) >= 5]

    kinds = q("SELECT spec->>'type', count(*) FROM si_artifacts GROUP BY 1 ORDER BY 2 DESC")
    d["by_kind"] = [{"kind": (r[0] or "?").strip(), "n": int(r[1])} for r in kinds if len(r) >= 2]

    modes = q("SELECT spec->'effect'->>'mode', count(*) FROM si_artifacts WHERE active "
              "GROUP BY 1 ORDER BY 2 DESC")
    d["active_by_effect"] = [{"mode": (r[0] or "?").strip(), "n": int(r[1])} for r in modes if len(r) >= 2]

    cases = q("SELECT count(*), min(created_at)::date, max(created_at)::date FROM friction_cases")
    if cases and len(cases[0]) >= 3:
        d["cases"] = {"mined": int(cases[0][0]), "first": cases[0][1].strip(),
                      "latest": cases[0][2].strip()}

    quar = q("SELECT count(*) FROM si_artifacts WHERE quarantined")
    d["quarantined"] = int(quar[0][0]) if quar and quar[0] else 0

    # Hook-side evidence: the estate's own measured verdicts, if they have been run.
    try:
        g = json.loads((REPO / ".claude" / "state" / "intent-grades.json").read_text())
        tally: dict = {}
        for v in g.get("gates", {}).values():
            tally[v.get("verdict")] = tally.get(v.get("verdict"), 0) + 1
        d["hook_intent_verdicts"] = tally
    except Exception:
        pass
    try:
        gg = json.loads((REPO / ".claude" / "state" / "gate-grades.json").read_text())
        tally2: dict = {}
        for v in gg.get("gates", {}).values():
            tally2[v.get("verdict")] = tally2.get(v.get("verdict"), 0) + 1
        d["hook_rate_verdicts"] = tally2
    except Exception:
        pass

    # Retirement is the leg no competitor claims, so it is stated explicitly rather than
    # left for a reader to infer from counts.
    reg = json.loads((REPO / "bin" / "hook-registry.json").read_text())
    tombs = [h for h in reg["hooks"] if h.get("retired")]
    seen, uniq = set(), []
    for t in tombs:
        if t["name"] not in seen:
            seen.add(t["name"])
            uniq.append({"hook": t["name"], "why": _scrub(t.get("retired_reason") or "")[:300]})
    d["retired_hooks"] = uniq
    d["hooks_with_intent"] = sum(1 for h in reg["hooks"] if h.get("intent"))
    return d


def render(d: dict) -> str:
    L = [
        "# Self-improvement: the track record",
        "",
        f"_Generated {d['generated']} from this Core's live database. Structure and counts only —",
        "no rule text, no correction text. See bin/si-record.py for why._",
        "",
        "This is the evidence the loop **learns**. `bin/si-demo` shows the mechanism — a",
        "correction becoming a tested, installed, firing rule in about a second. That proves the",
        "pipe exists. This proves water went through it.",
        "",
    ]
    if d.get("cases"):
        c = d["cases"]
        L += [f"**{c['mined']} corrections mined** from real sessions, {c['first']} to {c['latest']}.", ""]

    if d.get("by_provenance"):
        L += ["## Rules the system wrote for itself", "",
              "| origin | total | active | revised at least once | most revisions |",
              "|---|---:|---:|---:|---:|"]
        for r in d["by_provenance"]:
            L.append(f"| {r['provenance']} | {r['total']} | {r['active']} | {r['revised']} | {r['max_revision']} |")
        L += ["",
              "`revised` counts rules carrying a `prior_spec` — a previous version kept for rollback.",
              "A revision means the rule was changed after installation because evidence said so,",
              "not because a human edited it.", ""]

    if d.get("active_by_effect"):
        L += ["## What the live rules do", "",
              "| effect | live |", "|---|---:|"]
        for r in d["active_by_effect"]:
            L.append(f"| {r['mode']} | {r['n']} |")
        L += ["", "`inject` adds a reminder to context. `block` stops the turn until the",
              "condition is satisfied. Blocks are deliberately rare and must survive a",
              "shadow-proof window before they enforce.", ""]

    if d.get("hook_intent_verdicts") or d.get("hook_rate_verdicts"):
        L += ["## The hand-written gates, measured against themselves", ""]
        if d.get("hooks_with_intent"):
            L.append(f"{d['hooks_with_intent']} gates carry an intent record — the examples they must")
            L.append("catch and must not — so behaviour can be compared to purpose rather than to a rate.")
            L.append("")
        if d.get("hook_intent_verdicts"):
            L.append("Intent verdicts: " +
                     ", ".join(f"**{k}** {v}" for k, v in sorted(d["hook_intent_verdicts"].items())))
        if d.get("hook_rate_verdicts"):
            L.append("Rate verdicts: " +
                     ", ".join(f"**{k}** {v}" for k, v in sorted(d["hook_rate_verdicts"].items())))
        L.append("")

    L += ["## Retirement", ""]
    if d.get("retired_hooks"):
        L.append("Rules are removed when evidence says they stopped earning their place:")
        L.append("")
        for r in d["retired_hooks"]:
            L.append(f"- **{r['hook']}** — {r['why']}")
        L.append("")
    else:
        L += ["No hook has been retired yet.", ""]
    if d.get("quarantined"):
        L += [f"{d['quarantined']} artifact(s) currently quarantined by the watchdog.", ""]

    L += ["## What this does not claim", "",
          "These are counts from one deployment — the author's. They show a loop that mines,",
          "installs, revises and retires on its own evidence. They do not show it working for",
          "anyone else, because it has not yet run for anyone else.", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    d = build()
    if a.json:
        print(json.dumps(d, indent=2))
        return 0
    md = render(d)
    if a.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(md)
        print(f"wrote {OUT.relative_to(REPO)}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
