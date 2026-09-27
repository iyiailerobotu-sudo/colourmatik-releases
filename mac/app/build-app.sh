#!/usr/bin/env bash
# Build the one-double-click macOS installer "colourMatik Installer.app" - it
# carries the whole program (Resources/payload.zip), nothing is downloaded.
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

# The installer carries the whole program as Resources/payload.zip, so installing
# (and the in-app updater, which downloads this same zip) never fetches source
# from GitHub. It packs the COMMITTED tree - `git archive HEAD`, so uncommitted
# edits are not shipped - minus what .gitattributes marks export-ignore
# (artwork, tests, CI).
echo "==> Bundling the program (payload.zip)"
HEAD_REV="$(git -C "$ROOT" rev-parse --short HEAD)"
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
  echo "  !! uncommitted changes are NOT in this installer - it packs HEAD ($HEAD_REV)"
fi
STAGE="$BUILD/payload"; mkdir -p "$STAGE/colourMatik"
# autocrlf=false: the plug-ins' _CodeSignature/CodeResources are text plists, and
# a single rewritten line ending breaks their signature.
git -C "$ROOT" -c core.autocrlf=false archive --format=tar HEAD | tar -x -C "$STAGE/colourMatik"
PVER="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$STAGE/colourMatik/version.json")"
[ "$PVER" = "$VER" ] || { echo "  x HEAD says $PVER but version.json here says $VER - commit the version bump first."; exit 1; }
# The .ccx is what Adobe's agent installs: a stale one would put an old panel on
# every new Mac while everything else says $VER.
CCXVER="$(/usr/bin/python3 -c 'import json,sys,zipfile; print(json.loads(zipfile.ZipFile(sys.argv[1]).read("manifest.json"))["version"])' "$STAGE/colourMatik/colourmatik-uxp/colourMatik.ccx")"
[ "$CCXVER" = "$VER" ] || { echo "  x colourMatik.ccx carries $CCXVER, not $VER - rebuild the .ccx first."; exit 1; }
if [ "$MODE" = "sign" ]; then
  # Notarization inspects the program inside payload.zip as well: every plug-in
  # bundle in it needs a valid Developer ID signature (re-signed only if not).
  for b in "$STAGE"/colourMatik/colourmatik-fx/*.plugin; do
    codesign --verify --strict "$b" 2>/dev/null || {
      echo "  signing $(basename "$b")"
      codesign --force --options runtime --timestamp --sign "$APP_IDENTITY" "$b"; }
  done
fi
/usr/bin/ditto -c -k --keepParent "$STAGE/colourMatik" "$APP/Contents/Resources/payload.zip"
rm -rf "$STAGE"
echo "  payload.zip: colourMatik $VER from $HEAD_REV ($(du -h "$APP/Contents/Resources/payload.zip" | awk '{print $1}'))"

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
