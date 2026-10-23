#!/bin/sh
# Render the prototype and demo clips (10 to 15 s MP4s) from the real SwiftUI views over synthetic fixtures.
#
#   scripts/companion-clips.sh [out_dir]      (default: docs/companion/media)
#
# These are keyframe clips: each beat is one state of the real popover, rendered offscreen and held,
# with short cross-fades. They are not screen recordings of a person using the app; for those, run
# scripts/companion-capture.sh. The proof clip ends on the real viewer, served from a synthetic store,
# opened at the exact deep link the companion builds. Nothing from your own sessions is used.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/docs/companion/media}"
WORK="${TMPDIR:-/tmp}/apc-clips-$$"
APP_BIN="${APP_BIN:-$ROOT/dist/companion/AgentPlay Companion.app/Contents/MacOS/AgentPlayCompanion}"
FIX="$ROOT/apps/macos/AgentPlayCompanion/Fixtures"
mkdir -p "$OUT" "$WORK"
command -v ffmpeg >/dev/null || { echo "Needs ffmpeg."; exit 1; }
[ -x "$APP_BIN" ] || { echo "Build the dev app first: scripts/companion-build.sh dev"; exit 1; }

AGENTPLAY_COMPANION_FIXTURES="$FIX" "$APP_BIN" --render-clips "$WORK/frames" >/dev/null

# The real viewer at the moment the proof clip's card links to, from a synthetic store.
STORE="$WORK/store"
COMPANION_FIXTURE_STORE="$STORE" "$ROOT/python/.venv/bin/python" "$ROOT/scripts/companion_fixtures.py" "$WORK/fx" >/dev/null
SHELL_BIN="$(find "$HOME/Library/Caches/ms-playwright" -name chrome-headless-shell -type f 2>/dev/null | head -1 || true)"
if [ -n "$SHELL_BIN" ]; then
  AGENTPLAY_HOME="$STORE" AGENTPLAY_NO_NETWORK=1 "$ROOT/python/.venv/bin/python" -m agentplay view --no-open --no-build --port 0 > "$WORK/viewer.log" 2>&1 &
  VPID=$!
  sleep 3
  URL="$(grep -o 'http://127.0.0.1:[0-9]*/' "$WORK/viewer.log" | head -1)"
  for theme in light dark; do
    "$SHELL_BIN" --headless --hide-scrollbars --window-size=1440,900 --virtual-time-budget=8000 \
      --screenshot="$WORK/viewer-$theme.png" "${URL}?session=local%3Aclaude-code%3Asample-7f3a&at=5&stab=errors&theme=$theme" 2>/dev/null
  done
  kill "$VPID" 2>/dev/null || true
fi

dims() { sips -g pixelWidth -g pixelHeight "$1" | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w, h}'; }

# clip <name> <theme> <seconds per beat> [extra image appended as last beat]
clip() {
  name=$1; theme=$2; hold=$3; extra=${4:-}
  frames=$(ls "$WORK/frames/$name/"*-"$theme".png)
  [ -n "$extra" ] && [ -f "$extra" ] && frames="$frames $extra"
  W=0; H=0
  for f in $frames; do set -- $(dims "$f"); [ "$1" -gt "$W" ] && W=$1; [ "$2" -gt "$H" ] && H=$2; done
  if [ -n "$extra" ]; then W=1440; H=900; fi
  W=$(( (W + 1) / 2 * 2 )); H=$(( (H + 1) / 2 * 2 ))
  bg=$([ "$theme" = dark ] && echo 0x1C1D1B || echo 0xFFFFFF)
  inputs=""; filters=""; n=0
  for f in $frames; do
    inputs="$inputs -loop 1 -t $hold -i $f"
    filters="$filters[$n:v]scale=w='min($W,iw)':h='min($H,ih)':force_original_aspect_ratio=decrease,pad=$W:$H:(ow-iw)/2:$([ -n "$extra" ] && echo '(oh-ih)/2' || echo 0):color=$bg,setsar=1,fps=30,format=yuv420p[v$n];"
    n=$((n + 1))
  done
  chain="[v0]"; off=0; i=1
  while [ $i -lt $n ]; do
    off=$(echo "$off + $hold - 0.25" | bc)
    filters="$filters$chain[v$i]xfade=transition=fade:duration=0.25:offset=$off[x$i];"
    chain="[x$i]"; i=$((i + 1))
  done
  filters="${filters%;}"
  # shellcheck disable=SC2086
  ffmpeg -y -loglevel error $inputs -filter_complex "$filters" -map "$chain" -c:v libx264 -pix_fmt yuv420p \
    -movflags +faststart -crf 22 "$OUT/$name-$theme.mp4"
  printf '  %s-%s.mp4  %ss\n' "$name" "$theme" "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/$name-$theme.mp4" | cut -c1-4)"
}

echo "Clips in $OUT:"
for theme in light dark; do
  clip A-pulse "$theme" 1.6
  clip B-breakpoint "$theme" 4
  clip C-lab "$theme" 5.5
  clip v1-tour "$theme" 3
  clip proof "$theme" 3.5 "$WORK/viewer-$theme.png"
done
rm -rf "$WORK"
