#!/usr/bin/env python3
"""An agent spec may not claim "no side effects" while holding a tool that writes.

WHY THIS EXISTS (2026-08-12, Phase 5). The master plan's Phase 5 asserts "Writes stay
single-threaded. Agents are added as read-only or advisory roles." Audited against the specs:

    close-reconciler   tools: Read, Grep, Glob, Bash
    sentinel-code      tools: Read, Bash
    sentinel           tools: Read, Bash

All three declared a read-only posture. All three hold **Bash**, which is an unrestricted write
channel. And `pretooluse-guard.sh` cannot help: it has no agent_type check anywhere, so it cannot
distinguish a subagent's Bash from the main thread's, and it gates only OUTWARD actions — push, send,
curl. An ordinary local write or delete passes.

So three specs stated a safety property that no mechanism provided:

    sentinel.md        "It has no side effects."
    sentinel-code.md   "It has no side effects."
    close-reconciler   "Read-only; proposes edits, never makes them."

THIS IS THE NIGHT'S MOST EXPENSIVE DEFECT CLASS, in its third venue. The same day,
`sentinel-approve.sh:123-125` claimed "peer-msg send() now refuses to post as another Core" — the
single sentence justifying peer approval over two mechanisms the same file rejects — and it was false
for three days. Three seats read that file hunting exactly this and all three walked past it, because
a stated property reads as a settled fact rather than a claim needing a test.

A false safety claim is worse than none: a reader relaxes against a net that was never strung.

WHAT THIS ASSERTS. Not "agents must be read-only" — removing Bash would break them, since a reviewer
that cannot run `git diff` cannot review. The assertion is that the CLAIM must match the TOOLS: a spec
holding a write-capable tool must not assert it cannot write, and must carry the caveat instead.

Deliberately checks the spec files rather than runtime behaviour: the defect is a documentation claim,
and documentation is where it must be caught.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / ".claude" / "agents"

# Tools that can write, delete, or otherwise change the seat.
WRITE_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit", "MultiEdit"}

# Phrases that assert an absolute absence of writes. Substring-matched against the spec, lowercased.
ABSOLUTE_CLAIMS = (
    "has no side effects",
    "no side effects.",
    "never makes them",
    "cannot write",
    "makes no writes.",
)

# The caveat that makes an absolute-sounding spec honest. One canonical wording so a grep finds all.
CAVEAT = "nothing enforces that"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def main() -> int:
    print("test_agent_specs_dont_overclaim")
    specs = sorted(AGENTS.glob("*.md"))
    check("agent specs exist to check", bool(specs),
          f"no *.md under {AGENTS} — this test would pass while measuring nothing")
    if not specs:
        return 1

    for f in specs:
        text = f.read_text()
        # WHITESPACE-NORMALISED. The first version matched raw text, so a claim wrapped across two
        # lines — which is how these specs are actually written — slipped past. It caught 1 of 3
        # while looking like it checked all 3, which is the same shape as the defect it guards.
        low = " ".join(text.lower().split())
        m = re.search(r"^tools:\s*(.+)$", text, re.M)
        declared = {t.strip() for t in (m.group(1) if m else "").split(",") if t.strip()}
        # No `tools:` line means ALL tools — strictly more dangerous, not less.
        writes = WRITE_TOOLS & declared if m else WRITE_TOOLS

        claims = [c for c in ABSOLUTE_CLAIMS if c in low]
        if not writes or not claims:
            continue

        check(f"{f.name}: claims {claims[0]!r} while holding {sorted(writes)} — carries the caveat",
              CAVEAT in low,
              f"the spec asserts an absolute absence of writes and declares a write-capable tool. "
              f"Either drop the claim or add the caveat (see .claude/agents/sentinel.md §Role). "
              f"pretooluse-guard.sh has no agent_type check and gates only OUTWARD actions, so "
              f"nothing stops a local write.")

    # The test must be capable of failing — if no spec both claims and can write, it proved nothing.
    checked = [f for f in specs
               if any(c in f.read_text().lower() for c in ABSOLUTE_CLAIMS)]
    check("at least one spec makes an absolute no-write claim (else this test is vacuous)",
          bool(checked),
          "no spec asserts absence of writes, so this file asserts nothing about any of them")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
