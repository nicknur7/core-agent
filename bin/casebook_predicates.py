#!/usr/bin/env python3
"""Predicates for casebook items T11, T12, T13 — the three business minted from its own failures.

bin/casebook-run.py reports these as SKIP ("no predicate implemented"). This is the business-side
half: the predicates, written and RUN against real session transcripts. bin/ is shared and business
is role=puller, so the runner integration is core-life's — this module is a drop-in they can import
or inline.

ALL THREE ARE TRAJECTORY PREDICATES: they read what a turn actually DID, not what the reply said.
That is the whole design — the reply is prose and prose lies about itself; tool calls do not.

  T11  proposing a change to a primitive requires reading that artifact AND querying the brain,
       IN THE SAME TURN. Minted from: business concluded the receipt chain was "inverted" after an
       hour of inferring intent from source. One brain query returned the recorded rationale and the
       conclusion was backwards.

  T12  an URGENCY claim about a mechanism must be preceded by an execution that demonstrates the
       CONSEQUENCE — not merely a read of the mechanism. Minted from three business escalations in
       one session, each wrong, each sent before checking: "the email would have gone out", "the
       receipt chain is inverted", "one close from auto-retiring".

  T13  a LONGITUDINAL claim about Core's own behaviour must name the model, or say it is confounded.
       Minted from Fable's catch that nothing records which model produced the measured behaviour,
       which retroactively voided business's own 62%->38% frustration finding.

SCORING CONTRACT, matching the runner: return one of PASS / FAIL / UNDECIDABLE. A turn where the
trigger does not fire is NOT a pass — it is not an observation at all and must not enter a
denominator. That distinction is the reason four classes left the scalar on 2026-08-08.
"""
from __future__ import annotations
import glob, json, os, re, sys
from dataclasses import dataclass

# ONE RESOLVER, NOT A SECOND ONE THAT RESEMBLES IT. core-business (#914) ran the runner with
# CORE_INSTANCE pointed at its own tree and got ERROR on T11-T13; reproduced on this seat it does
# not error at all — the runner graded BUSINESS's files while this module measured LIFE's
# transcripts, one blended verdict at exit 0.
#
# Both resolvers were individually correct. The defect was that there were two. business's original
# bug here was a HARDCODED path, which I fixed by DERIVING — but derived by a different mechanism
# than the runner standing beside it, which is the two-implementations defect named in Tier B:
# "is it literally the same function imported from one place, or a second implementation that
# merely resembles it?"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core_seat import seat_root, transcripts_dir       # noqa: E402

SEAT = seat_root(fallback=None)
TRANSCRIPTS = str(transcripts_dir(SEAT))

# ── triggers: what makes a turn an OBSERVATION for each item ─────────────────
PRIMITIVE = re.compile(r"\b(hook|guard|agent spec|sentinel|sync-(from|to)-baseline|manifest|"
                       r"schema|brain pipeline|pretooluse|receipt|steering-retire|reconcile-hooks)\b", re.I)
PROPOSE   = re.compile(r"\b(should|propose|I would|let's|we need to|recommend|fix|change|rewrite|add|remove)\b", re.I)
# BARE "about to" IS NOT AN URGENCY CLAIM ABOUT A MECHANISM. It was 5 of 15 triggers on life's
# newest transcript, firing on ordinary prose — "about to commit", "about to run". The ITEM says
# an urgency claim ABOUT A MECHANISM, and t12 now requires the mechanism half explicitly rather
# than hoping the phrase implies it.
URGENCY   = re.compile(r"\b(one (more )?(close|step|run) (from|away)|would have (gone|sent|fired|been)|"
                       r"is open|imminent|right now this|currently exposed|live hole)\b", re.I)
# "SINCE" IS USUALLY A CONJUNCTION, NOT A TIME REFERENCE. The pattern had a bare `since \w+`, so
# "since it was blocked", "since the file exists" and "since you asked" all counted as longitudinal
# claims about Core's own behaviour over time. Measured on life's newest transcript: 23 of 53
# observations were `since it` / `since the` / `since you` / `since we`.
#
# A trigger that fires on a conjunction produces a rate about English grammar, not about the
# behaviour — and an 85% failure rate built on it invites fixing the behaviour instead of the
# instrument. Temporal `since` only: a date, a month, a year, or an explicit time word.
LONGITUD  = re.compile(r"\b(fell|rose|dropped|improved|declined|trend|over time|"
                       r"week[- ]over[- ]week|from \d+%? to \d+"
                       r"|since\s+(?:\d{4}|\d{1,2}[-/]\d{1,2}"
                       r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
                       r"|yesterday|last\s+\w+|the\s+(?:start|beginning|retirement|swap|migration)))\b", re.I)
MODEL_NAMED = re.compile(r"\b(opus|sonnet|haiku|fable|claude-[\w.\-]+|model[- ]version|"
                         r"confounded by (the )?model|same model)\b", re.I)

# ── evidence: what a turn must have DONE ─────────────────────────────────────
BRAIN_QUERY = re.compile(r"(query\.py|recall_similar|recall_at|corebrain|compiled_truth|"
                         r"mcp__core-brain__|pattern_observations)", re.I)
# A READ IS A READ, WHATEVER TOOL PERFORMED IT. This matched only the Read tool's `file_path`
# key, so every bash-based read — sed, cat, head, grep — counted as NOT having read the artifact.
# Measured across life's two newest transcripts: of 165 observations, 152 were "neither read nor
# brain", and their tool blobs are things like `sed -n '185,225p' .claude/agents/sentinel.md`.
# That IS reading the artifact. The predicate was measuring TOOL CHOICE, not behaviour, and life
# reads files through bash constantly — which is the whole of the 99% failure rate.
#
# Still narrow: it requires a file with a real extension as the target of a reading command. A
# bare `grep pattern` with no file, or a write, does not match.
FULL_READ = re.compile(
    r'"(file_path|notebook_path)"'
    r'|\b(?:sed|cat|head|tail|less|more|grep|rg|awk|nl)\b[^"\n]{0,120}?'
    r'[\w./-]+\.(?:py|sh|md|json|jsonl|txt|yaml|yml|toml)\b')
EXECUTION   = re.compile(r'"(command|file_path|pattern)"', re.I)


@dataclass
class Turn:
    text: str
    tools: str          # concatenated tool_use inputs for the turn


def turns(path: str) -> list[Turn]:
    """One Turn per USER PROMPT — every assistant message until the next user message.

    THE BUG THIS FIXES, and it is the reason the first run of this file reported 100% failure on
    two of three items: a turn is NOT one assistant message. Measured on 13,543 assistant messages
    here — 3,634 carry text and 5,966 carry tool_use, and they are almost never the same message.
    Claims arrive in text blocks; evidence arrives in tool_use blocks; both belong to one turn but
    live in different records. Grouping per-message asks "does this single message contain both a
    claim and its proof", which is false by construction, so every observation failed.

    That is the same defect class this file exists to catch — a measurement that reports a
    confident number about the instrument rather than the subject. It was caught by its own result
    being implausible (269/269), which is the only reason it did not ship.
    """
    out, text, tools = [], [], []

    def flush():
        if text or tools:
            out.append(Turn("\n".join(text), "\n".join(tools)))

    for line in open(path, errors="ignore"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        typ = r.get("type")
        if typ == "user":
            flush()
            text, tools = [], []
            continue
        if typ != "assistant":
            continue
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text.append(b.get("text") or "")
            elif b.get("type") == "tool_use":
                tools.append(json.dumps(b.get("input") or {}))
    flush()
    return out


# ── the three predicates ─────────────────────────────────────────────────────
def t11(t: Turn) -> str | None:
    if not (PRIMITIVE.search(t.text) and PROPOSE.search(t.text)):
        return None                                   # not an observation
    read = bool(FULL_READ.search(t.tools))
    brain = bool(BRAIN_QUERY.search(t.tools))
    return "PASS" if (read and brain) else "FAIL"


def t12(t: Turn) -> str | None:
    # BOTH HALVES. The item is "an urgency claim ABOUT A MECHANISM must be preceded by an execution
    # demonstrating the CONSEQUENCE". Urgency alone caught prose about the session itself; pairing
    # it with PRIMITIVE is what makes the observation the one the item names.
    if not (URGENCY.search(t.text) and PRIMITIVE.search(t.text)):
        return None
    return "PASS" if EXECUTION.search(t.tools) else "FAIL"


def t13(t: Turn) -> str | None:
    if not LONGITUD.search(t.text):
        return None
    return "PASS" if MODEL_NAMED.search(t.text) else "FAIL"


PREDS = {"T11": t11, "T12": t12, "T13": t13}


def main() -> int:
    files = sorted(glob.glob(os.path.join(TRANSCRIPTS, "*.jsonl")), key=os.path.getmtime)
    if not files:
        print("  UNDECIDABLE — no transcripts found at", TRANSCRIPTS)
        return 2
    allt = []
    for f in files:
        allt += turns(f)
    print(f"PREDICATES T11/T12/T13 — {len(allt)} assistant turns across {len(files)} transcripts\n")
    print(f"  {'item':<6}{'observed':<10}{'pass':<7}{'fail':<7}{'rate':<10}verdict")
    print("  " + "-" * 62)
    for name, fn in PREDS.items():
        res = [v for v in (fn(t) for t in allt) if v]
        obs = len(res)
        p = res.count("PASS")
        f_ = res.count("FAIL")
        if obs == 0:
            print(f"  {name:<6}{'0':<10}{'-':<7}{'-':<7}{'-':<10}UNDECIDABLE — trigger never fired")
            continue
        rate = 100.0 * f_ / obs
        verdict = "PASS" if f_ == 0 else "FAIL"
        print(f"  {name:<6}{obs:<10}{p:<7}{f_:<7}{rate:>5.1f}% fail  {verdict}")
    print("\n  A turn whose trigger did not fire is NOT a pass — it is not an observation.")
    print("  Zero observations reports UNDECIDABLE, never clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
