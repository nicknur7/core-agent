#!/usr/bin/env python3
"""test-core-manifest.py — contract regression guard for the discovery engine.

Asserts the invariants that matter (Codex's EXACT component counts + namespace,
sanitization, fingerprint stability). A "Codex exists" smoke test would miss PARTIAL
discovery — e.g. if a Claude Code update moved plugin command dirs, Codex might show
1 command instead of 8 and nothing would notice. This pins the shape.

Run manually or from CI:  python3 bin/test-core-manifest.py   (exit 0 pass, 1 fail)
No writes — imports the generator and asserts on in-memory discovery.
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gen_core_manifest", HERE / "gen-core-manifest.py")
gcm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gcm)

fails = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        fails.append(msg)


def main():
    root = gcm._root()
    print("== host discovery ==")
    host = gcm.discover_host()
    inv = host["inventory"]

    # Envelope invariants.
    check(host["schema_version"] == gcm.SCHEMA_VERSION, "host manifest has current schema_version")
    check(host["scope"] == "host" and host["scope_id"] == "host", "host scope tagged correctly")
    check(host["fingerprint"].startswith("sha256:"), "host manifest carries a fingerprint")

    # Codex plugin present + EXACT component counts (the partial-discovery guard).
    codex = next((p for p in inv["plugins"] if p["id"] == "plugin:codex@openai-codex"), None)
    check(codex is not None, "codex plugin discovered")
    if codex:
        comps = codex.get("components", {})
        check(codex.get("enabled") is True, "codex enabled")
        check(comps.get("commands") == 8, f"codex has 8 commands (got {comps.get('commands')})")
        check(comps.get("skills") == 3, f"codex has 3 skills (got {comps.get('skills')})")
        check(comps.get("agents") == 1, f"codex has 1 agent (got {comps.get('agents')})")
        check(comps.get("hooks") == 3, f"codex has 3 hooks (got {comps.get('hooks')})")

    # Namespaced command IDs (no collision with core-local /review etc.).
    codex_cmds = [c["name"] for c in inv["commands"] if "plugin:codex" in c["id"]]
    check("/codex:review" in codex_cmds, "codex commands namespaced (/codex:review present)")
    check(len(codex_cmds) == 8, f"8 codex commands in flat command list (got {len(codex_cmds)})")

    # Sanitization — mcp entries must NEVER carry env/args/secret keys.
    safe = {"id", "name", "source", "scope", "transport", "wiring"}
    bad = [m["name"] for m in inv["mcp"] if set(m) - safe]
    check(not bad, f"host mcp entries sanitized (offenders: {bad})")

    print("== core discovery ==")
    core = gcm.discover_core(root)
    cbad = [m["name"] for m in core["inventory"]["mcp"] if set(m) - safe]
    check(not cbad, f"core mcp entries sanitized (offenders: {cbad})")

    print("== fingerprint stability ==")
    fp1 = gcm.discover_host()["fingerprint"]
    fp2 = gcm.discover_host()["fingerprint"]
    check(fp1 == fp2, f"host fingerprint stable across runs ({fp1} == {fp2})")

    print()
    if fails:
        print(f"RESULT: {len(fails)} FAILED")
        sys.exit(1)
    print("RESULT: all contract invariants hold ✓")
    sys.exit(0)


if __name__ == "__main__":
    # SEAT-LOCKED: REFUSE AN ARGUMENT RATHER THAN DISCARD IT. core-business, bus #971. It aimed a
    # seat-locked scanner at life's tree; Python accepted the stray argv silently, the tool scanned
    # its OWN seat, and the two runs "converged" — which reads as a regressed fix rather than as a
    # tool that never moved. A WRONG NUMBER, not no number. That is the whole defect: not the
    # seat-locking, which is fine, but claiming to honour a tree it discards.
    if len(sys.argv) > 1:
        sys.exit("REFUSING: %s is seat-locked and cannot scan %r.\n"
                 "It resolves its own Core from __file__. To measure another Core, run it from "
                 "that Core's tree or use seat_matrix staging." % (Path(__file__).name, sys.argv[1]))
    main()
