#!/usr/bin/env python3
"""RETIRED — inert on purpose. Do not treat this file as an enforcement mechanism.

This hook is registered ZERO times in .claude/settings.json and its entire body is a bare
sys.exit(0). It enforces nothing and has enforced nothing since it was stubbed.

WHY IT IS STILL HERE
    `.claude/hooks/` is a SHARED path that syncs to every Core, so deleting a file has
    orphan-cleanup consequences across the fleet; leaving an obviously-dead stub in place is the
    cheaper and safer state until a deliberate retirement pass removes it everywhere at once.

WHY THIS DOCSTRING EXISTS
    Until 2026-08-05, `.claude/identity.json` recorded this file as the structural gate for two
    learned contracts — and for `frustration-deescalate` it was the ONLY gate listed. So the system
    believed a contract was enforced by a hook that does not run and would do nothing if it did.
    That belief fed `measure-contract-fitness.py`, which suppresses a NOT-BINDING alarm for a
    freshly-gated contract (GATED-WATCH). It happened to be harmless because the recorded gate date
    was long past the 7-day watch window, so no suppression was active — a false record rather than
    a live fault. Both attributions now name `stop-signal-gate.sh`, which is what actually fires.

    The 14 historical blocks attributed to `approval-gate` in hook-events.log predate the stubbing.
    `hook_block_miner.py` still lists it in SECURITY_TIER so those blocks are never mined for
    tuning, which stays correct: a dead gate must not be "tuned" for coverage either.

If you are looking for the real approval/outward-action gate, it is `pretooluse-guard.sh` plus the
`sentinel-approve.sh` / `sentinel-receipt.sh` token chain.
"""
import sys

sys.exit(0)
