#!/usr/bin/env python3
"""An existence question must not be answered from a random sample.

WHY THIS EXISTS (2026-08-20).

`trigger_is_unreachable` retires an artifact whose prompt conjunction matches NOTHING in the corpus
it was mined from — the retire-side counterpart to the install-side sensitivity floor, built after
core-ops measured an installed workflow with ZERO fires in four days and life found 6 of its own 18
prompt-conditioned artifacts in the same state.

THE FIRST VERSION FED IT `friction_installer._fetch_corpus_prompts`, WHICH IS
`ORDER BY random() LIMIT 150`.  # privacy-ok: SQL LIMIT clause, not a course code

That helper is correct for the SPECIFICITY gate: "does this fire too often" is a RATE question, and a
random sample estimates a rate. "Can this ever fire" is an EXISTENCE question. A rule matching 1
prompt in 400 usually misses a 150-row random draw — so the flag would flip between runs and retire
working artifacts on sampling luck.

Caught before shipping: the first run flagged two artifacts that a full-corpus check showed matching
1 and 9 times. A false positive in the dangerous direction — the signal RETIRES things.

WHAT THIS ASSERTS.
  1. The tune pass does not source the unreachable check from the sampling helper.
  2. Its query has no LIMIT and no ORDER BY random().
  3. `trigger_is_unreachable` is undecidable, not clean, on a corpus below MIN_CORPUS — a thin seat
     (finance runs at ~36 observations) must not have working artifacts retired for lack of data.
  4. It returns None for artifacts with no prompt legs — tool-shaped rules cannot be "unreachable"
     in this sense and must never be flagged by it.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SI = REPO / "scheduling" / "claude-si"
sys.path.insert(0, str(SI))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

import friction_loop as F  # noqa: E402

checks = 0
src = (SI / "friction_loop.py").read_text()

# --- 1 & 2) the corpus feeding the unreachable check is the FULL set -----------------------------
tune = src[src.index("def tune_pass("):]
tune = tune[:tune.index("\ndef ", 10)]
assign = re.search(r"_tune_corpus\s*=\s*(.+)", tune)
assert assign, "tune_pass no longer builds _tune_corpus — retarget this test"
# Strip comments before checking — the code explains WHY it does not use the sampler, and a bare
# substring search matches that prose. A test that fails on its own subject's documentation is a
# test measuring the wrong text.
tune_code = "\n".join(ln.split("#", 1)[0] for ln in tune.splitlines())
assert "_fetch_corpus_prompts" not in tune_code, (
    "tune_pass sources the unreachable check from _fetch_corpus_prompts, which is "
    "`ORDER BY random() LIMIT 150` — a RATE sampler answering an EXISTENCE question. The flag would "
    "flip between runs and retire working artifacts on sampling luck.")
checks += 1

qry = re.search(r'"SELECT [^"]*FROM pattern_observations "[\s\S]{0,400}?\(org,\)\)', tune)
assert qry, "could not locate the corpus query in tune_pass — retarget"
q = qry.group(0)
assert "LIMIT" not in q.upper() and "RANDOM()" not in q.upper(), \
    "the unreachable-check corpus query samples or truncates: %s" % q[:160]
checks += 1

# --- 3) undecidable on a thin corpus, not clean --------------------------------------------------
import friction_test_gate as tg  # noqa: E402

art = {"condition": {"all": [{"op": "prompt_regex", "value": r"\bzzunmatchable\b"}]}}
thin = ["nothing here matches"] * (tg.MIN_CORPUS - 1)
assert F.trigger_is_unreachable(art, thin) is None, \
    "flagged an artifact against a corpus below MIN_CORPUS — a thin seat would have working " \
    "artifacts retired for lack of data, not for lack of reachability"
fat = ["nothing here matches"] * (tg.MIN_CORPUS + 10)
assert F.trigger_is_unreachable(art, fat) is not None, \
    "a genuinely unreachable trigger was not flagged on a sufficient corpus"
checks += 1

# --- 4) tool-shaped artifacts are out of scope ---------------------------------------------------
tool_art = {"condition": {"all": [{"op": "event_is", "value": "PreToolUse"},
                                  {"op": "tool_name_in", "value": ["Edit"]}]}}
assert F.trigger_is_unreachable(tool_art, fat) is None, \
    "flagged a tool-shaped artifact — it has no prompt condition to be unreachable about, and the " \
    "four skills installed on life are exactly this shape"
checks += 1

# --- 5) a reachable conjunction is never flagged -------------------------------------------------
# A WELL-MATCHED conjunction is clean. This originally asserted that ONE match was enough, which
# encoded the belief core-business falsified: reachable is not the same as alive. One match now
# returns a `fragile` payload (checked below), so this case uses a genuinely well-matched rule.
ok_art = {"condition": {"all": [{"op": "prompt_regex", "value": r"\bverify\b"},
                                {"op": "prompt_regex", "value": r"\bclaim\b"}]}}
corpus = ["please verify that claim first"] * 5 + ["unrelated"] * (tg.MIN_CORPUS + 10)
assert F.trigger_is_unreachable(ok_art, corpus) is None, \
    "flagged a well-matched conjunction — 5 matching prompts is unambiguously reachable"
checks += 1

# --- 6) REACHABLE IS NOT ALIVE, AND FRAGILE IS NEVER RETIRED ------------------------------------
#
# The exemplar this detector was built around falsified its own diagnosis. Life read ops's
# `art_wf72bf83d8ec6f7b5e` — zero fires in four days — as an unsatisfiable conjunction, FROM READING
# ITS CONDITION. ops ran the check on its own seat: it matches 2 of 107 prompts. Reachable. The real
# explanation is a rate — 1.9% of history means four quiet days is what RARE looks like, not broken.
#
# core-business found the general shape: one of its four survivors hangs on a SINGLE corpus row, and
# on life 6 of 19 prompt-conditioned artifacts match two or fewer. A binary verdict treats 1 match
# and 18 identically. So thinness is REPORTED and never acted on — where to draw a fragility line is
# a judgement, and inventing a threshold inside a detector is how a measurement becomes a policy
# nobody chose.
thin_corpus = ["please verify that claim first"] + ["unrelated text here"] * (tg.MIN_CORPUS + 10)
thin = F.trigger_is_unreachable(ok_art, thin_corpus)
assert thin is not None and thin.get("reachable") is True, \
    "an artifact matching exactly one row reports as clean — the fragile tail becomes invisible"
assert thin.get("hits") == 1, "hit count not reported: %r" % thin
checks += 1

health_src = (SI / "friction_loop.py").read_text()
frag = health_src[health_src.index("_unreach = trigger_is_unreachable("):]
frag = frag[:frag.index("if _unreach:")]
assert '"action": "none"' in frag, \
    "the fragile branch takes an action — reachable-but-rare must never be retired, which is exactly " \
    "the mistake made about ops's exemplar"
checks += 1

# --- 7) THE CORPUS COLUMN MUST BE THE ONE THE RUNTIME MATCHES ------------------------------------
#
# `pattern_observations.prompt_text` is the PRECEDING turn's prompt. `correction_text` is what Nick
# actually typed. The dispatcher matches `prompt_regex` against `ctx["prompt_text"]`, set from
# `payload["prompt"]` — the CURRENT user message — so the runtime corresponds to this table's
# `correction_text`. `ask_miner._member_prompts:562` already coalesces for exactly this reason:
# preferring `prompt_text` "grounded every trigger in text unrelated to the ask" (2026-07-27).
#
# core-business found both corpus queries using the wrong one. On life the reachability check
# reported 2 dead / 4 fragile against `prompt_text` and 0 / 0 against the right column — the entire
# finding was an artifact of the query, and it had already been published to four seats.
for path, what in ((SI / "friction_loop.py", "the unreachable check"),
                   (SI.parent / "claude-si" / "friction_installer.py", "the specificity gate")):
    text = path.read_text()
    # Scoped to queries that BUILD A PROMPT CORPUS — ones whose payload is prompt_text. Other
    # selects from this table (source_uuid, pattern_label, canonical_ask) are unrelated to regex
    # matching and must not be caught; the first version of this assertion flagged them and would
    # have been a test demanding unrelated code change to satisfy it.
    for m in re.finditer(r'"SELECT ([^"]*?) FROM pattern_observations', text):
        sel = m.group(1)
        if "prompt_text" not in sel:
            continue
        assert "correction_text" in sel, (
            "%s builds a prompt corpus from %r — that is the PRECEDING turn. The runtime matches "
            "the CURRENT user message, which is this table's correction_text." % (what, sel[:70]))
checks += 1

print("ok — %d checks: full corpus not a sample, undecidable below MIN_CORPUS=%d, tool-shaped exempt"
      % (checks, tg.MIN_CORPUS))
