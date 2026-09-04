#!/usr/bin/env python3
# UserPromptSubmit hook — demand-side rot detection + Adaptive Behavioral Anchoring (ABA).
# Supersedes the prior supply-side staleness check (tokens/calls/elapsed) which was archived 2026-05-15
# — rot-check is the active drift signal; the on-demand /health slash command provides supply-side readout.
#
# Framework: Rath 2026 (arXiv 2601.04170) Agent Drift. Core-adapted v2 per
# tasks/research/agent-drift-framework-2026-05-14.md +
# tasks/research/asi-core-empirical-2026-05-14.md.
#
# Computes Core-ASI_v2 over rolling 50-assistant-turn windows:
#
#   Core-ASI_v2 = 0.25 * T_sel
#               + 0.50 * (B_human + B_error) / 2
#               + 0.25 * B_length
#
# Dimensions (each normalized to [0,1] where 1 = perfect stability):
#   T_sel    — Tool Selection Stability: chi-sq on tool freq dists (current 50
#              vs prior 50 assistant turns). Mapped via 1 / (1 + chi2/dof).
#   B_human  — Human Intervention Rate: 1 - correction_rate over user messages
#              following assistant turns in window.
#   B_error  — Error Pattern Emergence: 1 / (1 + block_count / 10), counting
#              hook-block injections (state-claim-gate, say-do-gap, sentinel,
#              verification-trigger) in user messages within window.
#   B_length — Output Length Stability: 1 / (1 + CV) of assistant text-block
#              char counts within window. If <5 text-producing turns, B_length=1.0.
#
# Trigger threshold: τ=0.60 — see the TAU constant for the measurement it comes from. This
# line said 0.65 for five weeks after RC3 lowered the constant to 0.60, which is how the
# master plan came to list "restore τ to the documented 0.65" as a fix. Paper τ=0.75 is far
# too high for Core; at 0.70 every long session fires.
#
# Firing policy: ONCE per session (marker /tmp/rot-check-fired-<sha>). No nag-spam.
# Exit silently otherwise. Never blocks the prompt.

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib'))
import coreuser as _U  # operator name from identity.json, never hardcoded

import json
import os
import sys
import re
import hashlib
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
import core_paths  # noqa: E402

WINDOW_SIZE = 50          # last N assistant turns
BASELINE_MIN = 20         # need this many assistant turns before measuring
# Core-ASI threshold. RC3 lowered this 0.65 -> 0.60 on 2026-06-23; the module docstring and the
# injected framework line both still said 0.65, and the master plan (Phase 0.3) read that
# disagreement as a bug to fix by restoring 0.65. Measured 2026-07-30 over 20 real sessions, by
# the MINIMUM ASI each session reaches — which is what decides a once-per-session fire:
#
#     tau=0.60   6/20 sessions =  30%
#     tau=0.65  15/20 sessions =  75%
#     tau=0.70  20/20 sessions = 100%
#
# So the code was right and the prose was stale, not the other way round. 0.65 would fire on
# three sessions in four, which is a constant, and a constant carries no signal. 0.60 stays;
# the stale 0.65 references are corrected rather than the constant.
TAU = 0.60

# Phase 0.4 — what ABA actually re-injects.
#
# It used to append CLAUDE.md and all four rules files VERBATIM: 8,198 tokens. Those files are
# loaded at session start and are still in context when this fires, so the re-injection added a
# second identical copy of the steering surface and nothing else. That is not re-anchoring, it
# is duplication, and at 30% of sessions it was 58% of all injection cost in the system.
#
# The hook already knows which dimension is dragging. Anchor THAT, in the register of the
# specific failure it represents, and leave the rest of context alone.
ABA_ANCHORS = {
    # Nick is correcting a lot: the work is not landing on what he asked for.
    "B_human": [
        f"{_U.name()} is correcting you at an elevated rate. Before the next response:",
        "  · Lead with the bottom line — one sentence of answer/decision BEFORE context.",
        "  · If his prompt was short or has two plausible readings, state your one-line",
        "    interpretation before executing. He redirects the paraphrase, not the work.",
        "  · Disagree where you have reason to. Validation he didn't ask for is the failure",
        "    mode here, not bluntness.",
    ],
    # Hooks are blocking: claims are outrunning evidence.
    "B_error": [
        "Gates are blocking your responses at an elevated rate. Every one of them checks the",
        "same thing: a claim with no tool call behind it, this turn.",
        "  · Assert system state only from a read IN THIS RESPONSE, not from memory or from a",
        "    prior summary — including your own earlier ones.",
        "  · 'X doesn't exist' / 'that was never built' needs a multi-file grep, not one read.",
        "  · Say you'll write something only in the same turn you write it.",
    ],
    # Response lengths have gone erratic relative to the questions.
    "B_length": [
        "Response length has gone erratic relative to what was asked.",
        "  · Match depth to question depth. Short question, short answer.",
        "  · No summarising back what he just said; no options survey you won't pursue.",
        "  · Write substantive artifacts to a file, then paste a tight summary.",
    ],
    # The tool mix shifted sharply against the prior window.
    "T_sel": [
        "Your tool mix has shifted sharply from the prior window — usually reasoning from",
        "context where you were previously reading.",
        "  · Read the file before describing it. Memory files and prior summaries go stale.",
        "  · Strategic / Path / Track framings: grep memory/decisions-log.md FIRST.",  # lint-code-paths: ignore — advisory text shown to the agent, not a path op; the repo-relative form is what it would type
        "  · Recall the brain before answering anything with an implicit 'previously'.",
    ],
    "_default": [
        "Session quality has degraded. Re-anchor: bottom line first, claims backed by a",
        "same-turn read, depth matched to the question.",
    ],
}

HOOK_BLOCK_PATTERNS = [
    r"Stop hook feedback",
    r"STATE-?CLAIM GATE",
    r"SAY[/-]DO GAP",
    r"SENTINEL GUARD",
    r"Outward-facing action requires Sentinel review",
    r"VERIFICATION TRIGGER detected",
]
HOOK_BLOCK_RE = re.compile("|".join(HOOK_BLOCK_PATTERNS), re.IGNORECASE)

CORRECTION_PATTERNS = [
    r"^\s*no\b", r"^\s*noo+", r"^\s*nope\b", r"^\s*stop\b", r"^\s*wrong\b",
    r"^\s*wtf\b", r"^\s*ugh+\b", r"^\s*you'?re\b", r"^\s*you said\b",
    r"^\s*you didn'?t\b", r"^\s*you forgot\b", r"^\s*you missed\b",
    r"^\s*that'?s not\b", r"^\s*not what i\b", r"^\s*i didn'?t ask\b",
    r"^\s*i didnt ask\b", r"^\s*actually,?\b", r"^\s*wait,?\b",
    r"^\s*hold on\b", r"^\s*bruh\b",
]
CORRECTION_RE = re.compile("|".join(CORRECTION_PATTERNS), re.IGNORECASE)
SKIP_START_RE = re.compile(
    r"^\s*(<|ok\b|okay\b|yes\b|yeah\b|great\b|sure\b|thanks|thank)",
    re.IGNORECASE,
)


def get_user_text(content):
    if isinstance(content, str):
        if content.startswith("<"):
            return None
        return content
    if isinstance(content, list):
        if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
            return None
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                txt = p.get("text", "") or ""
                if txt.startswith("<"):
                    return None
                return txt
    return None


def get_assistant_data(content):
    """Return (text_chars, tool_names_used)."""
    text_chars = 0
    tools = []
    if isinstance(content, str):
        text_chars = len(content)
    elif isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if pt == "text":
                text_chars += len(p.get("text", "") or "")
            elif pt == "tool_use":
                name = p.get("name", "")
                if name:
                    tools.append(name)
    return text_chars, tools


def classify_user(text):
    """Return (is_block, is_correction)."""
    if not text:
        return (False, False)
    if HOOK_BLOCK_RE.search(text):
        return (True, False)
    if SKIP_START_RE.match(text):
        return (False, False)
    if CORRECTION_RE.match(text):
        return (False, True)
    return (False, False)


def chi_squared(curr_counts, prior_counts):
    """Pearson chi-sq for two categorical distributions over the same key set.

    Returns the RAW statistic. Callers must not use it as a distance — see tool_stability().
    """
    keys = set(curr_counts) | set(prior_counts)
    if not keys:
        return 0.0, 0
    n_curr = sum(curr_counts.values())
    n_prior = sum(prior_counts.values())
    if n_curr == 0 or n_prior == 0:
        return 0.0, 0
    chi2 = 0.0
    for k in keys:
        c = curr_counts.get(k, 0)
        p = prior_counts.get(k, 0)
        # expected freq under null: same distribution scaled
        # use combined proportion times n_curr for expected in current
        total = c + p
        n_total = n_curr + n_prior
        if total == 0:
            continue
        exp_c = total * n_curr / n_total
        if exp_c > 0:
            chi2 += (c - exp_c) ** 2 / exp_c
        exp_p = total * n_prior / n_total
        if exp_p > 0:
            chi2 += (p - exp_p) ** 2 / exp_p
    dof = max(1, len(keys) - 1)
    return chi2, dof


def tool_stability(curr_counts, prior_counts):
    """T_sel in [0,1]: how similar this window's tool mix is to the prior window's.

    2026-07-30, master plan Phase 0.3. The old line was:

        T_sel = 1.0 / (1.0 + chi2 / max(1, dof))

    and chi-squared is a TEST STATISTIC, not a distance. It grows with sample size, so the
    same proportional shift in tool mix scores differently depending only on how many tool
    calls the window happened to contain. Dividing by degrees of freedom corrects for the
    number of tool NAMES, not for N. A tool-heavy window therefore looked less stable than a
    tool-light window with identical behaviour, and the dimension was biased low throughout.

    Cramer's V is the standard effect-size normalisation of exactly this statistic. For a
    2 x k table min(r-1, c-1) = 1, so V = sqrt(chi2 / n) and V is in [0,1] by construction.
    Stability is its complement.

    MEASURED over 2,169 sliding windows across 24 real transcripts:
        T_sel  old  min 0.062  med 0.433  p90 0.623  sd 0.151
        T_sel  new  min 0.000  med 0.588  p90 0.793  sd 0.165
        ASI    old  med 0.719   ->  new med 0.755

    A CORRECTION TO THE PLAN. Phase 0.3 was written to fix "T_sel frozen at 0.0476 in
    260/319 readings". That number came from .claude/state/.rot-score-*.json, and that
    corpus cannot support it: 331 files carry only ELEVEN distinct (asi, dims) signatures,
    270 of them identical. Replayed against transcripts whose input can actually be
    inspected, T_sel varies normally and reaches 1.0. It was never frozen. The
    sample-size bias above is real and is the reason to change this, but it is a different
    defect from the one the plan named, and the plan's version is retracted.
    """
    chi2, _dof = chi_squared(curr_counts, prior_counts)
    n = sum(curr_counts.values()) + sum(prior_counts.values())
    if n == 0:
        return 1.0
    return 1.0 - math.sqrt(min(1.0, chi2 / n))


def compute_asi(turns, prior_turns):
    """Compute Core-ASI_v2 over a window of assistant turns.
    turns: list of dicts {text_chars, tools, next_user_text}
    prior_turns: prior window for T_sel comparison (or None if first window).
    Returns (asi, dims_dict).
    """
    # T_sel — chi-sq on tool freq distributions
    curr_counts = {}
    for t in turns:
        for name in t["tools"]:
            curr_counts[name] = curr_counts.get(name, 0) + 1
    if prior_turns:
        prior_counts = {}
        for t in prior_turns:
            for name in t["tools"]:
                prior_counts[name] = prior_counts.get(name, 0) + 1
        T_sel = tool_stability(curr_counts, prior_counts)
    else:
        T_sel = 1.0

    # B_human — 1 - correction_rate over next_user_text in window
    correction_count = 0
    intervention_opportunities = 0
    for t in turns:
        nut = t.get("next_user_text")
        if nut is None:
            continue
        intervention_opportunities += 1
        _, is_corr = classify_user(nut)
        if is_corr:
            correction_count += 1
    if intervention_opportunities > 0:
        B_human = 1.0 - (correction_count / intervention_opportunities)
    else:
        B_human = 1.0

    # B_error — DISTINCT block messages in next_user_text within window.
    # RC1 fix (2026-06-23): a multi-turn tool-chain assigns the SAME next_user_text
    # to every assistant turn before the user replies, so one block message was
    # counted N times (B_error inflated N×). Dedup by message identity.
    seen_blocks = set()
    for t in turns:
        nut = t.get("next_user_text")
        if nut is None:
            continue
        is_block, _ = classify_user(nut)
        if is_block:
            seen_blocks.add(nut[:200])
    block_count = len(seen_blocks)
    B_error = 1.0 / (1.0 + block_count / 10.0)

    # B_length — CV of text_chars across text-producing turns
    text_turns = [t["text_chars"] for t in turns if t["text_chars"] >= 5]
    if len(text_turns) < 5:
        B_length = 1.0
    else:
        mean = sum(text_turns) / len(text_turns)
        if mean == 0:
            B_length = 1.0
        else:
            var = sum((x - mean) ** 2 for x in text_turns) / len(text_turns)
            cv = math.sqrt(var) / mean
            B_length = 1.0 / (1.0 + cv)

    # RC4 fix (2026-06-23): when there's no prior window, T_sel is a DEFAULT 1.0,
    # not a real measurement — including it at full weight inflated ASI on short/
    # first sessions. Renormalize the formula over the 3 real dimensions instead.
    t_sel_valid = bool(prior_turns)
    if t_sel_valid:
        asi = 0.25 * T_sel + 0.50 * (B_human + B_error) / 2.0 + 0.25 * B_length
    else:
        asi = (0.50 * (B_human + B_error) / 2.0 + 0.25 * B_length) / 0.75
    return asi, {
        "T_sel": T_sel,
        "T_sel_valid": t_sel_valid,
        "B_human": B_human,
        "B_error": B_error,
        "B_length": B_length,
    }


def load_active_lessons(path, max_lines=30):
    try:
        with open(path) as f:
            lines = f.readlines()
    except Exception:
        return [f"(could not load {core_paths.TASK_LESSONS})"]
    out = []
    in_active = False
    for line in lines:
        if line.startswith("## Active rules at a glance"):
            in_active = True
            continue
        if in_active:
            if line.startswith("## ") or line.strip() == "---":
                break
            stripped = line.rstrip()
            if stripped:
                out.append(stripped)
            if len(out) >= max_lines:
                break
    return out or ["(no active rules section found)"]


def load_file(path, max_lines=None):
    try:
        with open(path) as f:
            lines = f.readlines()
    except Exception:
        return [f"(could not load {path})"]
    if max_lines:
        lines = lines[:max_lines]
    return [line.rstrip() for line in lines]


def parse_jsonl(jsonl_path):
    """Walk JSONL; return list of assistant-turn dicts with next_user_text resolved."""
    turns = []
    pending_user_text = None  # the latest seen user text since last assistant
    raw_records = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    raw_records.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []

    # First pass: build sequential list of (kind, content)
    # then walk to attach next_user_text to each assistant turn
    sequence = []
    for d in raw_records:
        t = d.get("type")
        msg = d.get("message") or {}
        content = msg.get("content")
        if t == "user":
            txt = get_user_text(content)
            if txt is not None:
                sequence.append(("user", txt))
        elif t == "assistant":
            text_chars, tools = get_assistant_data(content)
            sequence.append(("assistant", (text_chars, tools)))

    for i, (kind, payload) in enumerate(sequence):
        if kind != "assistant":
            continue
        text_chars, tools = payload
        next_user_text = None
        for j in range(i + 1, len(sequence)):
            if sequence[j][0] == "user":
                next_user_text = sequence[j][1]
                break
        turns.append({
            "text_chars": text_chars,
            "tools": tools,
            "next_user_text": next_user_text,
        })
    return turns


def main():
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("rot-check", "UserPromptSubmit")
    except Exception:
        pass
    stdin_data = ""
    if not sys.stdin.isatty():
        stdin_data = sys.stdin.read()
    try:
        payload = json.loads(stdin_data) if stdin_data else {}
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")

    if transcript_path:
        if not os.path.exists(transcript_path):
            sys.exit(0)
        jsonl = transcript_path
    else:
        if cwd:
            projects_dir = os.path.expanduser(
                "~/.claude/projects/-"
                # Canonical slug: EVERY non-alphanumeric becomes a dash (bin/core_seat.py::
                # transcripts_dir). The two-character version diverged on any path with a dot or
                # underscore, resolving to a directory that does not exist — which this reads as an
                # empty history rather than as a bad path.
                + re.sub(r"[^A-Za-z0-9]", "-", cwd.lstrip("/"))
            )
        else:
            # No transcript_path AND no cwd → can't locate JSONL; skip silently.
            sys.exit(0)
        try:
            import glob
            files = sorted(
                glob.glob(f"{projects_dir}/*.jsonl"),
                key=os.path.getmtime,
                reverse=True,
            )
            if not files:
                sys.exit(0)
            jsonl = files[0]
        except Exception:
            sys.exit(0)

    session_key = hashlib.sha256(jsonl.encode()).hexdigest()[:16]
    marker = f"/tmp/rot-check-fired-{session_key}"
    cwd_root = os.environ.get("CORE_INSTANCE") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    turns = parse_jsonl(jsonl)
    # Need baseline + at least one window
    if len(turns) < BASELINE_MIN + WINDOW_SIZE:
        sys.exit(0)

    window = turns[-WINDOW_SIZE:]
    if len(turns) >= 2 * WINDOW_SIZE:
        prior = turns[-2 * WINDOW_SIZE: -WINDOW_SIZE]
    else:
        prior = None

    asi, dims = compute_asi(window, prior)

    # Persist per-prompt rot state so statusline reads live ASI even when ABA already fired.
    try:
        state_dir = f"{cwd_root}/.claude/state"
        os.makedirs(state_dir, exist_ok=True)
        with open(f"{state_dir}/.rot-score-{session_key}.json", "w") as _f:
            json.dump({
                "asi": round(asi, 4),
                "dims": {k: round(v, 4) for k, v in dims.items()},
                "tau": TAU,
                "window_size": WINDOW_SIZE,
                "session_key": session_key,
                "updated": int(__import__("time").time()),
            }, _f)
    except Exception:
        pass

    # ABA fires ONCE per session. Either marker exists OR ASI is healthy → no injection.
    if os.path.exists(marker) or asi >= TAU:
        sys.exit(0)

    try:
        open(marker, "w").close()
    except Exception:
        pass

    # Identify dragging dimension for surfacing line
    sorted_dims = sorted(
        ((k, v) for k, v in dims.items() if isinstance(v, (int, float))),
        key=lambda kv: kv[1],
    )
    drag_name, drag_val = sorted_dims[0]

    parts = [
        f"ROT WARNING — Core-ASI {asi:.3f} (below τ={TAU:.2f}) over last "
        f"{WINDOW_SIZE} assistant turns. Dragging dimension: {drag_name}={drag_val:.3f}.",
        # RC2 fix (2026-06-23): the required PREPEND action used to live at byte ~60K
        # and was truncated away by the preview cap (0% compliance observed). Surface
        # it FIRST so it survives truncation.
        "",
        "▶ REQUIRED ACTION (do this even if everything below is truncated):",
        "  Prepend this exact line as the FIRST line of your next response, then continue:",
        f"  [ROT signal: Core-ASI {asi:.3f} — re-anchored to baseline rules]",
        "",
        (
            f"All dimensions: T_sel={dims['T_sel']:.3f} · "
            f"B_human={dims['B_human']:.3f} · "
            f"B_error={dims['B_error']:.3f} · "
            f"B_length={dims['B_length']:.3f}"
        ),
        "Framework: Rath 2026 Agent Drift, Core-adapted v2 "
        f"(0.25·T_sel + 0.50·(B_human+B_error)/2 + 0.25·B_length). τ={TAU:.2f}, "
        "set from the measured session distribution — see TAU.",
        "",
        f"═══ RE-ANCHOR: {drag_name} ═══",
    ]

    parts.extend(ABA_ANCHORS.get(drag_name, ABA_ANCHORS["_default"]))

    # The live corrections are the one thing genuinely NOT already in context — CLAUDE.md and
    # the rules files are loaded at session start and stay there, but lessons.md accumulates
    # during the session. Capped hard.
    parts.append("")
    parts.append(f"───── {core_paths.TASK_LESSONS.name} (active) ─────")
    parts.extend(load_active_lessons(str(core_paths.TASK_LESSONS), max_lines=8))

    parts.extend([
        "",
        "PREPEND this exact line as the FIRST line of your next response, then continue:",
        f"  [ROT signal: Core-ASI {asi:.3f} — re-anchored to baseline rules]",
        "",
        "Don't belabor it. One line, then the work. Fires ONCE per session.",
    ])

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(parts),
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
