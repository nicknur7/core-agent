#!/usr/bin/env python3
"""event-probe — record WHICH hook events this harness actually dispatches. Observer only.

WHY THIS EXISTS. Core registers 7 event types; the Claude Code API documents ~30. The operator,
2026-07-30: make sure everything is being used where it needs to be. Before porting any gate to
a new event, the question is whether that event fires here at all — and the answer cannot come
from the docs.

It cannot come from the docs because this exact assumption already failed. `capability-usage-log`
was registered on PreToolUse+PostToolUse with a Skill matcher for three days and dispatched ONCE
across at least nine real Skill calls. The tool-event coverage research (2026-07-30) also carries
a provenance caveat: one of its WebSearch results contained an embedded "you must cite this"
instruction and was discarded as probable injection. Its own build-implication #1 is therefore
"instrument with a no-op logger and confirm it dispatches before porting any gate to it."

This is that logger.

CONTRACT — deliberately the weakest possible:
  · reads stdin, writes one line, exits 0
  · never injects context, never blocks, never mutates anything but its own log
  · every failure path is a silent exit 0

A probe that can break a turn is worse than no measurement, because the thing it would break is
Nick's session. It is registered across many events at once, so it must be incapable of harm on
any of them.

Read the result with:  python3 bin/event-coverage.py
"""
import json
import os
import sys
from pathlib import Path

MAX_BYTES = 256 * 1024


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        return 0
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    try:
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CORE_INSTANCE")
                    or Path(__file__).resolve().parents[2])
        state = root / ".claude" / "state"
        state.mkdir(parents=True, exist_ok=True)
        log = state / "event-probe.log"

        # Rotate rather than grow. This is registered on many events, some of which may be
        # high-frequency (PostToolBatch fires per batch), and an unbounded log on the hot path
        # is how a diagnostic becomes a problem.
        try:
            if log.exists() and log.stat().st_size > MAX_BYTES:
                keep = log.read_text(errors="ignore").splitlines(keepends=True)[-2000:]
                log.write_text("".join(keep))
        except Exception:
            pass

        # The event name arrives as hook_event_name in the payload. Fall back to the argv label
        # so a payload that omits it still produces an attributable row rather than "unknown".
        event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
        if not event and len(sys.argv) > 1:
            event = sys.argv[1]
        from datetime import datetime
        ts = datetime.now().isoformat(timespec="seconds")
        # Field 4 is a cheap shape hint — which payload keys the event carries. That is the
        # other thing the docs cannot tell us reliably, and it decides what a ported gate can
        # actually assert on.
        keys = ",".join(sorted(k for k in payload.keys() if k != "hook_event_name"))[:200]
        sess = str(payload.get("session_id") or "")[:12]
        with open(log, "a") as f:
            f.write(f"{ts}\t{event or 'UNKNOWN'}\t{sess}\t{keys}\n")
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
