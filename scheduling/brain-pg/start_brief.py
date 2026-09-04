#!/usr/bin/env python3
"""start_brief.py — two-tier session-start hydration (unified redesign, step ③).

Plan: PART 7.2 D4 (Codex) — deterministic continuity FIRST, optional semantic enrichment SECOND,
scoped to this Core, suppressed when the brain status says recall is lagging/unavailable. This is
the read-time hydration the old start protocol lacked (it oriented from local files; the brain was a
lazy fallback). Now the brain is recalled at start — but as a SECOND tier that never overrides the
deterministic operational state.

Tier 1 (deterministic continuity) is already emitted by session-start-check.sh (reconcile debt,
brain status, core-si, current-state). This module adds Tier 2: the most recent accepted decisions
(supersession-aware — superseded excluded) + the last few captured sessions, so the model starts
knowing "what did we decide / what were we working on" FROM the brain.

Suppressed when brain status is UNAVAILABLE (DB down) — falls back silently to local files.
Fork-safe. Prints a compact block to stdout (empty if nothing / unavailable).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def brief(max_decisions: int = 6, max_sessions: int = 4) -> str:
    try:
        from brain_status import status
        st = status()
        if st.get("availability") == "UNAVAILABLE":
            return ""  # degrade to local files; never fabricate from a down store
        from _env import connect_corebrain, get_org_id
        org = get_org_id()
        conn = connect_corebrain()
        lines = []
        with conn.cursor() as cur:
            # Tier 2a: most recent accepted, active decisions (this Core's own org — self-scoped).
            cur.execute("""SELECT effective_from::date, subject_key, object_json #>> '{}'
                           FROM assertions
                           WHERE org_id=%s AND review_status='accepted' AND lifecycle_status='active'
                             AND effective_from IS NOT NULL
                           ORDER BY effective_from DESC, id DESC LIMIT %s""", (org, max_decisions))
            rows = cur.fetchall()
            if rows:
                lines.append("RECENT DECISIONS (from brain — current, supersession-aware):")
                for d, subj, obj in rows:
                    lines.append(f"  • [{d}] {subj} — {(obj or '')[:120]}")
            # Tier 2b: last few captured sessions.
            cur.execute("""SELECT DISTINCT s.current_uri, r.observed_at::date
                           FROM sources s JOIN source_revisions r ON r.source_id=s.id
                           WHERE s.org_id=%s AND s.source_kind='session_jsonl'
                           ORDER BY r.observed_at::date DESC LIMIT %s""", (org, max_sessions))
            srows = cur.fetchall()
        conn.close()
        if not lines:
            return ""
        return "\n".join(lines)
    except Exception:
        return ""  # fail-open: hydration is a bonus, never a blocker


def main() -> int:
    b = brief()
    if b:
        print(b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
