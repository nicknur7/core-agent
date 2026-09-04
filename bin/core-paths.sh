#!/usr/bin/env bash
# Core path registry (shell loader).
#
# Sources every tracked path from bin/core-paths.json (single source of truth)
# and exports them as $CORE_<NAME> environment variables. Python loader is
# bin/core_paths.py — both read the same JSON, so drift between the two is
# structurally impossible (the lint at bin/lint-code-paths.py also enforces
# this).
#
# Sourcing pattern:
#   REPO=$(git rev-parse --show-toplevel)
#   source "$REPO/bin/core-paths.sh"
#
# If you find yourself hardcoding a path in a hook/script, STOP — add a
# constant to core-paths.json and reference it here. Drift between writers
# and readers must be structurally impossible.
#
# Last updated: 2026-05-15 (JSON refactor — replaces literal definitions).

# Repo root. $CORE_INSTANCE wins if set; fall back to git rev-parse.
export CORE_INSTANCE="${CORE_INSTANCE:-$(git rev-parse --show-toplevel 2>/dev/null)}"

# CORE_ENGINE + CORE_BRAIN — sibling repos, not instance-relative.
# Env var wins. Fallback assumes engine + brain live as siblings of the instance
# under the same parent dir (the init-instance.sh / init-brain.sh convention):
#   ~/AI Projects/core              ← engine ($CORE_ENGINE)
#   ~/AI Projects/<instance>        ← instance ($CORE_INSTANCE)
#   ~/AI Projects/<instance>-brain  ← brain ($CORE_BRAIN)
# If your layout differs, set CORE_ENGINE / CORE_BRAIN in your shell rc.
# Fail-loud guards in lint.py / consolidate.py / run-brain-update.sh catch
# unset vars in critical paths.
# Added 2026-05-16 cascade-fix follow-up: prior version exported only CORE_INSTANCE,
# leaving ENGINE+BRAIN reliant on per-user .zshrc — fresh clones broke silently.
if [[ -n "$CORE_INSTANCE" ]]; then
  _INSTANCE_PARENT="$(dirname "$CORE_INSTANCE")"
  _INSTANCE_BASE="$(basename "$CORE_INSTANCE")"
  export CORE_ENGINE="${CORE_ENGINE:-$_INSTANCE_PARENT/core}"
  # Brain fallback: 2026-05-19 multi-Core split, brain is shared across all
  # 3 Cores at $_INSTANCE_PARENT/core-brain (NOT $instance-brain — that
  # was the pre-split per-instance convention and gives a non-existent
  # path under multi-Core).
  export CORE_BRAIN="${CORE_BRAIN:-$_INSTANCE_PARENT/core-brain}"
fi

# Load JSON via python3, emit "export CORE_KEY=/abs/path" lines, eval them.
# All tracked paths are repo-relative in the JSON; we prepend $CORE_INSTANCE.
_CORE_PATHS_JSON="$CORE_INSTANCE/bin/core-paths.json"
if [[ ! -f "$_CORE_PATHS_JSON" ]]; then
  echo "[core-paths.sh] ERROR: $_CORE_PATHS_JSON not found" >&2
  return 1 2>/dev/null || exit 1
fi

eval "$(python3 - "$_CORE_PATHS_JSON" "$CORE_INSTANCE" <<'PY'
import json, sys, shlex
with open(sys.argv[1]) as f:
    data = json.load(f)
instance = sys.argv[2]
for section, entries in data.items():
    if section.startswith("_"):  # skip _comment and other meta keys
        continue
    if not isinstance(entries, dict):
        continue
    for key, rel in entries.items():
        if not isinstance(rel, str):
            continue
        # Prepend instance unless already absolute.
        abs_path = rel if rel.startswith("/") else f"{instance}/{rel}"
        print(f"export CORE_{key}={shlex.quote(abs_path)}")
PY
)"
