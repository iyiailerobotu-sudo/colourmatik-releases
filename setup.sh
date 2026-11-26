#!/usr/bin/env bash
# colourMatik — one-shot setup. Creates the Python venv and installs the fast
# classical engine. Re-run any time; it is idempotent.
# Needs ONLY Python 3.11+ — no Homebrew, no git, no system ffmpeg (ffmpeg is
# bundled via the imageio-ffmpeg pip package).
#
#   ./setup.sh          # fast classical engine (the product default)
#   ./setup.sh --ai     # optional: heavy local-AI extras (GBs; not required)
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Optional progress reporting: when COLOURMATIK_PROGRESS is set to a file path,
# write "PCT|CAP|message" stage markers there (read by the installer progress bar).
# A failed write must NEVER kill the setup (set -e) — progress is cosmetic.
prog() { { [ -n "${COLOURMATIK_PROGRESS:-}" ] && echo "$1|$2|$3" > "$COLOURMATIK_PROGRESS"; } 2>/dev/null || true; }

# A freshly installed engine is slow the first time it runs: macOS checks every
# new library when it is first loaded (the first match after an install took
# 37 s, every later one 4). Run one tiny match here, under the installer's
# progress bar, instead of in the user's first match.
WARMUP='import numpy as np, subprocess, imageio_ffmpeg, cv2
import colourmatik.webapp
from colourmatik.match import match
r = np.random.default_rng(0)
match(r.random((48, 64, 3)), r.random((48, 64, 3)), corresponded=False, size=17)
subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-version"], capture_output=True)'
warm_up() {
  prog 42 44 "Preparing the engine (first run)…"
  echo "==> Preparing the engine (first run)"
  ./.venv/bin/python -c "$WARMUP" >/dev/null 2>&1 || true
}

# The engine's own Python, when the installer carried one (runtime/, Apple
# Silicon): the packages are already in it, so nothing is downloaded and pip
# never runs. .venv is a thin layer over it (--system-site-packages), rebuilt
# whenever the runtime changes; everything else keeps calling .venv/bin/python.
RT="$DIR/runtime"
ENGINE_IMPORTS='import fastapi, uvicorn, numpy, scipy, cv2, colour, imageio_ffmpeg, PIL'
if [ -x "$RT/bin/python3" ] && "$RT/bin/python3" -c "$ENGINE_IMPORTS" 2>/dev/null; then
  WANT="$(cat "$RT/RUNTIME_ID" 2>/dev/null || echo runtime)"
  HAVE="$(cat .venv/RUNTIME_ID 2>/dev/null || echo none)"
  if [ "$WANT" != "$HAVE" ] || ! ./.venv/bin/python -c "$ENGINE_IMPORTS" 2>/dev/null; then
    prog 32 40 "Creating the engine environment…"
    echo "==> Creating .venv on the bundled runtime ($WANT)"
    # Built beside the old one and swapped in only when it starts: a failed
    # build must never leave no environment. A running engine keeps the files
    # it has open; the restarted one starts on the new venv.
    rm -rf .venv.new
    "$RT/bin/python3" -m venv --system-site-packages .venv.new
    ./.venv.new/bin/python -c "$ENGINE_IMPORTS" 2>/dev/null \
      || { rm -rf .venv.new; echo "ERROR: the engine environment on the bundled runtime does not start."; exit 1; }
    echo "$WANT" > .venv.new/RUNTIME_ID
    rm -rf .venv && mv .venv.new .venv
  fi
  ./.venv/bin/python -c "$ENGINE_IMPORTS" 2>/dev/null \
    || { echo "ERROR: the engine environment on the bundled runtime does not start."; exit 1; }
  echo "==> Engine environment ready (bundled runtime, nothing downloaded)."
  warm_up
  [ "${1:-}" = "--ai" ] || exit 0
fi

# Find Python 3.11+: $PYTHON override, PATH, python.org framework, Homebrew.
find_py() {
  for c in "${PYTHON:-}" python3.11 python3.12 python3.13 \
           /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
           /usr/local/bin/python3.11 /opt/homebrew/bin/python3.11 python3; do
    [ -n "$c" ] || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_py)" || { echo "ERROR: Python 3.11+ not found."; exit 1; }

prog 32 37 "Creating the engine environment…"
echo "==> Creating virtualenv (.venv) with $PY"
# a venv left on a removed runtime cannot start: start it over
if [ -f .venv/RUNTIME_ID ] && [ ! -x "$RT/bin/python3" ]; then rm -rf .venv; fi
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip

prog 37 42 "Installing the engine…"
echo "==> Installing base engine deps"
./.venv/bin/python -m pip install --quiet -r requirements.txt

# The heavy local-AI stack (PyTorch, transformers, CanonCGT weights - GBs of
# downloads and minutes of install) is OPT-IN now. The default engine is the
# fast classical core; nothing below downloads anything.
if [ "${1:-}" = "--ai" ]; then
    prog 42 72 "Downloading the AI engine (a few GB)..."
    ./.venv/bin/python -m pip install --quiet -r requirements-ai.txt
    mkdir -p vendor
    if [ ! -d vendor/CanonCGT ]; then
        curl -fsSL "https://github.com/Jinwon-Ko/CanonCGT/archive/refs/heads/main.zip" -o vendor/_canoncgt.zip
        ( cd vendor && unzip -o -q _canoncgt.zip && rm -f _canoncgt.zip && mv CanonCGT-main CanonCGT )
    fi
    # The weights are what make CanonCGT actually load — without this step the
    # multi-GB opt-in silently degrades to classical forever.
    mkdir -p vendor/CanonCGT/pretrained
    W="vendor/CanonCGT/pretrained/SSL_updated_251111.pth"
    if [ ! -s "$W" ] || [ "$(wc -c < "$W" 2>/dev/null || echo 0)" -lt 1000000 ]; then
        ./.venv/bin/gdown "1SqzCXjdJ95TAhDYY9Z4TaQPuoqlEyfkT" -O vendor/CanonCGT/pretrained/_dl.zip
        ( cd vendor/CanonCGT/pretrained && unzip -o -q _dl.zip && rm -f _dl.zip )
    fi
fi

warm_up
echo "==> Done. Start the engine with:  ./colourmatik-app"
