#!/bin/bash
# colourMatik — update to the latest version. Double-click this.
# Downloads the newest macOS installer (the one file on the release) through the
# GitHub API, takes the program out of it, refreshes deps, and reinstalls the
# panel + effect.
#
# The whole script is one { ... } block: bash reads a script as it runs, and the
# update below overwrites this very file - a block is read in full before any of
# it executes.
{
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"; cd "$DIR"
B='\033[1;34m'; G='\033[1;32m'; N='\033[0m'
# The panel shows a live progress bar by polling this file through the engine
# (GET /update_progress). Format: "pct|message", or "FAIL|reason".
PROG="$HOME/Library/Application Support/colourMatik/update_progress"
mkdir -p "$(dirname "$PROG")" 2>/dev/null || true
TMP="$(mktemp -d /tmp/colourMatik-upd.XXXXXX)"
prog() { printf '%s|%s' "$1" "$2" > "$PROG" 2>/dev/null || true; }
fail() { printf 'FAIL|%s' "$1" > "$PROG" 2>/dev/null || true; exit 1; }
# Unexpected exits still end the panel's bar - without replacing the specific
# reason fail() already wrote.
trap 'st=$?; rm -rf "$TMP"; if [ $st -ne 0 ] && ! grep -q "^FAIL" "$PROG" 2>/dev/null; then printf "FAIL|update stopped (code %s) - see update.log" "$st" > "$PROG" 2>/dev/null; fi' EXIT
prog 5 "Downloading the newest colourMatik"
echo "${B}==> Updating colourMatik...${N}"

# One file, the official way: the GitHub REST API finds colourMatik-mac.zip on
# the fixed release darwin-latest, and that asset is downloaded as
# application/octet-stream with a User-Agent that says what we are (never a
# browser's). The installer app in it carries the program as payload.zip: no
# git pull, no source zip, no raw files.
API="https://api.github.com/repos/iyiailerobotu-sudo/colourmatik-releases"
PY=""
for c in "$DIR/.venv/bin/python" /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
         /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  [ -x "$c" ] && { PY="$c"; break; }
done
[ -n "$PY" ] || fail "Python is missing - run the colourMatik installer again"
NOW="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$DIR/version.json" 2>/dev/null || echo unknown)"
UA="colourMatik-updater/$NOW (macOS)"
curl -fsSL -A "$UA" -H "Accept: application/vnd.github+json" "$API/releases/tags/darwin-latest" -o "$TMP/release.json" \
  || fail "could not reach the release - check your internet connection"
ASSET="$("$PY" -c 'import json,sys
a = [x for x in json.load(open(sys.argv[1])).get("assets", []) if x.get("name") == "colourMatik-mac.zip"]
print("%s %d" % (a[0]["url"], a[0]["size"]) if a else "")' "$TMP/release.json")"
[ -n "$ASSET" ] || fail "the release has no colourMatik-mac.zip"
ASSET_URL="${ASSET% *}"; ASSET_SIZE="${ASSET##* }"
echo "==> Downloading colourMatik-mac.zip ($ASSET_SIZE bytes)..."
curl -fsSL -A "$UA" -H "Accept: application/octet-stream" "$ASSET_URL" -o "$TMP/colourMatik-mac.zip" \
  || fail "download failed - check your internet connection"
[ "$(wc -c < "$TMP/colourMatik-mac.zip" | tr -d ' ')" = "$ASSET_SIZE" ] || fail "the download is incomplete - try again"
ditto -x -k "$TMP/colourMatik-mac.zip" "$TMP/app" || fail "could not unpack the download"
PAYLOAD="$(find "$TMP/app" -path '*.app/Contents/Resources/payload.zip' | head -1)"
[ -n "$PAYLOAD" ] || fail "that installer carries no program (an old build) - download colourMatik again from catheadai.com"
ditto -x -k "$PAYLOAD" "$TMP/src" || fail "could not unpack the program"
[ -f "$TMP/src/colourMatik/version.json" ] || fail "unexpected program layout"
# Over the installed copy, in place: .venv / vendor / slot files are not part
# of the program and are left alone; setup.sh below refreshes deps.
ditto "$TMP/src/colourMatik" "$DIR" || fail "could not write the new version into $DIR"
rm -rf "$TMP"

prog 25 "Refreshing the engine"
echo "${B}==> Refreshing engine + AI...${N}"
./setup.sh || fail "engine setup failed - see update.log"
prog 75 "Reinstalling the panels"
echo "${B}==> Reinstalling panel + effect...${N}"
./install-panel.sh  >/dev/null 2>&1 || true
# The AE (CEP) panel is per-user — refresh it DIRECTLY. install-effect.sh also
# copies it, but that script needs sudo for the effect first and a silent
# background run has no way to ask for a password, so it can bail before the
# CEP step; this guarantees the panel never stays stale.
CEPDEST="$HOME/Library/Application Support/Adobe/CEP/extensions/com.catheadai.colourmatik"
if [ -d colourmatik-cep ]; then
  mkdir -p "$CEPDEST" && cp -R colourmatik-cep/. "$CEPDEST"/ 2>/dev/null || true
fi
prog 85 "Reinstalling the effect"
./install-effect.sh || true
prog 96 "Restarting the engine"
launchctl kickstart -k "gui/$(id -u)/com.colourmatik.engine" 2>/dev/null || true
prog 100 "Done"

echo "${G}==> Updated. Restart Premiere Pro.${N}"
[ -t 0 ] && { printf "(press any key)"; read -rn1 _; echo; } || true
exit 0
}
