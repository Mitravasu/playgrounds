UV := UV_CACHE_DIR=.uv-cache uv
PLAYWRIGHT := PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers $(UV)

.PHONY: help setup sync browsers analyzer-up analyzer-down test lint format format-check typecheck lock check

help:
	@echo "setup     Install Python dependencies and Chromium"
	@echo "sync      Synchronize the Python environment"
	@echo "browsers  Install Playwright Chromium"
	@echo "analyzer-up  Build the browser image and recreate the analyzer proxy"
	@echo "analyzer-down  Stop the analyzer proxy"
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

analyzer-up:
	docker build --tag playgrounds-browser:latest sandbox
	docker compose -f docker-compose.analyzer-egress.yml up --build --force-recreate --detach

analyzer-down:
	docker compose -f docker-compose.analyzer-egress.yml down

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
