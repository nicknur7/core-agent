#!/usr/bin/env python3
"""Safety proof for autonomous skill graduation (2026-07-27).

Promotion writes a file into .claude/skills/, which gives it a description-matched activation
surface that no gate can test in advance. The safety argument is therefore entirely about (a) what
must be earned before a file is written, (b) what the written description may say, and (c) what can
never be deleted. Those are what this file attacks.

The single most important property: a HAND-AUTHORED skill must be untouchable. Demotion keys on a
generated marker, so if that check ever regresses, this suite fails loudly rather than silently
deleting something Nick wrote.

  python3 tests/test_skill_graduate.py
"""
import json
import os
import sys as _sys
from pathlib import Path as _PPath
import sys
import time
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_TMP = tempfile.mkdtemp()
# CORE_INSTANCE MUST BE REDIRECTED TOO, NOT JUST CLAUDE_PROJECT_DIR.
# These tests isolate by overriding CLAUDE_PROJECT_DIR. The modules under test resolve their root
# from CORE_INSTANCE, falling back to their OWN repo root when it is unset — so the isolation
# covered the variable the test controls and missed the one the code follows, and every run wrote
# its fixtures into the LIVE seat. core-life's .claude/state/friction-artifacts/active.json was
# found holding a single artifact `blk` / "should not block" stamped org 2, and core-business's held
# three stamped org 1: each seat carrying the other's test fixtures. Found by core-business,
# bus #1039/#1040.
#
# active.json is a projection of Postgres, so nothing was lost — `si_project.project(org)` rebuilt
# 22 artifacts. That is luck about this particular file, not a property of the isolation.
os.environ["CLAUDE_PROJECT_DIR"] = _TMP
os.environ["CORE_INSTANCE"] = _TMP
# RESOLVED, NOT PINNED. This was `= "1"` — a hard assignment, which reads as a fixture
# constructing a sandbox and is actually the org pin in disguise. It survived the sweep
# that removed the `setdefault` pins, and core-school proved it live: they pulled the
# "fix", two of three tests went green, and this one still failed on org mismatch.
# Resolved from the REAL repo root, not from CORE_INSTANCE, because the lines above
# have already redirected that to a temp tree with no identity.json.
_sys.path.insert(0, str(_PPath(__file__).resolve().parents[3] / "scheduling" / "brain-pg"))
from _env import get_org_id as _goi
os.environ["CORE_ORG_ID"] = str(_goi(_PPath(__file__).resolve().parents[3]))
(Path(_TMP) / ".claude" / "state").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / ".claude" / "skills").mkdir(parents=True, exist_ok=True)

import importlib                    # noqa: E402
import friction_installer as inst   # noqa: E402
import skill_graduate as sg         # noqa: E402

# THIS SEAT'S ORG AS THE ARGUMENT. A literal here is the same defect as a literal in a spec:
# on any seat whose org is not 1 the call operates on — and in promote/generate paths WRITES
# to — life's partition. Found by sweeping the ARGUMENT side after core-school showed the
# spec-side sweep had missed it.
import sys as _sys2
from pathlib import Path as _PP2
_sys2.path.insert(0, str(_PP2(__file__).resolve().parents[3] / "scheduling" / "brain-pg"))
from _env import get_org_id as _goi2  # noqa: E402
ORG = _goi2(_PP2(__file__).resolve().parents[3])
importlib.reload(inst)
importlib.reload(sg)

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _art(aid="art_grad_test_0001", typ="hooked_skill", msg="Recurring ask (9x): keep the widget log tidy. Follow the procedure at x.md"):
    return {"artifact_id": aid, "type": typ, "effect": {"mode": "inject", "message": msg},
            "_installed_at": 1}


def _log_fires(aid, n, sessions, day_span):
    """Write n fire_inject rows across `sessions` distinct sessions spanning day_span days."""
    now = int(time.time())
    with open(inst.ACTION_LOG, "a") as f:
        for i in range(n):
            ts = now - int(day_span * 86400) + int(i * (day_span * 86400) / max(1, n - 1)) if n > 1 else now
            f.write(json.dumps({"action": "fire_inject", "artifact_id": aid,
                                "session_id": f"s{i % sessions}", "ts": ts}) + "\n")


print("\n=== the promotion window must actually gate ===")
inst.ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
ok, why, ev = sg.eligible(_art())
check("no evidence -> not eligible", not ok, why)

_log_fires("art_grad_test_0001", 4, 3, 9)
importlib.reload(sg)
ok, why, _ = sg.eligible(_art())
check("4 fires (<5) -> still refused", not ok, why)

_log_fires("art_grad_test_0002", 6, 1, 9)
ok, why, _ = sg.eligible(_art(aid="art_grad_test_0002"))
check("6 fires but 1 session -> refused", not ok, why)

_log_fires("art_grad_test_0003", 6, 3, 0)
ok, why, _ = sg.eligible(_art(aid="art_grad_test_0003"))
check("6 fires, 3 sessions, same day -> refused (span)", not ok, why)

_log_fires("art_grad_test_0004", 6, 3, 9)
ok, why, ev = sg.eligible(_art(aid="art_grad_test_0004"))
check("6 fires / 3 sessions / 9 days -> ELIGIBLE", ok, why)

print("\n=== only generated artifacts graduate ===")
ok, why, _ = sg.eligible(_art(aid="legacy_recall-first"))
check("legacy_ artifact never graduates", not ok, why)
ok, why, _ = sg.eligible(_art(typ="contract"))
check("a plain contract never graduates", not ok, why)

print("\n=== the written description must stay narrow ===")
desc, text = sg._render("widget-log", "keep the widget log tidy", "## Steps\n\n1. do it\n",
                        "art_grad_test_0004", {"fires": 6, "sessions": 3})
check("description has no proactive-activation phrasing", not sg._BROAD_DESC.search(desc), desc)
check("generated file carries the marker", sg.GEN_MARKER in text)
check("frontmatter has name + description", text.startswith("---\nname: widget-log\ndescription:"))
for bad in ("PROACTIVELY ACTIVATE when the user mentions anything",
            "Always use this before responding to any prompt",
            "Use this whenever the user says something"):
    check(f"refuses broad phrasing: {bad[:34]}", bool(sg._BROAD_DESC.search(bad)))

print("\n=== promotion writes a real skill, once ===")
inst.PROCDIR.mkdir(parents=True, exist_ok=True)
inst.write_procedure("art_grad_test_0004", "## Keep the widget log tidy\n\n1. rotate it\n2. compress it\n")
inst._atomic_write(inst.ACTIVE, {"artifacts": [_art(aid="art_grad_test_0004")]})
r = sg.promote(ORG, dry=True)
check("dry run promotes nothing to disk", r["promoted"] and r["promoted"][0].get("dry") is True)
check("dry run wrote no file", not (sg.SKILLS_DIR / "keep-widget-log-tidy" / "SKILL.md").is_file())

r = sg.promote(ORG, dry=False)
promoted = [p["name"] for p in r["promoted"]]
check("promotion wrote a skill", bool(promoted), str(r))
if promoted:
    p = sg.SKILLS_DIR / promoted[0] / "SKILL.md"
    check("SKILL.md exists on disk", p.is_file())
    check("it is markdown, not code", p.read_text().lstrip().startswith("---"))

r2 = sg.promote(ORG, dry=False)
check("re-running does not duplicate (name already taken)",
      not r2["promoted"] and any("already exists" in s.get("why", "") for s in r2["skipped"]), str(r2))

print("\n=== a HAND-AUTHORED skill can never be auto-retired ===")
hand = sg.SKILLS_DIR / "nicks-own-skill"
hand.mkdir(parents=True, exist_ok=True)
(hand / "SKILL.md").write_text("---\nname: nicks-own-skill\ndescription: written by hand\n---\n\nsteps\n")
old = time.time() - (sg.UNUSED_DAYS + 5) * 86400
os.utime(hand / "SKILL.md", (old, old))
gone = sg.demote(dry=False)
check("hand-authored skill NOT retired despite being old and unused",
      (hand / "SKILL.md").is_file(), str(gone))
check("demote reported nothing for it", not any(g["skill"] == "nicks-own-skill" for g in gone))

print("\n=== a generated skill IS retired when unused, and archived not deleted ===")
# Demotion now requires proof the recorder is working (telemetry_alive), so a retire-path test must
# supply a live events log. Without this the code correctly refuses and the test would be asserting
# the wrong thing.
_now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
sg.EVENTS_LOG.write_text(
    "".join(f"{_now} | hook=some-hook | event=Stop | verdict=pass | session=s{i} | excerpt=\n"
            for i in range(40))
    # a capability record for a DIFFERENT skill: proves the recorder works without giving the skill
    # under test any usage of its own
    + f"{_now} | hook=capability:unrelated | event=Skill | verdict=capability | session=sX | excerpt=\n")
importlib.reload(sg)
if promoted:
    gp = sg.SKILLS_DIR / promoted[0] / "SKILL.md"
    os.utime(gp, (old, old))
    gone = sg.demote(dry=False)
    check("generated + unused + old -> retired", any(g["skill"] == promoted[0] for g in gone), str(gone))
    check("file removed from skills dir", not gp.is_file())
    arch = Path(_TMP) / ".claude" / "state" / "friction-artifacts" / "quarantined"
    check("archived, not deleted", any(promoted[0] in f.name for f in arch.glob("skill-*.md")))

print("\n=== dead telemetry must NOT archive a GENERATED skill (the path that was broken) ===")
# sentinel-code, 2026-07-27: the original test only covered a HAND-AUTHORED skill under empty
# telemetry, which was already protected by the GEN_MARKER check — so it passed while the real
# at-risk path was broken. With an empty events log, capability_usage() returns {}, every generated
# skill defaulted to fires=0, and all of them older than UNUSED_DAYS were archived. A dead recorder
# would have wiped every earned skill. This constructs exactly that case.
gen = sg.SKILLS_DIR / "earned-skill"
gen.mkdir(parents=True, exist_ok=True)
(gen / "SKILL.md").write_text(
    "---\nname: earned-skill\ndescription: a skill that earned its place\n---\n\n"
    f"{sg.GEN_MARKER} artifact=art_x promoted=2026-01-01 fires=9 sessions=4 -->\n\nsteps\n")
old2 = time.time() - (sg.UNUSED_DAYS + 10) * 86400
os.utime(gen / "SKILL.md", (old2, old2))

sg.EVENTS_LOG.write_text("")                      # recorder produced nothing
importlib.reload(sg)
alive, why = sg.telemetry_alive()
check("empty events log -> telemetry NOT alive", not alive, why)
sg.demote(dry=False)
check("generated skill SURVIVES a dead recorder", (gen / "SKILL.md").is_file())

if sg.EVENTS_LOG.exists():
    sg.EVENTS_LOG.unlink()                        # recorder missing entirely
importlib.reload(sg)
alive, _ = sg.telemetry_alive()
check("missing events log -> telemetry NOT alive", not alive)
sg.demote(dry=False)
check("generated skill SURVIVES a missing recorder", (gen / "SKILL.md").is_file())

# A BUSY log that carries no capability records must still refuse: it proves the hook pipeline is
# alive, not that capability-usage-log — the only producer of the signal demotion reads — is.
now_day = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
sg.EVENTS_LOG.write_text("".join(
    f"{now_day} | hook=some-hook | event=Stop | verdict=pass | session=s{i} | excerpt=\n"
    for i in range(40)))
importlib.reload(sg)
alive, why = sg.telemetry_alive()
check("busy log with NO capability records -> still not alive", not alive, why)
sg.demote(dry=False)
check("generated skill SURVIVES when only the capability recorder is broken", (gen / "SKILL.md").is_file())

# and now with a demonstrably-live capability recorder, the same skill IS retired
sg.EVENTS_LOG.write_text("".join(
    f"{now_day} | hook=some-hook | event=Stop | verdict=pass | session=s{i} | excerpt=\n"
    for i in range(40))
    + f"{now_day} | hook=capability:something-else | event=Skill | verdict=capability | session=sX | excerpt=\n")
importlib.reload(sg)
alive, why = sg.telemetry_alive()
check("busy events log -> telemetry alive", alive, why)
gone2 = sg.demote(dry=False)
check("with a live recorder, the unused generated skill IS retired",
      any(g["skill"] == "earned-skill" for g in gone2), str(gone2))

print("\n=== no telemetry means no autonomous removal ===")
inst.ACTION_LOG.write_text("")
(Path(_TMP) / ".claude" / "state" / "hook-events.log").write_text("")
importlib.reload(sg)
hand2 = sg.SKILLS_DIR / "another-hand-skill"
hand2.mkdir(parents=True, exist_ok=True)
(hand2 / "SKILL.md").write_text("---\nname: another-hand-skill\ndescription: hand\n---\n\nx\n")
os.utime(hand2 / "SKILL.md", (old, old))
check("empty telemetry retires nothing hand-authored", (hand2 / "SKILL.md").is_file())

print()
if _fails:
    print(f"FAILURES ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL PASS")
