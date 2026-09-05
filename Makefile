.PHONY: install test test-fresh lint strip-check setup-brain init-brain arch-doc help

help:
	@echo "Core engine — developer commands"
	@echo ""
	@echo "  make install         Install Python deps (editable, [all] extras)"
	@echo "  make test            Run hook test harness"
	@echo "  make lint            Run doc-paths + shellcheck + ruff"
	@echo "  make strip-check     Verify engine clean of personal data"
	@echo "  make setup-brain     Postgres DB + markdown vault (BRAIN_DIR=<path>, or \$$CORE_BRAIN) — bin/setup-brain.sh"
	@echo "  make test-fresh      The fresh-clone contract: 0 FAIL/CRASH/LEAK is green; SKIP/ABSTAIN are named"
	@echo "  make arch-doc        Regenerate docs/architecture/core-system-architecture.html"
	@echo "                       from live measurement (bin/gen-arch-roster.py + gen-architecture-doc.py)"

install:
	uv pip install -e .[all]

test:
	@if [ -d tests/hooks ]; then \
		for t in tests/hooks/test-*.sh; do bash "$$t"; done ; \
	else \
		for t in .claude/hooks/tests/test-*.sh; do bash "$$t"; done ; \
	fi

lint:
	bash bin/lint-doc-paths.sh
	bash bin/lint-code-paths.sh
	find .claude/hooks bin -type f -name "*.sh" -print0 | xargs -0 shellcheck -S warning || true
	ruff check . || true

strip-check:
	@# Delegates to bin/strip-check.py, which scans ONLY what the manifest says actually ships
	@# (shared.dirs + shared.files MINUS per_core_keep). The previous inline grep scanned the whole
	@# tree, so on the baseline-WRITER Core it always failed — on sessions/, memory/ and CLAUDE.md,
	@# none of which ever travel. A check that cannot pass where it runs is a check people mute.
	@python3 bin/strip-check.py

setup-brain:
	@# The documented implementation. `init-brain` below is a deprecating alias: the old bin/init-brain.sh
	@# is tombstoned in bin/sync-manifest.json (deleted on every seat at pull) and this target still
	@# named it — `make init-brain` was broken on every seat until 2026-09-04 (codex #5943 P0).
	@test -n "$(BRAIN_DIR)$(CORE_BRAIN)" || { echo "set BRAIN_DIR=<path> or CORE_BRAIN"; exit 1; }
	CORE_BRAIN="$${BRAIN_DIR:-$$CORE_BRAIN}" bash bin/setup-brain.sh

init-brain: setup-brain
	@echo "note: 'make init-brain' is an alias for 'make setup-brain' and will be removed"

test-fresh:
	bash bin/tests/run-all.sh --quiet --fresh

arch-doc:
	@# The architecture doc is DERIVED, never hand-edited — see bin/gen-architecture-doc.py.
	python3 bin/gen-architecture-doc.py
