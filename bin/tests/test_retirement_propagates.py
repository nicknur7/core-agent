#!/usr/bin/env python3
"""A file retired on the baseline must be deletable on a puller. Tombstones are the only path.

WHY THIS EXISTS (core-finance, measured on four seats, 2026-08-13).

`sync-from-baseline.sh` rsyncs shared dirs with `rsync -a` — **no `--delete`**, additive by design.
The orphan-cleanup pass beside it is bounded to `scheduling/` and is driven by the manifest's
`shared.dirs`, never by a baseline deletion. So `git rm` on the baseline removes a file THERE AND
NOWHERE ELSE, and pulling a thousand times will not remove it. `bin/retire-legacy.py` moves
retirements into `scheduling/_archive/`, which is not a shared dir, so the archive never arrives
either: on the writer the file is gone AND archived, on a puller it is simply still there.

Six were on disk when this was written, the oldest retired 2026-05-15, across 3–4 of five seats.

THE HALF WITH TEETH IS DOCUMENTATION. `.claude/rules/privacy.md` stated the close-reconciler
dir-form spec was removed — true on the writer, false on three seats where it sits beside the flat
form carrying the OLD output contract, in the very file that tells readers not to cite a dir-form
spec. And that file's own prescribed remedy ("verify against a fresh baseline clone") CANNOT catch
it: the clone is the one place the deletion definitely landed.

WHAT THIS ASSERTS. Not that any particular file is gone — a puller legitimately still has them
until it pulls. It asserts the MECHANISM exists and is safe:

  · the manifest carries a `retired` list, and every entry is genuinely absent from THIS seat
    (the writer). Tombstoning a path that still exists here would delete a live file fleet-wide.
  · the sync script actually consults it — a tombstone list nothing reads is the void-write shape
    this session has been cataloguing, and would be worse than none because it looks fixed.
  · the pass refuses a path that escapes the Core or that the manifest also calls per_core_keep.
    `rsync --delete` was rejected for exactly this: it removes whatever the destination has and the
    source lacks, so one imperfect exclude takes a peer's own data, on four seats, unattended.
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "bin" / "sync-manifest.json"
SYNC = REPO / "bin" / "sync-from-baseline.sh"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_retirement_propagates")
    if not MANIFEST.is_file() or not SYNC.is_file():
        print("  note: CHECK DID NOT RUN — manifest or sync script absent")
        print("\n0 passed, 0 failed")
        return 0

    man = json.loads(MANIFEST.read_text())
    sync = SYNC.read_text()
    retired = man.get("retired", [])

    check("the manifest declares a `retired` tombstone list",
          isinstance(retired, list),
          "no `retired` key — a baseline deletion has no way to reach a puller, which is the "
          "defect this file exists for")

    # MATCH THE jq EXPRESSION, NOT THE WORD. This first read
    #     ".retired[]?" in sync or '"retired"' in sync
    # and the `or` clause matched the word "retired" inside this pass's own EXPLANATORY COMMENT.
    # Dosed by rewriting the jq to `.NOTHING[]?` — the script stopped reading the list entirely and
    # the assertion still passed, because the comment describing the feature survived the removal
    # of the feature. A mention is not an assertion; the prose about a mechanism outlives it.
    reads_it = bool(re.search(r"jq\s+-r\s+'\.retired\[\]\?'", sync))
    check("the sync script actually READS the list (jq expression, not the word)",
          reads_it,
          "no `jq -r '.retired[]?'` in sync-from-baseline.sh — the tombstone list would be a write "
          "nobody reads, which is WORSE than no list, because the manifest would look like the fix "
          "while every retired file stayed on every puller")

    # EVERY tombstone must be absent HERE. This is the assertion that protects the fleet: this seat
    # is the baseline writer, so a path listed while still present here would be deleted on every
    # puller while remaining alive on the writer — the current bug with the sign flipped.
    still_here = [p for p in retired if (REPO / p).exists()]
    check(f"every tombstoned path is genuinely absent from the writer ({len(retired)} listed)",
          not still_here,
          "these are listed as retired but still exist HERE, so a pull would delete a live file on "
          f"every other seat: {still_here}")

    # No tombstone may escape the Core or collide with per_core_keep.
    escapes = [p for p in retired if p.startswith("/") or ".." in p]
    check("no tombstone escapes the Core directory",
          not escapes,
          f"absolute or parent-traversing paths would delete outside the repo: {escapes}")

    # GLOB-AWARE, because per_core_keep is 15/31 GLOB PATTERNS and this check was exact-match —
    # the same defect as the shell guard it verifies, so it could never have caught it. A test that
    # reproduces the bug it is testing for is worse than no test: it reports the protection as
    # present. Found by core-finance on the shell side hours after both shipped.
    def protected(path: str) -> bool:
        for k in man.get("per_core_keep", []):
            base = k[:-3] if k.endswith("/**") else k
            if path == base or path.startswith(base.rstrip("/") + "/"):
                return True
        return False

    collide = [p for p in retired if protected(p)]
    check("no tombstone is protected by per_core_keep (glob-aware)",
          not collide,
          f"a path in both lists is a manifest bug, and acting on it deletes a peer's OWN data: "
          f"{collide}")

    # And prove the matcher is actually glob-aware, rather than passing because nothing collides.
    # Every one of these is a real file a future tombstone could plausibly name.
    must_refuse = [".claude/agents/sentinel/CLAUDE.md", "secrets/keys.json",
                   "memory/current-state.md", "sessions/2026-08-13.md"]
    unprotected = [p for p in must_refuse if not protected(p)]
    check("glob entries protect the FILES UNDER them, not just the literal pattern",
          not unprotected,
          f"these sit under a per_core_keep glob and are not recognised as protected: "
          f"{unprotected}. The shipped shell guard had exactly this hole — it refused the literal "
          "string `secrets/**` and would have deleted `secrets/keys.json`.")

    # The shell guard must be glob-aware too, not merely the Python mirror of it.
    check("the shell guard strips a trailing /** rather than comparing literally",
          "sub(" in sync and "startswith(" in sync,
          "sync-from-baseline.sh still compares per_core_keep entries by equality, so every glob "
          "entry — memory/**, secrets/**, both sentinel trust-root specs — protects nothing")

    check("the pass refuses escaping paths in code, not only by convention",
          bool(re.search(r"/\*\|\*\.\.\*|\.\.", sync)) and "REFUSED" in sync,
          "no refusal branch found in sync-from-baseline.sh — the manifest could be edited to "
          "carry an escaping path and nothing would stop it")

    check("the pass refuses a per_core_keep collision in code",
          "per_core_keep" in sync and "REFUSED" in sync,
          "the script does not cross-check per_core_keep before deleting")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
