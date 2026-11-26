#!/usr/bin/env bash
# Build the engine's own Python for the Mac installer: a relocatable CPython
# (python-build-standalone) with the engine's packages already in it, so an
# install needs no Python download and no pip - it works offline, in seconds.
#
#   ./mac/app/build-runtime.sh <out-dir> [sign]
#
# Writes <out-dir>/runtime.zip (unpacks to runtime/) and prints its RUNTIME_ID.
# Apple Silicon only: Intel Macs keep the online path (python.org + pip) that
# setup.sh falls back to when there is no runtime for this Mac.
#
# "sign": every Mach-O file is signed with Developer ID (hardened runtime for
# the executables, secure timestamp) - notarization inspects the zip inside the
# installer app and refuses any unsigned binary in it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:?usage: build-runtime.sh <out-dir> [sign]}"
MODE="${2:-unsigned}"
APP_IDENTITY="Developer ID Application: Sevki Bugra Ozbek (PCH6L56487)"

# Pinned: a release at least two weeks old when it was chosen, and its checksum.
PBS_TAG="20261110"
PBS_FILE="cpython-3.11.18+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_SHA="3afb0567b2a2b30b3a7341bd143cb4e8a69f3d8e95d4641218eaa45f0fabbd07"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-3.11.18%2B${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"

CACHE="$ROOT/dist/cache"; mkdir -p "$CACHE" "$OUT"
TGZ="$CACHE/$PBS_FILE"
if [ ! -f "$TGZ" ] || [ "$(shasum -a 256 "$TGZ" | awk '{print $1}')" != "$PBS_SHA" ]; then
  echo "==> Downloading $PBS_FILE"
  curl -fsSL "$PBS_URL" -o "$TGZ.part"
  mv "$TGZ.part" "$TGZ"
fi
[ "$(shasum -a 256 "$TGZ" | awk '{print $1}')" = "$PBS_SHA" ] || { echo "  x checksum mismatch for $PBS_FILE"; exit 1; }

WORK="$OUT/runtime-build"; rm -rf "$WORK"; mkdir -p "$WORK"
tar -xzf "$TGZ" -C "$WORK"
RT="$WORK/runtime"; mv "$WORK/python" "$RT"
PY="$RT/bin/python3.11"

echo "==> Installing the engine's packages into it"
"$PY" -m pip install --quiet --disable-pip-version-check --no-warn-script-location \
  --only-binary=:all: -r "$ROOT/requirements.txt"
"$PY" -c 'import fastapi, uvicorn, numpy, scipy, cv2, colour, imageio, imageio_ffmpeg, PIL, certifi, multipart' \
  || { echo "  x the runtime cannot import the engine's packages"; exit 1; }

echo "==> Leaving out what the engine never runs"
L="$RT/lib/python3.11"; SP="$L/site-packages"
find "$SP" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +
rm -rf "$L/test" "$L/idlelib" "$L/tkinter" "$L/turtledemo" "$L/lib2to3" "$L"/lib-dynload/_tkinter* \
       "$RT"/lib/tcl* "$RT"/lib/tk* "$RT"/lib/itcl* "$RT"/lib/thread* "$RT"/lib/libtcl* "$RT"/lib/libtk* \
       "$RT/share" "$SP/colour/htmlcov"   # colour-science ships a 75 MB coverage report
find "$RT" -name "__pycache__" -type d -prune -exec rm -rf {} +
# compiled once here, so the first engine start does not write them
"$PY" -m compileall -q -j 0 "$L" >/dev/null 2>&1 || true

REQ_SHA="$(shasum -a 256 "$ROOT/requirements.txt" | awk '{print substr($1,1,12)}')"
RUNTIME_ID="cpython-3.11.18+${PBS_TAG}-aarch64-apple-darwin|req-${REQ_SHA}|1"
echo "$RUNTIME_ID" > "$RT/RUNTIME_ID"

if [ "$MODE" = "sign" ]; then
  echo "==> Signing every binary (Developer ID)"
  ENT="$WORK/python.entitlements"
  # An interpreter: packages a user adds later (the opt-in AI extras) are not
  # signed by us, and ctypes/cffi need writable code pages.
  cat > "$ENT" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
</dict></plist>
XML
  n=0
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q "Mach-O"; then
      if file -b "$f" | grep -q "executable"; then
        codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$APP_IDENTITY" "$f"
      else
        codesign --force --timestamp --sign "$APP_IDENTITY" "$f"
      fi
      n=$((n + 1))
    fi
  done < <(find "$RT" -type f -print0)
  echo "  signed $n binaries"
fi

"$RT/bin/python3" -c 'import fastapi, numpy, scipy, cv2, colour, imageio_ffmpeg' \
  || { echo "  x the runtime does not start after signing"; exit 1; }
rm -f "$OUT/runtime.zip"
( cd "$WORK" && /usr/bin/ditto -c -k --keepParent runtime "$OUT/runtime.zip" )
rm -rf "$WORK"
echo "  runtime.zip: $(du -h "$OUT/runtime.zip" | awk '{print $1}')  ($RUNTIME_ID)"
