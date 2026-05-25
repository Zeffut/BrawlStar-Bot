.PHONY: install test test-all test-bench lint run smoke overlay record clean help

VENV := venv
PYTHON := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest

help:
	@echo "BrawlStar-Bot Makefile"
	@echo ""
	@echo "Setup:"
	@echo "  make install        Create venv + install all dependencies"
	@echo ""
	@echo "Run:"
	@echo "  make run            Start the bot (assumes phone connected via USB)"
	@echo "  make smoke          Run smoke test against phone (no game actions)"
	@echo "  make overlay        Snap one debug overlay from phone"
	@echo "  make record         Record a session (frames + states) for 2 min"
	@echo ""
	@echo "Test:"
	@echo "  make test           Run unit + fixture + e2e tests"
	@echo "  make test-all       Same + perf benchmarks"
	@echo "  make test-bench     Only perf benchmarks"
	@echo ""
	@echo "Misc:"
	@echo "  make clean          Remove pycache, .pytest_cache, debug screenshots"

install:
	./install.sh

run:
	$(PYTHON) -m bsbot.main

smoke:
	$(PYTHON) tools/smoke_test.py

overlay:
	$(PYTHON) tools/debug_overlay.py

record:
	$(PYTHON) tools/session_recorder.py --minutes 2 --fps 1

test:
	$(PYTEST)

test-all:
	$(PYTEST) -m "bench or not bench"

test-bench:
	$(PYTEST) -m bench -s

lint:
	@$(PYTHON) -c "import sys; sys.exit('lint not configured yet — consider ruff/black')"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .coverage htmlcov
	find debug -name "overlay_*.png" -delete 2>/dev/null || true
	find debug -name "20*.png" -delete 2>/dev/null || true
	@echo "Cleaned."
