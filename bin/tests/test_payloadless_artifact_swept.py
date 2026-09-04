#!/usr/bin/env python3
"""An artifact whose payload is gone must be quarantined, not left firing into a void.

WHY THIS EXISTS (2026-08-12, Phase 4). The watchdog swept one direction and not the other:

    payload file with no artifact   ->  _sweep_orphan_payloads: logged, retired
    artifact with no payload file   ->  NOTHING

friction_dispatch:522 correctly refuses to inject an unverifiable procedure and logs
`payload_mismatch`. Nothing consumed that log. So a live `hooked_skill` sat matching prompts,
failing verification, and injecting nothing — on every single fire, indefinitely.

Found by measurement rather than by reading the code: the action log carried 8 `payload_mismatch`
events, ALL for one artifact (art_wf4e24d222a3d9b9a7, declaring an 878-byte payload with a pinned
sha256), while `procedures/` held ZERO files.

Worth recording that the master plan's framing of this item — "the hooked_skill payload-hash bug
(8/8 payload_mismatch)" — reads as eight skills failing a hash check. It is ONE skill failing eight
times because its file does not exist. Different defect, different fix: the gap was a missing sweep,
not a broken hash computation. A count of events is not a count of subjects.

WHAT THIS ASSERTS: the sweep FINDS a payloadless artifact, and does NOT touch a healthy one. Both
directions, because a sweep that quarantines everything is as broken as one that quarantines nothing
— and this one reaches for the quarantine actuator, so an over-fire silently removes working rules.

ISOLATION: builds its own artifact dicts in memory and calls the sweep with dry=True. It does not
write the DB, does not touch active.json, and does not need a live seat. The wiring (that sweep()
actually calls it, and that a real quarantine lands) was verified live once when it shipped —
art_wf4e24d222a3d9b9a7 went quarantined=true and dropped out of the projection, 21 live -> 20.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_payloadless_artifact_swept")
    try:
        import friction_watchdog as wd
    except Exception as exc:
        print(f"  FAIL  cannot import friction_watchdog ({exc.__class__.__name__}: {exc})")
        return 1

    check("the inverse sweep exists at all",
          hasattr(wd, "_sweep_payloadless_artifacts"),
          "an artifact with no payload would again fail silently on every fire, forever")
    if not hasattr(wd, "_sweep_payloadless_artifacts"):
        return 1

    # --- a payloadless hooked_skill must be FOUND -----------------------------------------
    broken_art = {
        "artifact_id": "art_selftest_nopayload01",
        "type": "hooked_skill",
        "payload": {"path": "art_selftest_nopayload01.md", "bytes": 878,
                    "sha256": "6" * 64},
    }
    found = wd._sweep_payloadless_artifacts({broken_art["artifact_id"]: broken_art}, dry=True)
    check("a hooked_skill whose payload file is absent is FOUND",
          len(found) == 1 and found[0][0] == "art_selftest_nopayload01",
          f"got {found!r} — the defect this file exists for would go unswept")
    check("...and the reason names the actual failure, not a generic one",
          bool(found) and "payload" in found[0][1].lower(),
          f"reason={found[0][1] if found else '(none)'!r}")

    # --- a NON-hooked_skill must be left alone --------------------------------------------
    contract = {"artifact_id": "art_selftest_contract01", "type": "contract",
                "condition": {"all": []}}
    left = wd._sweep_payloadless_artifacts({contract["artifact_id"]: contract}, dry=True)
    check("a plain contract (no payload by design) is NOT swept",
          left == [],
          f"got {left!r} — this sweep calls rollback(), so an over-fire silently quarantines "
          f"working rules. A sweep that flags everything is as broken as one that flags nothing.")

    # --- dry=True must not act ------------------------------------------------------------
    # The value of a dry run is that it is safe to run anywhere. If it quarantines, then
    # `--check` is not a check and every operator habit built on it is wrong.
    import inspect
    src = inspect.getsource(wd._sweep_payloadless_artifacts)
    body_after_dry = src.split("if not dry:")[-1] if "if not dry:" in src else ""
    check("all mutation is behind `if not dry:`",
          "if not dry:" in src and "rollback" in body_after_dry,
          "the rollback call must sit inside the not-dry branch, or --check mutates the seat")

    # --- and it must reach the REAL verifier, not a copy of it ----------------------------
    check("verification is delegated to friction_dispatch._payload_verified, not reimplemented",
          "_payload_verified" in src and "hashlib" not in src,
          "a second hash implementation here would drift from the dispatcher's, and the sweep "
          "would disagree with the thing that actually refuses to fire")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
