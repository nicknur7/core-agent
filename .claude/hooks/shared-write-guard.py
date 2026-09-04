#!/usr/bin/env python3
"""PreToolUse(Write|Edit|MultiEdit) — baseline-shared-file writer guard.

The clobber loop (verified 2026-06-23): a NON-writer Core edits a baseline-SHARED
file (a hook, a rule, bin/, scheduling/…). It looks fixed — but `baseline wins`, so
the next `/sync pull` overwrites it with the writer Core's version and the fix
silently vanishes. Business/school/finance all hit this on the same files.

This hook makes the single-writer policy structural: on a non-writer Core, block
edits to shared-path files and point the model at the durable path — make the
change in the writer Core (life), then `/sync push`.

  writer Core (manifest.baseline_writer == identity.domain_label) → always pass
  shared path AND not per_core_keep → block (exit 2)
  everything else (per-core files, own work) → pass

Shared/keep sets come from bin/sync-manifest.json (single source of truth), so this
never drifts from what actually syncs. Fail-open on any error — telemetry/guards
must never break a turn.
"""
import fnmatch
import json
import os
import sys
from pathlib import Path

SHARED_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _matches(rel: str, patterns) -> bool:
    """Does repo-relative `rel` fall under any manifest pattern?
    Handles `dir/**` globs, `*`-globs, plain files, and dir prefixes."""
    for p in patterns:
        if p.endswith("/**"):
            base = p[:-3]
            if rel == base or rel.startswith(base + "/"):
                return True
        elif "*" in p:
            if fnmatch.fnmatch(rel, p):
                return True
        else:
            if rel == p or rel.startswith(p.rstrip("/") + "/"):
                return True
    return False


def _heartbeat() -> None:
    """Write one `invoke` row per session, so silence is distinguishable from absence.

    LIVENESS FOR A GUARD WHOSE NORMAL STATE IS SILENT (2026-08-12, core-finance DOSE 35). This hook
    had ZERO rows in hook-events.log — not zero invocations logged, zero rows of any kind. On life
    that is expected, because life is the baseline WRITER and this guard only refuses pull-only
    Cores; but "expected here" and "running here" are different claims, and nothing could tell them
    apart on any seat.

    estate-sweep protects this hook on the reasoning that "a security gate that happens to be quiet
    is still a security gate — quietness is what a working guard looks like." Quietness is equally
    what an unregistered guard looks like. The 2026-08-06 hook retirement is auditable precisely
    because the retired hooks logged invocations; this one could vanish from settings.json — the
    documented loss mechanism, since settings.json is per_core_keep and does not sync — and leave
    the log completely unchanged.

    Once per session, keyed off `.session-start`, so a guard on a write-heavy seat does not bury the
    ledger. Fail-silent: telemetry must never affect whether a write is refused.
    """
    try:
        state = Path(os.environ.get("CORE_INSTANCE")
                     or Path(__file__).resolve().parents[2]) / ".claude" / "state"
        mark = state / ".hook-alive-shared-write-guard"
        started = state / ".session-start"
        if mark.exists() and started.exists() and mark.stat().st_mtime >= started.stat().st_mtime:
            return
        sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
        import hooklog
        hooklog.log("shared-write-guard", "PreToolUse", verdict="invoke",
                    trigger="session heartbeat — the guard ran")
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
    except Exception:
        pass


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    # BEFORE the tool filter, deliberately: the question this answers is "did this hook run", not
    # "did it examine a shared path". Placing it after the filter would make a seat that simply
    # never touched a shared file look identical to a seat where the hook was unregistered — the
    # exact conflation being fixed.
    _heartbeat()
    if data.get("tool_name", "") not in SHARED_TOOLS:
        return 0
    fp = (data.get("tool_input") or {}).get("file_path", "")
    if not fp:
        return 0

    inst = os.environ.get("CORE_INSTANCE") or os.environ.get("CLAUDE_PROJECT_DIR")
    if not inst:
        return 0
    inst = Path(inst).resolve()

    try:
        ident = json.load(open(inst / ".claude" / "identity.json"))
        man = json.load(open(inst / "bin" / "sync-manifest.json"))
    except Exception:
        return 0

    # ROLE IS STATED, SO READ IT — do not infer writer-ness from a name comparison.
    #
    # This used to compare identity.domain_label against manifest.baseline_writer, and
    # fail OPEN when the slug was empty. The shipped template has no domain_label at all
    # (it carries core_slug), so `slug` was "" on every Core created from the template and
    # the guard permitted everything: a fresh Core could edit any hook, rule, bin/ or
    # scheduling/ file, and the next sync would silently overwrite it because baseline wins.
    # A guard whose whole job is "you are not the writer" was answering "I don't know, so
    # go ahead." Found by core-business running the suites on a cold clone of 172a758.
    #
    # hook_profile.role answers the question directly. The template already sets it to
    # "puller" and documents it as REQUIRED (reconcile-hooks.py exits 2 on any other value),
    # and bin/spawn-core writes it on every spawn. life carries "writer".
    # Bound unconditionally — the block message below interpolates it, and defining it only
    # inside the fallback branch left it unbound on the role path. The suite did not catch
    # that because this Core is the writer and returns before ever reaching the message;
    # running the guard as a template-shaped puller did. (2026-07-28.)
    slug = ident.get("domain_label", "") or ident.get("core_slug", "") or "unconfigured"
    writer = (man.get("baseline_writer", "") or "")
    if writer.startswith("core-"):
        writer = writer[len("core-"):]

    role = ((ident.get("hook_profile") or {}).get("role") or "").strip().lower()
    if role == "writer":
        # BOTH KEYS, because this and bin/sync-to-baseline.sh were two resolvers for one question
        # and they disagreed exactly where it matters. This read identity.json's declared role; the
        # push script compares the DIRECTORY BASENAME against manifest.baseline_writer. A renamed
        # tree that still carries the real identity.json — a peer-review checkout (named
        # `<basename>-<sha>`), a restored backup, a differently-named clone, a git worktree — got
        # "you are the writer, go ahead" at edit time and "REFUSED — not the baseline writer" at
        # push time.
        #
        # Failing at the push is failing closed, so nothing was ever clobbered. What it costs is
        # WORK: edits authored in a tree that can never ship them, discovered at the end. Refusing
        # early is the same verdict delivered when it is still cheap.
        #
        # The identity file travels with a copy and the directory name does not, which is precisely
        # why the directory is the honest signal for "is this the canonical writer tree" and the
        # role is the honest signal for "is this Core allowed to author shared code". Both, or
        # neither.
        here = inst.name
        declared = (man.get("baseline_writer", "") or "")
        if declared and here != declared:
            sys.stderr.write(
                "shared-write-guard: REFUSED — this tree declares role=writer but is named %r,\n"
                "  while the baseline writer is %r. bin/sync-to-baseline.sh compares the DIRECTORY\n"
                "  name and would refuse to push whatever you write here, so the edit is authored\n"
                "  in a tree that cannot ship it. Work in %r, or re-run the push with\n"
                "  --force-writer if this change is genuinely meant to originate in a copy.\n"
                % (here, declared, declared))
            return 2
        return 0                       # the one Core allowed to edit shared code

    if not role:
        # No declared role — fall back to the legacy slug comparison, for identities written
        # before hook_profile existed.
        if slug != "unconfigured" and slug == writer:
            return 0
        # ...and if identity is still indeterminate, FAIL CLOSED. This is the direction that
        # was backwards. The guard only fires on writes to SHARED paths; a Core that cannot
        # establish it is the writer has no business editing them silently, and the block
        # message below tells the operator exactly which field to set.
        role = "puller"

    try:
        rel = str(Path(fp).resolve().relative_to(inst))
    except Exception:
        return 0  # outside this instance → not our concern

    shared = man.get("shared", {})
    in_shared = _matches(rel, shared.get("dirs", [])) or rel in shared.get("files", [])
    in_keep = _matches(rel, man.get("per_core_keep", []))
    if not (in_shared and not in_keep):
        return 0

    sys.stderr.write(
        "\n================================================================\n"
        "  SHARED-FILE WRITE BLOCKED — pull-only Core\n"
        "================================================================\n"
        f"  This is the '{slug}' Core (pull-only). The target is a baseline-SHARED file:\n"
        f"    {rel}\n\n"
        "  Editing it here is the clobber loop: `baseline wins`, so the next\n"
        "  `/sync pull` overwrites your change with the writer Core's version and\n"
        f"  the fix silently vanishes. (Baseline writer = '{writer}'.)\n\n"
        "  Durable fix: make this change in the writer Core, then `/sync push`.\n"
        "  Per-core files (memory/, settings.json, identity.json, sentinel/…) are\n"
        "  NOT blocked — only baseline-shared code.\n"
        "================================================================\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
