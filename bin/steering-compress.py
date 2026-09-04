#!/usr/bin/env python3
"""Recurring compression of always-loaded steering prose. Moves history OUT, keeps the rule IN.

WHY THIS EXISTS. `tasks/lessons.md` has had bounded self-retirement since 2026-06-07
(`scheduling/core-si/lessons-evict.py`, wired at close, `--apply`), and hooks and steering
components have `bin/steering-retire.py`. **The rules files had nothing.** So of the ~11,100 tok
this Core loads on every single prompt, a pruner covered 2,500 — the lessons — and the other ~8,600
could only grow. That is why the budget sat 763 tok over its own ratchet with a working ratchet, a
working evictor, and no contradiction: the mechanisms were real and covered a quarter of the load.

The operator, 2026-08-26: the obvious answer is self-pruning or compression without losing value —
a durable fix, not a one-time patch.

COMPRESS, NOT EVICT — because the unit is different. A lesson is a dated entry that can age out
whole. A rules file is policy that must stay, wrapped around incident history that explains why the
policy exists. Deleting the history loses the reasoning; loading it every prompt costs tokens
forever. So the history MOVES to `docs/steering-detail/<name>.md` and the rule stays where it is,
with one pointer line per file. Nothing is deleted, and the reasoning is one click away for the
reader who needs it — which is almost never mid-turn.

FIVE STRUCTURAL SAFETY PROPERTIES, not five good intentions (the bar lessons-evict set):

1. MOVES, NEVER DELETES. Every moved block is appended verbatim to the detail file under the
   heading it came from, and recorded in `.claude/state/steering-compress-log.jsonl`. Undo is a
   copy-paste, and the log says exactly what to paste where.

2. OPERATIVE PROSE IS EXCLUDED IN CODE. A block containing an imperative marker (MUST, NEVER,
   Do not, Always, forbidden, required...) is never movable, whatever else it looks like. The
   movable set is narrative: a paragraph that carries a DATE and gives no instruction.

3. IT STOPS AT THE BUDGET, not at exhaustion. It moves blocks one at a time and halts the instant
   the seat is back under its own ratchet. An over-eager pass that stripped every candidate would
   be a worse failure than the overage, so the loop is bounded by the goal rather than by supply.

4. BOUNDED PER RUN. `MAX_MOVES` caps a single pass, so a bad heuristic costs a handful of blocks
   that a human can see in one diff, not a rewritten policy tree.

5. IT NEVER TOUCHES THE FILES ANOTHER MECHANISM OWNS. `tasks/lessons.md` belongs to lessons-evict;
   `CLAUDE.md` is Nick's own per-Core overlay and is his to write. Two mechanisms editing one file
   is the accretion this Core keeps being told not to build.

Proposal mode by default; `--apply` to act. Wired at close beside lessons-evict.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import steering_load as _sl  # noqa: E402  — ONE source of truth for what is always-loaded
sys.path.insert(0, str(HERE.parent / ".claude" / "hooks" / "lib"))
import coreuser as _U  # noqa: E402  — operator name from identity.json, never hardcoded

# Owned elsewhere; see property 5.
NOT_OURS = {"tasks/lessons.md", "CLAUDE.md"}

DETAIL_DIR = Path("docs") / "steering-detail"
LOG_REL = Path(".claude") / "state" / "steering-compress-log.jsonl"
MAX_MOVES = 6          # per pass — property 4
MIN_BLOCK_CHARS = 180  # below this the pointer costs as much as the block

DATE_RE = re.compile(r"\b20\d\d-\d\d-\d\d\b")

# Property 2. Deliberately broad: a false NEGATIVE costs tokens, a false POSITIVE moves a rule out
# of the file that is supposed to state it. The asymmetry is not close, so this errs toward keeping.
IMPERATIVE = re.compile(
    r"\bMUST\b|\bNEVER\b|\bALWAYS\b|\bDo NOT\b|\bDo not\b|\bdo not\b|\bdo NOT\b"
    r"|\bDon't\b|\bdon't\b|\bNever\b|\bnever\b|\bAlways\b|\balways\b"
    r"|\brequired\b|\bRequired\b|\bforbidden\b|\bForbidden\b|\bmust\b|\bshould\b"
    r"|^\s*[-*]\s|\bRead\b|\bRun\b|\bUse\b|\bWrite\b|\bVerify\b|\bAsk\b|\bPrefer\b|\bCite\b"
    # SECOND PERSON AND ATTRIBUTION — added after the first proposal run, which selected three
    # blocks it must never have selected: memory.md's "Nothing enforces this. It is on you", the
    # Three Anti-Patterns paragraph, and a block opening "Nick's standing directive, 2026-07-24".
    # That last one is the failure that matters: moving one of Nick's standing directives out of the
    # always-loaded set is strictly worse than being over budget, because the budget costs tokens
    # and this costs an instruction. Anything addressed TO the reader, or attributed to Nick, is
    # policy no matter how much dated prose surrounds it.
    r"|\byou\b|\byour\b|\bYou\b|\bYour\b|"
    rf"\b{re.escape(_U.name())}\b|\bfollow\b|\bassume\b|\bstanding\b"
    r"|\bdirective\b|\bposture\b|\bdefault\b|\brule\b|\bRule\b",
    re.M)


def _blocks(text: str):
    """(start, end, body) for each blank-line-separated block, with its nearest heading."""
    out, pos, heading = [], 0, ""
    for chunk in re.split(r"(\n\s*\n)", text):
        if not chunk.strip():
            pos += len(chunk)
            continue
        if chunk.lstrip().startswith("#"):
            heading = chunk.strip().splitlines()[0].lstrip("# ").strip()
        out.append((pos, pos + len(chunk), chunk, heading))
        pos += len(chunk)
    return out


def movable(body: str) -> bool:
    if len(body) < MIN_BLOCK_CHARS:
        return False
    if body.lstrip().startswith("#"):          # heading
        return False
    if "|" in body or "```" in body:           # table or code
        return False
    if not DATE_RE.search(body):               # not history
        return False
    if IMPERATIVE.search(body):                # operative — property 2
        return False
    return True


def candidates(root: Path):
    out = []
    for rel in _sl.ALWAYS_LOADED:
        if rel in NOT_OURS:
            continue
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(errors="ignore")
        for s, e, body, heading in _blocks(text):
            if movable(body):
                out.append({"rel": rel, "start": s, "end": e, "body": body,
                            "heading": heading, "tok": len(body) // 4})
    out.sort(key=lambda c: -c["tok"])          # biggest first — fewest moves to reach the goal
    return out


def record_hand_move(root: Path, rel: str, to: str, heading: str, body: str,
                     judgement: str) -> None:
    """Log a HAND move with the classifier's verdict AND the reason for overriding it.

    core-ops, on my first remedy ("if the tool refuses and you proceed anyway, log the refusal"):
    at a 100%-refusal calibration **every entry becomes `refused, proceeded anyway`, a marker that
    fires on 100% of entries and therefore carries zero bits.** Within a month it reads as
    boilerplate and the one move that should have been stopped sits in a column of identical
    warnings. That is today's other lesson aimed at my own fix — a signal that does not
    discriminate is not a signal, and "refused" stops discriminating the moment refusal is
    universal.

    So the discriminating field is not WHETHER the classifier refused; it is WHY the author
    overrode it. `judgement` is required and must be non-trivial: a reviewer can then check six
    stated reasons instead of re-deriving six classifications from scratch.
    """
    if not judgement or len(judgement.strip()) < 20:
        raise ValueError("a hand move requires a stated judgement — why this block is history and "
                         "not policy. The classifier's verdict alone carries no information when "
                         "it refuses everything.")
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "from": rel, "to": to, "heading": heading, "tok": len(body) // 4, "body": body,
           "by": "hand-edit", "classifier": "allow" if movable(body) else "refuse",
           "judgement": judgement.strip()}
    logp = root / LOG_REL
    logp.parent.mkdir(parents=True, exist_ok=True)
    with logp.open("a") as f:
        f.write(json.dumps(row) + "\n")


def run(root: Path, apply: bool) -> int:
    try:
        head, total, ceiling = _sl.headroom(root)
    except _sl.UnstableMeasurement as e:
        print(f"[steering-compress] refusing to act: {e}")
        return 0
    if ceiling is None:
        print("[steering-compress] no recorded ratchet on this seat — nothing to compress toward.")
        return 0
    print(f"[steering-compress] {total} tok · ratchet {ceiling} (+{_sl.TOLERANCE}) · headroom {head}")
    if head >= 0:
        print("[steering-compress] under budget — no-op. (This is the normal outcome.)")
        return 0

    # ---- MEASURE THE GATE, NOT JUST THE PROSE ------------------------------------------------
    # core-ops, reading the first week's numbers: this tool can move ~107 tok of ~8,600 always-
    # loaded rules prose (~1%), and every one of the six moves a competent author actually made by
    # hand would have been REFUSED — two of them on a list bullet, one on the bare word "rule".
    #
    # Their conclusion, which is better than the one I reached: **at that calibration hand-editing
    # is not routing around the gate, it is the only way to do the job at all.** "I built a gate
    # then worked around it" is a character finding; "I built a gate whose false-positive rate is
    # 100% on real work" is a design finding, and only the second is fixable by changing the code.
    #
    # So the gate reports its own coverage on every run. If auto-movable stays near zero while the
    # file keeps needing compression, the classifier is the thing to fix — and that is now visible
    # in the output rather than inferable by a peer three hours later.
    all_dated = []
    for rel in _sl.ALWAYS_LOADED:
        if rel in NOT_OURS:
            continue
        fp = root / rel
        if not fp.is_file():
            continue
        for _s, _e, body, _h in _blocks(fp.read_text(errors="ignore")):
            if len(body) >= MIN_BLOCK_CHARS and DATE_RE.search(body) and not body.lstrip().startswith("#"):
                all_dated.append(len(body) // 4)
    cands = candidates(root)
    _auto = sum(c["tok"] for c in cands)
    _pool = sum(all_dated)
    if _pool:
        print(f"  gate coverage: {_auto}/{_pool} tok of dated prose is auto-movable "
              f"({100.0 * _auto / _pool:.0f}%) — the rest needs a human, and a human who is refused "
              f"on every real move will stop asking.")
    if not cands:
        print("[steering-compress] over budget but NO movable block — every candidate is operative "
              "prose or a table. This needs a human editor, not this tool. Reporting, not guessing.")
        return 0

    chosen, freed = [], 0
    for c in cands:
        if len(chosen) >= MAX_MOVES or head + freed >= 0:   # properties 3 and 4
            break
        chosen.append(c)
        freed += c["tok"]

    print(f"  movable blocks found: {len(cands)} · selecting {len(chosen)} · frees ~{freed} tok")
    for c in chosen:
        first = " ".join(c["body"].split())[:88]
        print(f"    - {c['rel']}  ~{c['tok']} tok  [{c['heading'][:34]}]  {first}...")
    if not apply:
        print("  (proposal only — rerun with --apply to MOVE these to docs/steering-detail/)")
        return 0

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logp = root / LOG_REL
    logp.parent.mkdir(parents=True, exist_ok=True)
    by_file: dict[str, list] = {}
    for c in chosen:
        by_file.setdefault(c["rel"], []).append(c)

    for rel, items in by_file.items():
        src = root / rel
        text = src.read_text(errors="ignore")
        detail = root / DETAIL_DIR / (Path(rel).stem + ".md")
        detail.parent.mkdir(parents=True, exist_ok=True)
        if not detail.exists():
            detail.write_text(
                f"# Incident history moved out of `{rel}`\n\n"
                f"Why this file exists: the prose in `{rel}` loads on EVERY prompt. The rules stay "
                f"there; the dated history explaining why each rule exists lives here, where it "
                f"costs nothing until someone needs it. Moved automatically by "
                f"`bin/steering-compress.py`, verbatim, never deleted.\n")
        add = []
        for c in sorted(items, key=lambda x: -x["start"]):   # descending, so offsets stay valid
            text = text[:c["start"]] + text[c["end"]:]
            add.append(f"\n## From “{c['heading']}” — moved {stamp[:10]}\n\n{c['body'].strip()}\n")
            with logp.open("a") as f:
                f.write(json.dumps({"ts": stamp, "from": rel, "to": str(detail.relative_to(root)),
                                    "heading": c["heading"], "tok": c["tok"],
                                    "body": c["body"]}) + "\n")
        with detail.open("a") as f:
            f.write("".join(reversed(add)))
        pointer = f"_Dated incident history for these rules: `{detail.relative_to(root)}`._"
        if pointer not in text:
            text = text.rstrip() + "\n\n" + pointer + "\n"
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        src.write_text(text)
        print(f"  MOVED {len(items)} block(s) out of {rel} -> {detail.relative_to(root)}")

    _, after = _sl.measure(root)
    print(f"[steering-compress] {total} -> {after} tok (ratchet {ceiling}); "
          f"reversible from {LOG_REL}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="perform the moves (default: propose)")
    ap.add_argument("--root", default=None)
    a = ap.parse_args()
    return run(Path(a.root) if a.root else _sl.default_root(), a.apply)


if __name__ == "__main__":
    sys.exit(main())
