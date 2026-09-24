#!/usr/bin/env bash
# colourMatik — install the native "colourMatik" effect (applies the match, with a
# built-in Intensity slider) into Premiere Pro / After Effects. After running,
# RESTART the host app; it appears under Video Effects ▸ colourMatik ▸ colourMatik.
# macOS / Apple Silicon. Uses admin (sudo) only if the plug-ins folder isn't writable.
# NOT `set -e`: a panel-driven update has no terminal, so sudo cannot prompt.
# Aborting on the first non-writable plug-ins folder used to skip everything
# after it — including the per-user After Effects panel, which needs no admin
# at all. Every step is now independent and the admin ones are gated.
set -uo pipefail
# "Can we get admin?" is not "is sudo already cached" — a user running this in
# Terminal CAN be prompted, and testing only `sudo -n` silently skipped the
# effect for them (a regression against the previous behaviour). Ask whether a
# prompt is possible at all: cached credentials OR an interactive terminal.
CAN_SUDO=0
if sudo -n true 2>/dev/null || [ -t 0 ]; then CAN_SUDO=1; fi
CM_SKIPPED=""
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/colourmatik-fx/colourMatik.plugin"
# After Effects gets a variant with a DIFFERENT effect match name, so AE — which
# scans both MediaCore and its own folder — doesn't flag the two as a "duplicated
# effect plugin". The display name stays "colourMatik" in both hosts.
AESRC="$DIR/colourmatik-fx/colourMatik-ae.plugin"; [ -d "$AESRC" ] || AESRC="$SRC"
DEST="/Library/Application Support/Adobe/Common/Plug-ins/7.0/MediaCore/colourMatik.plugin"
DESTDIR="$(dirname "$DEST")"

[ -d "$SRC" ] || { echo "Built plugin not found at $SRC"; exit 1; }

# Copy a .plugin bundle to its destination WITHOUT ever nesting it. `cp -R src dest`
# copies src INTO dest when dest still exists — and dest does survive `rm -rf`
# whenever its parent folder is root-owned (the After Effects Plug-Ins folder on a
# machine where an earlier install ran with admin rights): rm empties the bundle
# but cannot remove the folder itself, cp then produces
# colourMatik.plugin/colourMatik-ae.plugin/Contents, and After Effects loads
# nothing. ditto merges the bundle's CONTENTS into the destination, so the
# layout is right whether or not the folder could be removed; the result is
# checked before it is called installed.
# The effect binaries change rarely; most updates only touch the engine. An
# identical installed bundle needs nothing - and no admin rights - so the
# panel's one-click update stays silent instead of reporting "needs admin".
bundle_current() {   # bundle_current <src.plugin> <dest.plugin>
    [ -d "$2/Contents/MacOS" ] && diff -rq "$1" "$2" >/dev/null 2>&1
}

install_bundle() {   # install_bundle <src.plugin> <dest.plugin>
    local src="$1" dest="$2"
    $SUDO rm -rf "$dest" 2>/dev/null || true
    if [ -d "$dest" ]; then
        # could not remove the folder itself: at least empty it
        $SUDO find "$dest" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
    fi
    if ! $SUDO /usr/bin/ditto "$src" "$dest" 2>/dev/null; then
        echo "  could not write $dest (needs admin rights)"
        return 1
    fi
    if [ ! -d "$dest/Contents/MacOS" ]; then
        echo "  $dest is not a valid plug-in bundle after the copy"
        return 1
    fi
    return 0
}

# Prefer no-sudo; fall back to sudo if the shared MediaCore folder needs admin.
SUDO=""
if bundle_current "$SRC" "$DEST"; then
    echo "Effect (Premiere) already current → $DEST"
else
    if ! mkdir -p "$DESTDIR" 2>/dev/null || [ ! -w "$DESTDIR" ]; then
        echo "The Adobe plug-ins folder needs admin rights — you'll be asked for your Mac password."
        if [ "$CAN_SUDO" = "1" ]; then SUDO="sudo"; else
          echo "  (skipping: needs admin and no password prompt is possible here)"
          SUDO=""; CM_SKIPPED="yes"
        fi
        $SUDO mkdir -p "$DESTDIR"
    fi
    install_bundle "$SRC" "$DEST" || CM_SKIPPED="yes"
    $SUDO xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
    # Only adhoc-sign as a fallback if the copy has no valid signature at all (e.g. a
    # locally hand-built plugin). A shipped Developer-ID/notarized build is left untouched.
    if ! codesign --verify "$DEST" >/dev/null 2>&1; then
        $SUDO codesign --force --sign - --timestamp=none "$DEST" >/dev/null 2>&1 || true
    fi
    echo "Installed (Premiere) → $DEST"
fi

# After Effects does NOT load effects from the shared MediaCore folder — only from
# its OWN Plug-Ins folder. Install a copy there too, for every AE version present,
# or the effect shows in Premiere but is invisible in After Effects.
shopt -s nullglob
for AEAPP in /Applications/Adobe\ After\ Effects\ *; do
    AEPLUG="$AEAPP/Plug-Ins"
    [ -d "$AEPLUG" ] || continue
    AEDEST="$AEPLUG/colourMatik/colourMatik.plugin"
    if bundle_current "$AESRC" "$AEDEST"; then
        echo "Effect (After Effects) already current → $AEDEST"
        continue
    fi
    if [ ! -w "$AEPLUG" ] && [ -z "$SUDO" ]; then
        echo "After Effects plug-ins folder needs admin — you may be asked for your password."
        if [ "$CAN_SUDO" = "1" ]; then SUDO="sudo"; else
      echo "  (skipping: needs admin and no password prompt is possible here)"
      SUDO=""; CM_SKIPPED="yes"
    fi
    fi
    $SUDO mkdir -p "$AEPLUG/colourMatik"
    if install_bundle "$AESRC" "$AEDEST"; then
        $SUDO xattr -dr com.apple.quarantine "$AEPLUG/colourMatik" 2>/dev/null || true
        echo "Installed effect (After Effects) → $AEDEST"
    else
        CM_SKIPPED="yes"
    fi
    # remove the deprecated ScriptUI panel if a previous version installed it
    $SUDO rm -f "$AEAPP/Scripts/ScriptUI Panels/colourMatik.jsx" 2>/dev/null || true
done

# AE Match & Apply panel (CEP — full HTML, identical to the Premiere panel).
# Per-user, no admin: Window ▸ Extensions ▸ colourMatik.
if [ -d "$DIR/colourmatik-cep" ]; then
    CEPDEST="$HOME/Library/Application Support/Adobe/CEP/extensions/com.catheadai.colourmatik"
    rm -rf "$CEPDEST"; mkdir -p "$CEPDEST"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude='.DS_Store' "$DIR/colourmatik-cep/" "$CEPDEST/"
    else
        cp -R "$DIR/colourmatik-cep/." "$CEPDEST/"
    fi
    # allow the (unsigned) extension to load; harmless if already set
    for v in 9 10 11 12; do defaults write com.adobe.CSXS.$v PlayerDebugMode 1 2>/dev/null || true; done
    killall cfprefsd 2>/dev/null || true
    echo "Installed AE panel (CEP) → Window ▸ Extensions ▸ colourMatik"
fi

# The AE panel talks to the local engine via curl, which needs AE's "Allow Scripts
# to Write Files and Access Network" preference. A running script can't flip it
# (security), so we write it straight into each AE version's prefs — identical to
# ticking the checkbox. AE must be closed (it is during a normal install).
for PF in "$HOME/Library/Preferences/Adobe/After Effects/"*/"Adobe After Effects "*" Prefs.txt"; do
    [ -f "$PF" ] || continue
    if grep -q '"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "0"' "$PF" 2>/dev/null; then
        sed -i '' 's/"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "0"/"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "1"/' "$PF"
        echo "Enabled scripting/network for $(basename "$(dirname "$PF")")"
    fi
done

echo "Restart Premiere Pro / After Effects, then find it under Effects ▸ colourMatik ▸ colourMatik."

# After Effects blocks scripts from writing files / reaching the network until
# this preference is on. The Windows installer already flips it; without it the
# AE panel cannot render frames for precomps, solids, text or shape layers and
# reports a confusing "cannot create colourMatik/aeframes".
for pref in "$HOME/Library/Preferences/Adobe/After Effects"/*/ ; do
  [ -d "$pref" ] || continue
  f="$pref/Adobe After Effects $(basename "$pref") Prefs-indep-general.txt"
  [ -f "$f" ] || continue
  if grep -q 'Pref_SCRIPTING_FILE_NETWORK_SECURITY' "$f" 2>/dev/null; then
    /usr/bin/sed -i '' 's/"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "0"/"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "1"/' "$f" 2>/dev/null || true
  else
    printf '\n["Main Pref Section"]\n\t"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "1"\n' >> "$f" 2>/dev/null || true
  fi
  echo "Allowed AE scripts to write files / access the network ($(basename "$pref"))"
done

if [ -n "$CM_SKIPPED" ]; then
  echo ""
  echo "NOT EVERYTHING WAS INSTALLED: some folders needed admin rights and no"
  echo "password could be asked for here. Run this in Terminal to finish:"
  echo "  \"$DIR/install-effect.sh\""
  exit 1
fi
