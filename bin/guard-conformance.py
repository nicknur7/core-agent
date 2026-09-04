#!/usr/bin/env python3
"""Cross-Core conformance runner for pretooluse-guard.sh.

WHY THIS EXISTS. On 2026-08-09 core-business and core-life reported DIFFERENT verdicts for
the same commands against the same guard file. Both had working harnesses; both were honest;
neither could reproduce the other. Two rounds of review were spent on hypotheses that were
retracted, because every case crossed the wire as PROSE — a description of a command, which
each Core then re-typed into its own harness through its own quoting. A shell string that
survives one Core's escaping and not the other's is a different string, and a different
string is a different test.

So the vectors are stored BASE64-ENCODED. Not for obscurity — so that the bytes one Core
tests are provably the bytes the other tests, with no shell, JSON, or message-transport layer
in between able to alter them silently. Any Core can run this and the results are comparable
by construction.

    python3 bin/guard-conformance.py            # run all, print a table
    python3 bin/guard-conformance.py --json     # machine-readable, for a bus reply
    python3 bin/guard-conformance.py --add "cmd" --expect block --id T99 --why "..."

A vector's `expect` is the CONTRACT, not a snapshot of current behaviour. When a Core finds a
form that walks past the guard, it adds the vector with expect=block FIRST — the suite then
fails until the guard is fixed, which is the point. A vector is never edited to match
behaviour; that would make the suite grade itself, which is the failure mode this whole
system was built to remove.
"""
import argparse
import base64
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GUARD = os.path.join(ROOT, ".claude", "hooks", "pretooluse-guard.sh")
VECTORS = os.path.join(HERE, "guard-conformance.json")


def load():
    with open(VECTORS) as f:
        return json.load(f)


def run_one(cmd, tool="Bash", tool_input=None):
    payload = {"tool_name": tool, "tool_input": tool_input or {"command": cmd}}
    try:
        r = subprocess.run(["bash", GUARD], input=json.dumps(payload),
                           capture_output=True, text=True, cwd=ROOT, timeout=30)
        return r.returncode
    except subprocess.TimeoutExpired:
        return -1


def verdict(code):
    return "block" if code == 2 else "pass"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--add")
    ap.add_argument("--expect", choices=["block", "pass"])
    ap.add_argument("--id")
    ap.add_argument("--why", default="")
    a = ap.parse_args()

    data = load()

    if a.add:
        if not (a.expect and a.id):
            sys.exit("--add requires --expect and --id")
        if any(v["id"] == a.id for v in data["vectors"]):
            sys.exit("id %s already exists" % a.id)
        data["vectors"].append({
            "id": a.id,
            "b64": base64.b64encode(a.add.encode()).decode(),
            "expect": a.expect,
            "why": a.why,
        })
        with open(VECTORS, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        print("added %s (expect=%s)" % (a.id, a.expect))
        return

    results = []
    for v in data["vectors"]:
        cmd = base64.b64decode(v["b64"]).decode()
        code = run_one(cmd, v.get("tool", "Bash"), v.get("tool_input"))
        got = verdict(code)
        gap = v.get("known_gap")
        r = {"id": v["id"], "expect": v["expect"], "got": got, "exit": code,
             "ok": got == v["expect"], "why": v.get("why", ""), "known_gap": gap}
        # A KNOWN GAP is a vector we cannot close with this mechanism and have said so. It is
        # neither a pass nor a regression, and collapsing it into either is the lie. Reported
        # as its own state so the suite stays green for regressions while the limit stays
        # undeniable — and if one ever starts blocking, that is news in the other direction.
        if gap:
            r["state"] = "GAP CLOSED" if r["ok"] else "known gap"
        else:
            r["state"] = "ok" if r["ok"] else "FAIL"
        results.append(r)

    failed = [r for r in results if r["state"] == "FAIL"]
    gaps = [r for r in results if r["state"] == "known gap"]
    closed = [r for r in results if r["state"] == "GAP CLOSED"]

    if a.json:
        print(json.dumps({
            "core": os.path.basename(ROOT),
            "guard_sha": subprocess.run(["shasum", "-a", "256", GUARD], capture_output=True,
                                        text=True).stdout.split()[0][:12],
            "passed": len(results) - len(failed) - len(gaps) - len(closed),
            "failed": len(failed),
            "known_gaps": len(gaps),
            "gaps_closed": len(closed),
            "results": results,
        }, indent=2))
        return 1 if failed else 0

    sha = subprocess.run(["shasum", "-a", "256", GUARD], capture_output=True,
                         text=True).stdout.split()[0][:12]
    print("\n  GUARD CONFORMANCE — %s, guard sha %s\n" % (os.path.basename(ROOT), sha))
    for r in results:
        mark = {"ok": "  ok  ", "FAIL": "  FAIL", "known gap": "  GAP ",
                "GAP CLOSED": "  NEWS!"}[r["state"]]
        print("%s %-6s exp=%-5s got=%-5s %s" % (mark, r["id"], r["expect"], r["got"], r["why"]))
    print("\n  %d passed, %d failed, %d known gaps"
          % (len(results) - len(failed) - len(gaps) - len(closed), len(failed), len(gaps)))
    if gaps:
        print("\n  KNOWN GAPS — open by design of the mechanism, not by oversight:")
        for r in gaps:
            print("    %-6s %s" % (r["id"], r["known_gap"]))
    if closed:
        print("\n  A KNOWN GAP NOW BLOCKS — verify it is real, then drop the known_gap marker:")
        for r in closed:
            print("    %-6s %s" % (r["id"], r["why"]))
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
