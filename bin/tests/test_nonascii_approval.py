"""A command containing a non-ASCII character must be approvable end to end.

core-business (#954): the guard canonicalised tool_input with json.dumps' default
ensure_ascii=True, so an em-dash became the six characters \\u2014 — while the REVIEWER, a language
model asked to echo the command on its REVIEWED line, emits the literal em-dash. The two never
matched, and the failure surfaced as "no fresh APPROVE receipt", which reads as SENTINEL DECLINED
rather than THE STRINGS DID NOT MATCH.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

# DERIVED, never hardcoded — test_no_cross_core_paths caught this file on its first run, as it
# caught two of my fixtures earlier today. A test naming one Core is indistinguishable from a
# hardcoded dependency on it, and this file ships to every Core.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _core import core_root  # noqa: E402
REPO = str(core_root())
CMD = 'echo "UNCONFIRMED — Sam gave a start"'      # em-dash, exactly business's shape

# core-business measured NINE ordinary payload shapes and found only plain ASCII approvable (#955).
# Three are worth naming because they are not corner cases:
#
#   CURLY APOSTROPHE     what macOS autocorrect makes of "Sam's". A human typing a normal note in
#                        a normal editor produced an unapprovable action with no way to know.
#   NON-BREAKING SPACE   INVISIBLE. The operator sees a refusal, inspects the payload, sees nothing
#                        wrong, and is told Sentinel objected. Nothing on screen could explain it.
#   ACCENTED NAME        an action naming Zoë, José or Müller could not be approved. That is not an
#                        edge case, it is a category of person, and the system refused to act on
#                        their behalf while reporting it as a security review outcome.
#
# Checked at the STRING layer, which is where the defect lives — business proved the end-to-end
# consequence once, on the em-dash, with three real Sentinel runs (refuse, refuse, mint-on-ASCII)
# and deliberately did not spend eight more subagent reviews to confirm what the encoder determines.
SHAPES = [
    ("plain ASCII",          'echo "UNCONFIRMED - Sam gave a start"'),
    ("em-dash",              'echo "UNCONFIRMED — Sam gave a start"'),
    ("en-dash in a range",   'echo "window 9–11am"'),
    ("curly apostrophe",     'echo "Sam\u2019s slot"'),
    ("curly quotes",         'echo "he said \u201cyes\u201d to it"'),
    ("ellipsis",             'echo "pending…"'),
    ("non-breaking space",   'echo "9\u00a0am start"'),
    ("accented name",        'echo "call Zoë and José"'),
    ("emoji",                'echo "done ✅"'),
]


def main():
    ok = True
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, ".claude", "hooks", "lib"), exist_ok=True)
        os.makedirs(os.path.join(td, ".claude", "state"), exist_ok=True)
        for f in ("sentinel-approve.sh", "sentinel-receipt.sh"):
            subprocess.run(["cp", os.path.join(REPO, ".claude/hooks", f),
                            os.path.join(td, ".claude/hooks", f)], check=True)
        subprocess.run(["cp", os.path.join(REPO, ".claude/hooks/lib/hookinvoke.sh"),
                        os.path.join(td, ".claude/hooks/lib/")], check=False)
        subprocess.run(["cp", os.path.join(REPO, ".claude/identity.json"),
                        os.path.join(td, ".claude/")], check=False)

        # A reviewer that echoes the command VERBATIM, as a language model does.
        payload = json.dumps({"agent_type": "sentinel", "session_id": "t",
                              "last_assistant_message":
                                  "REVIEWED: %s\nLooks fine.\nVERDICT: APPROVE" % CMD})
        r = subprocess.run(["bash", os.path.join(td, ".claude/hooks/sentinel-receipt.sh")],
                           input=payload, text=True, capture_output=True,
                           env=dict(os.environ, CORE_INSTANCE=td))
        m = subprocess.run(["bash", os.path.join(td, ".claude/hooks/sentinel-approve.sh"), CMD],
                           capture_output=True, text=True, cwd=td,
                           env=dict(os.environ, CORE_INSTANCE=td))
        minted = m.returncode == 0
        print("  em-dash command, reviewer echoed it verbatim -> %s"
              % ("MINTED" if minted else "REFUSED"))
        if not minted:
            print("     %s" % (m.stdout + m.stderr).strip().splitlines()[0][:90])
        ok &= minted

        # CONTROL: a review of a DIFFERENT command must still refuse, or the match is vacuous.
        r2 = subprocess.run(["bash", os.path.join(td, ".claude/hooks/sentinel-receipt.sh")],
                            input=json.dumps({"agent_type": "sentinel", "session_id": "t",
                                              "last_assistant_message":
                                                  "REVIEWED: echo something else\nVERDICT: APPROVE"}),
                            text=True, capture_output=True,
                            env=dict(os.environ, CORE_INSTANCE=td))
        m2 = subprocess.run(["bash", os.path.join(td, ".claude/hooks/sentinel-approve.sh"), CMD],
                            capture_output=True, text=True, cwd=td,
                            env=dict(os.environ, CORE_INSTANCE=td))
        refused = m2.returncode != 0
        print("  a review of a DIFFERENT command -> %s" % ("REFUSED" if refused else "MINTED — BAD"))
        ok &= refused

    # EVERY SHAPE, at the layer the defect lives in: the guard's canonicalisation must produce the
    # SAME bytes a reviewer echoing the command produces, or the two can never match.
    print("\n  canonicalisation vs what a reviewer echoes:")
    # THE PROPERTY IS ABOUT THE NON-ASCII CHARACTERS, not the whole command — JSON escapes the inner
    # quotes either way, so substring-matching the raw command fails even for plain ASCII. My first
    # version did exactly that and reported all nine broken, which is the check being wrong rather
    # than the fix; it failed loudly, which is the right direction for a wrong check to fail.
    bad = []
    for label, cmd in SHAPES:
        guard = json.dumps({"command": cmd}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
        escaped = json.dumps({"command": cmd}, sort_keys=True, separators=(",", ":"))
        nonascii = sorted({ch for ch in cmd if ord(ch) > 127})
        survives = all(ch in guard for ch in nonascii)
        was_escaped = bool(nonascii) and any(ch not in escaped for ch in nonascii)
        print("    %-20s %-3d non-ASCII  survives=%-5s  pre-fix escaped=%s"
              % (label, len(nonascii), survives, was_escaped))
        if not survives:
            bad.append(label)
        # A shape with non-ASCII MUST have been broken before, or this row proves nothing.
        if nonascii and not was_escaped:
            bad.append(label + " (control: was not broken pre-fix)")
    print("  %s" % ("  all nine shapes survive canonicalisation"
                    if not bad else "  STILL BROKEN: %s" % bad))
    ok &= not bad

    print("\n%s" % ("BOTH DIRECTIONS PASS" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
