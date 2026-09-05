#!/usr/bin/env python3
"""Every path a Makefile RECIPE invokes must exist in the tree.

WHY. `make init-brain` invoked bin/init-brain.sh for weeks after that script was tombstoned in
bin/sync-manifest.json — deleted on every seat at pull — so the target was broken fleet-wide and
nothing said so (codex, bus #5943/#5944, 2026-09-04). Help text and comments may NAME retired paths
(that is how history is explained); recipes may not RUN them. This parses recipe lines only.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MK = ROOT / "Makefile"


def main() -> int:
    if not MK.exists():
        print("  SKIP — no Makefile at the repo root")
        return 0
    bad, checked = [], 0
    for ln in MK.read_text().splitlines():
        if not ln.startswith("\t") or ln.lstrip("\t").startswith(("@#", "#", "@echo")):
            continue                                    # comments and help text are not invocations
        for tok in re.findall(r"(?<![\w./-])(bin/[A-Za-z0-9_./-]+|scheduling/[A-Za-z0-9_./-]+|template/[A-Za-z0-9_./-]+)", ln):
            tok = tok.rstrip(".,;:")
            checked += 1
            if not (ROOT / tok).exists():
                bad.append((tok, ln.strip()))
    for tok, ln in bad:
        print(f"  FAIL  recipe invokes a path that does not exist: {tok}\n          {ln}")
    print(f"  {'FAIL' if bad else 'PASS'}  every path a Makefile recipe invokes exists ({checked} checked, {len(bad)} missing)")
    print(f"\n=== Results: {0 if bad else 1} passed, {1 if bad else 0} failed ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
