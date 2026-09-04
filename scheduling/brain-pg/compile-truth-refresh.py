#!/usr/bin/env python3
"""Compile-truth refresh — detect drifted hubs + re-synthesize only those.

Step 6 (stop-hook incremental embed) keeps the vector index current but does NOT
re-synthesize entities.compiled_truth_md. Over weeks of new sessions, hub
summaries drift from the underlying evidence. This script detects drift and
re-runs compile-truth selectively (cheap) instead of full-corpus (expensive).

Drift triggers (ANY of):
  - last_compiled_at older than AGE_DAYS (default 14)
  - evidence row count grew by ≥GROWTH_THRESHOLD (default 20%) since last_compiled_at
  - entity name appears in the manual force-list at $CORE_INSTANCE/tasks/compile-truth-refresh/force-list.json

Phases:
  --detect           Run drift detection, write report to compile-truth-work/drift-report-<date>.json
  --partition        Partition drifted hubs into N batches for subagent fan-out (reuses compile-truth.py)
  --ingest           Bulk-update entities.compiled_truth_md from batch outputs + bump last_compiled_at
  --status           Show current detector state + last refresh log

Usage:
  python3 compile-truth-refresh.py --detect
  python3 compile-truth-refresh.py --partition --batches 14
  python3 compile-truth-refresh.py --ingest

Companion: scheduling/brain-pg/compile-truth.py (the original one-shot pass).
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import connect_corebrain, connect_or_skip, get_org_id  # noqa: E402

_BRAIN_ENV = os.environ.get("CORE_BRAIN")
if not _BRAIN_ENV or not Path(_BRAIN_ENV).is_dir():
    # Named skip, exit 0 — the close-path convention (_env.connect_or_skip). A seat with no brain
    # vault (every fresh clone until setup-brain.sh runs) has no hubs to drift; exiting 1 here
    # killed the close chain at step 7.6 with an error a stranger cannot act on mid-close.
    print(f"COMPILE-TRUTH: skipped (no brain vault at $CORE_BRAIN={_BRAIN_ENV or 'unset'})")
    sys.exit(0)
BRAIN_ROOT = Path(_BRAIN_ENV)
_INSTANCE = Path(os.environ.get("CORE_INSTANCE", Path(__file__).resolve().parents[2]))
WORK_DIR = _INSTANCE / "scheduling" / "brain-pg" / "compile-truth-work"
FORCE_LIST = _INSTANCE / "tasks" / "compile-truth-refresh" / "force-list.json"

# Tuning constants — adjust as we learn what threshold actually fires meaningfully.
AGE_DAYS_THRESHOLD = 7          # tightened 2026-05-28 from 14 — auto-refresh wired into stop-hook, catch drift sooner
GROWTH_THRESHOLD = 0.10         # tightened 2026-05-28 from 0.20 — same reason
MIN_NEW_ROWS = 3                # don't flag if fewer than this many new rows (noise floor)

HUB_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def connect():
    return connect_corebrain()


def parse_hub_source_files(hub_path: Path) -> List[str]:
    """Pull session/subagent file refs out of a hub markdown body."""
    try:
        text = hub_path.read_text(errors="replace")
    except OSError:
        return []
    m = HUB_FRONTMATTER_RE.match(text)
    body = m.group(2) if m else text
    refs = re.findall(r"projects/[\w\-./]+\.md", body)
    return [str(BRAIN_ROOT / r) for r in refs]


def discover_hubs() -> List[Path]:
    return sorted((BRAIN_ROOT / "entities").glob("*.md")) + sorted((BRAIN_ROOT / "topics").glob("*.md"))


def load_force_list() -> set:
    if not FORCE_LIST.exists():
        return set()
    try:
        names = json.loads(FORCE_LIST.read_text())
        if isinstance(names, list):
            return {n.strip() for n in names if isinstance(n, str)}
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def detect_drift(verbose: bool = True, min_degree: int = 0) -> Dict:
    """Walk all hubs, compute drift score per entity, return report dict."""
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    force_set = load_force_list()

    # NEVER-COMPILED HUBS ARE THE MOST DRIFTED THING THERE IS (2026-08-28).
    #
    # This filtered `last_compiled_at IS NOT NULL`, so the detector could only ever see hubs it had
    # ALREADY compiled once. Anything never compiled was structurally invisible — and that is
    # 67,762 of 68,296 non-Source entities (99.2%), including EVERY entity on business, school,
    # finance and ops. The detector reported "No hubs flagged. System is fresh" across all five
    # Cores while almost nothing in the brain had ever been synthesised.
    #
    # It is the same class as every other defect found this session: an instrument that answers a
    # narrower question than its name implies, and reports green. A hub written once at extraction
    # and never re-synthesised is precisely what makes the NEXT session's recall stale — the case
    # the operator raised as "that hub needed updating" — a person hub whose stored summary was
    # months behind the relationship it describes.
    #
    # NULL now sorts FIRST with age_days = NULL, treated below as infinite age.
    cur.execute("""
        SELECT id, name, kind, last_compiled_at, source_file,
               CASE WHEN last_compiled_at IS NULL THEN NULL
                    ELSE EXTRACT(EPOCH FROM (now() - last_compiled_at))/86400.0 END AS age_days
        FROM entities
        WHERE valid_until IS NULL
          AND org_id = current_setting('app.current_org_id')::bigint
        ORDER BY (last_compiled_at IS NOT NULL), name
    """)
    rows = cur.fetchall()

    hubs_by_name = {h.stem: h for h in discover_hubs()}
    hubs_by_canonical = {}
    for h in discover_hubs():
        try:
            text = h.read_text(errors="replace")
            m = HUB_FRONTMATTER_RE.match(text)
            if m:
                for line in m.group(1).splitlines():
                    if line.startswith("name:"):
                        nm = line.split(":", 1)[1].strip()
                        hubs_by_canonical[nm] = h
        except OSError:
            pass

    drift_records = []
    no_hub_possible = 0
    for row in rows:
        name = row["name"]
        hub = hubs_by_canonical.get(name) or hubs_by_name.get(name.lower().replace(" ", "-"))
        source_files = parse_hub_source_files(hub) if hub else []

        if not source_files:
            # No source files to count against — skip
            continue

        # PHANTOM HUB MATCH GUARD (measured 2026-08-31/09-01: 136-160 rows fleet-wide, every run).
        #
        # `hub` above is found by NAME ALONE (hubs_by_canonical / hubs_by_name), case/slug-insensitive,
        # with NO check that the file is actually THIS row's own hub. The shared vault is one corpus
        # across all 5 orgs, so a display name collides constantly: finance's (Project, "Core") has
        # never had a hub written for it, but life's entities/core.md happens to share the string
        # "Core" and gets handed to it anyway. detect_drift() then measures LIFE's evidence growth
        # against a FINANCE row and, when it crosses threshold, flags the finance row "would
        # re-synthesize" — a re-synthesis that can never happen: partition_drifted() matches on
        # (kind, name) against the DB row's OWN identity, that pair has no hub of its own, and the
        # row is correctly refused every time. Fleet-wide measurement (2026-09-01 reproduction):
        # 124-155 of ~136-160 permanently-unmatched rows are exactly this — entities.source_file is a
        # `chunk-body-*.json` / `chunk-entities.json` / `chunk-tools-full.json` / `__merge_stub__` id
        # from the bulk chunk-ingestion pipeline, which never writes a per-entity markdown hub at all.
        # Nothing downstream can fix a match failure for a hub that was never authored for this row.
        #
        # entities.source_file is the authoritative "was a hub ever authored for this specific row"
        # signal — set at ingestion, independent of the coincidental name lookup above. Gate on it:
        # a row whose own provenance is not a vault entities/topics markdown path is excluded from
        # drift scoring (it cannot be refreshed by this pipeline, so it is not "drifted" by any
        # definition this pipeline can act on) but counted separately below, so the report stays
        # honest about how many entities exist outside this pipeline's reach rather than silently
        # shrinking "drifted_count" — the muted-instrument failure this repo has hit before.
        if not re.search(r"/(entities|topics)/[^/]+\.md$", row["source_file"] or ""):
            no_hub_possible += 1
            continue

        # Count evidence at last_compiled_at vs now
        cur.execute("""
            SELECT
              COUNT(*) FILTER (WHERE created_at <= %s) AS prev_count,
              COUNT(*) AS curr_count
            FROM evidence
            WHERE source_file = ANY(%s)
              AND org_id = current_setting('app.current_org_id')::bigint
        """, (row["last_compiled_at"], source_files))
        cts = cur.fetchone()
        prev = cts["prev_count"] or 0
        curr = cts["curr_count"] or 0
        new_rows = curr - prev

        # Drift signals.
        # never_compiled is its own signal and does NOT require new evidence: a hub that has never
        # been synthesised is stale on arrival, however quiet its sources have been since.
        never_compiled = row["last_compiled_at"] is None
        age_drift = (not never_compiled
                     and row["age_days"] >= AGE_DAYS_THRESHOLD and new_rows >= MIN_NEW_ROWS)
        growth_drift = (prev > 0 and (curr - prev) / prev >= GROWTH_THRESHOLD and new_rows >= MIN_NEW_ROWS)
        forced = name in force_set

        if not (never_compiled or age_drift or growth_drift or forced):
            continue

        drift_score = max(
            5.0 if never_compiled else 0,   # below `forced` (10), above ordinary age drift
            row["age_days"] / AGE_DAYS_THRESHOLD if age_drift else 0,
            (curr - prev) / max(prev, 1) / GROWTH_THRESHOLD if growth_drift else 0,
            10.0 if forced else 0,  # forced always wins
        )
        drift_records.append({
            "entity_id": row["id"],
            "name": name,
            "kind": row["kind"],
            "last_compiled_at": row["last_compiled_at"].isoformat() if row["last_compiled_at"] else None,
            "age_days": round(row["age_days"], 2) if row["age_days"] is not None else None,
            "evidence_prev": prev,
            "evidence_curr": curr,
            "evidence_added": new_rows,
            "drift_score": round(drift_score, 3),
            "reasons": [
                *(["never-compiled"] if never_compiled else []),
                *(["age"] if age_drift else []),
                *(["growth"] if growth_drift else []),
                *(["forced"] if forced else []),
            ],
        })

    drift_records.sort(key=lambda r: -r["drift_score"])

    # --min-degree: RESTRICT TO HUBS THAT ACTUALLY ANCHOR THE GRAPH (2026-08-31).
    #
    # Drift score cannot prioritise on a Core that has never compiled anything. Every never-compiled
    # hub scores the 5.0 maximum, so on the four peer Cores — 0 compiled of ~36,000 entities — the
    # whole corpus ties at the top and the sort degenerates into insertion order. Partitioning that
    # spends the budget on leaf nodes: a peer's report holds 4,043 records, of which 152 have degree
    # >= 10. The stale-hub complaint this pipeline exists for ("things nowhere near relevant are main
    # hubs") is ABOUT the high-degree ones, and they were indistinguishable from the tail.
    #
    # Degree is computed in ONE query over the candidate ids rather than per record — a per-record
    # lookup here is 4,043 round trips on a peer.
    if min_degree > 0 and drift_records:
        ids = [r["entity_id"] for r in drift_records]
        try:
            _c = connect(); _k = _c.cursor()
            _k.execute(
                "SELECT e.id, count(ed.*) FROM entities e "
                "LEFT JOIN entity_edges ed ON (ed.from_entity_id=e.id OR ed.to_entity_id=e.id) "
                "WHERE e.id = ANY(%s) GROUP BY e.id", (ids,))
            degree = {r[0]: r[1] for r in _k.fetchall()}
            _c.close()
        except Exception as exc:
            # Fail LOUD. Silently returning the unfiltered set would spend a full-corpus budget
            # while the caller believes it asked for the top slice.
            raise RuntimeError(f"--min-degree could not compute degrees: {exc}") from exc
        before = len(drift_records)
        drift_records = [r for r in drift_records if degree.get(r["entity_id"], 0) >= min_degree]
        for r in drift_records:
            r["degree"] = degree.get(r["entity_id"], 0)
        drift_records.sort(key=lambda r: (-r["drift_score"], -r.get("degree", 0)))
        if verbose:
            print(f"  --min-degree {min_degree}: {before} drifted -> {len(drift_records)} kept")

    # Cost estimate: ~5K input tokens evidence + ~500 output per hub, Sonnet 4.6 rates
    # Input: $3/M, Output: $15/M. Per-hub ≈ 5000 * 3/1M + 500 * 15/1M = $0.015 + $0.0075 = $0.0225
    est_cost = round(len(drift_records) * 0.025, 2)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "age_days": AGE_DAYS_THRESHOLD,
            "growth_pct": GROWTH_THRESHOLD * 100,
            "min_new_rows": MIN_NEW_ROWS,
        },
        "total_entities_compiled": len(rows),
        "drifted_count": len(drift_records),
        "estimated_cost_usd": est_cost,
        "force_list_size": len(force_set),
        "min_degree": min_degree,
        # Visible, not folded into drifted_count — see PHANTOM HUB MATCH GUARD above. These rows
        # are excluded from "drifted" because this pipeline cannot ever act on them, not because
        # the corpus is fresher than drifted_count alone would suggest.
        "no_hub_possible_count": no_hub_possible,
        "drift_records": drift_records,
    }

    if verbose:
        print(f"Drift report — {datetime.now(timezone.utc).isoformat()}")
        print(f"  Compiled entities scanned: {len(rows)}")
        print(f"  Drifted (would re-synthesize): {len(drift_records)}")
        print(f"  No hub possible (chunk-ingested, outside this pipeline's reach — not counted as drift): {no_hub_possible}")
        print(f"  Estimated cost: ${est_cost}")
        print(f"  Force-list: {len(force_set)} explicit entries")
        if drift_records:
            print(f"\n  Top 10 by drift score:")
            for r in drift_records[:10]:
                reasons = ",".join(r["reasons"])
                _age = f"{r['age_days']}d" if r['age_days'] is not None else "never"
            print(f"    {r['drift_score']:5.2f}  [{reasons:>15}]  {r['name']:30s}  age={_age}  evidence: {r['evidence_prev']} → {r['evidence_curr']} (+{r['evidence_added']})")
        else:
            print("\n  No hubs flagged. System is fresh or drift hasn't accumulated.")

    conn.close()
    return report


def write_report(report: Dict) -> Path:
    """Write the drift report, ORG-QUALIFIED and ATOMIC.

    Codex, 2026-08-28 (CRITICAL): the name was `drift-report-<date>.json` with no org in it.
    The moment the nightly ran --detect for all five orgs in a loop, each iteration overwrote
    the previous one and only org 5 survived — and session-start-truth-drift.sh consumes the
    NEWEST report in this directory, so a life session could partition ops's hubs. That is live
    state corruption, not just misleading logging. The non-atomic write_text could also be read
    half-written by a concurrent SessionStart.

    Org goes in the filename; the write goes to a temp file in the same directory and is
    os.replace'd, which is atomic on the same filesystem.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    # The org in the FILENAME must be the org the data actually came from, not the one the caller
    # asked for. _env resolves org from identity.json and IGNORES CORE_ORG_ID when they disagree
    # (it logs "using 1"), so labelling by the env var produced drift-report-org5 containing org-1
    # rows — mislabelled, which is worse than the overwriting this filename was added to fix.
    org = "unknown"
    try:
        _c = connect(); _k = _c.cursor()
        _k.execute("SELECT current_setting('app.current_org_id')")
        org = str(_k.fetchone()[0]); _c.close()
    except Exception:
        # Same silent-fallback-to-1 the resolver exists to kill (test_org_is_single_sourced.py):
        # reading CORE_ORG_ID directly here just re-opens the door this whole file's connect()
        # path was written to close. _env.get_org_id() is identity-first and fails loud instead.
        org = str(get_org_id())
    out = WORK_DIR / f"drift-report-org{org}-{date_str}.json"
    tmp = out.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(report, indent=2, default=str))
    os.replace(tmp, out)
    print(f"\nWritten: {out}")
    return out



def _reports_for_this_org():
    """Drift reports for THIS org, newest LAST by mtime.

    2026-08-29: this was `sorted(glob("drift-report-*.json"))` and took the last entry
    ALPHABETICALLY. Once reports became org-qualified, `drift-report-org4-...` sorted after
    `drift-report-org1-...`, so partition read a stale 0-record org4 file and printed
    "No drift to partition" while the org1 report sat beside it holding 4,112 records.
    Same shape as every other defect this session: a selector that answers a slightly
    different question than its caller assumes, and fails quietly. Filter by org, order by
    mtime, and fall back to the legacy un-orged names so old reports still load.
    """
    try:
        _c = connect(); _k = _c.cursor()
        _k.execute("SELECT current_setting('app.current_org_id')")
        org = str(_k.fetchone()[0]); _c.close()
    except Exception:
        # See write_report's matching except: _env.get_org_id(), not a raw CORE_ORG_ID read.
        org = str(get_org_id())
    mine = list(WORK_DIR.glob(f"drift-report-org{org}-*.json"))
    if not mine:
        mine = [f for f in WORK_DIR.glob("drift-report-2*.json")]
    return sorted(mine, key=lambda f: f.stat().st_mtime)

def partition_drifted(report_path: Path, batches: int) -> List[Path]:
    """Read drift report → emit batch-NN.json files filtered to drifted hubs."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    report = json.loads(report_path.read_text())
    # Match on (kind, name), NOT name alone (2026-07-30).
    #
    # `ingest_refresh` updates `WHERE entities.kind = ? AND entities.name = ?`, so kind is half
    # the identity. This built a name-only set and then took `kind` from whichever HUB FILE
    # matched — which invents pairs the DB has never had. Measured on 2026-07-30: two rows
    # drifted (Entity/"Receipt Reader", Topic/"sentinel-security-review"), the vault happens to
    # hold both `entities/receipt-reader.md` and `topics/receipt-reader.md`, and partition emitted
    # THREE batches. Topic/"Receipt Reader" exists in no DB row, so a Sonnet worker compiled a hub
    # that updated nothing — paid for, verified, ingested, and silently dropped.
    #
    # The normalised fallback is now keyed by kind too, so a Topic file can only ever rescue a
    # drifted Topic row. Nothing else about the collision guard below changes: it already keyed
    # on (kind, name) and was correct.
    drift_pairs = {(r["kind"], r["name"]) for r in report["drift_records"]}
    drift_names = {n for _, n in drift_pairs}
    if not drift_names:
        print("No drift to partition. Exiting.")
        return []

    # Map hub files → entries (reuse compile-truth.py parse_hub via subprocess-import)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import importlib.util
    spec = importlib.util.spec_from_file_location("compile_truth", str(Path(__file__).resolve().parent / "compile-truth.py"))
    ct = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ct)

    # Match hub files to drifted DB names EXACT-FIRST, then normalized (2026-07-28).
    #
    # `--detect` reads names from the DB; `--partition` reads them from each hub file's frontmatter.
    # Those two disagree, because the vault's `name:` field is not written consistently:
    # receipt-reader.md carries `name: Receipt Reader` (display form) while core.md carries
    # `name: core` (the slug). The DB row is `Core`. Exact matching therefore silently found
    # NOTHING for every slug-named hub — `--detect` reported them drifted, `--partition` produced
    # "0 non-empty batches", and the close could never refresh them no matter how many times it ran.
    # Measured: `Core` and `Job Hunter` had sat drifted at age 61.8d, unrefreshable.
    #
    # Normalising the MATCH is the narrow fix; rewriting 5589 hub files to one convention is not
    # something a close step should do silently. Exact match still wins so two genuinely distinct
    # hubs can never be collapsed by normalisation — the fallback only rescues names that differ
    # solely by case and separators.
    #
    # Character class widened 2026-09-01. Measured: of the ~136-160 rows partition reports
    # unmatched every run, ~4-5 point at a hub file that DOES exist and IS the right hub — the
    # DB name and the hub's frontmatter `name:` differ only by decorative punctuation the old
    # class didn't cover: an em-dash ("Core Brain Knowledge Graph Extraction Pipeline" vs hub
    # "Core Brain — knowledge graph extraction pipeline"), parentheses ("Core Brain project" vs
    # hub "Core Brain (project)", "Microsoft Copilot Work IQ" vs hub "Microsoft Copilot (Work
    # IQ)"), or a stray slash (Topic "session-close-reconciliation" vs hub
    # "session-close-/-reconciliation"). None of these are two different real things — they're
    # the same LLM-summarised name typeset two ways. Dash variants (U+2010-U+2015: hyphen,
    # non-breaking hyphen, figure/en/em dash, horizontal bar), parens/brackets, and "/" now
    # collapse the same way plain "-" already did. Still no plain alnum-collapse (e.g. spaces
    # alone) — that's the wider, riskier change the 2026-07-28 fallback deliberately avoided.
    def _norm(s: str) -> str:
        return re.sub(r"[\s_\-‐-―()\[\]/]+", " ", str(s)).strip().lower()

    all_hubs = ct.discover_hubs()

    # Matching flipped to be PER DRIFTED PAIR, not per hub file (2026-09-01).
    #
    # The old loop walked hub FILES and computed one `hit` per file — so a single physical hub
    # could satisfy AT MOST one drifted (kind, name), by construction, even when it was
    # legitimately the right hub for more than one. Measured case: school has BOTH
    # (Entity, "ECON 301 Market Structures and Pricing") and (Entity, "ECON 301 - Market  # privacy-ok: illustrative course code, invented
    # Structures and Pricing") drifted — near-duplicate rows from separate extraction passes —
    # and only ONE vault hub, entities/econ-301-market-structures-and-pricing.md (frontmatter
    # name carries the dash). The dashed row wins the file via EXACT match; the undashed row normalises to
    # the identical key but the per-file loop never gets a second chance to try it against the
    # same file, so it reports "no matching hub file" forever even though the hub visibly exists.
    #
    # Walking drifted pairs instead asks the right question per row: "which hub file(s) match
    # THIS (kind, name), exact-then-normalised" — and lets the same file answer that question for
    # more than one row. The collision guard's actual job — refuse when a name could mean more
    # than one DIFFERENT physical file — is unchanged; it now triggers on multiple DISTINCT
    # candidate files for one drifted pair, not on one file being claimed twice.
    exact_hub_index: Dict[tuple, List[dict]] = {}
    norm_hub_index: Dict[tuple, List[dict]] = {}
    for h in all_hubs:
        p = ct.parse_hub(h)
        exact_hub_index.setdefault((p["kind"], p["name"]), []).append(p)
        norm_hub_index.setdefault((p["kind"], _norm(p["name"])), []).append(p)

    parsed, matched_pairs = [], set()
    _collisions = {}   # (kind, db_name) -> [hub_path, ...] ambiguous candidates
    for dk, dn in sorted(drift_pairs):
        candidates = exact_hub_index.get((dk, dn)) or norm_hub_index.get((dk, _norm(dn))) or []
        distinct_paths = sorted({c["hub_path"] for c in candidates})
        if not distinct_paths:
            continue

        # COLLISION GUARD (sentinel-code, 2026-07-28; re-anchored to drifted pairs 2026-09-01).
        # More than one DISTINCT hub file matches this one (kind, name) — deciding which file
        # "really" owns the name is a judgement about the vault, not something inferable here.
        # Refuse rather than guess; every candidate is named so the ambiguity surfaces instead of
        # silently resolving itself the wrong way.
        if len(distinct_paths) > 1:
            _collisions[(dk, dn)] = distinct_paths
            continue

        # The DB name is authoritative — ingest matches on (kind, name), so handing the subagent
        # the hub file's own spelling would produce an out-file that updates nothing.
        p = dict(candidates[0])
        p["name"] = dn
        parsed.append(p)
        matched_pairs.add((dk, dn))

    if _collisions:
        print(f"  ⚠ {len(_collisions)} drifted (kind, name) pair(s) matched MORE THAN ONE hub file — "
              f"all skipped rather than guessing which one owns the name:")
        for (kind, name), paths in _collisions.items():
            print(f"      {kind}/{name}: {', '.join(p.rsplit('/', 1)[-1] for p in paths)}")

    # Pair-aware: a drifted Topic row is NOT covered by a matched Entity row of the same name.
    unmatched = [f"{k}/{n}" for (k, n) in sorted(drift_pairs) if (k, n) not in matched_pairs]
    if unmatched:
        # NEVER silent: a drifted hub with no file is a hub the close cannot fix, and the whole
        # point of 7.6 is that a clean close leaves nothing for the next session to flag. After
        # the 2026-09-01 fixes (detect_drift's phantom-hub guard + the matching changes above)
        # this should be near-empty — a genuine remainder here is either a real ambiguous
        # collision (see above) or a hub gap worth investigating, not the old permanent noise.
        print(f"  ⚠ {len(unmatched)} drifted hub(s) have NO matching hub file and cannot be "
              f"refreshed: {', '.join(map(str, unmatched[:8]))}")

    # Round-robin partition
    workers = [[] for _ in range(batches)]
    for i, p in enumerate(parsed):
        workers[i % batches].append(p)

    # Clear previous refresh batches first. Archive instead of delete — a re-run
    # (e.g. second session opening mid-refresh) must not destroy in-flight -out files.
    prev_dir = WORK_DIR / "prev"
    prev_dir.mkdir(exist_ok=True)
    for old in WORK_DIR.glob("refresh-batch-*.json"):
        old.replace(prev_dir / old.name)

    paths = []
    for n, batch in enumerate(workers, start=1):
        if not batch:
            continue
        path = WORK_DIR / f"refresh-batch-{n:02d}.json"
        path.write_text(json.dumps({"batch_id": n, "n_entities": len(batch), "entities": batch}, indent=2))
        paths.append(path)
    print(f"Partitioned {len(parsed)} drifted hubs into {len(paths)} non-empty batches at {WORK_DIR}/")
    for p in paths:
        with open(p) as fp:
            b = json.load(fp)
        print(f"  {p.name}: {b['n_entities']} entities")
    return paths


def ingest_refresh() -> None:
    """Read refresh-batch-*-out.json files and UPDATE entities.compiled_truth_md."""
    out_files = sorted(WORK_DIR.glob("refresh-batch-*-out.json"))
    if not out_files:
        print(f"No refresh-batch-*-out.json files in {WORK_DIR}/.")
        sys.exit(1)
    rows = []
    for of in out_files:
        data = json.loads(of.read_text())
        for entry in data.get("results", []):
            truth = entry.get("compiled_truth_md", "").strip()
            if not truth:
                continue
            rows.append((entry["kind"], entry["name"], truth, entry.get("confidence")))
    if not rows:
        print("No results to ingest.")
        return

    conn = connect()
    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        UPDATE entities SET
          compiled_truth_md = data.truth,
          -- ::real is NOT cosmetic (2026-08-31). psycopg2 adapts a Python float to a numeric
          -- literal and a str to text, so this UPDATE succeeded for every past batch purely
          -- because the synthesising subagents happened to emit `0.9` rather than `"0.9"`.
          -- One brief that says "confidence": "0.9" and the whole ingest dies on
          -- DatatypeMismatch after the model spend is already paid. Cast, don't hope.
          confidence = data.conf::real,
          last_compiled_at = now()
        FROM (VALUES %s) AS data(kind, name, truth, conf)
        WHERE entities.kind = data.kind AND entities.name = data.name
          AND entities.org_id = current_setting('app.current_org_id')::bigint
    """, rows)
    applied = cur.rowcount
    conn.commit()

    # Report what LANDED, not what was submitted (2026-07-30).
    #
    # This printed len(rows) — the number of results handed to the UPDATE. The UPDATE matches on
    # (kind, name, org_id); a result naming a pair with no row updates nothing and is discarded in
    # silence. The close protocol's own safeguard is "the ingest count MUST equal the drifted-hub
    # count", and on 2026-07-30 that check read 3 == 3 and passed while only 2 hubs were written —
    # a paid-for, verified, ingested Sonnet refresh vanished. A submitted-count is not evidence of
    # a write, and the one number the operator is told to check must be the applied one.
    if applied != len(rows):
        lost = len(rows) - applied
        print(f"  ⚠ SHORTFALL: {len(rows)} result(s) submitted, {applied} row(s) actually updated "
              f"— {lost} named a (kind, name) with no matching entity row for this org.")
        print(f"    Submitted pairs: {sorted({(k, n) for k, n, _, _ in rows})}")
        print(f"    Nothing was written for the missing pair(s). Do NOT report hubs as current.")
    print(f"Refresh-ingested {applied} entities (from {len(rows)} submitted).")
    # ROWCOUNT AFTER execute_values IS THE LAST PAGE ONLY (2026-08-29).
    # psycopg2's execute_values batches at page_size=100 and cur.rowcount reflects only the final
    # page. A 265-row ingest reported "65 updated, 200 named a (kind,name) with no matching entity"
    # and printed 200 SUCCESSFUL entities as failures — 100+100+65 is exactly the page arithmetic.
    # Sampling three of the "missing" (Andrej Karpathy, Il Fornaio, vendorvault) showed all three
    # live, org-scoped and compiled. The count below, which re-reads the table, was right all along.
    # An instrument that cries wolf gets muted, so it has to count what actually happened.
    cur.execute("SELECT count(*) FROM entities WHERE last_compiled_at > now() - interval '5 minutes' "
                "AND org_id = current_setting('app.current_org_id')::bigint")
    print(f"Entities updated in last 5 min: {cur.fetchone()[0]}")
    conn.close()


def status():
    print("compile-truth-refresh status:")
    print(f"  WORK_DIR:   {WORK_DIR}")
    print(f"  FORCE_LIST: {FORCE_LIST} {'(exists)' if FORCE_LIST.exists() else '(missing, no forced entries)'}")
    reports = _reports_for_this_org()
    if reports:
        latest = reports[-1]
        r = json.loads(latest.read_text())
        print(f"  Latest report: {latest.name}")
        print(f"    drifted_count: {r['drifted_count']}")
        print(f"    estimated_cost: ${r['estimated_cost_usd']}")
    batches = sorted(WORK_DIR.glob("refresh-batch-*.json"))
    in_batches = [p for p in batches if "-out" not in p.name]
    out_batches = sorted(WORK_DIR.glob("refresh-batch-*-out.json"))
    print(f"  Refresh batches: {len(in_batches)} input / {len(out_batches)} output")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--partition", action="store_true")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--batches", type=int, default=14)
    ap.add_argument("--min-degree", type=int, default=0,
                    help="Only hubs with at least this many graph edges. 0 = no filter.")
    ap.add_argument("--report", type=Path, help="Path to drift report JSON (for --partition)")
    args = ap.parse_args()
    # One probe up front (the convention in _env.connect_or_skip, whose docstring names this
    # file as the unguarded site). Every mode below needs the DB; without it, say so and exit 0
    # rather than let detect_drift() raise inside the close chain.
    _probe = connect_or_skip("COMPILE-TRUTH")
    if _probe is None:
        return 0
    _probe.close()

    # INDEPENDENT ifs, not an elif chain (2026-07-26 fix). The close directive (and the
    # SessionStart drift script) invoke `--detect --partition` in ONE call; the old elif chain
    # silently ran only detect, so partition kept serving STALE batch files — on 07-25 the close
    # refreshed the 07-24 batch (missing the actually-drifted 'Sentinel' topic), and on 07-26 the
    # batch on disk still held 07-24's four hubs. Flags now compose.
    ran = False
    if args.detect:
        ran = True
        report = detect_drift(min_degree=args.min_degree)
        write_report(report)
    if args.partition:
        ran = True
        report_path = args.report
        if not report_path:
            reports = _reports_for_this_org()
            if not reports:
                sys.exit("No drift report found. Run --detect first.")
            report_path = reports[-1]
        partition_drifted(report_path, args.batches)
    if args.ingest:
        ran = True
        ingest_refresh()
    if args.status:
        ran = True
        status()
    if not ran:
        ap.print_help()


if __name__ == "__main__":
    main()
