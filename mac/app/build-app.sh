#!/usr/bin/env bash
# Build the one-double-click macOS installer "colourMatik Installer.app".
# It is signed with Developer ID Application (which we have) and notarized, so it
# opens with NO Gatekeeper warning — download, unzip, double-click, done.
#
#   ./mac/app/build-app.sh           # unsigned (local test)
#   ./mac/app/build-app.sh sign      # sign + notarize + staple (ship this)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$ROOT"
MODE="${1:-unsigned}"
APPDIR="$ROOT/mac/app"
APPNAME="colourMatik Installer"
# The app's version is the release's version. It was hard-coded "1.2.0" here,
# so every rebuilt installer showed 1.2.0 in Finder's Get Info.
VER="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$ROOT/version.json")"
APP_IDENTITY="Developer ID Application: Sevki Bugra Ozbek (PCH6L56487)"
PROFILE="colourmatik"

BUILD="$APPDIR/build"; rm -rf "$BUILD"; mkdir -p "$BUILD"
APP="$BUILD/$APPNAME.app"

echo "==> osacompile -> $APPNAME.app"
osacompile -o "$APP" "$APPDIR/installer.applescript"

echo "==> Bundling install-mac.sh"
cp "$APPDIR/install-mac.sh" "$APP/Contents/Resources/install-mac.sh"
chmod +x "$APP/Contents/Resources/install-mac.sh"

echo "==> Icon + Info.plist"
cp "$ROOT/assets/icons/colourMatik.icns" "$APP/Contents/Resources/applet.icns"
PB=/usr/libexec/PlistBuddy
IP="$APP/Contents/Info.plist"
$PB -c "Set :CFBundleIdentifier com.catheadai.colourmatik.installer" "$IP" 2>/dev/null || $PB -c "Add :CFBundleIdentifier string com.catheadai.colourmatik.installer" "$IP"
$PB -c "Set :CFBundleName $APPNAME" "$IP" 2>/dev/null || $PB -c "Add :CFBundleName string $APPNAME" "$IP"
$PB -c "Set :CFBundleShortVersionString $VER" "$IP" 2>/dev/null || $PB -c "Add :CFBundleShortVersionString string $VER" "$IP"
$PB -c "Set :CFBundleVersion $VER" "$IP" 2>/dev/null || $PB -c "Add :CFBundleVersion string $VER" "$IP"
$PB -c "Set :CFBundleIconFile applet" "$IP" 2>/dev/null || $PB -c "Add :CFBundleIconFile string applet" "$IP"
$PB -c "Add :LSMinimumSystemVersion string 11.0" "$IP" 2>/dev/null || true
$PB -c "Add :NSHumanReadableCopyright string colourMatik — catheadai.com" "$IP" 2>/dev/null || true

mkdir -p "$ROOT/dist"
# Same name as the fixed download (release tag darwin-latest) - upload as is.
ZIP="$ROOT/dist/colourMatik-mac.zip"

if [ "$MODE" = "sign" ]; then
  echo "==> Signing (Developer ID Application, hardened runtime + timestamp)"
  codesign --force --options runtime --timestamp --sign "$APP_IDENTITY" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
  echo "==> Zipping for notarization"
  /usr/bin/ditto -c -k --keepParent "$APP" "$ZIP"
  echo "==> Notarizing (uploads to Apple, waits)…"
  xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
  echo "==> Stapling"
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP" && echo "  app notarized + stapled"
  rm -f "$ZIP"; /usr/bin/ditto -c -k --keepParent "$APP" "$ZIP"   # re-zip the stapled app
  echo "==> $ZIP  (signed, notarized, stapled)"
else
  /usr/bin/ditto -c -k --keepParent "$APP" "$ZIP"
  echo "==> UNSIGNED $ZIP (local test only). Run './mac/app/build-app.sh sign' to ship."
fi
echo "==> app: $APP  ($(du -sh "$APP" | awk '{print $1}'))"
