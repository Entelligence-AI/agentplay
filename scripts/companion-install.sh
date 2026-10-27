#!/bin/sh
# Install AgentPlay Companion from a DMG (a file or a URL), for teammates before the app is notarized.
#
#   sh companion-install.sh ~/Downloads/AgentPlay-Companion-0.2.6-arm64.dmg
#   sh companion-install.sh https://github.com/Entelligence-AI/agentplay/releases/download/<tag>/AgentPlay-Companion-0.2.6-arm64.dmg
#
# What it does: copies the app into /Applications, clears the "downloaded from the internet" flag macOS puts
# on it (an un-notarized app with that flag can't be opened), and opens it. Nothing else on the Mac changes.
# Once the app is notarized this isn't needed: download, drag to Applications, open.
set -eu
SRC="${1:?Give the DMG file or its URL}"
APP="AgentPlay Companion.app"
TMP="$(mktemp -d)"
trap 'hdiutil detach "$TMP/mnt" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

case "$SRC" in
  http://*|https://*) echo "Downloading…"; curl -fL --progress-bar -o "$TMP/app.dmg" "$SRC"; DMG="$TMP/app.dmg" ;;
  *) DMG="$SRC" ;;
esac
[ -f "$DMG" ] || { echo "No DMG at $DMG"; exit 1; }

[ "$(uname -m)" = "arm64" ] || { echo "This build is for Apple silicon Macs."; exit 1; }

mkdir -p "$TMP/mnt"
hdiutil attach -nobrowse -readonly -mountpoint "$TMP/mnt" "$DMG" >/dev/null
[ -d "$TMP/mnt/$APP" ] || { echo "The DMG has no $APP inside."; exit 1; }

# Quit a running copy so the new one replaces it cleanly.
osascript -e 'tell application "AgentPlay Companion" to quit' >/dev/null 2>&1 || true
sleep 1

if [ -d "/Applications/$APP" ]; then
  mv "/Applications/$APP" "$TMP/old.app"
fi
cp -R "$TMP/mnt/$APP" /Applications/
xattr -dr com.apple.quarantine "/Applications/$APP" 2>/dev/null || true

echo "Installed /Applications/$APP"
open "/Applications/$APP"
echo "Pip is in your menu bar; the welcome window walks you through the rest."
