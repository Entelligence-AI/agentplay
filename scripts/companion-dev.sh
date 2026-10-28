#!/bin/sh
# Build the dev app and run it. Dev builds can show synthetic fixtures and switch prototype directions.
#
#   scripts/companion-dev.sh                       your real sessions (first run shows onboarding)
#   scripts/companion-dev.sh --fixture two-active  a synthetic fixture, labelled Sample data
#   scripts/companion-dev.sh --fixture two-active --direction B
#
# Fixtures: two-active, no-agents, detected-idle, stale-quota, conflicting-plans, partial-unavailable,
# many-sessions. Directions: v1 (default), A, B, C.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
sh "$ROOT/scripts/companion-build.sh" dev
APP="$ROOT/dist/companion/AgentPlay Companion.app"
pkill -f "AgentPlay Companion.app/Contents/MacOS" 2>/dev/null || true
open -n "$APP" --args "$@"
echo "Running. It lives in the menu bar. Quit from Settings, or: pkill -f 'AgentPlay Companion.app'"
