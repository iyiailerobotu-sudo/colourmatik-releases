#!/bin/bash
# colourMatik macOS installer — launched (as root) by "colourMatik Installer.app".
# Downloads the latest colourMatik and sets up EVERYTHING: local engine + AI, the
# Premiere panel, the native effect, and login autostart. Runs headless; the app
# shows a live progress bar by reading the PROGRESS file this script writes
# ("PCT|CAP|message" per stage; "100|100|done" on success, "FAIL|0|reason" on error).
#
# It is run as root (the app asks for the admin password once), then drops to the
# logged-in user for all user-owned steps via `asuser`.
set -u
LOG="/tmp/colourMatik-install.log"
PROGRESS="/tmp/colourMatik-progress"
exec > >(tee -a "$LOG") 2>&1
echo "== colourMatik install $(date) =="

# 666: we write as root but setup.sh refines progress as the logged-in user —
# the user must be able to write this file too, or their redirect fails.
prog() { echo "$1|$2|$3" > "$PROGRESS"; chmod 666 "$PROGRESS" 2>/dev/null || true; }

# If this script dies ANYWHERE unexpectedly, surface it on the progress bar —
# the app must never sit on a frozen bar with nothing actually running.
on_exit() {
  code=$?
  if [ "$code" -ne 0 ] && ! /usr/bin/grep -q '^FAIL' "$PROGRESS" 2>/dev/null; then
    prog FAIL 0 "install stopped unexpectedly (code $code) — see /tmp/colourMatik-install.log"
  fi
}
trap on_exit EXIT

prog 2 5 "Starting…"

CONSOLE_USER="$(/usr/bin/stat -f%Su /dev/console)"
{ [ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "root" ]; } && CONSOLE_USER="$(/usr/bin/ls -l /dev/console | awk '{print $3}')"
USER_UID="$(/usr/bin/id -u "$CONSOLE_USER")"
USER_HOME="$(/usr/bin/dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory | awk '{print $2}')"
DEST="$USER_HOME/colourMatik"
echo "user=$CONSOLE_USER uid=$USER_UID home=$USER_HOME"

# run a command as the logged-in user, inside their GUI session
asuser() { /bin/launchctl asuser "$USER_UID" /usr/bin/sudo -u "$CONSOLE_USER" "$@"; }
notify() { asuser /usr/bin/osascript -e "display notification \"$1\" with title \"colourMatik\"" >/dev/null 2>&1 || true; }
fail()   { echo "FAIL: $1"; prog FAIL 0 "$1"; notify "Install failed — $1"; exit 1; }

# 0) download the latest source (as the user), unzip
prog 5 10 "Downloading colourMatik…"
echo "Downloading colourMatik…"
SRC="$(asuser /usr/bin/mktemp -d "/tmp/colourMatik-src.XXXXXX")"
asuser /usr/bin/curl -fsSL "https://github.com/iyiailerobotu-sudo/colourmatik-releases/archive/refs/heads/main.zip" -o "$SRC/main.zip" || fail "download failed"
asuser /usr/bin/ditto -x -k "$SRC/main.zip" "$SRC" || fail "unzip failed"
# GitHub names the archive's top folder "<repo>-<branch>": colourmatik-releases-main
# since the move to the new repo. Bash globs are case-SENSITIVE even on a
# case-insensitive disk, so the old "colourMatik-*" pattern matched nothing and
# every install stopped at "extract failed". Take the one folder the zip holds.
INNER="$(/bin/ls -d "$SRC"/*/ 2>/dev/null | /usr/bin/head -1)"; INNER="${INNER%/}"
[ -z "$INNER" ] && fail "extract failed"
echo "source: $INNER"

# 1) place the code in ~/colourMatik (user-owned)
prog 10 13 "Preparing files…"
/bin/rm -rf "$DEST"; /bin/mkdir -p "$DEST"
/usr/bin/ditto "$INNER" "$DEST"
/usr/sbin/chown -R "$CONSOLE_USER" "$DEST"
/usr/bin/xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

# 2) Python 3.11+ — the ONLY prerequisite. If missing, install the official
#    python.org package: we are root, so `installer` runs silently and
#    deterministically. No Homebrew, no git, no Xcode tools, no system ffmpeg
#    (ffmpeg is bundled via the imageio-ffmpeg pip package; CanonCGT comes as a zip).
have_py() {
  for c in python3.11 \
           /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
           /usr/local/bin/python3.11 /opt/homebrew/bin/python3.11 python3; do
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      return 0
    fi
  done
  return 1
}
if ! have_py; then
  prog 13 30 "Installing Python (official package)…"
  echo "Installing Python 3.11 from python.org…"
  PYPKG="/tmp/colourMatik-python311.pkg"
  /usr/bin/curl -fsSL "https://www.python.org/ftp/python/3.11.9/python-3.11.9-macos11.pkg" -o "$PYPKG" || fail "Python download failed"
  /usr/sbin/installer -pkg "$PYPKG" -target / || fail "Python install failed"
  /bin/rm -f "$PYPKG"
fi
have_py || fail "Python 3.11 not available after install"

# 3) Full engine setup + AI (as the user). setup.sh finds Python itself and
#    refines progress (32 -> 84) via COLOURMATIK_PROGRESS.
PYPATHS="/Library/Frameworks/Python.framework/Versions/3.11/bin:/usr/local/bin:/opt/homebrew/bin"
prog 32 42 "Setting up the engine…"
echo "Running engine setup (venv, deps, AI models)…"
asuser /bin/bash -c "cd '$DEST' && COLOURMATIK_PROGRESS='$PROGRESS' PATH=\"$PYPATHS:\$PATH\" ./setup.sh" || fail "engine setup failed"
prog 86 90 "Installing the Premiere panel…"
echo "Installing the Premiere panel…"
asuser /bin/bash -c "cd '$DEST' && PATH=\"$PYPATHS:\$PATH\" ./install-panel.sh" || echo "WARN: panel install returned non-zero"

# 4) native effect -> Premiere (shared MediaCore) AND After Effects (its own
#    Plug-Ins folder — AE does NOT load effects from MediaCore). We are root here.
prog 90 94 "Installing the colourMatik effect…"
FX="$DEST/colourmatik-fx/colourMatik.plugin"
if [ -d "$FX" ]; then
  MC="/Library/Application Support/Adobe/Common/Plug-ins/7.0/MediaCore"
  /bin/mkdir -p "$MC"
  /bin/rm -rf "$MC/colourMatik.plugin"
  /usr/bin/ditto "$FX" "$MC/colourMatik.plugin"
  /usr/bin/xattr -dr com.apple.quarantine "$MC/colourMatik.plugin" 2>/dev/null || true
  echo "Effect installed for Premiere (MediaCore)."
  # every installed After Effects version -> its own Plug-Ins/colourMatik/
  # AE gets the distinct-match-name variant so AE doesn't see MediaCore + its own
  # copy as a "duplicated effect plugin" (display name stays "colourMatik").
  FXAE="$DEST/colourmatik-fx/colourMatik-ae.plugin"; [ -d "$FXAE" ] || FXAE="$FX"
  for AEAPP in /Applications/Adobe\ After\ Effects\ *; do
    AEPLUG="$AEAPP/Plug-Ins"
    [ -d "$AEPLUG" ] || continue
    /bin/mkdir -p "$AEPLUG/colourMatik"
    /bin/rm -rf "$AEPLUG/colourMatik/colourMatik.plugin"
    /usr/bin/ditto "$FXAE" "$AEPLUG/colourMatik/colourMatik.plugin"
    /usr/bin/xattr -dr com.apple.quarantine "$AEPLUG/colourMatik" 2>/dev/null || true
    echo "Effect installed for After Effects -> $AEPLUG/colourMatik"
    # remove the deprecated ScriptUI panel from older installs
    /bin/rm -f "$AEAPP/Scripts/ScriptUI Panels/colourMatik.jsx" 2>/dev/null || true
  done
  # AE Match & Apply panel (CEP — full HTML, identical to the Premiere panel).
  # Per-user (no admin needed): Window ▸ Extensions ▸ colourMatik.
  if [ -d "$DEST/colourmatik-cep" ]; then
    CEPDEST="$USER_HOME/Library/Application Support/Adobe/CEP/extensions/com.catheadai.colourmatik"
    /bin/rm -rf "$CEPDEST"
    asuser /bin/mkdir -p "$CEPDEST"
    /usr/bin/ditto "$DEST/colourmatik-cep" "$CEPDEST"
    /usr/sbin/chown -R "$CONSOLE_USER" "$CEPDEST"
    /usr/bin/xattr -dr com.apple.quarantine "$CEPDEST" 2>/dev/null || true
    /usr/bin/find "$CEPDEST" -name ".DS_Store" -delete 2>/dev/null || true
    # allow the (unsigned) extension to load — write as the USER, then flush prefs
    asuser /bin/bash -c 'for v in 9 10 11 12; do defaults write com.adobe.CSXS.$v PlayerDebugMode 1; done' 2>/dev/null || true
    /usr/bin/killall cfprefsd 2>/dev/null || true
    echo "AE panel (CEP) installed -> Window / Extensions / colourMatik"
  fi
fi

# 5) start the engine at login (LaunchAgent) + now
prog 94 99 "Starting the engine…"
if [ -d "$DEST/.venv" ]; then
  PLIST="$USER_HOME/Library/LaunchAgents/com.colourmatik.engine.plist"
  asuser /bin/mkdir -p "$USER_HOME/Library/LaunchAgents" "$USER_HOME/Library/Logs"
  /bin/cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.colourmatik.engine</string>
  <key>ProgramArguments</key><array>
    <string>$DEST/.venv/bin/python</string><string>-u</string><string>-m</string><string>colourmatik.webapp</string>
  </array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PYPATHS:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string><key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$USER_HOME/Library/Logs/colourmatik-engine.log</string>
  <key>StandardErrorPath</key><string>$USER_HOME/Library/Logs/colourmatik-engine.log</string>
</dict></plist>
PL
  /usr/sbin/chown "$CONSOLE_USER" "$PLIST"
  asuser /bin/launchctl bootout "gui/$USER_UID/com.colourmatik.engine" 2>/dev/null || true
  asuser /bin/launchctl bootstrap "gui/$USER_UID" "$PLIST" 2>/dev/null || \
  asuser /bin/launchctl load -w "$PLIST" 2>/dev/null || true
  # "configured" is not "running": both launchctl forms can fail silently, and
  # even a healthy engine needs ~30s of imports before it serves. Verify by
  # ASKING it, fall back to a direct start, and never claim success blind —
  # a reinstall that killed the service and could not restart it is exactly the
  # "everything is broken and slow" report from the field.
  ENGINE_OK=""
  for i in $(seq 1 45); do
    if /usr/bin/curl -s --max-time 2 "http://127.0.0.1:8765/version" >/dev/null 2>&1; then
      ENGINE_OK=1; break
    fi
    [ "$i" = "15" ] && asuser /bin/launchctl kickstart "gui/$USER_UID/com.colourmatik.engine" 2>/dev/null || true
    [ "$i" = "25" ] && asuser /bin/sh -c "cd '$DIR' && nohup ./.venv/bin/python -u -m colourmatik.webapp >> '$HOME_DIR/Library/Logs/colourmatik-engine.log' 2>&1 &" 2>/dev/null || true
    sleep 2
  done
  if [ -n "$ENGINE_OK" ]; then
    echo "Engine is up and answering."
  else
    echo "WARNING: the engine did not answer on 127.0.0.1:8765 — open the panel; it can take a minute after login."
  fi
fi

# clean up the download
/bin/rm -rf "$SRC" 2>/dev/null || true

echo "== colourMatik install done $(date) =="
prog 100 100 "done"
notify "colourMatik is ready! Restart Premiere Pro."
exit 0
