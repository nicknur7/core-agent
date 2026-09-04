#!/usr/bin/env python3
"""PreToolUse gate — model-pin chokepoint for subagent fan-outs (contract-binding
proposal fix 4, live 2026-06-09, Nick-approved: "4 sounds good", no shadow).

The "that was supposed to be sonnet" failure always happens at an Agent/Task/
Workflow call: fan-outs INHERIT the session model, so an unpinned 7-agent batch
on a Fable session runs 7 Fable agents. Model identity is best-effort, but the
TOOL CALL is deterministic — so gate the chokepoint, not the model:

  · Agent/Task: the 3rd+ spawn within ROLLING_WINDOW seconds that lacks an
    explicit `model` is refused (1-2 unpinned spawns = normal judgment
    delegation; 3+ = a fan-out that must be pinned).
  · Workflow: refused unless the script/args mention `model` anywhere
    (workflow scripts must pin mechanical phases; named workflows pass).

State: .claude/state/.agent-spawns-<session_id>.json (rolling timestamps).
Fail-open: any error → exit 0. Kill-switch: LEARNED_LAYER=0.
"""
import json
import pathlib
import os
import re
import sys
import time
from pathlib import Path

INSTANCE = Path(os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STATE_DIR = INSTANCE / ".claude" / "state"
ROLLING_WINDOW = 180  # seconds — spawns this close together are one fan-out
UNPINNED_LIMIT = 2    # 3rd unpinned spawn in window gets refused

# Agents that pin their own model in .claude/agents/<name>.md frontmatter NEVER
# inherit the session model, so they are not a fan-out-on-Fable. Counting them
# tripped the gate on a 3rd back-to-back Sentinel review (M1 false-fire,
# 2026-06-22). Exempt from the unpinned count. Keep in sync with agents/*.md.
PINNED_AGENT_TYPES = {"sentinel", "sentinel-code", "close-reconciler"}


def _shadow_log(**kv) -> None:
    """Phase 5 SHADOW (2026-06-27): record every fan-out-shaped call so the actual spend
    pattern can be quantified + the gate's thresholds calibrated, WITHOUT changing live
    enforcement. Also flags the named-workflow free-pass (a hole: a named workflow can still
    fan out on the session model, but currently passes unconditionally). Append-only, fail-open."""
    try:
        import datetime
        line = "\t".join(f"{k}={v}" for k, v in kv.items())
        with open(STATE_DIR / ".model-pin-shadow.log", "a") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{line}\n")
    except Exception:
        pass


def main() -> int:
    # telemetry: record that this hook RAN, matched or not (lib/hooklog.invoked)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "lib"))
        import hooklog as _hl; _hl.invoked("model-pin-gate", "PreToolUse")
    except Exception:
        pass
    if os.environ.get("LEARNED_LAYER") == "0":
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name") or ""
    tin = data.get("tool_input") or {}
    session_id = data.get("session_id") or data.get("sessionId") or "unknown"

    if tool == "Workflow":
        blob = json.dumps(tin)
        named = bool(tin.get("name"))
        # Phase 5 (2026-06-27): require a REAL pin (`model:` / `model=` / `model':`), not just the
        # substring "model" appearing somewhere in prose. Tighter, near-zero false-block (a script
        # that truly pins a phase writes `model:`/`{model:`). Named workflows still pass — their pins
        # live in the saved script, not the call args, so blocking them would false-fire.
        # READ THE SCRIPT WHEN THE CALL ONLY REFERENCES IT (2026-08-08, found by core-business,
        # which this gate was blocking live).
        #
        # A Workflow launched with scriptPath — which is EVERY resume, and the documented way to
        # iterate without resending a script — carries roughly {scriptPath, resumeFromRunId} as its
        # input. THE SCRIPT CONTENT IS NOT IN tool_input. So a script whose every phase is properly
        # pinned scanned as unpinned, has_pin was False, named was False, and the gate blocked
        # correct behaviour. A gate that fires on the compliant case is worse than no gate: it
        # trains you to work around it, which is the alarm-fatigue failure this repo has now hit
        # from four directions in one night.
        _sp = tin.get("scriptPath")
        if _sp:
            try:
                blob += " " + pathlib.Path(_sp).read_text(errors="ignore")
            except Exception:
                # Cannot read the script -> cannot decide. Do NOT block: this gate governs SPEND,
                # not safety, and a false block costs real work while a miss costs tokens Nick is
                # already warned about elsewhere. Recorded so the gap stays countable.
                _shadow_log(tool="Workflow", named=str(bool(named)).lower(), pinned="unknown",
                            would_block_live="false", would_block_if_tightened="false")
                return 0
        elif tin.get("resumeFromRunId"):
            # A bare resume replays a script this gate never sees. Same reasoning, same record.
            _shadow_log(tool="Workflow", named=str(bool(named)).lower(), pinned="unknown",
                        would_block_live="false", would_block_if_tightened="false")
            return 0

        has_pin = bool(re.search(r"model['\"]?\s*[:=]", blob))
        if has_pin or named:
            # SHADOW: a named workflow with NO model pin is the free-pass hole — log it as
            # would-block-if-tightened so we can see how often it happens before enforcing.
            if named and not has_pin:
                _shadow_log(tool="Workflow", named="true", pinned="false",
                            would_block_live="false", would_block_if_tightened="true")
            return 0
        sys.stderr.write(
            "MODEL-PIN GATE — Workflow launch with no model pin anywhere in the script. "
            "Fan-outs inherit the session model (Fable = 2x Opus pricing). Pin mechanical "
            "phases (agent(..., {model:'sonnet'|'haiku'}) or a model field per phase) and "
            "re-launch. Judgment-only phases may stay on session tier — but say so in the script.\n"
        )
        return 2

    if tool not in ("Agent", "Task"):
        return 0
    if (tin.get("subagent_type") or tin.get("subagentType")) in PINNED_AGENT_TYPES:
        return 0   # pins own model in frontmatter — never inherits session model

    has_model = bool(tin.get("model"))
    now = time.time()
    spawn_file = STATE_DIR / f".agent-spawns-{session_id}.json"
    try:
        spawns = json.loads(spawn_file.read_text())
    except Exception:
        spawns = []
    spawns = [s for s in spawns if isinstance(s, dict) and now - s.get("ts", 0) <= ROLLING_WINDOW]

    unpinned_in_window = sum(1 for s in spawns if not s.get("pinned"))
    would_block = (not has_model) and unpinned_in_window >= UNPINNED_LIMIT
    _shadow_log(tool=tool, pinned=str(has_model).lower(),
                unpinned_in_window=unpinned_in_window, would_block_live=str(would_block).lower())
    if would_block:
        sys.stderr.write(
            f"MODEL-PIN GATE — this is unpinned subagent spawn #{unpinned_in_window + 1} within "
            f"{ROLLING_WINDOW}s: that's a fan-out, and fan-outs INHERIT the session model "
            "(on Fable that's $10/$50 per MTok — 2x Opus). Pin `model` explicitly on this and "
            "every remaining mechanical agent in the batch (sonnet for execution, haiku for bulk "
            "extraction); only judgment agents may inherit, and then pin nothing but SAY so.\n"
        )
        return 2

    spawns.append({"ts": now, "pinned": has_model})
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        spawn_file.write_text(json.dumps(spawns))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
