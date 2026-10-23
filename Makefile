VENV := python/.venv
# Windows venvs put the interpreter in Scripts/ (GNU make under Git Bash or MSYS2; `OS` is set by Windows).
ifeq ($(OS),Windows_NT)
PY ?= python
VPY := $(VENV)/Scripts/python.exe
else
PY ?= python3
VPY := $(VENV)/bin/python
endif

.PHONY: setup test test-python test-viewer check lint viewer run clean companion-dev companion-app companion-dmg companion-test companion-clips

setup:
	$(PY) -m venv $(VENV)
	$(VPY) -m pip install -q --upgrade pip
	$(VPY) -m pip install -q -e "python[dev]"
	cd viewer && pnpm install

test: test-python test-viewer

test-python:
	cd python && ../$(VPY) -m pytest -q

test-viewer:
	cd viewer && pnpm test

lint:
	cd python && ../$(VPY) -m ruff check src tests
	cd viewer && pnpm run typecheck

check: lint test
	$(PY) scripts/check_clean.py

# Build the web viewer (this also writes dist/THIRD_PARTY_LICENSES.txt) and copy it into the Python package
# so `agentplay view` serves it.
viewer:
	cd viewer && pnpm run build
	rm -rf python/src/agentplay/viewer
	cp -r viewer/dist python/src/agentplay/viewer

run:
	cd python && ../$(VPY) -m agentplay

clean:
	rm -rf python/.venv python/dist viewer/dist viewer/node_modules python/src/agentplay/viewer
	find . -name __pycache__ -prune -exec rm -rf {} +

# macOS companion (apps/macos/AgentPlayCompanion). macOS only; the CLI and viewer above are unaffected.
companion-dev:
	sh scripts/companion-dev.sh --fixture two-active

companion-app:
	sh scripts/companion-build.sh release

companion-dmg:
	sh scripts/companion-dmg.sh

companion-test:
	sh scripts/companion-test.sh

companion-clips:
	sh scripts/companion-build.sh dev && sh scripts/companion-clips.sh
