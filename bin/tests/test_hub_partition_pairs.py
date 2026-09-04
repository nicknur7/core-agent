#!/usr/bin/env python3
"""partition_drifted must match on (kind, name) — EXECUTED, not grepped.

WHAT THIS REPLACES. `bin/tests/test_hub_refresh_identity.py`'s four "Invariant 1" checks each run a
`re.search` against compile-truth-refresh.py's SOURCE TEXT — they assert that a particular expression
appears, never that the partition behaves. Two consequences, both live:

  · a correct REFACTOR that preserves the behaviour turns them red
  · a REWRITE that reintroduces the 2026-07-30 phantom-pair bug in a different spelling passes them

That is the same shape as three other tests found today: keyed to an implementation rather than a
property. This file executes `partition_drifted()` against a real vault instead.

THE DEFECT IT GUARDS, which is not hypothetical. `ingest_refresh` updates
`WHERE entities.kind = ? AND entities.name = ?`, so kind is half the identity. The partition once
built a NAME-ONLY set and took `kind` from whichever hub file matched — inventing pairs the DB has
never had. Measured 2026-07-30: two rows drifted, the vault held both `entities/receipt-reader.md` and
`topics/receipt-reader.md`, and partition emitted THREE batches. `Topic/"Receipt Reader"` exists in no
DB row, so a Sonnet worker compiled a hub that updated nothing — paid for, verified, ingested, and
silently dropped.

Run: python3 bin/tests/test_hub_partition_pairs.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
SRC = ROOT / "scheduling" / "brain-pg" / "compile-truth-refresh.py"

HUB = """---
type: %s
name: %s
---

# %s

Some body text.
"""


def build_vault(base: Path) -> Path:
    """A vault holding the SAME NAME under two kinds — the exact 2026-07-30 shape."""
    brain = base / "brain"
    (brain / "entities").mkdir(parents=True)
    (brain / "topics").mkdir(parents=True)
    (brain / "entities" / "receipt-reader.md").write_text(HUB % ("entity", "Receipt Reader", "Receipt Reader"))
    (brain / "topics" / "receipt-reader.md").write_text(HUB % ("topic", "Receipt Reader", "Receipt Reader"))
    return brain


def build_vault_topic_only(base: Path) -> Path:
    """ONLY a Topic hub exists for this name — no Entity file anywhere in the vault.

    Used by the mutation control below. With `build_vault` (both files present), a kind-blind
    matcher lands both hubs in the SAME lookup bucket and the 2026-07-28 collision guard refuses
    both — loud, not the silent corruption this file is named for. With only the Topic file
    present, a drifted Entity/"name" row has exactly ONE name match (no collision to trigger), so
    a kind-blind matcher emits it silently as (Topic, name) — the actual 2026-07-30 shape: a
    batch entry for a (kind, name) pair the DB never drifted.
    """
    brain = base / "brain_topic_only"
    (brain / "topics").mkdir(parents=True)
    (brain / "topics" / "receipt-reader.md").write_text(HUB % ("topic", "Receipt Reader", "Receipt Reader"))
    return brain


def run_partition(module_path: Path, base: Path, brain: Path):
    """Execute the REAL partition against a planted vault. Returns the emitted entries."""
    inst = base / "inst"
    (inst / "scheduling" / "brain-pg").mkdir(parents=True, exist_ok=True)
    report = base / "drift.json"
    # ONLY the Entity row drifted. A name-only matcher will also pull in the Topic file.
    report.write_text(json.dumps({"drift_records": [{"kind": "Entity", "name": "Receipt Reader"}]}))

    env_keep = {k: os.environ.get(k) for k in ("CORE_BRAIN", "CORE_INSTANCE")}
    os.environ["CORE_BRAIN"] = str(brain)
    os.environ["CORE_INSTANCE"] = str(inst)
    try:
        spec = importlib.util.spec_from_file_location("ctr_%d" % id(module_path), str(module_path))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.partition_drifted(report, 1)
        # The batch file is {"batch_id", "n_entities", "entities": [...]}, not a bare list.
        out = []
        for bp in sorted((inst / "scheduling" / "brain-pg" / "compile-truth-work").glob("refresh-batch-*.json")):
            for e in (json.loads(bp.read_text()).get("entities") or []):
                out.append((e.get("kind"), e.get("name")))
        return sorted(out)
    finally:
        for k, v in env_keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    p = f = 0

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    print("=== partition matches on (kind, name), executed ===\n")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        brain = build_vault(base)
        got = run_partition(SRC, base, brain)
        check("a drifted Entity pulls in ONLY the entity hub, not the same-named topic",
              got == [("Entity", "Receipt Reader")], "emitted: %r" % (got,))

        # THE CONTROL. Without it, a PASS is consistent with a partition that emits nothing at all,
        # or with the vault fixture being wrong. Reintroduce the 2026-07-30 defect in a COPY — a
        # name-only `exact` match — and require the SAME fixture to produce the phantom pair.
        print("\n--- control: the fixture must actually be able to produce the phantom pair ---")
        broken = base / "broken.py"
        src = SRC.read_text()
        # RE-POINTED 2026-08-31. The matcher this used to mutate — a single
        # `exact = (p["kind"], p["name"]) in drift_pairs` line — was replaced on 2026-09-01 by
        # "matching flipped to be PER DRIFTED PAIR" (compile-truth-refresh.py's own comment):
        # hub files are now indexed once into exact_hub_index/norm_hub_index keyed by (kind, name),
        # and each drifted pair looks itself up. why-red's diagnosis for this whole failing suite
        # named exactly this: the TEST matches the baseline, the SUBJECT (this file) does not —
        # this repo is ahead of the pushed baseline, not behind it. The property under test is
        # unchanged (kind is half the identity), so the control is re-pointed at the new shape
        # rather than deleted: drop `kind` from BOTH the index keys and the pair lookup, which is
        # the new code's equivalent of the old name-only bug.
        targets = [
            ('exact_hub_index.setdefault((p["kind"], p["name"]), [])',
             'exact_hub_index.setdefault((None, p["name"]), [])'),
            ('norm_hub_index.setdefault((p["kind"], _norm(p["name"])), [])',
             'norm_hub_index.setdefault((None, _norm(p["name"])), [])'),
            ('candidates = exact_hub_index.get((dk, dn)) or norm_hub_index.get((dk, _norm(dn))) or []',
             'candidates = exact_hub_index.get((None, dn)) or norm_hub_index.get((None, _norm(dn))) or []'),
        ]
        missing = [old for old, _ in targets if old not in src]
        if missing:
            check("could locate the (kind, name) indexing lines to break", False,
                  "the expression changed again; re-point this control rather than deleting it — "
                  "missing: %r" % (missing,))
        else:
            # partition_drifted imports compile-truth.py RELATIVE TO ITS OWN LOCATION, so the
            # broken copy needs its sibling beside it — otherwise the control dies with
            # FileNotFoundError and reads as "the control could not run", which is exactly the
            # silent-failure shape this file exists to refuse.
            import shutil as _sh
            _sh.copy2(SRC.parent / "compile-truth.py", base / "compile-truth.py")
            mutated = src
            for old, new in targets:
                mutated = mutated.replace(old, new, 1)
            broken.write_text(mutated)
            # Use the TOPIC-ONLY vault (see build_vault_topic_only): the two-hub vault above
            # would run this mutation into the collision guard instead of a phantom emission —
            # a real behavior difference from the pre-guard 2026-07-30 bug, but not the one this
            # control is checking for.
            topic_only_brain = build_vault_topic_only(base)
            got_bad = run_partition(broken, base, topic_only_brain)
            check("name-only matching DOES emit the phantom Topic pair for an Entity drift "
                  "(so the check has teeth)",
                  ("Topic", "Receipt Reader") in got_bad, "emitted: %r" % (got_bad,))
            got_good_topic_only = run_partition(SRC, base, topic_only_brain)
            check("...and the shipped (kind, name)-aware version refuses instead of mis-emitting "
                  "(no Entity hub exists here — nothing SHOULD match)",
                  got_good_topic_only == [], "emitted: %r" % (got_good_topic_only,))
            check("...and the shipped version never emits the phantom pair against the "
                  "original two-hub vault either", ("Topic", "Receipt Reader") not in got)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
