#!/usr/bin/env python3
"""A violation split across two streamed chunks must still be seen.

WHY THIS EXISTS
---------------
MessageDisplay fires once per streamed CHUNK, not once per reply. reply-observer's first version ran
every detector against the individual `delta`, which looks right and is not:

    delta 1: "All five "
    delta 2: "Cores now have this."

Neither chunk matches the cross-core pattern. The reply still counts toward the objective's
denominator, which is built from whole assistant turns. So the score improves while the behaviour is
identical — a degeneracy in the replacement objective, found by Codex on adversarial review
2026-08-06, before the change shipped to four peer Cores.

The gaming reading is the less important one. The worse reading is that this was ALREADY corrupting
the numbers: chunk boundaries fall wherever the stream happens to break them, so an unknown share of
real violations was being dropped silently. Every liveness probe still passed, because a probe
supplies its violation as a single delta — which is precisely how a test can be green over a
half-working instrument.

WHAT THE FIX HAD TO GET RIGHT, in both directions:
  · detect on the ACCUMULATED text for the turn, so boundaries stop mattering;
  · do NOT then log the same phrase once per subsequent chunk, because the matched text stays in the
    buffer for the rest of the reply and a naive version inflates the numerator against a
    whole-turn denominator — the same error with the sign flipped;
  · never affect the turn. Every failure path stays a silent exit 0, and a buffer that cannot be read
    or written degrades to the old per-chunk behaviour (an undercount) rather than raising on the hot
    path of every chunk.

Run: python3 bin/tests/test_reply_observer_streaming.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".claude" / "hooks" / "reply-observer.py"

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


class Turn:
    """Drives the SHIPPED hook as a subprocess against a throwaway ROOT."""

    def __init__(self, root: Path, turn_id: str, tmpdir: Path | None = None):
        self.root = root
        self.turn_id = turn_id
        # TMPDIR is passed EXPLICITLY. The first version handed the subprocess a minimal env with only
        # CLAUDE_PROJECT_DIR and PATH, so tempfile.gettempdir() inside the hook fell back to /tmp while
        # this process resolved /var/folders/... — the test then looked for the buffer in a directory the
        # hook had never written to, found it empty, and reported a cleanup success it had not observed.
        # sentinel-code called the bare env a test-fidelity gap; this is what that gap actually cost.
        self.tmpdir = tmpdir
        self.empty = root / "e.jsonl"
        self.empty.write_text("")
        self.log = root / ".claude" / "state" / "reply-observations.jsonl"

    def feed(self, delta: str, index: int = 0, final: bool = False) -> int:
        payload = json.dumps({"delta": delta, "session_id": "s", "turn_id": self.turn_id,
                              "index": index, "final": final,
                              "transcript_path": str(self.empty)})
        r = subprocess.run([sys.executable, str(HOOK)], input=payload, text=True,
                           capture_output=True, timeout=20,
                           env={"CLAUDE_PROJECT_DIR": str(self.root), "PATH": "/usr/bin:/bin",
                                **({"TMPDIR": str(self.tmpdir)} if self.tmpdir else {})})
        return r.returncode

    def rows(self) -> list:
        if not self.log.is_file():
            return []
        out = []
        for ln in self.log.read_text(errors="ignore").splitlines():
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return [r for r in out if r.get("turn") == self.turn_id]


def main() -> int:
    if not HOOK.is_file():
        print("  FAIL  reply-observer.py missing")
        return 1

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as tmpd:
        root = Path(td)
        TMP = Path(tmpd)          # the TMPDIR the hook will see — controlled, so assertions are exact
        (root / ".claude" / "state").mkdir(parents=True, exist_ok=True)

        # ── Codex's exact trigger ─────────────────────────────────────────────
        t = Turn(root, "split1", TMP)
        rc1 = t.feed("All five ", 0, False)
        rc2 = t.feed("Cores now have this.", 1, True)
        check("hook exits 0 on every chunk (it must never be able to affect a turn)",
              rc1 == 0 and rc2 == 0, f"{rc1},{rc2}")
        kinds = {r["kind"] for r in t.rows()}
        check("a cross-core violation SPLIT across two chunks is detected "
              "(the degeneracy Codex found: neither chunk matches alone)",
              "cross_core_claim" in kinds, str(sorted(kinds)))

        # ── the inverse error: no double-counting ─────────────────────────────
        t2 = Turn(root, "nodouble", TMP)
        t2.feed("All five Cores have it.", 0, False)
        for i, chunk in enumerate([" More text.", " Even more.", " Done."], start=1):
            t2.feed(chunk, i, i == 3)
        n = sum(1 for r in t2.rows() if r["kind"] == "cross_core_claim")
        check("the same phrase is logged ONCE across four chunks, not once per chunk "
              "(a whole-turn denominator with a per-chunk numerator is the same bug inverted)",
              n == 1, f"logged {n} times")

        # ── two DIFFERENT violations in one turn both land ────────────────────
        t3 = Turn(root, "twokinds", TMP)
        t3.feed("We have been working 3 hours. ", 0, False)
        t3.feed("All five Cores are synced.", 1, True)
        kinds3 = {r["kind"] for r in t3.rows()}
        check("two distinct violations in one reply are both recorded",
              {"duration_claim", "cross_core_claim"} <= kinds3, str(sorted(kinds3)))

        # ── THE BUFFER MUST NOT LIVE IN THE REPO ──────────────────────────────
        #
        # sentinel-code BLOCKED the first version of this change over exactly this. The buffer held up
        # to 64K of raw reply text under .claude/state/reply-accum/, and core-life's .gitignore
        # whitelists .claude/state/ file by file rather than ignoring the directory — 94 state files are
        # tracked today, and `git add -A --dry-run` confirmed the buffer stages. session-lifecycle.sh
        # runs `git add -A` on close and defensive-save.sh drives the same routine on the walk-away
        # path, so the scenario defensive-save exists for is the one that commits reply text into
        # permanent git history. Each Core pushes its own repo to GitHub; on finance that is brokerage
        # material.
        #
        # The obvious fix — a .gitignore line — protects THIS Core only, because .gitignore is not in the
        # sync manifest. So the test is that the path is outside the repo, not that it is ignored.
        import importlib.util
        _spec = importlib.util.spec_from_file_location("_ro", HOOK)
        _ro = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_ro)
        hooksrc = HOOK.read_text()
        check("the accumulation buffer lives OUTSIDE the repo "
              "(a .gitignore fix would not travel to the four peer Cores)",
              not str(_ro.ACCUM_DIR.resolve()).startswith(str(REPO.resolve())),
              str(_ro.ACCUM_DIR))
        check("...and outside the throwaway ROOT too, so no Core's tree can ever hold reply text",
              not str(_ro.ACCUM_DIR.resolve()).startswith(str(root.resolve())))
        # The buffer dir is namespaced PER ROOT, so two Cores running at once cannot read each other's
        # in-flight replies. That is a property worth having and it is why the checks below compute the
        # subprocess's own namespace rather than reusing _ro.ACCUM_DIR — which, imported in THIS process
        # with no CLAUDE_PROJECT_DIR set, resolves to core-life's namespace and not the temp root's. The
        # first version of this test compared against the wrong directory, found it empty, and reported
        # a cleanup success it had not actually observed.
        import hashlib as _hl, os as _os
        sub_accum = (TMP / f"core-reply-accum-{_os.getuid()}"
                     / _hl.sha256(str(root).encode()).hexdigest()[:16])
        check("the buffer namespace is derived per-ROOT, so concurrent Cores cannot read each "
              "other's in-flight reply text",
              sub_accum != _ro.ACCUM_DIR)
        # A digest, not a truncation. Truncating the de-punctuated path to its last 24 chars is
        # collision-free across today's five Core paths only because the distinguishing segment sits at
        # the end; two roots differing in a MIDDLE segment would share a buffer directory, which is the
        # cross-Core leak the namespacing exists to prevent.
        check("the namespace is a digest of the whole root, so a collision cannot depend on WHERE "
              "two paths differ",
              "sha256" in hooksrc and "[-24:]" not in hooksrc)
        import hashlib as _h
        a = _h.sha256(b"/x/work/core-life").hexdigest()[:16]
        b = _h.sha256(b"/x/other/core-life").hexdigest()[:16]
        check("...demonstrated: two roots differing only in a middle segment get different namespaces",
              a != b)
        check("both levels of the buffer path are created 0700",
              hooksrc.count("mode=0o700") >= 2)
        # mkdir(mode=, exist_ok=True) does not chmod a directory that already exists, so a pre-planted
        # symlink or world-writable dir at the deterministic path would simply be accepted. lstat so a
        # symlink is caught rather than followed.
        check("the buffer dir is VERIFIED after creation — not a symlink, owned by us, no group/other bits",
              "os.lstat" in hooksrc and "st_uid != os.getuid()" in hooksrc
              and "0o077" in hooksrc)
        # ASSERTED AS A PROPERTY, NOT AS A SPELLING. This required the literal `return delta, set()`
        # within 120 characters of the mode constant, so it broke when the check gained a repair
        # branch — with the property fully intact. Second time in one day a test keyed to one
        # implementation blocked the correct fix to it (test_objective_liveness demanded the token
        # `re.sub` in an assignment). The behavioural version is below and it is the one with teeth.
        #
        # AND THE RULE CHANGED DELIBERATELY, so state it: a directory we OWN with loose bits is
        # REPAIRED to 0700, not refused. Refusing was measured disabling the entire mechanism in
        # production — $TMPDIR/core-reply-accum-<uid> is shared across every Core on the machine, and
        # whichever process created it first under umask 022 left it 0755 permanently. An attacker
        # cannot create a directory owned by us, so an owned-but-loose dir is our own bad umask, not
        # an attack. Not-a-directory and not-owned-by-us still bail; those are the real cases.
        _tail = hooksrc.split("0o077", 1)[1][:900]
        check("...an UNREPAIRABLE or foreign-owned buffer bails to the bare delta rather than writing",
              "return delta, set()" in _tail and "st_uid != os.getuid()" in hooksrc,
              _tail[:80])

        # Prove it: hand the hook a pre-planted world-writable buffer dir and confirm it refuses.
        import os as _o
        hostile_root = root / "hostile"
        (hostile_root / ".claude" / "state").mkdir(parents=True, exist_ok=True)
        h_accum = (TMP / f"core-reply-accum-{_o.getuid()}"
                   / _hl.sha256(str(hostile_root).encode()).hexdigest()[:16])
        h_accum.mkdir(parents=True, exist_ok=True)
        _o.chmod(h_accum, 0o777)
        th = Turn(hostile_root, "hostile", TMP)
        th.feed("All five ", 0, False)
        th.feed("Cores now have this.", 1, True)
        # THE RULE CHANGED ON 2026-08-10 AND THE NEW ONE IS SAFER. This asserted that a
        # world-writable buffer dir is REFUSED. Refusing is what disabled the whole mechanism in
        # production: $TMPDIR/core-reply-accum-<uid> is shared by every Core on the machine, and
        # whichever process created it first under umask 022 left it 0755 permanently — so
        # _accumulate bailed on every chunk, the per-turn dedupe never ran, and detection fell back
        # to the bare delta. Measured: _TMP mode=755, and the split violation below was being missed
        # in real operation, not just in this fixture.
        #
        # AND REFUSING WAS THE WEAKER SECURITY POSTURE. The directory here is owned by US — an
        # attacker cannot create one under our uid — so 0777 means our own bad umask. Bailing leaves
        # it world-writable forever and merely declines to use it; repairing CLOSES the window on
        # first run. Not-a-directory (symlink) and not-owned-by-us still refuse, which are the cases
        # the check was actually written for and the ones an attacker can actually produce.
        check("a world-writable buffer dir WE OWN is repaired to 0700 rather than left open",
              (_o.lstat(h_accum).st_mode & 0o077) == 0,
              "mode=%o" % (_o.lstat(h_accum).st_mode & 0o777))
        check("...and accumulation then works, so the SPLIT violation is caught rather than missed",
              any(f.name.endswith(".json") for f in h_accum.iterdir())
              or (hostile_root / ".claude" / "state" / "reply-observations.jsonl").is_file(),
              str([f.name for f in h_accum.iterdir()]))
        check("it is under the system temp dir and namespaced per uid",
              "gettempdir()" in hooksrc and "getuid()" in hooksrc)
        check("the buffer dir is created mode 0700 — it holds reply text, however briefly",
              "mode=0o700" in hooksrc)

        # ── the stored excerpt must not carry DATA ─────────────────────────────
        #
        # `matched` says WHICH phrasing fired. For six detectors the match IS the phrasing
        # ("fleet-wide", "is live", "you decided"). financial_figure is different in kind: its pattern is
        # a currency amount, so the match is the VALUE — a brokerage balance verbatim in a TRACKED file,
        # pushed to GitHub from core-finance. Redacted at write time, because the log is tracked by
        # design and an ignore rule would not travel.
        tf = Turn(root, "money", TMP)
        tf.feed("Your account balance is ", 0, False)
        tf.feed("$12,431.88 as of now.", 1, True)
        fin = [r for r in tf.rows() if r["kind"] == "financial_figure"]
        check("a currency figure split across chunks is still detected", bool(fin))
        check("...and the stored excerpt has every DIGIT masked, keeping the shape and not the figure",
              bool(fin) and not any(c.isdigit() for c in fin[0]["matched"]),
              fin[0]["matched"] if fin else "")
        check("...while a PHRASE-matching detector keeps its text (that text is the diagnostic)",
              any(r["matched"] and not r["matched"].startswith("#")
                  for r in t.rows() if r["kind"] == "cross_core_claim"))
        check("only financial_figure is redacted — over-redacting would make the log undiagnosable",
              _ro._REDACT_DIGITS == {"financial_figure"}, str(_ro._REDACT_DIGITS))

        # ── buffers are cleaned up ────────────────────────────────────────────
        accum = sub_accum
        left = sorted(p.name for p in accum.glob("*.json")) if accum.is_dir() else []
        check("the per-turn buffer is removed once the final chunk arrives "
              "(otherwise every reply leaks a file into state/)",
              not left, str(left))

        # ── an interrupted reply must not leak forever ────────────────────────
        t4 = Turn(root, "nofinal", TMP)
        t4.feed("A reply that never finishes", 0, False)
        left2 = sorted(p.name for p in accum.glob("*.json")) if accum.is_dir() else []
        check("a turn with no final chunk DOES leave a buffer (that is the state pruning exists for)",
              len(left2) == 1, str(left2))
        src = HOOK.read_text()
        check("...and there is an age-based prune for exactly that case",
              "_prune" in src and "ACCUM_TTL" in src)

        # ── inside a Core tree, the ONLY thing written is the observation log ──
        inrepo = [str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and p.name != "e.jsonl"]
        # THE PROPERTY IS "no reply text in the tree", NOT "exactly one path". This listed every
        # file under `root`, and `root` contains the nested hostile Core built above — so once the
        # buffer repair made accumulation work there, that Core's own observation log appeared and
        # the assertion failed with nothing wrong. Checking the NAMES keeps the guarantee (an
        # evidence or buffer file appearing anywhere in a Core tree still fails) without pinning the
        # count.
        #
        # This is the assertion that caught a real defect the same afternoon and is worth keeping
        # sharp: the evidence sidecar was first written to .claude/state/ with a .gitignore line to
        # protect it — but **.gitignore is not in the sync manifest**, so the file travelled to four
        # peer Cores and the ignore rule did not. Evidence now lives outside every repo entirely.
        check("inside a Core tree the hook writes ONLY observation logs — no reply text at all",
              inrepo and all(p.endswith("reply-observations.jsonl") for p in inrepo), str(inrepo))

        # ── the buffer must fail OPEN, never raise on the hot path ────────────
        check("a buffer failure degrades to the bare delta rather than raising "
              "(this runs on every streamed chunk of every reply)",
              "return delta, set()" in src)
        check("the accumulator is capped, so a long reply cannot grow state without bound",
              "ACCUM_MAX" in src and "[-ACCUM_MAX:]" in src)

    print(f"\n=== Results: {PASS} passed, {len(FAIL)} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
