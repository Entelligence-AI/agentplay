#!/bin/sh
# Record a real 15 s screen capture of the popover while you use it (needs Screen Recording permission
# for your terminal). Close anything private on screen first: this records the whole main display.
#
#   scripts/companion-capture.sh [name]     → docs/companion/media/<name>-screen.mov
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="${1:-live}"
OUT="$ROOT/docs/companion/media/$NAME-screen.mov"
echo "Recording 15 s in 3 s. Open the AgentPlay popover from the menu bar and walk through it."
sleep 3
screencapture -v -V 15 -x "$OUT"
echo "Saved $OUT. Check it for anything private before committing."
