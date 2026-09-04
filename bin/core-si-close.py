#!/usr/bin/env python3
"""core-si close pass — the autonomy + visibility step that runs at every session close.

Pipeline:
  1. Run detect.sh --tsv to get the live SI items.
  2. For each, read its approval streak (x/K) + trusted flag (si-fix-admission, read-only).
  3. AUTO-APPLY only items that are ALL of: in AUTO_SAFE (scheduling/core-si/auto-safe.txt)
     AND trusted AND have a registered deterministic applier below. Never anything else.
  3b. For an item that is in AUTO_SAFE and has an applier but is NOT yet trusted, run that
     applier in SHADOW (verifies preconditions, mutates nothing) and record the outcome via
     _record_evidence (:159) as kind='evidence' / 'evidence_fail'. Two consecutive successes
     admit it, and it applies for real on the NEXT close — never in the pass that produced its
     own evidence.

     ADDED 2026-08-26. Before this, the only thing that could advance a fix toward trusted was
     Nick approving it by hand, so autonomy was structurally incapable of outrunning him. This
     is the mutating step that lets the ratchet turn without him. Declared here because
     test_documented_checks_are_run.py refuses any mutating step main() calls that the Pipeline
     does not name — it caught this one the same hour it was written.

  4. Write .claude/state/core-si-inbox.json — the visibility surface + the app bridge
     (the future Core OS one-click card reads this exact file).
  5. Fire LOCAL notifications (bin/core-notify.sh) for: critical (🔴) items that need Nick's
     call, and the FIRST time any trusted fix auto-fires (so autonomy is never invisible).

  6. Run the FRICTION ENGINE (run_friction_engine, :140) — mine recent frictions, route,
     corpus-grounded test-gate, and INSTALL inject-contracts (budget-capped <=5, reversible,
     org-scoped). Runs at EVERY close unless CORE_FRICTION_DISABLE=1. Fail-open: any error
     returns a skip summary and never breaks close.

     ADDED TO THIS LIST 2026-08-13 (core-finance DOSE 40). It was called by main() and named
     nowhere in the Pipeline, so the enumeration said five steps while the code ran six — and
     the missing one is the one that INSTALLS. The safety prose was attached to the smaller
     action: step 3's auto-apply is fenced meticulously ("ALL of ... Never anything else") and
     has exactly ONE registered applier, while the undocumented step installs up to five
     artifacts. Anyone auditing "what does close change autonomously?" read the Pipeline, saw a
     tightly-fenced one-applier path, and never learned the same script installs contracts.
     The engine is not unsafe — budget-capped, inject-only, reversible, org-scoped, fail-open,
     with an escape hatch. It was invisible, which is a different problem and this is its fix.

HARD SAFETY: fail-open (any exception → exit 0, close proceeds). Auto-apply is triple-gated
and the applier registry contains only local, reversible commands. Outward/destructive keys
can never be applied here regardless of trust — and the load-bearing reason is STRUCTURAL rather
than the list: `eligible = in_safe and trusted and has_applier` (:488) is a three-way AND, and
# THE FENCE IS `has_applier` PLUS THE SOURCE-SCAN TESTS in bin/tests/test_appliers_are_safe.py
# — NOT the size of APPLIERS. This line used to say the dict "holds exactly ONE entry" and
# leaned on that count as if scarcity were the safety property. It held 1, holds 3, and will
# hold more; a fence that erodes every time the system grows is not a fence. What actually
# constrains an applier is: it must be registered, it must accept shadow= and mutate nothing
# under it, it may not name a trust-root path, and it may not reach an outward verb — all four
# asserted by tests rather than by prose.
cannot be applied however trusted it is and whatever auto-safe.txt happens to say.

This paragraph used to credit "they're excluded from auto-safe.txt by rule" — a hand-maintained
file — for a property the registry enforces structurally. Corrected 2026-08-13 (core-finance
DOSE 40), and it is the same shape as the assert-work manifest gap found hours earlier: the robust
layer holds while the prose credits the fragile one. A reader who trusts the sentence audits the
wrong file; a reader who finds a mistake IN that file concludes the fence is broken when it is not.

Called by .claude/hooks/session-lifecycle.sh (close phase). Standalone-testable:
  CORE_INSTANCE=$(git rev-parse --show-toplevel) CORE_ORG_ID=1 python3 bin/core-si-close.py [--dry-run]
"""
from __future__ import annotations
import json
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv


def _repo() -> Path:
    env = os.environ.get("CORE_INSTANCE")
    if env:
        return Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(out)
    except Exception:
        return Path(__file__).resolve().parent.parent


REPO = _repo()
STATE = REPO / ".claude" / "state"
LOG = Path(f"/tmp/core-si-close-{REPO.name}-{datetime.now():%Y-%m-%d}.log")
# ─── THIS SEAT'S ORG — resolved ONCE, from identity ────────────────────────────────────────────
# Was `ORG` at ten sites in this file alone. That default is a
# SILENT FALLBACK TO LIFE: on school/finance/ops any invocation without the var in its environment
# read, and in the promote/graduate/project paths WROTE, life's org partition. `_env.get_org_id()`
# has been the hardened resolver since 2026-08-05 — identity wins over the environment, and a
# disagreement is reported loudly — but 22 sites across the shared tree still bypassed it.
# Fails LOUD rather than defaulting: a wrong org silently succeeds, which is how this survived.
try:
    # brain-pg on the path HERE: this file's other inserts live inside functions, so at module
    # scope `_env` is not importable and the resolve raised SystemExit on every invocation.
    sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
    from _env import get_org_id as _get_org_id
    ORG = _get_org_id()
except Exception as _e:
    raise SystemExit(f"core-si-close: cannot resolve this seat's org id: {_e}")

DETECT = REPO / "scheduling" / "core-si" / "detect.sh"
ADMIT = REPO / "scheduling" / "core-si" / "si-fix-admission.py"
AUTOSAFE_FILE = REPO / "scheduling" / "core-si" / "auto-safe.txt"
NOTIFY = REPO / "bin" / "core-notify.sh"


def log(msg: str) -> None:
    try:
        with open(LOG, "a") as f:
            f.write(f"[{datetime.now():%H:%M:%S}] {msg}\n")
    except Exception:
        pass


def load_autosafe() -> set[str]:
    keys = set()
    try:
        for line in AUTOSAFE_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keys.add(line)
    except Exception:
        pass
    return keys


def detect_items() -> list[dict]:
    """Run detect.sh --tsv → list of {sev, domain, detected, fix, fitness, key}."""
    env = {**os.environ, "CORE_INSTANCE": str(REPO),
           "CORE_ORG_ID": str(ORG)}
    try:
        out = subprocess.run(["bash", str(DETECT), "--tsv"], capture_output=True,
                             text=True, env=env, timeout=30).stdout
    except Exception as e:
        log(f"detect.sh failed: {e}")
        return []
    items = []
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) >= 6:
            items.append({"sev": parts[0], "domain": parts[1], "detected": parts[2],
                          "fix": parts[3], "fitness": parts[4], "key": parts[5]})
    return items


# ── ONE LOUD PRECONDITION CHECK, instead of thirteen quiet ones ────────────────────────────────
#
# There are 13 `except: log("... skipped (fail-open)")` sites below. Fail-open is right — an SI
# pass must never break Nick's close. But when the DB DRIVER ITSELF is missing, all 13 fire at
# once and the close still reports success. Observed 2026-08-26: every SI step logged "skipped",
# the friction engine did not run, no evidence was recorded, and nothing anywhere said the loop
# had not run. Thirteen quiet skips is indistinguishable from a healthy close.
#
# The cause is that `python3` is UNPINNED in ~39 call sites across the close path, so which
# interpreter runs decides whether the entire SI layer works. On this machine
# /usr/bin/python3 has psycopg2 and /opt/homebrew/bin/python3 does not; a PATH order change is
# enough to silence the whole loop. The same instability makes `bin/tests/run-all.sh` return
# different results run to run, which means "the suite is green" is not a stable claim either.
#
# This does not fix the pinning. It makes the failure LOUD and, critically, SURFACED: a missing
# driver becomes a 🔴 inbox item rather than a log line nobody reads. Detecting-vs-silent is the
# distinction this whole subsystem exists to preserve.
def _db_driver_available() -> "tuple[bool, str]":
    """(ok, detail). Reports on THIS process AND on what a bare `python3` resolves to.

    CORRECTED 2026-08-26 after core-ops caught it crying wolf. The first version tested only
    `import psycopg2` in the running process, i.e. `sys.executable`. That is right when the close
    hook invokes this file, and WRONG when a human runs it by hand from a shell whose PATH happens
    to resolve a different interpreter — it wrote a 🔴 into the persisted inbox for a seat where
    the actual close path was healthy.

    ops demonstrated exactly that: their interactive shell resolves an interpreter with no
    psycopg2, while their hooks resolve one that has it — proven by live Postgres results sitting in
    .brain-health.json. Shipping the first version fleet-wide would have fired on a seat with
    nothing wrong.

    So: the process check decides whether THIS pass is degraded (it is, if the import fails here),
    and the subprocess check tells the reader whether the CLOSE PATH is affected too — because a
    bare `python3` is what all ~39 unpinned call sites actually invoke. The two answers are
    different questions and the item must not conflate them.
    """
    try:
        import psycopg2  # noqa: F401
        return True, ""
    except Exception as e:
        this_err = str(e)
    # Does a bare `python3` — what every unpinned close-path site resolves — have the driver?
    bare_ok = None
    bare_path = ""
    try:
        r = subprocess.run(["python3", "-c",
                            "import psycopg2,sys;print(sys.executable)"],
                           capture_output=True, text=True, timeout=20)
        bare_ok = (r.returncode == 0)
        bare_path = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
    except Exception:
        bare_ok = None
    if bare_ok:
        return False, (f"{this_err} — BUT a bare `python3` DOES have it ({bare_path}), so the close "
                       f"path is probably fine and this is an artifact of how this pass was invoked "
                       f"({sys.executable})")
    return False, f"{this_err} — and a bare `python3` lacks it too, so the close path is affected"


# ── trust state (read-only; one DB connection, fail-open to "nothing trusted") ──────────
def trust_lookup(items: list[dict]) -> dict[str, dict]:
    """Return {key: {streak, trusted}}. If the DB is unreachable, everything is untrusted
    (conservative: no auto-apply)."""
    result = {it["key"]: {"streak": 0, "trusted": False} for it in items}
    try:
        sys.path.insert(0, str(REPO / "scheduling" / "core-si"))
        sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("si_admit", ADMIT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        conn = mod.connect_corebrain()
        try:
            for it in items:
                result[it["key"]] = {
                    "streak": mod.streak_of(conn, it["key"], it["fix"]),
                    "trusted": mod.is_trusted(conn, it["key"], it["fix"]),
                }
        finally:
            conn.close()
    except Exception as e:
        log(f"trust lookup skipped (DB?): {e}")
    return result


def _record_evidence(key: str, fix: str, ok: bool) -> None:
    """Write one evidence row for (key, fix). Fail-open: never breaks the close.

    Deliberately its own connection rather than reusing trust_lookup's — that one is opened and
    closed inside the lookup, and threading a live handle through the item loop to make a WRITE
    would turn a read-only visibility pass into a mutating one at a distance.
    """
    try:
        sys.path.insert(0, str(REPO / "scheduling" / "core-si"))
        sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("si_admit_w", ADMIT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        conn = mod.connect_corebrain()
        try:
            r = mod.record_evidence(conn, key, fix, ok)
            log(f"evidence[{key}] ok={ok} streak={r['streak']}/{r['k']} admitted={r['admitted']}")
        finally:
            conn.close()
    except Exception as e:
        log(f"evidence record skipped for {key} (DB?): {e}")


# ── applier registry: ONLY local, reversible commands. key → callable(shadow=False) -> bool ──
def _apply_recall_eval(shadow: bool = False) -> bool:
    """Re-run the recall benchmark in the background (heavy-ish; no API spend).

    `shadow=True` verifies the preconditions and returns what the real run WOULD have returned,
    without spawning anything. That is what lets this fix earn trust from its own demonstrated
    behaviour instead of from Nick approving it twice — see si-fix-admission.record_evidence.
    Every applier registered in APPLIERS must accept this argument and must mutate NOTHING when
    it is set.
    """
    eval_py = REPO / "scheduling" / "brain-pg" / "eval.py"
    if not eval_py.exists():
        return False
    if shadow:
        return True    # preconditions hold; the real call would have spawned successfully
    try:
        with open(LOG, "a") as lf:
            subprocess.Popen(["python3", str(eval_py)], stdout=lf, stderr=lf,
                             cwd=str(REPO), start_new_session=True)
        return True
    except Exception as e:
        log(f"recall-eval apply failed: {e}")
        return False


def _apply_sys_embed(shadow: bool = False) -> bool:
    """sys-embed — the detector's fix text is "inspect the log; re-run embed".

    Fires when the last recorded embed exited non-zero, which leaves entities/evidence rows with
    NULL embeddings and silently degrades recall.

    TWO CORRECTIONS TO WHAT THIS DOCSTRING USED TO CLAIM, both load-bearing and both wrong:

      (a) `run-brain-update.sh fast` is NOT "re-run embed". It is extract-core-sessions.py PLUS the
          embed. The embed half is idempotent and resumable; the extraction half is more than this
          applier's name implies, and an author reading the old text would have under-estimated the
          blast radius.
      (b) The lock waiters carry a 3600s backstop that SIGTERM+SIGKILLs the lock HOLDER'S ENTIRE
          PROCESS TREE (run-brain-update.sh:91-115, embed.py:159-190). That backstop was written
          for the case where a human occasionally starts a waiter. This applier converts that into
          "every failing close starts one", so the backstop can now fire unattended and kill a
          peer Core's pipeline. That is the real risk here, and it is why the dedup below is not
          an optimisation.

    Detached because the embed blocks on the shared brain lock with no timeout (Nick's 2026-07-24
    call: all Cores queue), and a close must never hang on a peer holding it.
    """
    runner = REPO / ".claude" / "hooks" / "run-brain-update.sh"
    if not runner.exists():
        return None          # not applicable on a seat without the pipeline — not a failure
    if shadow:
        # Preconditions: the runner exists AND the brain is reachable. A shadow run must not take
        # the lock, so reachability is checked with a read-only connect, not by starting the job.
        try:
            sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
            from _env import connect_corebrain as _cc
            c = _cc()
            c.close()
            return True
        except Exception as e:
            log(f"sys-embed shadow: brain unreachable ({e})")
            return False
    # DEDUP. Without this, N failing closes stack N queued extract+embed chains, each of which can
    # start a 3600s lock waiter whose backstop kills the holder's process tree. There was no dedup
    # anywhere in the chain.
    try:
        if subprocess.run(["pgrep", "-f", str(runner)], capture_output=True,
                          text=True, timeout=5).stdout.strip():
            log("sys-embed: run-brain-update.sh already alive — not spawning a duplicate")
            return True
    except Exception:
        pass
    try:
        env = {**os.environ,
               "CORE_INSTANCE": str(REPO),
               "CORE_ORG_ID": str(ORG)}
        with open(LOG, "a") as lf:
            subprocess.Popen(["bash", str(runner), "fast"], stdout=lf, stderr=lf,
                             cwd=str(REPO), env=env, start_new_session=True)
        return True
    except Exception as e:
        log(f"sys-embed apply failed: {e}")
        return False


def _apply_sys_marker(shadow: bool = False) -> "bool | None":
    """sys-marker — clears the pending-push marker ONLY when the baseline proves it stale.

    PROOF CONTRACT, verified against bin/sync-to-baseline.sh on 2026-08-26 by reading the script
    and by running it:

      rc 10  ("N shared file(s) would be pushed", :391)  -> genuinely pending -> None, marker is
                                                            CORRECT and must be left alone
      rc 0  + "no shared changes; skipping push." (:255) -> pending set EMPTY -> marker is stale
      rc 0  WITHOUT that sentinel                        -> the CLONE FAILED (:100 also exits 0)
                                                            -> nothing was verified -> False

    THE STRING "0 shared file(s) would be pushed" NEVER PRINTS. :255 exits *above* the --check
    counter at :391, so that counter is only ever reached when the count is >= 1. The first version
    of this applier keyed its success on exactly that string, which made its success path
    unreachable: on every close where the marker WAS stale it returned False, wrote an
    evidence_fail, wiped the trust streak, and burned a GitHub clone plus up to 180s. It could
    never have graduated. Found by adversarial review, confirmed by reading :255 and :391.

    SHADOW MUST NOT CALL --check. That path clones from GitHub and, on clone failure, APPENDS to
    .claude/state/.sync-failures (:100) — a real write. A shadow that mutates defeats the entire
    point of shadow-earned trust, and would have turned test_appliers_are_safe red the first time
    the network blinked mid-test. Shadow therefore checks preconditions only.

    The marker is MOVED, not deleted: a cleared marker is audit evidence. Pushing is never done
    here and must never be added — that is Nick's floor.
    """
    marker = STATE / ".pending-push-marker"
    if not marker.exists():
        return None          # not applicable: nothing of ours in this item
    try:
        names = [ln.strip() for ln in marker.read_text().splitlines() if ln.strip()]
    except Exception:
        return False
    if not names:
        # An empty marker is stale by definition — nothing can be pending.
        if shadow:
            return True
        marker.replace(STATE / ".pending-push-marker.cleared")
        log("sys-marker: empty marker retired")
        return True

    checker = REPO / "bin" / "sync-to-baseline.sh"
    if not checker.exists():
        return False
    if shadow:
        return True          # preconditions hold; the real run is what does the verifying
    try:
        r = subprocess.run(["bash", str(checker), "--check"], cwd=str(REPO),
                           capture_output=True, text=True, timeout=180)
    except Exception as e:
        log(f"sys-marker: --check failed ({e})")
        return False
    if r.returncode == 10:
        log("sys-marker: shared files genuinely pending — marker is CORRECT, not stale")
        return None
    if r.returncode == 0 and "no shared changes; skipping push." in (r.stdout or ""):
        marker.replace(STATE / ".pending-push-marker.cleared")
        log(f"sys-marker: baseline reports nothing pending — stale marker retired "
            f"({len(names)} name(s) it had listed)")
        return True
    log(f"sys-marker: --check inconclusive (rc={r.returncode}) — leaving marker alone")
    return False


def _apply_sys_docpath_archival(shadow: bool = False) -> "bool | None":
    """sys-docpath-archival — rewrite broken refs whose basename lives in exactly ONE archive dir.

    REGISTERED UNDER THE SPLIT KEY ONLY. The bare `sys-docpath` key covers the judgment population
    that auto-safe.txt's HARD FLOOR reserves ("sys-docpath real-ref fixes"), and the KEY is the
    admission unit — so the bare key must never enter auto-safe.txt, or fixing the provable half
    would mark the reserved half auto-applied.

    Shadow and detector both read lint-doc-paths.py's `--split`; the predicate is NOT replicated
    here. Two copies of a resolution rule is how the previous doc-path bug survived.
    """
    script = REPO / "bin" / "lint-doc-paths.py"
    if not script.exists():
        return None
    if shadow:
        try:
            r = subprocess.run(["python3", str(script), "--split"], cwd=str(REPO),
                               capture_output=True, text=True, timeout=120)
            n = int(json.loads(r.stdout)["archival_fixable"])
        except Exception as e:
            log(f"sys-docpath-archival shadow: --split failed ({e})")
            return False
        # None, not False: an empty slice is NOT a failed attempt, and recording it as one would
        # reset this key's trust streak on every close where the repo happens to be clean.
        return True if n > 0 else None
    try:
        r = subprocess.run(["python3", str(script), "--fix-archival"], cwd=str(REPO),
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        log(f"sys-docpath-archival: apply failed ({e})")
        return False
    fixed = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("fix-archival:"):
            d = "".join(ch for ch in line.split("ref(s)")[0] if ch.isdigit())
            if d:
                fixed = int(d)
    if fixed is None:
        # The script can die mid-loop AFTER some writes have landed, so the exit code disambiguates
        # nothing. Never log "nothing to fix" over possible partial writes.
        log(f"sys-docpath-archival: no summary line (exit={r.returncode}) — not claiming success")
        return False
    log(f"sys-docpath-archival: {fixed} archival ref(s) rewritten")
    return fixed > 0


def _apply_sys_brainlint_refresh(shadow: bool = False) -> "bool | None":
    """sys-brainlint-refresh — regenerate the brain-lint report when it is missing or >7d stale.

    Same shape as _apply_recall_eval: re-runs a read-only diagnostic whose only output is one
    report file. It NEVER edits memory content — reconciling real gap-memory drift is the separate
    `sys-brainlint` key and stays Nick's.

    VERIFIES THE FILE THIS RUN WROTE, by parsing lint.py's own `Wrote <path>` line (lint.py:424,
    preserved through lint-pass.sh). Never by listing the directory: sorted() is lexicographic
    while detect.sh uses `ls -t`, so a stray README.md or _template.md would permanently shadow the
    real report and this would "verify" a file the run never produced.
    """
    lint_pass = REPO / "scheduling" / "brain-lint" / "lint-pass.sh"
    if not lint_pass.exists():
        return None
    if shadow:
        # PRECONDITIONS ONLY. An in-process shadow would walk the whole ~180MB vault inline with no
        # timeout at every untrusted close — the first unbounded shadow in the registry. Not worth
        # it: the real leg below is self-verifying.
        return True if (REPO / "memory").is_dir() else False
    try:
        env = {**os.environ, "CORE_INSTANCE": str(REPO),
               "CORE_BRAIN": os.environ.get("CORE_BRAIN", str(pathlib.Path.home() / "AI Projects/core-brain"))}
        r = subprocess.run(["bash", str(lint_pass)], cwd=str(REPO), env=env,
                           capture_output=True, text=True, timeout=900)
    except Exception as e:
        log(f"sys-brainlint-refresh: lint-pass failed ({e})")
        return False
    wrote = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("Wrote "):
            wrote = line[len("Wrote "):].strip()
    if not wrote:
        log(f"sys-brainlint-refresh: no 'Wrote' line (exit={r.returncode}) — not claiming success")
        return False
    if not pathlib.Path(wrote).is_file():
        log(f"sys-brainlint-refresh: reported writing {wrote} but it is not on disk")
        return False
    log(f"sys-brainlint-refresh: report regenerated -> {wrote}")
    return True


def _apply_si_detector_liveness(shadow: bool = False) -> "bool | None":
    """si-detector-liveness — refresh the liveness stamp by re-running the probe.

    The item fires for two causes: a MISSING or STALE stamp (mechanical — nobody has run the
    probe) or FAILING probes (a genuinely broken detector, which is Nick's). Re-running resolves
    the first and only RE-MEASURES the second, which then keeps firing with an honest, fresh
    failing list rather than a stale one.

    Returns None when probes are still failing: the run did its job, but the ITEM is not resolved
    and claiming otherwise would mark a broken instrument fixed.

    NOTE: si-objective.py also refreshes the oracle-request queue (:634) — a reversible local JSON
    write. That happens on the REAL leg only; the shadow must never invoke it.
    """
    obj = REPO / "bin" / "si-objective.py"
    if not obj.exists():
        return None
    if shadow:
        return True
    try:
        subprocess.run(["python3", str(obj), "--json"], cwd=str(REPO),
                       capture_output=True, text=True, timeout=300)
        stamp = json.loads((STATE / ".si-liveness.json").read_text())
    except Exception as e:
        log(f"si-detector-liveness: probe run failed ({e})")
        return False
    failing = stamp.get("failing") or []
    if failing:
        log(f"si-detector-liveness: stamp refreshed; {len(failing)} probe(s) still FAILING "
            f"{failing[:3]} — that half is the operator's")
        return None
    log("si-detector-liveness: stamp refreshed, all probes green")
    return True


def _apply_sys_memstale_proven(shadow: bool = False) -> "bool | None":
    """sys-memstale-proven — bump `Last updated:` to the date of a PROVEN content edit.

    Registered on the SPLIT key only. The bare `sys-memstale` key is named on auto-safe.txt's HARD
    FLOOR and stays notify-only: for a file with no stamp or no proven edit there is NO NON-LYING
    VALUE TO WRITE, so that half is not a deferred judgment call, it is an absence of information.

    WRITES THE DATE OF THE PROVEN EDIT, NEVER TODAY'S. And proof requires a commit that touched a
    NON-STAMP line — see bin/memstale.py's docstring. Without that requirement this applier's own
    auto-committed stamp write becomes the next close's "edit after the stamp", and in two closes
    the loop launders today's date onto content last edited months ago, then silences the detector
    for that file permanently. A stamp is an ATTESTATION; this session already found two false
    facts surviving under one that claimed reverification.

    Shadow calls memstale.classify() — pure reads (file text + `git log`/`git show`). It must NEVER
    call detect_items(), which re-runs detect.sh: that writes .core-si-count and
    .core-si-items.tsv, opens Postgres and shells `git ls-remote`, all under shadow=True.
    """
    try:
        sys.path.insert(0, str(REPO / "bin"))
        import memstale
    except Exception as e:
        log(f"sys-memstale-proven: predicate unavailable ({e})")
        return False
    try:
        c = memstale.classify(REPO)
    except Exception as e:
        log(f"sys-memstale-proven: classify failed ({e})")
        return False
    proven = c.get("proven") or []
    if not proven:
        return None                      # empty slice — withhold, never a false failure
    if shadow:
        return True
    fixed_any = False
    for item in proven:
        path = REPO / item["rel"]
        try:
            lines = path.read_text().splitlines(keepends=True)
            i = item["line_idx"]
            if i >= len(lines) or item["old"] not in lines[i]:
                log(f"sys-memstale-proven: {item['rel']} line {i} no longer holds {item['old']} — skipped")
                continue
            lines[i] = lines[i].replace(item["old"], item["new"], 1)
            path.write_text("".join(lines))
            # Set BEFORE readback: a write that happened is a mutation whether or not the
            # verification below succeeds, and reporting False after writing would be a lie.
            fixed_any = True
            back = path.read_text().splitlines()[i]
            if item["new"] not in back:
                log(f"sys-memstale-proven: readback MISMATCH on {item['rel']} — wrote but cannot confirm")
            else:
                log(f"sys-memstale-proven: {item['rel']} {item['old']} -> {item['new']} (proven edit)")
        except Exception as e:
            log(f"sys-memstale-proven: {item['rel']} failed ({e})")
    return True if fixed_any else False


def _baseline_remote_sha():
    """Remote HEAD of the baseline branch, or None. MODULE LEVEL on purpose.

    test_appliers_are_safe check 3b is FUNCTION-scoped: every subprocess invocation inside a
    function that mentions the sync script must carry the read-only flag. `git ls-remote` is
    read-only but is not that flag, so keeping it inside the applier body would either fail the
    check or force the check to be loosened. Hoisting it here keeps the applier's own body
    trivially auditable — the strictest possible reading of the rule stays satisfiable.
    """
    manifest = REPO / "bin" / "sync-manifest.json"
    try:
        m = json.loads(manifest.read_text())
        repo_name = m.get("baseline_repo")
        branch = m.get("baseline_branch") or "main"
        if not repo_name:
            return None
        r = subprocess.run(["git", "ls-remote", "-h",
                            f"https://github.com/{repo_name}.git", branch],
                           cwd=str(REPO), capture_output=True, text=True, timeout=30)
        line = (r.stdout or "").strip().splitlines()
        return line[0].split()[0] if line else None
    except Exception:
        return None


def _apply_sys_baseline(shadow: bool = False) -> "bool | None":
    """sys-baseline — record convergence when the shared tree is ALREADY identical to the baseline.

    THIS NEVER PUSHES AND NEVER PULLS. Both are Nick's floor and both are genuinely dangerous here:

      · PUSH — sync-to-baseline.sh:13 states PreToolUse cannot gate a push made from subprocess
        context, so from a close hook it would be a fully UNGATED outward action, not even
        Sentinel-reviewed, landing last-pusher-wins over four pullers and an external fork.
      · PULL — on the writer seat it rsyncs the OLDER baseline over unpushed shared edits.
        detect.sh's own 2026-08-04 note records that this would have reverted a test fix committed
        18 minutes earlier and deleted two archive dirs, on this exact Core.

    All this applier does is close the case where the remote advanced, our shared tree already
    matches it, and only the local `.last-baseline-sync` breadcrumb is behind — a bookkeeping lag
    that otherwise nags forever with a prescribed fix that is dangerous to run.

    PROOF: `--check` must exit 0 AND print its no-changes sentinel. rc==0 alone proves nothing —
    sync-to-baseline.sh:100-106 also exits 0 when the CLONE FAILED. The remote SHA is re-read after
    the check so a baseline that moved mid-verification is not stamped as converged.
    """
    sync_log = STATE / ".last-baseline-sync"
    before = _baseline_remote_sha()
    if not before:
        return None                      # offline or no manifest: not applicable, no claim
    local = None
    try:
        if sync_log.is_file():
            for line in reversed(sync_log.read_text().splitlines()):
                if "baseline=" in line:
                    local = line.split("baseline=", 1)[1].split()[0]
                    break
    except Exception:
        return False
    if local == before:
        return None                      # already recorded — nothing of ours in this item
    if shadow:
        return True
    checker = REPO / "bin" / "sync-to-baseline.sh"
    if not checker.exists():
        return False
    try:
        r = subprocess.run(["bash", str(checker), "--check"], cwd=str(REPO),
                           capture_output=True, text=True, timeout=180)
    except Exception as e:
        log(f"sys-baseline: --check failed ({e})")
        return False
    if r.returncode == 10:
        log("sys-baseline: shared files genuinely pending — NOT converged, this is the operator's push")
        return None
    if not (r.returncode == 0 and "no shared changes; skipping push." in (r.stdout or "")):
        log(f"sys-baseline: --check inconclusive (rc={r.returncode}) — not stamping")
        return False
    after = _baseline_remote_sha()
    if after != before:
        log(f"sys-baseline: baseline moved during verification ({before[:8]}->{after[:8] if after else '?'}) — not stamping")
        return False
    try:
        with sync_log.open("a") as fh:
            fh.write(f"{datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')} "
                     f"baseline={after} via=core-si-close.py source=convergence-verified\n")
    except Exception as e:
        log(f"sys-baseline: could not append to sync log ({e})")
        return False
    log(f"sys-baseline: shared tree verified identical to {after[:8]} — convergence recorded")
    return True


APPLIERS = {
    "recall-eval": _apply_recall_eval,
    "sys-embed": _apply_sys_embed,
    "sys-marker": _apply_sys_marker,
    "sys-docpath-archival": _apply_sys_docpath_archival,
    "sys-brainlint-refresh": _apply_sys_brainlint_refresh,
    "si-detector-liveness": _apply_si_detector_liveness,
    "sys-memstale-proven": _apply_sys_memstale_proven,
    "sys-baseline": _apply_sys_baseline,
}


# ── generative self-building loop: friction-case SI engine (v1 inject-only) ──────────────
def run_friction_engine() -> dict:
    """Autonomous friction-case SI: mine recent frictions → route → corpus-grounded test-gate →
    install inject-contracts (budget-capped ≤5, reversible, org-scoped). This is the generative
    half of Core self-improvement — it feeds the live friction-dispatch hook. Fail-open: any error
    returns a skip summary and NEVER breaks close. Escape hatch: CORE_FRICTION_DISABLE=1."""
    if os.environ.get("CORE_FRICTION_DISABLE") == "1":
        return {"ran": False, "reason": "disabled via CORE_FRICTION_DISABLE"}
    try:
        sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
        sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
        import friction_loop as fl
        # WINDOW WIDENED 14 -> 90 (2026-08-17, per the operator's go-ahead to widen it).
        # The mining window is the ceiling on everything the loop can learn from: friction_loop
        # selects candidates WHERE session_date >= CURRENT_DATE - days, so a correction older than
        # the window is not deprioritised, it is invisible to the code that decides what to build.
        # Measured on life at the moment of the change:
        #     7d   6 clusters /  22 rows      30d  12 clusters /  221 rows
        #    14d  10 clusters /  86 rows      90d  14 clusters /  824 rows      all 17 / 1293
        # 90d captures 63% of life's corpus and 14 of its 17 distinct clusters, against 7% and 10
        # at the old 14d. Not unbounded: the tail past 90d is old detector generations and the
        # v1/learned-miner-v1 changeover, which a fitness comparison should not span anyway.
        # Throughput is unchanged — MAX_CONTRACTS=5 still caps installs per pass, and run()
        # dedupes by canonical_ask, so widening surfaces older RECURRING themes rather than volume.
        out = fl.run(days=90, dry=DRY_RUN)
        log(f"friction engine: {out.get('cases')} cases, eligible={out.get('eligible')}, "
            f"installed={out.get('installed')}, watchdog={out.get('watchdog')}")

        # ── THE ACTING ARM (Phase T, 2026-08-05) ────────────────────────────────────────────
        # Everything above MEASURES and MINTS. Until now nothing ACTED on a measurement:
        # friction_tune.py had every caller in bin/tests/ (1,145 installs, 0 tune actions ever)
        # while contract-fitness.json named its not_binding findings and nothing read them.
        #
        # Ordered re-arm FIRST, then tune. A rule whose pattern has come back should regain its
        # voice before the same pass considers demoting anything else — otherwise a single close
        # could demote a rule and leave its recovery a full cycle behind.
        try:
            import friction_promote as _fp
            _re = _fp.rearm_shadowed(ORG, dry=DRY_RUN)
            if _re.get("rearmed"):
                log(f"tuning: RE-ARMED {len(_re['rearmed'])} shadowed artifact(s) — "
                    f"pattern returned at the proof bar: "
                    f"{[x['artifact_id'] for x in _re['rearmed']]}")
            for h in (_re.get("holding") or []):
                log(f"tuning: {h['artifact_id']} still shadowed — {h['why']}")
            for e in (_re.get("errors") or []):
                log(f"⚠ tuning re-arm error: {e}")
        except Exception as _e:
            log(f"⚠ tuning re-arm skipped: {str(_e)[:120]}")

        # ── THE DISTILLATION BACKLOG — the loop's own blind spot ────────────────────────────
        # Measured 2026-08-05: 916 of 1,203 recorded corrections on life (76%) carry NO
        # canonical_ask, and fleet-wide it is ~1,107 of 1,662. Every stage downstream of mining
        # keys off canonical_ask — ask_miner.ask_cases() clusters on it, friction_router refuses
        # without it — so the loop has been learning from roughly a QUARTER of its own evidence
        # and reporting healthy numbers while doing it.
        #
        # The cause is structural, not a bug: turning a raw correction into a canonical ask needs
        # an LLM, `ask_miner.extract_pending()` -> model -> `cache_asks()`, and NOTHING CALLS IT.
        # A hook cannot call Claude, and the headless API path was retired 2026-07-24, so the step
        # can only run from an agent-executed context — it lives in /close-core (step 2d) where a
        # Haiku subagent does the extraction and the parent validates and writes.
        #
        # This block does NOT do the extraction; it cannot. It MEASURES the backlog and says so
        # loudly at every close, because the failure mode here is silence: an undistilled row is
        # indistinguishable from an absent one to everything downstream, so a growing backlog reads
        # as "no new corrections" rather than "the loop stopped being able to see them". Same
        # absent-inputs-are-reported principle the tuning arm above already follows.
        try:
            from _env import connect_corebrain as _cc
            _org = ORG
            _con = _cc()
            try:
                _c = _con.cursor()
                # NULL means NOT YET EXTRACTED. Empty string means EXTRACTED AND THERE WAS NO
                # DURABLE ASK — cache_asks stores '' deliberately so such a row is not re-extracted
                # forever. They are opposite states and must not be summed.
                #
                # The first version of this query counted `IS NULL OR btrim(...)=''` as backlog,
                # which on this Core meant 611 of 1,203 — so it would have reported a 51% backlog
                # PERMANENTLY, immediately after a pass that took the real backlog to zero. A gate
                # that fires forever regardless of the underlying state is exactly the over-firing
                # failure this session spent its time removing from the blast-radius oracle, and I
                # reintroduced it in the fix for a different problem an hour later. Caught by
                # checking the number against the data instead of trusting the predicate.
                #
                # `correction_text IS NOT NULL` mirrors ask_miner.extract_pending()'s own filter, so
                # this counts what the extractor would actually pick up rather than a looser set.
                _c.execute(
                    "SELECT count(*) FILTER (WHERE canonical_ask IS NULL AND correction_text IS NOT NULL),"
                    "       count(*) FILTER (WHERE btrim(COALESCE(canonical_ask,'')) = ''"
                    "                        AND canonical_ask IS NOT NULL),"
                    "       count(*) FROM pattern_observations WHERE org_id=%s", (_org,))
                _undist, _no_ask, _tot = _c.fetchone()
            finally:
                _con.close()
            _pct = (100.0 * _undist / _tot) if _tot else 0.0
            _with_ask = _tot - _undist - _no_ask
            (STATE / "distillation-backlog.json").write_text(json.dumps({
                "measured_at": datetime.now().isoformat(timespec="seconds"),
                "org_id": _org, "total": _tot,
                "unextracted": _undist,          # canonical_ask IS NULL — the real backlog
                "extracted_no_ask": _no_ask,     # '' — processed, no durable ask. NOT backlog.
                "extracted_with_ask": _with_ask,
                "pct_unextracted": round(_pct, 1),
                "note": "canonical_ask NULL = not yet extracted (invisible to the loop). "
                        "Empty string = extracted, no durable ask (correctly processed). "
                        "Summing the two would report a permanent backlog that no pass can clear.",
            }, indent=1))
            if _undist == 0:
                log(f"distillation: 0 pending — all {_tot} corrections extracted "
                    f"({_with_ask} carry an ask, {_no_ask} had no durable ask)")
            elif _pct >= 25:
                log(f"⚠ DISTILLATION BACKLOG: {_undist} of {_tot} corrections ({_pct:.0f}%) are "
                    f"UNEXTRACTED, so the loop CANNOT SEE THEM. Run /close-core step 2d to clear "
                    f"it. This is not 'no new corrections'.")
            else:
                log(f"distillation: {_undist} of {_tot} corrections ({_pct:.0f}%) unextracted — "
                    f"cleared at /close-core step 2d")
        except Exception as _e:
            log(f"⚠ distillation-backlog measurement skipped: {str(_e)[:120]}")

        # ── PHASE H: hook blocks as a failure corpus ────────────────────────────────────────
        # The loop mines corrections Nick TYPES. Every gate also records, mechanically, the moments
        # the agent made the exact mistake a guard exists to catch — 363 blocks in 30 days, and
        # nothing had ever read them. This surfaces the metric Nick named ("if self-improvement
        # works, the Stop hooks should not need to fire") and routes over-the-bar hooks to tuning
        # the GATE, never to minting a contract that restates it.
        try:
            import hook_block_miner as _hbm
            _rep = _hbm.report()
            _hcases = _hbm.cases()
            log(f"hook-blocks: {_rep['total_blocks']} in {_rep['window_days']}d — "
                f"{ {k: v for k, v in list(_rep['by_hook'].items())[:6]} }")
            if _rep.get("security_tier_excluded"):
                log(f"hook-blocks: security tier NOT mined (correct): "
                    f"{_rep['security_tier_excluded']}")
            for c in _hcases:
                log(f"⚠ HOOK OVER BAR: {c['hook']} — {c['blocks']} blocks / {c['sessions']} "
                    f"sessions -> {c['action']}. {c['why'][:150]}")
            # Persist so the trend is readable across sessions rather than only in this log.
            _bpath = STATE / "hook-block-metric.json"
            _bpath.write_text(json.dumps({"measured_at": datetime.now().isoformat(timespec="seconds"),
                                          "report": _rep, "cases": _hcases}, indent=1))
        except Exception as _e:
            log(f"⚠ hook-block mining skipped: {str(_e)[:120]}")

        # ── DEDUPE: stop the loop accreting restatements of its own reminders ───────────────
        # Measured 2026-08-05: SIX active artifacts about routing work to Codex, two pairs of them
        # byte-identical once the "Recurring ask (Nx):" / "Recurring expectation (...)" prefix is
        # stripped. Because they all match on prompts about code, a single turn had three
        # near-identical Codex reminders injected at once — token waste, and the thing that teaches
        # Nick to stop reading injected context at all.
        #
        # friction_loop.run() already dedupes by canonical_ask WITHIN one run, which is why this
        # went unnoticed: these arrived from different paths (ask_ vs fc_) across different closes,
        # so no single run ever saw both. Runs before tune_pass so the tuner is not spending its
        # 2-action budget narrowing an artifact that is about to be deactivated as a duplicate.
        try:
            _dd = fl.dedupe_active(ORG, dry=DRY_RUN)
            if _dd.get("deactivated"):
                log(f"dedupe: deactivated {len(_dd['deactivated'])} restatement(s) — "
                    f"{[(d['artifact_id'], 'dup of ' + d['duplicate_of']) for d in _dd['deactivated']]}")
            for k in (_dd.get("kept") or []):
                log(f"dedupe: kept {k['artifact_id']} (restated by {k['restated_by']}): {k['key']}")
            if _dd.get("skipped"):
                log(f"dedupe: {_dd['skipped']} duplicate(s) left for next close (per-close cap)")
            for e in (_dd.get("errors") or []):
                log(f"⚠ dedupe error: {e}")
        except Exception as _e:
            log(f"⚠ dedupe skipped: {str(_e)[:120]}")

        try:
            _t = fl.tune_pass(ORG, dry=DRY_RUN)
            log(f"tuning: narrowed={_t.get('narrowed')} shadowed={_t.get('shadowed')} "
                f"flagged_rederive={_t.get('flagged_rederive')} untouched={_t.get('skipped')}")
            for aid, act, why in (_t.get("detail") or []):
                log(f"tuning: {aid} -> {act} ({why})")
            for e in (_t.get("errors") or []):
                log(f"⚠ tuning error: {e}")
            # An ineffective rule is FLAGGED, never silenced — surface it so the next session sees
            # it rather than leaving it to rot in a queue file nobody reads. That failure mode is
            # the entire reason this arm was missing for weeks.
            _q = REPO / ".claude" / "state" / "rederive-queue.json"
            if _q.is_file():
                import json as _j
                _items = _j.loads(_q.read_text())
                if _items:
                    log(f"⚠ REDERIVE QUEUE ({len(_items)}): contracts that fire and do not bind — "
                        f"narrowing them would only make them quieter. "
                        f"{[x['artifact_id'] for x in _items]}")
        except Exception as _e:
            log(f"⚠ tuning pass skipped: {str(_e)[:120]}")
        # An unrun extraction pipeline looks exactly like a quiet one. Say which it is.
        _cov = out.get("distillation") or {}
        if _cov.get("extraction_lagging"):
            log(f"⚠ DISTILLATION LAGGING: {_cov.get('action')}")
        elif _cov.get("extraction_never_ran"):
            log(f"⚠ DISTILLATION: 0/{_cov.get('cases')} cases have a canonical_ask — "
                f"{_cov.get('action')}")
        elif _cov.get("cases"):
            log(f"distillation coverage: {_cov.get('distilled')}/{_cov.get('cases')} cases distilled")
        # UNIFIED SPINE (post-cutover): rebuild the runtime projection from the canonical DB so
        # active.json always matches si_artifacts (Codex WS1). Pre-cutover this is skipped — the
        # legacy install path owns active.json and the classifier owns the legacy contracts.
        if not DRY_RUN and (STATE / ".si-unified-spine").exists():
            try:
                import si_project
                proj = si_project.project(ORG)
                inv = si_project.verify_invariants(ORG)
                out["projection"] = proj
                out["invariants"] = inv
                log(f"unified projection: {proj} invariants={inv}")
            except Exception as e:
                log(f"projection skipped (fail-open): {e}")
        # GRADUATION (2026-07-27): a hooked_skill that has fired ≥5 times across ≥3 sessions over
        # ≥7 days, through a corpus-gated trigger, without erroring, promotes itself to a real
        # .claude/skills/ entry. Same evidence window friction_promote uses for enforcement, because
        # gaining an ungated activation surface is at least as consequential as gaining teeth.
        # Demotion runs in the same pass and only ever touches files carrying the generated marker,
        # so a hand-authored skill cannot be removed by it. Fail-open, like everything else here.
        try:
            import skill_graduate as sg
            grad = sg.promote(ORG, dry=DRY_RUN)
            out["graduation"] = grad
            if grad.get("promoted") or grad.get("demoted"):
                log(f"skill graduation: promoted={[p.get('name') for p in grad['promoted']]} "
                    f"demoted={[d.get('skill') for d in grad['demoted']]}")
        except Exception as e:
            log(f"graduation skipped (fail-open): {e}")
        return {"ran": True, **out}
    except Exception as e:
        log(f"friction engine skipped (fail-open): {e}")
        return {"ran": False, "reason": str(e)[:200]}


def notify(title: str, message: str, subtitle: str = "") -> None:
    if DRY_RUN:
        log(f"[dry-run] NOTIFY: {title} — {message}")
        return
    try:
        subprocess.run(["bash", str(NOTIFY), title, message, subtitle], timeout=10)
    except Exception as e:
        log(f"notify failed: {e}")


def main() -> int:
    K = 2  # mirrors si-fix-admission K_DEFAULT; display only

    if "--test-notify" in sys.argv:
        # Verify the real notification path (subprocess → core-notify.sh → osascript).
        notify("Core · test", "Notification path works. This is what a critical alert looks like.")
        log("test-notify fired")
        return 0
    items = detect_items()
    log(f"close pass: {len(items)} item(s) detected")
    autosafe = load_autosafe()
    _db_ok, _db_err = _db_driver_available()
    if not _db_ok:
        # LOUD, and surfaced where Nick actually looks. Without this the entire SI pass no-ops and
        # the close reports success — the exact silent-no-op shape this subsystem is meant to catch.
        log("=" * 72)
        log(f"SI PASS DEGRADED: the Postgres driver is unavailable to this interpreter ({_db_err}).")
        log(f"  interpreter: {sys.executable}")
        log("  Consequence: trust lookup, evidence recording, the friction engine, projection and")
        log("  graduation ALL no-op this close. Nothing self-improved. This is not a healthy close.")
        log("  Cause is almost always an unpinned `python3` resolving to an interpreter without")
        log("  psycopg2. Compare `which -a python3`.")
        log("=" * 72)
        # SEVERITY REFLECTS WHICH QUESTION FAILED. If a bare `python3` still has the driver, the
        # CLOSE PATH is fine and only this invocation was degraded — that is a 🟡 note, not a 🔴
        # outage. Reporting both as 🔴 is what would have fired on core-ops, where the hooks
        # resolve a working interpreter and only an interactive shell does not.
        _close_path_affected = "bare `python3` lacks it too" in _db_err
        items.insert(0, {
            "sev": "🔴" if _close_path_affected else "🟡", "domain": "Liveness",
            "detected": ("SI pass DEGRADED — no Postgres driver, and the close path is affected"
                         if _close_path_affected else
                         "this SI pass ran without a Postgres driver, but the close path looks fine"),
            "fix": ("pin the interpreter for the close path (compare `which -a python3`)"
                    if _close_path_affected else
                    "no action needed unless it recurs from the close hook itself"),
            "fitness": "—", "key": "si-driver-missing",
        })

    trust = trust_lookup(items)

    friction = run_friction_engine()  # generative self-building loop — feeds friction-dispatch

    # ── PROMOTION: a PROVEN contract graduates to a standing CLAUDE.md directive ──────────
    # Nick approved 2026-08-17 ("3 yes"), choosing CLAUDE.md over inject->block. The directive
    # path is append-only and git-reversible, which is the safety he named on 2026-07-23; a block
    # can stop his own work and a revert does not give the turn back.
    #
    # Gates live in artifact_generator.promote_proven_contract and all fail CLOSED:
    #   verdict == DECAYING     measured from pattern_observations (LIVE on every seat).
    #                           Deliberately NOT fire_count, which on life is frozen at 2026-08-05
    #                           because the classifier was disabled 2026-07-23 (T067) — a promoter
    #                           keyed on it would promote from a dead counter.
    #   key in live TRIGGERS    blocks the two contracts the 2026-06-23 L4 dedup deliberately
    #                           retired (stop-and-plan, frustration-deescalate) because
    #                           stop-signal-gate.py already covers those signals, measured at
    #                           40/73 double-injection. BOTH STILL READ DECAYING, so the verdict
    #                           gate alone would promote exactly the two a human switched off.
    #                           This is the guard, not a formality.
    #   auto_apply_directive    refuses text already present, or a corrupt marker section.
    #
    # Dry-run on life at build time: recall-first and verify-dont-claim PROMOTE;
    # frustration-deescalate is DECAYING and BLOCKED as retired; the other three are not DECAYING.
    # DRY_RUN GUARD, ADDED AFTER THIS EXACT BUG. The first wiring had no --dry-run check, and a
    # dry run appended two real directives to CLAUDE.md at 22:47. They were correct lines and the
    # mechanism was approved, but a dry run that writes is not a dry run. Every other autonomous
    # arm in this file honours DRY_RUN; this one now does too.
    promotions = []
    try:
        import json as _json
        sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
        import artifact_generator as _ag
        _fit = REPO / ".claude" / "state" / "contract-fitness.json"
        if _fit.is_file():
            _d = _json.loads(_fit.read_text())
            _items = _d if isinstance(_d, list) else _d.get("contracts", _d.get("items", []))
            if isinstance(_items, dict):
                _items = list(_items.values())
            for _c in _items:
                if not isinstance(_c, dict):
                    continue
                if DRY_RUN:
                    _k = str(_c.get("contract") or "").split("—")[0].strip()
                    _v = str(_c.get("verdict") or "")
                    if _v.startswith("DECAYING") and _k in _ag.live_trigger_keys():
                        promotions.append({"action": "promote_would", "key": _k})
                        log(f"promote (dry): {_k} -> WOULD apply directive")
                    continue
                _r = _ag.promote_proven_contract(ORG, _c)
                if _r.get("action") == "promote_attempted":
                    promotions.append(_r)
                    log(f"promote: {_r.get('key')} -> {_r.get('result', {}).get('action')}")
    except Exception as _e:          # fail-open: a promotion error never breaks close
        log(f"promote: skipped ({type(_e).__name__}: {_e})")

    staleness = {"state": "UNKNOWN"}  # WS3: one unified staleness readout, checked every close
    try:
        sys.path.insert(0, str(REPO / "scheduling" / "claude-si"))
        import staleness_invariant
        staleness = staleness_invariant.readout()
        log(f"staleness: {staleness.get('state')} — {[s['name']+':'+s['state'] for s in staleness.get('signals', [])]}")
    except Exception as e:
        log(f"staleness readout skipped (fail-open): {e}")

    # STEERING LEDGER (master plan Phase 2.4). The value question — is each component's firing
    # worth what it costs — computed every close so the answer is standing evidence rather than
    # something someone has to go and derive. Flags PRE-EMPTED components (the failure is now
    # impossible by construction, so any firing is pure cost) and EXPENSIVE ones (paying a lot,
    # behaviour not declining). Fail-open: a ledger error must never break a close.
    ledger = {"state": "UNKNOWN"}
    try:
        sys.path.insert(0, str(REPO / "bin"))
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("steering_ledger", REPO / "bin" / "steering-ledger.py")
        _sl = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_sl)
        ledger = _sl.build(14)
        act = [c for c in ledger.get("components", [])
               if c["verdict"] in ("EXPENSIVE", "PRE-EMPTED")]
        log(f"steering ledger: {len(ledger.get('components', []))} components, "
            f"{ledger.get('rows')} rows, {len(act)} flagged "
            f"({', '.join(c['hook'] for c in act) or 'none'})")
        if not DRY_RUN:
            (STATE / "steering-ledger.json").write_text(
                json.dumps(ledger, indent=2, ensure_ascii=False))
    except Exception as e:
        log(f"steering ledger skipped (fail-open): {e}")

    # STEERING JUDGE (Phase 3.1). Scores each of this window's steering events on what happened
    # in the turn that FOLLOWED it — read taken, block overridden, or Nick pushing back on the
    # intervention itself. Ground truth is the transcript, never anything a gate wrote about
    # itself, and the judge role holds SELECT-only on steering_events so that is enforced by
    # Postgres rather than by convention. Deterministic; anything undecidable scores neutral.
    judge = {}
    try:
        sys.path.insert(0, str(REPO / "scheduling" / "brain-pg"))
        # INGEST FIRST. steering_ingest.ingest_log() existed and was called from NOTHING in any
        # runtime path — so hook-events.log rows never reached the DB after the one-time backfill,
        # and the judge (which queries the DB) scored a frozen table forever while reporting a
        # perfectly normal "0 scored". A dead measurement loop that looks exactly like a quiet
        # one. Found by Codex's adversarial pass before the baseline push; it is the same
        # silent-absence defect this whole session was about, shipped inside the fix for it.
        import steering_ingest as _si
        _ing = _si.ingest_log(dry_run=DRY_RUN)
        log(f"steering ingest: {_ing}")
        import steering_judge as _sj
        judge = _sj.run(14, write=not DRY_RUN)
        # Surface unscorable and the privilege state. Codex: the close omitted
        # unscorable_transcript_gone, so a peer whose transcripts the judge could not find looked
        # merely quiet. And a judge that failed to drop privilege must say so — it is running with
        # write access to the telemetry it scores.
        log(f"steering judge: {judge.get('scored')} scored across "
            f"{judge.get('sessions')} session(s) — {judge.get('tally')}"
            + (f" · {judge.get('unscorable_transcript_gone')} unscorable (transcript not found)"
               if judge.get("unscorable_transcript_gone") else "")
            + ("" if judge.get("restricted_role")
               else "  ⚠ NOT restricted — judge holds write access to steering_events"))
    except Exception as e:
        log(f"steering judge skipped (fail-open): {e}")

    # AUTO-RETIRE observation (Phase 3.2). Records this close's verdict per component so a
    # retirement needs the same verdict on MIN_OBSERVATIONS separate days before it fires — one
    # bad measurement retires nothing, which matters because three metrics were found this
    # session confidently reporting conclusions their data did not support. Observation only
    # here; --apply is a separate deliberate step. Trust roots are excluded in code regardless.
    retire = {}
    try:
        _rspec = _ilu.spec_from_file_location("steering_retire", REPO / "bin" / "steering-retire.py")
        _sr = _ilu.module_from_spec(_rspec)
        _rspec.loader.exec_module(_sr)
        _hist, _ = _sr.observe(14)
        ready = [n for n, r in _hist.items()
                 if r.get("verdict") in _sr.RETIRABLE
                 and r.get("streak", 0) >= _sr.MIN_OBSERVATIONS
                 and n not in _sr.TRUST_ROOTS]
        retire = {"ready": ready, "watching": [n for n, r in _hist.items()
                                               if r.get("verdict") in _sr.RETIRABLE]}
        if not DRY_RUN:
            (STATE / "retire-observations.json").write_text(json.dumps(_hist, indent=2))
        log(f"auto-retire: watching {len(retire['watching'])}, "
            f"ready {len(ready)} ({', '.join(ready) or 'none'})")
    except Exception as e:
        log(f"auto-retire skipped (fail-open): {e}")

    inbox = {"generated_at": datetime.now().isoformat(timespec="seconds"),
             "k": K, "items": [], "friction": friction, "staleness": staleness,
             "ledger": ledger, "retire": retire, "judge": judge}
    first_fires: list[dict] = []
    criticals: list[dict] = []

    for it in items:
        key, fix = it["key"], it["fix"]
        t = trust.get(key, {"streak": 0, "trusted": False})
        in_safe = key in autosafe
        has_applier = key in APPLIERS
        eligible = in_safe and t["trusted"] and has_applier

        # TRI-STATE APPLIER CONTRACT (2026-08-26). True = resolved. False = tried and could NOT
        # verify. None = NOT APPLICABLE, no claim made.
        #
        # The None state is not a nicety, it is what makes the whole evidence mechanism work, and
        # three independent adversarial reviews found its absence the same day the mechanism
        # shipped. `detect.sh` raises ONE item for a whole population ("N stale files", "N broken
        # refs"), while an applier can only ever prove a SLICE of that population. On any close
        # where its slice is empty the applier had nothing to do — and under the two-state contract
        # it returned False, which `_record_evidence` wrote as `evidence_fail`, which resets the
        # streak EXACTLY AS NICK'S REJECT DOES (si-fix-admission.py RESET_KINDS).
        #
        # So a narrow applier could never accumulate two consecutive positives: every quiet close
        # wiped its progress, including progress built from Nick's own approvals. The gate designed
        # to let autonomy outrun the human could not, in fact, ever admit anything. It is also what
        # lets a RARELY-FIRING detector graduate at all — a key now accumulates across gaps instead
        # of being reset by every close it does not appear in.
        #
        # A no-op must therefore be silent in the ledger. It can only ever WITHHOLD an evidence
        # row; it can never manufacture trust.
        status = "notify-only"
        applied = False
        if eligible:
            # GUARDED, AND THE OUTCOME IS RECORDED. Both halves were missing until 2026-08-26 and
            # both were found by an adversarial review of a NINTH applier that was then rejected —
            # the defects were in the orchestrator, so they applied to all eight already shipped.
            #
            # (a) UNGUARDED CALL. This read `APPLIERS[key]()` bare. Every applier catches
            #     internally, but the seams do not: an ImportError, a serialization error, a failed
            #     write — anything raising here propagates out of the item loop, so the remaining
            #     items are never processed, the inbox is never written, and the audit trail is
            #     skipped, all AFTER earlier appliers in the same pass have already mutated. A
            #     fail-open close that dies half-way is not fail-open.
            #
            # (b) THE REAL LEG RECORDED NO EVIDENCE. _record_evidence was called only on the shadow
            #     branch, so a TRUSTED applier that stopped working could return False at every
            #     close forever and never lose trust — nothing reset its streak, and the only
            #     signal was one "apply-failed" line. Trust earned by demonstrated behaviour has to
            #     be LOST by demonstrated behaviour too, or it is not evidence-based, it is just a
            #     one-time exam. A real-leg False now writes evidence_fail; None still withholds.
            try:
                outcome = True if DRY_RUN else APPLIERS[key]()
            except Exception as e:
                outcome = False
                log(f"applier raised for {key}: {e}")
            if not DRY_RUN and outcome is not None:
                _record_evidence(key, fix, bool(outcome))
            if outcome is None:
                status = "no-op (nothing in this applier's slice)"
            elif outcome:
                status, applied = "auto-applied", True
                marker = STATE / f".core-si-autofired-{key}"
                if not marker.exists():
                    first_fires.append({"key": key, "detected": it["detected"]})
                    if not DRY_RUN:
                        marker.write_text(datetime.now().isoformat())
            else:
                status = "apply-failed"
        elif in_safe and has_applier and not t["trusted"]:
            # EVIDENCE-SEEDED ADMISSION (2026-08-26). This branch used to REPORT progress toward
            # trust and do nothing to create any, because the only thing that advanced the streak
            # was Nick approving the fix by hand. That made autonomy structurally incapable of
            # outrunning him: a fix could sit at "trusted-in 2 more" forever while the close pass
            # ran past it every session.
            #
            # Now the close pass earns the evidence itself: run the applier in SHADOW (verifies
            # preconditions, mutates nothing), and record the outcome as 'evidence' /
            # 'evidence_fail'. Two consecutive successes admit it, and it applies for real on the
            # NEXT close — never in the same pass that produced its own evidence, so a bad shadow
            # can never authorise itself into an immediate application.
            #
            # Still gated by in_safe AND has_applier, both unchanged. This widens only HOW the
            # `trusted` term can be satisfied.
            # `bool(...)` was WRONG here and was the other half of the same defect: it collapsed a
            # not-applicable None into False before _record_evidence ever saw it, so the withhold
            # could not happen even once the appliers learned to report it.
            shadow_ok = None
            if not DRY_RUN:
                try:
                    shadow_ok = APPLIERS[key](shadow=True)
                except Exception as e:
                    shadow_ok = False
                    log(f"shadow run raised for {key}: {e}")
                if shadow_ok is None:
                    log(f"shadow no-op for {key} — empty slice, no evidence row written")
                else:
                    _record_evidence(key, fix, bool(shadow_ok))
            remaining = max(0, K - t["streak"])
            if shadow_ok is False:
                status = "evidence-failed (streak reset)"
            elif shadow_ok is True:
                status = f"evidence recorded — trusted-in {max(0, remaining - 1)} more"
            else:
                status = f"trusted-in {remaining} more (no-op this close)"

        it_out = {**it, "streak": t["streak"], "trusted": t["trusted"],
                  "autosafe": in_safe, "status": status, "applied": applied}
        inbox["items"].append(it_out)
        if it["sev"] == "🔴" and not applied:
            criticals.append(it_out)

    # write the inbox (visibility + app bridge) — always, even if empty
    if not DRY_RUN:
        try:
            (STATE / "core-si-inbox.json").write_text(json.dumps(inbox, indent=2, ensure_ascii=False))
        except Exception as e:
            log(f"inbox write failed: {e}")

    # notifications: first auto-fires (autonomy proof), then a single criticals ping
    for ff in first_fires:
        notify("Core · autonomy", f"Auto-fixed '{ff['key']}' at close — first time it ran on its own.")
        log(f"FIRST AUTO-FIRE: {ff['key']}")

    if criticals:
        import hashlib
        h = hashlib.sha1(",".join(sorted(c["key"] for c in criticals)).encode()).hexdigest()[:12]
        marker = STATE / f".core-si-notified-{h}"
        if not marker.exists():
            head = criticals[0]["detected"][:60]
            n = len(criticals)
            label = "1 critical improvement needs you" if n == 1 else f"{n} critical improvements need you"
            notify("Core · needs you", f"{label} — {head}. Open Core OS → Improvements.")
            if not DRY_RUN:
                marker.write_text(datetime.now().isoformat())
            log(f"CRITICAL NOTIFY: {n} item(s)")

    # friction engine visibility — autonomy is never invisible: notify the FIRST close that
    # installs a self-built contract (deduped by artifact-id set), like the auto-fire ping above.
    installed = (friction.get("installed") or {}).get("contract", 0) if friction.get("ran") else 0
    if installed and not DRY_RUN:
        import hashlib
        ids = sorted(r[0] for r in friction.get("results", []) if len(r) > 1 and r[1] == "installed")
        h = hashlib.sha1(",".join(ids).encode()).hexdigest()[:12]
        marker = STATE / f".friction-installed-{h}"
        if not marker.exists():
            notify("Core · self-built", f"Installed {installed} friction contract(s) at close — "
                   f"Core wired a rule from your own corrections. Open Core OS → Improvements.")
            marker.write_text(datetime.now().isoformat())
            log(f"FRICTION INSTALL NOTIFY: {installed} contract(s) {ids}")

    log(f"close pass done: {len(first_fires)} auto-fired, {len(criticals)} critical, "
        f"friction={installed} installed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FATAL (fail-open): {e}")
        sys.exit(0)
