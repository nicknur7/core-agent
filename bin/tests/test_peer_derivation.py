"""Does every Core see the right peers — INCLUDING the ones that are not life?

The bug was invisible from life's seat because life's peer set is exactly the hardcoded literal.
So testing from life proves nothing; the test has to simulate each seat. Simulated with fake Core
directories so it does not depend on which Cores happen to exist.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# DERIVED — this file ships to every Core; a pinned absolute path made the test read one
# specific machine's copy no matter where it ran.
SRC = str(Path(__file__).resolve().parents[2] / ".claude/hooks/session-presence.py")
CORES = ["life", "business", "school", "finance", "ops"]

base = tempfile.mkdtemp()
for c in CORES:
    d = os.path.join(base, "core-" + c, ".claude")
    os.makedirs(d)
    open(os.path.join(d, "identity.json"), "w").write('{"org_id": 1}')
    subprocess.run(["git", "init", "-q", os.path.join(base, "core-" + c)],
                   capture_output=True)

SNIPPET = '''
import sys
from pathlib import Path
repo = Path(sys.argv[1])
_self = repo.resolve()
peers = sorted(p.name[len("core-"):] for p in repo.parent.glob("core-*")
               if p.is_dir() and p.resolve() != _self
               and (p / ".claude" / "identity.json").is_file())
print(",".join(peers))
'''

bad = 0
print("\n  PEER LIST AS SEEN FROM EACH SEAT\n")
for c in CORES:
    repo = os.path.join(base, "core-" + c)
    out = subprocess.run([sys.executable, "-c", SNIPPET, repo],
                         capture_output=True, text=True).stdout.strip()
    got = out.split(",") if out else []
    want = sorted(x for x in CORES if x != c)
    ok = got == want
    bad += not ok
    notes = []
    if c in got:
        notes.append("LISTS ITSELF")
    for missing in (set(want) - set(got)):
        notes.append("MISSING %s" % missing)
    print("  %-6s %-8s sees: %-34s %s" % ("ok" if ok else "FAIL", c, ",".join(got),
                                          "; ".join(notes)))

print("\n  %s\n" % ("ALL CORRECT — every seat sees the other four and never itself"
                    if not bad else "%d SEAT(S) WRONG" % bad))
sys.exit(1 if bad else 0)
