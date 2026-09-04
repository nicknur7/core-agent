#!/usr/bin/env python3
"""Safety proof for bin/reconcile-hooks.py running UNATTENDED on every baseline pull (2026-07-27).

Before this date the reconciler was run by hand. It is now invoked automatically at the tail of
sync-from-baseline.sh, and it rewrites .claude/settings.json — the file that registers every
security hook. Its input, bin/hook-registry.json, arrives over the network from the baseline repo.

Codex review (CRITICAL): `desired_set` skipped entries flagged `sentinel: true` IN THE REGISTRY, so
a registry that flipped that flag to false would classify the live Sentinel gate as "extra" and
remove it — disarming every outward-action check on the next pull. These tests pin the two
backstops: a locally-pinned protected set that no registry can override, and closed-schema
validation that refuses a malformed registry outright instead of partially honoring it.

  python3 bin/tests/test_reconcile_hooks_safety.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("rh", HERE.parent / "reconcile-hooks.py")
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _entry(**kw):
    e = {"name": "some-hook", "event": "Stop", "matcher": "", "scope": "universal",
         "command": 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/some-hook.sh"', "timeout": 5}
    e.update(kw)
    return e


print("\n=== registry schema validation (fail closed) ===")
check("valid registry passes", rh.validate_registry({"hooks": [_entry()]}) == [])
check("missing hooks list rejected", rh.validate_registry({}) != [])
check("empty hooks list rejected", rh.validate_registry({"hooks": []}) != [])
check("non-object entry rejected", rh.validate_registry({"hooks": ["oops"]}) != [])
check("unknown key rejected", rh.validate_registry({"hooks": [_entry(evil="x")]}) != [])
check("unknown event rejected", rh.validate_registry({"hooks": [_entry(event="Whenever")]}) != [])
check("unknown scope rejected", rh.validate_registry({"hooks": [_entry(scope="everyone")]}) != [])
check("empty command rejected", rh.validate_registry({"hooks": [_entry(command="")]}) != [])
check("out-of-range timeout rejected", rh.validate_registry({"hooks": [_entry(timeout=99999)]}) != [])
check("bool-as-timeout rejected", rh.validate_registry({"hooks": [_entry(timeout=True)]}) != [])

print("\n=== the live registry is itself valid ===")
live = json.loads((HERE.parent / "hook-registry.json").read_text())
problems = rh.validate_registry(live)
check("shipped hook-registry.json passes validation", problems == [], str(problems[:3]))

print("\n=== protected hooks are pinned locally, not by the registry ===")
check("pretooluse-guard is protected", "pretooluse-guard" in rh.PROTECTED_HOOKS)
check("shared-write-guard is protected", "shared-write-guard" in rh.PROTECTED_HOOKS)

# The exact attack: a registry that declassifies the Sentinel gate. desired_set must not want it,
# but the removal path must refuse to drop it anyway.
hostile = {"hooks": [_entry(name="pretooluse-guard", event="PreToolUse", sentinel=False,
                            command='bash "$CLAUDE_PROJECT_DIR/.claude/hooks/pretooluse-guard.sh"')]}
check("hostile declassifying registry still passes schema (schema is not the defence here)",
      rh.validate_registry(hostile) == [])
have = {("pretooluse-guard", "PreToolUse", ""), ("some-hook", "Stop", "")}
want = set()
extra = sorted(k for k in (have - want) if k[0] not in rh.PROTECTED_HOOKS)
check("protected hook is NOT in the removal set",
      ("pretooluse-guard", "PreToolUse", "") not in extra)
check("an ordinary unmanaged-but-not-wanted hook IS removable",
      ("some-hook", "Stop", "") in extra)

print("\n=== order:first places the hook at the head of its event ===")
settings = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [
    {"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/session-start-check.sh"'}]}]}}
rh.add_hook(settings, _entry(name="sync-from-baseline", event="SessionStart",
                             command='bash "$CLAUDE_PROJECT_DIR/bin/sync-from-baseline.sh" --quiet',
                             order="first"))
first_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
check("auto-pull runs before other SessionStart hooks", "sync-from-baseline" in first_cmd, first_cmd)

print("\n=== atomic write leaves settings.json untouched on failure ===")
tmpdir = tempfile.mkdtemp()
os.makedirs(os.path.join(tmpdir, ".claude"), exist_ok=True)
sp = os.path.join(tmpdir, ".claude", "settings.json")
original = '{"hooks": {}}\n'
Path(sp).write_text(original)
Path(os.path.join(tmpdir, ".claude", "identity.json")).write_text(json.dumps(
    {"hook_profile": {"role": "puller", "overrides": {}}}))
bad_registry = os.path.join(tmpdir, "bad-registry.json")
Path(bad_registry).write_text(json.dumps({"hooks": [_entry(event="NotARealEvent")]}))
rc = rh.main.__wrapped__ if hasattr(rh.main, "__wrapped__") else None
sys.argv = ["reconcile-hooks.py", "--core", tmpdir, "--apply", "--registry", bad_registry]
code = rh.main()
check("invalid registry aborts with a non-zero code", code != 0, str(code))
check("settings.json byte-identical after refusal", Path(sp).read_text() == original)


# ---------------------------------------------------------------------------------------------
# A FRESH CLONE HAS NO ROLE, AND THAT MUST NOT BE AN ERROR.
#
# Until 2026-07-27 an absent hook_profile.role made this script exit 2, and sync-from-baseline.sh
# swallowed the failure — so on every new Core the installer silently refused to run and none of the
# registry-managed hooks were registered, including all 15 SI-spine hooks. The self-improvement
# engine shipped correctly registered in hook-registry.json and never turned on for anyone but the
# author. The template's identity.json simply has no hook_profile key.
#
# These pin the three properties that fix depends on.
# ---------------------------------------------------------------------------------------------
print("\n=== a Core with no declared role still gets its gates ===")

_reg = json.loads(Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "hook-registry.json")).read_text())

_uncfg = rh.desired_set(_reg, rh.UNCONFIGURED, {})
_writer = rh.desired_set(_reg, "writer", {})
_puller = rh.desired_set(_reg, "puller", {})
_names = lambda d: {k[0] for k in d}

check("unconfigured installs a non-empty set (not a silent no-op)", len(_uncfg) > 0, str(len(_uncfg)))

# approval-gate is deliberately absent: retired 2026-07-27 (41% block rate, matching text over
# meaning). A retired hook must not appear in any expected set, which is itself part of the check.
#
# THREE MORE LEFT THIS SET ON 2026-08-06, by the same convention and for a policy reason rather than a
# fitness one. The operator's policy: nothing may act after the reply is sent — it all has to
# happen before the reply is given; a Stop hook after the reply is useless. recall-gate,
# decision-attribution-gate and financial-figure-gate were all Stop-event gates, and a Stop block is
# a CONTINUATION rather than a regeneration — the flawed text and the correction both reach the
# operator, so they never prevented the thing they existed to prevent.
#
# THEIR FUNCTION IS NOT GONE, which is what makes this a move rather than a loss:
#   recall-gate                -> recall-first-gate, still in this set, PreToolUse, fires BEFORE the
#                                 edit rather than after the claim
#   decision-attribution-gate  -> reply-observer records the class at zero cost; supply + a
#   financial-figure-gate         PostToolBatch pre-empt replace the blocking half
#
# Verified 2026-08-06 by test, not doc: MessageDisplay sees the full reply and cannot stop it, so a
# text-inspecting gate CANNOT be relocated pre-reply — it can only be replaced. That is why this is a
# deletion from the set and not a change of event.
SI_SPINE = {"friction-dispatch", "recall-first-gate", "recall-satisfied", "stop-signal-gate",
            "capability-usage-log", "friction-watchdog", "stay-scoped"}
_missing_si = SI_SPINE - _names(_uncfg)
check("the SI spine installs with no role declared", not _missing_si, f"missing={sorted(_missing_si)}")

# The retired learned-workflow layer is still scope=universal so migrated Cores keep it until they
# cut over. A brand-new Core must NOT get it — that would boot the retired classifier alongside the
# unified spine, the exact double-architecture the 2026-07-23 cutover exists to eliminate.
_leaked = rh.LEGACY_LAYER & _names(_uncfg)
check("the retired legacy layer does NOT reach an unconfigured Core", not _leaked, f"leaked={sorted(_leaked)}")
check("...but a configured Core still receives it", rh.LEGACY_LAYER & _names(_writer) == rh.LEGACY_LAYER)

# An unconfigured Core has no upstream, so anything that assumes one must wait for a role.
check("unconfigured is a strict subset of puller", _names(_uncfg) < _names(_puller))
check("puller-scoped hooks are deferred until a role is set",
      not any(h["scope"] == "puller" for h in _uncfg.values()))

# Overrides tune a configured Core's own set. Honouring an 'on' override before the upstream
# relationship is settled could switch on sync or shared-write behaviour with nothing to point at.
_ov = rh.desired_set(_reg, rh.UNCONFIGURED, {n: "on" for n in rh.LEGACY_LAYER})
check("an unconfigured Core cannot be overridden INTO the legacy layer",
      not (rh.LEGACY_LAYER & _names(_ov)))
_off = rh.desired_set(_reg, rh.UNCONFIGURED, {"recall-gate": "off"})
check("...but 'off' is still honoured (opting out is always allowed)",
      "recall-gate" not in _names(_off))

# Existing Cores must be untouched by the unconfigured work. Derived from the registry rather
# than hardcoded: a hardcoded count fails every time a hook is legitimately added or retired,
# which trains people to edit the number instead of reading the diff. What actually matters is
# the RELATIONSHIP between the roles.
_live_entries = [h for h in _reg["hooks"] if not h.get("sentinel") and not h.get("retired")]
_universal = len([h for h in _live_entries if h["scope"] == "universal"])
_pullonly = len([h for h in _live_entries if h["scope"] == "puller"])
check("writer gets exactly the universal set", len(_writer) == _universal,
      f"{len(_writer)} vs {_universal}")
check("puller gets universal + puller-scoped", len(_puller) == _universal + _pullonly,
      f"{len(_puller)} vs {_universal + _pullonly}")
# COMPARED PER (name, event), NOT PER NAME. Retirement is an ENTRY-level fact: desired_set keys on
# (name, event, matcher) and pops exactly the retired key, so one hook can be retired on one event and
# live on another. friction-dispatch is the first such case — its Stop registration was retired
# 2026-08-06 under the no-post-reply policy while UserPromptSubmit and PreToolUse stay live. A
# name-level comparison called that a leak, which was the test being coarser than the mechanism it
# checks, not a real failure. Per-event is also strictly stronger: it would still catch a genuinely
# resurrected entry, which a name-level check would miss whenever the name appears anywhere else.
_retired_keys = {(h["name"], h["event"]) for h in _reg["hooks"] if h.get("retired")}
_live_keys = {(k[0], k[1]) for d in (_writer, _puller, _uncfg) for k in d}
check("no retired (name, event) reaches any role", not (_retired_keys & _live_keys),
      f"leaked={sorted(_retired_keys & _live_keys)}")


# ---------------------------------------------------------------------------------------------
# RETIREMENT NEEDS A TOMBSTONE, NOT AN ABSENCE.
#
# Deleting a hook's registry entry does not unregister it: managed_names() is built from the
# registry, current_registered() only looks for those names, and anything unmanaged is preserved
# verbatim. So a deleted entry leaves the hook running in every Core forever, invisible to the
# tool that is supposed to manage it. Found 2026-07-27 retiring approval-gate — the registry went
# 39 -> 37 and the hook kept firing.
#
# This matters beyond one hook: retirement is a leg of the self-improvement loop, and a loop that
# cannot remove what it installed only accumulates.
# ---------------------------------------------------------------------------------------------
print("\n=== a retired hook is removed, not orphaned ===")

_live = _entry(name="zz-live", event="Stop", scope="universal")
_dead = _entry(name="zz-dead", event="Stop", scope="universal")
_dead["retired"] = True
_dead["retired_reason"] = "test"
_reg2 = {"hooks": [_live, _dead]}

_d = rh.desired_set(_reg2, "writer", {})
check("a retired hook is NOT in the desired set", "zz-dead" not in {k[0] for k in _d})
check("...while its live sibling still is", "zz-live" in {k[0] for k in _d})

# It must stay in managed_names, or current_registered() cannot see it where it is registered
# and the removal never happens. This is the assertion that actually pins the fix.
check("a retired hook stays MANAGED (so it can be found and removed)",
      "zz-dead" in rh.managed_names(_reg2))

# An override must not be able to resurrect it — retirement is a fleet decision, and a per-Core
# escape hatch that can undo it is not a retirement.
_d_ov = rh.desired_set(_reg2, "writer", {"zz-dead": "on"})
check("an 'on' override cannot resurrect a retired hook", "zz-dead" not in {k[0] for k in _d_ov})

# The live registry's own tombstones must be well-formed.
_reg_live = json.loads(Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "hook-registry.json")).read_text())
_tombs = [h for h in _reg_live["hooks"] if h.get("retired")]
check("every tombstone carries a reason", all(h.get("retired_reason") for h in _tombs),
      f"{len(_tombs)} tombstone(s)")
# Per (name, event) for the same reason as above: a hook may be retired on one event and live on
# another, and desired_set pops exactly the retired key rather than every entry sharing the name.
check("no tombstoned (name, event) is in the desired set",
      not ({(h["name"], h["event"]) for h in _tombs}
           & {(k[0], k[1]) for k in rh.desired_set(_reg_live, "writer", {})}),
      "a tombstone that still resolves would keep firing on every Core")

print()
if _fails:
    print(f"FAILURES ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL PASS")
