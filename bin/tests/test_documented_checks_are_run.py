#!/usr/bin/env python3
"""A tool must not advertise a detector it does not run, or quote a number it does not produce.

WHY THIS EXISTS (2026-08-12). Three defects found by core-finance dosing shared `bin/`, all one
shape: a CLAIM in prose that the CODE beneath it does not honour. Same shape as the fitness floor
found the same day (guard in the rationale, decision on the verdict) and the three agent specs
("it has no side effects" while holding Bash).

  a. bin/verify-brain-synced.py — header: "Checks (ALL must pass — fail-closed)" then numbered
     extraction / capture / embed / status. main() called three. `check_capture` did not exist; the
     word "capture" appeared once in 209 lines, in the docstring.

     Not an interchangeable omission. Checks 1/3/4 all key on ROWS — pending evidence, NULL
     embeddings, a status verdict. A transcript never captured has no row ANYWHERE, so the one
     condition check 2 exists to catch is the one condition the others structurally cannot see.
     Finance measured the gate MINTING at rc=0 with an uncaptured file present, and
     session-lifecycle.sh:389 reads that marker as proof capture+embed ran, standing the nightly
     down — reproducing the 2026-07-24 failure the file was written to make impossible.

  b. bin/tier-b-power.py — header said power "reaches ~86% only at 20 trials" while naming q=0.30.
     86% is the q=0.20 figure. Its own table twelve lines below printed 98.5%. Off by 12.7 points,
     in the direction that made a cancelled experiment look weaker than it was.

  c. bin/state-feeder.py — `_status_for` rejected only `value is None`, so an empty marker file  # privacy-ok: generic engineering vocabulary
     emitted value='' status='fresh': an uncollected observation reported with full confidence,
     one type away from the fake zero its own MISSING != ZERO header abolishes. And `hooks()`
     counted FILES while its comment said "registered hook scripts" — 57 counted, 47 real, because
     a hook is commonly a .sh wrapper plus a .py implementation of the same name.

WHY IT READS THE DOCSTRING AT RUNTIME. Assertion (a) parses the check names out of the header and
requires each to be called in main(). A grep for "capture" passes forever on the docstring line —
which is exactly why a grep could not have found this, and why finance's probe did. If someone
edits the header, this test re-reads the new promise. The promise is the input, not a constant.
"""
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "bin"

failures: list[str] = []
passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passes if ok else failures).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n          {detail}"))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_gate_runs_what_it_documents() -> None:
    src_path = BIN / "verify-brain-synced.py"
    if not src_path.is_file():
        check("verify-brain-synced.py present", False, f"{src_path} missing")
        return
    src = src_path.read_text()

    doc = re.search(r'"""(.*?)"""', src, re.S)
    block = re.search(r"Checks \(ALL must pass.*?\n((?:\s+\d+\.\s+\w+.*\n)+)",
                      doc.group(1) if doc else "", re.S)
    check("the gate's documented check list is parseable", block is not None,
          "the header no longer lists numbered checks — if the format changed, update this parser "
          "rather than dropping the assertion; the promise is what is being tested")
    if not block:
        return

    promised = re.findall(r"^\s+\d+\.\s+(\w+)", block.group(1), re.M)
    check(f"header promises a non-empty check list (got {promised})", bool(promised))

    # COMMENTS STRIPPED BEFORE SCANNING. The first version of this parser matched
    # `check_capture()` inside a `# DOSE` comment, so commenting the call OUT of main() still
    # passed — the dose could not fail, which is the same defect this file exists to catch, and the
    # fourth time today a textual assertion has matched prose instead of code. Verified by dosing:
    # with this stripping in place, commenting the call out now fails the run.
    def _decomment(text: str) -> str:
        return "\n".join(re.sub(r"(?<!['\"])#.*$", "", ln) for ln in text.splitlines())

    body = re.search(r"\ndef main\(.*?\n(.*?)(?=\nif __name__|\Z)", src, re.S)
    called = set(re.findall(r"\b(check_\w+)\s*\(", _decomment(body.group(1) if body else "")))

    # THE FUNCTION NAME NEED NOT EQUAL THE DOCUMENTED LABEL — check_db implements "embed". So the
    # mapping runs label -> the function whose body reports it -> is that function called in main().
    # Asserting merely that SOME check() call reports the label would pass on a function that
    # exists and is never invoked, which is precisely the state this gate shipped in.
    funcs = dict(re.findall(r"\ndef (check_\w+)\(.*?\n(.*?)(?=\ndef |\Z)", src, re.S))
    for label in promised:
        owner = next((fn for fn, fbody in funcs.items()
                      if re.search(rf'\bcheck\(\s*"{re.escape(label)}"', fbody)), None)
        check(f"documented check '{label}' is implemented AND called by main()",
              owner is not None and owner in called,
              f"header promises '{label}'; implementing function = {owner!r}; main() calls "
              f"{sorted(called)}. A fail-closed gate that advertises a detector it does not run "
              f"mints markers on the very condition it claims to catch — measured: it minted at "
              f"rc=0 with an uncaptured transcript present.")

    check("main() calls at least as many checks as the header promises",
          len(called) >= len(promised),
          f"main() calls {sorted(called)} ({len(called)}) for {len(promised)} promised")


def check_power_header_matches_output() -> None:
    path = BIN / "tier-b-power.py"
    if not path.is_file():
        check("tier-b-power.py present", False, f"{path} missing")
        return
    src = path.read_text()
    mod = _load(path, "_tbp")

    # The header quotes a power figure at a stated q. Recompute the floor at that q and require the
    # quoted number to be consistent with THIS module, not with a different column of its own table.
    m = re.search(r"power is ([\d.]+)% at 10 trials and ([\d.]+)% at 20, at the q=([\d.]+)", src)
    check("the header states its power figures and the q they belong to", m is not None,
          "cannot locate the quoted figures; the sentence was reworded — re-derive and re-assert "
          "rather than deleting, since a misquote here is invisible to every other check")
    if not m:
        return
    at20, q = float(m.group(2)), float(m.group(3))
    floor20 = 100 * mod.p_reaches(20, 2, q) ** 2

    check(f"the header's {at20}% at 20 trials matches this module at q={q} (floor {floor20:.1f}%)",
          abs(at20 - floor20) < 1.0,
          f"header says {at20}% at q={q}; the module computes {floor20:.1f}%. The previous version "
          f"quoted 86% (the q=0.20 figure) under a sentence naming q=0.30, whose real value is "
          f"98.5% — a 12.7-point misquote of the table printed directly beneath it.")

    # PIN THE MISQUOTED NUMBER TO THE COLUMN IT ACTUALLY CAME FROM. 86% was never wrong — it was
    # right about q=0.20 and printed under a sentence naming q=0.30. Asserting that keeps the
    # diagnosis testable: if this stops holding, the old header would no longer be explained by
    # a column mix-up and the correction would need re-deriving.
    #
    # This replaced a tautology — `max(abs(x - x)) < 1e-9`, which is 0 for any x and could never
    # fail. Written in this same file, in the pass that exists to catch claims code does not
    # honour. Left recorded rather than quietly swapped: the failure mode does not announce itself,
    # and a green tick from an assertion with no content is worth less than no assertion at all.
    floor_20 = 100 * mod.p_reaches(20, 2, 0.20) ** 2
    check(f"the old header's ~86% is exactly the q=0.20 floor ({floor_20:.1f}%), not q=0.30",
          abs(floor_20 - 86.6) < 0.5,
          f"got {floor_20:.1f}%. The documented cause of the 12.7-point misquote was that 86% "
          f"belongs to the q=0.20 column; if that no longer holds, re-derive the correction.")
    check("the floor rises with q (a better observe rate cannot decide less)",
          all(mod.p_reaches(20, 2, a) <= mod.p_reaches(20, 2, b)
              for a, b in zip(mod.QS, mod.QS[1:])),
          "monotonicity broken — p_reaches is not behaving as a cumulative binomial tail")


def check_state_feeder_missing_is_not_fresh() -> None:
    path = BIN / "state-feeder.py"
    if not path.is_file():
        check("state-feeder.py present", False, f"{path} missing")
        return
    import datetime as dt
    mod = _load(path, "_sf")
    now = dt.datetime.now(dt.timezone.utc)

    check("an EMPTY string is 'unknown', not 'fresh'",
          mod._status_for(now, "sync", now, "") == "unknown",
          "an empty or truncated marker file is an uncollected observation; reporting it 'fresh' "
          "is the fake zero this file's MISSING != ZERO header abolishes, one type away")
    check("whitespace-only is 'unknown'",
          mod._status_for(now, "sync", now, "  \n") == "unknown")
    # The other direction, which is the easy way to break this fix.
    check("a ZERO count is still 'fresh' — 0 is a real observation",
          mod._status_for(now, "sync", now, 0) == "fresh",
          "`not value` would have collapsed a legitimate zero into unknown, reintroducing the same "
          "defect from the opposite side")
    check("a real value is still 'fresh'",
          mod._status_for(now, "sync", now, "2026-08-12") == "fresh")
    check("None is still 'unknown'",
          mod._status_for(now, "sync", now, None) == "unknown")

    src = path.read_text()
    check("the hook count counts NAMES, not files",
          "p.stem for p in d.iterdir()" in src,
          "counting files double-counts every .sh wrapper over its .py implementation — measured "
          "57 files against 47 distinct names on both life and finance (10 pairs), identical "
          "because .claude/hooks is baseline-shared")
    check("the stale 'registered hook scripts' label is gone",
          "count of registered hook scripts" not in src,
          "the comment claimed registration; the code reads a directory. Counting from "
          "settings.json is NOT the fix — a wrapper or router invokes several hooks, which is why "
          "that approach produced a false '26 unregistered' when finance tried it.")


def check_sync_doc_matches_manifest() -> None:
    """The /sync doc lists what syncs. bin/sync-manifest.json decides what syncs. Keep them equal.

    ADDED 2026-08-12. The doc listed `scheduling/{brain-pg,graphify-brain,brain-lint}/` and omitted
    claude-si, core-si, system-health, eval, docs and template. Reading it, I concluded the SI loop
    was per-Core and told a peer their seat would need the fitness fix hand-applied. It does not —
    those directories propagate on the next pull.

    A hand-maintained copy of a machine-readable manifest is the redundancy Nick's standing
    directive names, and prose cannot compute. Since the copy is useful where it sits, this makes
    it SELF-CHECKING instead: the copy may exist, but it may not disagree with the authority.
    """
    import json
    doc = REPO / ".claude" / "commands" / "sync.md"
    man = REPO / "bin" / "sync-manifest.json"
    if not (doc.is_file() and man.is_file()):
        check("sync doc and manifest both present", False, f"{doc} / {man}")
        return
    dirs = json.loads(man.read_text())["shared"]["dirs"]

    # PARSE THE LIST, NOT THE PAGE. The first version searched the whole file and also accepted a
    # bare basename, so it matched "claude-si" inside the prose paragraph that EXPLAINS the
    # omission — the dose could not fail. Fifth time today an assertion matched prose instead of
    # the thing it names, and this one was written into the file whose subject is that exact
    # defect. Only backticked tokens in the "## What syncs" section count now, with brace groups
    # expanded the way the doc writes them.
    text = doc.read_text()
    section = re.search(r"^## What syncs$(.*?)(?=^## |\Z)", text, re.S | re.M)
    check("the /sync doc still has a 'What syncs' section to check", section is not None,
          "the section was renamed; re-point this parser rather than dropping the assertion")
    if not section:
        return

    # BRACE GROUPS CAN CARRY A SUFFIX. This matched `^(.*?)\{([^}]*)\}/?$` — a group that ENDS the
    # token, so `scheduling/{a,b}/` expanded and `.claude/commands/{a,b}.md` did NOT: the whole
    # token went in literally, and every shared COMMAND read as unlisted. The dir check never
    # noticed because no dir is written with a suffix; it surfaced the moment the FILE check was
    # added, since files are exactly where the suffix form is used.
    #
    # A parser that handles the shapes its current input happens to use is not a parser, and the
    # gap only appears when the input widens — which is the same reason the file check had to be
    # added at all.
    listed: set[str] = set()
    for tok in re.findall(r"`([^`]+)`", section.group(1)):
        tok = tok.strip()
        m = re.match(r"^(.*?)\{([^}]*)\}(.*)$", tok)
        if m:
            pre, parts, suf = m.group(1), m.group(2), m.group(3)
            listed.update(f"{pre}{p.strip()}{suf}".rstrip("/") for p in parts.split(","))
        else:
            listed.add(tok.rstrip("/"))

    missing = [d for d in dirs if d.rstrip("/") not in listed]
    check(f"every shared dir in the manifest is listed in the /sync doc ({len(dirs)} dirs)",
          not missing,
          f"the doc's list omits {missing}. A reader concludes those paths are per-Core and hand-"
          f"applies fixes that would have propagated on the next pull — which is exactly what "
          f"happened on 2026-08-12 with scheduling/claude-si.")

    # FILES TOO, and this half was missing until 2026-08-13. The dir check above was added as the
    # fix for the 08-12 drift and covered only `.shared.dirs`, so NINE shared FILES — both
    # `.claude/skills/*/SKILL.md`, `.claude/CLAUDE.base.md`, Makefile, README.md, LICENSE,
    # MULTI-CORE.md, .mcp.json.template, pyproject.toml — plus three shared COMMANDS (core-si,
    # deep-plan, retire-legacy) sat unlisted and unguarded. A guard that covers one key of a
    # two-key manifest reports the document as verified while half of it drifts.
    #
    # Found while auditing the doc by hand, which is the tell: if a hand audit finds something the
    # guard cannot, the guard's scope is the defect, not the document.
    files = json.loads(man.read_text())["shared"].get("files", [])
    fmissing = [f for f in files if f.rstrip("/") not in listed]
    check(f"every shared FILE in the manifest is listed in the /sync doc ({len(files)} files)",
          not fmissing,
          f"the doc's list omits {fmissing}. These propagate on every pull exactly like the dirs, "
          f"so a reader editing one of them on a puller loses the edit to the next sync and has no "
          f"way to learn that from this document.")


def check_close_pipeline_names_every_step() -> None:
    """core-si-close's numbered Pipeline must name every step main() actually runs.

    WHY (2026-08-13, core-finance DOSE 40). The docstring enumerated FIVE steps; main() called a
    sixth, `run_friction_engine` at :140 — which mines, routes, test-gates and INSTALLS
    inject-contracts (budget-capped ≤5) at every close unless CORE_FRICTION_DISABLE=1.

    The safety prose was attached to the SMALLER action. Step 3's auto-apply is fenced meticulously
    ("ALL of ... Never anything else") and has exactly ONE registered applier; the undocumented step
    installs up to five artifacts. Anyone auditing "what does close change autonomously?" read the
    Pipeline, found a tightly-fenced one-applier path, and never learned the same script installs
    contracts.

    AST, not regex — finance found this with the technique and then committed the very defect it
    guards against, using `re.search(r"eligible\\s*=\\s*(.+)")` which matched an f-string at :152
    instead of the real assignment at :488. A regex over source cannot tell a mentioned name from a
    called one. The parse tree can.
    """
    import ast
    src_path = REPO / "bin" / "core-si-close.py"
    if not src_path.is_file():
        check("core-si-close.py present", False, f"{src_path} missing")
        return
    tree = ast.parse(src_path.read_text())
    doc = ast.get_docstring(tree) or ""

    top = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    main_fn = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    check("core-si-close defines main()", main_fn is not None)
    if main_fn is None:
        return

    called = {n.func.id for n in ast.walk(main_fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in top}

    # ONLY STEPS THAT CHANGE SOMETHING MUST BE NAMED, not every helper.
    #
    # The first version required every main()-called function to appear literally in the docstring,
    # and failed on detect_items / load_autosafe / trust_lookup — which the Pipeline DOES describe,
    # in prose ("Run detect.sh --tsv", "read its approval streak"), just not by function name. That
    # is a normal and fine way to write a pipeline description, so the assertion was over-fitted to
    # the one case that prompted it and would have pressured someone into naming plumbing.
    #
    # The property that actually matters is narrower and is the finding's real content: a step that
    # INSTALLS or WRITES must be visible to someone auditing "what does this change autonomously?".
    # A read-only helper can stay described in prose. So each called function is classified by its
    # OWN docstring, and only the mutating ones are required to be named.
    defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    MUTATES = ("install", "write", "insert", "apply", "mint", "delete", "quarantine")

    def _claims(verb: str, text: str) -> bool:
        """True when the text ASSERTS the verb, not when it merely contains it.

        `trust_lookup`'s docstring reads "conservative: no auto-apply" — it says the function does
        NOT apply, and a bare substring match classified it as a mutator. Eleventh instance of
        use-vs-mention in three days, and the same rule already written for the BYPASSRLS check
        resolves it: a NEGATION is not a claim. Reused rather than re-invented, which is the point —
        one distinction applied everywhere beats a new special case each time.
        """
        for m in re.finditer(verb, text):
            before = text[:m.start()]
            if not re.search(r"\b(no|not|never|without|cannot|isn't)\b[^.]{0,20}$", before, re.I):
                return True
        return False

    mutators = set()
    for name in called:
        d = (ast.get_docstring(defs[name]) or "").lower() if name in defs else ""
        if any(_claims(v, d) for v in MUTATES):
            mutators.add(name)

    missing = sorted(fn for fn in mutators if fn not in doc)
    check(f"every MUTATING step main() calls is named in the Pipeline ({sorted(mutators)})",
          not missing,
          f"main() calls {missing}, whose own docstring says it changes state, and the Pipeline "
          f"never names it. That is how a step installing up to five artifacts at every close "
          f"stayed invisible while a one-applier path beside it was fenced meticulously.")

    check("the classifier found at least one mutating step (else this is vacuous)",
          bool(mutators),
          "no called function's docstring mentions installing or writing — either the pipeline "
          "genuinely mutates nothing, or the classifier stopped working and this assertion is "
          "measuring an empty set")

    # And the structural fence must remain structural — the header now credits `has_applier`
    # rather than the hand-maintained auto-safe.txt, so the AND must actually contain it.
    elig = [n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "eligible" for t in n.targets)]
    src_of = {ast.unparse(n.value) for n in elig}
    check("auto-apply eligibility is still a structural AND including has_applier",
          any("has_applier" in s and " and " in s for s in src_of),
          f"got {src_of or 'no eligible assignment'}. The docstring credits a one-entry applier "
          f"registry for the outward-key fence; if that term leaves the condition, the prose is "
          f"crediting a guard that no longer exists.")


def main() -> int:
    print("test_documented_checks_are_run")
    check_sync_doc_matches_manifest()
    check_close_pipeline_names_every_step()
    check_gate_runs_what_it_documents()
    check_power_header_matches_output()
    check_state_feeder_missing_is_not_fresh()
    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
