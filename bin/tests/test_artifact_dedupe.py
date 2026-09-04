#!/usr/bin/env python3
"""The loop must not accrete restatements of its own reminders.

WHY THIS EXISTS
---------------
Measured on life 2026-08-05: SIX active artifacts all telling Core to route work to Codex, of which
two pairs were byte-identical once the bookkeeping prefix was removed:

    art_aba93e58…  "Recurring ask (10x): use codex alongside core for substantial system/code work"
    art_9267bf57…  "Recurring expectation (instruction-directive): use codex alongside core for
                    substantial system/code work"

Every one of them matches on a prompt about code, so a single turn had three near-identical Codex
reminders injected into context at once. That is token waste, and worse, it is how a user learns to
stop reading injected context — which would defeat every artifact the loop ever mints.

WHY EXISTING DEDUPE MISSED IT. `friction_loop.run()` already dedupes by `canonical_ask` within a
single run. These arrived from DIFFERENT paths (the `ask_` clustering path and the `fc_` friction-case
path) across DIFFERENT closes, so no single run ever saw both. And the install-time check compares
exact lowercased text, which the varying prefix defeats.

Fable named the root cause: three arms define "the same ask" three different ways — minting merges at
Jaccard 0.20, install-time dedupe uses exact text, fitness uses substring. Measurement being stricter
than minting is systematic optimism: a reworded recurrence mints a duplicate on one side and reads as
a separate success on the other. `dedupe_active()` closes the output side of that mismatch; unifying
the three relations is a design change, not a patch.

Run: python3 bin/tests/test_artifact_dedupe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    try:
        from friction_loop import _dedupe_key as key
    except Exception as exc:
        print(f"  FAIL  import friction_loop._dedupe_key — {exc}")
        return 1

    # The real pair from the incident: same directive, different mint path, different prefix.
    a = "Recurring ask (10x): use codex alongside core for substantial system/code work"
    b = ("Recurring expectation (instruction-directive): use codex alongside core for "
         "substantial system/code work")
    check("the two real duplicates collapse to one key", key(a) == key(b),
          f"{key(a)!r} != {key(b)!r}")

    c = "Recurring expectation (instruction-directive): orchestrate codex and fable alongside core by default for substantial work"
    check("a DIFFERENT directive keeps a different key", key(a) != key(c))

    # Prefix variants seen in the wild must all normalise away.
    for variant in [
        "Recurring ask (3x): verify state against the live source before claiming",
        "Recurring ask (1x demonstrated of 3 in cluster): verify state against the live source before claiming",
        "Recurring expectation (correction-frustration): verify state against the live source before claiming",
        "recurring ask (99x):   verify state against the live source before claiming.",
    ]:
        check(f"prefix normalised: {variant[:44]}…",
              key(variant) == "verify state against the live source before claiming",
              f"got {key(variant)!r}")

    # Must NOT over-collapse: superficially similar but materially different asks stay distinct.
    check("similar-but-different asks stay distinct",
          key("Recurring ask (2x): verify state before claiming")
          != key("Recurring ask (2x): verify state against the live source before claiming"))

    # Degenerate inputs must not crash or produce a matching key.
    for bad in (None, "", "   ", "Recurring ask (1x):"):
        try:
            k = key(bad)
            ok = isinstance(k, str)
        except Exception as exc:
            ok = False
            k = f"raised {exc}"
        check(f"degenerate input handled: {bad!r}", ok, str(k))
    check("an empty body yields an empty key (so it is skipped, not grouped)",
          key("Recurring ask (1x):") == "")

    # Whitespace and trailing punctuation must not create false distinctions.
    check("whitespace/punctuation insensitive",
          key("Recurring ask (1x):  keep   the diagram in sync.  ")
          == key("Recurring expectation (x): keep the diagram in sync"))

    print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
