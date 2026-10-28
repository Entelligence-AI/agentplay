#!/bin/sh
# Write the appcast Sparkle reads, for the DMG a release ships. Run after companion-dmg.sh, with the private key.
#
#   SPARKLE_PRIVATE_KEY_FILE=~/.agentplay-sparkle.key sh scripts/companion-appcast.sh <download-url-of-the-dmg>
#
# The key pair is made once with Sparkle's generate_keys (see RELEASE_CHECKLIST.md); the public half lives in
# Resources/sparkle_public_key.txt and goes into every build's Info.plist, the private half never leaves the
# release machine (or the CI secret). Writes dist/companion/appcast.xml, which the release attaches next to the
# DMG; the app's SUFeedURL points at the latest release's appcast.xml.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$ROOT/dist/companion}"
URL="${1:?Give the public download URL of the DMG}"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/python/pyproject.toml" | head -1)"
DMG="$OUT/AgentPlay-Companion-$VERSION-$(uname -m).dmg"
[ -f "$DMG" ] || { echo "No DMG at $DMG; run scripts/companion-dmg.sh first" >&2; exit 1; }
KEYFILE="${SPARKLE_PRIVATE_KEY_FILE:?Set SPARKLE_PRIVATE_KEY_FILE to the EdDSA private key file}"
SIGN="$(find "$ROOT/.build" -name sign_update -path '*Sparkle*' -maxdepth 8 2>/dev/null | head -1)"
[ -x "$SIGN" ] || { echo "sign_update not found; build the app once so SwiftPM fetches Sparkle" >&2; exit 1; }
BUILD_NUMBER="$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$OUT/AgentPlay Companion.app/Contents/Info.plist")"
SIG="$("$SIGN" --ed-key-file "$KEYFILE" "$DMG")"   # prints: sparkle:edSignature="…" length="…"
NOTES="${RELEASE_NOTES_URL:-https://github.com/Entelligence-AI/agentplay/releases/tag/v$VERSION}"
cat > "$OUT/appcast.xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>AgentPlay Companion</title>
    <item>
      <title>$VERSION</title>
      <sparkle:version>$BUILD_NUMBER</sparkle:version>
      <sparkle:shortVersionString>$VERSION</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>14.0</sparkle:minimumSystemVersion>
      <sparkle:releaseNotesLink>$NOTES</sparkle:releaseNotesLink>
      <pubDate>$(date -u '+%a, %d %b %Y %H:%M:%S +0000')</pubDate>
      <enclosure url="$URL" type="application/octet-stream" $SIG />
    </item>
  </channel>
</rss>
XML
echo "Wrote $OUT/appcast.xml for $VERSION ($BUILD_NUMBER)"
