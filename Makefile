.PHONY: install test lint strip-check init-brain arch-doc help

help:
	@echo "Core engine — developer commands"
	@echo ""
	@echo "  make install         Install Python deps (editable, [all] extras)"
	@echo "  make test            Run hook test harness"
	@echo "  make lint            Run doc-paths + shellcheck + ruff"
	@echo "  make strip-check     Verify engine clean of personal data"
	@echo "  make init-brain      Bootstrap a new brain repo (BRAIN_DIR=<path>, or \$$CORE_BRAIN)"
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

init-brain:
	@test -n "$(BRAIN_DIR)" || { echo "BRAIN_DIR is empty — set CORE_BRAIN or pass BRAIN_DIR=<path>"; exit 1; }
	bash bin/init-brain.sh "$(BRAIN_DIR)"

arch-doc:
	@# The architecture doc is DERIVED, never hand-edited — see bin/gen-architecture-doc.py.
	python3 bin/gen-architecture-doc.py
