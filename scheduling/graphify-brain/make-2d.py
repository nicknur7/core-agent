#!/usr/bin/env python3
"""
make-2d.py — emit graph-2d.html using Vasturiano's force-graph (Canvas 2D).

Replaces the SVG-based graph.html (which glitches at >5K nodes). Same payload
slimming + community auto-naming as make-3d.py. Sister build to graph-3d.html;  # privacy-ok: 'sister' script naming convention, not a family relation
2D toggle in 3D viz links here, and 3D toggle here links back.

Run after each merge.py:
  python3 scheduling/graphify-brain/make-2d.py
"""
import json
import os
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime

ROOT = Path(__file__).resolve().parent
# Per spec-graphify-out-relocation-2026-05-16.md — pipeline outputs in brain repo.
_BRAIN = os.environ.get("CORE_BRAIN")
if not _BRAIN:
    print(f"{Path(__file__).name}: $CORE_BRAIN not set — required.", file=sys.stderr)
    sys.exit(1)
OUT = Path(_BRAIN) / "_build" / "output" / "graphify-out"
OUT.mkdir(parents=True, exist_ok=True)
GRAPH_JSON = OUT / "graph.json"
HTML_OUT = OUT / "graph-2d.html"


def main():
    data = json.loads(GRAPH_JSON.read_text())
    nodes = data["nodes"]
    edges = data["edges"]
    lib_path = OUT / "vendor" / "force-graph.min.js"
    if not lib_path.exists():
        raise SystemExit(
            f"Missing {lib_path}. Run: curl -L -o {lib_path} "
            "https://unpkg.com/force-graph/dist/force-graph.min.js"
        )
    lib_inline = lib_path.read_text()

    deg = {}
    for e in edges:
        s = e["source"] if isinstance(e["source"], str) else e["source"].get("id")
        t = e["target"] if isinstance(e["target"], str) else e["target"].get("id")
        deg[s] = deg.get(s, 0) + 1
        deg[t] = deg.get(t, 0) + 1

    slim_nodes = []
    for n in nodes:
        props = n.get("properties", {}) or {}
        slim_nodes.append({
            "id": n["id"],
            "label": n.get("label") or n["id"],
            "type": n.get("type", "unknown"),
            "community": n.get("community", -1),
            "deg": deg.get(n["id"], 0),
            "summary": props.get("summary") or props.get("source_excerpt") or "",
            "date": props.get("date", ""),
            "file": props.get("file", ""),
        })

    slim_links = []
    for e in edges:
        s = e["source"] if isinstance(e["source"], str) else e["source"].get("id")
        t = e["target"] if isinstance(e["target"], str) else e["target"].get("id")
        slim_links.append({"source": s, "target": t, "rel": e.get("relation", "")})

    com_members = defaultdict(list)
    for n in slim_nodes:
        com_members[n["community"]].append(n)
    community_names = {}
    for com, members in com_members.items():
        if com < 0:
            continue
        top = max(members, key=lambda n: n["deg"])
        lbl = top["label"]
        if len(lbl) > 42:
            lbl = lbl[:40] + "…"
        community_names[str(com)] = lbl

    payload = {"nodes": slim_nodes, "links": slim_links}
    inline = json.dumps(payload, separators=(",", ":"))

    counts = {
        "nodes": len(slim_nodes),
        "links": len(slim_links),
        "communities": len({n["community"] for n in slim_nodes if n["community"] >= 0}),
        "generated": datetime.now().strftime("%Y-%m-%d"),
    }

    # Community-bucket regex map for the viz's communityBucket(name) function.
    # Pulled from identity.json (sister_projects.community_naming) so project-
    # specific buckets (school, career, etc.) don't leak into the engine
    # template. Engine ships with no buckets configured -> communityBucket()
    # falls through to type-based heuristics. See _load_community_buckets().
    buckets_json = _load_community_buckets_json()
    html = (HTML_TEMPLATE
        .replace("__LIB_INLINE__", lib_inline)
        .replace("__DATA__", inline)
        .replace("__COUNTS__", json.dumps(counts))
        .replace("__COMMUNITY_NAMES__", json.dumps(community_names, separators=(",", ":")))
        .replace("__COMMUNITY_BUCKETS__", buckets_json))
    HTML_OUT.write_text(html)
    size_mb = HTML_OUT.stat().st_size / 1024 / 1024
    print(f"wrote {HTML_OUT} ({size_mb:.1f} MB)")
    print(f"  nodes={counts['nodes']} links={counts['links']} communities={counts['communities']}")


def _load_community_buckets_json() -> str:
    """Read $CORE_INSTANCE/.claude/identity.json -> community_naming and
    emit a JS-compatible JSON array of {bucket, regex} pairs. Each entry's
    `regex` is the OR-join of the bucket's listed substrings, ready to be
    fed to `new RegExp(regex, 'i')` in the emitted JS.

    Returns "[]" if identity.json is missing, malformed, or has no
    community_naming key — meaning the viz falls back to type-based bucket
    assignment (engine-template-friendly).
    """
    instance = os.environ.get("CORE_INSTANCE")
    if not instance:
        return "[]"
    id_path = Path(instance) / ".claude" / "identity.json"
    try:
        with open(id_path) as f:
            data = json.load(f)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
        return "[]"
    naming = data.get("community_naming", {})
    out = []
    for bucket, patterns in naming.items():
        if bucket.startswith("_"):
            continue
        if not isinstance(patterns, list) or not patterns:
            continue
        regex = r"\b(" + "|".join(patterns) + r")\b"
        out.append({"bucket": bucket, "regex": regex})
    return json.dumps(out, separators=(",", ":"))


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Core Brain · 2D</title>
<style>
:root {
  --bg: #0b0d12;
  --bg-elevated: rgba(18, 22, 30, 0.78);
  --fg: #d6dae3;
  --fg-strong: #f3f5f9;
  --muted: #6c7585;
  --muted-strong: #98a0b0;
  --border: rgba(255, 255, 255, 0.08);
  --border-strong: rgba(255, 255, 255, 0.16);
  --accent: #7aa6ff;
  --accent-bg: rgba(122, 166, 255, 0.10);
  --shadow: 0 6px 28px rgba(0, 0, 0, 0.45);
}
html, body {
  margin: 0; padding: 0; height: 100%; overflow: hidden;
  background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", "Segoe UI", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}
body::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 1;
  background: radial-gradient(ellipse at 50% 45%, transparent 0%, transparent 35%, rgba(0,0,0,0.45) 100%);
}
#viz { position: absolute; inset: 0; }
.panel { position: absolute; z-index: 10; background: var(--bg-elevated); border: 1px solid var(--border); border-radius: 10px; backdrop-filter: blur(14px) saturate(140%); -webkit-backdrop-filter: blur(14px) saturate(140%); box-shadow: var(--shadow); }
.panel .section-label { font-size: 9.5px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; margin-bottom: 8px; }

#topbar { top: 0; left: 0; right: 0; display: flex; align-items: center; gap: 16px; padding: 10px 16px; border-radius: 0; border-left: none; border-right: none; border-top: none; background: linear-gradient(to bottom, rgba(11,13,18,0.92), rgba(11,13,18,0.65)); border-bottom: 1px solid var(--border); box-shadow: none; backdrop-filter: blur(20px); }
.wordmark { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 13px; color: var(--fg-strong); letter-spacing: -0.01em; }
.wordmark .dot { width: 10px; height: 10px; border-radius: 50%; background: radial-gradient(circle at 30% 30%, #a3c2ff, #4a7ed1 60%, #2a4a8a); box-shadow: 0 0 12px rgba(122, 166, 255, 0.5); }
.wordmark .sep { color: var(--muted); margin: 0 2px; }
.wordmark .mode { color: var(--accent); font-weight: 500; font-size: 12px; }
#topbar .spacer { flex: 1; }
#search-box { background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--fg); padding: 6px 12px; border-radius: 7px; width: 280px; outline: none; font-size: 12px; font-family: inherit; transition: border-color 0.15s, background 0.15s; }
#search-box::placeholder { color: var(--muted); }
#search-box:focus { border-color: var(--accent); background: rgba(255,255,255,0.06); }
#mode-toggle { display: flex; gap: 2px; background: rgba(255,255,255,0.04); border: 1px solid var(--border); border-radius: 7px; padding: 2px; }
#mode-toggle a { padding: 5px 14px; border-radius: 5px; font-size: 11.5px; font-weight: 500; color: var(--muted-strong); text-decoration: none; cursor: pointer; transition: color 0.12s, background 0.12s; }
#mode-toggle a:hover { color: var(--fg); }
#mode-toggle a.active { background: var(--accent-bg); color: var(--accent); }

#info { top: 64px; right: 14px; width: 340px; max-height: calc(100vh - 130px); overflow-y: auto; padding: 14px 16px; font-size: 12px; display: none; }
#info.pinned { border-color: var(--border-strong); display: block; }
#info h3 { margin: 0 0 4px 0; font-size: 14.5px; font-weight: 600; color: var(--fg-strong); word-break: break-word; line-height: 1.3; }
#info .id { color: var(--muted); font-size: 10px; margin-bottom: 10px; word-break: break-all; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
#info .badges { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
#info .badge { background: rgba(255,255,255,0.05); border: 1px solid var(--border); padding: 3px 9px; border-radius: 999px; font-size: 10px; color: var(--muted-strong); font-weight: 500; }
#info .badge.com { background: var(--accent-bg); color: var(--accent); border-color: rgba(122,166,255,0.25); }
#info .summary { color: var(--fg); font-size: 11.5px; line-height: 1.5; margin-bottom: 12px; max-height: 90px; overflow-y: auto; padding-right: 4px; }
#info .meta { color: var(--muted-strong); font-size: 11px; margin-bottom: 12px; line-height: 1.6; }
#info .meta strong { color: var(--fg); font-weight: 500; }
#info .neighbors .section-label { margin-bottom: 6px; }
#info .neighbors a { display: block; color: var(--muted-strong); font-size: 11.5px; padding: 4px 0; cursor: pointer; text-decoration: none; border-bottom: 1px solid rgba(255,255,255,0.04); }
#info .neighbors a:last-child { border-bottom: none; }
#info .neighbors a:hover { color: var(--accent); }
#info .pin-hint { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); font-size: 10px; color: var(--muted); }

#legend { top: 64px; left: 14px; width: 230px; padding: 12px 14px; font-size: 11.5px; max-height: 44vh; overflow-y: auto; }
#legend .cat-header { font-size: 9.5px; color: var(--accent); text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; margin: 10px 2px 4px; padding-bottom: 3px; border-bottom: 1px solid var(--border); display: flex; align-items: baseline; gap: 6px; }
#legend .cat-header:first-child { margin-top: 0; }
#legend .cat-count { color: var(--muted); font-weight: 500; font-size: 9.5px; margin-left: auto; font-variant-numeric: tabular-nums; }
#legend .row { display: flex; align-items: center; gap: 9px; padding: 4px 4px; border-radius: 5px; cursor: pointer; transition: background 0.1s; }
#legend .row:hover { background: rgba(255,255,255,0.04); }
#legend .swatch { width: 10px; height: 10px; border-radius: 50%; flex: none; box-shadow: 0 0 6px currentColor; }
#legend .name { flex: 1; font-size: 11.5px; color: var(--fg); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#legend .count { color: var(--muted); font-size: 10.5px; font-variant-numeric: tabular-nums; }

#bottombar { bottom: 0; left: 0; right: 0; display: flex; align-items: center; gap: 16px; padding: 8px 16px; border-radius: 0; border-top: 1px solid var(--border); border-left: none; border-right: none; border-bottom: none; background: linear-gradient(to top, rgba(11,13,18,0.92), rgba(11,13,18,0.55)); box-shadow: none; backdrop-filter: blur(20px); }
#stats { font-size: 11px; color: var(--muted-strong); display: flex; gap: 14px; align-items: center; font-variant-numeric: tabular-nums; }
#stats .stat-num { color: var(--fg-strong); font-weight: 500; }
#stats .stat-sep { color: var(--border-strong); }
#stats .stat-stamp { color: var(--muted); margin-left: 2px; }
#bottombar .spacer { flex: 1; }
#help-text { font-size: 10.5px; color: var(--muted); }
#help-text kbd { background: rgba(255,255,255,0.05); border: 1px solid var(--border); border-radius: 3px; padding: 1px 5px; font-size: 10px; font-family: ui-monospace, "SF Mono", monospace; color: var(--muted-strong); }
#settings-toggle { display: flex; align-items: center; gap: 6px; padding: 6px 12px; cursor: pointer; user-select: none; font-size: 11.5px; font-weight: 500; color: var(--fg); background: rgba(255,255,255,0.04); border: 1px solid var(--border); border-radius: 7px; transition: color 0.12s, border-color 0.12s, background 0.12s; }
#settings-toggle:hover { color: var(--accent); border-color: rgba(122,166,255,0.3); background: var(--accent-bg); }
#settings-toggle.active { color: var(--accent); border-color: rgba(122,166,255,0.3); background: var(--accent-bg); }

#settings { bottom: 56px; right: 14px; width: 280px; padding: 14px 16px; font-size: 11.5px; max-height: calc(100vh - 110px); overflow-y: auto; display: none; }
#settings.open { display: block; }
#settings .section-label { margin: 12px 0 6px 0; }
#settings .section-label:first-child { margin-top: 0; }
#settings .row { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
#settings label { flex: 1; color: var(--fg); font-size: 11.5px; }
#settings input[type=range] { flex: 1.4; accent-color: var(--accent); height: 4px; }
#settings input[type=color] { width: 28px; height: 22px; border: 1px solid var(--border); background: transparent; padding: 0; cursor: pointer; border-radius: 4px; }
#settings input[type=checkbox] { accent-color: var(--accent); width: 14px; height: 14px; }
#settings select { flex: 1.4; background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--fg); padding: 4px 8px; border-radius: 5px; font-size: 11.5px; outline: none; font-family: inherit; }
#settings select:focus { border-color: var(--accent); }
#settings .val { color: var(--muted-strong); font-size: 10.5px; min-width: 38px; text-align: right; font-variant-numeric: tabular-nums; }
#settings .reset { margin-top: 12px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; color: var(--muted-strong); cursor: pointer; font-size: 10.5px; text-align: center; transition: color 0.12s, border-color 0.12s; }
#settings .reset:hover { color: var(--accent); border-color: rgba(122,166,255,0.3); }

#loading, #errbox { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; padding: 12px 18px; border-radius: 8px; z-index: 99; }
#loading { color: var(--muted); }
#errbox { color: #ff7b7b; background: rgba(20,10,12,0.92); border: 1px solid #ff7b7b; max-width: 600px; display: none; }
</style>
</head>
<body>
<div id="viz"></div>

<header id="topbar" class="panel">
  <div class="wordmark">
    <span class="dot"></span>
    <span>Core Brain</span>
    <span class="sep">·</span>
    <span class="mode">2D view</span>
  </div>
  <div class="spacer"></div>
  <input id="search-box" placeholder="search a node…" />
  <div class="spacer"></div>
  <nav id="mode-toggle">
    <a href="graph-3d.html">3D</a>
    <a class="active">2D</a>
  </nav>
</header>

<aside id="legend" class="panel">
  <div class="section-label">communities · grouped by topic</div>
  <div id="legend-body"></div>
</aside>

<section id="info" class="panel"></section>

<footer id="bottombar" class="panel">
  <div id="stats"></div>
  <div class="spacer"></div>
  <div id="help-text"><kbd>scroll</kbd> zoom · <kbd>drag</kbd> pan · <kbd>click</kbd> pin node</div>
  <div class="spacer"></div>
  <div id="settings-toggle">⚙ tune</div>
</footer>

<div id="settings" class="panel">
  <div class="section-label">links</div>
  <div class="row"><label>color</label><input type="color" id="s-link-color" value="#7e8c9a"><span class="val" id="s-link-color-v">#7e8c9a</span></div>
  <div class="row"><label>opacity</label><input type="range" id="s-link-opacity" min="0" max="1" step="0.02" value="0.22"><span class="val" id="s-link-opacity-v">0.22</span></div>
  <div class="row"><label>width</label><input type="range" id="s-link-width" min="0.1" max="3" step="0.05" value="0.6"><span class="val" id="s-link-width-v">0.6</span></div>
  <div class="row"><label>particles</label><input type="checkbox" id="s-link-particles"><span class="val" id="s-link-particles-v">off</span></div>

  <div class="section-label">nodes</div>
  <div class="row"><label>show isolated</label><input type="checkbox" id="s-show-isolated"><span class="val" id="s-show-isolated-v">off</span></div>
  <div class="row"><label>size scale</label><input type="range" id="s-node-size" min="0.4" max="4" step="0.05" value="1"><span class="val" id="s-node-size-v">1.0×</span></div>
  <div class="row"><label>min size</label><input type="range" id="s-node-min" min="0.4" max="6" step="0.05" value="1.5"><span class="val" id="s-node-min-v">1.5</span></div>
  <div class="row"><label>max size</label><input type="range" id="s-node-max" min="3" max="60" step="0.5" value="22"><span class="val" id="s-node-max-v">22</span></div>
  <div class="row"><label>size curve</label>
    <select id="s-size-curve">
      <option value="sqrt">sqrt · default</option>
      <option value="linear">linear · strong</option>
      <option value="cbrt">cbrt · softer</option>
      <option value="log">log · softest</option>
      <option value="flat">flat · uniform</option>
    </select>
  </div>
  <div class="row"><label>show labels</label>
    <select id="s-labels">
      <option value="hubs">hubs only · default</option>
      <option value="zoomed">when zoomed in</option>
      <option value="never">never</option>
      <option value="always">always (slow)</option>
    </select>
  </div>

  <div class="section-label">layout</div>
  <div class="row"><label>repel</label><input type="range" id="s-charge" min="-150" max="-5" step="1" value="-30"><span class="val" id="s-charge-v">-30</span></div>
  <div class="row"><label>link dist</label><input type="range" id="s-link-dist" min="6" max="120" step="1" value="28"><span class="val" id="s-link-dist-v">28</span></div>
  <div class="row"><label>center force</label><input type="range" id="s-center" min="0" max="0.4" step="0.005" value="0.05"><span class="val" id="s-center-v">0.05</span></div>

  <div class="section-label">scene</div>
  <div class="row"><label>background</label><input type="color" id="s-bg" value="#0b0d12"><span class="val" id="s-bg-v">#0b0d12</span></div>

  <div class="reset" id="s-reset">reset to defaults</div>
</div>

<div id="loading">loading…</div>
<div id="errbox"></div>

<script>__LIB_INLINE__</script>
<script>
window.addEventListener("error", e => {
  const box = document.getElementById("errbox");
  if (box) { box.style.display = "block"; box.textContent = "JS error: " + (e.message || e) + (e.filename ? "  @ " + e.filename + ":" + e.lineno : ""); }
  const l = document.getElementById("loading"); if (l) l.style.display = "none";
});
if (typeof ForceGraph === "undefined") {
  document.getElementById("errbox").style.display = "block";
  document.getElementById("errbox").textContent = "ForceGraph not defined — vendored lib failed to load. Re-run make-2d.py.";
} else {
document.getElementById("loading").style.display = "none";

const DATA = __DATA__;
const COUNTS = __COUNTS__;
const COMMUNITY_NAMES = __COMMUNITY_NAMES__;
function communityName(c) { return (c == null || c < 0) ? "uncategorized" : (COMMUNITY_NAMES[c] || `community ${c}`); }

const CATEGORY_ORDER = ["school", "people", "career", "projects", "ai", "tools", "sessions", "other"];
const CATEGORY_LABELS = {
  school: "school & coursework",
  people: "people",
  career: "career & job hunt",
  projects: "projects",
  ai: "ai & claude tooling",
  tools: "tools & code",
  sessions: "session arcs",
  other: "other",
};
const COM_HUB = new Map();
(function _initComHub() {
  for (const n of DATA.nodes) {
    if (n.community < 0) continue;
    const cur = COM_HUB.get(n.community);
    if (!cur || n.deg > cur.deg) COM_HUB.set(n.community, n);
  }
})();
function categorize(c) {
  const name = (communityName(c) || "").toLowerCase();
  const hub = COM_HUB.get(c);
  const t = hub ? hub.type : "";
  // Community buckets are configured per-instance via identity.json
  // community_naming. Engine template emits [] -> this loop is a no-op and
  // we fall through to the type-based assignment below.
  for (const {bucket, regex} of __COMMUNITY_BUCKETS__) {
    try { if (new RegExp(regex, "i").test(name)) return bucket; } catch (e) {}
  }
  if (t === "tool" || t === "code_location" || t === "framework" || t === "language" || t === "technology" || t === "service") return "tools";
  if (t === "entity" || t === "person" || t === "company" || t === "organization") return "people";
  if (t === "project" || t === "external_project") return "projects";
  if (t === "session" || t === "subagent_session" || t === "session_arc") return "sessions";
  return "other";
}

function communityColor(c) {
  if (c == null || c < 0) return "#6e7681";
  const h = (c * 137.508) % 360;
  const s = 55 + (c * 7) % 25;
  const l = 58 + (c * 13) % 12;
  return `hsl(${h.toFixed(0)} ${s.toFixed(0)}% ${l.toFixed(0)}%)`;
}

// Top-30 hubs by degree — used as the default "show labels for hubs" set.
const HUB_IDS = new Set(
  [...DATA.nodes].sort((a, b) => b.deg - a.deg).slice(0, 30).map(n => n.id)
);

const adj = new Map();
DATA.links.forEach(e => {
  if (!adj.has(e.source)) adj.set(e.source, new Set());
  if (!adj.has(e.target)) adj.set(e.target, new Set());
  adj.get(e.source).add(e.target);
  adj.get(e.target).add(e.source);
});
const idMap = new Map(DATA.nodes.map(n => [n.id, n]));

// Hide isolated nodes (deg=0) by default — at 11K-node scale they're visual
// noise and add ~hundreds of off-cluster orphans that drag every render frame.
// Toggle in settings panel restores them.
let showIsolated = false;
const isIsolated = n => (n.deg || 0) === 0;

const Graph = ForceGraph()(document.getElementById("viz"))
  .graphData(DATA)
  .backgroundColor("#0b0d12")
  .nodeRelSize(3.5)
  .nodeColor(n => communityColor(n.community))
  .nodeVal(n => Math.max(1, Math.sqrt(n.deg)))
  .nodeVisibility(n => showIsolated || !isIsolated(n))
  .linkColor(() => "rgba(120,140,160,0.22)")
  .linkWidth(0.6)
  .linkDirectionalParticles(0)
  .warmupTicks(80)
  .cooldownTicks(90)
  .cooldownTime(6000)
  .minZoom(0.1)
  .maxZoom(8)
  .d3VelocityDecay(0.55)
  .d3AlphaDecay(0.04)
  .d3AlphaMin(0.005)
  .onZoom(() => { if (!Graph.__zoomTimer) Graph.pauseAnimation(); clearTimeout(Graph.__zoomTimer); Graph.__zoomTimer = setTimeout(() => { Graph.__zoomTimer = null; Graph.resumeAnimation(); }, 180); })
  .nodeLabel(n => `${n.label}\n${n.type} · ${communityName(n.community)} · deg ${n.deg}`)
  .onNodeClick(n => {
    pinNode(n);
    Graph.centerAt(n.x, n.y, 800);
    Graph.zoom(Math.max(Graph.zoom(), 1.8), 800);
  })
  .onNodeHover(n => {
    hoveredId = n ? n.id : null;
    document.getElementById("viz").style.cursor = n ? "pointer" : null;
  })
  .onBackgroundClick(() => clearPin())
  .onEngineStop(() => {
    if (!fitDone) {
      try { Graph.zoomToFit(800, 60); } catch (e) {}
      fitDone = true;
    }
  });
let fitDone = false;

// Custom canvas drawing — node fade + optional label.
Graph.nodeCanvasObject((node, ctx, globalScale) => {
  const r = nodeValFn(node);
  // Determine visible color (apply hover-fade like Obsidian).
  let color;
  if (hoveredId && !isNodeHighlighted(node.id)) color = "#262b33";
  else color = communityColor(node.community);

  ctx.beginPath();
  ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
  ctx.fillStyle = color;
  ctx.fill();

  // Label rules.
  const showLbl = labelMode(node, globalScale);
  if (showLbl) {
    const fontSize = Math.max(8, 10 / globalScale);
    ctx.font = `${fontSize}px -apple-system, sans-serif`;
    ctx.fillStyle = (hoveredId && !isNodeHighlighted(node.id)) ? "rgba(150,160,180,0.35)" : "#d6dae3";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(node.label, node.x + r + 2, node.y);
  }
}).nodePointerAreaPaint((node, color, ctx) => {
  // Pointer hit area — same circle.
  const r = nodeValFn(node) + 3;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
  ctx.fill();
}).linkColor(link => {
  const base = hexToRgba(settings.linkColor, settings.linkOpacity);
  if (hoveredId && !isLinkHighlighted(link)) return hexToRgba(settings.linkColor, settings.linkOpacity * 0.15);
  return base;
});

// Custom 2D center-attract force — Obsidian-style pull toward (0,0).
function makeCenterAttract2D(strength) {
  let nodes = [];
  let s = strength;
  function force(alpha) {
    const k = s * alpha;
    if (k === 0) return;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      n.vx -= (n.x || 0) * k;
      n.vy -= (n.y || 0) * k;
    }
  }
  force.initialize = n => { nodes = n; };
  force.strength = v => { if (v === undefined) return s; s = +v; return force; };
  return force;
}

Graph.d3Force("charge").strength(-30);
Graph.d3Force("link").distance(28);
Graph.d3Force("centerAttract", makeCenterAttract2D(0.05));

let hoveredId = null;
function isNodeHighlighted(id) {
  if (!hoveredId) return true;
  if (id === hoveredId) return true;
  return (adj.get(hoveredId) || new Set()).has(id);
}
function isLinkHighlighted(link) {
  if (!hoveredId) return true;
  const s = typeof link.source === "object" ? link.source.id : link.source;
  const t = typeof link.target === "object" ? link.target.id : link.target;
  return s === hoveredId || t === hoveredId;
}

// Stats.
document.getElementById("stats").innerHTML =
  `<span><span class="stat-num">${COUNTS.nodes.toLocaleString()}</span> nodes</span>` +
  `<span class="stat-sep">·</span>` +
  `<span><span class="stat-num">${COUNTS.links.toLocaleString()}</span> edges</span>` +
  `<span class="stat-sep">·</span>` +
  `<span><span class="stat-num">${COUNTS.communities}</span> communities</span>` +
  `<span class="stat-stamp">— generated ${COUNTS.generated}</span>`;

// Legend — communities grouped by category, top by size within each.
function buildLegend() {
  const sizeByCom = new Map();
  DATA.nodes.forEach(n => {
    if (n.community < 0) return;
    sizeByCom.set(n.community, (sizeByCom.get(n.community) || 0) + 1);
  });
  const byCat = {};
  for (const [c, n] of sizeByCom) {
    const cat = categorize(c);
    (byCat[cat] = byCat[cat] || []).push([c, n]);
  }
  Object.values(byCat).forEach(arr => arr.sort((a, b) => b[1] - a[1]));

  const body = document.getElementById("legend-body");
  let html = "";
  for (const cat of CATEGORY_ORDER) {
    const arr = byCat[cat] || [];
    if (!arr.length) continue;
    const totalSize = arr.reduce((s, [, n]) => s + n, 0);
    html += `<div class="cat-header">${CATEGORY_LABELS[cat]}<span class="cat-count">${arr.length} · ${totalSize.toLocaleString()}</span></div>`;
    for (const [c, n] of arr.slice(0, 6)) {
      html += `<div class="row" data-com="${c}" title="${escapeAttr(communityName(c))}">
        <div class="swatch" style="background:${communityColor(c)};color:${communityColor(c)}"></div>
        <div class="name">${escapeHtml(communityName(c))}</div>
        <div class="count">${n}</div>
      </div>`;
    }
  }
  body.innerHTML = html;
  body.querySelectorAll(".row").forEach(row => {
    row.onclick = () => focusCommunity(parseInt(row.dataset.com, 10));
  });
}
buildLegend();

function focusCommunity(c) {
  const members = DATA.nodes.filter(n => n.community === c && n.x != null);
  if (!members.length) return;
  const cx = members.reduce((s, n) => s + n.x, 0) / members.length;
  const cy = members.reduce((s, n) => s + n.y, 0) / members.length;
  Graph.centerAt(cx, cy, 800);
  Graph.zoom(2, 800);
}

// Info panel.
function pinNode(n) {
  const info = document.getElementById("info");
  info.classList.add("pinned");
  const neighbors = [...(adj.get(n.id) || [])].map(id => idMap.get(id)).filter(Boolean).slice(0, 12);
  const summary = (n.summary || "").slice(0, 240);
  const meta = [
    n.date ? `<strong>date</strong> ${escapeHtml(n.date)}` : "",
    n.file ? `<strong>file</strong> <span style="color:#8b949e;font-size:10px">${escapeHtml(n.file)}</span>` : "",
  ].filter(Boolean).join(" · ");
  info.innerHTML = `
    <h3>${escapeHtml(n.label)}</h3>
    <div class="id">${escapeHtml(n.id)}</div>
    <div class="badges">
      <span class="badge">${escapeHtml(n.type)}</span>
      <span class="badge com">${escapeHtml(communityName(n.community))}</span>
      <span class="badge">deg ${n.deg}</span>
    </div>
    ${summary ? `<div class="summary">${escapeHtml(summary)}${n.summary.length > 240 ? "…" : ""}</div>` : ""}
    ${meta ? `<div class="meta">${meta}</div>` : ""}
    <div class="neighbors">
      <div class="section-label">neighbors (${neighbors.length}${(adj.get(n.id) || new Set()).size > 12 ? ` of ${adj.get(n.id).size}` : ""})</div>
      ${neighbors.map(nb => `<a data-id="${escapeAttr(nb.id)}">${escapeHtml(nb.label)} <span style="color:#586069;font-size:10px">· ${escapeHtml(nb.type)}</span></a>`).join("")}
    </div>
    <div class="pin-hint">click background to clear · click neighbor to jump</div>`;
  info.querySelectorAll("a[data-id]").forEach(a => {
    a.onclick = () => {
      const target = idMap.get(a.dataset.id);
      if (target) { pinNode(target); if (target.x != null) { Graph.centerAt(target.x, target.y, 800); Graph.zoom(Math.max(Graph.zoom(), 2), 800); } }
    };
  });
}
function clearPin() { document.getElementById("info").classList.remove("pinned"); }

// Search.
document.getElementById("search-box").addEventListener("input", e => {
  const q = e.target.value.trim().toLowerCase();
  if (!q) return;
  const hit = DATA.nodes.find(n => (n.label || "").toLowerCase().includes(q));
  if (hit) {
    pinNode(hit);
    if (hit.x != null) { Graph.centerAt(hit.x, hit.y, 800); Graph.zoom(2.5, 800); }
  }
});

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function escapeAttr(s) { return String(s).replace(/"/g, "&quot;"); }

// Settings.
const DEFAULTS = {
  linkColor: "#7e8c9a", linkOpacity: 0.22, linkWidth: 0.6, linkParticles: false,
  nodeSize: 1.0, nodeMin: 1.5, nodeMax: 22, sizeCurve: "sqrt",
  labelMode: "never",
  charge: -30, linkDist: 28, center: 0.05,
  bg: "#0b0d12",
};
const SS_KEY = "brain-graph-2d-settings";
let settings = (() => {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(SS_KEY) || "{}") }; }
  catch { return { ...DEFAULTS }; }
})();
function saveSettings() { try { localStorage.setItem(SS_KEY, JSON.stringify(settings)); } catch {} }

function hexToRgba(hex, a) {
  const m = hex.replace("#","").match(/.{2}/g);
  if (!m || m.length < 3) return `rgba(126,140,154,${a})`;
  const [r,g,b] = m.map(x => parseInt(x, 16));
  return `rgba(${r},${g},${b},${a})`;
}

function curveFn(d) {
  const x = Math.max(1, d);
  switch (settings.sizeCurve) {
    case "linear": return x;
    case "cbrt":   return Math.cbrt(x);
    case "log":    return Math.log(x + 1);
    case "flat":   return 1;
    default:       return Math.sqrt(x);
  }
}
function nodeValFn(n) {
  const raw = curveFn(n.deg) * settings.nodeSize;
  return Math.max(settings.nodeMin, Math.min(settings.nodeMax, raw));
}
function labelMode(node, globalScale) {
  if (hoveredId === node.id) return true;
  switch (settings.labelMode) {
    case "always": return true;
    case "never":  return false;
    case "zoomed": return globalScale > 1.8;
    default:       return HUB_IDS.has(node.id);  // hubs: top-30 by degree
  }
}

function applySettings(opts) {
  opts = opts || {};
  Graph
    .linkWidth(settings.linkWidth)
    .linkDirectionalParticles(settings.linkParticles ? 2 : 0)
    .nodeVal(nodeValFn)
    .backgroundColor(settings.bg);
  if (opts.forces) {
    try {
      const charge = Graph.d3Force("charge"); if (charge) charge.strength(settings.charge);
      const link = Graph.d3Force("link"); if (link) link.distance(settings.linkDist);
      const ca = Graph.d3Force("centerAttract"); if (ca && ca.strength) ca.strength(settings.center);
      Graph.d3ReheatSimulation && Graph.d3ReheatSimulation();
    } catch (e) { console.warn("force tweak failed:", e); }
  }
}

function bindSetting(id, key, parseVal, fmt) {
  const el = document.getElementById(id);
  const valEl = document.getElementById(id + "-v");
  el.value = settings[key];
  if (el.type === "checkbox") el.checked = !!settings[key];
  if (valEl) valEl.textContent = fmt(settings[key]);
  el.addEventListener("input", () => {
    settings[key] = el.type === "checkbox" ? el.checked : parseVal(el.value);
    if (valEl) valEl.textContent = fmt(settings[key]);
    const isForceKey = key === "charge" || key === "linkDist" || key === "center";
    applySettings({ forces: isForceKey });
    saveSettings();
  });
}

bindSetting("s-link-color",     "linkColor",     v => v,            v => v);
bindSetting("s-link-opacity",   "linkOpacity",   parseFloat,        v => v.toFixed(2));
bindSetting("s-link-width",     "linkWidth",     parseFloat,        v => v.toFixed(2));
bindSetting("s-link-particles", "linkParticles", v => v,            v => v ? "on" : "off");
// Isolated-node toggle: wired to the showIsolated flag (not into settings dict)
// so a single nodeVisibility re-eval fires immediately on toggle.
(function() {
  const el = document.getElementById("s-show-isolated");
  const val = document.getElementById("s-show-isolated-v");
  if (!el) return;
  el.checked = false;
  el.addEventListener("change", () => {
    showIsolated = el.checked;
    val.textContent = el.checked ? "on" : "off";
    // Force re-eval of nodeVisibility — no public API, so toggle a no-op setter.
    Graph.nodeVisibility(n => showIsolated || !isIsolated(n));
  });
})();
bindSetting("s-node-size",      "nodeSize",      parseFloat,        v => v.toFixed(2) + "×");
bindSetting("s-node-min",       "nodeMin",       parseFloat,        v => v.toFixed(2));
bindSetting("s-node-max",       "nodeMax",       parseFloat,        v => v.toFixed(0));
bindSetting("s-size-curve",     "sizeCurve",     v => v,            v => v);
bindSetting("s-labels",         "labelMode",     v => v,            v => v);
bindSetting("s-charge",         "charge",        parseFloat,        v => v.toFixed(0));
bindSetting("s-link-dist",      "linkDist",      parseFloat,        v => v.toFixed(0));
bindSetting("s-center",         "center",        parseFloat,        v => v.toFixed(3));
bindSetting("s-bg",             "bg",            v => v,            v => v);

document.getElementById("settings-toggle").addEventListener("click", () => {
  const opened = document.getElementById("settings").classList.toggle("open");
  document.getElementById("settings-toggle").classList.toggle("active", opened);
});
document.getElementById("s-reset").addEventListener("click", () => {
  settings = { ...DEFAULTS };
  saveSettings();
  Object.entries(settings).forEach(([k, v]) => {
    const id = "s-" + k.replace(/([A-Z])/g, "-$1").toLowerCase();
    const el = document.getElementById(id);
    const valEl = document.getElementById(id + "-v");
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    if (valEl) {
      if (typeof v === "boolean") valEl.textContent = v ? "on" : "off";
      else if (typeof v === "number") valEl.textContent = (k === "nodeSize" ? v.toFixed(2) + "×" : (k === "charge" || k === "linkDist") ? v.toFixed(0) : v.toFixed(2));
      else valEl.textContent = v;
    }
  });
  applySettings({ forces: true });
});

// Initial: defer apply until renderer mounted.
setTimeout(() => {
  try { applySettings({ forces: false }); } catch (e) { console.warn(e); }
  setTimeout(() => { try { applySettings({ forces: true }); } catch (e) { console.warn(e); } }, 400);
}, 100);
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
