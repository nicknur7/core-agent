#!/usr/bin/env python3
"""bin/gen-architecture-doc.py — roster JSON -> docs/architecture/core-system-architecture.html.

WHY THIS EXISTS (2026-09-02). The architecture doc it replaces was hand-written, had never been
opened, and drifted until it named hooks that don't exist and skipped ones that do — plus it
carried private fleet detail (real Core names) that has no business in a doc meant for a stranger
cloning this repo. The fix is structural, not another hand edit: this script is the ONLY thing
that may write docs/architecture/core-system-architecture.html. If the system changes, re-run
this — never edit the HTML directly.

INPUT: the JSON emitted by bin/gen-arch-roster.py — one row per capability, each carrying
{id, seat, layer, rung, event, owner_file, evidence, reachable, retired_reason}. Every node this
script draws comes from a roster row; it does not invent architecture.

LAYERS: Sense -> Judge -> Learn -> Act, with Police cutting across all four (it can intercept at
any point). Rendered in that order, each as its own section grouped by rung (hooks further
grouped by event, since "each hook shows its EVENT" is the point of the exercise). Retired rows
(reachable=false) render in the same section as their active peers, visibly greyed, with the
one-line reason attached — a reader should see what was tried, not just what survived.

PRIVACY: this doc describes ONE seat — whichever one gen-arch-roster.py was run against — never a
fleet. `_scrub()` additionally redacts a defensive, extensible list of literal-name patterns from
any upstream prose this script embeds verbatim (hook-registry.json's retired_reason/intent prose
was authored for an internal audience and is not covered by bin/prose-privacy-lint.py's narrower
JSON scan, which only inspects `*_comment`-named keys). If your own hook-registry.json narrates
your name into a reason string, add your pattern to `_REDACT`.

OUTPUT: fully self-contained HTML — inline CSS, inline JS, no CDN, no external fetch. Opens
correctly via `open docs/architecture/core-system-architecture.html`, no server required. Legible
in both light and dark (prefers-color-scheme, plus a manual toggle for viewers whose OS setting
doesn't match their intent).

Usage:
    python3 bin/gen-architecture-doc.py [--roster path/to/roster.json] [--out path/to/out.html]
    (no --roster => runs bin/gen-arch-roster.py fresh against $CORE_INSTANCE / git toplevel / cwd)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAYER_ORDER = ["Sense", "Judge", "Learn", "Act", "Police"]
LAYER_BLURB = {
    "Sense": "Gathers context before the model acts — session state, recall, live inputs.",
    "Judge": "Surfaces signals that shape a decision without blocking it — routing, reminders, "
             "self-check prompts injected into context.",
    "Learn": "Records what happened, at zero behavioral cost — measurement and the "
             "self-improvement corpus this system reads back from.",
    "Act": "Writes — commits, syncs, closes a session, runs a scheduled job.",
    "Police": "Blocks — hard gates that stop an action outright when a constraint is violated. "
              "Cuts across the other four; it can intercept at any point in the loop.",
}
RUNG_LABEL = {"hook": "Hooks", "skill": "Skills", "command": "Commands", "agent": "Agents",
              "mcp": "MCP servers", "daemon": "Daemons"}

# Defensive redaction for upstream prose this script embeds verbatim. See docstring PRIVACY note.
_REDACT = [(re.compile(r"\bNick'?s?\b"), "the operator")]


def _scrub(text: str | None) -> str:
    if not text:
        return ""
    for pat, repl in _REDACT:
        text = pat.sub(repl, text)
    return text


def _e(text: str | None) -> str:
    return html.escape(_scrub(text or ""))


def _root() -> Path:
    r = os.environ.get("CORE_INSTANCE")
    if r:
        return Path(r)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except Exception:
        pass
    return Path.cwd()


def load_roster(roster_path: str | None, root: Path) -> dict:
    if roster_path:
        return json.loads(Path(roster_path).read_text())
    gen = HERE / "gen-arch-roster.py"
    out = subprocess.run([sys.executable, str(gen)], capture_output=True, text=True,
                          env={**os.environ, "CORE_INSTANCE": str(root)}, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gen-arch-roster.py failed: {out.stderr[-2000:]}")
    return json.loads(out.stdout)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def render_card(row: dict) -> str:
    retired = not row.get("reachable", True)
    cls = "cap-card retired" if retired else "cap-card"
    badge = '<span class="badge badge-retired">RETIRED</span>' if retired else ""
    reason = f'<div class="cap-reason">{_e(row.get("retired_reason"))}</div>' if retired and row.get("retired_reason") else ""
    meta_bits = [RUNG_LABEL.get(row["rung"], row["rung"])]
    if row.get("event"):
        meta_bits.append(row["event"])
    return f"""<div class="{cls}">
  <div class="cap-head"><span class="cap-name">{_e(row["name"])}</span>{badge}</div>
  <div class="cap-meta">{_e(" · ".join(meta_bits))}</div>
  <div class="cap-evidence">{_e(row.get("evidence"))}</div>
  <div class="cap-owner">{_e(row.get("owner_file"))}</div>
  {reason}
</div>"""


def render_layer_section(layer: str, rows: list[dict]) -> str:
    if not rows:
        body = '<p class="empty">No capabilities assigned to this layer.</p>'
    else:
        # group by rung; within "hook", sub-group by event (that's the architecture).
        by_rung: dict[str, list[dict]] = {}
        for r in rows:
            by_rung.setdefault(r["rung"], []).append(r)
        blocks = []
        for rung in ("hook", "skill", "command", "agent", "mcp", "daemon"):
            rung_rows = by_rung.get(rung)
            if not rung_rows:
                continue
            if rung == "hook":
                by_event: dict[str, list[dict]] = {}
                for r in rung_rows:
                    by_event.setdefault(r["event"] or "?", []).append(r)
                event_blocks = []
                for ev in sorted(by_event):
                    cards = "\n".join(render_card(r) for r in
                                       sorted(by_event[ev], key=lambda r: r["name"]))
                    event_blocks.append(
                        f'<div class="event-group"><h4>{_e(ev)}</h4>'
                        f'<div class="cap-grid">{cards}</div></div>')
                blocks.append(f'<div class="rung-group"><h3>{RUNG_LABEL[rung]}</h3>'
                               + "\n".join(event_blocks) + "</div>")
            else:
                cards = "\n".join(render_card(r) for r in sorted(rung_rows, key=lambda r: r["name"]))
                blocks.append(f'<div class="rung-group"><h3>{RUNG_LABEL[rung]}</h3>'
                               f'<div class="cap-grid">{cards}</div></div>')
        body = "\n".join(blocks)

    n = len(rows)
    n_retired = sum(1 for r in rows if not r.get("reachable", True))
    return f"""<section class="layer layer-{layer.lower()}" id="layer-{layer.lower()}">
  <div class="layer-head">
    <h2>{_e(layer)}</h2>
    <span class="layer-count">{n} capabilit{'y' if n == 1 else 'ies'}{f' · {n_retired} retired' if n_retired else ''}</span>
  </div>
  <p class="layer-blurb">{_e(LAYER_BLURB[layer])}</p>
  {body}
</section>"""


def render_flow_svg() -> str:
    """Sense -> Judge -> Learn -> Act in a line; Police drawn as a band beneath all four,
    with intercept arrows into each — it is not a fifth step, it is a cross-cutting one."""
    steps = ["Sense", "Judge", "Learn", "Act"]
    box_w, box_h, gap, x0, y0 = 150, 60, 60, 40, 20
    boxes, arrows = [], []
    for i, name in enumerate(steps):
        x = x0 + i * (box_w + gap)
        boxes.append(f'<a href="#layer-{name.lower()}"><rect x="{x}" y="{y0}" width="{box_w}" '
                      f'height="{box_h}" rx="10" class="flow-box flow-{name.lower()}"/>'
                      f'<text x="{x + box_w/2}" y="{y0 + box_h/2 + 5}" class="flow-label" '
                      f'text-anchor="middle">{name}</text></a>')
        if i < len(steps) - 1:
            ax = x + box_w
            arrows.append(f'<line x1="{ax}" y1="{y0 + box_h/2}" x2="{ax + gap}" '
                           f'y2="{y0 + box_h/2}" class="flow-arrow" marker-end="url(#arrow)"/>')
    total_w = x0 * 2 + len(steps) * box_w + (len(steps) - 1) * gap
    police_y = y0 + box_h + 50
    police_w = total_w - x0 * 2
    police = (f'<a href="#layer-police"><rect x="{x0}" y="{police_y}" width="{police_w}" '
              f'height="46" rx="10" class="flow-box flow-police"/>'
              f'<text x="{x0 + police_w/2}" y="{police_y + 29}" class="flow-label" '
              f'text-anchor="middle">Police — intercepts any of the above</text></a>')
    intercepts = []
    for i in range(len(steps)):
        x = x0 + i * (box_w + gap) + box_w / 2
        intercepts.append(f'<line x1="{x}" y1="{police_y}" x2="{x}" y2="{y0 + box_h}" '
                           f'class="flow-intercept"/>')
    total_h = police_y + 46 + 20
    return f"""<svg viewBox="0 0 {total_w} {total_h}" class="flow-diagram" role="img"
     aria-label="Sense feeds Judge feeds Learn feeds Act; Police can intercept at any stage">
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" class="flow-arrowhead"/>
    </marker>
  </defs>
  {''.join(intercepts)}
  {police}
  {''.join(arrows)}
  {''.join(boxes)}
</svg>"""


def render_si_summary(si: dict) -> str:
    if not si.get("available"):
        return ('<p class="empty">SI artifact / learned-contract counts unavailable on this run '
                '(Postgres unreachable or not configured) — see errors below.</p>')
    parts = []
    if si.get("si_artifacts") is not None:
        parts.append(f'<div class="stat"><div class="stat-n">{si["si_artifacts"]}</div>'
                     f'<div class="stat-l">SI artifacts<br><span class="stat-sub">'
                     f'{si.get("si_artifacts_active", "?")} active</span></div></div>')
    if si.get("learned_contracts") is not None:
        parts.append(f'<div class="stat"><div class="stat-n">{si["learned_contracts"]}</div>'
                     f'<div class="stat-l">Learned contracts<br><span class="stat-sub">'
                     f'{si.get("learned_contracts_active", "?")} active</span></div></div>')
    return f'<div class="stat-row">{"".join(parts)}</div>'


CSS = """
:root {
  --bg: #f7f7f5; --panel: #ffffff; --border: #e2e0da; --text: #1b1a17; --text-dim: #5b584f;
  --accent: #2f6f4f; --accent-2: #7a4fa3; --code-bg: #f0efe9;
  --sense: #2f6f9c; --judge: #7a5fa3; --learn: #2f6f4f; --act: #a3672f; --police: #a33f3f;
  --retired-bg: #efefec; --retired-text: #8c8a82;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #16181a; --panel: #1e2124; --border: #33373b; --text: #e8e6e1; --text-dim: #a7a49b;
    --accent: #6fbf94; --accent-2: #c7a6ec; --code-bg: #232629;
    --sense: #7fb8e0; --judge: #c7a6ec; --learn: #7fd6ab; --act: #e0a86f; --police: #e08a8a;
    --retired-bg: #232629; --retired-text: #74716a;
  }
}
:root[data-theme="dark"] {
  --bg: #16181a; --panel: #1e2124; --border: #33373b; --text: #e8e6e1; --text-dim: #a7a49b;
  --accent: #6fbf94; --accent-2: #c7a6ec; --code-bg: #232629;
  --sense: #7fb8e0; --judge: #c7a6ec; --learn: #7fd6ab; --act: #e0a86f; --police: #e08a8a;
  --retired-bg: #232629; --retired-text: #74716a;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont,
  "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; line-height: 1.5; }
.wrap { max-width: 980px; margin: 0 auto; padding: 32px 20px 80px; }
header.top { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px;
  margin-bottom: 8px; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
.subtitle { color: var(--text-dim); margin: 0 0 24px; font-size: 0.95rem; }
button#theme-toggle { background: var(--panel); border: 1px solid var(--border); color: var(--text);
  border-radius: 8px; padding: 6px 12px; cursor: pointer; font-size: 0.85rem; }
.overview { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px;
  margin: 20px 0 32px; }
.overview .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px; text-align: center; }
.overview .stat-n { font-size: 1.4rem; font-weight: 700; }
.overview .stat-l { font-size: 0.75rem; color: var(--text-dim); margin-top: 2px; }
.flow-diagram { width: 100%; height: auto; margin: 8px 0 32px; }
.flow-box { fill: var(--panel); stroke: var(--border); stroke-width: 1.5; }
.flow-sense { stroke: var(--sense); } .flow-judge { stroke: var(--judge); }
.flow-learn { stroke: var(--learn); } .flow-act { stroke: var(--act); }
.flow-police { stroke: var(--police); stroke-dasharray: 4 3; }
.flow-label { fill: var(--text); font-size: 15px; font-weight: 600; }
.flow-arrow { stroke: var(--text-dim); stroke-width: 2; }
.flow-arrowhead { fill: var(--text-dim); }
.flow-intercept { stroke: var(--police); stroke-width: 1.5; stroke-dasharray: 3 3; opacity: 0.6; }
a { color: var(--accent-2); }
section.layer { margin: 40px 0; padding-top: 8px; border-top: 3px solid var(--border); }
.layer-sense { border-top-color: var(--sense); } .layer-judge { border-top-color: var(--judge); }
.layer-learn { border-top-color: var(--learn); } .layer-act { border-top-color: var(--act); }
.layer-police { border-top-color: var(--police); }
.layer-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.layer-head h2 { margin: 12px 0 0; font-size: 1.3rem; }
.layer-count { color: var(--text-dim); font-size: 0.85rem; }
.layer-blurb { color: var(--text-dim); margin: 4px 0 20px; font-size: 0.92rem; }
.rung-group { margin: 18px 0; }
.rung-group h3 { font-size: 1rem; margin: 0 0 10px; color: var(--text-dim);
  text-transform: uppercase; letter-spacing: 0.04em; }
.event-group { margin: 0 0 16px; }
.event-group h4 { font-size: 0.85rem; margin: 0 0 8px; font-family: ui-monospace, SFMono-Regular,
  Menlo, monospace; color: var(--text-dim); }
.cap-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
.cap-card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 10px 12px; }
.cap-card.retired { background: var(--retired-bg); border-style: dashed; opacity: 0.85; }
.cap-card.retired .cap-name, .cap-card.retired .cap-evidence { color: var(--retired-text); }
.cap-head { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
.cap-name { font-weight: 600; font-size: 0.92rem; }
.badge { font-size: 0.65rem; font-weight: 700; letter-spacing: 0.03em; padding: 1px 6px;
  border-radius: 999px; }
.badge-retired { background: var(--police); color: #fff; }
.cap-meta { font-size: 0.72rem; color: var(--text-dim); margin-top: 2px; }
.cap-evidence { font-size: 0.82rem; margin-top: 6px; }
.cap-owner { font-size: 0.72rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--text-dim); margin-top: 6px; background: var(--code-bg); border-radius: 6px;
  padding: 2px 6px; display: inline-block; }
.cap-reason { font-size: 0.78rem; margin-top: 6px; color: var(--police); font-style: italic; }
.empty { color: var(--text-dim); font-style: italic; }
.stat-row { display: flex; gap: 16px; flex-wrap: wrap; }
.stat-row .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 20px; text-align: center; }
.stat-row .stat-n { font-size: 1.6rem; font-weight: 700; }
.stat-row .stat-l { font-size: 0.78rem; color: var(--text-dim); margin-top: 2px; }
.stat-row .stat-sub { font-size: 0.7rem; }
.errors { background: var(--retired-bg); border: 1px solid var(--police); border-radius: 10px;
  padding: 12px 16px; font-size: 0.85rem; margin-top: 12px; }
.errors li { margin: 4px 0; }
footer { margin-top: 60px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--text-dim); font-size: 0.8rem; }
"""

JS = """
(function(){
  var KEY = 'core-arch-theme';
  var root = document.documentElement;
  function apply(mode){ if(mode==='light'||mode==='dark'){ root.setAttribute('data-theme', mode); }
    else { root.removeAttribute('data-theme'); } }
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch(e) {}
  apply(saved);
  var btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', function(){
      var current = root.getAttribute('data-theme');
      var next = current === 'dark' ? 'light' : (current === 'light' ? null : 'dark');
      apply(next);
      try { if (next) { localStorage.setItem(KEY, next); } else { localStorage.removeItem(KEY); } }
      catch(e) {}
    });
  }
})();
"""


def build_html(roster: dict) -> str:
    rows = roster["rows"]
    by_layer = {L: [r for r in rows if r["layer"] == L] for L in LAYER_ORDER}
    counts = roster["counts"]
    generated = roster.get("generated_at", datetime.now(timezone.utc).isoformat())
    try:
        gen_date = datetime.fromisoformat(generated.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        gen_date = generated[:10]

    overview = "".join(
        f'<div class="stat"><div class="stat-n">{counts["by_layer"].get(L, 0)}</div>'
        f'<div class="stat-l">{L}</div></div>' for L in LAYER_ORDER)

    sections = "\n".join(render_layer_section(L, by_layer[L]) for L in LAYER_ORDER)

    errors_html = ""
    if roster.get("errors"):
        items = "".join(f"<li>{_e(err)}</li>" for err in roster["errors"])
        errors_html = f'<div class="errors"><strong>Generator errors this run:</strong><ul>{items}</ul></div>'

    seat = _e(roster.get("seat", "core"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Core — System Architecture</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>Core — System Architecture</h1>
      <p class="subtitle">Seat: {seat} · {counts["total"]} capabilities across five layers —
        Sense, Judge, Learn, Act, and Police cutting across all four.</p>
    </div>
    <button id="theme-toggle" type="button">Toggle theme</button>
  </header>

  <div class="overview">{overview}</div>

  {render_flow_svg()}

  {sections}

  <section class="layer" id="si-data">
    <div class="layer-head"><h2>SI artifacts &amp; learned contracts</h2></div>
    <p class="layer-blurb">Data the system reads back from, not architecture — one row per
      recurring correction, synthesized from session history. Shown as a count only.</p>
    {render_si_summary(roster.get("si_summary", {}))}
  </section>

  {errors_html}

  <footer>
    Generated from bin/gen-arch-roster.py on {_e(gen_date)}, {counts["total"]} capabilities
    ({counts["reachable"]} reachable, {counts["retired"]} retired). Regenerate with
    <code>python3 bin/gen-architecture-doc.py</code> — do not hand-edit this file.
  </footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--roster", help="path to a pre-generated roster JSON (default: run "
                                      "bin/gen-arch-roster.py fresh)")
    ap.add_argument("--out", default=None, help="output HTML path (default: "
                    "docs/architecture/core-system-architecture.html under the Core root)")
    args = ap.parse_args()

    root = _root()
    roster = load_roster(args.roster, root)
    out_path = Path(args.out) if args.out else root / "docs" / "architecture" / \
        "core-system-architecture.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = build_html(roster)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(doc)
    tmp.replace(out_path)
    print(f"[gen-architecture-doc] wrote {out_path} — "
          f"{roster['counts']['total']} capabilities "
          f"({roster['counts']['reachable']} reachable, {roster['counts']['retired']} retired)")


if __name__ == "__main__":
    main()
