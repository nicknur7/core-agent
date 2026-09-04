#!/usr/bin/env python3
"""skill_graduate.py — autonomous promotion of a proven hooked_skill into a real `skill`, and
autonomous demotion when a promoted one stops earning its place.

WHY THIS IS SAFE TO DO WITHOUT ASKING
-------------------------------------
Nick's ratified stance (brain: 2026-07-23 decision + 2026-07-25 rule) is that anything with a
test-gate and reversibility installs autonomously; a human gate is only for the genuinely
irreversible or outward-facing. Writing a local markdown file is neither.

The objection that blocked this earlier was half-right. A skill activates on DESCRIPTION match, which
no gate can test in advance — that part stands. But the claim that skill invocations are invisible to
hooks, and so a watchdog could never see a misbehaving one, was disproven empirically on 2026-07-27:
plant .recall-required-<session>, invoke Skill(recall-similar), observe the marker cleared by
recall-satisfied.py's `tool_name == "Skill"` branch. Invocations ARE observable, and
capability-usage-log now records them onto the shared telemetry bus.

So this takes the shape the engine already uses for enforcement blocks: what cannot be proven in
advance is instead earned by evidence and then kept under measurement. A hooked_skill must have fired
repeatedly, across distinct sessions, over time, through a corpus-gated trigger, without erroring,
before it is granted a description-matched activation surface.

WHAT IT WILL NOT DO
-------------------
- never writes executable code — a SKILL.md is markdown only
- never touches a hand-authored skill: only files carrying GEN_MARKER are demotion-eligible
- never claims a name already used by any skill, command, or plugin
- never emits proactive-activation phrasing; that is refused, because "PROACTIVELY ACTIVATE when…"
  is exactly what turns a narrow capability into a broad one
- generated skills are NOT shared — .claude/skills/ is not in sync-manifest shared.dirs, so a skill
  earned on one Core stays on that Core
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "brain-pg"))

import friction_installer as inst  # noqa: E402

REPO = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[1])
SKILLS_DIR = REPO / ".claude" / "skills"
EVENTS_LOG = REPO / ".claude" / "state" / "hook-events.log"

# Promotion window — deliberately identical to friction_promote's enforcement window. A capability
# gaining an ungated activation surface is at least as consequential as a block gaining teeth, so it
# does not get an easier bar.
MIN_FIRES = 5
MIN_SESSIONS = 3
MIN_DAYS = 7

# Demotion is asymmetric on purpose: slow to promote, quick to retire. A capability nobody uses still
# costs context on every turn its description is considered.
UNUSED_DAYS = 30

# Every generated SKILL.md carries this. It is the ONLY thing that makes a file demotion-eligible, so
# a hand-authored skill cannot be removed by this module even by accident.
GEN_MARKER = "<!-- core:generated-skill"

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,38}$")
_BROAD_DESC = re.compile(
    r"(proactively|always use|use this (whenever|for any|for all)|before responding|"
    r"on every (turn|prompt|message)|any time the user)", re.I)



def _events_text() -> str:
    """hook-events.log across EVERY rotation generation, oldest first.

    ROTATION MADE THIS ORGAN DEMOTE WORKING SKILLS (2026-08-13). hook-events.log rotates at 1 MB and
    rotated tonight at 21:28. Both read sites below opened the LIVE generation only, so immediately
    after a rotation:

        capability rows visible, live only      : 0
        capability rows visible, across rotation: 4
        total lines                             : 282 live of 7295

    This module demotes a skill for being UNUSED. Reading 4% of the log, a skill used four times
    reads as used zero times — so a rotation silently converts "in use" into "retire it". That is
    strictly worse than the block miner's version of the same bug, which only missed cases.

    Its own guard at :112 refuses when fewer than 20 recent lines exist, precisely so "unused" and
    "unrecorded" stay distinguishable — but 282 lines clears that bar comfortably while carrying
    almost none of the evidence. The guard was defending against an empty log, not a truncated one.

    Same fix, same resolver, same fallback discipline as hook_block_miner: degrade to live-only if
    core_paths is unavailable, never to empty. An organ that reads nothing on resolver failure looks
    exactly like a seat where nothing was used.
    """
    try:
        import sys as _sys
        from pathlib import Path as _P
        _sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "bin"))
        from core_paths import read_rotated as _rr
        return _rr(EVENTS_LOG)
    except Exception:
        return EVENTS_LOG.read_text(errors="replace")


def _slug(ask: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (ask or "").lower()).strip("-")
    parts = [p for p in s.split("-") if p not in
             ("the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with", "by", "not")]
    # Cut on a word boundary, not at byte 38. "keep-architecture-diagram-documentatio" and
    # "autonomously-detect-recurring-frustrat" are both live skills whose names end mid-word
    # because the old slice ignored where the words were.
    out: list[str] = []
    for part in parts[:4]:
        if out and len("-".join(out + [part])) > 38:
            break
        out.append(part)
    return ("-".join(out) or "-".join(parts[:4]))[:38].strip("-")


def promoted_artifact_ids() -> set:
    """artifact_ids that ALREADY have a generated skill on disk, read from the GEN_MARKER.

    THE `taken` NAME CHECK IS NOT ENOUGH, and 2026-08-28 proved it the hard way. promote() skipped
    an artifact whose NAME was already claimed — which held only while the slug function was
    stable. The moment _slug was fixed to cut on word boundaries, two artifacts produced a
    different name than the one already on disk, sailed past the name check, and promoted a SECOND
    time:

        art_hs695c70b4eec100ce -> autonomously-detect-recurring-frustrat  (old)
                               -> autonomously-detect-recurring           (new, same artifact)
        art_hsb468fe055f293270 -> keep-architecture-diagram-documentatio  (old)
                               -> keep-architecture-diagram               (new, same artifact)

    Two skills, one procedure, identical descriptions — so the model sees the same capability twice
    and every demotion decision is now split across two directories, each with half the usage.

    The identity of a generated skill is its ARTIFACT, not its filename. That is exactly what the
    marker records, and it is what demote() already keys on. This makes promotion agree with it.
    """
    seen: set = set()
    if not SKILLS_DIR.is_dir():
        return seen
    for d in SKILLS_DIR.iterdir():
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        try:
            head = f.read_text(errors="replace")[:2000]
        except Exception:
            continue
        if GEN_MARKER not in head:
            continue
        m = re.search(r"artifact=(art_[A-Za-z0-9]+)", head)
        if m:
            seen.add(m.group(1))
    return seen


def existing_names() -> set:
    """Every name already invocable, across all three namespaces (skills, commands, plugins)."""
    try:
        import artifact_typer
        return artifact_typer._existing_capability_names()
    except Exception:
        return set()


def telemetry_alive() -> tuple[bool, str]:
    """Is the telemetry pipeline actually running right now?

    THIS IS THE LOAD-BEARING CHECK FOR DEMOTION and it exists because the first version of this file
    got it exactly backwards. `capability_usage()` returns {} when the events log is missing or
    empty — and demote() defaulted every skill to fires=0, so a DEAD TELEMETRY PIPELINE would have
    archived every generated skill older than 30 days. The docstring claimed fail-safe; the code was
    fail-dangerous. Caught by sentinel-code review, 2026-07-27, before it shipped.

    "ROTATED" WAS IN THAT LIST AND WAS NEVER HANDLED (removed 2026-08-13). The author enumerated
    three causes — missing, rotated, empty — and the guards below cover two: :137 refuses when the
    file is absent, :142 refuses when fewer than 20 recent lines exist. Neither catches rotation,
    which produces a file that is present and non-empty and carries almost none of the history. The
    hazard was named in the docstring of the function whose guard did not implement it, for two
    weeks, and core-finance's DOSE 32 recorded this file as PASS without testing rotation — a pass
    that was correct about what it dosed and silent about this.

    Now genuinely handled rather than merely named: `_events_text()` reads every rotation generation,
    so a rotation no longer empties the evidence. The word is removed from this list because it is
    no longer true, not because it stopped mattering.

    A guard against an EMPTY log does not catch a TRUNCATED one. That is the general form, and it
    is why the enumeration read as complete for two weeks while covering two thirds of itself.

    "This skill got zero fires" and "nothing produced any fires" are different facts and must be
    distinguished. Demotion requires the log to exist AND show recent activity of ANY kind, which is
    evidence the recorder is working, not merely that this one skill was quiet.
    """
    if not EVENTS_LOG.is_file():
        return (False, "hook-events.log missing — cannot distinguish unused from unrecorded")
    try:
        lines = _events_text().splitlines()
    except Exception as e:
        return (False, f"hook-events.log unreadable: {e}")
    if not lines:
        return (False, "hook-events.log empty — telemetry produced nothing")
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - UNUSED_DAYS * 86400))
    recent = [l for l in lines if l[:10] >= cutoff]
    if len(recent) < 20:
        return (False, f"only {len(recent)} telemetry events in the last {UNUSED_DAYS}d — "
                       "too little to treat silence as disuse")
    # The RIGHT recorder must be alive, not merely A recorder (sentinel-code follow-up, 2026-07-27).
    # A general "the log has lines" check proves the hook pipeline runs; it does not prove that
    # capability-usage-log — the ONLY producer of the signal demotion reads — is still working. If
    # that single PostToolUse hook silently broke while every other hook kept logging, every skill
    # would look unused and be archived. Demotion consumes capability events, so demotion requires
    # capability events.
    if not any("verdict=capability" in l for l in recent):
        return (False, f"{len(recent)} events in the last {UNUSED_DAYS}d but ZERO capability "
                       "records — capability-usage-log is not producing the signal demotion reads")
    return (True, f"{len(recent)} events in the last {UNUSED_DAYS}d, capability signal present")


def capability_usage() -> dict:
    """{name: {"fires": n, "sessions": set}} from the shared telemetry bus.

    Populated by capability-usage-log (PostToolUse on Skill). An empty return here means only that
    no capability was recorded — it does NOT license removal. See telemetry_alive(), which demote()
    consults first.
    """
    out: dict = {}
    if not EVENTS_LOG.is_file():
        return out
    # WINDOWED (Codex review, 2026-07-27). This read ALL history, so a single use years ago kept
    # fires>0 forever and the skill could never retire — which means "unused for 30 days" was not
    # what the code implemented, despite being what it said. Usage is now counted only inside the
    # same window the demotion decision uses, so the two agree.
    # Timestamps are PARSED, not compared lexically (Codex 2nd round). A lexical `line[:10] < cutoff`
    # let "9999-99-99", a far-future date, or junk like "zzzzzzzzzz" count as recent usage forever —
    # which would keep a dead skill alive permanently. Anything unparseable or outside
    # [cutoff, now+1d] is ignored rather than trusted.
    now = time.time()
    lo, hi = now - UNUSED_DAYS * 86400, now + 86400
    def _ts_ok(line: str) -> bool:
        # Parsed as UTC, not local (Codex 3rd round). hooklog writes UTC with a trailing Z;
        # time.mktime() interprets a naive struct in the MACHINE's timezone, so on Pacific time every
        # record shifted by 7-8 hours — dropping legitimate records near the 30-day boundary and
        # counting ones just outside it. datetime.fromisoformat handles the offset explicitly.
        from datetime import datetime, timezone
        try:
            raw = line.split("|", 1)[0].strip()
            # STRICT to this log's format (Codex 4th round). Bare fromisoformat also accepts naive
            # timestamps and date-only strings, which it then reads as machine-local midnight — the
            # same timezone confusion this replaced. Require an explicit UTC offset and reject
            # anything else rather than guessing what it meant.
            if not (raw.endswith("Z") or raw[-6:-3] in ("+0", "-0") or "+" in raw[10:] or "-" in raw[11:]):
                return False
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return False
            t = dt.astimezone(timezone.utc).timestamp()
        except Exception:
            return False
        return lo <= t <= hi
    for line in _events_text().splitlines():
        if "verdict=capability" not in line:
            continue
        if not _ts_ok(line):
            continue
        kv = {}
        for f in line.split("|")[1:]:
            if "=" in f:
                k, _, v = f.partition("=")
                kv[k.strip()] = v.strip()
        name = kv.get("hook") or ""
        if not name.startswith("capability:"):
            continue
        e = out.setdefault(name[len("capability:"):], {"fires": 0, "sessions": set()})
        e["fires"] += 1
        if kv.get("session"):
            e["sessions"].add(kv["session"])
    return out


def artifact_evidence(artifact_id: str) -> dict:
    """Fires / distinct sessions / span in days for one artifact, from the friction action log."""
    fires, sessions, ts = 0, set(), []
    f = inst.ACTION_LOG
    if not f.is_file():
        return {"fires": 0, "sessions": 0, "span_days": 0.0}
    for line in f.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("artifact_id") != artifact_id or r.get("action") != "fire_inject":
            continue
        fires += 1
        if r.get("session_id"):
            sessions.add(r["session_id"])
        if isinstance(r.get("ts"), int):
            ts.append(r["ts"])
    span = (max(ts) - min(ts)) / 86400.0 if len(ts) >= 2 else 0.0
    return {"fires": fires, "sessions": len(sessions), "span_days": round(span, 2)}


def eligible(art: dict) -> tuple[bool, str, dict]:
    """Has this hooked_skill earned a description-matched activation surface?"""
    if art.get("type") != "hooked_skill":
        return (False, "not a hooked_skill", {})
    aid = art.get("artifact_id", "")
    if not aid.startswith("art_"):
        return (False, "only generated artifacts graduate", {})
    ev = artifact_evidence(aid)
    reasons = []
    if ev["fires"] < MIN_FIRES:
        reasons.append(f"{ev['fires']}/{MIN_FIRES} fires")
    if ev["sessions"] < MIN_SESSIONS:
        reasons.append(f"{ev['sessions']}/{MIN_SESSIONS} sessions")
    if ev["span_days"] < MIN_DAYS:
        reasons.append(f"{ev['span_days']}/{MIN_DAYS} days")
    return (not reasons, "; ".join(reasons) or "eligible", ev)


_WHEN_RE = re.compile(r"^##\s*When this fires\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)


def _when_clause(body: str) -> str:
    """The activation condition, lifted from the procedure body instead of re-invented.

    THE DESCRIPTION IS THE ONLY THING THAT MAKES A SKILL FIRE (2026-08-28). Every generated
    description used to end "Use when the task at hand is exactly this" — which is circular: a
    skill is selected by matching its description against the situation, and that phrase gives
    the matcher nothing to match. All four skills promoted before this date were unfireable, and
    the failure was invisible because the hook they graduated FROM kept firing (promote() never
    deactivates it), so the capability worked and only the second, earlier surface was dead.

    The body already carries a correct, concrete condition under "## When this fires" — the
    procedure renderer derives it from the artifact's `condition` block. Re-inventing it in the
    front matter was the whole defect; this reuses it.
    """
    m = _WHEN_RE.search(body or "")
    if not m:
        return ""
    return " ".join(m.group(1).split()).rstrip(".")


def _render(name: str, ask: str, body: str, aid: str, ev: dict) -> tuple[str, str]:
    """Return (description, file_text). Narrow and non-proactive, but with a REAL trigger."""
    when = _when_clause(body)
    act = (f"Activate when {when[0].lower() + when[1:]}." if when
           else "Activate only when the task at hand matches this exactly.")
    desc = (f"{ask.rstrip('.')}. {act} "
            f"Learned from {ev['fires']} corrections across {ev['sessions']} sessions.")[:300]
    text = (
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        "---\n\n"
        f"{GEN_MARKER} artifact={aid} promoted={time.strftime('%Y-%m-%d')} "
        f"fires={ev['fires']} sessions={ev['sessions']} -->\n\n"
        f"{body.strip()}\n\n"
        "---\n"
        "_Generated by Core's self-improvement loop from a repeatedly-used procedure. It retires "
        "automatically if it stops being used. Edit freely — deleting the marker comment above makes "
        "it hand-authored and permanently exempt from automatic retirement._\n"
    )
    return desc, text


def promote(org: int, dry: bool = False) -> dict:
    out: dict = {"promoted": [], "skipped": [], "demoted": [], "errors": []}
    try:
        arts = inst._load_active().get("artifacts", [])
    except Exception as e:
        out["errors"].append(str(e)[:150])
        return out
    taken = existing_names()
    already = promoted_artifact_ids()
    for art in arts:
        ok, why, ev = eligible(art)
        aid = art.get("artifact_id", "")
        if not ok:
            if art.get("type") == "hooked_skill":
                out["skipped"].append({"artifact_id": aid, "why": why, "evidence": ev})
            continue
        if aid in already:
            out["skipped"].append({"artifact_id": aid, "why": "already has a generated skill"})
            continue
        msg = art.get("effect", {}).get("message") or ""
        ask = msg.split(":", 1)[-1].split(". Follow")[0].strip()
        name = _slug(ask)
        if not _NAME_RE.match(name or ""):
            out["skipped"].append({"artifact_id": aid, "why": f"unusable name {name!r}"})
            continue
        if name in taken:
            out["skipped"].append({"artifact_id": aid, "why": f"name '{name}' already exists"})
            continue
        try:
            body = inst._procedure_path(aid).read_text()
        except Exception:
            out["skipped"].append({"artifact_id": aid, "why": "payload missing"})
            continue
        desc, text = _render(name, ask, body, aid, ev)
        if _BROAD_DESC.search(desc):
            out["skipped"].append({"artifact_id": aid, "why": "description too broad — refused"})
            continue
        ok2, why2 = inst._payload_content_ok(text)
        if not ok2:
            out["skipped"].append({"artifact_id": aid, "why": why2})
            continue
        if dry:
            out["promoted"].append({"artifact_id": aid, "name": name, "dry": True, "evidence": ev})
            continue
        try:
            d = SKILLS_DIR / name
            if d.is_symlink() or (d.exists() and not d.is_dir()):
                out["skipped"].append({"artifact_id": aid, "why": "target exists and is not a directory"})
                continue
            d.mkdir(parents=True, exist_ok=True)
            p = d / "SKILL.md"
            if p.is_symlink():
                out["skipped"].append({"artifact_id": aid, "why": "SKILL.md is a symlink — refusing"})
                continue
            tmp = p.with_name(f".SKILL.md.tmp.{os.getpid()}")
            tmp.write_text(text)
            os.replace(tmp, p)
            taken.add(name)
            inst._log("skill_promote", artifact_id=aid, skill=name, fires=ev["fires"])
            out["promoted"].append({"artifact_id": aid, "name": name, "evidence": ev})
        except Exception as e:
            out["errors"].append(f"{aid}: {str(e)[:120]}")
    out["demoted"] = demote(dry=dry)
    return out


def demote(dry: bool = False) -> list:
    """Retire generated skills that stopped earning their place.

    Only GEN_MARKER files are considered, so a hand-authored skill is untouchable. Archived rather
    than deleted — the file is the evidence for why it was retired.
    """
    gone = []
    if not SKILLS_DIR.is_dir():
        return gone
    # Refuse to interpret silence as disuse unless the recorder is demonstrably working.
    alive, why = telemetry_alive()
    if not alive:
        inst._log("skill_demote_skipped", reason=why)
        return gone
    usage = capability_usage()
    now = time.time()
    for d in sorted(SKILLS_DIR.iterdir()):
        p = d / "SKILL.md"
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        if GEN_MARKER not in text:
            continue  # hand-authored — never auto-retired
        u = usage.get(d.name, {"fires": 0, "sessions": set()})
        age_days = (now - p.stat().st_mtime) / 86400.0
        if u["fires"] == 0 and age_days >= UNUSED_DAYS:
            reason = f"unused for {int(age_days)}d"
            if not dry:
                try:
                    arch = REPO / ".claude" / "state" / "friction-artifacts" / "quarantined"
                    arch.mkdir(parents=True, exist_ok=True)
                    os.replace(p, arch / f"skill-{d.name}.{int(now)}.md")
                    try:
                        d.rmdir()
                    except OSError:
                        pass
                    inst._log("skill_demote", skill=d.name, reason=reason)
                except Exception:
                    continue
            gone.append({"skill": d.name, "reason": reason, "dry": dry})
    return gone


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually promote/demote (default: dry)")
    a = ap.parse_args()
    from _env import get_org_id  # org from the ONE resolver (identity wins over a leaked env) — never a bare `, "1"` default
    org = get_org_id()
    print(json.dumps(promote(org, dry=not a.apply), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
