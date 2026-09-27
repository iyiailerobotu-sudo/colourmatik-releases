#!/bin/bash
# colourMatik — update to the latest version. Double-click this.
# Pulls the newest code from GitHub, refreshes deps, and reinstalls the panel + effect.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"; cd "$DIR"
B='\033[1;34m'; G='\033[1;32m'; N='\033[0m'
# The panel shows a live progress bar by polling this file through the engine
# (GET /update_progress). Format: "pct|message".
PROG="$HOME/Library/Application Support/colourMatik/update_progress"
mkdir -p "$(dirname "$PROG")" 2>/dev/null || true
prog() { printf '%s|%s' "$1" "$2" > "$PROG" 2>/dev/null || true; }
fail() { printf 'FAIL|%s' "$1" > "$PROG" 2>/dev/null || true; exit 1; }
trap 'st=$?; [ $st -ne 0 ] && printf "FAIL|update stopped (code %s) - see update.log" "$st" > "$PROG" 2>/dev/null' EXIT
prog 5 "Downloading the newest colourMatik"
echo "${B}==> Updating colourMatik...${N}"

GOT=""
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  git pull --ff-only && GOT=1 || echo "Pull failed (local changes / diverged) - refreshing from the zip instead."
fi
if [ -n "$GOT" ]; then
  :
else
  # Installed from the one-click installer (zip, no .git): refresh from the zip.
  # .venv / vendor / slot files are untouched; setup.sh below refreshes deps.
  echo "==> Downloading the latest colourMatik (zip)..."
  TMP="$(mktemp -d /tmp/colourMatik-upd.XXXXXX)"
  curl -fsSL "https://github.com/iyiailerobotu-sudo/colourmatik-releases/archive/refs/heads/main.zip" -o "$TMP/main.zip" || fail "download failed - check your internet connection"
  ditto -x -k "$TMP/main.zip" "$TMP" || fail "could not unpack the download"
  INNER="$(ls -d "$TMP"/colourMatik-* 2>/dev/null | head -1)"
  [ -z "$INNER" ] && { echo "Unexpected zip layout."; exit 1; }
  ditto "$INNER" "$DIR"
  rm -rf "$TMP"
fi

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
