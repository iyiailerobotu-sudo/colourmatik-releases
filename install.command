#!/bin/bash
# colourMatik — one-click installer for macOS (Apple Silicon).
# DOUBLE-CLICK to run. If macOS says "unidentified developer": right-click → Open (once).
# It installs the prerequisites, the engine + AI model, the Premiere panel, and the
# native effect, and sets the engine to run automatically. by catheadai.com
set -uo pipefail

B='\033[1;34m'; G='\033[1;32m'; Y='\033[1;33m'; R='\033[1;31m'; N='\033[0m'
say(){ printf "\n${B}==>${N} %s\n" "$1"; }
ok(){  printf "${G}  \xE2\x9C\x93${N} %s\n" "$1"; }
warn(){ printf "${Y}  !${N} %s\n" "$1"; }
pause(){ echo; printf "${B}You can close this window now.${N}\n"; [ -t 0 ] && { printf "(press any key)"; read -rn1 _; echo; } || true; }
die(){ printf "\n${R}  \xE2\x9C\x97 %s${N}\n" "$1"; pause; exit 1; }

clear 2>/dev/null || true
cat <<'BANNER'
  +----------------------------------------------+
  |   colourMatik  -  installer                  |
  |   Sevki Bugra Ozbek | catheadai.com          |
  +----------------------------------------------+
BANNER

[ "$(uname)" = "Darwin" ] || die "This installer is for macOS only."
[ "$(uname -m)" = "arm64" ] || warn "This Mac is Intel; the native effect is Apple-Silicon only (the rest still works)."

REPO="https://github.com/iyiailerobotu-sudo/colourmatik-releases.git"
SELF="$(cd "$(dirname "$0")" && pwd)"

# 1) Apple Command Line Tools (git needs it)
if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
  say "Installing Apple Command Line Tools — click 'Install' in the dialog that appears, then wait."
  /usr/bin/xcode-select --install >/dev/null 2>&1 || true
  die "When 'Command Line Tools' finishes, run this installer again."
fi
ok "Command Line Tools present"

# 2) Homebrew
if ! command -v brew >/dev/null 2>&1; then
  say "Installing Homebrew (you'll be asked for your Mac password)..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || die "Homebrew install failed."
fi
if   [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -x /usr/local/bin/brew   ]; then eval "$(/usr/local/bin/brew shellenv)"; fi
command -v brew >/dev/null 2>&1 || die "Homebrew is not on PATH."
ok "Homebrew ready"

# 3) Prerequisites
say "Installing Python 3.11, ffmpeg and git..."
brew install python@3.11 ffmpeg git >/dev/null 2>&1 || brew install python@3.11 ffmpeg git || die "Failed to install prerequisites."
BREW_PREFIX="$(brew --prefix)"
ok "Python / ffmpeg / git ready"

# 4) Get the code (use a local copy if this installer sits inside the repo; else download)
if [ -f "$SELF/setup.sh" ]; then
  INSTALL_DIR="$SELF"; say "Using colourMatik in: $INSTALL_DIR"
else
  INSTALL_DIR="$HOME/colourMatik"
  if [ -d "$INSTALL_DIR/.git" ]; then say "Updating colourMatik..."; git -C "$INSTALL_DIR" pull --ff-only || true
  else say "Downloading colourMatik to $INSTALL_DIR..."; git clone --depth 1 "$REPO" "$INSTALL_DIR" || die "Download failed."; fi
fi
cd "$INSTALL_DIR"

# 5) Engine + AI (a few GB; 10-20 min on first run)
say "Setting up the engine and the AI model. This downloads a few GB and can take 10-20 minutes."
PYTHON="$BREW_PREFIX/bin/python3.11" ./setup.sh || die "Setup failed (see messages above)."
ok "Engine + AI installed"

# 6) Panel + effect
say "Installing the Premiere panel..."
if ./install-panel.sh >/dev/null 2>&1; then ok "Panel installed"; else warn "Panel step skipped - open Premiere once, then re-run this installer."; fi
say "Installing the colourMatik effect..."
if ./install-effect.sh; then ok "Effect installed"; else warn "Effect step needs admin - re-run and enter your Mac password."; fi

# 7) Auto-start the engine (LaunchAgent with real paths)
say "Making the engine start automatically at login..."
PLIST="$HOME/Library/LaunchAgents/com.colourmatik.engine.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.colourmatik.engine</string>
  <key>ProgramArguments</key><array>
    <string>$INSTALL_DIR/.venv/bin/python</string><string>-u</string><string>-m</string><string>colourmatik.webapp</string>
  </array>
  <key>WorkingDirectory</key><string>$INSTALL_DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$BREW_PREFIX/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string><key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/colourmatik-engine.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/colourmatik-engine.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/com.colourmatik.engine" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST" 2>/dev/null || true
ok "Engine will run in the background from now on"

say "All done!"
echo "   Final step: RESTART Premiere Pro, then open  Window > UXP Plugins > colourMatik."
echo "   Pick a REFERENCE clip and a TARGET clip, choose Accurate or Cinematic AI, and Match & Apply."
pause
