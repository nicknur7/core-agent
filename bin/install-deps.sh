#!/usr/bin/env bash
# Bootstrap Core's Python deps from pyproject.toml.
#
# Default install pulls typical day-to-day extras (graphify +
# playwright). Pass --minimal to skip extras. (brain-lint-v3 extra
# was removed 2026-05-18 — see scheduling/brain-lint/lint-pass.sh
# header for OBSOLETED notice.)
#
# Run from anywhere; cd's to repo root automatically.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

EXTRAS="[graphify,playwright]"
if [[ "${1:-}" == "--minimal" ]]; then
  EXTRAS=""
  echo "Installing minimal (no extras) — hooks + brain export only"
fi

# Use `python3 -m pip`, never bare `pip`. macOS ships pip3 and NO `pip` on PATH, so
# with `set -e` the bare call aborted this script at exit 127 before it did anything —
# meaning the documented 4-step install failed at its first real command for every new
# user on a stock Mac, and nothing downstream had ever been exercised.
# (Verified 2026-07-27 by running this script on a clean clone.)
PIP=(python3 -m pip)
if ! python3 -m pip --version >/dev/null 2>&1; then
  echo "ERROR: python3 has no pip module. Install pip first:" >&2
  echo "  python3 -m ensurepip --upgrade   # or: brew install python" >&2
  exit 1
fi
# PEP 668 marks Homebrew/system Pythons "externally managed" and refuses installs  # privacy-ok: PEP 668 is a Python Enhancement Proposal, not a course
# outside a venv. Honour an active venv; otherwise fall back to --user so a fresh
# machine succeeds instead of erroring out.
PIP_TARGET_FLAGS=()
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  if python3 -c 'import sysconfig,sys; sys.exit(0 if sysconfig.get_config_var("EXT_SUFFIX") and __import__("os").path.exists(sysconfig.get_path("stdlib")+"/EXTERNALLY-MANAGED") else 1)' 2>/dev/null; then
    echo "NOTE: no virtualenv active and this Python is PEP 668 externally-managed."
    echo "      Installing with --user. To keep deps isolated instead:"
    echo "        python3 -m venv .venv && source .venv/bin/activate && bash bin/install-deps.sh"
    PIP_TARGET_FLAGS=(--user)
  fi
fi

# Prerequisites this script does NOT install (SETUP.md lists them). Warn early rather
# than failing three steps later inside setup-brain.sh.
for tool in psql createdb jq; do
  command -v "$tool" >/dev/null 2>&1 || echo "WARN: '$tool' not found — required later by bin/setup-brain.sh. Install Postgres client tools / jq."
done

echo "Installing brain pipeline deps (psycopg2-binary, pgvector, voyageai, flashrank)..."
"${PIP[@]}" install ${PIP_TARGET_FLAGS[@]+"${PIP_TARGET_FLAGS[@]}"} psycopg2-binary pgvector voyageai flashrank

# Project deps via pyproject.toml. (The old comment here claimed the repo ships NO
# root pyproject.toml — it does, and it declares the brain/graphify extras, so that
# branch was dead code. Corrected 2026-07-27.)
#
# NON-editable on purpose. `-e` requires PEP 660 support, which needs pip>=21.3 —  # privacy-ok: PEP number, not a course code
# the stock pip bundled with macOS's system python3.9 (and a fresh `python3 -m venv`
# built from it) ships pip 21.2.4 and fails with "editable mode currently requires a
# setuptools-based build", even though [build-system] IS setuptools. That failure was
# only a WARN here, so the documented 4-step install silently left anthropic,
# google-api-python-client, and networkx uninstalled on a stock Mac — the exact
# ModuleNotFoundError class this file exists to prevent. (Verified 2026-09-02 in a
# clean `python3 -m venv`: `-e` fails, plain install succeeds.) The repo is not
# structured as an importable package anyway (scripts live under bin/, scheduling/),
# so editable mode buys nothing — plain install resolves the same dependency list.
if [[ -f pyproject.toml || -f setup.py ]]; then
  echo "Installing Core deps from pyproject.toml${EXTRAS:+ with extras: $EXTRAS}"
  "${PIP[@]}" install ${PIP_TARGET_FLAGS[@]+"${PIP_TARGET_FLAGS[@]}"} ".${EXTRAS}" || echo "WARN: install failed — check pyproject.toml/setup.py"
else
  echo "WARN: no pyproject.toml/setup.py at repo root — skipped install."
  echo "      Brain deps above are installed; other Core deps (networkx, google-api-python-client, anthropic, etc.) are NOT declared anywhere. TODO: add a root pyproject.toml."
fi

# Playwright: needs the Chromium binary downloaded after pip install.
if [[ "$EXTRAS" == *"playwright"* ]]; then
  echo "Installing Playwright Chromium browser..."
  python3 -m playwright install chromium
fi

echo
echo "Done. Quick sanity check:"
echo "  python3 -c 'import networkx, googleapiclient, anthropic; print(\"core deps OK\")'"
echo
echo "macOS-only notes:"
echo "  - Hooks use BSD stat/date flags. Linux users need a wrapper layer first."
echo "  - mlx-lm (local-llm extra) only works on Apple Silicon."
