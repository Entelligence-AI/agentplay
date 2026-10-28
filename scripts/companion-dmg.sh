#!/bin/sh
# Package "AgentPlay Companion.app" into a DMG, and (only with Apple credentials) sign, notarize and staple it.
#
#   CODESIGN_IDENTITY="Developer ID Application: Entelligence AI (TEAMID)" \
#   NOTARY_PROFILE=agentplay-notary \
#   scripts/companion-dmg.sh
#
# NOTARY_PROFILE is a notarytool keychain profile, created once with:
#   xcrun notarytool store-credentials agentplay-notary --apple-id <id> --team-id <TEAMID> --password <app-specific>
#
# Without both variables this builds an unsigned-for-distribution DMG for local testing, says so, and exits 2.
# It never labels anything notarized unless `stapler validate` and `spctl` both pass.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$ROOT/dist/companion}"
APP="$OUT/AgentPlay Companion.app"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/python/pyproject.toml" | head -1)"
ARCH="$(uname -m)"
DMG="$OUT/AgentPlay-Companion-$VERSION-$ARCH.dmg"
RELEASE=0
[ -n "${CODESIGN_IDENTITY:-}" ] && [ -n "${NOTARY_PROFILE:-}" ] && RELEASE=1

# Always a fresh release build: never ship a dev build (fixtures, render and self-test modes) by accident.
sh "$ROOT/scripts/companion-build.sh" release

STAGE="$OUT/.dmg-$$"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp "$ROOT/apps/macos/AgentPlayCompanion/Resources/PRIVACY.txt" "$STAGE/Privacy.txt"
rm -f "$DMG"
hdiutil create -quiet -volname "AgentPlay Companion" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG"
rm -rf "$STAGE"

if [ "$RELEASE" = 0 ]; then
  echo "Built $DMG"
  echo "BLOCKED: not signed with a Developer ID and not notarized (set CODESIGN_IDENTITY and NOTARY_PROFILE)."
  echo "This DMG is for testing on this Mac only. Gatekeeper will refuse it elsewhere; do not publish it."
  exit 2
fi

codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$DMG"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
MNT="$(mktemp -d)"
hdiutil attach -quiet -nobrowse -mountpoint "$MNT" "$DMG"
codesign --verify --deep --strict --verbose=2 "$MNT/AgentPlay Companion.app"
spctl --assess --type execute --verbose=2 "$MNT/AgentPlay Companion.app"
hdiutil detach -quiet "$MNT"
( cd "$OUT" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256" )
echo "Notarized and stapled: $DMG"
cat "$DMG.sha256"
