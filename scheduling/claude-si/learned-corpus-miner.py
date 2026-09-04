#!/usr/bin/env python3
"""Learned-workflow corpus miner — Phase 1 of spec-learned-workflow-layer-2026-06-05.

THREE jobs over pattern_observations (the correction corpus):

  1. --backfill : for each JSONL-sourced row, walk the parentUuid chain to
     reconstruct (prompt N-2, response N-1) for the correction (turn N). Walks the
     FULL assistant turn (handles Opus-4.8 turn-splitting: text + tool calls land
     in separate assistant messages). Writes prompt_text + evidence_excerpt.

  2. --embed : voyage-3-large embed of prompt_text for rows missing an embedding,
     so the UserPromptSubmit classifier can cosine-match an incoming prompt against
     past situations.

  3. --detect  : INSERT newly-detected corrections. THIS FILE IS THE SOLE WRITER TO THE
     CORPUS — session-lifecycle.sh:546 says so in as many words ("learned-corpus-miner.py
     --detect is the ONLY writer to" pattern_observations), and run-brain-update.sh:279
     invokes it too. The INSERT is at :372.

--backfill and --embed are read-mostly: they only UPDATE prompt_text / evidence_excerpt /
embedding on EXISTING rows. Nothing here deletes. Brain-vault-sourced rows (no recoverable
JSONL chain) and JSONL rows whose file has aged out of retention are simply skipped.

CORRECTED 2026-08-13 (core-finance DOSE 39). This header said "Two jobs", "No inserts, no
deletes", and "brain_admin BYPASSRLS connection". All three were false, and the middle one is
the sentence someone reads when asking WHICH FILES WRITE TO pattern_observations — the exact
question finance was answering when they hit it. Anyone who trusted it would skip the sole
writer to the corpus that feeds si_induct, contract fitness and the rest of the SI loop.

The connection line was wrong in BOTH directions at once, which is worth keeping. The code
calls connect_corebrain() — brain_app, RLS-scoped — and the inline comment at :487 says so
correctly; only this header disagreed. Three statements in this file carry no org_id
predicate, which is FINE under the RLS-scoped connection it actually uses. So one false
sentence produced a false NEGATIVE (skip the writer) and a false POSITIVE (condemn three safe
queries as a multi-tenant leak) from opposite ends of the same audit.

Reuses scheduling/brain-pg/_env.py for secrets and connect_corebrain() — brain_app, NOT a
BYPASSRLS connection — and the same Voyage model as embed.py.

THE CONNECTION IS RLS-BOUND; THE QUERIES ARE NOT SCOPED BY IT. This line used to read
"RLS-scoped to CORE_ORG_ID", which is true of the connection and false of the effect:
pattern_observations' SELECT policy is USING(true), so brain_app sees all five orgs and every
query here must carry its own `org_id = current_setting('app.current_org_id', true)::bigint`.
Corrected 2026-08-17 after embed() was measured returning 1,219 rows across five Cores into an
external API call. Do not restore the old wording — it is the exact claim that hid the defect.

NOT COVERED BY lint-org-scoping: its SCAN_GLOB is "scheduling/brain-pg/*.py" and this file
lives in claude-si, so the org-scoping linter has never inspected it. Survivable only because
the connection really is RLS-scoped — i.e. the coverage gap is tolerable for the same reason
the header was wrong about.

  python3 learned-corpus-miner.py --backfill
  python3 learned-corpus-miner.py --embed
  python3 learned-corpus-miner.py --detect
  python3 learned-corpus-miner.py --dry-run --backfill   # counts only, no writes
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "brain-pg"))
from _env import load_secrets, connect_corebrain, get_org_id  # noqa: E402

load_secrets()

VOYAGE_MODEL = "voyage-3-large"  # 1024-dim, matches the embedding column

# TRANSCRIPTS COME FROM THIS SCRIPT'S OWN CORE, NOT FROM THE ENVIRONMENT.
#
# This read `os.environ.get("CLAUDE_PROJECT_DIR")` first. The org this writes to comes from
# `get_org_id()`; the transcripts it reads came from an env var — two unrelated sources for the
# two halves of one row, which is the identical defect the 2026-08-05 audit found in
# bin/correction-rate.py, where a leaked var paired one Core's corrections with another Core's
# prompts. Here the consequence is worse than a wrong number: a leaked CLAUDE_PROJECT_DIR makes
# this INSERT another Core's corrections into this Core's org, permanently, in the shared brain.
#
# Codex flagged it on adversarial review of the un-gating commit, and it was right that the
# un-gating widened the exposure: the marker had been keeping this call site from running on the
# one Core that carries it. Anchor to the file, warn on disagreement, exactly as _env does.
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "bin"))
import core_seat as _core_seat  # noqa: E402 — canonical seat/slug resolver
_ROOT = Path(__file__).resolve().parents[2]
_ENV_PD = os.environ.get("CLAUDE_PROJECT_DIR")
if _ENV_PD and Path(_ENV_PD).resolve() != _ROOT:
    print(f"[corpus-miner] NOTE: CLAUDE_PROJECT_DIR={_ENV_PD} but this script lives in {_ROOT} — "
          f"reading {_ROOT}'s transcripts. A session env var leaked across a `cd` is the usual "
          f"cause; mining the other Core's transcripts would write its corrections into this "
          f"Core's org.", file=sys.stderr)
SLUG = _core_seat.transcripts_dir(_ROOT).name
JSONL_DIR = Path.home() / ".claude" / "projects" / SLUG
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "bin"))
import turn_provenance as _prov  # noqa: E402

# KEPT ONLY FOR THE SECOND SELECTION SITE BELOW, and no longer the provenance test. Superseded by
# turn_provenance.is_human_turn(); see _is_nick_prompt for why a blocklist cannot hold.
HOOK_PREFIXES = ("<system-reminder>", "<command-name>", "<command-message>",
                 "<local-command-stdout>", "<user-prompt-submit-hook>",
                 # 2026-06-09 (fix 0, contract-binding proposal): hook/system text
                 # arrives as user-role messages and was being mined as Nick
                 # corrections — gate feedback inflated the recurrence measure.
                 "Stop hook feedback", "⛔", "[Request interrupted",
                 "<details>",
                 # 2026-07-27: background-agent completion notices also arrive as user-role
                 # messages. Adding the instruction detectors surfaced them immediately — a dry run
                 # attributed 33 "instruction-standing" observations to Nick that were actually
                 # <task-notification> bodies. Same class of pollution as the 2026-06-09 fix, caught
                 # before a single row was written because the run was dry.
                 "<task-notification>", "<system-notification>",
                 "[SYSTEM NOTIFICATION")


def _is_nick_prompt(entry: dict) -> bool:
    """True iff Claude Code stamped this turn as typed by a human.

    WAS: `userType == "external"` plus a HOOK_PREFIXES blocklist. Both halves were wrong.
    `userType` is 'external' on Nick's turns AND on every cron tick, so it separated nothing; the
    blocklist was a list of opening strings grown by hand after each incident, and the 30
    scheduler prompts in this Core's own archive match none of them because they open with
    ordinary prose. They were being mined as Nick's instructions.

    Now delegates to bin/turn_provenance.py — one resolver, shared with the friction path, so the
    two miners cannot disagree about whose words they are learning from. See that module for why
    an allowlist on provenance is the only thing that can work here.
    """
    return _prov.is_human_turn(entry)


def build_index(jsonl_path: Path) -> dict:
    by_uuid = {}
    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                u = d.get("uuid")
                if u:
                    by_uuid[u] = d
    except OSError:
        pass
    return by_uuid


def reconstruct_turn(correction_entry: dict, by_uuid: dict) -> tuple[str, str]:
    """Walk UP the parentUuid chain from a correction (user turn N): collect the
    full assistant turn (N-1, turn-wide) until the nearest Nick prompt (N-2)."""
    resp_parts: list[str] = []
    prompt_text = ""
    cur = by_uuid.get(correction_entry.get("parentUuid"))
    seen: set[str] = set()
    while cur is not None:
        u = cur.get("uuid")
        if u in seen:  # cycle guard
            break
        seen.add(u)
        t = cur.get("type")
        if t == "assistant":
            pc = (cur.get("message") or {}).get("content")
            if isinstance(pc, list):
                resp_parts += [p.get("text", "") for p in pc
                               if p.get("type") == "text" and p.get("text")]
            elif isinstance(pc, str) and pc.strip():
                resp_parts.append(pc)
            cur = by_uuid.get(cur.get("parentUuid"))
        elif t == "user":
            if _is_nick_prompt(cur):
                prompt_text = _prov.text_of(cur) or ""
                break
            cur = by_uuid.get(cur.get("parentUuid"))  # tool-result/hook → keep walking
        else:
            cur = by_uuid.get(cur.get("parentUuid"))
    resp_parts.reverse()
    return prompt_text, " ".join(resp_parts).strip()


def backfill(conn, dry_run: bool = False) -> int:
    cur = conn.cursor()
    cur.execute("""SELECT id, source_uuid, source_file FROM pattern_observations
                   WHERE source_uuid IS NOT NULL
                     AND (prompt_text IS NULL OR evidence_excerpt IS NULL OR evidence_excerpt='')""")
    targets = cur.fetchall()
    by_file: dict[str, dict] = {}
    for rid, suid, sfile in targets:
        by_file.setdefault(sfile, {}).setdefault(suid, []).append(rid)

    updated = files_resolved = no_prompt = 0
    for sfile, uuid_map in by_file.items():
        p = Path(sfile)
        if not p.exists():  # JSONL aged out of retention
            continue
        files_resolved += 1
        idx = build_index(p)
        for suid, ids in uuid_map.items():
            entry = idx.get(suid)
            if not entry:
                continue
            prompt, resp = reconstruct_turn(entry, idx)
            if not prompt and not resp:
                continue
            if not prompt:
                no_prompt += 1
            for rid in ids:
                if not dry_run:
                    cur.execute("UPDATE pattern_observations SET prompt_text=%s, evidence_excerpt=%s WHERE id=%s",
                                (prompt[:2000], resp[:2000], rid))
                updated += 1
    if not dry_run:
        conn.commit()
    tag = " DRY-RUN" if dry_run else ""
    print(f"[backfill] targets={len(targets)} files_resolved={files_resolved} "
          f"rows_updated={updated} (no_prompt_recovered={no_prompt}){tag}")
    return updated


# Correction-detection patterns for --detect (corpus self-growth). Label -> regex.
# Replaces the archived detect-patterns.py's role: find new corrections in recent
# JSONL and insert full triples so the corpus keeps growing for re-synthesis.
# THE CORRECTION MARKER, DEFINED ONCE. It is used twice — required by correction-frustration and
# excluded by instruction-emphatic — and those two must be exact complements or the split leaks. Two
# copies of one predicate is the defect class that cost this fleet a night; interpolating one string
# into both makes them impossible to drift apart.
#
# WIDENED TO what/how (core-business, reviewing the split on its own corpus): with `why` as the
# only interrogative, "What the fuck are you talking about?" filed as instruction-emphatic —
# method, not a correction. A near-identical longer case landed correctly ONLY because an
# unrelated "don't" appeared later, i.e. bucketed by incidental co-occurrence rather than by the
# rule. That is a correct verdict reached for the wrong reason, which is not evidence.
#
# `why\b...(?:do|are|is)` tolerates intervening words because "why the fuck do we still have this"
# is a correction and `why (?:do|are|is)` cannot see it — the intensifier sits between the question
# word and the verb. Found by a test case, not by reading the regex.
_CORRECTION_MARKER = (
    r"\bno\b|\bnot\b|\bnever\b|\bstop\b|\bwrong\b|\bdon'?t\b|\bdidn'?t\b|\bdoesn'?t\b|\bisn'?t\b|"
    r"\bcan'?t\b|\b(?:why|what|how)\b(?:\W+\w+){0,3}?\W+(?:do|are|is|did|can'?t|the)\b|\binstead\b|"
    r"should have|shouldn'?t"
)
_INTENSITY = r"\bwtf\b|\btf\b|fuck|\bffs\b"

CORRECTION_RX = {
    "correction-stop-execution": re.compile(r"\b(hol+d+\s*up|wait\s+wait|stop\s+(doing|with|right now)|no\s+stop|wait\s+on\s+building)\b", re.I),
    "correction-explicit-no": re.compile(r"(^\s*(no+|nope)\b)|\bgo\s+back\b", re.I),
    "correction-not-what-i-want": re.compile(r"\bnot\s+what\s+i\s+(want|asked|meant)\b|\bthat'?s\s+not\s+what\s+i\b", re.I),
    "correction-this-is-wrong": re.compile(r"\bthat'?s\s+(wrong|incorrect|not\s+right)\b|\bcompletely\s+(wrong|off)\b", re.I),
    # PROFANITY IS INTENSITY, NOT A SPEECH ACT. This was `\bwtf\b|\btf\b|fuck|\bffs\b` alone —
    # every other row here names something the operator DID ("that's wrong", "you should have", "go back");
    # this one named how he felt. Measured against this Core's provenance-filtered corpus it fires
    # on 76 of 658 human turns (11.6%) and only 18 of those 76 are corrections: 24% precision, the
    # largest cluster in the system and the least accurate.
    #
    # What the other 58 are matters more than the noise. They carry REQUESTS and SPECIFICATIONS —
    # three requests the operator made: clean up loose ends so nothing breaks; connect the bus
    # across all Cores with full permissions; have the system mine frustrations and workflows
    # without human interaction. That is METHOD, filed as correction, which is exactly the
    # signal the instruction-* family below was added to capture and exactly what a method miner
    # most needs. An emphatic specification is not a complaint about something already done.
    #
    # So intensity now has to co-occur with a correction marker. The emphatic REQUESTS fall through
    # to INSTRUCTION_RX where they belong, instead of inflating correction recurrence — which
    # measure-contract-fitness filters by pattern_label, so the mislabel was corrupting fitness math
    # for the biggest cluster in the corpus.
    # \A ANCHOR IS LOAD-BEARING. A bare `(?=...)(?!...)` pair is retried by re.search at every
    # position, so at a later offset the excluded term is already BEHIND the cursor and the negative
    # lookahead passes — 18 turns matched this pattern AND its complement, which is arithmetically
    # impossible for the rule as written. Measured, not reasoned about.
    "correction-frustration": re.compile(
        r"\A(?=[\s\S]*(?:" + _INTENSITY + r"))"
        r"(?=[\s\S]*(?:" + _CORRECTION_MARKER + r"))",
        re.I | re.S),
    "correction-flip-flop": re.compile(r"\byou\s+(just\s+)?said\b|\byou\s+changed\b|\byou\s+flip", re.I),
    "correction-should-have": re.compile(r"\byou\s+should\s+have\b|\bshould\s+have\s+(done|used|put|run)\b", re.I),
    "correction-already-told-you": re.compile(r"\b(like\s+i\s+(said|told\s+you)|already\s+told\s+you|i\s+told\s+you)\b", re.I),
    "recall-miss": re.compile(r"\byou\s+should\s+(know|be\s+able\s+to\s+see)\b|go\s+look\s+in\s+the\s+brain|you\s+forgot|we\s+(talked|discussed)", re.I),
}

# INSTRUCTION patterns (2026-07-27). Every pattern above is a negative-affect trigger — "no", "wtf",
# "fuck", "that's wrong", "you should have". Measured consequence: all 1212 observations for org 1
# carry a correction, and ZERO do not. The system could only ever learn from moments where Nick was
# annoyed. A standing instruction given calmly was invisible to the entire learning loop.
#
# That is not a theoretical gap. Nick's most-repeated directive — orchestrate with Codex and Fable —
# scored just 2 distinct moments in the correction corpus, because only twice was he irritated enough
# to trip a correction pattern. These patterns find 9 instances across the same transcripts.
#
# Precision measured against 705 real user prompts from this Core's transcripts before shipping,
# the same corpus-grounding bar the artifact gate applies to itself:
#   instruction-tooling 1.3% · instruction-preference 2.0% · instruction-standing 2.3% ·
#   instruction-directive 7.4%
#
# These rows are DELIBERATELY labelled instruction-* rather than correction-*. Everything downstream
# that measures correction recurrence (measure-contract-fitness) filters by pattern_label, so mixing
# them would silently corrupt fitness math. Note correction_text is NOT NULL in the schema, so it
# carries the instruction text here — the column name is wrong for this row class, the label is the
# discriminator.
INSTRUCTION_RX = {
    # THE OTHER HALF OF THE correction-frustration SPLIT (see that row above).
    #
    # Narrowing that pattern to intensity+correction was only half a fix: it dropped 33 turns, and
    # 28 of them matched NO other pattern in either family — they would have fallen out of the
    # corpus entirely. Checked before shipping, because a detector narrowed until it detects nothing
    # is the opposite error from the one being fixed and looks like progress in the metrics.
    #
    # What those 28 actually are:
    #     "clean up all loose ends so nothing fucking breaks on any session starts or closes"
    #     "for the last time all hooks old and new need to be tracked and tuned autonomusly"
    #     "ASK FUCKING WHAT DO YOU NEED GO ASK WHY CANT YOU JUST FUCKING PUSH"
    # Standing requirements and method preferences, stated with force. "For the last time" is a
    # RECURRENCE marker — the single most valuable thing a method miner can find, because it means
    # he has had to say it before.
    #
    # So they are rehomed here rather than deleted. instruction-* is the right family for the same
    # reason the family exists: these rows say how work should GO, and measure-contract-fitness
    # filters by pattern_label, so keeping them out of correction-* stops them inflating correction
    # recurrence while still feeding synthesis. Fires on ~5% of human turns, in line with
    # instruction-directive's measured 7.4%.
    "instruction-emphatic": re.compile(
        r"\A(?=[\s\S]*(?:" + _INTENSITY + r"))"
        r"(?![\s\S]*(?:" + _CORRECTION_MARKER + r"))",
        re.I | re.S),
    "instruction-standing": re.compile(r"\b(from now on|going forward|by default|every ?time|whenever|always)\b", re.I),
    "instruction-directive": re.compile(r"\bi want you to\b|\bmake sure (to|you|that)\b|\bdon'?t forget to\b|\bbe sure to\b", re.I),
    "instruction-tooling": re.compile(r"\b(use|work with|bring in|loop in)\s+(codex|fable|opus|sonnet|haiku|the triad)\b", re.I),
    "instruction-preference": re.compile(r"\bi (prefer|like it when|want it)\b|\bi'?d rather\b", re.I),
}


def detect(conn, dry_run: bool = False) -> int:
    """Walk the WHOLE transcript archive, detect new corrections, insert full triples
    (no embedding). Dedups on (source_uuid, pattern_label) in memory, and structurally
    behind uq_patobs_source_label_org, so a re-scan is idempotent and two concurrent
    runs cannot double-insert.

    THERE IS NO LOOKBACK WINDOW, AND THERE USED TO BE A LABEL CLAIMING OTHERWISE.
    A dead `days: int = 7` parameter — no caller ever passed it, no CLI flag set it —
    was interpolated into the completion line as `window=7d`. Nothing filtered on it.
    Removed 2026-08-20 rather than wired up, because the honest behaviour is the one
    we want: a full deduped re-scan means a Core that has not ingested for days loses
    nothing, and that property is worth more than a window. The label cost real
    reasoning time — it was read as a sliding loss-edge and a stale seat was nearly
    reported as losing corrections off the back of it."""
    cur = conn.cursor()
    cur.execute("SELECT source_uuid, pattern_label FROM pattern_observations WHERE source_uuid IS NOT NULL")
    seen = {(u, l) for u, l in cur.fetchall()}
    try:
        files = sorted(JSONL_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    # AN ARCHIVE WITH NO PROVENANCE MUST REFUSE, NOT MINE ZERO.
    #
    # The filter below is an ALLOWLIST on origin.kind == "human". Against an archive that never
    # stamps it — an older export, a fork, a different client — it admits nothing, and every count
    # downstream reads as a clean, healthy, empty result while the Core quietly stops learning.
    # That is the empty-suite failure arriving in the corpus instead of the tests, and it is worse
    # here because no test turns red.
    #
    # Checked ONCE over the whole archive rather than per file: a single old transcript among new
    # ones is normal and must not trip this.
    _any_prov = False
    for _p in files:
        if _prov.archive_has_provenance(build_index(_p).values()):
            _any_prov = True
            break
    if files and not _any_prov:
        print("REFUSING: no transcript in %s carries origin provenance." % JSONL_DIR, file=sys.stderr)
        print("  Mining is an allowlist on origin.kind=='human'. Without it this run would report", file=sys.stderr)
        print("  zero operator turns and look healthy. Cannot separate their words from generated ones.", file=sys.stderr)
        return 0

    inserted = 0
    for p in files:
        idx = build_index(p)
        for uuid, entry in idx.items():
            # SAME RESOLVER AS _is_nick_prompt. These two sites had the same rule typed twice,
            # which is how they would have drifted the moment one was fixed.
            if not _prov.is_human_turn(entry):
                continue
            content = _prov.text_of(entry)
            if content[:100].lstrip().startswith(HOOK_PREFIXES):
                continue
            labels = [lbl for lbl, rx in CORRECTION_RX.items() if rx.search(content)]
            # An instruction is only worth recording when it is NOT already a correction: if Nick is
            # correcting, the correction label is the truer signal and double-labelling the same turn
            # would double-count it as support.
            if not labels:
                labels = [lbl for lbl, rx in INSTRUCTION_RX.items() if rx.search(content)]
            labels = [l for l in labels if (uuid, l) not in seen]
            if not labels:
                continue
            prompt, resp = reconstruct_turn(entry, idx)
            sd = (entry.get("timestamp") or "")[:10] or None
            for lbl in labels:
                if not dry_run:
                    # ON CONFLICT DO NOTHING pairs with the partial unique index added in
                    # migrations/2026-08-05b. The `seen` set above dedupes within ONE process;
                    # it cannot see a concurrent run. Since 093a285 un-gated this call site, the
                    # close and the nightly heavy run can both mine on the same Core, and Codex
                    # flagged that both could pass the in-memory check and insert the same row.
                    # 135 such duplicate groups already existed when this was written. With the
                    # index live, a raw INSERT would now RAISE and fail the close — degrading to
                    # a no-op is the behaviour that keeps the close green.
                    cur.execute(
                        """INSERT INTO pattern_observations
                           (pattern_label, evidence_excerpt, correction_text, prompt_text,
                            session_date, source_file, source_uuid, detector_version, org_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                                   (current_setting('app.current_org_id',true))::bigint)
                           ON CONFLICT (source_uuid, pattern_label, org_id)
                             WHERE source_uuid IS NOT NULL
                           DO NOTHING""",
                        (lbl, resp[:2000], content[:2000], prompt[:2000] or None,
                         sd, str(p), uuid, "learned-miner-v1"),
                    )
                seen.add((uuid, lbl))
                inserted += 1
    if not dry_run:
        conn.commit()
    print(f"[detect] new correction observations inserted: {inserted}"
          f"{' DRY-RUN' if dry_run else ''} (full archive, no window; files={len(files)})")

    # Auto-trigger: if the corpus has grown >= RESYNTH_THRESHOLD since the last
    # resynth, drop a 'due' marker that SessionStart surfaces (the LLM resynth pass
    # then runs in-session via learned-resynth.py --prepare → subagent → --apply).
    if not dry_run:
        try:
            RESYNTH_THRESHOLD = 80
            state = Path(__file__).resolve().parents[2] / ".claude" / "state"
            # ONLY the resynth marker is retired by the SI-spine cutover — not the mining above.
            # Conflating the two is what froze org 1's corpus from 2026-07-23 to 2026-08-05: the
            # gate was added at both call sites to stop a zombie "learned contracts may be stale"
            # item from resurfacing forever, and it took the WRITER down with it. friction_loop.py
            # is a pure reader of pattern_observations, so adopting the spine deleted the only
            # writer and left a live consumer running on a frozen table — which looks healthy from
            # every angle, because nothing announces a table that simply stopped growing. All four
            # peer Cores refused to adopt the spine until this was separated (core-bus #267-#276).
            if (state / ".si-unified-spine").exists():
                return inserted
            # ORG-SCOPED, and explicitly so. This read was `SELECT count(*) FROM
            # pattern_observations` with no filter, compared against a PER-CORE watermark.
            # RLS does not save it — measured on brain_app: unfiltered 1711 vs org-1 1218 — so
            # every Core subtracted its own watermark from the FLEET total. Consequence, caught
            # by core-business twice (2026-08-04 and again tonight): a busy day on life fires a
            # MANDATORY resynth directive on four peer Cores, ordering each to re-tune its
            # behaviour on another Core's evidence. Business measured "175 new corrections"
            # against a corpus of 159 rows total, ever; by the time life fixed it the same
            # phantom had grown to 187. Business held and did not spend the subagent.
            org = get_org_id()
            cur.execute("SELECT count(*) FROM pattern_observations WHERE org_id = %s", (org,))
            total = cur.fetchone()[0]
            wm = state / ".learned-resynth-at"
            last = int(wm.read_text().strip()) if wm.exists() else 0
            # Self-healing migration: every existing watermark was written in FLEET units, so it
            # can exceed this org's real corpus. Rebase once rather than leaving a Core unable to
            # ever trigger — and rebase rather than zeroing, which would fire a spurious resynth
            # on the first run after the fix.
            if last > total:
                wm.write_text(str(total))
                print(f"[detect] resynth watermark rebased {last} -> {total} "
                      f"(was fleet-scoped; org {org} corpus is {total})")
                last = total
            if total - last >= RESYNTH_THRESHOLD:
                state.mkdir(parents=True, exist_ok=True)
                (state / ".learned-resynth-due").write_text(
                    f"{total - last} new corrections since last resynth — contracts may be stale.\n"
                    f"Run: learned-resynth.py --prepare → regenerate guidance (subagent) → --apply")
                print(f"[detect] RESYNTH DUE ({total - last} new since watermark {last})")
        except Exception:
            pass
    return inserted


def _vec_literal(vec) -> str:
    """pgvector accepts a text literal like '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def embed(conn, dry_run: bool = False, batch: int = 32) -> int:
    cur = conn.cursor()
    # ORG SCOPE (2026-08-17): this SELECT feeds an EXTERNAL API (voyage-3-large). Without the
    # predicate it returned every org's prompt_text — 1,219 rows across five Cores measured on
    # 2026-08-17 — because pattern_observations' SELECT policy is USING(true), so RLS does not
    # scope it. The module docstring claimed "RLS-scoped to CORE_ORG_ID", which is true of the
    # CONNECTION and was false of the EFFECT. Never let this query leave without the predicate.
    cur.execute("""SELECT id, prompt_text FROM pattern_observations
                   WHERE org_id = current_setting('app.current_org_id', true)::bigint
                     AND prompt_text IS NOT NULL AND prompt_text <> '' AND embedding IS NULL""")
    rows = cur.fetchall()
    if not rows:
        print("[embed] nothing to embed")
        return 0
    if dry_run:
        print(f"[embed] would embed {len(rows)} rows DRY-RUN")
        return 0
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        sys.exit("[embed] VOYAGE_API_KEY missing (expected in ~/.claude/secrets.env)")
    import voyageai
    client = voyageai.Client(api_key=key)
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        texts = [(t or "")[:8000] for _, t in chunk]
        r = client.embed(texts, model=VOYAGE_MODEL, input_type="document")
        for (rid, _), vec in zip(chunk, r.embeddings):
            cur.execute("UPDATE pattern_observations SET embedding=%s::vector WHERE id=%s",
                        (_vec_literal(vec), rid))
        conn.commit()
        done += len(chunk)
        print(f"[embed] {done}/{len(rows)}")
    print(f"[embed] embedded {done} rows")
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--detect", action="store_true", help="insert newly-detected corrections (corpus growth)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.backfill or args.embed or args.detect):
        ap.error("specify --backfill, --detect, and/or --embed")
    conn = connect_corebrain()  # brain_app; RLS-scoped to CORE_ORG_ID
    try:
        if args.detect:
            detect(conn, dry_run=args.dry_run)
        if args.backfill:
            backfill(conn, dry_run=args.dry_run)
        if args.embed:
            embed(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
