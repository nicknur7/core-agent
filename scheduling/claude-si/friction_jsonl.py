#!/usr/bin/env python3
"""friction-jsonl.py — shared streaming JSONL transcript parser for the friction-case
SI engine (P1). Reuses the transcript model proven in learned-corpus-miner.py (SLUG
dir, parentUuid reconstruction, HOOK_PREFIXES / external-user filtering) and EXTENDS it
to preserve tool_use / tool_result blocks — the friction miner needs them to derive
trigger_context (before_tool / during_tool / after_claim), whereas the old miner
discarded them.

Fail-closed by construction: a file with >0.1% malformed lines, or a correction whose
parentUuid chain is broken, is rejected (never guessed). Stdlib-only.  # privacy-ok: generic engineering vocabulary

Non-turn record types in a real stream (mode, file-history-snapshot, attachment,
system, queue-operation, summary, …) are simply not indexed as turns — they carry no
`type in {user,assistant}` we reconstruct from, and are counted as non-turn, not malformed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# --- reused verbatim from learned-corpus-miner.py (single source of truth for the model) ---
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "bin"))
import turn_provenance as _prov  # noqa: E402

# NO LONGER THE PROVENANCE TEST — superseded by turn_provenance.is_human_turn(). Retained because
# other call sites in this module still reference it for display/segmentation.
HOOK_PREFIXES = ("<system-reminder>", "<command-name>", "<command-message>",
                 "<local-command-stdout>", "<user-prompt-submit-hook>",
                 "Stop hook feedback", "⛔", "[Request interrupted",
                 "<details>")

MALFORMED_TOLERANCE = 0.001  # >0.1% unparseable lines -> reject the file (fail closed)


import re as _re
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "bin"))
import core_seat as _core_seat  # noqa: E402 — canonical seat/slug resolver
# Secret patterns masked before any correction/assistant text is persisted or injected (Codex #12).
_SECRET_RX = _re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|Bearer\s+[A-Za-z0-9._-]{12,}"
    r"|-----BEGIN[^-]+PRIVATE KEY-----|[A-Fa-f0-9]{40,})")


def redact(text):
    """Mask common secret shapes (API keys, tokens, JWTs, private keys, long hex) in any text
    that will be stored in the graph or injected into a future conversation."""
    if not isinstance(text, str):
        return text
    return _SECRET_RX.sub("[REDACTED]", text)


def transcript_dir(project_dir: str | os.PathLike | None = None) -> Path:
    """~/.claude/projects/<hyphenated-abs-project-path> for a Core project dir."""
    base = Path(project_dir or os.environ.get("CLAUDE_PROJECT_DIR")
                or Path(__file__).resolve().parents[2]).resolve()
    return _core_seat.transcripts_dir(base)


def session_jsonls(project_dir: str | os.PathLike | None = None) -> list[Path]:
    """TOP-LEVEL session transcripts only (exclude the subagents/ subdir), newest first."""
    d = transcript_dir(project_dir)
    if not d.is_dir():
        return []
    files = [p for p in d.glob("*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def is_external_user(entry: dict) -> bool:
    """A turn Claude Code stamped as typed by a human.

    THIS IS THE SAME QUESTION learned-corpus-miner asked, answered by its own copy of the rule —
    two resolvers, one subject, in the two places that decide whose words the system learns from.
    Both now call bin/turn_provenance.py, so a fix to one is a fix to both.

    The old rule combined `userType == "external"` (true for Nick AND for every scheduler prompt)
    with a hand-grown HOOK_PREFIXES blocklist that the scheduler prompts do not match. See
    turn_provenance for why content-based filtering cannot work here: the contaminant is
    better-formed than the real thing, so well-formedness and consistency both prefer the fake.
    """
    return _prov.is_human_turn(entry)


@dataclass
class ParseResult:
    ok: bool
    by_uuid: dict = field(default_factory=dict)
    total_lines: int = 0
    malformed: int = 0
    non_turn: int = 0
    error: str | None = None


def parse_file(jsonl_path: str | os.PathLike) -> ParseResult:
    """Stream-index a transcript by uuid. Fail closed if too many malformed lines."""
    by_uuid: dict = {}
    total = malformed = non_turn = 0
    try:
        with Path(jsonl_path).open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(d, dict):
                    malformed += 1
                    continue
                u = d.get("uuid")
                if u:
                    # index EVERY uuid'd record (user/assistant AND non-turn: system, mode,
                    # file-history-snapshot, …) — a correction's parentUuid points to the
                    # `system` hook-injection record, so the walk must traverse non-turn
                    # records to reach the assistant turn above them. reconstruct_moment's
                    # `else` branch skips through them; failing to index them broke the chain.
                    by_uuid[u] = d
                    if d.get("type") not in ("user", "assistant"):
                        non_turn += 1
                else:
                    non_turn += 1
    except OSError as e:
        return ParseResult(ok=False, error=f"unreadable: {e}")
    if total and (malformed / total) > MALFORMED_TOLERANCE:
        return ParseResult(ok=False, total_lines=total, malformed=malformed,
                           non_turn=non_turn, error=f"malformed {malformed}/{total} > 0.1%")
    return ParseResult(ok=True, by_uuid=by_uuid, total_lines=total,
                       malformed=malformed, non_turn=non_turn)


def _blocks_of(entry: dict) -> list[dict]:
    """Normalize an assistant/user message into typed blocks (text/tool_use/tool_result)."""
    role = entry.get("type")
    content = (entry.get("message") or {}).get("content")
    out: list[dict] = []
    if isinstance(content, str):
        if content.strip():
            out.append({"role": role, "kind": "text", "uuid": entry.get("uuid"),
                        "text": content, "tool_name": None})
        return out
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if pt == "text" and p.get("text"):
                out.append({"role": role, "kind": "text", "uuid": entry.get("uuid"),
                            "text": p.get("text"), "tool_name": None})
            elif pt == "tool_use":
                out.append({"role": role, "kind": "tool_use", "uuid": entry.get("uuid"),
                            "text": None, "tool_name": p.get("name")})
            elif pt == "tool_result":
                out.append({"role": role, "kind": "tool_result", "uuid": entry.get("uuid"),
                            "text": None, "tool_name": None})
    return out


@dataclass
class Moment:
    complete: bool          # parent chain fully reconstructed to a real user prompt
    correction_uuid: str
    correction_text: str
    prompt_text: str        # the user prompt that opened the turn being corrected (N-2)
    blocks: list = field(default_factory=list)   # assistant turn blocks (incl tool_use), oldest-first
    tool_uses: list = field(default_factory=list)  # tool names in the corrected turn, in order


def reconstruct_moment(correction_uuid: str, by_uuid: dict) -> Moment | None:
    """From a correction (user turn N), walk parentUuid up to collect the full assistant
    turn N-1 (text + tool_use, in order) and the opening user prompt N-2. Returns None if
    the correction isn't in the index; `complete=False` if the chain breaks before a prompt."""
    corr = by_uuid.get(correction_uuid)
    if corr is None or not is_external_user(corr):
        return None
    corr_text = _prov.text_of(corr) or ""
    corr_session = corr.get("sessionId")
    rec_blocks: list[list[dict]] = []  # newest-first list of per-record block lists
    prompt_text = ""
    complete = False
    cur = by_uuid.get(corr.get("parentUuid"))
    seen: set[str] = set()
    while cur is not None:
        u = cur.get("uuid")
        if u in seen:  # cycle guard
            break
        seen.add(u)
        t = cur.get("type")
        # STOP at a compaction/summary or a session boundary — never attribute blocks from a
        # different/pre-compaction turn to this correction (Codex #10). Chain stays incomplete.
        if t == "summary" or cur.get("isCompactSummary") or (
                corr_session and cur.get("sessionId") and cur.get("sessionId") != corr_session):
            break
        if t == "assistant":
            rec_blocks.append(_blocks_of(cur))  # keep WITHIN-record order (Codex #11)
            cur = by_uuid.get(cur.get("parentUuid"))
        elif t == "user":
            if is_external_user(cur):
                prompt_text = _prov.text_of(cur) or ""
                complete = True
                break
            cur = by_uuid.get(cur.get("parentUuid"))  # tool-result/hook -> keep walking
        else:
            cur = by_uuid.get(cur.get("parentUuid"))
    rec_blocks.reverse()  # oldest-first records; within-record order preserved
    blocks = [b for rb in rec_blocks for b in rb]
    tool_uses = [b["tool_name"] for b in blocks if b["kind"] == "tool_use" and b["tool_name"]]
    return Moment(complete=complete, correction_uuid=correction_uuid, correction_text=corr_text,
                  prompt_text=prompt_text, blocks=blocks, tool_uses=tool_uses)


if __name__ == "__main__":
    # read-only smoke: parse the largest real life transcript, reconstruct one moment.
    import sys
    files = session_jsonls()
    if not files:
        print(f"no transcripts under {transcript_dir()}")
        sys.exit(0)
    biggest = max(files, key=lambda p: p.stat().st_size)
    r = parse_file(biggest)
    print(f"parsed {biggest.name}: ok={r.ok} turns={len(r.by_uuid)} "
          f"non_turn={r.non_turn} malformed={r.malformed}/{r.total_lines} err={r.error}")
    if r.ok:
        # find any external-user entry with a parent to demo reconstruction
        for u, e in r.by_uuid.items():
            if is_external_user(e) and e.get("parentUuid"):
                m = reconstruct_moment(u, r.by_uuid)
                if m and m.complete and m.blocks:
                    print(f"sample moment: complete={m.complete} tools={m.tool_uses[:5]} "
                          f"prompt='{m.prompt_text[:50]}...' corr='{m.correction_text[:50]}...'")
                    break
