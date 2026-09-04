#!/usr/bin/env python3
""""Matched but did not fire" must be readable, not inferred.

WHY THIS EXISTS (2026-08-12, master-plan Phase 4). The dispatcher wrote `dispatch_nofire` carrying
only `considered` — how many artifacts were evaluated. That number cannot separate the two reasons
nothing fired, and they call for OPPOSITE responses:

    considered=19, matched=[]          no trigger fits the traffic     -> re-derive the trigger
    considered=19, matched=[art_x]     art_x matched and was DROPPED   -> find out what ate it

MEASURED BEFORE THE FIELD WAS ADDED: 6 of 20 active artifacts had never fired, and ZERO of the six
carried a named suppression reason (`budget_capped` / `payloadless_artifact` / `orphan_payload`).
For all six the two rows above were indistinguishable, so "this trigger is a fossil" and "this
trigger works and something ate the result" looked identical to every consumer. Re-deriving a rule
whose trigger is fine is how a working artifact gets replaced by a worse one.

The named suppressions already cover their own cases; this covers everything else, which was
everything that mattered for those six.

COSTS NO NEW ROWS. `dispatch_nofire` already fired once per session per event — deliberately, since
PreToolUse runs hundreds of times a day and a row per invocation would bury the ledger. This only
gives the existing row the field that makes it answerable, and in doing so retires that row's own
standing complaint: it was written and read by nothing.

WHAT THIS ASSERTS, through the REAL entry point (`friction_dispatch.run`) with stdin and env as the
hook supplies them — not a hand-rolled call to an internal, which would test a copy of the path:

  1. a prompt no trigger matches      -> matched == []           (and it is PRESENT, not absent)
  2. a prompt a trigger DOES match,
     where the effect yields nothing  -> matched names the artifact
  3. the two produce DIFFERENT rows   — the whole point; if they ever collapse, the distinction is
                                        gone and nothing downstream can tell fossil from suppressed.
"""
import hashlib
import io
import json
import os
import sys as _sys
from pathlib import Path as _PPath
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


COND = {"all": [{"op": "prompt_regex", "value": r"\bzebraxyz\b"}]}


def _artifact(aid):
    # An EMPTY effect message: the artifact matches and then produces no injection, which is the
    # matched-but-dropped shape without needing to fake a budget cap or a payload failure.
    return {"artifact_id": aid, "event": "UserPromptSubmit", "condition": COND,
            "effect": {"mode": "inject", "message": ""}, "type": "contract",
            "trusted_regex": True, "lease": {"max_fires_per_session": 5}}


def main() -> int:
    print("test_matched_but_did_not_fire")
    # RESOLVED, NOT PINNED. This was `= "1"` — a hard assignment, which reads as a fixture
    # constructing a sandbox and is actually the org pin in disguise. It survived the sweep
    # that removed the `setdefault` pins, and core-school proved it live: they pulled the
    # "fix", two of three tests went green, and this one still failed on org mismatch.
    # Resolved from the REAL repo root, not from CORE_INSTANCE, because the lines above
    # have already redirected that to a temp tree with no identity.json.
    _sys.path.insert(0, str(_PPath(__file__).resolve().parents[2] / "scheduling" / "brain-pg"))
    from _env import get_org_id as _goi
    os.environ["CORE_ORG_ID"] = str(_goi(_PPath(__file__).resolve().parents[2]))
    try:
        import friction_dispatch as fd
    except Exception as e:  # noqa: BLE001  # privacy-ok: noqa linter directive, not a course code
        print(f"  FAIL  cannot import friction_dispatch: {e}")
        return 1

    live = REPO / ".claude" / "state" / "friction-action-log.jsonl"
    live_before = hashlib.sha256(live.read_bytes()).hexdigest() if live.is_file() else None

    tmp = Path(tempfile.mkdtemp(prefix="nofire-selftest-"))
    real_log, real_fire, real_load = fd.ACTION_LOG, fd.FIRE_COUNT, fd._load_active
    fd.ACTION_LOG = tmp / "a.jsonl"
    fd.FIRE_COUNT = tmp / "fire.json"          # .dispatch-seen.json is derived from this parent
    real_stdin = sys.stdin

    def _dispatch(prompt: str):
        fd._load_active = lambda org: [_artifact("art_probe")]
        fd.ACTION_LOG.write_text("")
        (tmp / ".dispatch-seen.json").unlink(missing_ok=True)
        sys.stdin = io.StringIO(json.dumps({"prompt": prompt, "session_id": "selftest-nofire"}))
        out, real_out = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            fd.run("UserPromptSubmit")
        finally:
            sys.stdout = real_out
        rows = [json.loads(x) for x in fd.ACTION_LOG.read_text().splitlines() if x.strip()]
        return [r for r in rows if r.get("action") == "dispatch_nofire"]

    try:
        no_match = _dispatch("nothing relevant in this prompt")
        matched = _dispatch("here is a zebraxyz in the prompt")

        check("a no-match pass still writes a dispatch_nofire row", bool(no_match),
              "without the row there is no record the dispatcher ran at all — the absent-evidence "
              "state this row was added to end")
        check("...and its `matched` is an empty list, PRESENT not missing",
              bool(no_match) and no_match[0].get("matched") == [],
              f"got {no_match[0].get('matched')!r} — a MISSING key is indistinguishable from an old "
              f"row, which is why it must be written as []")

        check("a matching artifact that injects nothing is NAMED in `matched`",
              bool(matched) and matched[0].get("matched") == ["art_probe"],
              f"got {matched[0].get('matched') if matched else None!r}. This is the live case: 6 of "
              f"20 artifacts had never fired with no named suppression reason, so fossil-trigger "
              f"and matched-then-dropped were the same row.")

        check("the two cases produce DIFFERENT rows",
              bool(no_match) and bool(matched)
              and no_match[0].get("matched") != matched[0].get("matched"),
              "if these ever collapse, the distinction is gone and re-derive/investigate cannot be "
              "chosen between")

        check("`considered` is still recorded alongside it",
              bool(matched) and isinstance(matched[0].get("considered"), int),
              "the original field must survive — it answers 'did this path execute'")
    finally:
        fd.ACTION_LOG, fd.FIRE_COUNT, fd._load_active = real_log, real_fire, real_load
        sys.stdin = real_stdin

    if live_before is not None:
        check("the live action log was not written to",
              hashlib.sha256(live.read_bytes()).hexdigest() == live_before,
              "this test must not feed fixtures to the corpus the SI loop measures")

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
