#!/bin/sh
# Every companion check: Python contract and leak tests, Swift unit tests, fixture schema, the repo's guards.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/python/.venv/bin/python"
[ -x "$PY" ] || { echo "Run make setup first."; exit 1; }
echo "== Python: companion contract, privacy and alerts"
(cd "$ROOT/python" && "$PY" -m pytest -q tests/test_companion.py)
echo "== Python: whole suite (the companion must not change anything else)"
(cd "$ROOT/python" && "$PY" -m pytest -q)
(cd "$ROOT/python" && "$PY" -m ruff check src tests)
echo "== Fixtures regenerate identically"
"$PY" "$ROOT/scripts/companion_fixtures.py" >/dev/null
git -C "$ROOT" diff --quiet -- apps/macos/AgentPlayCompanion/Fixtures || { echo "Fixtures changed: commit them."; exit 1; }
echo "== Swift: AgentPlayCore"
if [ -z "${SDKROOT:-}" ] && ! xcode-select -p | grep -q Xcode.app && [ -d /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk ]; then
  export SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
fi
# One reused build directory: a new one per run fills the disk. Incremental rebuilds can lose the Swift
# Testing macro plugin path, so pass it explicitly from the active toolchain.
TEST_BUILD="${TMPDIR:-/tmp}/agentplay-companion-test-build"
PLUGINS="$(dirname "$(xcrun --find swift)")/../lib/swift/host/plugins/testing"
set --
[ -d "$PLUGINS" ] && set -- -Xswiftc -plugin-path -Xswiftc "$PLUGINS"
(cd "$ROOT/apps/macos/AgentPlayCompanion" && swift test --scratch-path "$TEST_BUILD" "$@")
echo "== Repo guards"
python3 "$ROOT/scripts/check_clean.py"
# Only a dev build has --selftest (it carries every fixture); a release build would just launch.
if [ -f "$ROOT/dist/companion/AgentPlay Companion.app/Contents/Resources/Fixtures/no-agents.json" ]; then
  echo "== Bundle self-test (dev build, store: ${AGENTPLAY_HOME:-~/.agentplay})"
  "$ROOT/dist/companion/AgentPlay Companion.app/Contents/MacOS/AgentPlayCompanion" --selftest
fi
