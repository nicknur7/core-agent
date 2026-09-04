#!/usr/bin/env python3
"""Every rule that claims a hook enforces something, checked against what actually runs.

WHY. On 2026-08-09 three claims in .claude/rules/memory.md said the state-claim-gate and
say-do-gap Stop hooks "enforce this structurally" and "block" a behaviour. Both hooks were
retired 2026-08-06. Both files were still on disk. Neither was registered in settings.json,
so neither had run in three days — while the rules asserting them loaded on EVERY prompt, and
while CLAUDE.base.md, loaded beside them, documented the retirement correctly.

A rule that promises a net which is not there is worse than no rule. You relax against it.
That is the most expensive staleness class in the system because it degrades judgement
silently and its cost only shows up as a mistake nobody caught.

This is derived, never restated: it reads the rules and it reads settings.json, and it
compares. It cannot go stale, because there is nothing in it to update.

    python3 bin/enforcement-audit.py           # human table
    python3 bin/enforcement-audit.py --json    # for the close / a bus reply

Exit 1 if any claim is unbacked.
"""
import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scheduling" / "brain-pg"))
from _env import caller_core_root  # noqa: E402


def resolve_root(argv):
    """Which Core am I auditing? Refuse to guess — a wrong-Core answer is worse than none.

    ANCHORING TO `__file__` IS CORRECT FOR NORMAL OPERATION and is what correction-rate.py
    deliberately does: every Core runs its OWN synced copy, and identity travels with the Core
    on disk while CORE_INSTANCE survives a `cd` into a different one. It breaks in exactly one
    situation — CROSS-CORE REVIEW, where a peer runs THIS Core's file from THEIR seat to check
    it before a sync. That is not an edge case; it is the operation the whole fleet depends on.

    core-business hit it immediately: it ran life's copy from business's cwd, got 29 registered
    hooks (life's number, not its 32), planted three false claims in its own rules, saw three
    silent passes, and was one keystroke from reporting "no unbacked claims on business" — a
    second read of LIFE wearing business's name. The liveness probe is what caught it, which is
    the whole argument for having one.

    So: an explicit --root wins; otherwise, if the caller is standing inside a DIFFERENT Core
    than this script lives in, refuse. Fail toward UNDECIDABLE, never toward a confident number
    about the wrong partition.
    """
    explicit = argv[argv.index("--root") + 1] if "--root" in argv else None
    try:
        return str(caller_core_root(Path(__file__), explicit))
    except RuntimeError as e:
        sys.exit(str(e))


ROOT = resolve_root(sys.argv)
HOOKS_DIR = os.path.join(ROOT, ".claude", "hooks")

# Files that load on every prompt: the rules dir, CLAUDE.md and its imports.
DOCS = []
for d in (os.path.join(ROOT, ".claude", "rules"),
          os.path.join(ROOT, ".claude", "rules-life")):
    if os.path.isdir(d):
        DOCS += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".md")]
# memory/capabilities.md and tasks/lessons.md load every turn too, and casebook item S1 was
# scanning them with its own second implementation of this check. One instrument, both callers.
for f in ("CLAUDE.md", os.path.join(".claude", "CLAUDE.base.md"),
          os.path.join("memory", "capabilities.md"), os.path.join("tasks", "lessons.md")):
    p = os.path.join(ROOT, f)
    if os.path.exists(p):
        DOCS.append(p)

# A hook name followed, within the same sentence, by a word asserting it ACTS.
# "is archived", "was retired", "does not block" are not claims — they are the truth we want.
ENFORCE = r"(enforce|block|prevent|gate[sd]?\b|require[sd]?\b|refus|stop[sp]|catch|fire[sd]?\b)"
NEGATED = r"(retir|archiv|no longer|not\s+register|never\s+ran|cannot|does\s+not|used\s+to|until\s+20|dead|tombston|inert|absent)"
CLAIM = re.compile(r"`?([a-z0-9][a-z0-9-]{3,}(?:\.py|\.sh)?)`?[^.\n]{0,120}?" + ENFORCE, re.I)


def registered_hooks():
    """Hook scripts actually wired into settings.json — the only ones that can run."""
    names = set()
    for sf in ("settings.json", "settings.local.json"):
        p = os.path.join(ROOT, ".claude", sf)
        if not os.path.exists(p):
            continue
        blob = open(p).read()
        for m in re.finditer(r"[\w./-]+\.(?:py|sh)", blob):
            names.add(os.path.basename(m.group(0)))
    return names


def _role_scoped_absent():
    """Hooks that are ABSENT ON PURPOSE for this Core's role — not broken promises.

    bin/hook-registry.json scopes each hook `universal` or `puller`. shared-write-guard is
    puller-scoped: it stops a pull-only Core editing shared paths, so on the WRITER Core it is
    correctly registered nowhere. Flagging it made `memory/capabilities.md`'s accurate description
    of the fleet read as a false claim.

    THIS IS THE FAILURE MODE THAT MATTERS MOST FOR AN AUTO-REVERTING GATE. A false positive here
    does not just add noise — it marks correct documentation as a violation, so a candidate that
    fixes something real still scores as a regression and gets reverted. An instrument that cannot
    tell "absent by design" from "absent by rot" cannot be allowed to revert anything.
    """
    try:
        reg = json.loads((Path(ROOT) / "bin" / "hook-registry.json").read_text())
    except Exception:
        return set()
    hooks = reg if isinstance(reg, list) else reg.get("hooks", reg)
    if isinstance(hooks, dict):
        hooks = [dict(name=k, **(v if isinstance(v, dict) else {})) for k, v in hooks.items()]
    try:
        ident = json.loads((Path(ROOT) / ".claude" / "identity.json").read_text())
        role = str(ident.get("hook_profile", {}).get("role", "")).lower()
    except Exception:
        return set()                     # unknown role: suppress nothing, stay strict
    is_writer = role in ("writer", "baseline-writer")
    out = set()
    for h in hooks or []:
        scope = str(h.get("scope", "")).lower()
        name = str(h.get("name", ""))
        if not name:
            continue
        if scope == "puller" and is_writer:
            out.add(name)
        elif scope == "writer" and not is_writer:
            out.add(name)
    return out


def known_hook_files():
    out = set()
    for dirpath, _, files in os.walk(HOOKS_DIR):
        if "archive" in dirpath.split(os.sep):
            continue
        for f in files:
            if f.endswith((".py", ".sh")):
                out.add(f)
    return out


def main():
    as_json = "--json" in sys.argv
    live = registered_hooks()
    on_disk = known_hook_files()
    by_role = _role_scoped_absent()
    stems = {os.path.splitext(h)[0] for h in on_disk}

    findings = []
    for doc in DOCS:
        for i, line in enumerate(open(doc, errors="replace").read().splitlines(), 1):
            if not line.strip() or line.lstrip().startswith(("|", ">")):
                continue
            for m in CLAIM.finditer(line):
                name = m.group(1)
                stem = os.path.splitext(name)[0]
                if stem not in stems:
                    continue  # not a hook we ship
                # WHOLE LINE, not a window. The 320-char window missed `memory/capabilities.md`
                # line 67 — a single long line LISTING the nine tombstoned hooks, where the word
                # "retired" sits ~180 chars before `recall-gate.py`. So an accurate disclosure of
                # a retirement was reported as a claim of enforcement.
                #
                # KNOWN LIMIT, named by core-business before this change and still true: a line
                # reading "no longer blocks X, but DOES block Y" is now suppressed WHOLE, so the Y
                # claim goes unchecked. Whole-line over-suppresses; the window under-suppressed and
                # produced false positives on honest docs. For an instrument that a gate may act
                # on, a FALSE POSITIVE IS THE MORE EXPENSIVE ERROR — it marks correct work as a
                # regression and reverts it. Splitting such lines into two sentences is the fix if
                # this ever bites.
                if re.search(NEGATED, line, re.I):
                    continue  # the line already says it does not run
                is_live = any(os.path.splitext(h)[0] == stem for h in live)
                if stem in by_role:
                    continue             # absent BY DESIGN for this Core's role — see above
                if not is_live:
                    findings.append({
                        "doc": os.path.relpath(doc, ROOT),
                        "line": i,
                        "hook": name,
                        "on_disk": any(os.path.splitext(h)[0] == stem for h in on_disk),
                        "text": line.strip()[:200],
                    })

    if as_json:
        print(json.dumps({"registered": sorted(live), "unbacked": findings}, indent=2))
        return 1 if findings else 0

    print("\n  ENFORCEMENT AUDIT — %d hooks registered, %d shipped on disk\n"
          % (len(live), len(on_disk)))
    if not findings:
        print("  No unbacked enforcement claims. Every rule that says a hook acts names one"
              " that runs.\n")
        return 0
    for f in findings:
        print("  UNBACKED  %s:%s" % (f["doc"], f["line"]))
        print("            claims `%s` %s" % (f["hook"],
              "— file exists but is registered in NOTHING" if f["on_disk"] else "— no such hook"))
        print("            %s\n" % f["text"])
    print("  %d unbacked claim(s). A rule promising a net that is not there is worse than"
          " no rule.\n" % len(findings))
    return 1


if __name__ == "__main__":
    sys.exit(main())
