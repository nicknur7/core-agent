#!/usr/bin/env python3
"""THE PER-TURN DEDUPE WAS CORRECT AND NEVER RAN, and the reason was a directory mode.

MEASURED IN THE LIVE LOG, which is why this is not a hypothetical:

  · 23 exact (turn, index, kind, matched) tuples written more than once, in 429 rows
  · one turn logged the SAME WORD `today` at indices 0, 4 and 5 — which `seen` exists to prevent
  · that same claim was stored sourced=False at index 0 and sourced=True at 4 and 5. One claim, one
    turn, both verdicts. Supply is a property of the turn; it cannot vary within one.

I FIRST DIAGNOSED THIS AS A LOCK RACE AND WAS WRONG. `_accumulate` read `seen`, main() decided
against it, `_remember` wrote it back — three unsynchronised steps while MessageDisplay runs a fresh
process per chunk, which is a real race and a real defect, so I fixed it. IT WAS NOT THE CAUSE. The
fix changed nothing: 12 rows for one claim before, 12 after.

THE CAUSE, found only by instrumenting instead of reasoning: `_accumulate` verifies its buffer
directory and bails on any group/other bit. `mkdir(exist_ok=True, mode=0o700)` DOES NOT CHMOD AN
EXISTING DIRECTORY, and $TMPDIR/core-reply-accum-<uid> is shared by every Core on the machine — so
whichever process created it first under umask 022 left it 0755 permanently. Production measured
_TMP mode=755. Every chunk failed the check and returned (delta, set()).

    the dedupe never ran            -> one claim logged once per chunk    (numerator inflation)
    detection ran on the BARE DELTA -> a violation split across a chunk boundary matched nothing

The second is the hole documented at the top of reply-observer.py as FIXED. It had been inert since
the fix shipped, and every liveness probe kept passing because a probe supplies its violation as a
single delta. The two errors push in OPPOSITE directions — duplicates inflate, missed splits deflate
— so the true rate was not merely overstated, it was unknown.

WHICH HALF MATTERS DEPENDS ON THE METRIC, and core-business corrected me on this (#918) after I had
written the general claim: DUPLICATION INFLATES COUNTS, NOT RATIOS. si-objective's primary term is
per-100-REPLIES, a fixed denominator, so an inflated numerator lands directly — 16.35 -> 9.77 here.
business scores unsourced/total-OBSERVATIONS, where duplication hits both halves and cancels: it
measured 25% of all rows duplicated and every class ratio moving by under one point.

So "every published class rate was overstated" is true of this Core's metric and FALSE as a general
statement. The undercount half is the one that biases a ratio, and its direction is unknown, because
the observations that never existed could have been sourced or unsourced.

A SECURITY CHECK THAT FAILS CLOSED INTO SILENCE IS STILL A FAILURE. And refusing was the weaker
posture: the directory is owned by us, an attacker cannot create one under our uid, so 0777 means
our own bad umask — bailing leaves it world-writable forever while declining to use it, where
repairing closes the window on first run. Not-a-directory and not-owned-by-us still refuse.

WHY THIS TEST SPAWNS REAL PROCESSES, and why the fixture shape is load-bearing: the first version
put the claim only in chunk 0 and PASSED AGAINST THE BROKEN OBSERVER, because later chunks had
nothing to re-detect. Every chunk now carries the claim, which is what made the 12-vs-1 difference
visible at all.

Run: python3 bin/tests/test_observer_concurrency.py
"""
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402

ROOT = core_root()
HOOK = ROOT / ".claude" / "hooks" / "reply-observer.py"
CHUNKS = 12          # one turn, streamed
CLAIM = "We shipped that tonight and it works."


def fire(td: str, transcript: str, turn: str, i: int):
    # EVERY CHUNK CARRIES THE CLAIM, and that shape is the whole test. The first version put the
    # claim only in chunk 0 and PASSED AGAINST THE BROKEN OBSERVER — 1 row either way — because the
    # later chunks had nothing to re-detect. Run against the real defect it reported 12 rows for one
    # claim, so the assertion had been earning its PASS from the fixture, not from the fix.
    payload = json.dumps({"delta": CLAIM,
                          "session_id": "race", "turn_id": turn, "index": i,
                          "final": False, "transcript_path": transcript})
    env = dict(os.environ, CLAUDE_PROJECT_DIR=td)
    env.pop("CORE_INSTANCE", None)
    return subprocess.run([sys.executable, str(HOOK)], input=payload, text=True,
                          capture_output=True, timeout=60, env=env)


def run_turn(td: str, turn: str) -> list:
    """Fire every chunk of one turn CONCURRENTLY, as the streaming runtime does."""
    (Path(td) / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    transcript = str(Path(td) / "t.jsonl")
    Path(transcript).write_text("")
    with ThreadPoolExecutor(max_workers=CHUNKS) as ex:
        list(ex.map(lambda i: fire(td, transcript, turn, i), range(CHUNKS)))
    log = Path(td) / ".claude" / "state" / "reply-observations.jsonl"
    if not log.is_file():
        return []
    out = []
    for ln in log.read_text(errors="ignore").splitlines():
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return [r for r in out if r.get("turn") == turn]


def main() -> int:
    p = f = 0
    print("=== observer concurrency ===\n")

    def check(label, cond, detail=""):
        nonlocal p, f
        print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "\n          " + detail))
        if cond:
            p += 1
        else:
            f += 1

    with tempfile.TemporaryDirectory() as td:
        rows = run_turn(td, "raceturn0001")
        dur = [r for r in rows if r.get("kind") == "duration_claim"]
        check("one claim in one turn produces exactly ONE row under %d concurrent chunks" % CHUNKS,
              len(dur) == 1,
              "got %d rows: %s" % (len(dur), [(r.get("index"), r.get("sourced")) for r in dur]))
        check("...and its verdict is single-valued (a claim cannot be both sourced and not)",
              len({bool(r.get("sourced")) for r in dur}) <= 1,
              "verdicts=%s" % [r.get("sourced") for r in dur])
        check("every row carries observer_sha, so the corpus is labelled by DETECTOR GENERATION",
              rows and all(r.get("observer_sha") for r in rows),
              "rows=%d missing=%d" % (len(rows), sum(1 for r in rows if not r.get("observer_sha"))))

        print("\n--- the control: this test must be able to FAIL ---")
        # A concurrency test that passes on the broken code proves nothing. Rather than reason about
        # it, drive the same claim through TWO turn ids: the dedupe is per-turn, so two turns MUST
        # produce two rows. If this comes back 1, the assertion above is being satisfied by
        # over-suppression rather than by correct locking, and the real signal would be hidden.
        r2 = run_turn(td, "raceturn0002")
        d2 = [r for r in r2 if r.get("kind") == "duration_claim"]
        check("a DIFFERENT turn with the same claim still records it (no over-suppression)",
              len(d2) == 1, "got %d rows for the second turn" % len(d2))

        print("\n--- the accumulation buffer must survive an ordinary umask ---")
        # THE DEFECT THAT DISABLED EVERYTHING. `mkdir(exist_ok=True, mode=0o700)` does not chmod an
        # existing directory, $TMPDIR/core-reply-accum-<uid> is shared across every Core on the
        # machine, and whichever process created it first under umask 022 left it 0755 FOREVER.
        # Measured in production: _TMP mode=755. Every later run failed the ownership check and
        # _accumulate returned (delta, set()) on every chunk — so the dedupe never ran AND detection
        # ran on the bare delta rather than the accumulated text.
        #
        # Controlled comparison, both starting from mode 0755:
        #     PRE-FIX  split-across-chunks claim detected: False
        #     POST-FIX split-across-chunks claim detected: True
        import tempfile as _tf
        tmp_root = Path(_tf.gettempdir()) / ("core-reply-accum-%d" % os.getuid())
        tmp_root.mkdir(exist_ok=True)
        os.chmod(tmp_root, 0o755)
        split_rows = []
        with tempfile.TemporaryDirectory() as td2:
            (Path(td2) / ".claude" / "state").mkdir(parents=True, exist_ok=True)
            tp = str(Path(td2) / "t.jsonl")
            Path(tp).write_text("")
            for i, d in enumerate(["All five ", "Cores now have this."]):
                payload = json.dumps({"delta": d, "session_id": "s", "turn_id": "splitturn001",
                                      "index": i, "final": i == 1, "transcript_path": tp})
                env = dict(os.environ, CLAUDE_PROJECT_DIR=td2)
                env.pop("CORE_INSTANCE", None)
                subprocess.run([sys.executable, str(HOOK)], input=payload, text=True,
                               capture_output=True, timeout=60, env=env)
            lg = Path(td2) / ".claude" / "state" / "reply-observations.jsonl"
            if lg.is_file():
                for ln in lg.read_text(errors="ignore").splitlines():
                    try:
                        split_rows.append(json.loads(ln))
                    except Exception:
                        pass
        check("a violation SPLIT ACROSS TWO CHUNKS is detected (accumulation actually runs)",
              sum(1 for r in split_rows if r.get("kind") == "cross_core_claim") == 1,
              "rows=%s" % [(r.get("kind"), r.get("matched")) for r in split_rows])
        check("...and a 0755 buffer dir we OWN is repaired rather than silently disabling the hook",
              (os.lstat(tmp_root).st_mode & 0o077) == 0,
              "mode=%o" % (os.lstat(tmp_root).st_mode & 0o777))

        print("\n--- evidence sidecar: a corrected detector must be re-scorable ---")
        # OUTSIDE THE REPO, keyed by a digest of the root. The first version put this in
        # .claude/state/ with a .gitignore line — but .gitignore is NOT in the sync manifest, so the
        # hook would have travelled to four peer Cores and the ignore rule would not, creating the
        # file inside each peer's tree and committing it there. On core-finance that is brokerage
        # material. life is the one seat that could never have seen it.
        import hashlib as _h
        ev = (Path.home() / ".claude" / "core-evidence"
              / _h.sha256(str(Path(td)).encode()).hexdigest()[:16] / "reply-evidence.jsonl")
        erows = []
        if ev.is_file():
            for ln in ev.read_text(errors="ignore").splitlines():
                try:
                    erows.append(json.loads(ln))
                except Exception:
                    pass
        check("evidence is written for each observation",
              len(erows) >= 2, "got %d evidence rows" % len(erows))
        check("evidence carries claim + blob + supply + observer_sha (what sourced_for consumes)",
              erows and all(set(("claim", "blob", "supply", "observer_sha")) <= set(r) for r in erows),
              "keys=%s" % (sorted(erows[0]) if erows else None))
        check("the sidecar is NOT the tracked verdict log — reply text stays out of git history",
              ev.name != "reply-observations.jsonl")

        print("\n--- redaction: every class that actually leaks, not just the one the fixture had ---")
    # THIS ASSERTION USED TO BE WORTHLESS AND core-business PROVED IT (#924 BLOCK 1). The fixture was
    # `api_key: sk-abc…`, which is itself >=32 characters — so the >=32-char CATCH-ALL matched it and
    # the test PASSED WITH THE KEYWORD BRANCH DELETED. Run against eight real credential shapes, the
    # shipped redactor caught ONE, incidentally, by that same catch-all.
    #
    # Every case below is a shape that the catch-all alone does NOT cover, so each one is testing the
    # branch it names. Third instance today of a fixture satisfying an assertion regardless of the
    # code under test.
    sys.path.insert(0, str(HOOK.parent))
    import importlib.util
    spec = importlib.util.spec_from_file_location("ro", str(HOOK))
    ro = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ro)

    LEAKS = [
        ("AWS key id",       "AKIAIOSFODNN7EXAMPLE",                                   "AKIAIOSF"),
        ("AWS secret",       "aws_secret_access_key=wJalrXUtnFEMI/K7MDENGbPxRfiCYKEY", "wJalrXUt"),
        ("URL password",     "https://admin:SuperSecretPass123@db.internal.example/x",  "SuperSecr"),
        ("private key hdr",  "-----BEGIN RSA PRIVATE KEY-----",                        "BEGIN RSA"),
        ("email address",    "contact someone@example.com now",                         "someone@"),
        ("US SSN",           "ssn 123-45-6789 on file",                                "123-45-67"),
        ("github token",     "ghp_abcdefghij0123456789ABCDEFGHIJ",                     "ghp_abcdef"),
        ("JWT claims",       "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig",            "eyJzdWIiOiIx"),
    ]
    leaked = [n for n, v, needle in LEAKS if needle in ro._redact(v, 900)]
    check("every credential class is redacted, not just the >=32-char catch-all",
          not leaked, "LEAKED: %s" % leaked)
    check("account and currency figures are MASKED, keeping shape without the value",
          "5RV12345" not in ro._redact("Robinhood account number 5RV12345 balance $12,431.88", 900)
          and "12,431" not in ro._redact("balance $12,431.88", 900),
          ro._redact("Robinhood account number 5RV12345 balance $12,431.88", 900))
    check("ordinary prose survives — a redactor that eats normal text is one nobody keeps",
          ro._redact("I read the account of what happened", 900)
          == "I read the account of what happened")
    check("and the slice is still capped", len(ro._redact("x" * 5000, 900)) <= 900)

    # THE DOSE: with the keyword branch removed, at least one class must LEAK. Otherwise every
    # assertion above is being satisfied by the catch-all again and this file has learned nothing.
    import re as _re
    weakened = _re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
    still_caught = [n for n, v, needle in LEAKS if needle not in weakened.sub("<redacted>", v)]
    check("...and the catch-all ALONE would miss most of them (so the branches are doing the work)",
          len(still_caught) <= 2, "catch-all alone already caught: %s" % still_caught)

    print("\n--- the turn buffer must not follow a symlink ---")
    # core-business (#924, ASK): is_file()/read_text()/write_text() FOLLOW symlinks, so with an
    # externally-loosened ACCUM_DIR a planted link makes Core create-or-truncate an attacker-chosen
    # path and write reply text into it. Its framing is the part that mattered: the OLD
    # bail-on-any-bit code avoided this BY ACCIDENT and only for directories loose AT CALL TIME —
    # that check has no memory, so a transient loosen-then-revert would have exposed it identically.
    # O_NOFOLLOW makes the KERNEL refuse, so there is no check-then-open window.
    import importlib.util as _iu
    _spec = _iu.spec_from_file_location("ro2", str(HOOK))
    ro2 = _iu.module_from_spec(_spec)
    _spec.loader.exec_module(ro2)
    with tempfile.TemporaryDirectory() as td3:
        victim = Path(td3) / "VICTIM.txt"
        victim.write_text("PRECIOUS")
        link = Path(td3) / "buffer.json"
        os.symlink(victim, link)
        ro2._safe_buffer_write(link, {"text": "reply text", "seen": []})
        check("a write through a planted symlink does NOT touch the target",
              victim.read_text() == "PRECIOUS", "victim now: %r" % victim.read_text()[:40])
        check("...and the read through it returns empty rather than the target's contents",
              ro2._safe_buffer_read(link) == ("", []))
        # THE DOSE: the guard must not have simply broken the buffer for everyone.
        real = Path(td3) / "real.json"
        ro2._safe_buffer_write(real, {"text": "hello", "seen": ["a"]})
        check("the ordinary path still round-trips (the guard did not disable the feature)",
              ro2._safe_buffer_read(real) == ("hello", ["a"]),
              str(ro2._safe_buffer_read(real)))
        check("and the buffer file is created 0600 — it holds reply text, however briefly",
              (os.stat(real).st_mode & 0o077) == 0,
              "mode=%o" % (os.stat(real).st_mode & 0o777))

    print("\n--- and it cannot be committed by ANY Core, not just this one ---")
    # Asserted as "outside every repo" rather than "gitignored here". A .gitignore fix protects the
    # Core it was written on and none of the four that pull the hook — which is exactly how the
    # first version of this sidecar would have shipped reply text to every peer.
    src = HOOK.read_text()
    check("evidence lives under ~/.claude, never inside a Core tree",
          "Path.home() / \".claude\" / \"core-evidence\"" in src and
          'ROOT / ".claude" / "state" / "reply-evidence' not in src,
          "hook still writes evidence into the repo")
    r = subprocess.run(["git", "check-ignore", "-q", ".claude/state/reply-evidence.jsonl"],
                       cwd=str(ROOT))
    check("...and the belt-and-braces ignore rule is still present on this Core",
          r.returncode == 0, "git check-ignore returned %d" % r.returncode)

    print("\n=== Results: %d passed, %d failed ===" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
