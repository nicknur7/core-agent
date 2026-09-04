#!/usr/bin/env python3
"""reconcile-hooks.py — derive a Core's .claude/settings.json hook registration
from the shared bin/hook-registry.json + the Core's identity.json hook_profile.

The universal-vs-personalized abstraction, made executable:
  - registry scope "universal"  -> every Core registers the hook
  - registry scope "puller"     -> only role==puller Cores (not the writer)
  - registry flag  "sentinel"   -> the security gate; MANAGED IN PHASE 4, never
                                   touched here (neither added nor removed).
  - identity hook_profile.overrides {name: "on"|"off"} -> per-Core escape hatch.

Anything registered that this tool does NOT manage (sentinel hooks, or any hook
absent from the registry) is PRESERVED verbatim — the reconciler only adds the
managed hooks a role should have and removes managed hooks it should not.

--emit-template regenerates a STATIC settings.json's hooks block wholesale (not an
incremental add/remove against whatever drift already exists) so the baseline's own
`.claude/settings.json` — the template every `git clone` starts from — can be kept in
sync with the registry mechanically instead of by hand. WHY THIS EXISTS: that file is
`per_core_keep`, so sync-to-baseline.sh's rsync never copies a live Core's settings.json
over it, and nothing else ever wrote to it either — it was hand-committed 2026-05-19 and
never touched again while hook-registry.json grew from ~12 entries to 58. A fresh clone
therefore registered 11 of 44 managed hooks, and `sync-from-baseline@SessionStart` — the
one hook that would have let the Core self-heal on first open — was itself among the
missing 33. Measured 2026-08-31: `reconcile-hooks.py --check` against that stale template
reported exactly this gap. --emit-template is the mechanical fix: sync-to-baseline.sh now
calls it on every push, so the committed template can never drift from the registry again.

Usage:
  reconcile-hooks.py [--core PATH] [--check|--apply] [--registry PATH]
    --core      Core root (default: $PWD). Reads <core>/.claude/{identity,settings.json}
    --check     (default) report drift, exit 0 if clean / 1 if drift
    --apply     mutate settings.json to the desired state, then re-verify
    --registry  registry path (default: <script_dir>/hook-registry.json)

  reconcile-hooks.py --emit-template PATH [--role writer|puller|unconfigured] [--registry PATH]
    Rebuild PATH's "hooks" key from scratch for --role (default: puller — what
    identity.json's template ships with) and write it back, leaving every OTHER
    top-level key in PATH (env, permissions, model, mcp, ...) byte-for-byte as it was.
    Unlike --apply, this ALSO seeds sentinel-flagged entries (e.g. pretooluse-guard):
    --check/--apply never add or remove those on a LIVE Core (Phase 4 boundary, see
    PROTECTED_HOOKS below) because an ongoing reconcile must never let the registry
    vote the security gate on or off — but a template with no prior settings.json has
    no existing sentinel registration to preserve, so if this didn't seed it here,
    nothing ever would, and a fresh clone would boot with pretooluse-guard silently
    absent. No --core/identity.json is read; there is no per-Core role to defer to.
"""
import argparse, json, os, sys

def load(p):
    with open(p) as f:
        return json.load(f)

def hook_name(cmd):
    import re
    m = re.search(r'([A-Za-z0-9_-]+)\.(sh|py)', cmd or '')
    return m.group(1) if m else None

# PROTECTED hooks — identities pinned HERE, in local code, never derived from the registry.
#
# The registry arrives over the network from the baseline repo, and since 2026-07-27 this script
# runs UNATTENDED at the tail of every pull. Before that a human ran it. Codex review caught the
# consequence: `desired_set` skipped entries flagged `sentinel: true` IN THE REGISTRY, so a registry
# that simply flipped that flag to false (or dropped the entry) would classify the live Sentinel gate
# as "extra" and remove it — disarming every outward-action check on the next pull. A registry must
# never be able to decide which hooks are security-critical.
PROTECTED_HOOKS = {"pretooluse-guard", "sentinel-approve", "shared-write-guard"}

# Closed field set for a registry entry. An entry carrying anything else is rejected outright rather  # privacy-ok: generic engineering vocabulary
# than partially honored — a data file from the network gets no benefit of the doubt.
_ENTRY_KEYS = {"name", "event", "matcher", "command", "timeout", "scope", "sentinel", "order",
               "retired", "retired_reason", "intent"}
_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd",
           "Notification", "SubagentStop", "PreCompact",
           # 2026-07-30: OBSERVER events, added so event-probe.py can measure which of the ~30
           # documented events this harness actually dispatches (the operator: there are 30
           # different aspects of a turn, make sure everything is being used where it needs to be).
           #
           # This set is a SECURITY control — it exists so a registry pulled from the network
           # gets no benefit of the doubt — so widening it is deliberate, not incidental. Every
           # name below is an observation point: a hook there sees what happened and can inject
           # or block, which is the same authority the existing nine already carry.
           #
           # DELIBERATELY EXCLUDED: PermissionRequest and PermissionDenied. A hook on those sits
           # in the APPROVAL path, where the failure mode is auto-approving something Nick did
           # not approve. That is a different authority class from everything here, it is
           # adjacent to the Sentinel trust root, and per the master plan (§8) blast-radius
           # changes get an adversarial review pass before they ship. They stay out until then.
           "PostToolBatch", "PostToolUseFailure", "PostCompact", "InstructionsLoaded",
           "UserPromptExpansion", "SubagentStart", "StopFailure", "MessageDisplay"}

# Events whose authority in THIS harness is not yet established. They may carry the observer probe
# and nothing else; see the check in validate_registry for why. Removing a name from this set is a
# deliberate act that should follow evidence from bin/event-coverage.py, not convenience.
_PROBE_ONLY_EVENTS = {"PostToolBatch", "PostToolUseFailure", "PostCompact", "InstructionsLoaded",
                      "UserPromptExpansion", "SubagentStart", "StopFailure", "MessageDisplay"}

# AUTHORITY ESTABLISHED BY TEST, PER EVENT — the exact thing _PROBE_ONLY_EVENTS was waiting for.
#
# The rail above says an event stays probe-only "until its authority is established". On 2026-08-06
# MessageDisplay's authority was established by running the experiment rather than reading the docs:
# a probe returning exit 2 on a sentinel chunk was IGNORED and the sentinel reached the terminal,
# while the same probe recorded the full reply text in 64-415 char chunks with index+final. So its
# authority is precisely: OBSERVE YES, ACT NO.
#
# That is a narrower permission than "no longer probe-only", and the difference matters. Granting
# blanket authority on the strength of an observe-only test is how a careless port lands a blocking
# hook on an event that cannot block — which would fail SILENTLY, since the hook would run, return 2,
# and be ignored. A gate that cannot refuse but believes it can is worse than no gate.
#
# So: named scripts, with the effect they are permitted, and anything else on the event still refused.
_ESTABLISHED_AUTHORITY = {
    "MessageDisplay": {
        # script basename -> permitted intent.effect
        "reply-observer.py": {"log-only"},
    },
    # PostToolBatch — tested 2026-08-25, same way and for the same reason as MessageDisplay was.
    #
    # WHAT WAS TESTED: a throwaway hook registered on PostToolBatch emitted
    # hookSpecificOutput.additionalContext carrying a sentinel string. The sentinel arrived in the
    # session on the very next tool batch, with no restart. So on this build the event both fires
    # (event-probe.log has thousands of rows) and INJECTS.
    #
    # WHAT WAS NOT TESTED, AND IS THEREFORE NOT GRANTED: whether a non-zero exit BLOCKS here. That
    # experiment was not run, so blocking authority on this event remains unestablished and any
    # entry claiming effect 'block' still hits the probe-only refusal below. This is the distinction
    # the MessageDisplay note exists to protect — "granting blanket authority on the strength of an
    # observe-only test is how a careless port lands a blocking hook on an event that cannot block",
    # and the symmetric mistake here would be reading an inject-yes result as act-yes. If someone
    # later needs to block at PostToolBatch, run the exit-2 experiment first and widen this set on
    # the evidence, exactly as this line was widened.
    "PostToolBatch": {
        "preempt-gate.py": {"inject"},
    },
}


def validate_registry(registry):
    """Reject a malformed or hostile registry BEFORE it can influence settings.json. Returns a list
    of problems; a non-empty list means refuse to apply anything (fail closed, change nothing)."""
    problems = []
    hooks = registry.get('hooks')
    if not isinstance(hooks, list) or not hooks:
        return ["registry has no 'hooks' list"]
    for i, h in enumerate(hooks):
        if not isinstance(h, dict):
            problems.append(f"hooks[{i}] is not an object")
            continue
        extra = set(h) - _ENTRY_KEYS
        if extra:
            problems.append(f"hooks[{i}] has unknown key(s): {sorted(extra)}")
        for k in ("name", "event", "command", "scope"):
            if not isinstance(h.get(k), str) or not h[k]:
                problems.append(f"hooks[{i}] missing/invalid {k}")
        if h.get("event") not in _EVENTS:
            problems.append(f"hooks[{i}] unknown event {h.get('event')!r}")
        # PROBE-ONLY EVENTS. The eight events added 2026-07-30 were added to MEASURE whether this
        # harness dispatches them; their authority here is UNKNOWN, not established-as-observation.
        # Two are specifically unclear: if the harness lets a hook on UserPromptExpansion rewrite
        # the expanded prompt, or one on MessageDisplay alter what Nick SEES, that is a capability
        # the original nine do not have — inject is not rewrite, and display tampering could hide
        # evidence from him. event-coverage.py currently asserts MessageDisplay "cannot change what
        # the model sees" on the strength of the DOCS, which is exactly the epistemology this whole
        # day rejected.
        #
        # So they carry the probe and nothing else until a gate is deliberately ported to one, at
        # which point this list is the thing that has to be edited on purpose. Matches the stated
        # intent — probe first, port deliberately — instead of leaving it to discipline.
        # (Fable, blast-radius review.)
        # Checked on the INVOKED SCRIPT, not by substring. Fable demonstrated the bypass on the
        # first version: `bash evil.sh # event-probe.py` and
        # `python3 evil.py --tag event-probe.py; rm -rf /tmp/x` both validated clean, because a
        # substring test is satisfied by a comment or an argument. The check now finds the first
        # token ending in .py/.sh and requires its BASENAME to be event-probe.py.
        #
        # Honest scope, because Fable was right about this too: the real threat model here is a
        # CARELESS port, not a hostile registry. A hostile registry needs no bypass at all —
        # command content on the nine established events is unvalidated, so Stop was always open,
        # and sentinel-code on pull is the actual defence there. This makes the careless case
        # impossible and the hostile case no easier.
        if h.get("event") in _PROBE_ONLY_EVENTS:
            # Strip quotes BEFORE the extension test. The registry's own commands are
            # `python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/event-probe.py" PostToolBatch`, so the
            # script token ends with `.py"` — quote included — and the first cut of this check
            # rejected all eight legitimate probe entries while reporting `got ''`. Third time
            # today a check I wrote failed on its own valid input; each one was found by running
            # it rather than reading it.
            _toks = [t.strip('"\'') for t in (h.get("command") or "").split()]
            _first = next((t for t in _toks if t.endswith((".py", ".sh"))), "")
            _base = os.path.basename(_first)
            # An event may have authority established for SPECIFIC scripts at a SPECIFIC effect.
            # MessageDisplay is the first: tested 2026-08-06 as observe-yes / act-no, so a log-only
            # observer is permitted there and anything claiming block or inject is still refused —
            # on that event a returned 2 is silently ignored, so such a hook would be a gate that
            # cannot refuse while believing it can.
            _allowed = _ESTABLISHED_AUTHORITY.get(h.get("event"), {})
            if _base in _allowed:
                _eff = ((h.get("intent") or {}).get("effect") or "").strip()
                if _eff not in _allowed[_base]:
                    problems.append(
                        f"hooks[{i}] {_base} on {h.get('event')!r} may only be "
                        f"{sorted(_allowed[_base])} — its established authority is observe-only "
                        f"(tested 2026-08-06: exit 2 there is ignored); got effect {_eff!r}")
                continue
            if _base != "event-probe.py":
                problems.append(
                    f"hooks[{i}] event {h.get('event')!r} is probe-only until its authority is "
                    f"established; the invoked script must be event-probe.py (got {_first!r})")
        if h.get("scope") not in ("universal", "puller"):
            problems.append(f"hooks[{i}] unknown scope {h.get('scope')!r}")
        t = h.get("timeout")
        if t is not None and not (isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= 120):
            problems.append(f"hooks[{i}] timeout out of range")
    return problems


# The role a Core has before it has declared one. Gets every hook that needs no upstream
# relationship — anti-pattern gates, the SI spine — and none that assume one.
UNCONFIGURED = 'unconfigured'

# The retired learned-workflow layer. Superseded by the unified SI spine at the 2026-07-23 cutover
# (memory/projects/core-improvement.md:13); the files stay until the whole fleet migrates, and the
# registry still scopes them 'universal' so migrated Cores keep them until they cut over.
#
# A BRAND-NEW Core must NOT get them: it would boot the retired classifier alongside the unified
# spine — the exact double-architecture the cutover exists to eliminate. An unconfigured Core has no
# legacy layer to preserve, so it starts on the current one only. (Codex review, 2026-07-27.)
#
# `learned-recallguard` LEFT THIS SET ON 2026-08-06. It is no longer "legacy but preserved" — it is
# retired outright, tombstoned in the registry, under Nick's policy that nothing may drive the agent
# after the reply is sent. Membership here means "still installed on migrated Cores"; a tombstoned
# hook must be REMOVED from those Cores instead, which the retired:true branch in desired_set does.
# Leaving it in both places would have asserted two contradictory fates for the same hook.
LEGACY_LAYER = {
    'learned-classifier', 'learned-resynth-trigger',
    'learned-stopguard', 'learned-validator',
}


def desired_set(registry, role, overrides, include_sentinel=False):
    """Return {(name, event, matcher): entry} for managed hooks this Core should register. Keyed per
    EVENT so a hook registered for one event (e.g. friction-dispatch on UserPromptSubmit) still gets its
    OTHER events (Stop) added — a name-only key silently dropped them.

    include_sentinel is False for every LIVE-Core caller (--check, --apply) — sentinel hooks stay
    Phase-4-managed, i.e. this function never adds or removes them, full stop. It is True only for
    --emit-template, which is building a settings.json that has no prior sentinel registration to
    preserve (see the module docstring). When True, a sentinel entry ships to EVERY role and ignores
    scope/overrides entirely — the security gate is not something a role or an override can opt out
    of, template or not."""
    managed = {}
    for h in registry['hooks']:
        if h.get('sentinel'):
            if not include_sentinel:
                continue  # Phase 4 — never managed here
            if h.get('retired'):
                managed.pop((h['name'], h['event'], h.get('matcher') or ''), None)
                continue
            managed[(h['name'], h['event'], h.get('matcher') or '')] = h
            continue
        if h.get('retired'):
            # RETIREMENT NEEDS A TOMBSTONE, NOT AN ABSENCE.
            #
            # Deleting a hook's registry entry does NOT unregister it. managed_names() is
            # built from the registry, current_registered() only looks for those names, and
            # anything unmanaged is preserved verbatim — so a deleted entry leaves the hook
            # running in every Core's settings.json forever, invisible to this tool. Found
            # 2026-07-27 while retiring approval-gate: the registry went 39 -> 37 and the
            # hook kept firing.
            #
            # So a retired hook KEEPS its entry with retired:true. It stays in managed_names,
            # gets found where it is registered, is excluded from the desired set, and is
            # therefore removed — on every Core, on their next pull. The tombstone is also
            # the audit trail: why it went, and when, travels with the fleet.
            managed.pop((h['name'], h['event'], h.get('matcher') or ''), None)
            continue
        if role == UNCONFIGURED:
            # universal only (a puller-scoped hook assumes an upstream), minus the retired layer
            want = h['scope'] == 'universal' and h['name'] not in LEGACY_LAYER
        else:
            want = h['scope'] == 'universal' or (h['scope'] == 'puller' and role == 'puller')
        ov = overrides.get(h['name'])
        if ov == 'off':
            want = False
        elif ov == 'on' and role != UNCONFIGURED:
            # An unconfigured Core may not override a hook ON — overrides are how a configured Core
            # tunes its own set, and honouring them before the upstream relationship is settled could
            # switch on sync or shared-write behaviour that has no upstream to point at.
            want = True
        if want:
            managed[(h['name'], h['event'], h.get('matcher') or '')] = h
    return managed

def managed_names(registry):
    return {h['name'] for h in registry['hooks'] if not h.get('sentinel')}

def current_registered(settings, names_filter):
    """{(name, event, matcher): [(event, matcher_index, hook_index)]} for managed hooks present now."""
    found = {}
    hooks = settings.get('hooks', {})
    for event, blocks in hooks.items():
        for bi, block in enumerate(blocks):
            matcher = block.get('matcher') or ''
            for hi, h in enumerate(block.get('hooks', [])):
                nm = hook_name(h.get('command', ''))
                if nm in names_filter:
                    found.setdefault((nm, event, matcher), []).append((event, bi, hi))
    return found

def add_hook(settings, entry):
    """Add one registry entry's hook object to settings under its event+matcher.

    `order: "first"` puts the hook at the head of its block AND its block at the head of the
    event. The auto-pull (sync-from-baseline) needs this: every other SessionStart hook reads
    state the pull refreshes, so running it second means the whole session reads stale state.
    Carried over from the retired shared-hooks.json `prepend` flag (2026-07-27 consolidation).
    """
    hooks = settings.setdefault('hooks', {})
    blocks = hooks.setdefault(entry['event'], [])
    obj = {'type': 'command', 'command': entry['command']}
    if entry.get('timeout') is not None:
        obj['timeout'] = entry['timeout']
    first = entry.get('order') == 'first'
    for bi, block in enumerate(blocks):
        if block.get('matcher') == entry.get('matcher'):
            lst = block.setdefault('hooks', [])
            lst.insert(0, obj) if first else lst.append(obj)
            if first and bi != 0:
                blocks.insert(0, blocks.pop(bi))
            return
    newblock = {'hooks': [obj]}
    if entry.get('matcher') is not None:
        newblock = {'matcher': entry['matcher'], 'hooks': [obj]}
    blocks.insert(0, newblock) if first else blocks.append(newblock)

def remove_hook(settings, key):
    """Remove a managed hook occurrence identified by (name, event, matcher); drop empty blocks."""
    name, ev, matcher = key
    hooks = settings.get('hooks', {})
    for event in list(hooks.keys()):
        if event != ev:
            continue
        for block in hooks[event]:
            if (block.get('matcher') or '') != matcher:
                continue
            block['hooks'] = [h for h in block.get('hooks', []) if hook_name(h.get('command', '')) != name]
        hooks[event] = [b for b in hooks[event] if b.get('hooks')]
        if not hooks[event]:
            del hooks[event]

_SKIP_BACKUP = False


def atomic_write_json(path, data):
    """Write `data` to `path` atomically: temp file, re-parse to prove valid JSON, keep a .bak of
    whatever was there before (best-effort), then os.replace() (atomic on POSIX). On ANY error the
    original file is left exactly as it was. Returns None on success, else an error string.

    Factored out of --apply 2026-08-31 so --emit-template inherits the same fail-safe property
    without a second copy of it — both run UNATTENDED (--apply at the tail of every pull,
    --emit-template at the tail of every baseline push), so a half-written settings.json is not
    an acceptable failure mode in either caller.
    """
    tmp_path = path + f'.tmp.{os.getpid()}'
    try:
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        with open(tmp_path) as f:
            json.load(f)  # prove it re-parses before it can replace anything
        if os.path.exists(path):
            try:
                # .bak lands beside the target. For --apply on a live Core that is a local
                # safety copy and correct. For --emit-template writing the BASELINE's
                # settings.json during a push, `git add -A` sweeps it upstream: sentinel-code
                # caught .claude/settings.json.bak entering the baseline as a new tracked file,
                # silently overwritten on every future push. Skip the backup when the caller is
                # generating a template — there is nothing local to protect, the prior content is
                # already in git.
                if _SKIP_BACKUP:
                    pass
                elif True:
                  with open(path) as src, open(path + '.bak', 'w') as dst:
                    dst.write(src.read())
            except Exception:
                pass  # backup is best-effort; never block the verified-good write
        os.replace(tmp_path, path)
        return None
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return str(e)


def emit_template(reg_path, out_path, role):
    """Rebuild out_path's "hooks" key WHOLESALE for `role` from the registry at reg_path, and write
    it back with every OTHER top-level key (env, permissions, model, mcp, ...) untouched. See the
    module docstring for why this exists and why it seeds sentinel entries that --check/--apply
    deliberately never touch.

    Wholesale, not incremental: --apply reconciles an existing settings.json's drift (add what's
    missing, remove what's extra) because a LIVE Core's file may carry hooks this tool doesn't
    manage at all, which must survive untouched. A template has no such history to respect — the
    whole point is to stop trusting whatever was last hand-committed, so every managed hook's
    registration here comes from the registry, full stop.
    """
    registry = load(reg_path)
    problems = validate_registry(registry)
    if problems:
        print("[reconcile-hooks --emit-template] REFUSING — registry failed validation, "
              f"{out_path} untouched:", file=sys.stderr)
        for p in problems[:12]:
            print(f"    {p}", file=sys.stderr)
        return 2

    want = desired_set(registry, role, overrides={}, include_sentinel=True)

    out = load(out_path) if os.path.exists(out_path) else {}
    out['hooks'] = {}  # only key touched — every other top-level key carries over via `out` itself
    for entry in want.values():
        add_hook(out, entry)

    # Suppress the .bak here. atomic_write_json keeps one beside the target, which is correct for
    # --apply on a live Core. But --emit-template writes the BASELINE's settings.json during a push,
    # and `git add -A` then sweeps the backup upstream: sentinel-code caught
    # .claude/settings.json.bak entering the baseline as a new tracked file that every future push
    # would silently overwrite. Nothing local needs protecting here — the prior content is in git.
    global _SKIP_BACKUP
    _SKIP_BACKUP = True
    try:
        err = atomic_write_json(out_path, out)
    finally:
        _SKIP_BACKUP = False
    if err:
        print(f"[reconcile-hooks --emit-template] ABORTED, {out_path} untouched ({err})",
              file=sys.stderr)
        return 1

    n_sentinel = sum(1 for h in want.values() if h.get('sentinel'))
    print(f"[reconcile-hooks --emit-template] role={role}: wrote {len(want)} hook registrations "
          f"({n_sentinel} sentinel) -> {out_path}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--core', default=os.getcwd())
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--registry', default=None)
    ap.add_argument('--emit-template', metavar='PATH', default=None,
        help='Rebuild PATH\'s "hooks" key from the registry for --role and write it back, leaving '
             'every other top-level key untouched. For the baseline\'s own template settings.json '
             '(see sync-to-baseline.sh) — a live Core should use --core/--apply instead so its own '
             'identity.json role + overrides are respected.')
    ap.add_argument('--role', default='puller', choices=('writer', 'puller', UNCONFIGURED),
        help='Role to emit for with --emit-template (default: puller — what a fresh clone\'s '
             'identity.json template ships with). Ignored otherwise.')
    a = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    reg_path = a.registry or os.path.join(script_dir, 'hook-registry.json')

    if a.emit_template:
        return emit_template(reg_path, a.emit_template, a.role)

    ident_path = os.path.join(a.core, '.claude', 'identity.json')
    settings_path = os.path.join(a.core, '.claude', 'settings.json')

    registry = load(reg_path)
    ident = load(ident_path)
    profile = ident.get('hook_profile') or {}
    role = profile.get('role')
    overrides = profile.get('overrides') or {}
    if role not in ('writer', 'puller', UNCONFIGURED):
        # A FRESH CLONE HAS NO ROLE, AND THAT IS NOT AN ERROR — it is the normal starting state.
        #
        # This returned exit 2 until 2026-07-27, and sync-from-baseline.sh swallowed the failure, so
        # on a new Core the installer silently refused to run and NONE of the registry-managed hooks
        # were ever registered — including all 15 SI-spine hooks. The self-improvement engine shipped
        # correctly registered in hook-registry.json and never turned on for anyone but the author.
        # The template's identity.json simply has no hook_profile key.
        #
        # So an absent role now means UNCONFIGURED: install everything that needs no upstream
        # relationship, skip everything that does, and say so loudly. A Core that has not yet declared
        # whether it is a writer or a puller still gets its anti-pattern gates and its SI spine.
        print(f"[reconcile-hooks] NOTE: {ident_path} declares no hook_profile.role — treating this "
              f"Core as '{UNCONFIGURED}'.", file=sys.stderr)
        print(f"[reconcile-hooks] Installing hooks that need no upstream relationship. Sync and "
              f"shared-write hooks are DEFERRED until a role is set.", file=sys.stderr)
        print(f"[reconcile-hooks] To finish setup, add to {ident_path}:", file=sys.stderr)
        print(f'[reconcile-hooks]     "hook_profile": {{"role": "puller"}}', file=sys.stderr)
        role = UNCONFIGURED

    settings = load(settings_path)
    want = desired_set(registry, role, overrides)   # {(name,event,matcher): entry}
    have = current_registered(settings, managed_names(registry))  # {(name,event,matcher): [locations]}

    problems = validate_registry(registry)
    if problems:
        print(f"[reconcile-hooks] REFUSING — registry failed validation, settings.json untouched:",
              file=sys.stderr)
        for p in problems[:12]:
            print(f"    {p}", file=sys.stderr)
        return 2

    missing = sorted(set(want) - set(have))
    # A protected hook is NEVER removed, whatever the registry says. This is the backstop for the
    # unattended-pull threat: even a registry that reclassifies or drops the Sentinel gate cannot
    # cause it to be unregistered here.
    extra = sorted(k for k in (set(have) - set(want)) if k[0] not in PROTECTED_HOOKS)
    shielded = sorted(k[0] for k in (set(have) - set(want)) if k[0] in PROTECTED_HOOKS)
    if shielded:
        print(f"[reconcile-hooks] protected, not removed: {', '.join(sorted(set(shielded)))}")
    _fmt = lambda keys: ', '.join(f"{n}@{e}" for (n, e, m) in keys)

    core_label = os.path.basename(a.core.rstrip('/'))
    if not a.apply:
        print(f"[reconcile-hooks --check] core={core_label} role={role}")
        print(f"  managed hook registrations desired: {len(want)} · present: {len(have)}")
        if missing:
            print(f"  MISSING (should register): {_fmt(missing)}")
        if extra:
            print(f"  EXTRA (should remove): {_fmt(extra)}")
        if not missing and not extra:
            print("  ✓ in sync — no drift")
            return 0
        return 1

    # --apply
    for key in extra:
        remove_hook(settings, key)
    for key in missing:
        add_hook(settings, want[key])
    # order:"first" applies to hooks that are ALREADY registered too, not just newly-added ones.
    # Otherwise a mis-ordered auto-pull hook is neither missing nor extra, so the ordering
    # requirement silently never takes effect on the Cores that need it (Codex #7).
    for key, entry in want.items():
        if entry.get("order") != "first" or key in missing:
            continue
        remove_hook(settings, key)
        add_hook(settings, entry)

    # ATOMIC + FAIL-SAFE write (2026-07-27; factored into atomic_write_json 2026-08-31 so
    # --emit-template gets the same property without a second copy of it). This runs unattended
    # at the tail of every sync-from-baseline pull, so a crash mid-write would leave a Core with a
    # corrupt settings.json and NO hooks at all — including the security gates.
    err = atomic_write_json(settings_path, settings)
    if err:
        print(f"[reconcile-hooks --apply] ABORTED, settings.json untouched ({err})", file=sys.stderr)
        return 1

    # re-verify: reload and re-check must be clean
    settings2 = load(settings_path)
    have2 = current_registered(settings2, managed_names(registry))
    missing2 = sorted(set(want) - set(have2))
    extra2 = sorted(set(have2) - set(want))
    print(f"[reconcile-hooks --apply] core={core_label} role={role}: +{len(missing)} -{len(extra)}")
    if missing2 or extra2:
        print(f"  ✗ POST-APPLY DRIFT — missing={missing2} extra={extra2}", file=sys.stderr)
        return 1
    print("  ✓ applied and re-verified clean")
    return 0

if __name__ == '__main__':
    sys.exit(main())
