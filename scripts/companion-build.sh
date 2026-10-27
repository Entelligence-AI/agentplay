#!/bin/sh
# Build "AgentPlay Companion.app": the SwiftUI menu-bar app plus an embedded, self-contained Python
# runtime with the AgentPlay package inside it, so the installed app needs no Python, uv or Terminal.
#
#   scripts/companion-build.sh [dev|release]
#
# Env:
#   COMPANION_PYTHON   a python-build-standalone tree (default: uv's managed CPython 3.12)
#   CODESIGN_IDENTITY  "Developer ID Application: ..." to sign for distribution (default: ad-hoc "-")
#   OUT                output dir (default: dist/companion)
#
# Signing: every Mach-O inside is signed inside-out, with the hardened runtime when an identity is given.
# Notarization is a separate step (scripts/companion-dmg.sh) and needs Apple credentials.
set -eu

MODE="${1:-dev}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/apps/macos/AgentPlayCompanion"
OUT="${OUT:-$ROOT/dist/companion}"
APP="$OUT/AgentPlay Companion.app"
IDENTITY="${CODESIGN_IDENTITY:--}"
BUILD="${SWIFT_BUILD_DIR:-$ROOT/.build/companion-$MODE}"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/python/pyproject.toml" | head -1)"
BUILD_NUMBER="${BUILD_NUMBER:-$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 1)}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# --- 1. The Python runtime -------------------------------------------------------------------------
PY_SRC="${COMPANION_PYTHON:-}"
if [ -z "$PY_SRC" ]; then
  command -v uv >/dev/null || { echo "Need uv (or set COMPANION_PYTHON to a python-build-standalone tree)."; exit 1; }
  uv python install 3.12 >/dev/null
  PY_SRC="$(dirname "$(dirname "$(uv python find --managed-python 3.12)")")"
fi
case "$PY_SRC" in
  */uv/python/cpython-*|*python-build-standalone*|*/python) ;;
  *) echo "COMPANION_PYTHON=$PY_SRC does not look like a python-build-standalone tree; refusing to bundle a system Python."; exit 1 ;;
esac
PYV="$(ls "$PY_SRC/lib" | sed -n 's/^python\(3\.[0-9]*\)$/\1/p' | head -1)"
[ -n "$PYV" ] || { echo "No lib/python3.x in $PY_SRC"; exit 1; }

# --- 2. The viewer must be bundled into the package --------------------------------------------------
# Built when missing, and rebuilt when its source changed since (a merge from main brings new pages).
BUILT="$ROOT/python/src/agentplay/viewer/index.html"
if [ ! -f "$BUILT" ] || [ -n "$(find "$ROOT/viewer/src" "$ROOT/viewer/index.html" "$ROOT/viewer/package.json" -newer "$BUILT" -print -quit 2>/dev/null)" ]; then
  say "Building the web viewer (make viewer)"
  make -C "$ROOT" viewer >/dev/null
fi

# --- 3. Swift build -------------------------------------------------------------------------------
say "Building the Swift app ($MODE)"
SWIFT_FLAGS="-c release"
[ "$MODE" = dev ] && SWIFT_FLAGS="$SWIFT_FLAGS -Xswiftc -DCOMPANION_DEV"
# Command Line Tools ship the macOS 27 SDK without SwiftUI's macro plugin; the 26.x SDK builds fine.
if [ -z "${SDKROOT:-}" ] && ! xcode-select -p | grep -q Xcode.app; then
  for sdk in /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk /Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk; do
    [ -d "$sdk" ] && export SDKROOT="$sdk" && break
  done
fi
ARCHS="${ARCHS:-$(uname -m)}"
ARCH_FLAGS=""
for a in $ARCHS; do ARCH_FLAGS="$ARCH_FLAGS --arch $a"; done
# shellcheck disable=SC2086
(cd "$PKG" && swift build $SWIFT_FLAGS $ARCH_FLAGS --scratch-path "$BUILD" --product AgentPlayCompanion)
BIN="$(cd "$PKG" && swift build $SWIFT_FLAGS $ARCH_FLAGS --scratch-path "$BUILD" --show-bin-path)/AgentPlayCompanion"

# --- 4. Assemble the bundle ------------------------------------------------------------------------
say "Assembling $APP"
STAGE="$OUT/.stage-$$"
mkdir -p "$STAGE"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources/Fixtures"
cp "$BIN" "$STAGE/Contents/MacOS/AgentPlayCompanion"
cp "$PKG/Resources/AppIcon.icns" "$STAGE/Contents/Resources/AppIcon.icns"
# The updater's public key goes into Info.plist; without it the app never checks for updates (Updater.available).
# Release builds take it from SPARKLE_PUBLIC_KEY or apps/macos/AgentPlayCompanion/Resources/sparkle_public_key.txt.
SPARKLE_KEY="${SPARKLE_PUBLIC_KEY:-$(cat "$PKG/Resources/sparkle_public_key.txt" 2>/dev/null || true)}"
sed -e "s/__VERSION__/$VERSION/g" -e "s/__BUILD__/$BUILD_NUMBER/g" -e "s|__SPARKLE_PUBLIC_KEY__|${SPARKLE_KEY:-__SPARKLE_PUBLIC_KEY__}|g" \
    "$PKG/Resources/Info.plist" > "$STAGE/Contents/Info.plist"
# Sparkle's framework, from the package SwiftPM resolved; the binary links it from ../Frameworks.
SPARKLE_FW="$(find "$BUILD/artifacts" -path '*Sparkle.xcframework/macos-arm64_x86_64/Sparkle.framework' -maxdepth 6 | head -1)"
[ -d "$SPARKLE_FW" ] || { echo "Sparkle.framework not found under $BUILD/artifacts" >&2; exit 1; }
mkdir -p "$STAGE/Contents/Frameworks"
cp -R "$SPARKLE_FW" "$STAGE/Contents/Frameworks/"
install_name_tool -add_rpath "@executable_path/../Frameworks" "$STAGE/Contents/MacOS/AgentPlayCompanion" 2>/dev/null || true
cp "$PKG/Resources/PRIVACY.txt" "$STAGE/Contents/Resources/PRIVACY.txt"
cp "$PKG/Resources/claude-statusline-bridge.sh" "$STAGE/Contents/Resources/claude-statusline-bridge.sh"
# The sample a user can ask for ("Try a sample") ships in every build; dev builds carry every fixture.
if [ "$MODE" = dev ]; then cp "$PKG"/Fixtures/*.json "$STAGE/Contents/Resources/Fixtures/"
else cp "$PKG/Fixtures/two-active.json" "$STAGE/Contents/Resources/Fixtures/"; fi

say "Embedding Python $PYV from $PY_SRC"
PYDEST="$STAGE/Contents/Resources/python"
mkdir -p "$PYDEST"
cp -R "$PY_SRC/bin" "$PY_SRC/lib" "$PYDEST/"
BEFORE_KB=$(du -sk "$PYDEST" | cut -f1)
STDLIB="$PYDEST/lib/python$PYV"
# Not needed to read transcripts or serve the viewer.
for d in test idlelib tkinter turtledemo ensurepip lib2to3 pydoc_data unittest/test sqlite3/test ctypes/test \
         distutils/tests "config-$PYV-darwin" site-packages/pip; do rm -rf "$STDLIB/$d"; done
rm -rf "$PYDEST/lib/"tcl* "$PYDEST/lib/"tk* "$PYDEST/lib/"itcl* "$PYDEST/lib/"thread* "$PYDEST/lib/pkgconfig" \
       "$STDLIB/lib-dynload/_tkinter"*.so "$PYDEST/bin/idle"* "$PYDEST/bin/pip"* "$PYDEST/bin/pydoc"* \
       "$PYDEST/bin/2to3"* "$PYDEST/bin/"*-config "$PYDEST/lib/libtcl"* "$PYDEST/lib/libtk"*
find "$PYDEST" -name '__pycache__' -type d -prune -exec rm -rf {} +
# AgentPlay itself, with its built viewer, as an installed package.
mkdir -p "$STDLIB/site-packages"
rsync -a --exclude '__pycache__' "$ROOT/python/src/agentplay" "$STDLIB/site-packages/"
# Precompile, so nothing is ever written inside the signed bundle at run time.
"$PYDEST/bin/python$PYV" -I -m compileall -q -j 0 "$STDLIB" >/dev/null || true
AFTER_KB=$(du -sk "$PYDEST" | cut -f1)
# The bundled runtime must start and import AgentPlay: a copy made on a full disk can leave empty files
# (a 0-byte libpython) that only fail when the app runs.
"$PYDEST/bin/python$PYV" -I -c "import agentplay, json, sqlite3" || { echo "The bundled Python doesn't start; is the disk full?" >&2; exit 1; }

# --- 5. Sign, inside out ---------------------------------------------------------------------------
say "Signing (identity: $IDENTITY)"
SIGN_FLAGS="--force --timestamp=none"
ENT=""
if [ "$IDENTITY" != "-" ]; then
  SIGN_FLAGS="--force --timestamp --options runtime"
  ENT="--entitlements $PKG/Resources/Companion.entitlements"
fi
find "$PYDEST" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 | xargs -0 -n 50 codesign $SIGN_FLAGS --sign "$IDENTITY"
# Sparkle, inside out, as its docs ask: the XPC services, the updater app, then the framework.
SPK="$STAGE/Contents/Frameworks/Sparkle.framework/Versions/B"
for x in "$SPK"/XPCServices/*.xpc; do codesign $SIGN_FLAGS --options runtime --sign "$IDENTITY" "$x"; done
codesign $SIGN_FLAGS --options runtime --sign "$IDENTITY" "$SPK/Autoupdate"
codesign $SIGN_FLAGS --options runtime --sign "$IDENTITY" "$SPK/Updater.app"
codesign $SIGN_FLAGS --options runtime --sign "$IDENTITY" "$STAGE/Contents/Frameworks/Sparkle.framework"
for exe in "$PYDEST/bin/python$PYV"; do
  # shellcheck disable=SC2086
  codesign $SIGN_FLAGS $ENT --sign "$IDENTITY" "$exe"
done
# shellcheck disable=SC2086
codesign $SIGN_FLAGS $ENT --identifier ai.entelligence.agentplay.companion --sign "$IDENTITY" "$STAGE"

rm -rf "$APP"
mv "$STAGE" "$APP"
codesign --verify --deep --strict "$APP"

SIZE_KB=$(du -sk "$APP" | cut -f1)
say "Built $APP"
echo "  version      $VERSION ($BUILD_NUMBER), $MODE, arch: $(lipo -archs "$APP/Contents/MacOS/AgentPlayCompanion")"
echo "  python       $PYV, $(lipo -archs "$APP/Contents/Resources/python/bin/python$PYV")"
echo "  runtime size $((BEFORE_KB / 1024)) MB copied, $((AFTER_KB / 1024)) MB after pruning and precompiling"
echo "  app size     $((SIZE_KB / 1024)) MB"
[ "$IDENTITY" = "-" ] && echo "  signature    ad-hoc (not distributable; Gatekeeper will refuse it on other Macs)"
exit 0
