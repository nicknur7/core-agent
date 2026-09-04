#!/usr/bin/env python3
"""UserPromptSubmit hook — detects verification triggers in Nick's prompt
and injects a strong reminder that the next response MUST start with a
tool call verifying the referenced thing — not paraphrase from memory.

Implements CLAUDE.md anti-pattern rule #2 structurally.

Conservative trigger list — accepts misses, avoids most false positives.
Output via hookSpecificOutput.additionalContext, never blocks the prompt.
"""
import json
import re
import sys
from pathlib import Path

# Prompt-stage hooks fire for runtime-injected text too — task-notifications, monitor events, command
# stdout. Guard imported rather than re-declared: HOOK_PREFIXES already existed in FOUR byte-identical
# copies and none of them listed `<task-notification>`, which is 72% of this seat's prompt-stage
# traffic. See .claude/hooks/_prompt_source.py. Import failure leaves today's behaviour unchanged.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _prompt_source import is_user_text as _is_user_text
except Exception:  # pragma: no cover - fail toward firing, never toward silent suppression
    def _is_user_text(_p):
        return True


_TRIGGERS_JSON = Path(__file__).with_name("verification-triggers.json")


def _load_triggers():
    """Load project, course, and phrase trigger entries from JSON config.

    Returns (project_patterns, course_patterns, phrase_triggers) as lists of tuples.
    phrase_triggers carries (slug, pattern, reminder_text) — added 2026-05-22 via
    /claude-si promotion for correction-already-told-you pattern (19 events / 35d).

    Defaults to empty lists if the file is missing or malformed — hook degrades
    gracefully so a missing config never breaks the session-start flow.
    """
    try:
        data = json.loads(_TRIGGERS_JSON.read_text())
        projects = [
            (entry["slug"], entry["pattern"])
            for entry in data.get("project_patterns", [])
            if "slug" in entry and "pattern" in entry
        ]
        courses = [
            (entry["slug"], entry["pattern"])
            for entry in data.get("course_patterns", [])
            if "slug" in entry and "pattern" in entry
        ]
        phrases = [
            (entry["slug"], entry["pattern"], entry.get("reminder", ""))
            for entry in data.get("phrase_triggers", [])
            if "slug" in entry and "pattern" in entry
        ]
        return projects, courses, phrases
    except Exception:
        return [], [], []


# System-state keyword lookahead (A3 fix, 2026-06-23). Broad question patterns
# (what-/how-/tell-me) are gated on co-occurrence with one of these so they fire
# on system-state questions, not "what is 2+2?".
_SK = (
    r'(?=.*\b(?:graphify|brain|memory|session|hook|lint|tracker|report|extraction|'
    r'community|graph|file|folder|directory|state|status|broken|done|running|current|'
    r'stale|empty|active|merged|installed|deployed|wired|fired|skipped|tested|verified|'
    r'extracted|processed|cluster|node|edge|topic|orphan|phase|batch|chunk|checkpoint|'
    r'pipeline|core|agent|skill|command|daemon|launchd|commit|repo|branch|push|sync)\w*)'
)



# What this hook's injection ASKS FOR. bin/grade-gate.py scores a read-demanding advisory by
# REDUNDANCY rather than frequency: its injected advice is literally *your FIRST tool call must read the referenced thing*,
# so a fire whose next turn already read was advice Core did not need.
ADVICE = "read"


def _claimtext():
    """Shared mention discriminators (lib/claimtext.py) — see that file's header. Fail-open."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import claimtext
        return claimtext
    except Exception:
        return None

# (regex, label). Word-boundary anchored. Generic verbs require object hints.
PATTERNS = [
    (r'\blook\s+at\b', 'look at'),
    (r'\bread\b(?!\s*(?:me\b|y\b))', 'read'),
    (r'\bopen\s+(?:the|that|this|up|`|/|~)', 'open'),
    (r'\bcheck\s+(?:the|that|if|whether|those|this|memory|file|brain|access|canvas|live|on)\b', 'check'),
    (r'\breview\s+\S', 'review'),
    (r'\bwhy\s+(?:is|are|isn\'?t|aren\'?t|did|didn\'?t|do|don\'?t|would|wouldn\'?t|won\'?t|can\'?t|haven\'?t|wasn\'?t)\b', 'why-question'),
    (r'\brem(?:em|m|ame)?ber\b', 'remember'),
    (r'\brecall\b', 'recall'),
    (r'\bwhere\s+(?:is|are|did|does)\b', 'where-question'),
    (r'\bshow\s+me\b', 'show me'),
    (r'\bdo\s+we\s+(?:have|already|still)\b', 'do we have'),
    (r'\bdid\s+we\b', 'did we'),
    (r'\bdid\s+you\s+(?:read|check|see|find|look|grep)\b', 'did you read/check'),
    (r'\bgrep\s+for\b', 'grep for'),
    # 2026-07-30: "see" after a discourse opener is Nick starting a sentence, not asking to
    # look at anything — "well see that is how i want you to answer", "ok see this is the
    # problem". 5 spans in 353 real prompts. The negative lookbehind is fixed-width per
    # alternative, which Python requires.
    (r'(?<!well )(?<!okay )(?<!ok )(?<!so )(?<!yeah )(?<!and )(?<!but )(?<!now )'
     r'\bsee\s+(?:the|this|that|my|our|in|`|/|~)', 'see (with object)'),
    # Broadened 2026-05-05: catch state-questions that don't use trigger verbs.
    # Same-pattern failures kept happening because normal questions ("what
    # happened to X / is X done / tell me about Y") don't trip the list
    # above, so the model reconstructs from memory and gets it wrong.
    # 2026-06-23 (A3 fix): these broad patterns flooded context on EVERY
    # question (418 injects/47 sessions; fired on "What is 2+2?"). Now gated
    # on co-occurrence with a system-state keyword (_SK) so they keep firing
    # on "what is the hook doing?" but drop "what's the best way to sort?".
    (rf'{_SK}\btell\s+me\s+(?:about|what|how|why|where|whether|if)\b', 'tell me (state)'),
    (rf'{_SK}\bwhat\s+(?:happened|happens|is|are|was|were|did|do|does|about|in)\b', 'what-question (state)'),
    (rf'{_SK}\bhow\s+(?:do|does|did|is|are|was|were|many|much|come|long)\b', 'how-question (state)'),
    (r'\bwhich\s+(?:file|files|one|ones|of|version|directory|folder|hook|agent|skill|memory|project|session)\b', 'which-question'),
    (r'\bwho\s+(?:is|are|was|were|owns|did)\b', 'who-question'),
    (r'\bis\s+(?:the|there|that|this|it|our|my)\s+\S+\s+(?:done|broken|working|running|current|stale|empty|active|updated|complete|finished|fixed|merged|installed|set\s*up)\b', 'is X state'),
    (r'\bstate\s+of\b', 'state of'),
    (r'\bcurrent\s+(?:state|status|version|count|number|file|directory|folder|setup)\b', 'current X'),
    (r'\bstatus\s+of\b', 'status of'),
    (r'\bcorrect\??\s*$', 'correct?-confirm-request'),
    (r'\bconfirm\s+(?:that|the|this|whether|if|report|file|state|number|count|status|graphify|memory|brain|session|hook|lint)\b', 'confirm'),
    # Substantive question heuristic: prompt contains "?" AND any system-state
    # keyword. Catches "is graphify done?" / "any sessions left?" / "how many
    # files in X?" without overfiring on greetings or trivial messages.
    (r'(?=.*\?)(?=.*\b(?:graphify|brain|memory|session|hook|lint|tracker|report|extraction|community|graph|file|folder|directory|state|status|broken|done|running|current|stale|empty|active|merged|installed|deployed|wired|fired|skipped|tested|verified|extracted|processed|cluster|node|edge|topic|orphan|phase|batch|chunk|checkpoint|pipeline)\b)', 'state-keyword + ?'),
]


def detect(text):
    """Verification-trigger labels present in a prompt. A trigger phrase inside quotes is being
    DISCUSSED, not asked — Nick writing *the hook fires on "look at"* is not a request to look."""
    ct = _claimtext()
    text = text or ""
    spans = ct.quoted_spans(text) if ct else None
    hits, seen = [], set()
    for pat, label in PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            if ct and m.end() > m.start() and ct.is_mention(text, m.start(), m.end(), spans):
                continue
            if label not in seen:
                hits.append(label)
                seen.add(label)
            break
    return hits


def main():
    # Record that this hook RAN, matched or not. Without it the ledger can count FIRES but not
    # INVOCATIONS, so yield (fires/invocations) is not computable — and a gate that fires 4 times
    # out of 4 looks identical to one that fires 4 times out of 400. Added 2026-07-30 across every
    # instrumented hook that lacked it, so low-yield becomes a measurable verdict rather than a
    # guess. (The ledger could retire EXPENSIVE components but not cheap-and-useless ones; this is
    # the missing term.)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("verification-trigger", "UserPromptSubmit")
    except Exception:
        pass
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = (data.get("prompt") or "")
    if not _is_user_text(prompt):
        return  # runtime-injected turn, not the user speaking

    if not prompt or len(prompt.strip()) < 4:
        return 0

    hits = detect(prompt)
    # Project-mention detection. When Nick names a known project (slug or
    # natural-language alias), synthesis about that project must be grounded
    # in its actual project file — not paraphrased from current-state.md's
    # pickup list (which only surfaces in-flight items, not dormant
    # strategic projects). Added 2026-05-11 after a project-alias miss:
    # gave strategic pushback as if team-assistant was a
    # fresh idea, when it was an active project with a locked stakeholder meeting
    # 48 hours out.
    #
    # Slug/pattern data lives in verification-triggers.json (audit #10, 2026-05-13).
    # phrase_triggers added 2026-05-22 via /claude-si for correction-already-told-you.
    project_patterns, course_patterns, phrase_triggers = _load_triggers()

    phrase_hits = []  # list of (slug, reminder_text)
    for slug, pat, reminder in phrase_triggers:
        if re.search(pat, prompt, flags=re.IGNORECASE):
            phrase_hits.append((slug, reminder))

    project_hits = []
    for slug, pat in project_patterns:
        if re.search(pat, prompt, flags=re.IGNORECASE):
            project_hits.append(slug)

    # School course patterns. Added 2026-05-11 after ECON 301 Q5/Q6 docx miss:  # privacy-ok: illustrative course code, invented — see verification-triggers.json
    # context-cleared session generated docx from stale .md files because no
    # hook signal forced a pre-read of the course folder. School courses live
    # under memory/education/courses/<slug>/ not memory/projects/, so they get
    # routed to a different path below.
    course_hits = []
    for slug, pat in course_patterns:
        if re.search(pat, prompt, flags=re.IGNORECASE):
            course_hits.append(slug)

    if not hits and not project_hits and not course_hits and not phrase_hits:
        return 0

    # PAYLOAD BUDGET (2026-07-30, master plan Phase 0.2).
    #
    # The plan sent me here to narrow a 53% fire rate. Measured against 353 real prompts the rate
    # is 40.8%, and reading every span of the two biggest suspect classes — all 22 "see" spans and
    # all 18 "why" spans — showed the fires are overwhelmingly CORRECT. "Why are only 24 of 37
    # hooks in the intent record?", "why is the close reconciler not updating", "go read the json
    # of the last session" are precisely the guarded behaviour. Nick's prompts to this Core really
    # are, most of the time, requests to go verify something; a 41% fire rate against a ~40% base
    # rate is not over-firing. So the trigger list is left alone except for one measured class
    # (5 spans of discourse-marker "see"), because narrowing past the evidence is how the gmail
    # guard lost real coverage while looking like a tidy narrowing.
    #
    # The cost lever is the PAYLOAD, and it is a clean one: same coverage, less spend. 735 lifetime
    # fires x 109 tok. Two of the four sentences below were pure restatement — "Anti-pattern rule
    # #2" is a label carrying no instruction, and the state-claims sentence is state-claim-gate's
    # job, duplicated here and in CLAUDE.base.md. Cutting them takes the base reminder from 433 to
    # ~150 chars with nothing enforced lost, which the hook suite proves by staying green.
    parts = []
    if hits:
        trigger_str = ", ".join(f"'{h}'" for h in hits[:4])
        parts.append(
            f"VERIFY FIRST ({trigger_str}): read/grep the referenced thing before answering — "
            "no paraphrase from memory. Ambiguous referent → ask ONE question, don't guess."
        )
    if project_hits:
        slugs_str = ", ".join(project_hits)
        repo_root = Path(__file__).resolve().parents[2]

        def project_path(slug):
            folder = repo_root / "memory" / "projects" / slug
            if folder.is_dir():
                return f"memory/projects/{slug}/overview.md"
            return f"memory/projects/{slug}.md"

        files_str = ", ".join(project_path(s) for s in project_hits)
        parts.append(
            f"PROJECT NAMED ({slugs_str}): before any synthesis or recommendation, Read "
            f"{files_str} — current-state.md does not surface dormant projects."
        )
    if course_hits:
        slugs_str = ", ".join(course_hits)
        paths_str = ", ".join(f"memory/education/courses/{s}/" for s in course_hits)
        parts.append(
            f"COURSE NAMED ({slugs_str}): before any Write/Edit or synthesis about coursework, "
            f"read the 2 newest files in {paths_str} plus sessions/<today>.md — otherwise you "
            f"generate from stale content."
        )
    if phrase_hits:
        # Phrase-trigger reminders carry their own bespoke text per slug.
        for slug, reminder in phrase_hits:
            if reminder:
                parts.append(reminder)

    reminder = "\n\n".join(parts)

    # Emit through hooklog so the injection is ACCOUNTED FOR (Phase 2.3). tokens_injected had
    # existed on every log line since the Phase 0 sensor and was 0 everywhere, because recording
    # the cost was a separate call each hook had to remember after building its payload — and
    # none of them did. Printing and accounting are one operation now, so it cannot be skipped.
    # Falls back to a bare print if the library is unavailable: steering the turn is the job,
    # measuring it is the improvement, and the improvement must never cost the job.
    try:
        import os as _o
        sys.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog
        hooklog.emit("verification-trigger", "UserPromptSubmit", reminder,
                     session=str(data.get("session_id", "")),
                     detail=",".join(hits[:3]))
        return 0
    except Exception:
        pass
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": reminder}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
