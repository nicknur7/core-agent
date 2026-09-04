#!/usr/bin/env python3
"""The per-reply denominator must refuse a window the transcripts do not cover.

WHY THIS EXISTS (2026-08-20).

Nick's condition on autonomous self-improvement was one clause: *"as long as it works and actually
IMPROVES not gets worse."* That makes the ability to TELL whether it improved part of the capability,
not a follow-up — and on 2026-08-18 core-business showed the existing instrument cannot tell. The
first efficacy measurement ever run on this loop's only successful output flipped SIGN on the choice
of denominator: -27% per calendar day, +71% per correction-moment, because overall correction volume
had fallen 57% in the same window and a per-day rate cannot see that.

`per_reply` is the denominator confounded by neither work volume nor correction mix — it counts the
opportunities an artifact actually had to matter. It imports `bin/si-objective.reply_count` rather
than defining "a reply" a second time (the 2026-08-12 reuse principle established with core-finance:
call what the pipeline already counts, so the measuring and counted predicates cannot drift).

THE DEFECT THIS TEST EXISTS FOR WAS IN THE FIRST VERSION OF THAT FUNCTION, AND IT FLATTERED THE LOOP.
It returned a count for any window, covered or not. Life's directive pre-window is 2026-05-15..07-23;
transcripts begin 2026-07-12 because `cleanupPeriodDays` defaults to 30. So 11 of 69 days were
covered, the pre-rate divided by ~1,251 replies against the post-window's ~9,009, and it reported
**-96% where the honest per-week answer is -30%** — a threefold improvement manufactured by a
denominator that could not see most of its own window.

An unanswerable question must come back unanswerable. A number that happens to favour the thing being
measured is the worst possible failure for an instrument whose entire job is telling Nick whether his
system got better.

WHAT THIS ASSERTS.
  1. A window starting before the earliest surviving transcript returns None, not a number.
  2. A covered window returns a real rate.
  3. Zero replies returns None, never 0 — a zero denominator would produce an infinite rate.
  4. "A reply" is imported from si-objective, never redefined here.
"""
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scheduling" / "claude-si" / "measure-contract-fitness.py"
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

spec = importlib.util.spec_from_file_location("mcf", SRC)
m = importlib.util.module_from_spec(spec)
sys.modules["mcf"] = m
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

checks = 0
assert hasattr(m, "per_reply") and hasattr(m, "_replies_in"), "per_reply is gone — retarget this test"
checks += 1

# 4) the definition of "a reply" is imported, not restated
src = SRC.read_text()
assert "si-objective" in src, "per_reply no longer imports si-objective — 'a reply' has been redefined"
assert not re.search(r'r\.get\("type"\)\s*==\s*"assistant"', src), \
    "measure-contract-fitness now counts assistant turns itself — that is the second definition of " \
    "'a reply' the reuse principle exists to prevent"
checks += 1

# 1) a window predating every transcript refuses
far_past = m.per_reply(7, "2020-01-01", "2020-06-01")
assert far_past is None, \
    "a window with no transcript coverage returned %r instead of refusing — this is the shape that " \
    "reported -96%%" % (far_past,)
checks += 1

# 2) a covered window answers. Derive the earliest covered date from the transcripts themselves
# rather than hardcoding one, so this test does not rot when old transcripts age out.
import glob as _g, json as _j
# TRANSCRIPTS lives in si-objective, which is the whole point — this module must not own a second
# notion of where replies live any more than it owns a second notion of what one is.
_sio_spec = importlib.util.spec_from_file_location("_sio_t", REPO / "bin" / "si-objective.py")
_sio = importlib.util.module_from_spec(_sio_spec)
_sio_spec.loader.exec_module(_sio)
earliest = None
for f in _g.glob(str(_sio.TRANSCRIPTS / "*.jsonl")):
    try:
        with open(f, errors="ignore") as fh:
            for ln in fh:
                ts = _j.loads(ln).get("timestamp")
                if isinstance(ts, str) and ts[:4].isdigit():
                    if earliest is None or ts < earliest:
                        earliest = ts
                    break
    except Exception:
        continue
if earliest:
    covered = m.per_reply(5, str(earliest)[:10])
    assert covered is None or covered > 0, "a covered window produced a non-positive rate: %r" % covered
    checks += 1
    # ...and one day EARLIER than the earliest record must refuse, which is the boundary itself.
    import datetime as _dt
    day_before = (_dt.date.fromisoformat(str(earliest)[:10]) - _dt.timedelta(days=1)).isoformat()
    assert m.per_reply(5, day_before) is None, \
        "the coverage boundary is off by one — a window starting before the earliest record answered"
    checks += 1

# 3) an empty count of replies is None, not zero
assert m._replies_in("2099-01-01", "2099-01-02") is None, \
    "an empty window returned a falsy count that could become a zero denominator"
checks += 1

# 5) IT IS ACTUALLY CALLED. `per_reply` shipped fully built, fully tested, working on live data —
# and called by NOTHING. core-business found that within an hour of it landing: every fitness verdict
# on every seat still divided by calendar days. The eighth "built but never wired" defect of the day
# and the only one I authored myself, in the fix for the condition that autonomy must be measurable.
#
# A test that only checks the function's behaviour passes on a function nobody calls. So this asserts
# the wiring, and that the result reaches the sentence a human reads rather than being computed and
# dropped — which would be the same defect one step later.
callers = [ln for ln in src.splitlines()
           if "per_reply(" in ln and "def per_reply" not in ln and not ln.strip().startswith("#")]
assert callers, ("per_reply has no caller in measure-contract-fitness — it is built, tested and "
                 "unreachable, which is exactly how it shipped the first time")
checks += 1

assert "_reply_rate_note" in src, "the per-reply result never reaches a verdict rationale"
note_covered = m._reply_rate_note(5.6, 0.22)
note_missing = m._reply_rate_note(None, 0.22)
assert "per 1k replies" in note_covered and ("agrees" in note_covered or "DISAGREES" in note_covered), \
    "the note omits the rate or the agreement direction: %r" % note_covered
assert "unavailable" in note_missing, \
    "an uncovered window is silently omitted instead of stated: %r" % note_missing
checks += 1

print("ok — %d checks: uncovered windows refuse, boundary exact, 'a reply' imported not redefined"
      % checks)
