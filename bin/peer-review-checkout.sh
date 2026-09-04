#!/usr/bin/env bash
# Materialise another Core's commit as a real tree, so a peer reviews what would actually ship.
#
# WHY. Twice on 2026-08-09 a review reached a wrong conclusion because of how the code CROSSED
# THE GAP between two Cores, not because of the code:
#
#   1. Test commands crossed as PROSE. Each Core re-typed them through its own quoting, so the
#      two seats ran different strings while believing they ran the same one. Fixed by storing
#      conformance vectors as base64 bytes.
#   2. A commit crossed as HAND-COPIED FILES. core-business copied bin/enforcement-audit.py into
#      its own tree, hit an ImportError on a helper that lives in scheduling/brain-pg/_env.py,
#      and reported an undeclared dependency that would break every pulling Core. BOTH FILES WERE
#      IN THE SAME COMMIT. The dependency was declared; the review method could not see it,
#      because copying one file out of a commit cannot show you the commit.
#
# The lesson is the same both times: A REVIEW IS ONLY AS GOOD AS THE FIDELITY OF WHAT CROSSED.
# Prose loses quoting, a copied file loses its siblings. A git worktree loses nothing.
#
#   bash bin/peer-review-checkout.sh                    # this Core's HEAD
#   bash bin/peer-review-checkout.sh <sha>              # a specific commit
#   bash bin/peer-review-checkout.sh <sha> --from life  # another Core's commit
#
# Prints the worktree path. Read-only by convention: run suites there, do not edit. Remove with
# `git -C <core> worktree remove <path>`, or leave it — they are cheap and cleaned by --prune.
set -euo pipefail

SHA="${1:-HEAD}"
[[ "$SHA" == --* ]] && SHA="HEAD"
FROM="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for ((i = 1; i <= $#; i++)); do
  if [[ "${!i}" == "--from" ]]; then
    j=$((i + 1))
    FROM="$HOME/AI Projects/core-${!j}"
  fi
done

if [[ ! -d "$FROM/.git" ]]; then
  echo "not a Core repo: $FROM" >&2
  exit 1
fi

RESOLVED="$(git -C "$FROM" rev-parse --short "$SHA")" || {
  echo "no such commit in $(basename "$FROM"): $SHA" >&2
  exit 1
}

DEST="${TMPDIR:-/tmp}/core-review/$(basename "$FROM")-$RESOLVED"
if [[ -d "$DEST" ]]; then
  echo "$DEST"
  exit 0
fi

mkdir -p "$DEST"
# `git archive`, NOT `git worktree add` — core-business's improvement on the first cut of this
# script, and it is strictly better. A worktree REGISTERS itself in the reviewed Core's repo:
# the author's `git worktree list` grows, stale entries need pruning, and a reviewer has written
# to the tree it is reviewing. An archive extract touches nothing on the author's side, which is
# the correct posture for a read-only adversarial review — the reviewer should not be able to
# perturb the thing under review, even benignly. It also reproduces a PULL exactly: tracked
# content at that commit and nothing else.
git -C "$FROM" archive "$RESOLVED" | tar -x -C "$DEST"

{
  echo "REVIEW TREE — $(basename "$FROM") @ $RESOLVED"
  echo "Extracted by peer-review-checkout.sh via git archive. This is the COMPLETE tracked tree at"
  echo "that commit, not a selection of files, so a change spanning several files is reviewable as"
  echo "one thing. Nothing was written to the reviewed Core."
  echo
  echo "CAVEAT: tracked content only. per_core_keep and gitignored files (settings.json among them)"
  echo "are absent or differ from the live Core, so counts derived from them will not match."
  echo "Review the LOGIC here; run the NUMBERS on your own live Core."
} > "$DEST/.REVIEW-TREE"

echo "$DEST"
