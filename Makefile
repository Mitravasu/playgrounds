UV := UV_CACHE_DIR=.uv-cache uv
PLAYWRIGHT := PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers $(UV)

.PHONY: help setup sync browsers test lint format format-check typecheck lock check

help:
	@echo "setup     Install Python dependencies and Chromium"
	@echo "sync      Synchronize the Python environment"
	@echo "browsers  Install Playwright Chromium"
	@echo "test      Run tests"
	@echo "lint      Run Ruff checks"
	@echo "format    Format Python files"
	@echo "format-check  Verify Python formatting"
	@echo "typecheck Run ty"
	@echo "lock      Verify the dependency lockfile"
	@echo "check     Run lockfile, lint, format, type, and test checks"

setup: sync browsers

sync:
	$(UV) sync

browsers:
	$(PLAYWRIGHT) run playwright install chromium

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

typecheck:
	$(UV) run ty check

lock:
	$(UV) lock --check

check: lock lint format-check typecheck test
